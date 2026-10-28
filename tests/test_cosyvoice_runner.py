import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tg_summary_bot.speech_synthesizer as speech_module


class RunnerProtocolTests(unittest.TestCase):
    def test_zero_shot_json_protocol_model_speed_and_pcm_output(self):
        chunk = SimpleNamespace(shape=(1, 100), cpu=lambda: chunk)
        inference = Mock(return_value=[{"tts_speech": chunk}])
        model = SimpleNamespace(sample_rate=24000, inference_zero_shot=inference)
        auto = Mock(return_value=model)
        save = Mock()
        modules = {
            "torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), cat=Mock(return_value=chunk)),
            "torchaudio": SimpleNamespace(save=save),
            "cosyvoice.cli.cosyvoice": SimpleNamespace(AutoModel=auto),
        }
        with tempfile.TemporaryDirectory() as temp:
            request = {"text": "Текст без переписывания.", "transcript": "Оригинальный текст.",
                       "reference": temp + "/reference.wav", "output": temp + "/raw.wav", "max_raw_seconds": 180}
            output = io.StringIO()
            with patch.dict("sys.modules", modules), patch("sys.version_info", (3, 10)), \
                 patch("sys.argv", ["runner", "--upstream", temp, "--model", temp]), \
                 patch("sys.stdin", io.StringIO(json.dumps(request))), redirect_stdout(output), \
                 patch("sys.path", list(sys.path)):
                runpy.run_path(str(Path(speech_module.__file__).with_name("cosyvoice_runner.py")), run_name="__main__")
            inference.assert_called_once_with(
                request["text"], "You are a helpful assistant.<|endofprompt|>Оригинальный текст.",
                request["reference"], stream=False, speed=1.0, text_frontend=False,
            )
            self.assertEqual(save.call_args.kwargs, {"encoding": "PCM_S", "bits_per_sample": 16})
            self.assertEqual(save.call_args.args[0], request["output"])
            self.assertEqual(json.loads(output.getvalue())["sample_rate"], 24000)
            self.assertNotIn(request["text"], output.getvalue())

    def test_wrong_python_reports_content_free_check_diagnosis(self):
        output = io.StringIO()
        with patch("sys.version_info", (3, 11)), \
             patch("sys.argv", ["runner", "--upstream", "/missing", "--model", "/missing", "--check"]), \
             redirect_stdout(output):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(Path(speech_module.__file__).with_name("cosyvoice_runner.py")), run_name="__main__")
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"ready": False, "error": "wrong_python_version"})
