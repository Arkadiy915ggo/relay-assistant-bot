from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from tg_summary_bot.config import Settings


IMAGE_RECOGNITION_SYSTEM_PROMPT = """
Ты аккуратный модуль распознавания и краткого анализа изображений.
Не выдумывай текст, которого не видно на изображении.
Сначала перепиши видимый текст на оригинальном языке максимально дословно.
Если видимый текст на английском, добавь перевод на русский.
После этого дай короткое саммари по-русски о том, что на изображении.
Если текста нет или он не читается, прямо скажи об этом.
""".strip()


IMAGE_RECOGNITION_USER_PROMPT = """
Распознай изображение и ответь строго в таком формате:

**Текст с изображения (оригинал)**
<дословно переписанный видимый текст; если текста нет, напиши «Текст не найден»>

**Перевод на русский**
<заполни только если оригинальный текст на английском; иначе напиши «Не нужен»>

**Краткое саммари**
<1-3 предложения по-русски о том, что на изображении>
""".strip()


class ImageRecognizer:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_base_url
        self.model = settings.image_recognition_model
        self.timeout_seconds = settings.ollama_timeout_seconds
        self.keep_alive = settings.ollama_keep_alive
        self.unload_after_task = settings.ollama_unload_after_task
        self.num_ctx = settings.image_recognition_num_ctx
        self.num_predict = settings.ollama_num_predict

    async def recognize(self, image_path: Path) -> str:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "think": False,
                    "keep_alive": self.keep_alive,
                    "messages": [
                        {"role": "system", "content": IMAGE_RECOGNITION_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": IMAGE_RECOGNITION_USER_PROMPT,
                            "images": [image_b64],
                        },
                    ],
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
            "Ollama vision response model=%s total_s=%.1f prompt_tokens=%s "
            "eval_tokens=%s eval_tps=%.2f",
            self.model,
            total_duration,
            prompt_eval_count,
            eval_count,
            tokens_per_second,
        )
        return str(data.get("message", {}).get("content", "")).strip()

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
                logging.exception("Failed to unload Ollama vision model %s", self.model)
                return
        logging.info("Ollama vision model unloaded model=%s", self.model)
