"""Crash the offline official pilot before its first summary, then resume the original slot."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from acceptance.v622.batch_recovery import readonly, require
from acceptance.v623.live import pilot
from acceptance.v622.public.enrollment import digest, read, save

SLOT = "01"


def snapshot(work):
    slot = work / SLOT
    with closing(readonly(slot / "state/gx.db")) as db:
        goals = db.execute("SELECT run_id,status FROM v6_goals").fetchall()
        require(len(goals) == 1 and goals[0]["status"] == "CLOSED", "Expected one terminal original Goal")
        rid = goals[0]["run_id"]
        jobs = [dict(r) for r in db.execute("SELECT job_id,status,reservation_ref,measured_json FROM jobs "
                                             "WHERE run_id=? ORDER BY job_id", (rid,))]
        require(jobs and all(r["status"] == "SUCCEEDED" for r in jobs), "Missing original terminal scientific Jobs")
        actions = [dict(r) for r in db.execute("""
            SELECT decision_ref,call_id,task_id,lease_id,thread_id,turn_id,request_json,receipt_json
            FROM v6_decisions WHERE run_id=? ORDER BY rowid""", (rid,))]
        facts = [dict(r) for r in db.execute("""SELECT event_id,kind,subject_id,payload_json FROM events WHERE
            run_id=? AND kind IN ('v621.material.assigned','v6.run.close_requested','v6.decision.committed')
            ORDER BY event_id""", (rid,))]
        require(sum(e["kind"] == "v6.run.close_requested" for e in facts) == 1, "Expected one adopted close")
        require(sum(e["kind"] == "v621.material.assigned" for e in facts) == 1, "Specimen assignment changed")
        turns = [dict(r) for r in db.execute("""SELECT thread_id,turn_id,event_id,reservation_ref,budget_status,
            total_tokens FROM p0_meter_native_turns WHERE run_id=? ORDER BY sequence""", (rid,))]
    with closing(readonly(work / "funding.sqlite")) as db:
        requests = []
        for record in db.execute("SELECT * FROM requests WHERE slot=? ORDER BY rowid", (SLOT,)):
            row = dict(record)
            require(row["state"] == "SETTLED" and row["actual"] is not None, "Missing physical settlement")
            row["wire_sha256"] = hashlib.sha256(row.pop("wire")).hexdigest()
            requests.append(row)
        require(requests and len({r["request_id"] for r in requests}) == len(requests)
                and len({r["provider_receipt_id"] for r in requests}) == len(requests),
                "Missing or duplicated physical request/response IDs")
    hashes = {path: digest(work / path) for path in (
        "registration.json", "source-pins.json", "01/first-attempt.json", "01/assignment-request.json",
        "01/assignment.json", "01/raw/physical-http.jsonl", "01/accounting.json")}
    return {"run_id": rid, "status": goals[0]["status"], "enrollment": read(slot / "first-attempt.json"),
            "hashes": hashes, "jobs": jobs, "native_actions": actions, "native_turns": turns,
            "events": facts, "requests": requests, "physical_tokens": sum(r["actual"] for r in requests),
            "science_state_sha256": pilot.rehearse.state_hash(slot, rid)}


def cut(work, resume):
    real_save = pilot.save
    terminal_path = work / SLOT / "summary.json"

    def terminate(path, body):
        if path == terminal_path and body.get("status") == "CLOSED":
            require(body["host"] == "offline" and body["paid_model_calls"] == 0, "Unexpected host or charging")
            real_save(work.parent / "cutpoint.json", {"before_write": str(path), "signal": "SIGKILL",
                      "run_id": body["run_id"], "cold_state_sha256": body["cold_state_sha256"],
                      "pid": os.getpid(), "status": body["status"]})
            os.kill(os.getpid(), signal.SIGKILL)
        real_save(path, body)

    with patch.object(pilot, "save", terminate):
        outcome = pilot.next_slot(work, SLOT, None, None, resume)
    raise RuntimeError(f"Missing intended terminal crash site: {outcome}")


def pause_running(work, directory, runner, options, commands):
    """Stop the real controller once a physical request exists. Resume keeps this enrollment and ledger."""
    command = [*runner, "run", *options, "--slot", SLOT]
    stop = [*runner, "stop", *options, "--slot", SLOT]
    with (directory / "run-to-pause.log").open("w") as output, subprocess.Popen(
            command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT) as process:
        deadline = time.monotonic() + 120
        try:
            while not (work / SLOT / "raw" / "physical-http.jsonl").is_file():
                require(process.poll() is None and time.monotonic() < deadline,
                        "First original provider request was never observed")
                time.sleep(.01)
            with (directory / "stop.log").open("w") as log:
                success = subprocess.run(stop, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=15)
            require(success.returncode == 0, "Normal operator stop did not create the pause request")
            require(process.wait(timeout=120) == 0, "Normal pilot run failed while pausing; see run-to-pause.log")
        finally:
            if process.poll() is None:
                process.terminate()
    commands += [{"argv": command, "exit": process.returncode, "log": "run-to-pause.log"},
                 {"argv": stop, "exit": success.returncode, "log": "stop.log"}]
    paused = read(work / "result.json")["cases"][SLOT]
    require(paused["status"] == "PAUSED", f"Run did not reach PAUSED: {paused}")
    first = read(work / SLOT / "first-attempt.json")
    with closing(readonly(work / "funding.sqlite")) as db:
        requests = [dict(r) for r in db.execute("""SELECT request_id,state,actual,payload_sha256
            FROM requests WHERE slot=? ORDER BY rowid""", (SLOT,))]
    require(requests, "No original requests at pause")
    saved = {"run_id": paused["run_id"], "enrollment": first, "requests": requests,
             "stop_request": read(work / SLOT / "stop-request.json")}
    save(directory / "pause.json", saved)
    return saved


def run(directory, exercise_pause):
    if directory.exists() or directory.is_relative_to(ROOT):
        raise ValueError("Use a fresh directory outside the source tree")
    directory.mkdir(parents=True, mode=0o700)
    work = directory / "pilot"
    runner = [sys.executable, "-B", str(pilot.HERE / "pilot.py")]
    options = ["--work", str(work), "--host", "offline"]
    commands = []

    def launch(command, log, expected=0):
        with (directory / log).open("w") as output:
            process = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=600)
        commands.append({"argv": command, "exit": process.returncode, "log": log})
        require(process.returncode == expected, f"Unexpected exit for {log}: {process.returncode}")

    launch([*runner, "register", *options], "register.log")
    pause = pause_running(work, directory, runner, options, commands) if exercise_pause else None
    options_cut = ["--resume-cut"] if pause else []
    launch([sys.executable, "-B", str(Path(__file__).resolve()), "--cut", "--work", str(work), *options_cut],
           "kill.log", -signal.SIGKILL)
    point = read(directory / "cutpoint.json")
    summary = work / SLOT / "summary.json"
    require(not summary.exists() or (pause and read(summary)["status"] == "PAUSED"),
            "The terminal summary was already acknowledged")
    cases = read(work / "result.json")["cases"]
    require(cases[SLOT]["status"] == "ACTIVE", "The first slot was acknowledged before the crash")
    before = snapshot(work)
    if pause:
        require(pause["run_id"] == before["run_id"] and pause["enrollment"] == before["enrollment"],
                "Operator resume replaced its original Run or deadline")
        persisted = {r["request_id"]: r for r in before["requests"]}
        for request in pause["requests"]:
            continued = persisted[request["request_id"]]
            require(request["payload_sha256"] == continued["payload_sha256"] and
                    (request["state"] != "SETTLED" or request["actual"] == continued["actual"]),
                    "Operator resume changed an earlier physical request/settlement")
    save(directory / "before.json", before)
    require((before["run_id"], before["science_state_sha256"]) ==
            (point["run_id"], point["cold_state_sha256"]), "Crash does not describe the original durable Run")
    launch([*runner, "resume", *options, "--slot", SLOT], "resume.log")
    after = snapshot(work)
    save(directory / "after.json", after)
    require(before == after, "Cold resume changed the original registered work, actions, receipts or charges")
    active_cases = read(work / "result.json")["cases"]
    require(active_cases[SLOT]["status"] == "CLOSED" and active_cases[SLOT]["run_id"] == after["run_id"]
            and all(active_cases[s]["status"] == "REGISTERED" for s in ("02", "03", "04")),
            "Original assignment was lost or a later slot started during recovery")
    require(read(work / SLOT / "summary.json")["cold_state_sha256"] == after["science_state_sha256"],
            "Acknowledged summary does not describe the original recovered state")
    result = {"schema": "gx.v623.pilot-cold-recovery/1", "status": "PASS", "diagnostic": True,
              "classification": "OFFICIAL_CODEX_LOOPBACK", "paid_provider_calls": 0,
              "source_commit": read(work / "registration.json")["source_commit"],
              "diagnostic_slot": SLOT, "cutpoint": point, "run_id": after["run_id"],
              "operator_pause_resumed": bool(pause),
              "original_deadline": after["enrollment"]["deadline"], "completion_status": "CLOSED",
              "original_jobs": [job["job_id"] for job in after["jobs"]],
              "native_decisions": len(after["native_actions"]), "physical_requests": len(after["requests"]),
              "physical_tokens": after["physical_tokens"], "duplicate_jobs": 0, "duplicate_requests": 0,
              "duplicate_charges": 0, "unresolved_requests": 0,
              "before_sha256": digest(directory / "before.json"), "after_sha256": digest(directory / "after.json"),
              "commands": commands}
    save(directory / "result.json", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--work", type=Path, required=True)
    args.add_argument("--pause-first", action="store_true", help="Exercise operator stop and resume before the kill")
    args.add_argument("--resume-cut", action="store_true", help=argparse.SUPPRESS)
    args.add_argument("--cut", action="store_true", help=argparse.SUPPRESS)
    values = args.parse_args()
    if values.cut:
        cut(values.work.resolve(), values.resume_cut)
    else:
        run(values.work.resolve(), values.pause_first)
