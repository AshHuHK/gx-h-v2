"""Cold-open closed research at its installed release and compare durable scientific identities."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def observation(state, run):
    report_dir = state / "research" / run
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?", (run,)
        ).fetchone()
        if row is None or row[0] != "CLOSED":
            raise ValueError("The cold export requires an already closed, archived Run.")
        inputs = json.loads(db.execute(
            "SELECT inputs_json FROM runs WHERE run_id=?", (run,)
        ).fetchone()[0])["inputs"]
        events = db.execute(
            "SELECT count(*),max(event_id) FROM events WHERE run_id=?", (run,)
        ).fetchone()
        ids = db.execute(
            "SELECT job_id,capability_id,capability_version,scheduler_kind,scheduler_ref,"
            "idempotency_key,input_manifest_ref FROM jobs WHERE run_id=? ORDER BY job_id", (run,)
        ).fetchall()
        originals = []
        for ref, job, kind, namespace, relpath, expected in db.execute(
            "SELECT artifact_ref,job_id,kind,namespace,rel_path,sha256 FROM artifacts "
            "WHERE run_id=? AND job_id IS NOT NULL AND published=1 ORDER BY artifact_ref", (run,)
        ):
            actual = digest(state / "artifacts" / namespace / relpath)
            if actual != expected:
                raise ValueError(f"Recorded SHA disagrees with the original bytes for {ref}.")
            originals.append({"artifact_ref": ref, "job_id": job, "kind": kind, "sha256": actual})
        recipes = db.execute(
            "SELECT reference,digest FROM v6_saved_recipes ORDER BY reference,digest"
        ).fetchall()
    result = json.loads((report_dir / "result.json").read_bytes())
    result.pop("invocation", None)
    return {
        "run": run, "goal_status": row[0], "stop_decision_ref": row[1],
        "original_policy": inputs.get("harness_policy"),
        "run_event_count": events[0], "highest_event": events[1],
        "external_job_identities": ids, "original_job_artifacts": originals,
        "registered_recipe_digests": recipes,
        "report_sha256": digest(report_dir / "REPORT.md"),
        "decision_sha256": digest(report_dir / "DECISION.md"),
        "result_scientific_sha256": hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def reopen(site, state, runs):
    marker = json.loads((state / ".gx-native-state.json").read_bytes())
    if marker["source_root"] != str(site / "gx-h") or marker["state_root"] != str(state):
        raise ValueError("Use the original, release-bound state and installed source.")
    observations = []
    for run in runs:
        before = observation(state, run)
        command = [str(site / "bin/gx-p0"), "research", "--state", str(state),
                   "--run-id", run, "--timeout", "12"]
        env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(command, cwd=site, env=env, text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        after = observation(state, run)
        if before != after:
            raise ValueError("Changed cold fields: " + ", ".join(k for k in before if before[k] != after[k]))
        added = json.loads(result.stdout)["invocation"]["added"]
        if added != {"jobs": 0, "host_turns": 0}:
            raise ValueError(f"Closed Run unexpectedly added work: {added!r}")
        observations.append({**after, "before_equals_after": True, "added": added})
    return observations


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    parser.add_argument("original_state", type=Path)
    parser.add_argument("runs", nargs="+")
    args = parser.parse_args()
    site = args.site.resolve()
    print(json.dumps({
        "evidence": "COLD_REOPEN_OF_LIVE_CODEX_EVIDENCE",
        "revision": subprocess.check_output(["git", "-C", str(site), "rev-parse", "HEAD"], text=True).strip(),
        "ordinary_entry": "bin/gx-p0 research --state <original-owned-state> --run-id <closed-id>",
        "result": reopen(site, args.original_state.resolve(), args.runs),
    }, indent=2))
