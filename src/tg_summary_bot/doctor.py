from __future__ import annotations

import importlib.util
import platform
import shutil
import sys

import httpx

from tg_summary_bot.config import load_settings


def _ok(message: str) -> None:
    print(f"OK: {message}")


def _warn(message: str) -> None:
    print(f"WARN: {message}")


def _fail(message: str) -> None:
    print(f"FAIL: {message}")


def _ollama_models(base_url: str, timeout: float = 5.0) -> set[str] | None:
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
    except httpx.RequestError:
        _fail(f"Ollama is not reachable at {base_url}. Start Ollama and run: ollama list")
        return None
    except httpx.HTTPStatusError as exc:
        _fail(f"Ollama tags endpoint failed: HTTP {exc.response.status_code}")
        return None

    data = response.json()
    return {str(model.get("name", "")) for model in data.get("models", []) if model.get("name")}


def _check_model(models: set[str] | None, model: str, label: str) -> bool:
    if not model or models is None:
        return False
    if model in models:
        _ok(f"{label} model is installed: {model}")
        return True
    _fail(f"{label} model is not installed: {model}. Run: ollama pull {model}")
    return False


def _check_optional_model(models: set[str] | None, model: str, label: str) -> bool:
    if not model or models is None:
        return False
    if model in models:
        _ok(f"{label} model is installed: {model}")
        return True
    _warn(f"{label} model is not installed: {model}. Run: ollama pull {model}")
    return False


def main() -> int:
    errors = 0
    warnings = 0

    version = sys.version_info
    if version >= (3, 11):
        _ok(f"Python {version.major}.{version.minor}.{version.micro}")
    else:
        _fail("Python 3.11+ is required")
        errors += 1

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Configuration error: {exc}")
        return 1

    _ok(".env loaded and TELEGRAM_BOT_TOKEN is set")
    provider = settings.resolved_llm_provider
    _ok(f"LLM provider: {provider}")

    system = platform.system().lower()
    if system == "darwin":
        _ok("macOS detected")
        if settings.whisper_device.lower() == "cuda":
            _fail("WHISPER_DEVICE=cuda does not work on Mac. Use WHISPER_DEVICE=cpu.")
            errors += 1

    ollama_models: set[str] | None = None
    needs_ollama = provider == "ollama"
    needs_ollama = needs_ollama or bool(settings.image_recognition_model)
    needs_ollama = needs_ollama or bool(settings.video_recognition_model)
    needs_ollama = needs_ollama or bool(settings.meme_enabled and (settings.meme_model or settings.image_recognition_model))
    needs_ollama = needs_ollama or bool(
        settings.transcription_format_enabled
        and settings.transcription_format_provider == "ollama"
        and settings.transcription_format_model
        and (settings.transcribe_voice or settings.video_transcribe_audio)
    )

    if provider == "openai":
        if settings.openai_api_key:
            _ok("OPENAI_API_KEY is set")
        elif settings.openai_base_url:
            _ok(f"OPENAI_BASE_URL is set for OpenAI-compatible backend: {settings.openai_base_url}")
        else:
            _fail("OPENAI_API_KEY is required unless OPENAI_BASE_URL points to a compatible local server")
            errors += 1

    if needs_ollama:
        ollama_models = _ollama_models(settings.ollama_base_url)
        if ollama_models is None:
            errors += 1
        elif provider == "ollama" and not _check_model(ollama_models, settings.ollama_model, "Text"):
            errors += 1

    if settings.compare_models:
        if ollama_models is None:
            ollama_models = _ollama_models(settings.ollama_base_url)
        for model in settings.compare_models:
            if not _check_optional_model(ollama_models, model, "/compare"):
                warnings += 1
    else:
        _ok("COMPARE_MODELS is empty; /compare is disabled until models are configured")

    if settings.image_recognition_model and ollama_models is not None:
        if not _check_optional_model(ollama_models, settings.image_recognition_model, "Image recognition"):
            warnings += 1

    if settings.video_recognition_model and ollama_models is not None:
        if not _check_optional_model(ollama_models, settings.video_recognition_model, "Video recognition"):
            warnings += 1

    if settings.meme_enabled and settings.image_recognition_model and ollama_models is not None:
        if not _check_optional_model(ollama_models, settings.meme_model or settings.image_recognition_model, "Meme"):
            warnings += 1

    if settings.video_transcribe_audio or settings.video_recognition_model:
        if shutil.which("ffmpeg"):
            _ok("ffmpeg is available")
        else:
            _warn("ffmpeg is required for video recognition/audio extraction. On Mac: brew install ffmpeg")
            warnings += 1

    if settings.transcribe_voice or settings.video_transcribe_audio:
        if importlib.util.find_spec("faster_whisper"):
            _ok("faster-whisper is installed")
        else:
            _fail("faster-whisper is not installed. Run: ./run.sh install-voice")
            errors += 1
        if system == "darwin" and settings.whisper_compute_type.lower() == "float16":
            _warn("WHISPER_COMPUTE_TYPE=float16 can be fragile on Mac CPU. Prefer int8 for setup.")
            warnings += 1
    else:
        _ok("Voice/video audio transcription is disabled")

    print(f"Doctor finished: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
