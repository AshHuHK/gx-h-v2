"""Build a private v2.1 checkpoint for v3 cold-start and rollback acceptance."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance"))
import e2e_responses
from e2e_grid import fixture_env
from v21_staging import (
    check, inspect, job_sidecars, migrations, now, original_facts, quiescent_copy,
    read_db, save, sha, stage_source,
)

FROZEN = "a05dcef64522fbbbc12120b1d78da518e6886b10"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pace_decision(action):
    def paced(body, request_id):
        event, facts = action(body, request_id)
        if (facts.get("round"), facts.get("output_kind"), facts.get("tool")) == (
                0, "science_decision", "gx_submit_product"):
            args = json.loads(event["arguments"])
            for grid in args["product"]["next_grids"].values():
                grid["delay_seconds"] = 5
            event = {**event, "arguments": json.dumps(args)}
        return event, facts
    return paced


def checkpoint(work, evidence):
    install, state = work / "install", work / "project"
    archive = stage_source(work, install, FROZEN)
    evidence["source_install"] = archive
    lock = json.loads((ROOT / "release-assets/lock.json").read_text())["linux_x64"]
    binary = ROOT / "vendor/codex-runtime/package" / lock["binary"]
    check(sha(binary) == lock["binary_sha256"], "Locked native binary differs")
    with (e2e_responses.local_responses(work / "loopback.jsonl") as peer,
          patch.object(e2e_responses, "fixture_action", pace_decision(e2e_responses.fixture_action))):
        env = fixture_env(work, peer, str(binary))
        env["PATH"] = str(ROOT / ".venv/bin") + os.pathsep + env["PATH"]

        def cmd(action, *args, source=install):
            return [str(source / "bin/gx-p0"), action, "--state", str(state), *args]

        def invoke(action, *args, source=install, label="frozen-v2.1"):
            n = peer.request_count
            argv = cmd(action, *args, source=source)
            proc = subprocess.run(argv, cwd=source, env=env, text=True, capture_output=True, timeout=45)
            evidence["commands"].append({"source": label, "command": argv, "exit_code": proc.returncode,
                                         "new_fixture_responses": peer.request_count - n})
            if proc.returncode:
                raise RuntimeError(f"{label} {action}: {proc.stderr[-1000:]}")
            return json.loads(proc.stdout)

        start = invoke("start", "--demo-grid")
        argv = cmd("run", "--timeout", "120")
        with subprocess.Popen(argv, cwd=install, env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as driver:
            try:
                until = time.monotonic() + 105
                while True:
                    view = inspect(state)
                    if (len(view["decisions"]) == 1 and len(view["jobs"]) == 3
                            and view["jobs"][-1]["status"] in {"SUBMITTED", "RUNNING"}
                            and all(row["status"] == "SETTLED" for row in view["paid"])):
                        break
                    check(driver.poll() is None and time.monotonic() < until,
                          "Frozen v2.1 did not reach a settled decision and third in-flight Job")
                    time.sleep(.15)
                stopped = invoke("stop")
                stdout, stderr = driver.communicate(timeout=30)
                check(driver.returncode == 0 and json.loads(stdout)["status"] == stopped["status"] == "PAUSED",
                      f"Frozen v2.1 did not quiesce. {stderr[-600:]}")
                evidence["commands"].append({"source": "frozen-v2.1", "command": argv,
                                             "exit_code": driver.returncode, "status": stopped["status"]})
            finally:
                if driver.poll() is None:
                    driver.terminate()
        view = original_facts(state)
        check(view["run"]["run_id"] == start["run_id"] and len(view["submissions"]) == 2
              and len(view["jobs"]) == 3, "Wrong source project checkpoint")
        until = time.monotonic() + 45
        while len(list(state.rglob("outcome.json"))) < len(view["jobs"]):
            check(time.monotonic() < until, "Timed out waiting for frozen CPU Job sidecars")
            time.sleep(.1)
        receipts = job_sidecars(state, view["jobs"])
        snapshot = work / "snapshot"
        snapshot.mkdir()
        native = Path(env["CODEX_HOME"])
        evidence["checkpoint"] = {
            "project": quiescent_copy(state, snapshot / "project"),
            "codex": quiescent_copy(native, snapshot / "codex"),
            "run": view["run"], "original_budget": view["original_budget_lines"],
            "source_model_mode": "scripted loopback Responses",
            "source_model_responses": peer.request_count,
            "original_facts_sha256": digest(view), "job_sidecars": receipts,
            "db_sha256": sha(snapshot / "project/gx.db"), "artifact_index_sha256": digest(view["artifacts"]),
            "published_artifacts": len(view["artifacts"]), "accepted_submissions": len(view["submissions"]),
            "native_turns": len(view["usage"]), "p0_migrations": migrations(snapshot / "project"),
        }
        meta = evidence["checkpoint"]
        rollouts = {p.relative_to(native).as_posix(): sha(p) for p in native.glob("sessions/**/*.jsonl")}
        check(rollouts and all(sha(snapshot / "codex" / rel) == value for rel, value in rollouts.items()),
              "Native rollout missing or changed during copy")
        meta["rollouts"] = rollouts
        check(original_facts(snapshot / "project") == view, "Source facts differ from checkpoint")
        check(job_sidecars(snapshot / "project", view["jobs"]) == receipts, "Source Job receipt changed")
        check(not (snapshot / "codex/auth.json").exists(), "Unexpected credential file in fixture")
        # Restore backup to the original absolute paths. Keep both the sources and the cold copy.
        state.rename(work / "source-project")
        native.rename(work / "source-codex")
        shutil.copytree(snapshot / "project", state, symlinks=True)
        shutil.copytree(snapshot / "codex", native, symlinks=True)
        check(sha(state / "gx.db") == meta["db_sha256"], "Restored DB bytes changed")
        count = peer.request_count
        for action, extra in (("status", ()), ("run", ("--timeout", "10"))):
            check(invoke(action, *extra)["status"] == "PAUSED", "Cold v2.1 project did not reopen")
        status = invoke("status", source=ROOT, label="v3-checkout-before-implementation")
        check(status["run_id"] == start["run_id"] and status["status"] == "PAUSED",
              "v3 checkout failed to read legacy workspace")
        check(peer.request_count == count and original_facts(state) == view
              and job_sidecars(state, view["jobs"]) == receipts, "Cold reopen changed facts or started new work")
        with closing(read_db(state)) as db:
            check(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                  and not db.execute("PRAGMA foreign_key_check").fetchall(), "Invalid restored GX DB")
        evidence["restore"] = {"status": status["status"], "source_facts_equal": True,
                               "job_receipts_equal": True, "db_integrity": "ok", "foreign_key_violations": 0,
                               "extra_fixture_responses": peer.request_count - count,
                               "retained_private_root": work.relative_to(ROOT).as_posix()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="A new directory under this checkout's var/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    if not work.is_relative_to(ROOT / "var"):
        parser.error("use an unused directory under var/ for the private project")
    work.mkdir(parents=True, exist_ok=False)
    report = {"test": "frozen v2.1 paused checkpoint and cold copy reopen",
              "start_utc": now(), "source": "acceptance/v3/staging/cold_fixture.py",
              "command": [str(Path(sys.executable).relative_to(ROOT)), "-B", *sys.argv],
              "staging_checkout_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                                  text=True).strip(),
              "commands": [], "result": "FAIL"}
    try:
        checkpoint(work, report)
        report.update(result="PASS", exit_code=0)
    except Exception as exc:
        report.update(error=str(exc), exit_code=1)
        raise
    finally:
        report["end_utc"] = now()
        save(args.output, report)


if __name__ == "__main__":
    main()
