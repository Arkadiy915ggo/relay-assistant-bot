from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from tg_summary_bot.config import Settings
from tg_summary_bot.observability import opik_track, update_opik_llm_usage, update_opik_span_metadata


IMAGE_RECOGNITION_SYSTEM_PROMPT = """
Ты аккуратный модуль распознавания и краткого анализа изображений.
Не выдумывай текст, которого не видно на изображении.
Сначала перепиши видимый текст на оригинальном языке максимально дословно.
Если видимый текст на русском, не переводи его и не дублируй в переводе.
Если видимый текст не на русском, добавь перевод на русский.
После этого дай короткое саммари по-русски о том, что на изображении.
Если текста нет или он не читается, прямо скажи об этом.
""".strip()


IMAGE_RECOGNITION_USER_PROMPT = """
Распознай изображение и ответь строго в таком формате:

**Текст с изображения (оригинал)**
<дословно переписанный видимый текст; если текста нет, напиши «Текст не найден»>

**Перевод на русский**
<заполни только если оригинальный текст не на русском; если оригинал на русском,
напиши строго «Не нужен» и не повторяй оригинальный текст>

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

    @opik_track(name="image.recognize")
    async def recognize(self, image_path: Path) -> str:
        image_size = image_path.stat().st_size if image_path.exists() else 0
        update_opik_span_metadata(
            {
                "model": self.model,
                "image_size_bytes": image_size,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            }
        )
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return await self._complete_with_image(image_b64=image_b64, image_size=image_size)

    @opik_track(name="image.vision", type="llm")
    async def _complete_with_image(self, *, image_b64: str, image_size: int) -> str:
        update_opik_span_metadata(
            {
                "provider": "ollama",
                "model": self.model,
                "image_count": 1,
                "image_size_bytes": image_size,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            }
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
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
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"Ollama is not reachable at {self.base_url}. "
                    "Start Ollama and check: ollama list"
                ) from exc
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text.strip()[:1000] or str(exc)
                if response.status_code == 404 and "model" in detail.lower():
                    raise RuntimeError(
                        f"Ollama model {self.model} is not installed. "
                        f"Run: ollama pull {self.model}"
                    ) from exc
                raise RuntimeError(f"Ollama vision API error: {detail}") from exc

        data = response.json()
        total_duration = (data.get("total_duration") or 0) / 1_000_000_000
        prompt_eval_count = data.get("prompt_eval_count") or 0
        eval_count = data.get("eval_count") or 0
        eval_duration = (data.get("eval_duration") or 0) / 1_000_000_000
        tokens_per_second = eval_count / eval_duration if eval_duration else 0
        content = str(data.get("message", {}).get("content", "")).strip()
        update_opik_llm_usage(
            provider="ollama",
            model=self.model,
            usage={
                "prompt_tokens": int(prompt_eval_count),
                "completion_tokens": int(eval_count),
                "total_tokens": int(prompt_eval_count) + int(eval_count),
            },
            metadata={
                "image_count": 1,
                "image_size_bytes": image_size,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
                "total_duration_s": round(total_duration, 3),
                "eval_duration_s": round(eval_duration, 3),
                "eval_tokens_per_second": round(tokens_per_second, 3),
                "output_chars": len(content),
            },
        )
        logging.info(
            "Ollama vision response model=%s total_s=%.1f prompt_tokens=%s "
            "eval_tokens=%s eval_tps=%.2f",
            self.model,
            total_duration,
            prompt_eval_count,
            eval_count,
            tokens_per_second,
        )
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
                logging.exception("Failed to unload Ollama vision model %s", self.model)
                return
        logging.info("Ollama vision model unloaded model=%s", self.model)
