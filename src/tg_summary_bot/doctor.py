from __future__ import annotations

import importlib.util
import asyncio
import platform
import shutil
import sys

import httpx

from tg_summary_bot.config import load_settings
from tg_summary_bot.voice_presets import VoiceRegistry
from tg_summary_bot.speech_synthesizer import COSYVOICE_MODEL_FILES, SpeechSynthesizer


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

    if settings.intent_router_model and settings.resolved_llm_provider == "ollama":
        if not _check_optional_model(ollama_models, settings.intent_router_model, "Intent router"):
            warnings += 1

    if settings.memory_enabled and settings.profile_model and provider == "ollama":
        if not _check_optional_model(ollama_models, settings.profile_model, "Profile refresh"):
            warnings += 1

    if settings.autonomous_joke_judge_model and settings.resolved_llm_provider == "ollama":
        if not _check_optional_model(ollama_models, settings.autonomous_joke_judge_model, "Autonomous joke judge"):
            warnings += 1

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

    if settings.video_recognition_model:
        if importlib.util.find_spec("yt_dlp"):
            _ok("yt-dlp is installed for YouTube video recognition")
        else:
            _fail("yt-dlp is not installed. Reinstall the bot dependencies: ./run.sh install")
            errors += 1

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

    registry = VoiceRegistry(settings.tts_local_voices_dir)
    for diagnostic in registry.diagnostics:
        _warn(f"Voice registry: {diagnostic}")
        warnings += 1
    speech = SpeechSynthesizer(settings, registry)
    reason = asyncio.run(speech.check_available())
    if reason == "disabled":
        _ok("TTS disabled; text bot needs no CosyVoice dependencies")
    elif reason:
        _warn(f"TTS unavailable: {reason}. Check TTS_* paths/limits; ./run.sh install-tts-cosyvoice3")
        diagnoses = {
            "missing_python": f"TTS_PYTHON_PATH must be executable: {settings.tts_python_path}",
            "wrong_python_version": "TTS_PYTHON_PATH must use the separate Python 3.10 environment",
            "missing_upstream": f"TTS_COSYVOICE_UPSTREAM_DIR needs cosyvoice/cli/cosyvoice.py: {settings.tts_cosyvoice_upstream_dir}",
            "missing_model": f"TTS_MODEL_DIR needs cosyvoice3.yaml and baseline weights: {settings.tts_model_dir}",
            "invalid_default_voice": f"TTS_DEFAULT_VOICE is not in the valid registry: {settings.tts_default_voice}",
            "missing_dependencies": "Reinstall pinned TTS dependencies and upstream submodules in the separate environment",
            "cuda_unavailable": "Dedicated TTS torch cannot see CUDA; check NVIDIA driver and cu121 torch installation",
            "missing_rubberband_or_libopus": "Install ffmpeg built with rubberband filter and libopus encoder",
            "invalid_limits": "All TTS character/duration/execution/encoding/upload limits must be positive integers",
        }
        if reason in diagnoses:
            _warn(diagnoses[reason])
        if reason == "missing_model":
            for name in COSYVOICE_MODEL_FILES:
                path = settings.tts_model_dir / name
                if not path.is_file() or path.stat().st_size == 0:
                    _warn(f"Missing/empty TTS model file: {path}")
        warnings += 1
    else:
        _ok("CosyVoice runner/CUDA and ffmpeg libopus/rubberband available (no model synthesis performed)")
    if settings.tts_enabled and not settings.ollama_unload_after_task:
        _warn("Resident Ollama may retain VRAM: set OLLAMA_UNLOAD_AFTER_TASK=true for LLM → TTS baseline")
        warnings += 1
    print(f"Doctor finished: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
