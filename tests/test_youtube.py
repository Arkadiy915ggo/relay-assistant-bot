import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_summary_bot.youtube import (
    YouTubeDownloadError,
    _download_youtube_video,
    download_youtube_video,
    youtube_url,
    youtube_url_from_message,
)
from tg_summary_bot import youtube


class YouTubeUrlTests(unittest.TestCase):
    def test_accepts_supported_video_urls(self) -> None:
        cases = (
            "https://www.youtube.com/watch?v=abc",
            "https://YOUTUBE.com/shorts/abc",
            "https://youtube.com/embed/abc",
            "https://youtube.com/live/abc",
            "https://youtu.be/abc?t=10",
        )
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(youtube_url(f"смотри <{url}>."), url)

    def test_rejects_non_video_and_lookalike_urls(self) -> None:
        cases = (
            "https://example.com/watch?v=abc",
            "https://youtube.com.evil/watch?v=abc",
            "https://youtube.com@evil.example/watch?v=abc",
            "https://youtube.com/",
            "https://youtube.com/watch",
            "https://youtube.com/playlist?list=abc",
            "https://youtu.be/",
            "youtube.com/watch?v=abc",
        )
        for url in cases:
            with self.subTest(url=url):
                self.assertIsNone(youtube_url(url))

    def test_replied_url_has_priority_over_current_message(self) -> None:
        message = SimpleNamespace(
            text="/video https://youtu.be/current",
            caption=None,
            reply_to_message=SimpleNamespace(text="https://youtu.be/replied", caption=None),
        )
        self.assertEqual(youtube_url_from_message(message), "https://youtu.be/replied")

    def test_uses_url_from_replied_message(self) -> None:
        message = SimpleNamespace(
            text="/video",
            caption=None,
            reply_to_message=SimpleNamespace(text="https://youtu.be/abc", caption=None),
        )
        self.assertEqual(youtube_url_from_message(message), "https://youtu.be/abc")


class FakeYoutubeDL:
    metadata: dict[str, object] = {}
    downloaded: dict[str, object] = {}
    download_callback = None

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def extract_info(self, _: str, *, download: bool) -> dict[str, object]:
        if not download:
            return dict(self.metadata)
        callback = type(self).download_callback
        if callback:
            callback(self.options)
        return dict(self.downloaded or self.metadata)


class YouTubeDownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeYoutubeDL.metadata = {"duration": 60, "title": "Test video", "_type": "video"}
        FakeYoutubeDL.downloaded = dict(FakeYoutubeDL.metadata)
        FakeYoutubeDL.download_callback = None

    def _download(self, directory: Path, *, max_size_mb: int = 1):
        module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with patch.dict(sys.modules, {"yt_dlp": module}):
            return _download_youtube_video(
                url="https://youtu.be/abc",
                directory=directory,
                max_size_mb=max_size_mb,
                max_seconds=120,
                prefix="youtube_test",
                stop_event=threading.Event(),
            )

    def test_selects_merged_file_and_removes_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def write_files(options: dict[str, object]) -> None:
                output = str(options["outtmpl"])
                Path(output.replace("%(ext)s", "mp4")).write_bytes(b"video")
                Path(output.replace("%(ext)s", "mp4.part")).write_bytes(b"partial")

            FakeYoutubeDL.download_callback = write_files
            result = self._download(directory)

            self.assertEqual(result.path.name, "youtube_test.mp4")
            self.assertEqual(result.duration, 60)
            self.assertEqual(result.title, "Test video")
            self.assertEqual([path.name for path in directory.iterdir()], ["youtube_test.mp4"])

    def test_rejects_playlist_live_and_unknown_duration(self) -> None:
        invalid_metadata = (
            {"_type": "playlist", "entries": [{"id": "abc"}], "duration": 60},
            {"_type": "video", "duration": 60, "is_live": True},
            {"_type": "video", "duration": None},
        )
        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                FakeYoutubeDL.metadata = metadata
                with self.assertRaises(YouTubeDownloadError):
                    self._download(Path(tmp))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_rejects_metadata_duration_and_size_limits(self) -> None:
        invalid_metadata = (
            {"_type": "video", "duration": 121},
            {"_type": "video", "duration": 60, "filesize": 1024 * 1024 + 1},
        )
        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                FakeYoutubeDL.metadata = metadata
                with self.assertRaises(YouTubeDownloadError):
                    self._download(Path(tmp))

    def test_removes_partial_files_after_download_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def fail(options: dict[str, object]) -> None:
                output = str(options["outtmpl"])
                Path(output.replace("%(ext)s", "webm.part")).write_bytes(b"partial")
                raise RuntimeError("network details must not escape")

            FakeYoutubeDL.download_callback = fail
            with self.assertRaisesRegex(YouTubeDownloadError, "YouTube download failed"):
                self._download(directory)
            self.assertEqual(list(directory.iterdir()), [])

    def test_enforces_aggregate_temporary_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def exceed_limit(options: dict[str, object]) -> None:
                output = str(options["outtmpl"])
                Path(output.replace("%(ext)s", "f1.mp4")).write_bytes(b"x" * 600_000)
                Path(output.replace("%(ext)s", "f2.m4a")).write_bytes(b"x" * 600_000)
                options["progress_hooks"][0]({"status": "downloading"})

            FakeYoutubeDL.download_callback = exceed_limit
            with self.assertRaisesRegex(YouTubeDownloadError, "too large"):
                self._download(directory)
            self.assertEqual(list(directory.iterdir()), [])


class YouTubeDownloadTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_success_is_cleaned_after_cancellation_or_timeout(self) -> None:
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                selected = threading.Event()
                release = threading.Event()
                cleaned = threading.Event()
                cleanup_calls = []
                workers = []
                stop_signals = []
                original_select = youtube._select_downloaded_file
                original_cleanup = youtube._cleanup_prefix
                original_to_thread = asyncio.to_thread
                original_wait = asyncio.wait

                async def run_thread(function, *args, **kwargs):
                    if function is youtube._download_youtube_video:
                        workers.append(asyncio.current_task())
                        stop_signals.append(kwargs["stop_event"])
                    return await original_to_thread(function, *args, **kwargs)

                async def wait_for_download(tasks, *, timeout):
                    if timeout_case:
                        if not await original_to_thread(selected.wait, 1):
                            raise RuntimeError("download did not reach final stop check")
                        return set(), set(tasks)
                    return await original_wait(tasks, timeout=timeout)

                def select_after_stop_check(directory: Path, prefix: str) -> Path:
                    selected.set()
                    if not release.wait(2):
                        raise RuntimeError("test did not release download")
                    return original_select(directory, prefix)

                def cleanup(directory: Path, prefix: str, *, keep: Path | None = None) -> None:
                    cleanup_calls.append((prefix, keep is None, threading.current_thread().name))
                    original_cleanup(directory, prefix, keep=keep)
                    if keep is None:
                        cleaned.set()

                def write_files(options: dict[str, object]) -> None:
                    Path(str(options["outtmpl"]).replace("%(ext)s", "mp4")).write_bytes(b"video")

                FakeYoutubeDL.metadata = {"duration": 1, "title": "Test video", "_type": "video"}
                FakeYoutubeDL.downloaded = dict(FakeYoutubeDL.metadata)
                FakeYoutubeDL.download_callback = write_files
                timeout_case = timeout
                with patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)}), patch(
                    "tg_summary_bot.youtube._select_downloaded_file", side_effect=select_after_stop_check
                ), patch("tg_summary_bot.youtube._cleanup_prefix", side_effect=cleanup), patch(
                    "tg_summary_bot.youtube.asyncio.to_thread", side_effect=run_thread
                ), patch(
                    "tg_summary_bot.youtube.asyncio.wait", side_effect=wait_for_download
                ):
                    task = asyncio.create_task(download_youtube_video(
                        url="https://youtu.be/abc", directory=directory,
                        max_size_mb=1, max_seconds=120, timeout_seconds=10,
                    ))
                    try:
                        self.assertTrue(await asyncio.to_thread(selected.wait, 1))
                        if timeout:
                            self.assertTrue(await asyncio.to_thread(stop_signals[0].wait, 1))
                        else:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        self.assertFalse(cleaned.is_set(), cleanup_calls)
                        release.set()
                        if timeout:
                            with self.assertRaisesRegex(YouTubeDownloadError, "timed out"):
                                await task
                        self.assertTrue(await asyncio.to_thread(cleaned.wait, 1))
                        # Join the worker and its callback before another patch/test starts.
                        await asyncio.gather(*workers, return_exceptions=True)
                        await asyncio.sleep(0)
                        self.assertEqual(list(directory.iterdir()), [])
                    finally:
                        release.set()
                        await asyncio.gather(task, *workers, return_exceptions=True)
                        await asyncio.sleep(0)

    async def test_timeout_requests_worker_cancellation(self) -> None:
        stopped = threading.Event()

        def slow_download(**kwargs: object) -> None:
            stop_event = kwargs["stop_event"]
            while not stop_event.is_set():
                time.sleep(0.005)
            stopped.set()
            raise YouTubeDownloadError("cancelled")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "tg_summary_bot.youtube._download_youtube_video",
            side_effect=slow_download,
        ):
            with self.assertRaisesRegex(YouTubeDownloadError, "timed out"):
                await download_youtube_video(
                    url="https://youtu.be/abc",
                    directory=Path(tmp),
                    max_size_mb=1,
                    max_seconds=120,
                    timeout_seconds=0.01,
                )
        self.assertTrue(stopped.is_set())
