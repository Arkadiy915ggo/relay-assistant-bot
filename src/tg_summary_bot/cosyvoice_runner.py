"""Run with the dedicated Python 3.10 environment, never imported by the bot.

Only installation paths are argv. Speech and reference metadata arrive on stdin.
The final stdout line is a small JSON result; upstream output is not logged by the bot.
"""
import argparse
import contextlib
import json
import sys
from pathlib import Path


class RunnerError(RuntimeError):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 10):
        raise RunnerError("wrong_python_version")
    upstream = Path(args.upstream).resolve()
    model_dir = Path(args.model).resolve()
    sys.path[:0] = [str(upstream), str(upstream / "third_party" / "Matcha-TTS")]
    # CosyVoice can print inputs. Redirect upstream diagnostics away from result stdout.
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        import torchaudio
        from cosyvoice.cli.cosyvoice import AutoModel
        if not torch.cuda.is_available():
            raise RunnerError("cuda_unavailable")
        if args.check:
            result = {"ready": True}
        else:
            request = json.loads(sys.stdin.read(65536))
            model = AutoModel(model_dir=str(model_dir), fp16=True)
            chunks = []
            samples = 0
            limit = int(request["max_raw_seconds"] * model.sample_rate)
            for item in model.inference_zero_shot(
                request["text"],
                "You are a helpful assistant.<|endofprompt|>" + request["transcript"],
                request["reference"], stream=False, speed=1.0, text_frontend=False,
            ):
                chunk = item["tts_speech"].cpu()
                samples += chunk.shape[-1]
                if samples > limit:
                    raise RuntimeError("audio duration limit")
                chunks.append(chunk)
            if not chunks:
                raise RuntimeError("empty audio")
            torchaudio.save(request["output"], torch.cat(chunks, dim=1), model.sample_rate,
                            encoding="PCM_S", bits_per_sample=16)
            result = {"duration": samples / model.sample_rate, "sample_rate": model.sample_rate}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No input/provider exception text crosses the process boundary.
        code = str(exc) if isinstance(exc, RunnerError) else (
            "missing_dependencies" if isinstance(exc, ImportError) else "runner_incompatible"
        )
        print(json.dumps({"ready": False, "error": code}), flush=True)
        raise SystemExit(0 if "--check" in sys.argv else 1)
