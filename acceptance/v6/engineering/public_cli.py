"""Probe the public Goal-only CLI and in-place migration against durable records."""
import argparse
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from acceptance.closure import archive, legacy_ledger

GOAL = "Which observations can be supported by the supplied measurements?"


def cli(state, env, action, *arguments, ok=True, expected_exit=0):
    call = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), action, "--state", str(state), *map(str, arguments)]
    result = subprocess.run(call, env=env, cwd=state.parent, capture_output=True, text=True, timeout=100)
    if (result.returncode != expected_exit if ok else result.returncode == 0):
        raise AssertionError(f"{action} exited {result.returncode}: {result.stderr[-1500:]} {result.stdout[-1500:]}")
    return {"action": action, "arguments": list(map(str, arguments)), "exit": result.returncode,
            "body": json.loads(result.stdout) if ok else {"error": result.stderr + result.stdout}}


def frozen_state(state, out):
    copy = out / "gx.db"
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(copy)) as target:
        source.backup(target)
        assert target.execute("PRAGMA quick_check").fetchone() == ("ok",)
    with gzip.open(out / "gx.db.gz", "wb") as gz:
        gz.write(copy.read_bytes())
    copy.unlink()


def counts(state, run_id):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        return {t: db.execute(f"SELECT count(*) FROM {t} WHERE run_id=?", (run_id,)).fetchone()[0]
                for t in ("runs", "tasks", "jobs", "checkpoints", "p0_roots")}


def goal(work, out):
    state = work / "state"
    env = {**os.environ, "GX_CODEX_OFFLINE": "1"}
    calls = []
    def call(action, *argv, **kwargs):
        result = cli(state, env, action, *argv, **kwargs)
        calls.append(result)
        archive.save(out / "commands.json", calls)
        return result["body"]

    opened = call("start", "--run-key", "v6-cold", "--goal", GOAL)
    rid = opened["run_id"]
    cold = call("status", "--run-id", rid)
    for status in (opened, cold):
        assert status["mode"] == "goal" and status["graph_head"] is None
        assert status["goal_version"] == 1
    assert counts(state, rid) == {"runs": 1, "tasks": 1, "jobs": 0, "checkpoints": 0, "p0_roots": 0}
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        initial_reservations = db.execute("SELECT * FROM budget_reservations ORDER BY rowid").fetchall()
    assert call("start", "--run-key", "v6-cold", "--goal", GOAL)["run_id"] == rid
    collision = call("start", "--run-key", "v6-cold", "--goal", "A different question", ok=False)
    assert "different" in collision["error"].lower() or "conflict" in collision["error"].lower(), collision
    resumed = call("resume", "--run-id", rid, "--timeout", "0.5", expected_exit=75)
    assert resumed["graph_head"] is None and resumed["tasks"] == cold["tasks"], resumed["tasks"]
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        pin = db.execute("SELECT workflow_id,workflow_version,workflow_sha256 FROM runs WHERE run_id=?",
                         (rid,)).fetchone()
        assert pin == (None, None, None), pin
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT * FROM budget_reservations ORDER BY rowid").fetchall() == initial_reservations
        assert db.execute("SELECT count(*) FROM p0_meter_native_turns").fetchone()[0] == 0
    observed = counts(state, rid)
    assert observed["tasks"] == 1 and observed["jobs"] == observed["checkpoints"] == 0
    frozen_state(state, out)
    return {"passed": True, "run_id": rid, "mode": cold["mode"], "old_workflow_pin": pin,
            "counts_after_resume": observed, "calls": len(calls), "model_turns": 0}


def legacy(work, out):
    source = ROOT / "acceptance/phase2a/rollback/gx-b8e4f7baeba668ed.db.gz"
    state = work / "old"
    result = legacy_ledger.phase2a_ledger(source, state, {"SETTLED": 3})
    assert "p0.migration.v6.sql" in result["added_migrations"], result["added_migrations"]
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        pin = db.execute("SELECT workflow_id,workflow_version,workflow_sha256 FROM runs").fetchone()
        assert all(pin), pin
    frozen_state(state, out)
    archive.save(out / "commands.json", {"source": str(source.relative_to(ROOT)),
                 "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(), "opens": 2, "public_status_reads": 3})
    return {"run_status": result.pop("status"), **result, "old_workflow_pin": pin}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("case", choices=("goal", "legacy"))
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="gx-v6-cli-") as tmp:
        receipt = {"goal": goal, "legacy": legacy}[args.case](Path(tmp), args.out)
    archive.save(args.out / "result.json", receipt)
