"""Restore an inactive sealed native Goal and its generated analytical method to a separate deployment."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def snapshot(db, run, recipe):
    row = db.execute("SELECT status,goal_version,stop_decision_ref FROM v6_goals WHERE run_id=?", (run,)).fetchone()
    if not row or row[0] != "CLOSED":
        raise ValueError("The source Goal must already be sealed")
    saved = db.execute("SELECT digest,definition_json FROM v6_saved_recipes WHERE reference=? AND source_run=?",
                       (recipe, run)).fetchone()
    if not saved:
        raise ValueError("The requested recipe was not saved by this Goal")
    return {"run_id": run, "status": row[0], "goal_version": row[1], "decision_ref": row[2],
            "recipe": recipe, "recipe_digest": saved[0],
            "jobs": db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0],
            "host_turns": db.execute("SELECT count(*) FROM p0_root_turn_usage u WHERE EXISTS "
                                     "(SELECT 1 FROM p0_roots r WHERE r.thread_id=u.thread_id AND r.run_id=?)",
                                     (run,)).fetchone()[0],
            "methods": {s["capability"]: s["method_digest"] for s in json.loads(saved[1])["steps"]}}


def files(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def run(args):
    origin, dest = args.source.resolve(), args.destination.resolve()
    before_release, release = args.source_release.resolve(), args.release.resolve()
    if origin == dest or before_release == release or dest.exists() or dest.is_relative_to(origin):
        raise ValueError("Restore into an unused work directory on a different deployment")
    runtime = release / ".venv/bin/python"
    if Path(sys.prefix).resolve() != runtime.parents[1]:
        raise ValueError(f"Run this script with {runtime}")
    sys.path.insert(0, str(release / "gx-h"))
    from gx_core.native_release import native_state

    package_path = "/".join(args.provider.split("@"))
    records = json.loads((before_release / "gx-h/config/workflow_packages.json").read_bytes())["plugins"]
    pin, = [r for r in records if r["path"] == package_path]
    package = before_release / "gx-h/plugins/installed" / package_path
    cmd = [str(runtime), str(release / "bin/gx-p0")]
    env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    def cli(*argv):
        proc = subprocess.run([*cmd, *map(str, argv)], env=env, check=True, capture_output=True, text=True)
        return json.loads(proc.stdout)

    plugin = cli("plugin", "install", package, "--sha256", pin["sha256"])
    check = cli("plugin", "verify", args.provider)
    if check["failures"] or not any(p["sha256"] == pin["sha256"] for p in check["plugins"]):
        raise ValueError("The target plugin verification did not match the source release")

    os.umask(0o077)
    state = dest / "state"
    source = origin / "state"
    # Hold a shared lock against the native owner's exclusive process lock. Keep all source files read-only.
    with (source / ".native.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        dest.mkdir(parents=True, mode=0o700)
        with native_state(state):
            # Stage DB and any WAL together under the source lock. SQLite can create scratch SHM files here.
            with tempfile.TemporaryDirectory(dir=dest) as scratch:
                copy_db = Path(scratch) / "gx.db"
                shutil.copyfile(source / "gx.db", copy_db)
                if (source / "gx.db-wal").is_file():
                    shutil.copyfile(source / "gx.db-wal", Path(scratch) / "gx.db-wal")
                with closing(sqlite3.connect(copy_db)) as original, closing(sqlite3.connect(state / "gx.db")) as backup:
                    before = snapshot(original, args.run_id, args.recipe)
                    original.backup(backup)
                    if snapshot(backup, args.run_id, args.recipe) != before:
                        raise ValueError("Backup changed the historical Run or recipe")
            copied = {}
            for name in ("artifacts", "threads", "runs", "jobs", "research"):
                if (source / name).exists():
                    shutil.copytree(source / name, state / name, symlinks=True)
                    inventory = files(source / name)
                    if files(state / name) != inventory:
                        raise ValueError("Restored content differs from archive: " + name)
                    copied[name] = len(inventory)
            old_sessions, new_sessions = origin / "codex/sessions", dest / "codex/sessions"
            shutil.copytree(old_sessions, new_sessions, symlinks=True)
            if files(old_sessions) != files(new_sessions):
                raise ValueError("Session archive copy differs from the original")
            copied["sessions"] = len(files(new_sessions))
        prior_report = source / "research" / args.run_id / "REPORT.md"
        old_sha = hashlib.sha256(prior_report.read_bytes()).hexdigest()

    export = cli("research", "--state", state, "--run-id", args.run_id)
    after_sha = hashlib.sha256(Path(export["report"]).read_bytes()).hexdigest()
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        after = snapshot(db, args.run_id, args.recipe)
        interpreter = db.execute("SELECT python_executable FROM environments WHERE env_id='control-plane-python'").fetchone()[0]
    result = json.loads(Path(export["result"]).read_bytes())
    if before != after or result["closure"]["decision_ref"] != before["decision_ref"] or old_sha != after_sha:
        raise ValueError("Cold report export differs from the sealed decision or archived report")
    if export["invocation"]["added"] != {"jobs": 0, "host_turns": 0}:
        raise ValueError("Cold report export dispatched work")
    receipt = {"source_release": revision(before_release), "destination_release": revision(release),
               "copied": copied, "sealed": after, "report_sha256": after_sha,
               "invocation": export["invocation"]["added"], "generated_plugin": plugin,
               "environment_ledger_interpreter": interpreter}
    (dest / "restoration.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    return receipt


def revision(directory):
    return subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "destination", "source-release", "release"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("provider", "recipe", "run-id"):
        parser.add_argument("--" + name, required=True)
    print(json.dumps(run(parser.parse_args()), indent=2, ensure_ascii=False))
