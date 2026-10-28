import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from tg_summary_bot.config import load_settings
from tg_summary_bot.storage import MessageStore, is_eligible_message_provenance
from tg_summary_bot.voice_presets import VoiceRegistry, load_preset


def make_preset(root, name="sample", voice_id="sample", **changes):
    directory = root / name
    directory.mkdir(parents=True)
    with wave.open(str(directory / "reference.wav"), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 240)
    data = {"version": 1, "id": voice_id, "display_name": "Sample", "reference": "reference.wav",
            "transcript": "Точный текст.", "language": "ru",
            "post_processing": {"speed": 0.85, "pitch_semitones": -1, "preserve_formants": True}}
    data.update(changes)
    (directory / "preset.json").write_text(json.dumps(data), encoding="utf-8")
    return directory


class RegistryTests(unittest.TestCase):
    def test_bundled_discovery_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as temp, patch("os.getcwd", return_value=temp):
            registry = VoiceRegistry(Path(temp))
            preset, fallback = registry.resolve(None, "anubarak")
            self.assertEqual(preset.transcript, "Мы, нежить, народ работящий. Такая уж наша тяжкая доля.")
            self.assertEqual((preset.speed, preset.pitch_semitones, preset.preserve_formants), (0.85, -1, True))
            self.assertFalse(fallback)
            self.assertTrue(preset.reference.is_file())

    def test_duplicates_excluded_and_bundled_not_overridden(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_preset(root, "a", "same")
            make_preset(root, "b", "same")
            make_preset(root, "imposter", "anubarak")
            make_preset(root, "valid")
            registry = VoiceRegistry(root)
            self.assertEqual(set(registry.presets), {"anubarak", "cartman", "sample"})
            self.assertEqual(len(registry.diagnostics), 2)
            self.assertEqual(registry.presets["anubarak"].display_name, "Ануб’арак")

    def test_types_invalid_json_missing_audio_and_bounds(self):
        cases = [{"version": True}, {"id": "reset"}, {"id": "../x"}, {"transcript": " "},
                 {"language": "xx"}, {"post_processing": {"speed": True}},
                 {"reference": "missing.wav"},
                 {"post_processing": {"speed": 0.85, "pitch_semitones": float("nan"), "preserve_formants": True}}]
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                make_preset(root, **changes)
                registry = VoiceRegistry(root, root / "absent")
                self.assertFalse(registry.presets)
                self.assertTrue(registry.diagnostics)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = make_preset(root)
            (directory / "preset.json").write_text("broken")
            self.assertTrue(VoiceRegistry(root).diagnostics)

    def test_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = make_preset(root, "outside")
            local = root / "local"
            local.mkdir()
            for name, reference in (("relative", "../../outside/reference.wav"),
                                    ("absolute", str(original / "reference.wav"))):
                directory = make_preset(local, name, reference=reference)
                with self.assertRaises(ValueError):
                    load_preset(directory)
            directory = make_preset(local, "symlink")
            (directory / "reference.wav").unlink()
            (directory / "reference.wav").symlink_to(original / "reference.wav")
            with self.assertRaises(ValueError):
                load_preset(directory)
            (local / "escaped").symlink_to(original, target_is_directory=True)
            self.assertFalse(VoiceRegistry(local, root / "missing").presets)

    def test_missing_selected_fallback_and_invalid_default(self):
        registry = VoiceRegistry(Path("/nonexistent-local-voices"))
        preset, fallback = registry.resolve("removed", "anubarak")
        self.assertEqual(preset.id, "anubarak")
        self.assertTrue(fallback)
        with self.assertRaises(ValueError):
            registry.resolve("anubarak", "broken")

    def test_cartman_bundled_reference_and_neutral_settings(self):
        registry = VoiceRegistry(Path("/nonexistent-local-voices"))
        preset, fallback = registry.resolve("cartman", "anubarak")
        self.assertFalse(fallback)
        self.assertEqual(preset.display_name, "Картман")
        self.assertEqual((preset.speed, preset.pitch_semitones), (1.0, 0))
        self.assertEqual(preset.transcript,
                         "Если ты вдруг не заметил, мы пока работаем в убыток. Мы продаем недостаточно, чтобы окупиться. "
                         "Мои дилеры держат меня за яйца. Все стоит денег, Баттерс. Вейпы, жидкость, даже плащ твой. "
                         "Мы не можем тормозить работу, понял?")
        with wave.open(str(preset.reference), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 11.4, places=3)

    def test_optional_config_does_not_read_env_or_require_ml(self):
        with patch("tg_summary_bot.config.load_dotenv"), patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "123:test", "TTS_MAX_TEXT_CHARS": "broken", "TTS_ENABLED": "false",
        }, clear=True):
            settings = load_settings()
        self.assertFalse(settings.tts_enabled)
        self.assertEqual(settings.tts_max_text_chars, 0)
        self.assertTrue(settings.tts_local_voices_dir.is_absolute())


class VoiceStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_selection_restart_reset_and_idempotent_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.sqlite3"
            store = MessageStore(path)
            await store.init()
            await store.set_chat_voice(1, "anubarak")
            await store.set_chat_voice(2, "local")
            store = MessageStore(path)
            await store.init()
            self.assertEqual(await store.get_chat_voice(1), "anubarak")
            self.assertEqual(await store.get_chat_voice(2), "local")
            await store.reset_chat_voice(1)
            await store.reset_chat_voice(1)
            self.assertIsNone(await store.get_chat_voice(1))
            self.assertEqual(await store.get_chat_voice(2), "local")
            self.assertFalse(is_eligible_message_provenance("generated", "tts_text"))
