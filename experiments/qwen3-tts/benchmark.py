"""Run clone.py and sample system-wide GPU load with nvidia-smi."""

import subprocess
import sys
import time
from pathlib import Path

SAMPLE_COMMAND = [
    "nvidia-smi",
    "--id=0",
    "--query-gpu=memory.used,utilization.gpu,utilization.memory",
    "--format=csv,noheader,nounits",
]


def sample_gpu() -> tuple[int, int, int]:
    output = subprocess.check_output(SAMPLE_COMMAND, text=True).strip()
    used, gpu_util, memory_util = output.split(",")
    return int(used), int(gpu_util), int(memory_util)


def main() -> int:
    baseline = sample_gpu()
    print(f"Before run: GPU 0 used {baseline[0]} MiB, utilization {baseline[1]}%", flush=True)
    command = [sys.executable, str(Path(__file__).with_name("clone.py")), "--profile", *sys.argv[1:]]
    start = time.perf_counter()
    process = subprocess.Popen(command)
    samples = []
    try:
        while process.poll() is None:
            samples.append(sample_gpu())
            time.sleep(0.25)
        returncode = process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()

    if samples:
        peak_used = max(sample[0] for sample in samples)
        gpu_load = [sample[1] for sample in samples]
        memory_load = [sample[2] for sample in samples]
        print(f"Wall time: {time.perf_counter() - start:.2f}s; GPU samples: {len(samples)}")
        print(f"GPU 0 used peak: {peak_used} MiB (change vs baseline: {peak_used - baseline[0]:+} MiB)")
        print(f"GPU 0 utilization: avg {sum(gpu_load) / len(gpu_load):.0f}%, peak {max(gpu_load)}%")
        print(
            f"GPU 0 memory-controller utilization: "
            f"avg {sum(memory_load) / len(memory_load):.0f}%, peak {max(memory_load)}%"
        )
        print("GPU 0 statistics include all processes using the card.")
    return returncode


if __name__ == "__main__":
    sys.exit(main())
