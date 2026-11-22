"""Compare same-state native Root claim calls from two exact Git exports."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent
PINS = {"v21": "61ed0067448658e02d181b3cb85d7f68e39b6616", "p1": "336225603dec3a1bc5efd6eccb401ccd25e63287"}
SPEC_BLOB = "acceptance/v3/performance/gate0/project-specifications.jsonl.gz"
CASES = ("eligible", "denied", "resumed")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--v21", type=Path, required=True)
    p.add_argument("--p1", type=Path, required=True)
    p.add_argument("--out", type=Path, default=HERE)
    p.add_argument("--blocks", type=int, default=8)
    p.add_argument("--samples-per-block", type=int, default=16)
    p.add_argument("--seed", type=int, default=6140)
    args = p.parse_args()
    assert args.blocks >= 2 and args.samples_per_block > 0
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    repos = {"v21": args.v21.resolve(), "p1": args.p1.resolve()}
    blob = git(repos["p1"], "show", f"{PINS['p1']}:{SPEC_BLOB}")
    spec_name = "00-east.json"
    spec = next(record for record in map(json.loads, gzip.decompress(blob).splitlines()) if record["name"] == spec_name)
    spec_bytes = json.dumps(spec["specification"], indent=2, sort_keys=True).encode() + b"\n"
    allowed = sorted(os.sched_getaffinity(0))
    cpu = allowed[len(allowed) // 2]
    info = {"started_utc": stamp(), "command": [sys.executable, *sys.argv], "cwd": str(Path.cwd()),
            "python": sys.version, "python_sha256": sha(Path(sys.executable).read_bytes()), "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "cpu_model": next(line.split(":",1)[1].strip() for line in
                Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")),
            "available_cpu_ids": allowed, "measurement_cpu": cpu, "journal_mode": "wal", "synchronous": "FULL",
            "wall_clock": vars(time.get_clock_info("perf_counter")), "cpu_clock": vars(time.get_clock_info("process_time")),
            "blocks": args.blocks, "samples_per_case_per_block": args.samples_per_block, "warmups_per_case": 3,
            "seed": args.seed, "pins": PINS, "fixture_from_p1_commit": SPEC_BLOB,
            "common_spec": {k: v for k,v in spec.items() if k != "specification"}, "specification": spec["specification"],
            "fixture_gzip_sha256": sha(blob), "harness_sha256": {f: sha((HERE/f).read_bytes()) for f in ("run.py","sample.py")},
            "exports": {}, "workers": {}, "block_order": []}
    raw = []
    rng = random.Random(args.seed)
    with tempfile.TemporaryDirectory(prefix="gx-claim-comparison-", dir="/tmp") as temporary, ExitStack() as stack:
        temporary = Path(temporary)
        info["storage"] = subprocess.check_output(["stat", "-f", "-c", "%T", str(temporary)], text=True).strip()
        spec_file = temporary / "spec.json"
        spec_file.write_bytes(spec_bytes)
        workers = {}
        for arm, pin in PINS.items():
            work = temporary / arm
            stage = work / "release"
            stage.mkdir(parents=True)
            (work / "tmp").mkdir()
            repo = repos[arm]
            assert git(repo, "rev-parse", pin + "^{commit}").decode().strip() == pin
            paths = ["bin", "gx-h", "examples"]
            archive = git(repo, "archive", "--format=tar", pin, *paths)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                tar.extractall(stage, filter="data")
            info["exports"][arm] = {"repository": str(repo), "git_archive_paths": paths, "git_tar_sha256": sha(archive),
                                    "tracked_gx_h_tree": git(repo, "rev-parse", pin + ":gx-h").decode().strip()}
            env = {**os.environ, "GX_SCOPE": "operator", "GX_PROJECTS": "*", "GX_P0_CODEX": str(work/"no-transport"),
                   "TMPDIR": str(work/"tmp"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                   "PYTHONPATH": str(stage/"gx-h")}
            command = [sys.executable, "-B", str(HERE/"sample.py"), str(stage), str(work/"state"),
                       str(spec_file), arm, str(cpu)]
            child = subprocess.Popen(command, cwd=stage, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)
            stack.callback(child.wait)
            stack.callback(child.stdin.close)
            stack.callback(child.stdout.close)
            workers[arm] = child
            first = child.stdout.readline()
            if not first:
                raise RuntimeError(f"{arm} failed to prepare its scientific fixture (status {child.poll()})")
            info["workers"][arm] = json.loads(first)
        a, b = [info["workers"][arm]["state"] for arm in PINS]
        for field in ("scientific_projection","scientific_sha256","workflow","role","output","initial_status","initial_attempt",
                      "jobs","counts"):
            assert a[field] == b[field], (field, a[field], b[field])
        assert info["workers"]["v21"]["database_state"] == info["workers"]["p1"]["database_state"]
        def run_block(arm, index, cases):
            worker = workers[arm]
            worker.stdin.write(json.dumps(cases) + "\n")
            worker.stdin.flush()
            rows = json.loads(worker.stdout.readline())
            assert [r["case"] for r in rows] == cases
            raw.extend({**row, "arm": arm, "block": index, "sequence": i} for i,row in enumerate(rows))
        for arm in PINS:
            run_block(arm, -1, list(CASES)*3)
        for index in range(args.blocks):
            cases = list(CASES) * args.samples_per_block
            rng.shuffle(cases)
            order = list(PINS) if index % 2 == 0 else list(reversed(PINS))
            info["block_order"].append(order)
            for arm in order:
                run_block(arm, index, cases)

    info["finished_utc"] = stamp()
    save(out / "environment.json", info)
    (out/"raw.jsonl").write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in raw))
    summary = {}
    for case in CASES:
        summary[case] = {}
        for arm in PINS:
            group = [r for r in raw if r["block"] >= 0 and r["arm"] == arm and r["case"] == case]
            summary[case][arm] = {"n": len(group)}
            for sample, label in (("wall_ns","wall_ms"), ("process_cpu_ns","process_cpu_ms")):
                vals = sorted(r[sample]/1_000_000 for r in group)
                summary[case][arm][label] = {"p50": statistics.median(vals), "p95": vals[math.ceil(len(vals)*0.95)-1],
                                              "max": vals[-1]}
        summary[case]["p95_wall_delta_ms"] = summary[case]["p1"]["wall_ms"]["p95"] - summary[case]["v21"]["wall_ms"]["p95"]
    save(out / "summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
