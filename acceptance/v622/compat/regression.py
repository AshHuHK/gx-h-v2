"""Run inherited GX suites and original official-host peers in separate, loopback-only namespaces."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from acceptance.closure.profile import test_environment

GROUPS = ("discovery", "repairs", "score-controls", "archive", "v6", "a1", "a2-through-a5", "a6", "a7", "b4", "c4")
LEGACY = ROOT / "acceptance/v621/compat"
PUBLIC = ROOT / "acceptance/v62c"
PYTHON = ROOT / ".venv/bin/python"
BINARY = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"


def commands(group, path, clash):
    python = [PYTHON, "-B"]
    payload = path / "results"
    peer = Path(__file__).with_name("legacy_peer.py")
    if group == "discovery":
        return [[*python, ROOT / "acceptance/v62b/regression.py", "--output", payload]]
    if group == "repairs":
        return [[*python, ROOT / "acceptance/v621/repairs.py", "--work", payload]]
    if group == "archive":
        return [[*python, Path(__file__).with_name("archived_score.py"), "--work", payload]]
    if group == "score-controls":
        return [[*python, "-m", "unittest", "-v", "acceptance.v62c.test_scoring", "acceptance.v62c.test_spending"]]
    if group == "v6":
        return [[*python, ROOT / "bin/gx-acceptance", "run", "v6-engineering", "--cache", ROOT, "--python",
                 PYTHON, "--clash", clash, "--binary", BINARY, "--run-id", "v622-compat", "--runs-dir", payload]]
    if group == "a6":
        return [[*python, LEGACY / "legacy_a6.py", "--work", payload]]
    if group in ("a2-through-a5", "a7"):
        cases = ["A7"] if group == "a7" else ["A2", "A3", "A4", "A5"]
        return [[*python, ROOT / "acceptance/v62a/interleavings.py", "--work", payload,
                 *(arg for case in cases for arg in ("--case",case))]]
    start = [*python, peer, group, "--work", payload, "--binary", BINARY]
    if group in ("a1", "b4"):
        return [start]
    return [start, *[[*python, PUBLIC / "rate.py", "--work", payload, "--stage", phase]
                     for phase in ("opportunity", "outcome")], [*python, PUBLIC / "parity.py", "--work", payload]]


def run_group(group, work, clash, head):
    path = work / group
    path.mkdir(mode=0o700)
    env = test_environment(path / "env", python=PYTHON, binary=BINARY)
    env.update(PYTHONPATH=os.pathsep.join(map(str, (ROOT, ROOT / "gx-h"))),
               GX_CODEX_OFFLINE="1", GX_CLASH=str(clash))
    calls = []
    for i, inner in enumerate(commands(group, path, clash)):
        net = path / f"network-{i}.json"
        log = path / f"command-{i}.log"
        args = list(map(str, [PYTHON, "-B", ROOT / "acceptance/phase2a/regression/loopback.py",
                              "--clash", clash, "--receipt", net, "--", *inner]))
        call = {"argv":args, "log":str(log.relative_to(work)), "started":datetime.now(timezone.utc).isoformat()}
        with log.open("wb") as stream:
            code = subprocess.run(args, env=env, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT).returncode
        with log.open("rb") as stream:
            log_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        call.update(exit_code=code, log_sha256=log_hash,
                    network=str(net.relative_to(work)))
        calls.append(call)
        if code:
            break
    record = {"source_commit":head, "group":group, "status":"FAIL" if calls[-1]["exit_code"] else "PASS",
              "steps":calls, "paid_model_calls":0}
    (path / "receipt.json").write_text(json.dumps(record, indent=2) + "\n")
    print(group, record["status"], flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--clash", required=True, type=Path)
    parser.add_argument("--group", choices=GROUPS, action="append")
    options = parser.parse_args()
    work, clash = options.work.resolve(), options.clash.resolve(strict=True)
    if work.is_relative_to(ROOT):
        parser.error("Supply a fresh external directory")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", ROOT, "status", "--porcelain"], text=True).splitlines()
    groups = list(dict.fromkeys(options.group or GROUPS))
    with ThreadPoolExecutor(max_workers=min(5,len(groups))) as pool:
        jobs = [pool.submit(run_group, group, work, clash, head) for group in groups]
        result = [job.result() for job in jobs]
    (work / "groups.json").write_text(json.dumps({"source_commit":head,"working_tree_at_start":status,
                                                  "groups":result}, indent=2) + "\n")
    return int(any(r["status"] != "PASS" for r in result))


if __name__ == "__main__":
    raise SystemExit(main())
