"""Local, non-destructive dialogue extraction. Paths are relative to the config."""
import argparse
import html
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seconds(value):
    if isinstance(value, (float, int)):
        return float(value)
    parts = value.split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def probe(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]))


def ffmpeg(source, dest, *options):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"Preserving existing file: {dest}; use a new output directory")
    run("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i", source, *options, dest)


def inspect(config, source, out):
    info = probe(source)
    save_json(out / "source_probe.json", info)
    lines = ["# Source analysis", "", f"Source: `{source}`", "",
             "Metadata labels are not proof of language or dubbing studio.", "",
             "| Global stream | Codec | Rate | Channels/layout | Bitrate | Language tag |",
             "|---|---|---|---|---|---|"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    for s in audio:
        lines.append(f"| {s['index']} | {s['codec_name']} | {s.get('sample_rate')} | "
                     f"{s['channels']}/{s.get('channel_layout', 'unknown')} | {s.get('bit_rate', 'unknown')} | "
                     f"{s.get('tags', {}).get('language', 'absent')} |")
        # Matroska supports stream-copy of common audio codecs without transcoding.
        ffmpeg(source, out / "streams" / f"stream_{s['index']}.mka", "-map", f"0:{s['index']}", "-c:a", "copy")
        ffmpeg(source, out / "streams" / f"stream_{s['index']}.wav", "-map", f"0:{s['index']}", "-c:a", "pcm_f32le")
    lines += ["", f"Duration: {info['format']['duration']} seconds.", "",
              "Original compressed packets are retained in MKA; float WAVs preserve decoded precision and native rate/layout.",
              "A lossy source cannot be made lossless retroactively."]
    if len(audio) == 1:
        lines += ["", "## Method B feasibility", "Only one audio track exists. No independent English reference is available.",
                  "English/Russian soundtrack cancellation and its hybrid are unavailable for this source.",
                  "Stereo L/R or mid/side extraction is a channel diagnostic, not independent-track subtraction."]
    if all(s["channels"] < 3 for s in audio):
        lines += ["", "No discrete multichannel center/dialogue channel exists. Inspect L/R and mid/side previews by listening."]
    lines += ["", "## Language verification", "See ASR JSON for automatically detected language and draft words.",
              "Listening must confirm Russian, Cartman-only speech, and Paramount Comedy provenance; ASR does not establish speaker/studio identity."]
    (out / "source_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def extract(config, source, out):
    info = probe(source)
    stream = next(s for s in info["streams"] if s["index"] == config["audio_stream"])
    if stream["codec_type"] != "audio":
        raise ValueError("Configured stream is not audio")
    rate = int(stream["sample_rate"])
    timeline = []
    for s in config["segments"]:
        start, end = seconds(s["start"]), seconds(s["end"])
        if not 0 <= start < end <= float(info["format"]["duration"]):
            raise ValueError(f"Invalid interval: {s['id']}")
        lo = max(0, start - config.get("margin_before", 0.2))
        hi = min(float(info["format"]["duration"]), end + config.get("margin_after", 0.2))
        a, b = round(lo * rate), round(hi * rate)
        # Decode before trimming: sample-accurate cuts, not AAC packet/keyframe seeking.
        ffmpeg(source, out / "raw" / f"{s['id']}.wav", "-map", f"0:{stream['index']}",
               "-af", f"atrim=start_sample={a}:end_sample={b},asetpts=PTS-STARTPTS", "-c:a", "pcm_f32le")
        timeline.append({"id": s["id"], "start_sample": a, "end_sample": b, "sample_rate": rate,
                         "source_start": a / rate, "source_end": b / rate,
                         "core_start_relative": start - a / rate, "core_end_relative": end - a / rate})
    save_json(out / "timing.json", timeline)
    save_json(out / "segments.json", {**config, "source": os.path.relpath(source, out), "output": "."})


def channels(config, out):
    import numpy as np
    import soundfile as sf
    metrics = {}
    for s in config["segments"]:
        source = out / "raw" / f"{s['id']}.wav"
        x, rate = sf.read(source, always_2d=True)
        if x.shape[1] != 2:
            continue
        variants = {"left": x[:, 0], "right": x[:, 1], "mid": x.mean(axis=1), "side": (x[:, 0] - x[:, 1]) / 2}
        metrics[s["id"]] = {"lr_correlation": float(np.corrcoef(x.T)[0, 1]),
                              "rms": {k: float(np.sqrt(np.mean(v*v))) for k, v in variants.items()}}
        for name, wave in variants.items():
            dest = out / "channels" / f"{s['id']}_{name}.wav"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise FileExistsError(dest)
            sf.write(dest, wave, rate, subtype="FLOAT")
    save_json(out / "channel_analysis.json", metrics)


def ai(config, out, args):
    # Demucs CLI normalizes internally and reverses it; avoid output rescaling/clamping by saving float.
    for s in config["segments"]:
        dest = out / "separated" / f"{s['id']}_ai_{args.model}.wav"
        if dest.exists():
            raise FileExistsError(dest)
        work = out / "demucs" / args.model
        if (work / args.model / s["id"]).exists():
            raise FileExistsError(f"Preserving existing Demucs intermediate: {work / args.model / s['id']}")
        run(sys.executable, "-m", "demucs", "-n", args.model, "--two-stems", "vocals", "--float32",
            "--clip-mode", "none", "--shifts", args.shifts, "--overlap", "0.5", "-d", args.device,
            "-o", work, out / "raw" / f"{s['id']}.wav")
        vocals = work / args.model / s["id"] / "vocals.wav"
        ffmpeg(vocals, dest, "-c:a", "pcm_f32le")
    save_json(out / f"method_ai_{args.model}.json", {"model": args.model, "shifts": args.shifts,
              "overlap": 0.5, "device": args.device, "warning": "All voices may remain; separation does not identify Cartman."})


def transcribe(config, out, args):
    from faster_whisper import WhisperModel
    model = WhisperModel(args.asr_model, device=args.device, compute_type="int8" if args.device == "cpu" else "float16",
                         local_files_only=True)
    for s in config["segments"]:
        path = candidate(out, s["id"], args.method)
        dest = out / "transcripts" / f"{s['id']}_{args.method}.json"
        if dest.exists():
            raise FileExistsError(dest)
        segments, info = model.transcribe(str(path), language=None if args.detect_language else "ru", beam_size=5,
                                          word_timestamps=True, vad_filter=False, condition_on_previous_text=False)
        entries = []
        for t in segments:
            entries.append({"start": t.start, "end": t.end, "text": t.text, "avg_logprob": t.avg_logprob,
                            "no_speech_prob": t.no_speech_prob,
                            "words": [{"start": w.start, "end": w.end, "word": w.word, "probability": w.probability} for w in t.words or []]})
        text = " ".join(e["text"].strip() for e in entries)
        save_json(dest, {"source": str(path), "model": args.asr_model, "language": info.language,
                        "language_probability": info.language_probability, "human_verified": False,
                        "draft": text, "segments": entries})
        txt = dest.with_suffix(".txt")
        if txt.exists():
            raise FileExistsError(txt)
        txt.write_text(text + "\n", encoding="utf-8")
        print(s["id"], info.language, text)


def candidate(out, sid, method):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", method):
        raise ValueError("Invalid method")
    return out / "raw" / f"{sid}.wav" if method == "raw" else out / "separated" / f"{sid}_{method}.wav"


def export(config, out):
    import numpy as np
    import soundfile as sf
    timings = {t["id"]: t for t in json.loads((out / "timing.json").read_text())}
    for s in config["segments"]:
        if not s.get("selected_method") or not s.get("transcript_verified") or not s.get("transcript"):
            print(f"Skipping {s['id']}: select a method and verify the exact transcript in segments.json")
            continue
        source = candidate(out, s["id"], s["selected_method"])
        start = s.get("reference_start", 0)
        end = s.get("reference_end", timings[s["id"]]["source_end"] - timings[s["id"]]["source_start"])
        x, rate = sf.read(source, always_2d=True)
        if not 0 <= start < end <= len(x) / rate + 1 / rate:
            raise ValueError("Reference trim is outside candidate duration")
        mono = x[round(start * rate):round(end * rate)].mean(axis=1)
        if not len(mono) or not np.isfinite(mono).all():
            raise ValueError("Empty or non-finite audio")
        peak = float(np.max(np.abs(mono)))
        gain = min(1, 10 ** (-1 / 20) / max(peak, 1e-12))
        # Attenuate only if needed; never raise quiet audio or compress its dynamics.
        wav = out / "references" / f"{s['id']}.wav"
        txt = wav.with_suffix(".txt")
        if txt.exists():
            raise FileExistsError(txt)
        ffmpeg(source, wav, "-af", f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS,pan=mono|c0=0.5*c0+0.5*c1,volume={gain},aresample={config['reference_sample_rate']}:resampler=soxr:precision=28" if x.shape[1] == 2 else
               f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS,volume={gain},aresample={config['reference_sample_rate']}:resampler=soxr:precision=28",
               "-ac", "1", "-c:a", "pcm_s16le")
        txt.write_text(s["transcript"].strip() + "\n", encoding="utf-8")
        final, final_rate = sf.read(wav)
        if np.max(np.abs(final)) >= 1 - 1 / 32768:
            raise ValueError(f"Export reached full scale after resampling: {wav}; reduce gain before use")
        save_json(wav.with_suffix(".json"), {"source": str(source), "trim_start": start, "trim_end": end,
                  "gain": gain, "transcript_verified": True, "purpose": "Personal synthetic-voice experiment"})


def review(config, out):
    """Playable PCM candidates, unverified texts, and a local A/B comparison page."""
    sections = ["<!doctype html><meta charset='utf-8'><title>Cartman listening comparison</title>",
                "<style>body{max-width:1000px;margin:2em auto;font:18px sans-serif}section{border-top:1px solid #aaa}audio{width:100%}pre{white-space:pre-wrap}</style>",
                "<h1>Cartman listening comparison</h1><p>Drafts only. Verify one speaker, wording, boundaries and voice identity. "
                "Compare at matched playback loudness. Select a method in segments.json after listening.</p>"]
    notes = ["# Listening review", "", "All scores and transcripts await human verification.", ""]
    for s in config["segments"]:
        sections.append(f"<section><h2>{s['id']}: {html.escape(s['start'])}–{html.escape(s['end'])}</h2>")
        notes += [f"## {s['id']} ({s['start']}–{s['end']})", "",
                  "| Method | Intelligibility | Music | English | Effects | Artifacts | Identity | Cloning |",
                  "|---|---|---|---|---|---|---|---|"]
        for method in ("raw", "ai_htdemucs", "ai_mdx_extra"):
            source = candidate(out, s["id"], method)
            if not source.exists():
                continue
            wav = out / "review_audio" / f"{s['id']}_{method}.wav"
            ffmpeg(source, wav, "-ac", "1", "-af", "aresample=24000:resampler=soxr:precision=28", "-c:a", "pcm_s16le")
            transcript_file = out / "transcripts" / f"{s['id']}_{method}.txt"
            text = transcript_file.read_text(encoding="utf-8") if transcript_file.exists() else "No ASR draft yet"
            wav.with_suffix(".txt").write_text(text, encoding="utf-8")
            save_json(wav.with_suffix(".json"), {"source": str(source), "transcript_verified": False, "selected": False})
            sections += [f"<h3>{method} — NOT FINAL</h3><audio controls preload='none' src='{wav.relative_to(out)}'></audio>",
                         f"<p>Unverified ASR draft:</p><pre lang='ru'>{html.escape(text)}</pre>"]
            notes.append(f"| {method} | pending | pending | pending | pending | pending | pending | pending |")
        sections.append("</section>")
        notes += ["", "Chosen method: pending", "Exact transcript: pending", "Trim boundaries: pending", ""]
    (out / "review.html").write_text("\n".join(sections), encoding="utf-8")
    if not (out / "listening_review.md").exists():
        (out / "listening_review.md").write_text("\n".join(notes), encoding="utf-8")


def report(config, out):
    import numpy as np
    import soundfile as sf
    rows, metrics = [], []
    for folder in ("raw", "separated", "review_audio", "references", "tts_comparison"):
        for path in sorted((out / folder).glob("*.wav")):
            x, rate = sf.read(path, always_2d=True)
            peak = float(np.max(np.abs(x)))
            rms = float(np.sqrt(np.mean(x*x)))
            m = {"file": str(path.relative_to(out)), "duration": len(x)/rate, "rate": rate,
                 "channels": x.shape[1], "subtype": sf.info(path).subtype, "peak": peak,
                 "peak_dbfs": 20*math.log10(max(peak, 1e-12)), "rms_dbfs": 20*math.log10(max(rms, 1e-12)),
                 "samples_at_or_above_full_scale": int(np.sum(np.abs(x) >= 1))}
            metrics.append(m)
            rows.append(f"| {m['file']} | {m['duration']:.3f} | {rate} | {m['channels']} | {m['subtype']} | {m['peak_dbfs']:.2f} | {m['samples_at_or_above_full_scale']} |")
    save_json(out / "quality_metrics.json", metrics)
    lines = ["# Quality report", "", "## Measured signal properties", "",
             "| File | Seconds | Hz | Channels | Encoding | Peak dBFS | Full-scale samples |", "|---|---|---|---|---|---|---|"] + rows
    lines += ["", "These metrics do not measure intelligibility, identity, or background removal. Full-scale counts flag risks, not proof of original clipping.",
              "", "## Method comparison", "- Raw: baseline for voice identity; no separation artifacts introduced.",
              "- AI htdemucs: music/vocals model; may retain English speech and effects and damage consonants/timbre.",
              "- AI mdx_extra: independent pretrained Demucs architecture/checkpoints for listening comparison, when run.",
              "- Soundtrack subtraction: unavailable here (only one track); no fabricated subtraction/hybrid outputs.",
              "- Channel previews: diagnostic only. Side often removes centered dialogue along with background.",
              "", "## Listening evaluation (required per candidate)",
              "Score 1–5: Russian intelligibility; music; English leakage; sound effects; artifacts; voice identity; cloning suitability.",
              "For contamination/artifacts, 5 means least contamination. For identity/intelligibility, 5 means best preservation.",
              "All perceptual scores are pending user listening. Do not select by RMS, ASR confidence, or loudness alone.",
              "Compare at matched playback loudness without altering the stored WAVs. Check first/last words and nearby speakers.",
              "", "## Transcript status", "ASR text is a draft, not an exact/human-verified transcript. Edit segments.json after listening.",
              "The 11-second second interval should be trimmed at a natural pause if a shorter single-speaker reference is possible.",
              "", "## Qwen3-TTS compatibility", "Installed experiments/qwen3-tts/clone.py uses Qwen3TTSModel.generate_voice_clone(ref_audio, ref_text).",
              "Export produces mono PCM16 24 kHz WAV + UTF-8 transcript; no pitch change, compression, or upward normalization.",
              "Final references and verified-reference TTS validation wait for explicit method selection and transcript verification. "
              "User-requested trials from a draft transcript can be generated separately with compare_tts.py and remain marked unverified.",
              "Optional generated outputs are synthetic experiments, not authentic new actor recordings."]
    comparison = []
    for s in config["segments"]:
        raw_file = candidate(out, s["id"], "raw")
        if not raw_file.exists():
            continue
        raw, raw_rate = sf.read(raw_file, always_2d=True)
        for method in ("ai_htdemucs", "ai_mdx_extra"):
            path = candidate(out, s["id"], method)
            if not path.exists():
                continue
            processed, rate = sf.read(path, always_2d=True)
            if rate != raw_rate or processed.shape != raw.shape:
                comparison.append({"id": s["id"], "method": method, "aligned_shape": False})
                continue
            baseline, voice = raw.mean(axis=1), processed.mean(axis=1)
            raw_rms = max(float(np.sqrt(np.mean(baseline**2))), 1e-12)
            delta = float(np.sqrt(np.mean((baseline-voice)**2))) / raw_rms
            corr = float(np.corrcoef(baseline, voice)[0, 1])
            comparison.append({"id": s["id"], "method": method, "aligned_shape": True,
                               "correlation_with_raw": corr, "relative_difference_rms": delta})
    save_json(out / "method_comparison.json", comparison)
    lines += ["", "## Measured method differences", "",
              "| Segment | Method | Correlation with raw | Difference RMS / raw RMS |",
              "|---|---|---|---|"]
    for item in comparison:
        if item["aligned_shape"]:
            lines.append(f"| {item['id']} | {item['method']} | {item['correlation_with_raw']:.6f} | {item['relative_difference_rms']:.4f} |")
    lines += ["", "Difference energy includes removed backgrounds AND altered speech. It is not a separation-quality score.",
              "", "## Actual input compatibility check"]
    validation = out / "qwen_input_validation.json"
    if validation.exists():
        checked = json.loads(validation.read_text(encoding="utf-8"))
        lines.append(f"Installed Qwen audio loader accepted {len(checked['candidates'])} mono PCM16 24 kHz audition candidates. "
                     "This verifies input loading/format only; prompt encoding, synthesis, transcript accuracy and similarity are not validated.")
    else:
        lines.append("Input-loader check not run yet; use validate_tts.py --audit-candidates.")
    (out / "quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    analysis = out / "source_analysis.md"
    if analysis.exists():
        text = analysis.read_text(encoding="utf-8").split("\n## Measured segment verification")[0]
        measured = ["", "## Measured segment verification", ""]
        channel_file = out / "channel_analysis.json"
        if channel_file.exists():
            for sid, value in json.loads(channel_file.read_text()).items():
                measured.append(f"- {sid}: L/R correlation {value['lr_correlation']:.9f}; "
                                f"side RMS {value['rms']['side']:.6f}, mid RMS {value['rms']['mid']:.6f}. "
                                "Channels are effectively dual mono; no useful isolated-dialogue channel demonstrated.")
        for path in sorted((out / "transcripts").glob("*_raw.json")):
            value = json.loads(path.read_text())
            measured.append(f"- {path.stem}: ASR language {value['language']}, probability {value['language_probability']:.4f}; "
                            "automatic evidence only, speaker/studio not verified.")
        analysis.write_text(text + "\n".join(measured) + "\n", encoding="utf-8")


def main():
    deps = Path(__file__).with_name(".deps")
    if deps.is_dir():
        sys.path.insert(0, str(deps))
        os.environ["PYTHONPATH"] = str(deps) + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.environ.setdefault("TORCH_HOME", str(Path(__file__).with_name("models") / "torch"))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("inspect", "extract", "channels", "ai", "transcribe", "review", "export", "report"))
    p.add_argument("--config", type=Path, default=Path(__file__).with_name("segments.json"))
    p.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    p.add_argument("--model", choices=("htdemucs", "mdx_extra"), default="htdemucs")
    p.add_argument("--shifts", type=int, default=4)
    p.add_argument("--asr-model", default="large-v3", help="Local faster-whisper path or already-cached model; downloads disabled")
    p.add_argument("--method", default="raw")
    p.add_argument("--detect-language", action="store_true")
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for s in config["segments"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", s["id"]):
            p.error("Segment IDs must contain only ASCII letters, digits, underscores and hyphens")
    if len({s["id"] for s in config["segments"]}) != len(config["segments"]):
        p.error("Segment IDs must be unique")
    base = args.config.resolve().parent
    source, out = (base / config["source"]).resolve(), (base / config["output"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.command == "inspect": inspect(config, source, out)
    elif args.command == "extract": extract(config, source, out)
    elif args.command == "channels": channels(config, out)
    elif args.command == "ai": ai(config, out, args)
    elif args.command == "transcribe": transcribe(config, out, args)
    elif args.command == "review": review(config, out)
    elif args.command == "export": export(config, out)
    elif args.command == "report": report(config, out)


if __name__ == "__main__":
    main()
