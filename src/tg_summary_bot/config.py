from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _csv_ints(value: str) -> set[int]:
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


def _csv_strings(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    llm_provider: str
    openai_api_key: str
    openai_model: str
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: int
    ollama_keep_alive: str
    ollama_num_ctx: int
    ollama_num_predict: int
    compare_models: list[str]
    database_path: Path
    log_file: Path
    response_log_file: Path
    max_message_chars: int
    max_summary_input_chars: int
    chunk_chars: int
    default_summary_period: str

    @property
    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        return "openai" if self.openai_api_key else "ollama"


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    provider = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    if provider not in {"auto", "openai", "ollama"}:
        raise RuntimeError("LLM_PROVIDER must be one of: auto, openai, ollama")

    return Settings(
        telegram_bot_token=token,
        allowed_chat_ids=_csv_ints(os.getenv("ALLOWED_CHAT_IDS", "")),
        llm_provider=provider,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip(),
        ollama_timeout_seconds=int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "1800")),
        ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "30m").strip(),
        ollama_num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "4096")),
        ollama_num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "800")),
        compare_models=_csv_strings(os.getenv("COMPARE_MODELS", "")),
        database_path=Path(os.getenv("DATABASE_PATH", "data/messages.sqlite3")),
        log_file=Path(os.getenv("LOG_FILE", "data/bot.log")),
        response_log_file=Path(os.getenv("RESPONSE_LOG_FILE", "data/responses.log")),
        max_message_chars=int(os.getenv("MAX_MESSAGE_CHARS", "4000")),
        max_summary_input_chars=int(os.getenv("MAX_SUMMARY_INPUT_CHARS", "120000")),
        chunk_chars=int(os.getenv("CHUNK_CHARS", "18000")),
        default_summary_period=os.getenv("DEFAULT_SUMMARY_PERIOD", "24h").strip(),
    )
