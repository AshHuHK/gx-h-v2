"""Generate portable, offline old-version fixtures with the published V6 checkout."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time

BASE = "47fc728d32bc10ba478df6a063529a8d98f98828"
QUALITY = "tl1a-coordinate-quality@1"


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def h(path):
    with path.open("rb") as fp:
        return hashlib.file_digest(fp, "sha256").hexdigest()


def pack(state, dest):
    # Immutable files and an online SQLite backup represent one quiescent fixture.
    image = dest.parent / "sqlite-backup.tmp"
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as src, closing(
        sqlite3.connect(image)
    ) as db:
        src.backup(db)
        db.execute("pragma journal_mode=DELETE")
        assert db.execute("pragma quick_check").fetchone() == ("ok",)
        assert not db.execute("pragma foreign_key_check").fetchone()
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(image, arcname="gx.db")
        for folder in ("artifacts", "jobs", "runs", "threads"):
            tree = state / folder
            if tree.is_dir():
                for path in sorted(tree.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        tar.add(path, arcname=str(path.relative_to(state)), recursive=False)
    image.unlink()


def seed(base, work, out, revision_repo):
    sys.path.insert(0, str(base / "gx-h"))
    from gx_core.native_release import native_state
    from gx_core.p0 import v6_claims, v6_decisions, v6_goal, v6_jobs, metering
    from gx_core.p0.roots import Roots
    from gx_core.persistence import store
    from tests.test_v6_decisions import branch, draft, bind_native_fixture

    for rel in (
        "bin/gx-p0",
        "gx-h/contracts/CONTRACT_SHA",
        "gx-h/gx_core/p0/v6_decisions.py",
        "gx-h/gx_core/p0/v6_jobs.py",
        "gx-h/gx_core/p0/v6_goal.py",
    ):
        published = subprocess.check_output(["git", "-C", revision_repo, "show", f"{BASE}:{rel}"])
        assert (base / rel).read_bytes() == published, rel
    work.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=True)
    state = work / "state"
    before = work / "grant.json"
    source = base / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
    save(
        before,
        {
            "project": "migration-fixture",
            "input_files": [{"role": "structure", "path": str(source), "sha256": h(source)}],
            "capabilities": [QUALITY],
        },
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            base / "bin/gx-p0",
            "start",
            "--state",
            state,
            "--goal",
            "Inventory the deposited coordinates and report the observed scope.",
            "--goal-config",
            before,
            "--run-key",
            "v61-migration",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GX_CODEX_OFFLINE": "1"},
    )
    run = json.loads(proc.stdout)["run_id"]
    with native_state(state), closing(store.open_store(state / "gx.db")) as db:
        call = bind_native_fixture(db, run, state)
        # A synthetic provider receipt through the released reservation and reconciliation owners.
        roots = Roots(db, run_id=run, cwd=state, token_budget=256)
        delivery = roots.delivery
        event = "v61-fixture-usage"
        row, _ = delivery._prepare(
            call["thread_id"], "offline usage fixture", event, False, False, "v61-fixture-client", None
        )
        with store.transaction(db, transaction_owner="V61Fixture", operation="meter_fixture", run_id=run):
            delivery._ack(event, "v61-fixture-metered-turn")
            metering.confirm(db, row["reservation_ref"], 73)
        call["turn_id"] = "v61-fixture-metered-turn"
        source_ref = json.loads(db.execute("SELECT materials_json FROM v6_goals").fetchone()[0])[0]
        proposal = draft(
            {**branch(), "budget_limits": {"job_submissions": 1, "cpu_seconds": 60}},
            {
                "op": "add_node",
                "node": "local:inventory",
                "branch": "local:sample",
                "capability": QUALITY,
                "inputs": [source_ref],
                "parameters": {},
                "depends_on": [],
                "question": "Which chains have deposited coordinates?",
            },
        )
        proposal["reconsider_when"] = [{"event": "job_settled", "subject_ref": "local:inventory"}]
        v6_decisions.adopt(db, **call, decision=proposal)
        assert not db.execute("select 1 from jobs").fetchone()
        assert db.execute("select count(*) from outbox where consumed_at is null").fetchone()[0]
        pack(state, out / "pending.tar.gz")
        for _ in range(500):
            v6_jobs.advance(db, run)
            node = db.execute("select status,result_ref from v6_nodes").fetchone()
            if node["status"] == "SUCCEEDED":
                break
            assert node["status"] in ("PENDING", "RUNNING"), tuple(node)
            time.sleep(0.03)
        else:
            raise RuntimeError("Baseline inventory did not return")
        ref = node["result_ref"]
        v6_goal.ensure_task(db, run)
        call = bind_native_fixture(db, run, state)
        context = db.execute(
            "SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (call["task_id"],)
        ).fetchone()[0]
        claim = {
            "level": "limited_support",
            "statement": "The inventory records observable coordinates.",
            "scope": "One deposited structure",
            "criteria": "The archived inventory must have completed.",
            "limitations": "This fixture evaluates preservation of the recorded scope.",
            "rationale": "A real inventory Job produced the cited result.",
            "evidence_refs": [ref],
        }
        staged = v6_claims.stage(
            db,
            **{**call, "call_id": "stage-migration-claim"},
            context_ref=context,
            payload=claim,
            allowed_refs=[ref],
        )
        stop = draft(
            {
                "op": "close_run",
                "reason": "Inventory returned.",
                "claim_refs": [staged["claim_ref"]],
                "unresolved": ["One deposition"],
            }
        )
        stop["evidence_refs"] = [ref]
        v6_decisions.adopt(db, **call, decision=stop)
        v6_jobs.advance(db, run)
        assert db.execute("select status from v6_goals").fetchone()[0] == "CLOSED"
    subprocess.run(
        [
            sys.executable,
            "-B",
            base / "bin/gx-p0",
            "resume",
            "--state",
            state,
            "--run-id",
            run,
            "--timeout",
            "1",
        ],
        capture_output=True,
        check=True,
        env={**os.environ, "GX_CODEX_OFFLINE": "1"},
    )
    pack(state, out / "terminal.tar.gz")
    save(
        out / "source.json",
        {
            "commit": BASE,
            "run_id": run,
            "input_sha256": h(source),
            "old_contract": json.loads((state / ".gx-native-state.json").read_text())["contract_sha256"],
            "artifacts": {f: h(out / f) for f in ("pending.tar.gz", "terminal.tar.gz")},
            "decision_origin": "SCRIPTED callback identities, public gx-p0 start, released V6 adoption",
            "meter_origin": "SCRIPTED 73 tokens through V6 reservation and settlement, zero provider calls",
            "compute": "REAL_COMPUTE deposited coordinate inventory",
            "pending_boundary": "after durable decision/outbox, before first JobService submit",
        },
    )
    print(json.dumps({"run_id": run, "archives": str(out), "status": "PASS"}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument(
        "--revision-repo", type=Path, help="Local Git object store containing the V6 review commit"
    )
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    seed(
        a.baseline.resolve(), a.work.resolve(), a.out.resolve(), (a.revision_repo or a.baseline).resolve()
    )
