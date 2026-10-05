"""Change the pitch of a generated WAV without changing its speaking tempo."""

import argparse
import math
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Transpose a WAV with FFmpeg rubberband.")
    parser.add_argument("--input", required=True, type=Path, help="Source WAV")
    parser.add_argument("--output", required=True, type=Path, help="New WAV path")
    parser.add_argument("--semitones", type=float, default=-1.0, help="-1 = one semitone lower")
    parser.add_argument("--formant", choices=("preserved", "shifted"), default="preserved")
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"Audio does not exist: {args.input}")
    if args.input.resolve() == args.output.resolve():
        parser.error("--output must differ from --input")
    if not math.isfinite(args.semitones) or not -12 <= args.semitones <= 12:
        parser.error("--semitones must be between -12 and 12")

    factor = 2 ** (args.semitones / 12)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(args.input),
            "-af", f"rubberband=tempo=1:pitch={factor:.8f}:formant={args.formant}:pitchq=quality",
            "-c:a", "pcm_s16le", str(args.output),
        ],
        check=True,
    )
    print(f"Saved: {args.output} ({args.semitones:+g} semitones; formants {args.formant})")


if __name__ == "__main__":
    main()
