"""Bounded music jobs, shared bandwidth budget and persistent Telegram reuse."""
import asyncio
import hashlib
import itertools
import logging
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from telegram.error import BadRequest, RetryAfter

from utils import music_db
from utils.music_fetch import _load_music_cfg, download_audio

log = logging.getLogger("music_service")


class MusicBusy(RuntimeError):
    pass


def media_key(url):
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    video = None
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        video = parse_qs(parsed.query).get("v", [None])[0]
        if not video and parsed.path.startswith(("/shorts/", "/embed/")):
            video = parsed.path.split("/")[2]
    elif host == "youtu.be":
        video = parsed.path.strip("/").split("/")[0]
    canonical = "youtube:" + video if video else url.strip().split("#", 1)[0]
    return hashlib.sha256(canonical.encode()).hexdigest()


class MusicService:
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or _load_music_cfg())
        self.concurrency = max(1, min(8, int(self.cfg["concurrency"])))
        self.queue = asyncio.PriorityQueue(maxsize=int(self.cfg["queue_size"]))
        self.upload_slots = asyncio.Semaphore(1)
        self.sequence = itertools.count()
        self.users = set()
        self.workers = []
        self.locks = {}
        self.closed = False

    def start(self):
        if not self.workers:
            self.workers = [asyncio.create_task(self._worker(), name=f"music-worker-{i}")
                            for i in range(self.concurrency)]
            log.info("music workers=%d queue=%d download_budget=%d upload_slots=1",
                     self.concurrency, self.queue.maxsize, self.cfg["download_budget_bps"])

    def submit(self, user_id, operation, *, priority=0):
        if self.closed:
            raise MusicBusy("音乐服务正在重启，请稍后再试。")
        if user_id in self.users:
            raise MusicBusy("你已有一个音乐任务，请等待完成后再试。")
        if self.queue.full():
            raise MusicBusy("点歌队列已满，请稍后再试。")
        future = asyncio.get_running_loop().create_future()
        # Retrieve failures even for fire-and-forget handlers; awaiting still raises.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        self.users.add(user_id)
        self.queue.put_nowait((priority, next(self.sequence), time.monotonic(),
                              user_id, operation, future))
        return future

    async def _worker(self):
        while True:
            _, _, queued, user_id, operation, future = await self.queue.get()
            start = time.monotonic()
            try:
                if not future.cancelled():
                    result = await operation()
                    if not future.done():
                        future.set_result(result)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                log.warning("music task failed type=%s", type(exc).__name__)
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.users.discard(user_id)
                self.queue.task_done()
                log.info("music task wait_sec=%.2f work_sec=%.2f queue=%d",
                         start - queued, time.monotonic() - start, self.queue.qsize())

    async def close(self):
        self.closed = True
        try:
            await asyncio.wait_for(self.queue.join(), timeout=30)
        except asyncio.TimeoutError:
            pass
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        while not self.queue.empty():
            *_, user_id, operation, future = self.queue.get_nowait()
            self.users.discard(user_id)
            future.cancel()
            self.queue.task_done()

    @asynccontextmanager
    async def _media_lock(self, key):
        lock, refs = self.locks.get(key, (asyncio.Lock(), 0))
        self.locks[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            _, refs = self.locks[key]
            if refs == 1:
                del self.locks[key]
            else:
                self.locks[key] = (lock, refs - 1)

    async def _send(self, bot, audio, **kwargs):
        async with self.upload_slots:
            for attempt in range(3):
                if hasattr(audio, "seek"):
                    audio.seek(0)
                try:
                    return await bot.send_audio(audio=audio, read_timeout=60,
                                                write_timeout=120, **kwargs)
                except RetryAfter as exc:
                    if attempt == 2:
                        raise
                    delay = exc.retry_after
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    log.warning("music Telegram rate limit retry_sec=%.1f", float(delay))
                    await asyncio.sleep(float(delay) + 1)

    async def send_audio(self, bot, url, **kwargs):
        # Include bot identity and encoding profile: Telegram file IDs are bot-specific.
        key = f"{bot.id}:{media_key(url)}:{self.cfg['audio_format']}:{self.cfg['audio_quality']}"
        async with self._media_lock(key):
            cached = music_db.get_audio_cache(key)
            if cached:
                try:
                    msg = await self._send(bot, cached["file_id"], **kwargs)
                    log.info("music cache_hit=1")
                    return msg, cached
                except BadRequest as exc:
                    if not any(x in str(exc).lower() for x in
                               ("file identifier", "file_id", "wrong file", "file reference", "file_reference")):
                        raise
                    music_db.delete_audio_cache(key)

            with tempfile.TemporaryDirectory(prefix="mayabot_music_") as tmp:
                cancel = threading.Event()
                cfg = dict(self.cfg)
                cfg["download_rate_bps"] = max(1, int(cfg["download_budget_bps"]) // self.concurrency)
                start = time.monotonic()
                task = asyncio.create_task(asyncio.to_thread(
                    download_audio, url, Path(tmp), cfg=cfg, cancel_event=cancel))
                try:
                    info = await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancel.set()
                    # Wait for process-group termination before removing its files.
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                log.info("music download_sec=%.2f bytes=%d", time.monotonic() - start,
                         info["path"].stat().st_size)
                if info["path"].stat().st_size > int(cfg["max_audio_bytes"]):
                    raise RuntimeError("音频超过发送大小限制，请选择较短曲目。")
                metadata = {k: info.get(k) for k in ("title", "artist", "duration")}
                options = dict(kwargs)
                options.update(title=metadata["title"], performer=metadata["artist"] or None,
                               duration=int(metadata["duration"]) if metadata["duration"] else None,
                               filename=info["path"].name)
                start = time.monotonic()
                with info["path"].open("rb") as audio:
                    msg = await self._send(bot, audio, **options)
                log.info("music upload_sec=%.2f cache_hit=0", time.monotonic() - start)
                if msg.audio:
                    metadata["file_id"] = msg.audio.file_id
                    try:
                        music_db.set_audio_cache(key, metadata)
                    except Exception as exc:
                        log.warning("music cache write failed type=%s", type(exc).__name__)
                return msg, metadata


def get_service(application):
    return application.bot_data["music_service"]


async def start_service(application):
    service = MusicService()
    application.bot_data["music_service"] = service
    service.start()


async def stop_service(application):
    await get_service(application).close()
