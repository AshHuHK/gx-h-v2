"""Exercise a paused historical project through 51e0e13 -> candidate -> 51e0e13 offline."""
import argparse
from contextlib import closing
import gzip
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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance"))
from v21_staging import check, git, job_sidecars, now, original_facts, quiescent_copy, read_db, save, sha, stage_source

BASE = "51e0e13583d95e9cad1386e1d4c77324977e97ea"
BUNDLE = Path(__file__).with_name("historical-project.tar.gz")
HISTORY = ROOT / "acceptance/v31/v2-checkpoint.json"


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def snapshot(state, output, scratch):
    facts = original_facts(state)  # Includes reading and hashing each published Artifact's bytes.
    receipts = job_sidecars(state, facts["jobs"])
    with closing(read_db(state)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = list(conn.execute("PRAGMA foreign_key_check"))
        check(integrity == "ok" and not fk, "invalid SQLite image")
        schema = [tuple(r) for r in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        schema_meta = dict(conn.execute("SELECT key,value FROM schema_meta"))
        check(not any(r[1] in ("graph_versions", "node_attempts") for r in schema), "unexpected dynamic table")
        waits = conn.execute("SELECT checkpoint_id,node,seq,interrupt_ref,state_json FROM checkpoints "
                             "WHERE run_id=? ORDER BY seq DESC LIMIT 1", (facts["run"]["run_id"],)).fetchone()
        state_json = json.loads(waits["state_json"])
        check("node_waits" not in state_json, "this rollback scenario needs a historical checkpoint")
        events = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY rowid")]
        topics = [dict(r) for r in conn.execute(
            "SELECT outbox_id,topic,idempotency_key,consumed_at,attempts FROM outbox ORDER BY rowid")]
        held = [dict(r) for r in conn.execute(
            "SELECT reservation_ref,subject_id,status,amounts_json FROM budget_reservations WHERE status='HELD'")]
        image = scratch / "snapshot.db"
        with closing(sqlite3.connect(image)) as dest:
            conn.backup(dest)
    checksum = sha(image)
    compressed = output / ("gx-" + checksum[:16] + ".db.gz")
    if not compressed.exists():
        compressed.write_bytes(gzip.compress(image.read_bytes(), mtime=0))
    image.unlink()
    return {
        "sqlite": {"sha256": checksum, "file": compressed.name, "integrity": integrity, "foreign_keys": len(fk)},
        "schema_sha256": digest(schema), "schema_meta_sha256": digest(schema_meta),
        "p0_migration_count": sum(k.startswith("p0.migration.") for k in schema_meta),
        "business_rows_and_artifact_bytes_sha256": digest(facts), "run": facts["run"],
        "checkpoint": {"id": waits["checkpoint_id"], "seq": waits["seq"], "node": waits["node"],
                       "completed": state_json["completed"], "interrupt_ref": waits["interrupt_ref"],
                       "state_json_sha256": digest(state_json)},
        "jobs": facts["jobs"], "job_receipt_hashes": receipts,
        "launch_claims": sorted(f.parent.name for f in (state / "jobs").glob("*/launch.claim")),
        "artifact_count": len(facts["artifacts"]), "artifact_index_sha256": digest(facts["artifacts"]),
        "budget_settlement_count": len(facts["charges"]), "budget_settlements_sha256": digest(facts["charges"]),
        "scientific_settlement_count": len(facts["settlements"]), "scientific_settlements_sha256": digest(facts["settlements"]),
        "event_count": len(events), "events_sha256": digest(events), "outbox": topics,
        "hard_budget_accounts": facts["original_budget_lines"], "held": held,
    }


def exercise(args, output, scratch, report):
    historical = json.loads(HISTORY.read_bytes())
    if args.project:
        source = args.project.resolve()
        report["source"] = str(source)
    else:
        source = scratch / "historical-source"
        source.mkdir()
        with tarfile.open(BUNDLE) as archive:
            archive.extractall(source, filter="data")
        report["source"] = BUNDLE.relative_to(ROOT).as_posix()
        report["source_bundle_sha256"] = sha(BUNDLE)
    expected = historical["checkpoint"]
    check(sha(source / "gx.db") == expected["db_sha256"], "wrong historical source DB")
    wal = source / "gx.db-wal"
    check(not wal.exists() or wal.stat().st_size == 0, "historical copy has uncheckpointed writes")
    rid = expected["run"]["run_id"]
    candidate = git("rev-parse", "--verify", args.candidate + "^{commit}")
    subprocess.run(["git", "merge-base", "--is-ancestor", BASE, candidate], cwd=ROOT, check=True)
    report.update(baseline=BASE, candidate=candidate, evidence_class="SCRIPTED",
                  input_receipt="acceptance/v31/v2-checkpoint.json", historical_source_commit=historical["source_install"]["git_commit"],
                  historical_db_sha256=expected["db_sha256"], invocation=[sys.executable, *sys.argv],
                  source_checkout_status=git("status", "--porcelain", "--untracked-files=normal"),
                  runner_sha256=sha(Path(__file__)), archive_dirty=False)
    install = scratch / "install"
    stage = scratch / "candidate"
    project = scratch / "project"
    backup = scratch / "before-upgrade"
    report["archives"] = [stage_source(scratch, install, BASE), stage_source(scratch, stage, candidate)]
    guard = scratch / "host-attempt"
    guard.write_text('#!/bin/sh\necho codex >> "$GX_ROLLBACK_AUDIT_LOG"\nexit 71\n')
    guard.chmod(0o700)
    attempts = scratch / "offline-events"
    attempts.touch()
    audit_dir = scratch / "instrumentation"
    audit_dir.mkdir()
    (audit_dir / "sitecustomize.py").write_text(
        "import os, sys\ndef observe(event, args):\n"
        "    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):\n"
        "        with open(os.environ['GX_ROLLBACK_AUDIT_LOG'], 'a') as f: print(event, file=f)\n"
        "        raise RuntimeError('rollback evidence is offline')\nsys.addaudithook(observe)\n")
    env = {**os.environ, "GX_CODEX_OFFLINE": "1", "GX_P0_CODEX": str(guard), "GX_ROLLBACK_AUDIT_LOG": str(attempts),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join((str(audit_dir), str(install / "gx-h")))}
    # Establish the temporary install's native marker once, in an empty directory. It is backed up as-is.
    setup = ("import sys\nfrom gx_core.native_release import native_state\n"
             "with native_state(sys.argv[1]): pass\n")
    subprocess.run([sys.executable, "-B", "-c", setup, str(project)], cwd=install, env=env, check=True)
    shutil.copytree(source, project, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
        ".native.lock", ".gx-native-state.json", "gx.db-shm", "gx.db-wal"))
    initial = original_facts(project)
    check(digest(initial) == expected["original_facts_sha256"], "fixture differs from the published scientific facts")
    check(job_sidecars(project, initial["jobs"]) == expected["job_sidecars"], "source Job receipt mismatch")
    report["source_migrations"] = len(expected["p0_migrations"])
    report["historical_facts_sha256"] = digest(initial)
    report["run_id"] = rid
    report["timeline"] = []
    log = output / "cli.jsonl.gz"

    def invoke(phase, revision, action):
        argv = [sys.executable, "-B", str(install / "bin/gx-p0"), action, "--state", str(project), "--run-id", rid]
        if action == "run":
            argv += ["--timeout", "5"]
        row = {"stage": phase, "revision": revision, "started_utc": now(), "command": argv}
        proc = subprocess.run(argv, cwd=install, env=env, capture_output=True, text=True, timeout=30)
        row.update(ended_utc=now(), exit_code=proc.returncode, stdout_sha256=hashlib.sha256(proc.stdout.encode()).hexdigest(),
                   stderr=proc.stderr)
        raw = json.dumps({**row, "stdout": proc.stdout}, ensure_ascii=False) + "\n"
        with log.open("ab") as stream:
            stream.write(gzip.compress(raw.encode(), mtime=0))
        report["timeline"].append(row)
        check(proc.returncode == 0, f"{phase} {action}: {proc.stderr}")
        reply = json.loads(proc.stdout)
        row.update(status=reply["status"], run_id=reply["run_id"])
        check(reply["run_id"] == rid and reply["status"] == "PAUSED", "wrong run or active rollback")
        check(not attempts.read_text(), "unexpected model process or networking")

    invoke("cold-51e0e13", BASE, "stop")
    invoke("cold-51e0e13", BASE, "run")
    report["backup_method"] = quiescent_copy(project, backup)
    report["before"] = snapshot(backup, output, scratch)
    check(report["before"]["business_rows_and_artifact_bytes_sha256"] == digest(initial), "baseline rewrote history")
    install.rename(scratch / "baseline")
    stage.rename(install)
    invoke("candidate-quiescent", candidate, "stop")
    invoke("candidate-quiescent", candidate, "status")
    report["candidate_before_restore"] = snapshot(project, output, scratch)
    # Preserve both sides. Old software only sees the copied pre-upgrade project at its own installed paths.
    install.rename(stage)
    project.rename(scratch / "retained-candidate-project")
    (scratch / "baseline").rename(install)
    shutil.copytree(backup, project, ignore=shutil.ignore_patterns("gx.db-wal", "gx.db-shm"))
    check(sha(backup / "gx.db") == sha(project / "gx.db"), "backup file differs immediately after restore")
    for action in ("status", "run", "run"):
        invoke("restored-51e0e13", BASE, action)
    report["restored"] = snapshot(project, output, scratch)
    for name in ("candidate_before_restore", "restored"):
        for key in report["before"].keys() - {"sqlite"}:
            check(report[name][key] == report["before"][key], f"{name} changed {key}")
    check(sha(source / "gx.db") == expected["db_sha256"], "source DB changed")
    report["cli_log"] = {"file": log.name, "sha256": sha(log)}
    report["network_attempts"] = len([line for line in attempts.read_text().splitlines() if line.startswith("socket.")])
    report["codex_spawn_attempts"] = attempts.read_text().splitlines().count("codex")
    def counts(view):
        return {"jobs": len(view["jobs"]), "launch_claims": len(view["launch_claims"]),
                "artifacts": view["artifact_count"], "budget_settlements": view["budget_settlement_count"],
                "scientific_settlements": view["scientific_settlement_count"]}

    before = counts(report["before"])
    report["additional_counts"] = {
        stage: {k: count - before[k] for k, count in counts(report[stage]).items()}
        for stage in ("candidate_before_restore", "restored")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="committed product revision")
    parser.add_argument("--project", type=Path, help="optional external copy of the same frozen historical project")
    parser.add_argument("--out", type=Path, required=True, help="a fresh evidence directory")
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"test": "historical static Run rollback and repeat writable reopen", "result": "FAIL", "exit_code": 1}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-phase2a-rollback-") as temp:
            exercise(args, output, Path(temp), report)
        report.update(result="PASS", exit_code=0)
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        save(output / "receipt.json", report)


if __name__ == "__main__":
    main()
