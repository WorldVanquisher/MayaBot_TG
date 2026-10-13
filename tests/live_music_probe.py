"""Opt-in source download probe. Telegram upload is simulated; never sends messages."""
import argparse
import asyncio
import json
import logging
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from utils import music_db
from utils.music_fetch import _DEFAULTS, MusicRejected
from utils.music_service import MusicService


class SinkBot:
    id = 999999999

    def __init__(self):
        self.uploads = self.reuses = self.bytes = self.active = self.peak = 0

    async def send_audio(self, audio, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if isinstance(audio, str):
                self.reuses += 1
                file_id = audio
            else:
                self.uploads += 1
                self.bytes += len(audio.read())
                file_id = f"probe-{self.uploads}"
            await asyncio.sleep(0.05)
            return SimpleNamespace(audio=SimpleNamespace(file_id=file_id), message_id=1)
        finally:
            self.active -= 1


async def main(args):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with sqlite3.connect(f"file:{args.source_db}?mode=ro", uri=True) as db:
        urls = [row[0] for row in db.execute(
            "SELECT url FROM user_downloads WHERE url<>'' GROUP BY url ORDER BY MAX(id) DESC LIMIT ?",
            (args.workers * 2 + 4,))]
    target = args.workers * 2
    assert len(urls) >= target, "Not enough distinct historical URLs"
    with tempfile.TemporaryDirectory(prefix="music_probe_db_") as tmp:
        music_db.DB_PATH = Path(tmp) / "music.db"
        music_db.init_db()
        service = MusicService(dict(_DEFAULTS, concurrency=args.workers))
        service.start()
        bot = SinkBot()
        active = peak = 0
        elapsed = []
        sizes = []
        async def operation(url):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            start = time.monotonic()
            try:
                result = await service.send_audio(bot, url, chat_id=0)
                elapsed.append(round(time.monotonic() - start, 2))
                sizes.append(result[1].get("duration"))
                return result
            finally:
                active -= 1
        start = time.monotonic()
        try:
            successful, failures, rejected = [], [], 0
            cursor = 0
            while len(successful) < target and cursor < len(urls) and not failures:
                batch = urls[cursor:cursor + target - len(successful)]
                results = await asyncio.gather(*(service.submit(cursor + i, lambda u=url: operation(u))
                                                for i, url in enumerate(batch)), return_exceptions=True)
                cursor += len(batch)
                for url, result in zip(batch, results):
                    if isinstance(result, MusicRejected):
                        rejected += 1
                    elif isinstance(result, BaseException):
                        failures.append(type(result).__name__)
                        print("probe_failure", re.sub(r"https?://\S+", "<url>", str(result))[-500:], flush=True)
                    else:
                        successful.append(url)
            cold_bytes = bot.bytes
            cold_seconds = round(time.monotonic() - start, 2)
            if not failures:
                await asyncio.gather(*(service.submit(100 + i, lambda u=url: operation(u))
                                       for i, url in enumerate(successful)))
            metrics = {"workers": args.workers, "cold_jobs": len(successful),
                       "source_attempts": cursor, "rejected_by_policy": rejected,
                       "failures": failures, "cold_seconds": cold_seconds,
                       "cold_job_seconds": elapsed[:len(successful)], "output_bytes": cold_bytes,
                       "durations": sizes[:len(successful)],
                       "peak_jobs": peak, "peak_uploads": bot.peak,
                       "uploads": bot.uploads, "cache_reuses": bot.reuses,
                       "extra_warm_upload_bytes": bot.bytes - cold_bytes,
                       "telegram_upload": "simulated"}
            for name in ("memory.peak", "memory.max", "cpu.max"):
                path = Path("/sys/fs/cgroup") / name
                if path.exists():
                    metrics[name] = path.read_text().strip()
            print(json.dumps(metrics), flush=True)
            assert not failures, metrics
            assert peak == args.workers and bot.peak == 1, metrics
            assert len(successful) == target, metrics
            assert bot.uploads == target and bot.reuses == target, metrics
        finally:
            await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(2, 4, 8), required=True)
    parser.add_argument("--source-db", required=True)
    asyncio.run(main(parser.parse_args()))
