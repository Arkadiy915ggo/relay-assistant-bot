"""Generate separate sentences with one cloned voice and controlled inter-sentence pauses."""

import argparse
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

from clone import MODEL_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and join separate cloned-voice sentences.")
    parser.add_argument("--ref-audio", required=True, type=Path)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument("--part", required=True, action="append", help="Repeat for each sentence")
    parser.add_argument("--pause-ms", type=int, default=250, help="Silence between sentences")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if not args.ref_audio.is_file():
        parser.error(f"Reference audio does not exist: {args.ref_audio}")
    if not args.ref_text.strip() or any(not part.strip() for part in args.part):
        parser.error("Reference transcript and all parts must be nonempty")
    if not 0 <= args.pause_ms <= 2000:
        parser.error("--pause-ms must be between 0 and 2000")
    if not MODEL_DIR.is_dir():
        parser.error(f"Model not found at {MODEL_DIR}; download it first (see README.md)")
    if not torch.cuda.is_available():
        parser.error("CUDA is unavailable")

    model = Qwen3TTSModel.from_pretrained(str(MODEL_DIR), device_map="cuda:0", dtype=torch.bfloat16)
    prompt = model.create_voice_clone_prompt(ref_audio=str(args.ref_audio), ref_text=args.ref_text)
    chunks = []
    sample_rate = None
    for index, part in enumerate(args.part, start=1):
        started = time.perf_counter()
        wavs, sr = model.generate_voice_clone(
            text=part,
            language="Russian",
            voice_clone_prompt=prompt,
            non_streaming_mode=True,
        )
        torch.cuda.synchronize()
        if sample_rate is not None:
            chunks.append(np.zeros(round(sr * args.pause_ms / 1000), dtype=np.float32))
        chunks.append(wavs[0])
        sample_rate = sr
        print(f"Part {index}/{len(args.part)}: {time.perf_counter() - started:.2f}s")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, np.concatenate(chunks), sample_rate)
    print(f"Saved: {args.output} ({sample_rate} Hz; {args.pause_ms} ms between sentences)")


if __name__ == "__main__":
    main()
