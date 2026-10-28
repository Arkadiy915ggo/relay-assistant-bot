from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx
from openai import AsyncOpenAI

from tg_summary_bot.config import Settings
from tg_summary_bot.observability import trace_stage, tracing_status, update_llm_usage


class LLMClient(ABC):
    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        response_format: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def unload(self) -> None:
        return None


class OpenAIClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "",
    ) -> None:
        if not api_key and not base_url:
            raise RuntimeError("OPENAI_API_KEY is required for LLM_PROVIDER=openai")
        self.client = AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url or None)
        self.model = model
        self.last_usage: dict[str, int] = {}
        self.last_metadata: dict[str, Any] = {}

    async def complete(
        self,
        *,
        system: str,
        user: str,
        response_format: str | None = None,
    ) -> str:
        self.last_usage = {}
        self.last_metadata = {
            "system_chars": len(system),
            "user_chars": len(user),
            "temperature": 0.2,
            "response_format": response_format or "text",
        }
        request: dict[str, object] = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if response_format == "json":
            request["response_format"] = {"type": "json_object"}
        response = await self.client.chat.completions.create(
            **request,
        )
        if response.usage:
            self.last_usage = {
                "prompt_tokens": int(response.usage.prompt_tokens or 0),
                "completion_tokens": int(response.usage.completion_tokens or 0),
                "total_tokens": int(response.usage.total_tokens or 0),
            }
        content = response.choices[0].message.content
        result = content.strip() if content else "Failed to get a response from the model."
        self.last_metadata["output_chars"] = len(result)
        return result


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
        self.last_usage: dict[str, int] = {}
        self.last_metadata: dict[str, Any] = {}

    async def complete(
        self,
        *,
        system: str,
        user: str,
        response_format: str | None = None,
    ) -> str:
        self.last_usage = {}
        self.last_metadata = {
            "system_chars": len(system),
            "user_chars": len(user),
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "temperature": 0.2,
            "response_format": response_format or "text",
        }
        payload: dict[str, object] = {
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
        }
        if response_format == "json":
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
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
                raise RuntimeError(f"Ollama API error: {detail}") from exc
        data = response.json()
        total_duration = (data.get("total_duration") or 0) / 1_000_000_000
        prompt_eval_count = data.get("prompt_eval_count") or 0
        eval_count = data.get("eval_count") or 0
        eval_duration = (data.get("eval_duration") or 0) / 1_000_000_000
        tokens_per_second = eval_count / eval_duration if eval_duration else 0
        self.last_usage = {
            "prompt_tokens": int(prompt_eval_count),
            "completion_tokens": int(eval_count),
            "total_tokens": int(prompt_eval_count) + int(eval_count),
        }
        self.last_metadata.update(
            {
                "total_duration_s": round(total_duration, 3),
                "eval_duration_s": round(eval_duration, 3),
                "eval_tokens_per_second": round(tokens_per_second, 3),
                "ctx_saturated": bool(prompt_eval_count >= self.num_ctx - 1),
                "output_too_short": bool(eval_count <= 2),
            }
        )
        logging.info(
            "Ollama response model=%s total_s=%.1f prompt_tokens=%s eval_tokens=%s eval_tps=%.2f",
            self.model,
            total_duration,
            prompt_eval_count,
            eval_count,
            tokens_per_second,
        )
        result = str(data.get("message", {}).get("content", "")).strip()
        self.last_metadata["output_chars"] = len(result)
        return result

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


class TracedLLMClient(LLMClient):
    def __init__(
        self,
        wrapped: LLMClient,
        *,
        provider: str,
    ) -> None:
        self.wrapped = wrapped
        self.provider = provider
        self.model = getattr(wrapped, "model", "")

    async def complete(
        self,
        *,
        system: str,
        user: str,
        response_format: str | None = None,
    ) -> str:
        with trace_stage(f"llm.{self.provider}.{self.model}", type="llm") as span:
            if span is not None and tracing_status().capture_content:
                span.input = {"system": system, "user": user, "response_format": response_format}
            result = await self.wrapped.complete(system=system, user=user,
                                                 response_format=response_format)
            update_llm_usage(provider=self.provider, model=self.model,
                             usage=getattr(self.wrapped, "last_usage", {}),
                             metadata=getattr(self.wrapped, "last_metadata", {}))
            if span is not None and tracing_status().capture_content:
                span.output = {"text": result}
            return result

    async def unload(self) -> None:
        await self.wrapped.unload()


def build_llm_client(
    settings: Settings,
    *,
    model: str | None = None,
    provider: str | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
) -> LLMClient:
    provider = provider or settings.resolved_llm_provider
    if provider == "openai":
        client: LLMClient = OpenAIClient(
            settings.openai_api_key,
            model or settings.openai_model,
            base_url=settings.openai_base_url,
        )
    elif provider == "ollama":
        client = OllamaClient(
            settings.ollama_base_url,
            model or settings.ollama_model,
            settings.ollama_timeout_seconds,
            settings.ollama_keep_alive,
            settings.ollama_unload_after_task,
            num_ctx or settings.ollama_num_ctx,
            num_predict or settings.ollama_num_predict,
        )
    else:
        raise RuntimeError(f"Unsupported LLM provider: {provider}")
    return TracedLLMClient(client, provider=provider) if tracing_status().active else client
