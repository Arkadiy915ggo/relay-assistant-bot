from __future__ import annotations

import asyncio
import gc
import logging
import time
from pathlib import Path

from tg_summary_bot.config import Settings


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.whisper_model
        self.device = settings.whisper_device
        self.compute_type = settings.whisper_compute_type
        self.language = settings.whisper_language or None

    async def transcribe(self, audio_path: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio_path)

    def _transcribe_sync(self, audio_path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run: ./run.sh install-voice"
            ) from exc

        started = time.perf_counter()
        model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
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
