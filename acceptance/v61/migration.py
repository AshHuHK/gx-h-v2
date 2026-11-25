"""Cold in-place V6 upgrade, crash replay, fresh state and full pre-upgrade rollback."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
from migration_seed import BASE, h, save

LAYOUT = ("bin", "gx-h", "codex_adapter", "release-assets", "examples")
TABLES = (
    "v6_goals",
    "v6_graph_versions",
    "v6_decisions",
    "v6_nodes",
    "v6_job_attempts",
    "v6_branches",
    "v6_decision_tasks",
    "p0_v6_native_calls",
    "v6_node_waits",
    "artifacts",
    "jobs",
    "outbox",
    "hypotheses",
    "hypothesis_revisions",
    "budgets",
    "budget_reservations",
    "p0_meter_turns",
    "p0_root_deliveries",
)
FIELDS = {
    "heads": "SELECT run_id,version,sha256 FROM v6_graph_versions ORDER BY run_id,version",
    "claims": "SELECT hypothesis_id,head_revision FROM hypotheses ORDER BY hypothesis_id",
    "jobs": "SELECT job_id,status,idempotency_key,reservation_ref FROM jobs ORDER BY job_id",
    "attempts": "SELECT node_id,operation_key,exec_key FROM v6_job_attempts ORDER BY node_id",
    "nodes": "SELECT node_id,status,job_id,result_ref,operation_key FROM v6_nodes ORDER BY node_id",
    "receipts": "SELECT decision_ref,thread_id,turn_id,call_id FROM v6_decisions ORDER BY decision_ref",
    "outbox": "SELECT outbox_id,topic,idempotency_key,consumed_at FROM outbox ORDER BY rowid",
    "meter": "SELECT reservation_ref,confidence,settled_tokens FROM p0_meter_turns ORDER BY reservation_ref",
    "balance": "SELECT budget_ref,spent_json,reserved_json FROM budgets ORDER BY budget_ref",
    "artifacts": "SELECT artifact_ref,sha256,job_id FROM artifacts ORDER BY artifact_ref",
    "pin": "SELECT json_extract(inputs_json,'$.inputs.capability_catalog[0].plugin') FROM runs",
}


def business(state):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {}
        for table in TABLES:
            # Hash every column so equal IDs alone cannot hide changed returns, budgets, or claims.
            rows = sorted(
                json.dumps(r, default=lambda b: b.hex(), ensure_ascii=False)
                for r in db.execute(f'SELECT * FROM "{table}"')
            )
            tables[table] = {
                "count": len(rows),
                "sha256": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            }
        for namespace, relative, sha in db.execute("SELECT namespace,rel_path,sha256 FROM artifacts"):
            assert h(state / "artifacts" / namespace / relative) == sha, relative
        facts = {name: db.execute(sql).fetchall() for name, sql in FIELDS.items()}
        assert facts["pin"][0][0] and json.loads(facts["pin"][0][0])["id"] == "org.gx.protein"
        return {"tables": tables, "facts": facts}


def contract(state):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        return dict(
            db.execute(
                "SELECT key,value FROM schema_meta WHERE key IN ('contract_sha256','db_schema_sha256')"
            )
        )


def copy_state(src, target):
    """Source is quiescent. Use SQLite backup for a consistent image even with a WAL."""
    shutil.copytree(
        src, target, ignore=shutil.ignore_patterns("gx.db*", ".native.lock", ".gx-driver-wake")
    )
    with closing(sqlite3.connect((src / "gx.db").as_uri() + "?mode=ro", uri=True)) as db, closing(
        sqlite3.connect(target / "gx.db")
    ) as copy:
        db.backup(copy)
        assert copy.execute("PRAGMA quick_check").fetchone() == ("ok",)


def published(repo, destination):
    destination.mkdir()
    with tempfile.TemporaryFile() as stream:
        subprocess.run(["git", "-C", repo, "archive", BASE, *LAYOUT], stdout=stream, check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as pack:
            pack.extractall(destination, filter="data")


def install(source, deploy):
    deploy.mkdir()
    for name in LAYOUT:
        shutil.copytree(
            source / name,
            deploy / name,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc", ".venv"),
        )


def cold(deploy, state, action, *args, expected=(0,)):
    env = {
        **os.environ,
        "GX_CODEX_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
    }
    # Tests call the normal production CLI in a new OS process.
    cmd = [sys.executable, "-B", deploy / "bin/gx-p0", action, "--state", state, *args]
    cp = subprocess.run(cmd, cwd=deploy, env=env, capture_output=True, text=True, timeout=30)
    if cp.returncode not in expected:
        raise ValueError(f"{action} returned {cp.returncode}: {(cp.stderr+cp.stdout)[-2000:]}")
    body = json.loads(cp.stdout)
    return {
        k: body[k]
        for k in ("run_id", "status", "mode", "workflow_id", "graph_head", "goal_status")
        if k in body
    }


def run(root, repo, out, tmp, keep_fixtures=None):
    deploy = tmp / "release"
    published(repo, deploy)
    work = tmp / "old-run"
    state = work / "state"
    generated = tmp / "generated-fixtures"
    subprocess.run(
        [
            sys.executable,
            "-B",
            HERE / "migration_seed.py",
            "--baseline",
            deploy,
            "--revision-repo",
            repo,
            "--work",
            work,
            "--out",
            generated,
        ],
        check=True,
        cwd=tmp,
    )
    if keep_fixtures:
        shutil.copytree(generated, keep_fixtures, dirs_exist_ok=True)
    source = json.loads((generated / "source.json").read_text())
    rid = source["run_id"]
    before = business(state)
    assert len(before["facts"]["jobs"]) == len(before["facts"]["claims"]) == 1
    assert sum(r[2] for r in before["facts"]["meter"]) == 73
    save(out / "baseline.json", {"source": source, "business": before, "contract": contract(state)})
    marker = h(state / ".gx-native-state.json")
    assert json.loads((state / ".gx-native-state.json").read_text())["source_root"] == str(
        deploy / "gx-h"
    )
    snapshot = tmp / "pre-upgrade"
    copy_state(state, snapshot)
    old = tmp / "original-release"
    deploy.rename(old)
    install(root, deploy)
    candidate_contract = json.loads((deploy / "gx-h/contracts/CONTRACT_SHA").read_text())["__combined__"]
    after = []
    for i in range(2):
        row = cold(deploy, state, "resume", "--run-id", rid, "--timeout", "1")
        assert row["status"] == "STOPPED" and row["goal_status"] == "CLOSED"
        observed = business(state)
        assert observed == before, {
            t: (before["tables"][t], observed["tables"][t])
            for t in TABLES
            if before["tables"][t] != observed["tables"][t]
        }
        assert h(state / ".gx-native-state.json") == marker
        assert contract(state)["contract_sha256"] == candidate_contract != source["old_contract"]
        after.append(row)
    save(
        out / "terminal.json",
        {
            "public_cold_resumes": after,
            "marker_sha256": marker,
            "same_source_and_state_roots": True,
            "all_business_rows_equal": True,
            "contract": contract(state),
        },
    )
    # Restore exactly the last committed pre-dispatch DB, while retaining its original V6 marker.
    state.rename(tmp / "terminal-upgraded")
    state.mkdir()
    shutil.copy2(snapshot / ".gx-native-state.json", state)
    with tarfile.open(generated / "pending.tar.gz") as pack:
        pack.extractall(state, filter="data")
    pending = business(state)
    assert pending["facts"]["jobs"] == [] and pending["facts"]["nodes"][0][1] == "PENDING"
    calls = []
    for i in range(2):
        calls.append(cold(deploy, state, "resume", "--run-id", rid, "--timeout", "1.2", expected=(0, 75)))
        now = business(state)
        assert len(now["facts"]["jobs"]) == 1 and now["facts"]["jobs"][0][1] == "SUCCEEDED"
        assert len(now["facts"]["attempts"]) == 1 and now["facts"]["attempts"][0][2]
        assert now["facts"]["nodes"][0][4] == pending["facts"]["nodes"][0][4]
        assert now["facts"]["pin"] == pending["facts"]["pin"]
        if i == 0:
            first = now
        else:
            for key in ("jobs", "attempts", "nodes", "artifacts", "meter", "balance", "receipts"):
                assert first["facts"][key] == now["facts"][key], key
    save(
        out / "pending.json",
        {
            "before": pending["facts"],
            "after": now["facts"],
            "calls": calls,
            "post_commit_dispatch_new_jobs": 1,
            "second_cold_resume_new_jobs": 0,
            "new_model_reservations": 0,
        },
    )
    # Fresh installation uses no historical schema_meta or state objects.
    fresh = tmp / "fresh"
    new = cold(deploy, fresh, "start", "--run-key", "fresh", "--goal", "Inspect future sensor data.")
    assert new["graph_head"] is None and contract(fresh)["contract_sha256"] == candidate_contract
    # Production status on a restored, paused historical static archive; run drives writable migration.
    static = tmp / "static"
    # Establish the previous release's native marker at the static restore location in the same installation.
    upgraded = tmp / "candidate-release"
    deploy.rename(upgraded)
    old.rename(deploy)
    state.rename(tmp / "recovered-pre-dispatch")
    state.mkdir()
    shutil.copy2(snapshot / ".gx-native-state.json", state)
    with tarfile.open(generated / "pending.tar.gz") as package:
        package.extractall(state, filter="data")
    crash_record = tmp / "crash.json"
    crashed = subprocess.run(
        [sys.executable, "-B", HERE / "migration_crash.py", deploy, state, rid, crash_record],
        cwd=tmp,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GX_CODEX_OFFLINE": "1"},
    )
    assert crashed.returncode == 83, (crashed.returncode, crashed.stderr[-1500:])
    interrupted = business(state)
    marker_data = json.loads(crash_record.read_text())
    assert len(interrupted["facts"]["jobs"]) == 1
    assert interrupted["facts"]["jobs"][0][0] == marker_data["job_id"]
    assert interrupted["facts"]["nodes"][0][3] is None
    native_mark = (
        "from pathlib import Path; import sys; sys.path.insert(0,str(Path(sys.argv[1])/'gx-h')); "
        "from gx_core.native_release import native_state; "
        "cm=native_state(Path(sys.argv[2])); cm.__enter__(); cm.__exit__(None,None,None)"
    )
    subprocess.run([sys.executable, "-B", "-c", native_mark, deploy, static], check=True, cwd=tmp)
    import gzip

    static_fixture = root / "acceptance/phase2a/rollback/gx-b8e4f7baeba668ed.db.gz"
    (static / "gx.db").write_bytes(gzip.decompress(static_fixture.read_bytes()))
    with tarfile.open(root / "acceptance/phase2a/rollback/historical-project.tar.gz") as pack:
        pack.extractall(
            static, members=(m for m in pack if m.name.startswith("artifacts/")), filter="data"
        )
    with closing(sqlite3.connect(static / "gx.db")) as db:
        srid, pin = db.execute("select run_id,workflow_sha256 from runs").fetchone()
    status0 = cold(deploy, static, "status", "--run-id", srid)
    # Some pre-V6 fixtures report historical contract hashes predating V6 itself. Here V6 first
    # upgrades the older backup so the tested static deployment is a supported V6 warm store.
    cold(deploy, static, "run", "--run-id", srid, "--timeout", "0.3", expected=(0, 75))
    deploy.rename(old)
    upgraded.rename(deploy)
    crash_reopens = []
    for _ in range(2):
        crash_reopens.append(
            cold(deploy, state, "resume", "--run-id", rid, "--timeout", "1.2", expected=(0, 75))
        )
        recovered = business(state)["facts"]
        assert len(recovered["jobs"]) == 1 and recovered["jobs"][0][1] == "SUCCEEDED"
        assert recovered["jobs"][0][:1] == (marker_data["job_id"],)
        assert recovered["jobs"][0][2:] == interrupted["facts"]["jobs"][0][2:]
        assert recovered["nodes"][0][3]
        assert recovered["attempts"] == interrupted["facts"]["attempts"]
        assert recovered["meter"] == interrupted["facts"]["meter"]
    save(
        out / "inflight.json",
        {
            "observed_crash_exit": crashed.returncode,
            "old_receipt": marker_data,
            "at_crash": interrupted["facts"],
            "recovered": recovered,
            "public_cold_resumes": crash_reopens,
            "additional_submissions": 0,
            "additional_paid_turns": 0,
        },
    )
    for _ in range(2):
        st = cold(deploy, static, "run", "--run-id", srid, "--timeout", "0.3", expected=(0, 75))
        assert st["status"] == status0["status"]
    with closing(sqlite3.connect(static / "gx.db")) as db:
        assert db.execute("select workflow_sha256 from runs").fetchone()[0] == pin
        assert db.execute("pragma foreign_key_check").fetchall() == []
    assert contract(static)["contract_sha256"] == candidate_contract
    # Restore both exact old deployment and complete pre-upgrade state; no migration reversal SQL.
    deploy.rename(upgraded)
    old.rename(deploy)
    state.rename(tmp / "pending-upgraded")
    shutil.copytree(snapshot, state)
    rollback_hash = h(state / "gx.db")
    assert rollback_hash == h(snapshot / "gx.db") and h(state / ".gx-native-state.json") == marker
    rolled = cold(deploy, state, "resume", "--run-id", rid, "--timeout", "1")
    assert (
        rolled["status"] == "STOPPED"
        and business(state) == before
        and contract(state)["contract_sha256"] == source["old_contract"]
    )
    return {
        "passed": True,
        "fixture_source": source,
        "fresh": new,
        "terminal_reopens": 2,
        "terminal_new_jobs": 0,
        "terminal_new_charges": 0,
        "pending_new_jobs": 1,
        "pending_replayed_jobs": 0,
        "pending_new_paid_turns": 0,
        "interrupted_old_job_new_submissions": 0,
        "old_protein_pin": json.loads(before["facts"]["pin"][0][0]),
        "static": {
            "run": srid,
            "status": st["status"],
            "source_db_sha256": h(static_fixture),
            "workflow_sha256": pin,
            "cold_opens": 2,
        },
        "rollback": {
            "whole_state_backup": True,
            "original_source_and_state_roots": True,
            "backup_db_sha256": rollback_hash,
            "old_marker_sha256": marker,
            "business_rows_identical": True,
        },
        "old_contract": source["old_contract"],
        "new_contract": candidate_contract,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--baseline",
        type=Path,
        default=ROOT,
        help="Local Git repo containing 47fc728 (default: product checkout)",
    )
    p.add_argument(
        "--save-fixtures", type=Path, help="Save the exact generated V6 fixtures beside the receipt"
    )
    p.add_argument("--candidate", type=Path, default=ROOT)
    opt = p.parse_args()
    opt.out = opt.out.resolve()
    opt.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gx-v61-migration-") as work:
        result = run(
            opt.candidate.resolve(), opt.baseline.resolve(), opt.out, Path(work), opt.save_fixtures
        )
    save(opt.out / "result.json", result)
    print(json.dumps(result, sort_keys=True))
