"""Validated, immutable voice library. No model imports or executable preset fields."""
from __future__ import annotations

import json
import math
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


REFERENCE_MIN_SAMPLE_RATE = 16000
REFERENCE_MAX_SECONDS = 30


class ReferenceAudioError(ValueError):
    """Content-free diagnosis for references outside the CosyVoice input contract."""


@dataclass(frozen=True)
class VoicePreset:
    id: str
    display_name: str
    reference: Path
    transcript: str
    language: str
    speed: float
    pitch_semitones: float
    preserve_formants: bool


def load_preset(directory: Path) -> VoicePreset:
    root = directory.resolve(strict=True)
    manifest = directory / "preset.json"
    if not manifest.resolve(strict=True).is_relative_to(root):
        raise ValueError("manifest escapes preset root")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("unsupported preset version")
    for field in ("id", "display_name", "reference", "transcript", "language"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"invalid {field}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", data["id"]) or data["id"] == "reset":
        raise ValueError("invalid preset id")
    if data["language"] not in {"ru", "en", "zh", "ja", "ko", "de", "fr", "es", "it"}:
        raise ValueError("unsupported language")
    reference_name = Path(data["reference"])
    if reference_name.is_absolute() or ".." in reference_name.parts:
        raise ValueError("reference escapes preset root")
    reference = (root / reference_name).resolve(strict=True)
    if not reference.is_relative_to(root) or not reference.is_file():
        raise ValueError("reference escapes preset root")
    with wave.open(str(reference), "rb") as audio:
        frames, sample_rate = audio.getnframes(), audio.getframerate()
        if frames <= 0 or sample_rate <= 0:
            raise ValueError("empty reference")
        if sample_rate < REFERENCE_MIN_SAMPLE_RATE:
            raise ReferenceAudioError("reference_sample_rate_below_16000_hz")
        if frames > sample_rate * REFERENCE_MAX_SECONDS:
            raise ReferenceAudioError("reference_longer_than_30_seconds")
    post = data.get("post_processing")
    if not isinstance(post, dict):
        raise ValueError("missing post_processing")
    for field, low, high in (("speed", 0.5, 2), ("pitch_semitones", -12, 12)):
        value = post.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"invalid {field}")
    if type(post.get("preserve_formants")) is not bool:
        raise ValueError("invalid preserve_formants")
    return VoicePreset(data["id"], data["display_name"], reference, data["transcript"],
                       data["language"], float(post["speed"]), float(post["pitch_semitones"]),
                       post["preserve_formants"])


class VoiceRegistry:
    def __init__(self, local_root: Path, bundled_root: Path | None = None) -> None:
        bundled_root = bundled_root or Path(__file__).with_name("voice_presets")
        presets: dict[str, VoicePreset] = {}
        diagnostics: list[str] = []
        for root in (bundled_root, local_root):
            candidates: dict[str, list[VoicePreset]] = {}
            try:
                if not root.is_dir():
                    continue
                directories = sorted(root.iterdir())
            except OSError:
                diagnostics.append("voice library unreadable")
                continue
            for directory in directories:
                try:
                    if not directory.is_dir():
                        continue
                    if not directory.resolve().is_relative_to(root.resolve()):
                        raise ValueError("preset directory escapes library root")
                    preset = load_preset(directory)
                    candidates.setdefault(preset.id, []).append(preset)
                except (OSError, EOFError, ValueError, TypeError, RuntimeError, wave.Error) as exc:
                    reason = str(exc) if isinstance(exc, ReferenceAudioError) else type(exc).__name__
                    diagnostics.append(f"{directory.name}: invalid preset ({reason})")
            for voice_id, group in candidates.items():
                if len(group) != 1 or voice_id in presets:
                    diagnostics.append(f"{voice_id}: duplicate preset excluded")
                    continue
                presets[voice_id] = group[0]
        self.presets = MappingProxyType(presets)
        self.diagnostics = tuple(diagnostics)

    def resolve(self, selected: str | None, default: str) -> tuple[VoicePreset, bool]:
        if default not in self.presets:
            raise ValueError("configured default voice is unavailable")
        if selected in self.presets:
            return self.presets[selected], False
        return self.presets[default], selected is not None
