"""Opt-in live Opik ingestion check with fake models, ffmpeg and Telegram.

Run: .venv/bin/python tests/voice_trace_smoke.py
Uses existing Opik SDK configuration. Never loads .env, production logs/databases,
GPU models or Telegram credentials. Sends only synthetic test data and stage metadata.
"""
import asyncio
import json
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import opik

import tg_summary_bot.bot as bot_module
from tg_summary_bot.assistant import ChatAssistant
from tg_summary_bot.observability import configure_opik_tracing, opik_track
from tg_summary_bot.speech_synthesizer import SpeechSynthesizer
from tg_summary_bot.voice_presets import VoiceRegistry
from test_voice_handlers import FakeMessage, HandlerTests
from test_joke_handlers import handler


async def main():
    case = HandlerTests("test_direct_speech_provenance_and_reply_link")
    await case.asyncSetUp()
    trace_ids = []
    try:
        class SyntheticLLM:
            @opik_track(name="verification.fake_llm", type="llm", capture_input=True, capture_output=True)
            async def complete(self, **kwargs):
                return "Synthetic verification answer."

            async def unload(self):
                return None

        case.assistant = ChatAssistant(SyntheticLLM(), chunk_chars=1000)
        case.speech = SpeechSynthesizer(case.settings, VoiceRegistry(case.root / "local"))
        case.speech.unavailable_reason = None
        case.speech.check_available = AsyncMock(return_value=None)
        # The production synthesizer uses a real asyncio lock; fake model methods record it.
        case.lock = asyncio.Lock()
        async def fake_process(argv, *, timeout, payload=None):
            if payload is not None:
                request = json.loads(payload)
                with wave.open(request["output"], "wb") as audio:
                    audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    audio.writeframes(b"\0\0" * 24000)
                return b"{}"
            if argv[0] == "ffmpeg":
                Path(argv[-1]).write_bytes(b"synthetic-placeholder")
                return b""
            return b'{"format":{"duration":1},"streams":[{"codec_name":"opus"}]}'

        original = bot_module.update_opik_span_metadata
        def tagged_metadata(metadata, **kwargs):
            original(metadata, **kwargs)
            if kwargs.get("name", "").startswith("telegram."):
                # Tag at admission, so even an interrupted smoke run stays identifiable.
                opik.opik_context.update_current_trace(tags=["synthetic", "verification"],
                                                       metadata={"verification_mode": "fake_models_fake_telegram"})
                trace_ids.append(opik.opik_context.get_current_trace_data().id)

        configure_opik_tracing(enabled=True, project_name="relay-assistant-bot")
        with patch("tg_summary_bot.speech_synthesizer.run_process", fake_process), \
             patch.object(bot_module, "update_opik_span_metadata", tagged_metadata):
            dp = await case.dispatcher()
            await handler(dp, "voice_command")(FakeMessage("/voice Synthetic verification only."))
            await handler(dp, "ask_command")(FakeMessage("/ask voice Synthetic verification only."))
            message = FakeMessage("/voice Synthetic failure verification only.")
            message.upload_error = True
            await handler(dp, "voice_command")(message)
    finally:
        opik.flush_tracker()
        configure_opik_tracing(enabled=False, project_name="")
        case.doCleanups()
    print(json.dumps({"project": "relay-assistant-bot", "trace_ids": trace_ids}))


if __name__ == "__main__":
    asyncio.run(main())
