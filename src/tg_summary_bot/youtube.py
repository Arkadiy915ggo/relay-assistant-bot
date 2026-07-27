from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS = 300
YOUTUBE_SOCKET_TIMEOUT_SECONDS = 15
_TEMPORARY_SUFFIXES = (".part", ".ytdl", ".temp")


class YouTubeDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedYouTubeVideo:
    path: Path
    duration: int
    title: str


def _is_supported_video_url(candidate: str) -> bool:
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        return False
    if parsed.username or parsed.password:
        return False

    parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be":
        return bool(parts and parts[0])
    if parsed.path.rstrip("/") == "/watch":
        return bool(parse_qs(parsed.query).get("v", [""])[0].strip())
    return len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"} and bool(parts[1])


def youtube_url(text: str) -> str | None:
    for token in text.split():
        candidate = token.strip("<>.,!?()[]{}\"'")
        if _is_supported_video_url(candidate):
            return candidate
    return None


def youtube_url_from_message(message: object) -> str | None:
    replied = getattr(message, "reply_to_message", None)
    if replied:
        replied_url = youtube_url(
            str(getattr(replied, "text", "") or getattr(replied, "caption", "") or "")
        )
        if replied_url:
            return replied_url
    return youtube_url(str(getattr(message, "text", "") or getattr(message, "caption", "") or ""))


async def download_youtube_video(
    *,
    url: str,
    directory: Path,
    max_size_mb: int,
    max_seconds: int,
    timeout_seconds: float = YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS,
) -> DownloadedYouTubeVideo:
    if not _is_supported_video_url(url):
        raise YouTubeDownloadError("Unsupported YouTube video URL.")

    prefix = f"youtube_{uuid4().hex}"
    stop_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _download_youtube_video,
            url=url,
            directory=directory,
            max_size_mb=max_size_mb,
            max_seconds=max_seconds,
            prefix=prefix,
            stop_event=stop_event,
        )
    )
    try:
        done, _ = await asyncio.wait({worker}, timeout=max(timeout_seconds, 0.01))
        if worker in done:
            return worker.result()
        stop_event.set()
        try:
            await asyncio.wait_for(worker, timeout=YOUTUBE_SOCKET_TIMEOUT_SECONDS + 5)
        except Exception:  # noqa: BLE001
            pass
        raise YouTubeDownloadError("YouTube download timed out.")
    except asyncio.CancelledError:
        stop_event.set()
        worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        raise


def _prefix_files(directory: Path, prefix: str) -> list[Path]:
    return [path for path in directory.glob(f"{prefix}*") if path.is_file()]


def _cleanup_prefix(directory: Path, prefix: str, *, keep: Path | None = None) -> None:
    keep_resolved = keep.resolve() if keep else None
    for path in _prefix_files(directory, prefix):
        if keep_resolved and path.resolve() == keep_resolved:
            continue
        path.unlink(missing_ok=True)


def _validate_metadata(info: dict[str, object], *, max_seconds: int, max_size_mb: int) -> int:
    if info.get("entries") or info.get("_type") in {"playlist", "multi_video", "url_transparent"}:
        raise YouTubeDownloadError("YouTube playlists and channels are not supported.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise YouTubeDownloadError("Live and upcoming YouTube videos are not supported.")

    raw_duration = info.get("duration")
    try:
        duration = int(raw_duration) if raw_duration is not None else 0
    except (TypeError, ValueError, OverflowError) as exc:
        raise YouTubeDownloadError("YouTube returned an invalid video duration.") from exc
    if duration <= 0:
        raise YouTubeDownloadError("YouTube video duration is unavailable.")
    if duration > max_seconds:
        raise YouTubeDownloadError(
            f"YouTube video is too long: {duration} sec. Limit: {max_seconds} sec."
        )

    raw_size = info.get("filesize") or info.get("filesize_approx")
    if raw_size is not None:
        try:
            size = int(raw_size)
        except (TypeError, ValueError, OverflowError) as exc:
            raise YouTubeDownloadError("YouTube returned an invalid video size.") from exc
        if size > max_size_mb * 1024 * 1024:
            raise YouTubeDownloadError(f"YouTube video is too large. Limit: {max_size_mb} MB.")
    return duration


def _select_downloaded_file(directory: Path, prefix: str) -> Path:
    candidates = [
        path
        for path in _prefix_files(directory, prefix)
        if not path.name.endswith(_TEMPORARY_SUFFIXES)
    ]
    if not candidates:
        raise YouTubeDownloadError("YouTube download did not create a video file.")
    mp4_candidates = [path for path in candidates if path.suffix.lower() == ".mp4"]
    return max(mp4_candidates or candidates, key=lambda path: path.stat().st_size)


def _download_youtube_video(
    *,
    url: str,
    directory: Path,
    max_size_mb: int,
    max_seconds: int,
    prefix: str,
    stop_event: threading.Event,
) -> DownloadedYouTubeVideo:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise YouTubeDownloadError(
            "YouTube support is not installed. Reinstall the bot dependencies."
        ) from exc

    directory.mkdir(parents=True, exist_ok=True)
    limit_bytes = max(max_size_mb, 1) * 1024 * 1024

    def progress_hook(_: dict[str, object]) -> None:
        if stop_event.is_set():
            raise YouTubeDownloadError("YouTube download was cancelled.")
        total_size = sum(path.stat().st_size for path in _prefix_files(directory, prefix))
        if total_size > limit_bytes:
            raise YouTubeDownloadError(f"YouTube video is too large. Limit: {max_size_mb} MB.")

    output_template = str(directory / f"{prefix}.%(ext)s")
    options = {
        "format": "bv*+ba/b",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "max_downloads": 1,
        "max_filesize": limit_bytes,
        "socket_timeout": YOUTUBE_SOCKET_TIMEOUT_SECONDS,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "restrictfilenames": True,
    }

    path: Path | None = None
    try:
        with YoutubeDL(options) as downloader:
            metadata = downloader.extract_info(url, download=False)
            if not isinstance(metadata, dict):
                raise YouTubeDownloadError("YouTube did not return video metadata.")
            duration = _validate_metadata(
                metadata,
                max_seconds=max_seconds,
                max_size_mb=max_size_mb,
            )
            downloaded = downloader.extract_info(url, download=True)
            if not isinstance(downloaded, dict):
                raise YouTubeDownloadError("YouTube did not return downloaded video metadata.")
            _validate_metadata(
                downloaded,
                max_seconds=max_seconds,
                max_size_mb=max_size_mb,
            )

        if stop_event.is_set():
            raise YouTubeDownloadError("YouTube download was cancelled.")
        path = _select_downloaded_file(directory, prefix)
        if path.stat().st_size > limit_bytes:
            raise YouTubeDownloadError(f"YouTube video is too large. Limit: {max_size_mb} MB.")
        title = str(downloaded.get("title") or metadata.get("title") or "YouTube video")
        _cleanup_prefix(directory, prefix, keep=path)
        return DownloadedYouTubeVideo(path=path, duration=duration, title=title)
    except YouTubeDownloadError:
        _cleanup_prefix(directory, prefix)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup_prefix(directory, prefix)
        raise YouTubeDownloadError("YouTube download failed.") from exc
