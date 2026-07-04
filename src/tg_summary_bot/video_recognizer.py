from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx

from tg_summary_bot.config import Settings


VIDEO_RECOGNITION_SYSTEM_PROMPT = """
Ты аккуратный модуль распознавания и краткого анализа видео по ключевым кадрам.
Не выдумывай текст, которого не видно на кадрах.
Главный приоритет — OCR: внимательно прочитай весь видимый текст на каждом кадре.
Перепиши видимый текст на оригинальном языке максимально дословно.
Если текст английский, добавь перевод на русский.
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
<заполни только если оригинальный текст на английском; иначе напиши «Не нужен»>

**Краткое саммари**
<1-3 предложения по-русски о главном смысле видео>

**Что происходит в видео**
<короткое описание видимых действий, объектов и контекста>
""".strip()


PROMPT_VERSION = "v9-qwen25-video"


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

    async def recognize(self, video_path: Path, *, message_id: int, duration: int | None) -> str:
        frames = await self._extract_frames(video_path, message_id=message_id, duration=duration)
        if not frames:
            raise RuntimeError("ffmpeg did not extract any video frames")

        try:
            images = [base64.b64encode(frame.read_bytes()).decode("ascii") for frame in frames]
            return await self._complete_with_images(images)
        finally:
            for frame in frames:
                frame.unlink(missing_ok=True)

    async def _complete_with_images(self, images: list[str]) -> str:
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
        logging.info(
            "Ollama video response model=%s frames=%s total_s=%.1f prompt_tokens=%s "
            "eval_tokens=%s eval_tps=%.2f",
            self.model,
            len(images),
            total_duration,
            prompt_eval_count,
            eval_count,
            tokens_per_second,
        )
        content = str(data.get("response", "")).strip()
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
    ) -> list[Path]:
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        pattern = self.frame_dir / f"{video_path.stem}_{message_id}_%03d.jpg"
        fps = self.frame_count / max(duration or self.frame_count, 1)
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
            f"fps={fps:.6f},scale='min({self.frame_max_width},iw)':-2",
            "-frames:v",
            str(self.frame_count),
            "-q:v",
            "2",
            str(pattern),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed: {detail[:1000]}")
        return sorted(self.frame_dir.glob(f"{video_path.stem}_{message_id}_*.jpg"))
