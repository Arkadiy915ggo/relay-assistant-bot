"""Standalone voice cloning experiment with Qwen3-TTS Base."""

import argparse
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent / "models" / "Qwen3-TTS-12Hz-1.7B-Base"


def main() -> None:
    parser = argparse.ArgumentParser(description="Clone a reference voice into a new WAV.")
    parser.add_argument("--ref-audio", required=True, type=Path, help="Short, clean reference audio")
    parser.add_argument("--ref-text", required=True, help="Exact words spoken in the reference")
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument("--output", required=True, type=Path, help="Output WAV path")
    parser.add_argument("--language", default="Russian", help="Target language (default: Russian)")
    parser.add_argument("--model", default=str(MODEL_DIR), help="Local model directory or HF ID")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    args = parser.parse_args()

    if not args.ref_audio.is_file():
        parser.error(f"Reference audio does not exist: {args.ref_audio}")
    if not args.ref_text.strip() or not args.text.strip():
        parser.error("--ref-text and --text must not be empty")
    if args.model == str(MODEL_DIR) and not MODEL_DIR.is_dir():
        parser.error(f"Model not found at {MODEL_DIR}; download it first (see README.md)")

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA is unavailable; use --device cpu or install CUDA-enabled PyTorch")

    model = Qwen3TTSModel.from_pretrained(
        args.model,
        device_map="cuda:0" if args.device == "cuda" else "cpu",
        dtype=torch.bfloat16 if args.device == "cuda" else torch.float32,
    )
    wavs, sample_rate = model.generate_voice_clone(
        text=args.text,
        language=args.language,
        ref_audio=str(args.ref_audio),
        ref_text=args.ref_text,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, wavs[0], sample_rate)
    print(f"Saved: {args.output} ({sample_rate} Hz)")


if __name__ == "__main__":
    main()
