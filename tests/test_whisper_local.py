import importlib.util
import json
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
        (self.root / "preprocessor_config.json").write_text(
            json.dumps({"feature_size": 128}), encoding="utf-8",
        )
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

    def test_large_v3_and_turbo_require_valid_preprocessing_before_model_construction(self):
        preprocessor = self.root / "preprocessor_config.json"
        for model_name in ("large-v3", "turbo"):
            for contents in (None, "", "not JSON", "{}", "[]", '{"feature_size": true}',
                             '{"feature_size": 0}', '{"feature_size": -1}'):
                with self.subTest(model=model_name, preprocessor=contents):
                    self.transcriber.model_name = model_name
                    if contents is None:
                        preprocessor.unlink(missing_ok=True)
                    else:
                        preprocessor.write_text(contents, encoding="utf-8")
                    with patch.dict(sys.modules, self.modules), \
                         patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                         patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
                        with self.assertRaisesRegex(RuntimeError, "download-whisper"):
                            self.transcriber._transcribe_sync(self.root / "audio.ogg")
                    self.model.assert_not_called()

    def test_downloads_require_explicit_opt_in(self):
        self.transcriber.local_files_only = False
        with patch.dict(sys.modules, self.modules):
            self.transcriber._transcribe_sync(self.root / "audio.ogg")
        self.download.assert_not_called()
        self.model.assert_called_once_with("fake-size", device="cpu", compute_type="int8", local_files_only=False)


@unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "optional faster-whisper is not installed")
class WhisperPreprocessingContractTests(unittest.TestCase):
    def test_complete_snapshot_preserves_real_80_and_128_mel_preprocessing_without_weights(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
            from faster_whisper import WhisperModel
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("config.json", "model.bin", "tokenizer.json"):
                    (root / name).write_bytes(b"synthetic")
                with patch("faster_whisper.transcribe.ctranslate2.models.Whisper") as weights, \
                     patch("faster_whisper.transcribe.tokenizers.Tokenizer.from_file", return_value=object()), \
                     patch("faster_whisper.utils.download_model", side_effect=AssertionError("download")):
                    with self.assertRaisesRegex(RuntimeError, "download-whisper"):
                        resolve_whisper_model(str(root), local_files_only=True)
                    weights.assert_not_called()
                    for feature_size in (80, 128):
                        with self.subTest(feature_size=feature_size):
                            weights.reset_mock()
                            (root / "preprocessor_config.json").write_text(
                                json.dumps({"feature_size": feature_size}), encoding="utf-8",
                            )
                            path = resolve_whisper_model(str(root), local_files_only=True)
                            model = WhisperModel(path, device="cpu", local_files_only=True)
                            self.assertEqual(model.feature_extractor.mel_filters.shape[0], feature_size)
                            weights.assert_called_once()
