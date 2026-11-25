"""Crash immediately before the first formal batch acknowledgment, then resume all original slots."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from acceptance.v622.public import batch

FIRST = "01"
SUITE = "controls"


def require(value, message):
    if not value:
        raise AssertionError(message)


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def kill_at_acknowledgment(work, binary):
    real_save = batch.save

    def intervene(path, summary):
        if path == work / FIRST / "summary.json":
            # The normal slot code has finished Job settlement and its terminal GX replay.
            require(summary["status"] == "CLOSED" and summary["paid_model_calls"] == 0, "Unexpected crash site")
            real_save(work.parent / "cutpoint.json", {"path":str(path),"run_id":summary["run_id"],
                      "cold_state_sha256":summary["cold_state_sha256"],"goal_status":summary["status"],
                      "pid":os.getpid(),"signal":"SIGKILL"})
            os.kill(os.getpid(), signal.SIGKILL)
        real_save(path, summary)

    with patch.object(batch, "save", intervene):
        batch.process(work, SUITE, binary)
    raise AssertionError("The native batch failed to reach the first summary acknowledgment")


def first_slot(work):
    """Read from the GX and batch owners; reconstruction does not require the missing summary."""
    slot = work / FIRST
    with closing(readonly(slot / "state/gx.db")) as db:
        goals = db.execute("SELECT run_id,status FROM v6_goals ORDER BY rowid").fetchall()
        require(len(goals) == 1 and goals[0]["status"] == "CLOSED", "The interrupted first Goal is not closed")
        rid = goals[0]["run_id"]
        jobs = [dict(row) for row in db.execute(
            "SELECT job_id,status,reservation_ref,measured_json FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))]
        require(jobs and all(j["status"] == "SUCCEEDED" for j in jobs), "Missing terminal scientific Job evidence")
        assigned = db.execute("SELECT event_id,subject_id FROM events WHERE run_id=? AND kind='v621.material.assigned'",
                              (rid,)).fetchall()
        require(len(assigned)==1, "The original material assignment must occur exactly once")
    with closing(readonly(work / "funding.sqlite")) as db:
        requests = [dict(r) for r in db.execute("SELECT * FROM requests WHERE slot=? ORDER BY rowid",(FIRST,))]
    require(requests and len({r["request_id"] for r in requests}) == len(requests), "Missing or repeated HTTP IDs")
    require(all(r["state"] == "SETTLED" and r["actual"] is not None for r in requests), "Missing settled request")
    receipt = read(slot / "assignment.json")
    require(receipt["run_id"] == rid and receipt["event_id"] == assigned[0]["event_id"] and
            receipt["artifact_ref"] == assigned[0]["subject_id"], "Lost assignment linkage")
    return {"run_id":rid, "first_attempt":read(slot/"first-attempt.json"),"goal_status":goals[0]["status"],
            "jobs":jobs, "requests":requests,"settled_tokens":sum(r["actual"] for r in requests),
            "held_tokens":sum(r["maximum"] for r in requests if r["actual"] is None),
            "registration_sha256":file_sha(work/"registration.json"), "assignment":receipt,
            "assignment_request_sha256":file_sha(slot/"assignment-request.json"),
            "official_wire_sha256":file_sha(slot/"raw/wire.json"),
            "request_log_sha256":file_sha(slot/"raw/requests.jsonl"),"cold_state_sha256":batch.old.state_hash(slot,rid)}


def run(directory, binary):
    if directory.exists() or directory.is_relative_to(ROOT):
        raise ValueError("Provide a fresh result directory outside the candidate")
    directory.mkdir(mode=0o700, parents=True)
    work = directory / "run"
    crash = [sys.executable, "-B", str(Path(__file__).resolve()), "--inject", "--work", str(directory),
             "--binary", str(binary)]
    resume = [sys.executable, "-B", str(HERE/"public/batch.py"), "--work", str(work), "--suite", SUITE,
              "--host","offline","--binary",str(binary),"--resume"]
    with (directory/"kill.log").open("w") as log:
        crashed = subprocess.run(crash,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=600)
    require(crashed.returncode == -signal.SIGKILL, "Expected the batch process to die from SIGKILL; see kill.log")
    require(not (work/FIRST/"summary.json").exists(), "The first terminal summary was already acknowledged")
    require(read(work/"result.json")["cases"][FIRST]["status"] == "ACTIVE", "The batch already accepted the first result")
    cut = read(directory/"cutpoint.json")
    before = first_slot(work)
    require(cut["run_id"] == before["run_id"] and cut["cold_state_sha256"] == before["cold_state_sha256"],
            "Cutpoint does not describe the durable scientific result")
    batch.save(directory/"before.json",before)
    with (directory/"resume.log").open("w") as log:
        resumed = subprocess.run(resume,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=2100)
    require(resumed.returncode==0, "Cold resume failed; see resume.log")
    after = first_slot(work)
    batch.save(directory/"after.json",after)
    require(before==after,"First-slot scientific, funding, HTTP or assignment history changed on recovery")
    result = read(work/"result.json")
    registered = read(work/"registration.json")["slots"]
    ids = {}
    for expected in registered:
        slot = expected["slot"]
        row=result["cases"][slot]
        require(row["status"]=="CLOSED" and row["paid_model_calls"]==0, "A registered slot did not finish offline")
        with closing(readonly(work/slot/"state/gx.db")) as db:
            original=[r[0] for r in db.execute("SELECT run_id FROM runs")]
        require(original==[row["run_id"]], f"Replacement or unregistered Run in slot {slot}")
        require(read(work/slot/"first-attempt.json")["slot"]==slot, "Run enrollment path changed")
        ids[slot]=original[0]
    require(list(ids)==["01","02","03","04"] and len(set(ids.values()))==4 and ids[FIRST]==before["run_id"],
            "The registered cohort changed after resume")
    funding=read(work/"funding.json")
    require(funding["paid_provider_calls"]==0 and funding["slots"][FIRST]["held_tokens"]==after["held_tokens"] and
            funding["slots"][FIRST]["settled_tokens"]==after["settled_tokens"],"Recovered funding summary changed")
    record={"schema":"gx.v622.batch-recovery/1","status":"PASS","source_commit":read(work/"registration.json")["source_commit"],
            "cut":cut,"first_summary_acknowledged_at_kill":False,"same_first_slot_history":True,
            "before_sha256":file_sha(directory/"before.json"),"after_sha256":file_sha(directory/"after.json"),
            "registration_sha256":before["registration_sha256"],"case_runs":ids,
            "first_job_ids":[j["job_id"] for j in after["jobs"]],"first_requests":len(after["requests"]),
            "first_settled_tokens":after["settled_tokens"],"first_held_tokens":after["held_tokens"],
            "additional_first_jobs":0,"additional_first_requests":0,"additional_first_token_charges":0,
            "paid_provider_calls":0,"commands":[{"argv":crash,"exit":crashed.returncode},{"argv":resume,"exit":resumed.returncode}]}
    batch.save(directory/"result.json",record)
    print(json.dumps(record,sort_keys=True))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",required=True,type=Path)
    parser.add_argument("--binary",type=Path,default=batch.old.BINARY)
    parser.add_argument("--inject",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.inject:
        kill_at_acknowledgment(args.work.resolve()/"run",args.binary.resolve())
    else:
        run(args.work.resolve(),args.binary.resolve())
