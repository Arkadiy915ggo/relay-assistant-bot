import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_summary_bot.transcriber import FasterWhisperTranscriber, resolve_whisper_model


class WhisperLocalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (self.root / name).write_bytes(b"synthetic")
        self.download = Mock(return_value=str(self.root))
        self.model = Mock(return_value=SimpleNamespace(transcribe=Mock(return_value=(
            [SimpleNamespace(text=" local transcript ")], SimpleNamespace(),
        ))))
        self.modules = {"faster_whisper": SimpleNamespace(WhisperModel=self.model),
                        "faster_whisper.utils": SimpleNamespace(download_model=self.download)}
        cfg = SimpleNamespace(whisper_model="fake-size", whisper_device="cpu",
                              whisper_compute_type="int8", whisper_language="ru",
                              whisper_local_files_only=True)
        self.transcriber = FasterWhisperTranscriber(cfg)

    def test_cached_model_uses_local_resolution_and_explicit_directory(self):
        with patch.dict(sys.modules, self.modules), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            self.assertEqual(self.transcriber._transcribe_sync(self.root / "audio.ogg"), "local transcript")
        self.download.assert_called_once_with("fake-size", local_files_only=True)
        self.model.assert_called_once_with(str(self.root), device="cpu", compute_type="int8", local_files_only=True)

    def test_missing_or_incomplete_model_fails_before_whisper_can_download_tokenizer(self):
        for incomplete in (False, True):
            self.download.side_effect = None if incomplete else FileNotFoundError("private cache path")
            if incomplete:
                (self.root / "tokenizer.json").unlink()
            with patch.dict(sys.modules, self.modules):
                with self.assertRaisesRegex(RuntimeError, "download-whisper") as caught:
                    self.transcriber._transcribe_sync(self.root / "audio.ogg")
            self.assertNotIn("private cache path", str(caught.exception))
        self.model.assert_not_called()

    def test_complete_explicit_directory_does_not_resolve_hub_name(self):
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(resolve_whisper_model(str(self.root), local_files_only=True), str(self.root))
        self.download.assert_not_called()

    def test_downloads_require_explicit_opt_in(self):
        self.transcriber.local_files_only = False
        with patch.dict(sys.modules, self.modules):
            self.transcriber._transcribe_sync(self.root / "audio.ogg")
        self.download.assert_not_called()
        self.model.assert_called_once_with("fake-size", device="cpu", compute_type="int8", local_files_only=False)
