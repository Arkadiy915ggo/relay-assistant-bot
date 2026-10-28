import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Chat, Message, User

import tg_summary_bot.bot as bot_module
from tg_summary_bot.intent_router import IntentRoute
from tg_summary_bot.speech_synthesizer import SpeechResult
from tg_summary_bot.storage import MessageStore, is_eligible_message_provenance
from tg_summary_bot.voice_presets import VoiceRegistry
from test_joke_handlers import RecordingLock, handler
from test_speech_synthesizer import settings as speech_settings


NOW = datetime.now(timezone.utc)


class FakeMessage:
    def __init__(self, text="", *, message_id=100, sender_id=7):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=1, type="group")
        self.from_user = SimpleNamespace(id=sender_id, is_bot=sender_id == 999,
                                         username="user", full_name="User")
        self.sender_chat = None
        self.date = NOW
        self.text = text
        self.caption = None
        self.reply_to_message = None
        self.entities = []
        self.voice = None
        self.audio = None
        self.video = None
        self.video_note = None
        self.document = None
        self.photo = None
        self.answers = []
        self.reply_ids = []
        self.audio_paths = []
        self.upload_error = False
        self.events = []

    async def answer(self, text, **kwargs):
        response = FakeMessage(text, message_id=200 + len(self.answers), sender_id=999)
        self.answers.append(response)
        self.events.append("text:delivery")
        return response

    async def reply(self, text, **kwargs):
        response = await self.answer(text, **kwargs)
        response.reply_to_message = self
        self.reply_ids.append(self.message_id)
        return response

    async def edit_text(self, text, **kwargs):
        self.text = text

    async def reply_voice(self, voice, **kwargs):
        self.assert_path = Path(voice.path)
        assert self.assert_path.exists(), "cleanup before upload"
        self.audio_paths.append(self.assert_path)
        self.events.append("voice:upload")
        if self.upload_error:
            raise RuntimeError("private Telegram/provider error")
        response = await self.reply("")
        response.voice = SimpleNamespace(file_id="generated", duration=1)
        return response


class FakeSpeech:
    def __init__(self, root, events, *, error=False, unavailable=None, on_generate=None):
        self.root, self.events = root, events
        self.error, self.unavailable_reason = error, unavailable
        self.calls = []
        self.on_generate = on_generate

    async def check_available(self):
        return self.unavailable_reason

    @asynccontextmanager
    async def synthesize(self, text, preset, lock):
        self.calls.append((text, preset.id))
        async with lock:
            self.events.append("tts:run")
            if self.on_generate:
                await self.on_generate()
            if self.error:
                raise RuntimeError("private provider text")
        path = self.root / "fake.ogg"
        path.write_bytes(b"fake")
        try:
            yield SpeechResult(path, 1, preset.id)
        finally:
            path.unlink(missing_ok=True)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_profile_skips_busy_gpu_without_model_or_unload(self):
        gate = asyncio.Event()
        sleeping = asyncio.Event()
        memory = SimpleNamespace(participant_context=AsyncMock(return_value=""), unload=AsyncMock(),
                                 ensure_recent_profiles_current=AsyncMock(return_value=0), unload_profiles=AsyncMock())
        original_sleep = asyncio.sleep
        async def sleep(delay):
            if delay == 2:
                sleeping.set()
                await gate.wait()
            else:
                await original_sleep(delay)
        with patch.object(bot_module, "should_use_memory", return_value=False), \
             patch.object(bot_module.asyncio, "sleep", sleep):
            dp = await bot_module.create_dispatcher(
                self.settings, self.store, SimpleNamespace(), self.assistant, memory,
                SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                self.whisper, self.formatter, self.lock, SimpleNamespace(),
                speech_synthesizer=self.speech, voice_registry=VoiceRegistry(self.root / "local"),
            )
            await handler(dp, "ask_command")(FakeMessage("/ask вопрос"))
            await asyncio.wait_for(sleeping.wait(), 1)
            async with self.lock:
                gate.set()
                await original_sleep(0)
            await dp.emit_shutdown()
        memory.ensure_recent_profiles_current.assert_not_awaited()
        memory.unload_profiles.assert_not_awaited()

    async def test_profile_refresh_is_coalesced_throttled_and_detached(self):
        gate = asyncio.Event()
        sleeping = asyncio.Event()
        unloaded = asyncio.Event()
        marker = ContextVar("test_foreground_marker", default=None)
        marker.set("foreground")
        async def batch(*args, **kwargs):
            self.assertTrue(self.lock.locked())
            self.assertIsNone(marker.get())
            return 0
        async def unload():
            self.assertTrue(self.lock.locked())
            unloaded.set()
        memory = SimpleNamespace(participant_context=AsyncMock(return_value=""), unload=AsyncMock(),
                                 ensure_recent_profiles_current=AsyncMock(side_effect=batch),
                                 unload_profiles=AsyncMock(side_effect=unload))
        original_sleep = asyncio.sleep
        async def sleep(delay):
            if delay == 2:
                sleeping.set()
                await gate.wait()
            else:
                await original_sleep(delay)
        with patch.object(bot_module, "should_use_memory", return_value=False), \
             patch.object(bot_module.asyncio, "sleep", sleep):
            dp = await bot_module.create_dispatcher(
                self.settings, self.store, SimpleNamespace(), self.assistant, memory,
                SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                self.whisper, self.formatter, self.lock, SimpleNamespace(),
                speech_synthesizer=self.speech, voice_registry=VoiceRegistry(self.root / "local"),
            )
            await handler(dp, "ask_command")(FakeMessage("/ask voice первый вопрос"))
            await asyncio.wait_for(sleeping.wait(), 1)
            await handler(dp, "ask_command")(FakeMessage("/ask voice второй вопрос"))
            memory.ensure_recent_profiles_current.assert_not_awaited()
            gate.set()
            await asyncio.wait_for(unloaded.wait(), 1)
            await handler(dp, "ask_command")(FakeMessage("/ask voice третий вопрос"))
            memory.ensure_recent_profiles_current.assert_awaited_once()
            await dp.emit_shutdown()
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MessageStore(self.root / "db.sqlite3")
        await self.store.init()
        self.events = []
        self.lock = RecordingLock(self.events)
        self.settings = speech_settings(self.root)
        self.settings.allowed_chat_ids = set()
        self.settings.default_summary_period = "24h"
        self.settings.max_summary_input_chars = 20000
        self.settings.max_message_chars = 20000
        self.settings.resolved_llm_provider = "ollama"
        self.settings.ollama_model = "fake"
        self.settings.question_model = ""
        self.settings.intent_router_model = ""
        self.settings.transcribe_voice = True
        self.settings.whisper_model = "fake"
        self.settings.whisper_device = "cpu"
        self.settings.whisper_compute_type = "int8"
        self.settings.whisper_language = "ru"
        self.settings.max_voice_seconds = 600
        self.settings.max_transcription_chars = 10000
        self.settings.tts_local_voices_dir = self.root / "local"
        self.speech = FakeSpeech(self.root, self.events)
        self.assistant = SimpleNamespace(ask=AsyncMock(side_effect=self.answer), unload=AsyncMock(side_effect=self.unload))
        self.answer_text = "**Готовый ответ**"
        self.whisper = SimpleNamespace(transcribe=AsyncMock(return_value="Человек говорит"))
        self.formatter = SimpleNamespace(format=AsyncMock(), unload=AsyncMock())

    async def answer(self, *args, **kwargs):
        self.assertTrue(self.lock.locked())
        self.events.append("llm:run")
        return self.answer_text

    async def unload(self):
        self.assertTrue(self.lock.locked())
        self.events.append("llm:unload")

    async def dispatcher(self, router=None):
        return await bot_module.create_dispatcher(
            self.settings, self.store, SimpleNamespace(), self.assistant, None,
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
            self.whisper, self.formatter, self.lock,
            router or SimpleNamespace(), speech_synthesizer=self.speech,
            voice_registry=VoiceRegistry(self.root / "local"),
        )

    async def test_one_answer_text_first_split_replies_lock_and_one_eligible_source(self):
        self.answer_text = "**Ответ**\n" + ("Слова. " * 700)
        self.settings.tts_max_text_chars = 10000
        message = FakeMessage("/ask voice 6h Вопрос?")
        message.events = self.events
        dp = await self.dispatcher()
        await handler(dp, "ask_command")(message)
        self.assistant.ask.assert_awaited_once()
        self.assertEqual(self.assistant.ask.call_args.args[1:3], ("6h", "Вопрос?"))
        self.assertEqual(self.speech.calls[0][0], bot_module.speech_text(self.answer_text))
        self.assertNotIn("Answer for", self.speech.calls[0][0])
        self.assertGreater(len(message.answers), 2)
        self.assertEqual(message.reply_ids, [100] * len(message.answers))
        self.assertLess(self.events.index("llm:unload"), self.events.index("tts:run"))
        tts = self.events.index("tts:run")
        self.assertEqual(self.events[tts - 1], "lock:acquire")
        self.assertIn("text:delivery", self.events[:tts])
        self.assertLess(self.events.index("tts:run"), self.events.index("voice:upload"))
        self.assertFalse(message.audio_paths[0].exists())
        rows = await self.store.get_messages_since(chat_id=1, since=NOW, limit_chars=20000)
        self.assertEqual(len(rows), 1)
        self.assertTrue(is_eligible_message_provenance(rows[0].origin, rows[0].kind))
        self.whisper.transcribe.assert_not_called()
        self.formatter.format.assert_not_called()

    async def test_text_alias_and_question_voice_literal_keep_contract(self):
        dp = await self.dispatcher()
        for callback, command in (("ask_command", "/ask 6h тест"),
                                  ("question_command", "/question voice тест")):
            self.assistant.ask.reset_mock()
            message = FakeMessage(command)
            await handler(dp, callback)(message)
            self.assistant.ask.assert_awaited_once()
            self.assertFalse(message.reply_ids)
        self.assertEqual(self.assistant.ask.call_args.args[2], "voice тест")
        self.assertFalse(self.speech.calls)

    async def test_generation_failure_is_safe_and_never_synthesizes(self):
        self.assistant.ask.side_effect = RuntimeError("private provider detail")
        message = FakeMessage("/ask voice вопрос")
        with self.assertLogs(level="ERROR"):
            await handler(await self.dispatcher(), "ask_command")(message)
        self.assertFalse(self.speech.calls)
        self.assistant.unload.assert_awaited_once()
        self.assertFalse(self.lock.locked())
        self.assertNotIn("private", message.answers[0].text)

    async def test_voice_mode_preserves_profile_memory_and_alias_identity(self):
        memory = SimpleNamespace(
            recent_since=lambda now: now,
            ensure_current=AsyncMock(return_value=1), search=AsyncMock(return_value=[]),
            blocks_as_messages=lambda blocks: [],
            unload=AsyncMock(), participant_context=AsyncMock(return_value="profile context"),
            participant_context_as_message=lambda chat_id, text: text,
            ensure_recent_profiles_current=AsyncMock(return_value=0),
        )
        await self.store.add_chat_bot_alias(chat_id=1, alias="Реле", normalized_alias="реле",
                                            created_by_user_id=7, created_at=NOW)
        with patch.object(bot_module, "should_use_memory", return_value=True):
            dp = await bot_module.create_dispatcher(
                self.settings, self.store, SimpleNamespace(), self.assistant, memory,
                SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                self.whisper, self.formatter, self.lock, SimpleNamespace(),
                speech_synthesizer=self.speech, voice_registry=VoiceRegistry(self.root / "local"),
            )
            await handler(dp, "ask_command")(FakeMessage("/ask voice 7d вопрос"))
            await asyncio.sleep(0)
        self.assistant.ask.assert_awaited_once()
        self.assertEqual(self.assistant.ask.call_args.kwargs["bot_names"], ["Реле"])
        self.assertEqual(self.assistant.ask.call_args.args[0], ["profile context"])
        memory.ensure_current.assert_awaited_once()
        memory.search.assert_awaited_once()
        self.assertGreaterEqual(memory.unload.await_count, 2)

    async def test_direct_speech_provenance_and_reply_link(self):
        message = FakeMessage("/voice@mybot Текст команды")
        await handler(await self.dispatcher(), "voice_command")(message)
        rows = await self.store.get_messages_since(chat_id=1, since=NOW, limit_chars=10000)
        self.assertEqual([(row.text, row.origin, row.kind, row.reply_to_message_id) for row in rows],
                         [("Текст команды", "generated", "tts_text", 100)])
        self.assertFalse(is_eligible_message_provenance(rows[0].origin, rows[0].kind))
        self.assistant.ask.assert_not_called()
        self.whisper.transcribe.assert_not_called()
        self.formatter.format.assert_not_called()

    async def test_combined_persistence_speech_and_upload_failures_safe_partial(self):
        for upload in (False, True):
            with self.subTest(upload=upload):
                self.speech.error = not upload
                message = FakeMessage("/ask voice Вопрос?")
                message.upload_error = upload
                dp = await self.dispatcher()
                with patch.object(self.store, "save_message", AsyncMock(side_effect=RuntimeError("private database"))), \
                     patch.object(bot_module, "log_operation_outcome") as logged, self.assertLogs(level="WARNING"):
                    await handler(dp, "ask_command")(message)
                outcome = logged.call_args.args[0]
                self.assertEqual(outcome.status, "partial")
                self.assertIn("context_persistence_failed", outcome.reason)
                self.assertIn("speech_delivery_failed", outcome.reason)
                self.assertIn("Готовый ответ", message.answers[0].text)
                rendered = " ".join(item.text for item in message.answers)
                self.assertNotIn("private", rendered)
                self.assertIn("сохранить", rendered)
                self.assertIn("озвучку", rendered)
                self.assertFalse((self.root / "fake.ogg").exists())

    async def test_direct_upload_and_persistence_failure_do_not_claim_success(self):
        dp = await self.dispatcher()
        message = FakeMessage("/voice Текст")
        message.upload_error = True
        with patch.object(self.store, "save_message", AsyncMock()) as saved, \
             patch.object(bot_module, "log_operation_outcome") as logged, self.assertLogs(level="WARNING"):
            await handler(dp, "voice_command")(message)
            saved.assert_not_awaited()
            self.assertEqual(logged.call_args.args[0].status, "failed")
        message = FakeMessage("/voice Текст")
        with patch.object(self.store, "save_message", AsyncMock(side_effect=RuntimeError())), \
             patch.object(bot_module, "log_operation_outcome") as logged, self.assertLogs(level="WARNING"):
            await handler(dp, "voice_command")(message)
            self.assertEqual(logged.call_args.args[0].status, "partial")
        self.assertFalse((self.root / "fake.ogg").exists())

    async def test_oversized_answer_full_text_without_synthesis(self):
        self.answer_text = "Длинный ответ " * 400
        message = FakeMessage("/ask voice вопрос")
        await handler(await self.dispatcher(), "ask_command")(message)
        self.assertFalse(self.speech.calls)
        combined = "".join(item.text for item in message.answers)
        self.assertEqual(combined.count("Длинный ответ"), 400)
        self.assertIn("превышен лимит", combined)

    async def test_admission_denied_empty_over_limit_disabled_before_models(self):
        dp = await self.dispatcher()
        for callback, text in (("voice_command", "/voice"), ("voice_command", "/voice " + "a" * 2001),
                               ("ask_command", "/ask voice")):
            await handler(dp, callback)(FakeMessage(text))
        self.speech.unavailable_reason = "missing_model"
        await handler(dp, "ask_command")(FakeMessage("/ask voice вопрос"))
        self.settings.allowed_chat_ids = {2}
        for callback, text in (("ask_command", "/ask voice вопрос"), ("voice_command", "/voice слово"),
                               ("voice_type_command", "/voice_type anubarak")):
            message = FakeMessage(text)
            await handler(dp, callback)(message)
            self.assertFalse(message.answers)
        self.assistant.ask.assert_not_called()
        self.assertFalse(self.speech.calls)

    async def test_selection_invalid_reset_missing_fallback_and_admission_snapshot(self):
        dp = await self.dispatcher()
        await handler(dp, "voice_type_command")(FakeMessage("/voice_type anubarak"))
        await handler(dp, "voice_type_command")(FakeMessage("/voice_type unknown"))
        self.assertEqual(await self.store.get_chat_voice(1), "anubarak")
        await handler(dp, "voice_type_command")(FakeMessage("/voice_type reset"))
        self.assertIsNone(await self.store.get_chat_voice(1))
        await self.store.set_chat_voice(1, "removed")
        message = FakeMessage("/ask voice вопрос")
        async def change_selection(*args, **kwargs):
            await self.store.set_chat_voice(1, "changed_after_admission")
            return "Ответ"
        self.assistant.ask.side_effect = change_selection
        await handler(dp, "ask_command")(message)
        self.assertEqual(self.speech.calls[-1][1], "anubarak")
        self.assertIn("больше недоступен", message.answers[0].text)
        self.assertEqual(await self.store.get_chat_voice(1), "changed_after_admission")

    async def test_select_cartman_for_chat_then_generate_voice(self):
        dp = await self.dispatcher()
        selection = FakeMessage("/voice_type cartman")
        await handler(dp, "voice_type_command")(selection)
        self.assertEqual(await self.store.get_chat_voice(1), "cartman")
        self.assertIn("Картман", selection.answers[0].text)
        self.assertIsNone(await self.store.get_chat_voice(2))
        await handler(dp, "voice_command")(FakeMessage("/voice Привет!"))
        self.assertEqual(self.speech.calls, [("Привет!", "cartman")])
        self.assertEqual(self.settings.tts_default_voice, "anubarak")

    async def test_own_bot_auto_slash_routed_transcribe_never_downloads_or_formats(self):
        router = SimpleNamespace(route=AsyncMock(return_value=IntentRoute("transcribe", None, None, True, "ok")),
                                 unload=AsyncMock())
        dp = await self.dispatcher(router)
        source = FakeMessage(sender_id=999)
        source.voice = SimpleNamespace(file_id="own", duration=1)
        bot = SimpleNamespace(id=999)
        request = FakeMessage("/transcribe")
        request.reply_to_message = source
        with patch.object(bot_module, "download_audio_message", AsyncMock()) as download:
            await handler(dp, "transcribe_audio_message")(source, bot)
            await handler(dp, "transcribe_command")(request, bot)
            addressed = FakeMessage("Реле, расшифруй")
            addressed.reply_to_message = source
            with patch.object(bot_module, "cached_bot_identity", AsyncMock(return_value=(999, "mybot", "Реле"))), \
                 patch.object(bot_module, "extract_addressed_request", return_value="расшифруй"):
                await handler(dp, "save_regular_message")(addressed, bot)
            download.assert_not_called()
        self.assertFalse(source.answers)
        self.assertIn("самого бота", request.answers[0].text)
        self.whisper.transcribe.assert_not_called()
        self.formatter.format.assert_not_called()

    async def test_human_voice_is_still_transcribed(self):
        self.formatter = None
        dp = await self.dispatcher()
        message = FakeMessage()
        message.voice = SimpleNamespace(file_id="human", duration=1)
        path = self.root / "human.ogg"
        path.write_bytes(b"human")
        with patch.object(bot_module, "download_audio_message", AsyncMock(return_value=path)) as download:
            await handler(dp, "transcribe_audio_message")(message, SimpleNamespace(id=999))
            download.assert_awaited_once()
        self.whisper.transcribe.assert_awaited_once()
        rows = await self.store.get_messages_since(chat_id=1, since=NOW, limit_chars=10000)
        self.assertEqual((rows[0].origin, rows[0].kind), ("incoming", "voice_transcript"))


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_parser_defaults_voice_position_and_username_admission(self):
        for text, expected in (("/ask вопрос", ("24h", "вопрос", False)),
                               ("/ask voice 6h вопрос здесь", ("6h", "вопрос здесь", True)),
                               ("/ask@mybot voice вопрос", ("24h", "вопрос", True)),
                               ("/ask 6h voice вопрос", ("6h", "voice вопрос", False)),
                               ("/ask voice", None), ("/ask voice 6h", None), ("/ask", None)):
            self.assertEqual(bot_module.parse_ask_command(text, "24h"), expected)
        self.assertEqual(bot_module.parse_question_command("/question voice вопрос", "24h"), ("24h", "voice вопрос"))
        self.assertEqual(bot_module.speech_text("## Заголовок\n**Слова**, *важно*, _да_, `some_id`. [Источник](https://example.com)."),
                         "Заголовок\nСлова, важно, да, some_id. Источник.")
        bot = Bot("999:test")
        try:
            for name in ("voice", "voice_type", "ask"):
                message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), text=f"/{name}@mybot text")
                with patch.object(Bot, "get_me", AsyncMock(return_value=User(id=999, is_bot=True, first_name="Bot", username="mybot"))):
                    self.assertTrue(await Command(name)(message, bot))
                    wrong = message.model_copy(update={"text": f"/{name}@otherbot text"})
                    self.assertFalse(await Command(name)(wrong, bot))
        finally:
            await bot.session.close()
