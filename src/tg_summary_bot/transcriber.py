from __future__ import annotations

import asyncio
import gc
import logging
import time
from pathlib import Path

from tg_summary_bot.config import Settings
from tg_summary_bot.observability import trace, update_span_metadata


def resolve_whisper_model(model_name: str, *, local_files_only: bool) -> str:
    """Resolve a complete cached snapshot without allowing tokenizer fallbacks online."""
    if not local_files_only:
        return model_name
    from faster_whisper.utils import download_model
    try:
        directory = Path(model_name)
        if not directory.is_dir():
            directory = Path(download_model(model_name, local_files_only=True))
        # WhisperModel would otherwise call Tokenizer.from_pretrained() online.
        for name in ("model.bin", "config.json", "tokenizer.json"):
            if not (directory / name).is_file() or (directory / name).stat().st_size == 0:
                raise FileNotFoundError(name)
    except Exception:
        raise RuntimeError(
            "Whisper model is missing or incomplete locally. Prepare it explicitly with "
            "./run.sh download-whisper <model>, or set WHISPER_MODEL to a complete local directory. "
            "WHISPER_LOCAL_FILES_ONLY=false permits automatic downloads."
        ) from None
    return str(directory)


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.whisper_model
        self.device = settings.whisper_device
        self.compute_type = settings.whisper_compute_type
        self.language = settings.whisper_language or None
        self.local_files_only = settings.whisper_local_files_only

    @trace(name="audio.transcribe")
    async def transcribe(self, audio_path: Path) -> str:
        update_span_metadata(
            {
                "model": self.model_name,
                "device": self.device,
                "compute_type": self.compute_type,
                "language": self.language or "auto",
                "audio_size_bytes": audio_path.stat().st_size if audio_path.exists() else 0,
            }
        )
        text = await asyncio.to_thread(self._transcribe_sync, audio_path)
        update_span_metadata({"output_chars": len(text)})
        return text

    def _transcribe_sync(self, audio_path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run: ./run.sh install-voice"
            ) from exc

        started = time.perf_counter()
        model = WhisperModel(
            resolve_whisper_model(self.model_name, local_files_only=self.local_files_only),
            device=self.device,
            compute_type=self.compute_type,
            local_files_only=self.local_files_only,
        )
        try:
            segments, info = model.transcribe(
                str(audio_path),
                language=self.language,
                vad_filter=True,
            )
            text = " ".join(
                segment.text.strip()
                for segment in segments
                if segment.text.strip()
            )
            elapsed = time.perf_counter() - started
            logging.info(
                "Whisper transcription finished model=%s device=%s language=%s "
                "duration_s=%.1f elapsed_s=%.1f",
                self.model_name,
                self.device,
                getattr(info, "language", None),
                getattr(info, "duration", 0.0),
                elapsed,
            )
            return " ".join(text.split())
        finally:
            del model
            gc.collect()


def main() -> None:
    """Explicit network-enabled model preparation; does not load bot configuration."""
    import argparse
    parser = argparse.ArgumentParser(description="Download a Whisper model for offline use")
    parser.add_argument("model", help="Whisper size (e.g. small or large-v3) or Hugging Face model ID")
    args = parser.parse_args()
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        parser.exit(1, "Install voice dependencies first: ./run.sh install-voice\n")
    print(download_model(args.model, local_files_only=False))


if __name__ == "__main__":
    main()
