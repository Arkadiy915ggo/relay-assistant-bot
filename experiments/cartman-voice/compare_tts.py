"""Generate sequential, unprocessed CosyVoice/Qwen trials from one reference pair."""
import argparse
import html
import json
import os
import subprocess
import time
from pathlib import Path


def main():
    base = Path(__file__).resolve().parent
    repo = base.parents[1]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ref-audio", type=Path, default=base / "output/separated/cartman_002_ai_mdx_extra.wav")
    p.add_argument("--ref-text-file", type=Path, default=base / "output/transcripts/cartman_002_ai_mdx_extra.txt")
    p.add_argument("--output", type=Path, default=base / "output/tts_comparison")
    p.add_argument("--text", default="Привет, я чат-бот Алёша. У меня есть отличный план. Сначала закажем пиццу, а потом решим, кто за неё платит.")
    p.add_argument("--models", nargs="+", choices=("cosyvoice", "qwen"), default=["cosyvoice", "qwen"])
    args = p.parse_args()
    transcript = args.ref_text_file.read_text(encoding="utf-8").strip()
    if not args.ref_audio.is_file() or not transcript or not args.text.strip():
        p.error("Existing reference audio and nonempty reference/test text required")
    args.output.mkdir(parents=True, exist_ok=True)
    jobs = {
        "cosyvoice": (repo / ".venv-cosyvoice3/bin/python", base.parent / "cosyvoice3/clone.py"),
        "qwen": (repo / ".venv-tts/bin/python", base.parent / "qwen3-tts/clone.py"),
    }
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    results = []
    for name in args.models:
        python, wrapper = jobs[name]
        audio = args.output / f"cartman_002_{name}_synthetic_raw.wav"
        log = audio.with_suffix(".log")
        metadata = audio.with_suffix(".json")
        if any(path.exists() for path in (audio, log, metadata)):
            p.error(f"Preserving existing {name} output; choose a new --output directory")
        command = [str(python), str(wrapper), "--ref-audio", str(args.ref_audio.resolve()),
                   "--ref-text", transcript, "--text", args.text, "--speed", "1.0", "--output", str(audio.resolve())]
        if name == "qwen":
            command += ["--language", "Russian"]
        started = time.monotonic()
        # Stream logs to the caller, while keeping an exact copy for this trial.
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", env=env, cwd=repo)
            try:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    stream.write(line)
                    stream.flush()
                code = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        entry = {"model": name, "reference_audio": str(args.ref_audio.resolve()), "reference_text": transcript,
                 "reference_transcript_verified": False, "test_text": args.text, "speed": 1.0,
                 "postprocessing": None, "synthetic": True, "output": audio.name,
                 "elapsed_seconds": round(time.monotonic()-started, 3), "exit_code": code}
        metadata.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if code:
            raise subprocess.CalledProcessError(code, command)
        audio.with_suffix(".txt").write_text("Синтетический эксперимент, не подлинная запись актёра.\n" + args.text + "\n", encoding="utf-8")
        results.append(entry)
    sections = ["<!doctype html><meta charset='utf-8'><title>Cartman synthetic TTS comparison</title>",
                "<style>body{max-width:900px;margin:2em auto;font:18px sans-serif}audio{width:100%}</style>",
                "<h1>Synthetic TTS comparison</h1><p>Personal synthetic-voice experiment; not authentic actor recordings. "
                "Same reference and test text; default speed, no output post-processing. Reference transcript is an unverified ASR draft.</p>",
                f"<p lang='ru'>{html.escape(args.text)}</p>"]
    for entry in results:
        sections.append(f"<h2>{entry['model']}</h2><audio controls preload='none' src='{entry['output']}'></audio>")
    (args.output / "comparison.html").write_text("\n".join(sections), encoding="utf-8")
    print(f"Comparison page: {args.output / 'comparison.html'}")


if __name__ == "__main__":
    main()
