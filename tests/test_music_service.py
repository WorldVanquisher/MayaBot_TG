import asyncio
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, RetryAfter
from telegram import Bot
from telegram.request import BaseRequest

from utils import music_db
from utils.music_fetch import _DEFAULTS, _run_process
from utils.music_service import MusicBusy, MusicService, media_key
from handlers import music


class FakeBot:
    id = 123

    def __init__(self):
        self.uploads = 0
        self.reuses = 0
        self.active = 0
        self.peak = 0

    async def send_audio(self, audio, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            if isinstance(audio, str):
                self.reuses += 1
                file_id = audio
            else:
                self.uploads += 1
                self.assert_bytes = audio.read()
                file_id = f"cached-{self.uploads}"
            return SimpleNamespace(audio=SimpleNamespace(file_id=file_id), message_id=1)
        finally:
            self.active -= 1


class TelegramTransport(BaseRequest):
    """Exercise real PTB serialization without making HTTP requests."""
    def __init__(self):
        self.sends = []

    @property
    def read_timeout(self):
        return 30

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        if url.endswith("getMe"):
            result = {"id": 123, "is_bot": True, "first_name": "test", "username": "test_bot"}
        else:
            self.sends.append((request_data.multipart_data, request_data.json_parameters, kwargs))
            result = {"message_id": len(self.sends), "date": 1,
                      "chat": {"id": 1, "type": "private"},
                      "audio": {"file_id": "telegram-file", "file_unique_id": "unique",
                                "duration": 10}}
        return 200, json.dumps({"ok": True, "result": result}).encode()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(music_db, "DB_PATH", Path(self.tmp.name) / "music.db")
        self.db_patch.start()
        music_db.init_db()
        self.services = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    def service(self, workers=2, queue_size=16):
        cfg = dict(_DEFAULTS, concurrency=workers, queue_size=queue_size)
        service = MusicService(cfg)
        self.services.append(service)
        return service

    async def test_worker_limits_at_two_and_four(self):
        for workers in (2, 4):
            service = self.service(workers)
            active = peak = 0
            async def operation():
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return True
            jobs = [service.submit(uid, operation) for uid in range(12)]
            service.start()
            self.assertTrue(all(await asyncio.gather(*jobs)))
            self.assertEqual(peak, workers)
            self.assertFalse(service.users)

    async def test_bounded_queue_and_per_user_limit(self):
        service = self.service(queue_size=1)
        job = service.submit(1, lambda: asyncio.sleep(0))
        with self.assertRaises(MusicBusy):
            service.submit(1, lambda: asyncio.sleep(0))
        with self.assertRaises(MusicBusy):
            service.submit(2, lambda: asyncio.sleep(0))
        service.start()
        await job
        await service.submit(1, lambda: asyncio.sleep(0))

    async def test_manual_priority_and_failure_recovery(self):
        service = self.service(workers=1)
        order = []
        async def background():
            order.append("daily")
        async def manual():
            order.append("manual")
            raise ValueError("expected")
        daily = service.submit(1, background, priority=1)
        user = service.submit(2, manual)
        service.start()
        with self.assertRaises(ValueError):
            await user
        await daily
        self.assertEqual(order, ["manual", "daily"])
        self.assertFalse(service.users)

    async def test_same_song_one_download_upload_and_persistent_reuse(self):
        service = self.service(workers=4)
        bot = FakeBot()
        urls = ["https://youtu.be/abc", "https://www.youtube.com/watch?v=abc&list=x"]
        def download(url, dest, **kwargs):
            time.sleep(0.02)
            path = dest / "song.mp3"
            path.write_bytes(b"audio")
            return {"path": path, "title": "song", "artist": "artist", "duration": 10}
        with patch("utils.music_service.download_audio", side_effect=download) as downloader:
            result = await asyncio.gather(*(service.send_audio(bot, url, chat_id=i)
                                            for i, url in enumerate(urls * 2)))
            self.assertEqual(downloader.call_count, 1)
            self.assertEqual(bot.uploads, 1)
            self.assertEqual(bot.reuses, 3)
            self.assertEqual(bot.peak, 1)
            self.assertTrue(all(info["title"] == "song" for _, info in result))
            another = self.service()
            await another.send_audio(bot, urls[0], chat_id=1)
            self.assertEqual(downloader.call_count, 1)
        self.assertFalse(service.locks)

    async def test_distinct_songs_upload_serially_and_download_budget(self):
        service = self.service(workers=4)
        bot = FakeBot()
        rates = []
        def download(url, dest, **kwargs):
            rates.append(kwargs["cfg"]["download_rate_bps"])
            path = dest / "song.mp3"
            path.write_bytes(b"audio")
            return {"path": path, "title": "song", "artist": "", "duration": 10}
        with patch("utils.music_service.download_audio", side_effect=download):
            await asyncio.gather(*(service.send_audio(bot, f"https://example.com/{i}", chat_id=i)
                                   for i in range(4)))
        self.assertEqual(bot.uploads, 4)
        self.assertEqual(bot.peak, 1)
        self.assertEqual(sum(rates), _DEFAULTS["download_budget_bps"])

    async def test_real_telegram_serialization_then_file_id_reuse(self):
        service = self.service()
        transport = TelegramTransport()
        bot = Bot("123456:offline-test", request=transport, get_updates_request=TelegramTransport())
        def download(url, dest, **kwargs):
            path = dest / "song.mp3"
            path.write_bytes(b"mp3-content")
            return {"path": path, "title": "song", "artist": "artist", "duration": 10}
        async with bot:
            with patch("utils.music_service.download_audio", side_effect=download) as downloader:
                await service.send_audio(bot, "https://youtu.be/a", chat_id=1)
                await service.send_audio(bot, "https://youtu.be/a", chat_id=1)
                self.assertEqual(downloader.call_count, 1)
        self.assertTrue(transport.sends[0][0])
        self.assertFalse(transport.sends[1][0])
        self.assertEqual(transport.sends[1][1]["audio"], "telegram-file")
        self.assertEqual(transport.sends[0][2]["write_timeout"], 120)

    async def test_invalid_file_id_fallback_but_chat_errors_do_not_redownload(self):
        service = self.service()
        url = "https://youtu.be/abc"
        key = f"123:{media_key(url)}:mp3:192K"
        music_db.set_audio_cache(key, {"file_id": "expired", "title": "song"})
        bot = FakeBot()
        bot.send_audio = AsyncMock(side_effect=BadRequest("Chat not found"))
        with patch("utils.music_service.download_audio") as downloader:
            with self.assertRaises(BadRequest):
                await service.send_audio(bot, url, chat_id=1)
            downloader.assert_not_called()
        def download(url, dest, **kwargs):
            path = dest / "song.mp3"
            path.write_bytes(b"audio")
            return {"path": path, "title": "song", "artist": "", "duration": 10}
        good = SimpleNamespace(audio=SimpleNamespace(file_id="new"), message_id=1)
        bot.send_audio = AsyncMock(side_effect=[BadRequest("Wrong file identifier"), good])
        with patch("utils.music_service.download_audio", side_effect=download) as downloader:
            await service.send_audio(bot, url, chat_id=1)
            self.assertEqual(downloader.call_count, 1)
        self.assertEqual(music_db.get_audio_cache(key)["file_id"], "new")

    async def test_rate_limit_rewinds_file(self):
        service = self.service()
        positions = []
        async def send(audio, **kwargs):
            positions.append(audio.tell())
            audio.read()
            if len(positions) == 1:
                raise RetryAfter(0)
            return "ok"
        bot = SimpleNamespace(send_audio=send)
        audio = io.BytesIO(b"data")
        with patch("utils.music_service.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await service._send(bot, audio, chat_id=1), "ok")
            sleep.assert_awaited_once()
        self.assertEqual(positions, [0, 0])

    async def test_cancel_download_waits_for_cleanup(self):
        service = self.service()
        started = threading.Event()
        paths = []
        def download(url, dest, cancel_event, **kwargs):
            paths.append(dest)
            started.set()
            cancel_event.wait(2)
            self.assertTrue(cancel_event.is_set())
            (dest / "partial").write_bytes(b"partial")
            raise RuntimeError("cancelled")
        with patch("utils.music_service.download_audio", side_effect=download):
            task = asyncio.create_task(service.send_audio(FakeBot(), "https://youtu.be/a", chat_id=1))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(paths[0].exists())
        self.assertFalse(service.locks)

    async def test_close_rejects_new_jobs_and_cancels_queued_futures(self):
        service = self.service()
        future = service.submit(1, lambda: asyncio.sleep(0))
        async def expire(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError
        with patch("utils.music_service.asyncio.wait_for", side_effect=expire):
            await service.close()
        self.assertTrue(future.cancelled())
        self.assertFalse(service.users)
        with self.assertRaises(MusicBusy):
            service.submit(1, lambda: asyncio.sleep(0))

    async def test_search_handler_returns_without_waiting_for_work(self):
        service = self.service()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=["song"],
                                  application=SimpleNamespace(bot_data={"music_service": service}))
        with patch.object(music, "check_environment", return_value={"ytdlp": True, "ffmpeg": True}), \
                patch.object(music, "_search_and_reply", new=AsyncMock()) as search:
            await music.cmd_music(update, context)
            search.assert_not_awaited()
            self.assertEqual(service.queue.qsize(), 1)
            service.start()
            await service.queue.join()
            search.assert_awaited_once()

    async def test_old_search_buttons_keep_their_original_result(self):
        service = self.service()
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 effective_chat=SimpleNamespace(id=9),
                                 message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(user_data={},
                                  application=SimpleNamespace(bot_data={"music_service": service}))
        first = [{"url": "https://youtu.be/first", "title": "first"}]
        second = [{"url": "https://youtu.be/second", "title": "second"}]
        with patch.object(music, "search_media", side_effect=[first, second]), \
                patch.object(music_db, "record_search"):
            await music._search_and_reply(update, context, "first")
            button = reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
            await music._search_and_reply(update, context, "second")
        query = SimpleNamespace(data=button.callback_data, answer=AsyncMock(),
                                edit_message_text=AsyncMock(), from_user=SimpleNamespace(id=1),
                                message=SimpleNamespace(chat_id=9))
        update.callback_query = query
        with patch.object(music, "_download_and_reply", new=AsyncMock()) as download:
            await music.cb_download(update, context)
            service.start()
            await service.queue.join()
            self.assertEqual(download.call_args.args[2]["url"], first[0]["url"])

    async def test_expired_search_button_does_not_download(self):
        service = self.service()
        query = SimpleNamespace(data="MUSIC_DL:old:0", answer=AsyncMock(),
                                edit_message_text=AsyncMock(), from_user=SimpleNamespace(id=1),
                                message=SimpleNamespace(chat_id=9))
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={"music_results": {
            "old": {"created": time.monotonic() - 601, "chat_id": 9,
                    "results": [{"url": "https://youtu.be/a"}]}}},
            application=SimpleNamespace(bot_data={"music_service": service}))
        await music.cb_download(update, context)
        self.assertEqual(service.queue.qsize(), 0)
        query.edit_message_text.assert_awaited_once()


class ProcessTests(unittest.TestCase):
    def test_process_timeout_kills_child_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pid"
            code = ("import subprocess,time,pathlib; "
                    "p=subprocess.Popen(['sleep','60']); "
                    f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)")
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_process([sys.executable, "-c", code], 0.5)
            child = pidfile.read_text()
            status = Path(f"/proc/{child}/stat")
            for _ in range(20):
                if not status.exists() or status.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")


if __name__ == "__main__":
    unittest.main()
