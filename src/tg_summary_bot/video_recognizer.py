from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx

from tg_summary_bot.config import Settings
from tg_summary_bot.observability import opik_track, update_opik_llm_usage, update_opik_span_metadata


VIDEO_RECOGNITION_SYSTEM_PROMPT = """
Ты аккуратный модуль распознавания и краткого анализа видео по ключевым кадрам.
Не выдумывай текст, которого не видно на кадрах.
Главный приоритет — OCR: внимательно прочитай весь видимый текст на каждом кадре.
Перепиши видимый текст на оригинальном языке максимально дословно.
Если текст не на русском, добавь перевод на русский.
После этого дай короткое саммари по-русски о том, что происходит в видео.
Если видео похоже на Telegram-кружочек, кратко опиши происходящее в кружочке.
Если текста нет или он не читается, прямо скажи об этом.
""".strip()


VIDEO_RECOGNITION_USER_PROMPT = """
Проанализируй ключевые кадры видео и ответь строго в таком формате:

**Текст с видео / кадров (оригинал)**
<дословно переписанный видимый текст со всех кадров; сохраняй строки и числа;
если текст меняется по кадрам, перечисли отдельными строками; если текста нет,
напиши «Текст не найден»>

**Перевод на русский**
<заполни, если оригинальный текст не на русском; иначе напиши «Не нужен»>

**Краткое саммари**
<1-3 предложения по-русски о главном смысле видео>

**Что происходит в видео**
<короткое описание видимых действий, объектов и контекста>
""".strip()


PROMPT_VERSION = "v11-translate-non-russian-text"


class VideoRecognizer:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_base_url
        self.model = settings.video_recognition_model
        self.timeout_seconds = settings.ollama_timeout_seconds
        self.keep_alive = settings.ollama_keep_alive
        self.unload_after_task = settings.ollama_unload_after_task
        self.num_ctx = settings.video_recognition_num_ctx
        self.num_predict = settings.video_recognition_num_predict
        self.frame_dir = settings.video_frame_dir
        self.frame_count = max(settings.video_frame_count, 1)
        self.frame_max_width = max(settings.video_frame_max_width, 320)

    @property
    def cache_key(self) -> str:
        return (
            f"{PROMPT_VERSION}|model={self.model}|frames={self.frame_count}|"
            f"width={self.frame_max_width}|ctx={self.num_ctx}|predict={self.num_predict}"
        )

    @opik_track(name="video.recognize")
    async def recognize(self, video_path: Path, *, message_id: int, duration: int | None) -> str:
        update_opik_span_metadata(
            {
                "model": self.model,
                "message_id": message_id,
                "duration_s": duration,
                "video_size_bytes": video_path.stat().st_size if video_path.exists() else 0,
                "configured_frame_count": self.frame_count,
                "configured_frame_max_width": self.frame_max_width,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            }
        )
        last_error: Exception | None = None
        for frame_count, frame_max_width in self._compression_attempts():
            frames = await self._extract_frames(
                video_path,
                message_id=message_id,
                duration=duration,
                frame_count=frame_count,
                frame_max_width=frame_max_width,
            )
            if not frames:
                last_error = RuntimeError("ffmpeg did not extract any video frames")
                continue

            try:
                images = [base64.b64encode(frame.read_bytes()).decode("ascii") for frame in frames]
                return await self._complete_with_images(
                    images,
                    frame_max_width=frame_max_width,
                )
            except RuntimeError as exc:
                if not self._is_context_limit_error(exc):
                    raise
                last_error = exc
                logging.warning(
                    "Ollama video context limit exceeded model=%s frames=%s width=%s; "
                    "retrying with stronger compression",
                    self.model,
                    len(frames),
                    frame_max_width,
                )
            finally:
                for frame in frames:
                    frame.unlink(missing_ok=True)

        if last_error:
            raise RuntimeError(f"video frames still exceed context after compression: {last_error}") from last_error
        raise RuntimeError("ffmpeg did not extract any video frames")

    @opik_track(name="video.vision", type="llm")
    async def _complete_with_images(self, images: list[str], *, frame_max_width: int) -> str:
        update_opik_span_metadata(
            {
                "provider": "ollama",
                "model": self.model,
                "frame_count": len(images),
                "frame_max_width": frame_max_width,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            }
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "system": VIDEO_RECOGNITION_SYSTEM_PROMPT,
                    "prompt": VIDEO_RECOGNITION_USER_PROMPT,
                    "images": images,
                    "options": {
                        "temperature": 0.0,
                        "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict,
                    },
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text.strip()[:1000] or str(exc)
                raise RuntimeError(f"Ollama vision API error: {detail}") from exc

        data = response.json()
        total_duration = (data.get("total_duration") or 0) / 1_000_000_000
        prompt_eval_count = data.get("prompt_eval_count") or 0
        eval_count = data.get("eval_count") or 0
        eval_duration = (data.get("eval_duration") or 0) / 1_000_000_000
        tokens_per_second = eval_count / eval_duration if eval_duration else 0
        content = str(data.get("response", "")).strip()
        update_opik_llm_usage(
            provider="ollama",
            model=self.model,
            usage={
                "prompt_tokens": int(prompt_eval_count),
                "completion_tokens": int(eval_count),
                "total_tokens": int(prompt_eval_count) + int(eval_count),
            },
            metadata={
                "frame_count": len(images),
                "frame_max_width": frame_max_width,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
                "total_duration_s": round(total_duration, 3),
                "eval_duration_s": round(eval_duration, 3),
                "eval_tokens_per_second": round(tokens_per_second, 3),
                "output_chars": len(content),
            },
        )
        logging.info(
            "Ollama video response model=%s frames=%s width=%s total_s=%.1f prompt_tokens=%s "
            "eval_tokens=%s eval_tps=%.2f",
            self.model,
            len(images),
            frame_max_width,
            total_duration,
            prompt_eval_count,
            eval_count,
            tokens_per_second,
        )
        if not content:
            thinking = str(data.get("thinking", "")).strip()
            logging.warning(
                "Ollama video response was empty model=%s thinking_chars=%s keys=%s",
                self.model,
                len(thinking),
                sorted(data.keys()) if isinstance(data, dict) else [],
            )
            if thinking:
                raise RuntimeError(
                    "Ollama returned only thinking and no final video recognition result; "
                    "increase VIDEO_RECOGNITION_NUM_PREDICT or use a non-thinking vision model"
                )
            raise RuntimeError("Ollama returned an empty video recognition result")
        return content

    async def unload(self) -> None:
        if not self.unload_after_task:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.model, "prompt": "", "keep_alive": 0},
                )
                response.raise_for_status()
            except Exception:  # noqa: BLE001
                logging.exception("Failed to unload Ollama video model %s", self.model)
                return
        logging.info("Ollama video model unloaded model=%s", self.model)

    async def _extract_frames(
        self,
        video_path: Path,
        *,
        message_id: int,
        duration: int | None,
        frame_count: int,
        frame_max_width: int,
    ) -> list[Path]:
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        glob_pattern = f"{video_path.stem}_{message_id}_{frame_count}_{frame_max_width}_*.jpg"
        for old_frame in self.frame_dir.glob(glob_pattern):
            old_frame.unlink(missing_ok=True)

        pattern = self.frame_dir / f"{video_path.stem}_{message_id}_{frame_count}_{frame_max_width}_%03d.jpg"
        fps = frame_count / max(duration or frame_count, 1)
        fps = min(max(fps, 0.05), 1.0)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps:.6f},scale='min({frame_max_width},iw)':-2",
            "-frames:v",
            str(frame_count),
            "-q:v",
            "4",
            str(pattern),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed: {detail[:1000]}")
        return sorted(self.frame_dir.glob(glob_pattern))

    def _compression_attempts(self) -> list[tuple[int, int]]:
        candidates = [
            (self.frame_count, self.frame_max_width),
            (self.frame_count, min(self.frame_max_width, 960)),
            (max(self.frame_count * 3 // 4, 1), min(self.frame_max_width, 960)),
            (max(self.frame_count // 2, 1), min(self.frame_max_width, 768)),
            (max(self.frame_count // 3, 1), min(self.frame_max_width, 640)),
            (1, min(self.frame_max_width, 480)),
            (1, min(self.frame_max_width, 320)),
        ]
        attempts: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for frame_count, frame_max_width in candidates:
            attempt = (max(frame_count, 1), max(frame_max_width, 320))
            if attempt in seen:
                continue
            seen.add(attempt)
            attempts.append(attempt)
        return attempts

    @staticmethod
    def _is_context_limit_error(error: Exception) -> bool:
        text = str(error).lower()
        return "exceed_context_size" in text or "exceeds the available context size" in text
