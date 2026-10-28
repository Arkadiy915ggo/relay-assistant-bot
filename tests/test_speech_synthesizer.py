import asyncio
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_summary_bot.speech_synthesizer import COSYVOICE_MODEL_FILES, SpeechError, SpeechSynthesizer, run_process
from tg_summary_bot.voice_presets import VoiceRegistry


def settings(root, **overrides):
    values = dict(tts_enabled=True, tts_provider="cosyvoice3", tts_default_voice="anubarak",
                  tts_python_path=Path(sys.executable), tts_cosyvoice_upstream_dir=root,
                  tts_model_dir=root, tts_output_dir=root / "outputs", tts_max_text_chars=2000,
                  tts_max_audio_seconds=180, tts_timeout_seconds=1,
                  tts_encoding_timeout_seconds=1, tts_upload_timeout_seconds=1)
    values.update(overrides)
    return SimpleNamespace(**values)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_bounded_capture_and_failure(self):
        output = await run_process([sys.executable, "-c", "import sys; print('x'*50000); sys.stderr.write('y'*50000)"], timeout=2)
        self.assertLessEqual(len(output), 16384)
        with self.assertRaises(SpeechError):
            await run_process([sys.executable, "-c", "raise SystemExit(2)"], timeout=2)

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    async def test_timeout_cancel_kill_real_exit_and_no_late_writes_before_unlock(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pidfile = root / "pid"
                late = root / "late"
                code = ("import signal,os,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                        f"time.sleep(0.5); pathlib.Path({str(late)!r}).write_text('late'); time.sleep(10)")
                lock = asyncio.Lock()
                async def operation():
                    async with lock:
                        await run_process([sys.executable, "-c", code], timeout=0.1 if not cancel else 10, kill_grace=0.05)
                task = asyncio.create_task(operation())
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(pidfile.exists())
                pid = int(pidfile.read_text())
                self.assertTrue(lock.locked())
                if cancel:
                    task.cancel()
                    # Repeated cancellation cannot release the lock during kill/wait.
                    await asyncio.sleep(0.01)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                    await task
                self.assertFalse(lock.locked())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                await asyncio.sleep(0.5)
                self.assertFalse(late.exists())


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cartman_encodes_without_any_audio_effect_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = VoiceRegistry(root / "local")
            synth = SpeechSynthesizer(settings(root), registry)
            synth.unavailable_reason = None
            lock = asyncio.Lock()
            encoded = []
            async def fake_run(argv, *, timeout, payload=None):
                if payload:
                    self.assertTrue(lock.locked())
                    request = json.loads(payload)
                    self.assertEqual(request["reference"], str(registry.presets["cartman"].reference))
                    self.assertEqual(request["transcript"], registry.presets["cartman"].transcript)
                    with wave.open(request["output"], "wb") as audio:
                        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                        audio.writeframes(b"\0\0" * 24000)
                    return b"{}"
                self.assertFalse(lock.locked())
                if argv[0] == "ffmpeg":
                    encoded.append(argv)
                    self.assertNotIn("-af", argv)
                    self.assertNotIn("-filter_complex", argv)
                    self.assertNotIn("atempo", " ".join(argv))
                    self.assertNotIn("rubberband", " ".join(argv))
                    self.assertIn("libopus", argv)
                    Path(argv[-1]).write_bytes(b"OggS")
                    return b""
                return b'{"format":{"duration":1.0065},"streams":[{"codec_name":"opus"}]}'
            with patch("tg_summary_bot.speech_synthesizer.run_process", fake_run):
                async with synth.synthesize("Привет!", registry.presets["cartman"], lock) as result:
                    self.assertEqual(result.preset_id, "cartman")
                    self.assertTrue(result.path.is_file())
            self.assertEqual(len(encoded), 1)
            self.assertEqual(list((root / "outputs").iterdir()), [])

    async def test_runner_and_encoder_cancellation_wait_before_cleanup(self):
        for stage in ("runner", "encoder"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                registry = VoiceRegistry(root / "local")
                synth = SpeechSynthesizer(settings(root, tts_timeout_seconds=10), registry)
                synth.unavailable_reason = None
                started = asyncio.Event()
                pidfile = root / "pid"
                lock = asyncio.Lock()
                async def fake_run(argv, *, timeout, payload=None):
                    if payload and stage == "encoder":
                        request = json.loads(payload)
                        with wave.open(request["output"], "wb") as audio:
                            audio.setparams((1, 2, 100, 0, "NONE", "not compressed"))
                            audio.writeframes(b"\0\0" * 100)
                        return b"{}"
                    self.assertEqual(lock.locked(), stage == "runner")
                    code = ("import signal,os,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                            f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(10)")
                    started.set()
                    return await run_process([sys.executable, "-c", code], timeout=10, kill_grace=0.05)
                async def operation():
                    async with synth.synthesize("текст", registry.presets["anubarak"], lock):
                        self.fail("cancelled request must not yield")
                with patch("tg_summary_bot.speech_synthesizer.run_process", fake_run):
                    task = asyncio.create_task(operation())
                    await started.wait()
                    for _ in range(100):
                        if pidfile.exists():
                            break
                        await asyncio.sleep(0.005)
                    self.assertTrue(pidfile.exists())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertFalse(lock.locked())
                self.assertEqual(list((root / "outputs").iterdir()), [])
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)

    async def test_limits_encoding_lock_and_lifetime_success_failure(self):
        for failure in (None, "missing_wav", "invalid_wav", "long_audio", "encode", "missing_ogg", "bad_opus", "long_opus"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                registry = VoiceRegistry(root / "local")
                synth = SpeechSynthesizer(settings(root), registry)
                synth.unavailable_reason = None
                lock = asyncio.Lock()
                commands = []
                async def fake_run(argv, *, timeout, payload=None):
                    commands.append(argv)
                    if payload:
                        self.assertTrue(lock.locked())
                        request = json.loads(payload)
                        self.assertNotIn("Слова", " ".join(argv))
                        self.assertEqual(request["text"], "Слова")
                        if failure != "missing_wav":
                            with wave.open(request["output"], "wb") as audio:
                                audio.setparams((1, 2, 100, 0, "NONE", "not compressed"))
                                audio.writeframes(b"\0\0" * (20000 if failure == "long_audio" else 100))
                            if failure == "invalid_wav":
                                Path(request["output"]).write_bytes(b"bad")
                        return b"{}"
                    self.assertFalse(lock.locked())
                    if argv[0] == "ffmpeg":
                        if failure == "encode":
                            raise SpeechError("encoding_failed")
                        self.assertIn("atempo=0.85,rubberband=tempo=1:pitch=0.94387431:formant=preserved:pitchq=quality", argv)
                        if failure != "missing_ogg":
                            Path(argv[-1]).write_bytes(b"OggS")
                        return b""
                    return json.dumps({"format": {"duration": 200 if failure == "long_opus" else 1.2},
                                       "streams": [{"codec_name": "wav" if failure == "bad_opus" else "opus"}]}).encode()
                with patch("tg_summary_bot.speech_synthesizer.run_process", fake_run):
                    if failure:
                        with self.assertRaises(SpeechError):
                            async with synth.synthesize("Слова", registry.presets["anubarak"], lock):
                                self.fail("invalid audio must not be delivered")
                    else:
                        async with synth.synthesize("Слова", registry.presets["anubarak"], lock) as result:
                            self.assertTrue(result.path.exists())
                            path = result.path
                        self.assertFalse(path.exists())
                self.assertEqual(list((root / "outputs").iterdir()), [])
                self.assertFalse(lock.locked())

    async def test_rejects_input_before_lock_process_or_output_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = VoiceRegistry(root / "local")
            synth = SpeechSynthesizer(settings(root), registry)
            synth.unavailable_reason = None
            with patch("tg_summary_bot.speech_synthesizer.run_process") as process:
                with self.assertRaises(SpeechError):
                    async with synth.synthesize("x" * 2001, registry.presets["anubarak"], asyncio.Lock()):
                        pass
                process.assert_not_called()
            self.assertFalse((root / "outputs").exists())

    async def test_disabled_and_broken_setup_without_imports_or_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = VoiceRegistry(root)
            with patch("tg_summary_bot.speech_synthesizer.run_process") as process:
                for overrides, expected in (({"tts_enabled": False}, "disabled"),
                                            ({"tts_default_voice": "missing"}, "invalid_default_voice"),
                                            ({"tts_max_text_chars": 0}, "invalid_limits"),
                                            ({"tts_python_path": root / "missing"}, "missing_python")):
                    synth = SpeechSynthesizer(settings(root, **overrides), registry)
                    self.assertEqual(await synth.check_available(), expected)
                process.assert_not_called()

    async def test_dependency_check_safe_diagnoses_without_loading_models(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cosyvoice/cli").mkdir(parents=True)
            (root / "cosyvoice/cli/cosyvoice.py").touch()
            for name in COSYVOICE_MODEL_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake model fixture")
            registry = VoiceRegistry(root / "local")
            for response, expected in ((b'{"ready":true}', None),
                                       (b'{"ready":false,"error":"cuda_unavailable"}', "cuda_unavailable"),
                                       (b'{"ready":false,"error":"private provider exception"}', "runner_unavailable"),
                                       (b'[]', "runner_unavailable")):
                with self.subTest(response=response):
                    async def fake_run(argv, **kwargs):
                        if "filter=rubberband" in argv:
                            return b"Filter rubberband"
                        if "encoder=libopus" in argv:
                            return b"Encoder libopus"
                        self.assertIn("--check", argv)
                        self.assertNotIn("payload", kwargs)
                        return response
                    synth = SpeechSynthesizer(settings(root), registry)
                    with patch("tg_summary_bot.speech_synthesizer.run_process", fake_run), \
                         patch("tg_summary_bot.speech_synthesizer.shutil.which", return_value="fake"):
                        self.assertEqual(await synth.check_available(), expected)
