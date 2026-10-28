"""Validate a human-selected reference and optionally synthesize a labeled test."""
import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path(__file__).with_name("segments.json"))
    p.add_argument("--id")
    p.add_argument("--audit-candidates", action="store_true", help="Check all review WAVs through the installed Qwen audio loader, without loading model weights")
    p.add_argument("--text", default="Это синтетический тест голоса. Сегодня мы обсуждаем новый план.")
    p.add_argument("--check-only", action="store_true")
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    out = (args.config.resolve().parent / config["output"]).resolve()
    if args.audit_candidates:
        import numpy as np
        from qwen_tts import Qwen3TTSModel
        # Only exercise the stateless file-loading helper. No model initialization or GPU inference.
        loader = Qwen3TTSModel.__new__(Qwen3TTSModel)
        results = []
        for audio in sorted((out / "review_audio").glob("*.wav")):
            with wave.open(str(audio)) as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                    p.error(f"Unexpected reference format: {audio}")
            x, rate = loader._load_audio_to_np(str(audio))
            if rate != 24000 or x.ndim != 1 or not len(x) or not np.isfinite(x).all() or np.max(np.abs(x)) >= 1 - 1/32768:
                p.error(f"Invalid or full-scale candidate: {audio}")
            text = audio.with_suffix(".txt").read_text(encoding="utf-8").strip()
            if not text:
                p.error(f"Empty transcript draft: {audio}")
            results.append({"file": str(audio.relative_to(out)), "input_loader_accepted": True,
                            "sample_rate": rate, "duration": len(x)/rate, "transcript_verified": False})
        if not results:
            p.error("No review WAVs found; run pipeline.py review first")
        dest = out / "qwen_input_validation.json"
        dest.write_text(json.dumps({"scope": "Actual installed Qwen file loader + format only; no prompt encoding or synthesis",
                                   "candidates": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Installed Qwen input loader accepted all {len(results)} mono PCM16 24 kHz candidates")
        return
    if not args.id:
        p.error("--id is required unless --audit-candidates is used")
    segment = next(s for s in config["segments"] if s["id"] == args.id)
    if not segment.get("selected_method") or not segment.get("transcript_verified"):
        p.error("Select a method and verify the transcript before TTS validation")
    audio = out / "references" / f"{args.id}.wav"
    text = audio.with_suffix(".txt").read_text(encoding="utf-8").strip()
    if text != segment["transcript"].strip():
        p.error("Exported transcript differs from the current configuration; review the reference pair")
    with wave.open(str(audio)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != config["reference_sample_rate"]:
            p.error("Expected mono PCM16 at configured sample rate")
        print(f"Reference format verified: {wav.getframerate()} Hz, {wav.getnframes()/wav.getframerate():.3f}s")
    if args.check_only:
        return
    result = out / "tts_validation" / f"{args.id}_synthetic_qwen.wav"
    if result.exists():
        p.error("Preserving existing TTS output; use a new output directory")
    wrapper = Path(__file__).resolve().parent.parent / "qwen3-tts" / "clone.py"
    subprocess.run([sys.executable, str(wrapper), "--ref-audio", str(audio), "--ref-text", text,
                    "--text", args.text, "--language", "Russian", "--output", str(result)], check=True)
    result.with_suffix(".txt").write_text("Синтетический тест, не подлинная запись актёра.\n" + args.text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
