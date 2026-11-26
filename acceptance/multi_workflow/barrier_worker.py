"""Test-only detached CPU workload for a two-party barrier and coordinator restart."""
import argparse
import json
import os
from pathlib import Path
import time


def await_file(path, deadline):
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Worker did not observe {path.name}")
        time.sleep(.01)


def measure(sample):
    directory = Path(sample["barrier"])
    side = sample["side"]
    if side not in ("left", "right"):
        raise ValueError("Unknown worker")
    start = time.monotonic_ns()
    result = {"side": side, "pid": os.getpid(), "entered_ns": start,
              "affinity": sorted(os.sched_getaffinity(0))}
    (directory / (side + ".ready")).write_text(str(os.getpid()))
    deadline = time.monotonic() + 20
    for label in ("left", "right"):
        await_file(directory / (label + ".ready"), deadline)
    result["compute_start_ns"] = time.monotonic_ns()
    cpu_start = time.process_time_ns()
    total = 0
    while time.process_time_ns() - cpu_start < 350_000_000:
        total += sum(i * i for i in range(1000))
    result.update(cpu_ns=time.process_time_ns() - cpu_start, compute_end_ns=time.monotonic_ns(), checksum=total)
    if side == "right":
        await_file(directory / "coordinator-resumed", deadline)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    (args.out / "sample.json").write_text(json.dumps(measure(json.loads(args.sample.read_bytes()))))
