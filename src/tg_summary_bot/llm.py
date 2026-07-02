from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx
from openai import AsyncOpenAI

from tg_summary_bot.config import Settings


class LLMClient(ABC):
    @abstractmethod
    async def complete(self, *, system: str, user: str) -> str:
        raise NotImplementedError

    async def unload(self) -> None:
        return None


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for LLM_PROVIDER=openai")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def complete(self, *, system: str, user: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content
        return content.strip() if content else "Не удалось получить ответ от модели."


class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int,
        keep_alive: str,
        unload_after_task: bool,
        num_ctx: int,
        num_predict: int,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive
        self.unload_after_task = unload_after_task
        self.num_ctx = num_ctx
        self.num_predict = num_predict

    async def complete(self, *, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "options": {
                        "temperature": 0.2,
                        "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict,
                    },
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text.strip()[:1000] or str(exc)
                raise RuntimeError(f"Ollama API error: {detail}") from exc
        data = response.json()
        total_duration = (data.get("total_duration") or 0) / 1_000_000_000
        prompt_eval_count = data.get("prompt_eval_count") or 0
        eval_count = data.get("eval_count") or 0
        eval_duration = (data.get("eval_duration") or 0) / 1_000_000_000
        tokens_per_second = eval_count / eval_duration if eval_duration else 0
        logging.info(
            "Ollama response model=%s total_s=%.1f prompt_tokens=%s eval_tokens=%s eval_tps=%.2f",
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
                logging.exception("Failed to unload Ollama model %s", self.model)
                return
        logging.info("Ollama model unloaded model=%s", self.model)


def build_llm_client(settings: Settings, *, model: str | None = None) -> LLMClient:
    provider = settings.resolved_llm_provider
    if provider == "openai":
        return OpenAIClient(settings.openai_api_key, model or settings.openai_model)
    if provider == "ollama":
        return OllamaClient(
            settings.ollama_base_url,
            model or settings.ollama_model,
            settings.ollama_timeout_seconds,
            settings.ollama_keep_alive,
            settings.ollama_unload_after_task,
            settings.ollama_num_ctx,
            settings.ollama_num_predict,
        )
    raise RuntimeError(f"Unsupported LLM provider: {provider}")
