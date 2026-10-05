"""Standalone CosyVoice 3 voice cloning experiment; run from the repository root."""

import argparse
import sys
import time
from pathlib import Path

EXPERIMENT = Path(__file__).resolve().parent
MODEL_DIR = EXPERIMENT / "models" / "Fun-CosyVoice3-0.5B-2512"
UPSTREAM = EXPERIMENT / "upstream"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Russian speech with CosyVoice 3.")
    parser.add_argument("--ref-audio", required=True, type=Path)
    parser.add_argument("--ref-text", default="", help="Reference transcript (required for zero-shot)")
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--speed", type=float, default=1.0, help="Model speech speed (default 1.0)")
    parser.add_argument("--instruct", help="Optional style instruction; enables instruct2 mode")
    args = parser.parse_args()

    if not args.ref_audio.is_file():
        parser.error(f"Reference audio does not exist: {args.ref_audio}")
    if not args.text.strip() or (not args.instruct and not args.ref_text.strip()):
        parser.error("--text and, in zero-shot mode, --ref-text must not be empty")
    if args.instruct is not None and not args.instruct.strip():
        parser.error("--instruct must not be empty")
    if not 0.5 <= args.speed <= 2.0:
        parser.error("--speed must be between 0.5 and 2.0")
    if not (MODEL_DIR / "cosyvoice3.yaml").is_file() or not UPSTREAM.is_dir():
        parser.error("CosyVoice code/model not found; see experiments/cosyvoice3/README.md")

    sys.path[:0] = [str(UPSTREAM), str(UPSTREAM / "third_party" / "Matcha-TTS")]
    import torch
    import torchaudio
    from cosyvoice.cli.cosyvoice import AutoModel

    if not torch.cuda.is_available():
        parser.error("CUDA is unavailable")
    start = time.perf_counter()
    model = AutoModel(model_dir=str(MODEL_DIR), fp16=True)
    torch.cuda.synchronize()
    loaded = time.perf_counter()

    if args.instruct:
        audio = model.inference_instruct2(
            args.text,
            f"You are a helpful assistant. {args.instruct}<|endofprompt|>",
            str(args.ref_audio),
            stream=False,
            speed=args.speed,
            text_frontend=False,
        )
    else:
        audio = model.inference_zero_shot(
            args.text,
            f"You are a helpful assistant.<|endofprompt|>{args.ref_text}",
            str(args.ref_audio),
            stream=False,
            speed=args.speed,
            text_frontend=False,
        )

    chunks = [item["tts_speech"].cpu() for item in audio]
    if not chunks:
        raise RuntimeError("Model produced no audio")
    torch.cuda.synchronize()
    generated = time.perf_counter()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(args.output), torch.cat(chunks, dim=1), model.sample_rate)
    print(f"Saved: {args.output} ({model.sample_rate} Hz)")
    print(f"Model load: {loaded - start:.2f}s; synthesis: {generated - loaded:.2f}s")
    print(f"PyTorch peak reserved: {torch.cuda.max_memory_reserved() / 1024**2:.0f} MiB")


if __name__ == "__main__":
    main()
