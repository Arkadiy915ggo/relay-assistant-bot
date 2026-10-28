"""Opt-in live Opik ingestion check with fake models, ffmpeg and Telegram.

Run: .venv/bin/python dev/opik/voice_trace_smoke.py --send
Requires explicit TRACING_API_KEY (or OPIK_API_KEY) in the process environment.
Never loads .env, production logs/databases,
GPU models or Telegram credentials. Sends only synthetic test data and stage metadata.
"""
import asyncio
import argparse
import json
import os
import sys
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Test fakes live in the checkout, not in either installed distribution.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
import tg_summary_bot.bot as bot_module
from tg_summary_bot.assistant import ChatAssistant
from tg_summary_bot.observability import close_tracing, configure_tracing, get_current_span
from tg_summary_bot.llm import TracedLLMClient
from tg_summary_bot.speech_synthesizer import SpeechSynthesizer
from tg_summary_bot.voice_presets import VoiceRegistry
from test_voice_handlers import FakeMessage, HandlerTests
from test_joke_handlers import handler
from test_observability_operations import settings


async def main():
    cfg = settings(
        tracing_enabled=True,
        tracing_api_key=os.getenv("TRACING_API_KEY", os.getenv("OPIK_API_KEY", "")),
        tracing_project_name=os.getenv("TRACING_PROJECT_NAME", "relay-assistant-bot"),
        tracing_workspace=os.getenv("TRACING_WORKSPACE", os.getenv("OPIK_WORKSPACE", "")),
        tracing_endpoint=os.getenv("TRACING_ENDPOINT", os.getenv("OPIK_URL_OVERRIDE", "")),
    )
    status = await configure_tracing(cfg)
    if not status.active:
        raise RuntimeError(f"Tracing unavailable: {status.state}; run ./run.sh install-dev and set a key")
    case = HandlerTests("test_direct_speech_provenance_and_reply_link")
    trace_ids = []
    try:
        await case.asyncSetUp()
        class SyntheticLLM:
            model = "synthetic"
            last_usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            last_metadata = {}
            async def complete(self, **kwargs):
                return "Synthetic verification answer."

            async def unload(self):
                return None

        case.assistant = ChatAssistant(TracedLLMClient(SyntheticLLM(), provider="synthetic"), chunk_chars=1000)
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

        original = bot_module.update_span_metadata
        def tagged_metadata(metadata, **kwargs):
            original(metadata, **kwargs)
            if kwargs.get("name", "").startswith("telegram."):
                # Tag at admission, so even an interrupted smoke run stays identifiable.
                span = get_current_span()
                if span is not None:
                    span.tags = ["synthetic", "verification"]
                    span.metadata["verification_mode"] = "fake_models_fake_telegram"
                    trace_ids.append(span.trace_id)

        with patch("tg_summary_bot.speech_synthesizer.run_process", fake_process), \
             patch.object(bot_module, "update_span_metadata", tagged_metadata):
            dp = await case.dispatcher()
            await handler(dp, "voice_command")(FakeMessage("/voice Synthetic verification only."))
            await handler(dp, "ask_command")(FakeMessage("/ask voice Synthetic verification only."))
            message = FakeMessage("/voice Synthetic failure verification only.")
            message.upload_error = True
            await handler(dp, "voice_command")(message)
    finally:
        final_status = await close_tracing()
        case.doCleanups()
    if not final_status.active:
        raise RuntimeError(f"Synthetic export failed: {final_status.state}")
    print(json.dumps({"project": cfg.tracing_project_name, "trace_ids": trace_ids}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Explicitly send synthetic traces to configured backend")
    if not parser.parse_args().send:
        parser.error("--send is required for this live network check")
    asyncio.run(main())
