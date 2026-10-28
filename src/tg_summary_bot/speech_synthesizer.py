from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import tempfile
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from tg_summary_bot.config import Settings
from tg_summary_bot.observability import trace_stage
from tg_summary_bot.voice_presets import VoicePreset, VoiceRegistry


COSYVOICE_MODEL_FILES = (
    "cosyvoice3.yaml", "llm.pt", "flow.pt", "hift.pt", "campplus.onnx",
    "speech_tokenizer_v3.onnx", "CosyVoice-BlankEN/config.json",
    "CosyVoice-BlankEN/model.safetensors", "CosyVoice-BlankEN/tokenizer_config.json",
    "CosyVoice-BlankEN/vocab.json", "CosyVoice-BlankEN/merges.txt",
)


class SpeechError(RuntimeError):
    """Content-free, safe failure code."""


async def _drain(stream: asyncio.StreamReader) -> bytes:
    tail = b""
    while chunk := await stream.read(8192):
        tail = (tail + chunk)[-16384:]
    return tail


async def _finish_despite_cancellation(task: asyncio.Task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run_process(argv: list[str], *, timeout: float, payload: bytes | None = None,
                      kill_grace: float = 2) -> bytes:
    """Bounded output, kill escalation, and real exit before returning on any path."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    ))
    cancelled = False
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        cancelled = True
        process = await _finish_despite_cancellation(spawn)

    async def communicate():
        stdout = asyncio.create_task(_drain(process.stdout))
        stderr = asyncio.create_task(_drain(process.stderr))
        try:
            if payload is not None:
                try:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
            await process.wait()
            result = await stdout
            await stderr
            return result
        finally:
            # Both pipe consumers must finish before work directories are removed.
            await asyncio.gather(stdout, stderr)

    io_task = asyncio.create_task(communicate())

    async def stop():
        def send(sig):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, sig)
                elif sig == signal.SIGTERM:
                    process.terminate()
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        send(signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(io_task), kill_grace)
        except asyncio.TimeoutError:
            send(signal.SIGKILL)
        await io_task

    try:
        if cancelled:
            raise asyncio.CancelledError
        result = await asyncio.wait_for(asyncio.shield(io_task), timeout)
        if process.returncode:
            raise SpeechError("process_failed")
        return result
    except (asyncio.CancelledError, asyncio.TimeoutError):
        await _finish_despite_cancellation(asyncio.create_task(stop()))
        raise
    except BaseException:
        await _finish_despite_cancellation(asyncio.create_task(stop()))
        raise


def speech_text(answer: str) -> str:
    """Remove presentation markup only; never rewrite or recognize the answer."""
    answer = re.sub(r"\[([^\]]+)\]\([^\n)]+\)", r"\1", answer)
    answer = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", answer)
    answer = answer.replace("**", "").replace("__", "").replace("`", "")
    answer = re.sub(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", answer)
    return answer.strip()


@dataclass(frozen=True)
class SpeechResult:
    path: Path
    duration: float
    preset_id: str
    provider: str = "cosyvoice3"
    model: str = "Fun-CosyVoice3-0.5B-2512"


class SpeechSynthesizer:
    def __init__(self, settings: Settings, registry: VoiceRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.unavailable_reason: str | None = "not_checked"
        self.runner = Path(__file__).with_name("cosyvoice_runner.py")

    def runner_args(self) -> list[str]:
        return [str(self.settings.tts_python_path.absolute()), str(self.runner),
                "--upstream", str(self.settings.tts_cosyvoice_upstream_dir.resolve()),
                "--model", str(self.settings.tts_model_dir.resolve())]

    async def check_available(self) -> str | None:
        try:
            return await self._check_available()
        except (OSError, ValueError, RuntimeError):
            self.unavailable_reason = "configuration_unreadable"
            return self.unavailable_reason

    async def _check_available(self) -> str | None:
        s = self.settings
        reason = None
        if not s.tts_enabled:
            reason = "disabled"
        elif s.tts_provider != "cosyvoice3":
            reason = "unsupported_provider"
        elif s.tts_default_voice not in self.registry.presets:
            reason = "invalid_default_voice"
        elif any(type(value) is not int or value <= 0 for value in (
            s.tts_max_text_chars, s.tts_max_audio_seconds, s.tts_timeout_seconds,
            s.tts_encoding_timeout_seconds, s.tts_upload_timeout_seconds,
        )):
            reason = "invalid_limits"
        elif not s.tts_python_path.is_file() or not os.access(s.tts_python_path, os.X_OK):
            reason = "missing_python"
        elif not (s.tts_cosyvoice_upstream_dir / "cosyvoice/cli/cosyvoice.py").is_file():
            reason = "missing_upstream"
        elif any(not (s.tts_model_dir / name).is_file() or (s.tts_model_dir / name).stat().st_size == 0
                 for name in COSYVOICE_MODEL_FILES):
            reason = "missing_model"
        elif not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            reason = "missing_ffmpeg"
        else:
            try:
                filters = await run_process(["ffmpeg", "-hide_banner", "-h", "filter=rubberband"], timeout=10)
                encoders = await run_process(["ffmpeg", "-hide_banner", "-h", "encoder=libopus"], timeout=10)
                if b"Filter rubberband" not in filters or b"Encoder libopus" not in encoders:
                    reason = "missing_rubberband_or_libopus"
                else:
                    result = await run_process(self.runner_args() + ["--check"], timeout=30)
                    info = json.loads(result.splitlines()[-1]) if result.splitlines() else {}
                    if info.get("ready") is not True:
                        code = info.get("error")
                        reason = code if code in {"wrong_python_version", "missing_dependencies",
                                                  "cuda_unavailable", "runner_incompatible"} else "runner_unavailable"
            except (OSError, ValueError, TypeError, AttributeError, SpeechError, asyncio.TimeoutError):
                reason = "runner_unavailable"
        self.unavailable_reason = reason
        return reason

    @asynccontextmanager
    async def synthesize(self, text: str, preset: VoicePreset, gpu_lock: asyncio.Lock):
        s = self.settings
        if self.unavailable_reason:
            raise SpeechError("unavailable")
        if not text.strip() or len(text) > s.tts_max_text_chars:
            raise SpeechError("text_limit")
        s.tts_output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="speech-", dir=s.tts_output_dir) as temporary:
            work = Path(temporary)
            wav = work / "raw.wav"
            output = work / "voice.ogg"
            payload = json.dumps({"text": text, "reference": str(preset.reference),
                                  "transcript": preset.transcript, "output": str(wav),
                                  "max_raw_seconds": s.tts_max_audio_seconds * preset.speed},
                                 ensure_ascii=False).encode()
            with trace_stage("speech.gpu_wait", {"voice_id": preset.id}):
                await gpu_lock.acquire()
            try:
                with trace_stage("speech.synthesize", {"voice_id": preset.id, "input_chars": len(text),
                                                     "model_speed": 1.0, "post_speed": preset.speed,
                                                     "pitch_semitones": preset.pitch_semitones}):
                    await run_process(self.runner_args(), payload=payload, timeout=s.tts_timeout_seconds)
            finally:
                gpu_lock.release()
            try:
                with wave.open(str(wav), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                if not 0 < duration / preset.speed <= s.tts_max_audio_seconds:
                    raise SpeechError("audio_limit")
            except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
                raise SpeechError("invalid_audio") from exc
            filters = []
            if preset.speed != 1.0:
                filters.append(f"atempo={preset.speed}")
            if preset.pitch_semitones != 0:
                factor = 2 ** (preset.pitch_semitones / 12)
                formant = "preserved" if preset.preserve_formants else "shifted"
                filters.append(f"rubberband=tempo=1:pitch={factor:.8f}:formant={formant}:pitchq=quality")
            filter_args = ["-af", ",".join(filters)] if filters else []
            with trace_stage("speech.encode", {"voice_id": preset.id, "effect_filters": len(filters)}):
                await run_process(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                                   "-i", str(wav), *filter_args, "-ac", "1", "-ar", "48000",
                                   "-c:a", "libopus", "-b:a", "48k", str(output)],
                                  timeout=s.tts_encoding_timeout_seconds)
            with trace_stage("speech.validate"):
                probe = await run_process(["ffprobe", "-v", "error", "-show_entries",
                                           "format=duration:stream=codec_name", "-of", "json", str(output)],
                                          timeout=s.tts_encoding_timeout_seconds)
                try:
                    info = json.loads(probe)
                    duration = float(info["format"]["duration"])
                    if (not math.isfinite(duration) or not 0 < duration <= s.tts_max_audio_seconds
                            or info["streams"][0]["codec_name"] != "opus" or output.stat().st_size == 0):
                        raise ValueError("invalid output")
                except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                    raise SpeechError("invalid_output") from exc
            yield SpeechResult(output, duration, preset.id)
