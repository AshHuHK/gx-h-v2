"""Admit each shipped Goal through GX and check the original's compressed or plain archive pin before driving it."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[2]


def verify(case, private):
    public = HERE / "studies" / case
    state = private / case
    command = [str(PRODUCT / "bin/gx-p0"), "research", "--state", str(state), "--run-key", "archive-source-admission",
               "--goal-file", str(public / "question.md"), "--goal-config", str(public / "goal.json"), "--timeout", "0.000001"]
    launch = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE":"1"})
    if launch.returncode != 75:
        raise RuntimeError(launch.stderr or launch.stdout)
    invocation = json.loads(launch.stdout)
    assert invocation["driver"] == "time_limit" and invocation["goal_status"] == "OPEN"
    run = invocation["run_id"]
    config = json.loads((public / "goal.json").read_bytes())
    expected = {i["sha256"]: json.loads((public / i["acquisition"]).read_bytes())
                for i in config["input_files"] if "acquisition" in i}
    with closing(sqlite3.connect((state/"gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        events = [(eid, json.loads(encoded)) for eid, encoded in db.execute(
            "SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v6.observation.added' ORDER BY event_id",
            (run,))]
        jobs = db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0]
    assert len(events) == len(expected) and {event["sha256"] for _,event in events} == set(expected)
    assert invocation["invocation"]["after"]["host_turns"] == jobs == 0
    receipts = []
    for eid, event in events:
        pin = expected[event["sha256"]]
        acquisition = event["acquisition"]
        identity = acquisition["source_identity"]
        assert identity["status"] == "matched_accession" and identity["revision"]["status"] == "verified"
        assert identity["artifact_sha256"] == event["sha256"] == pin["sha256"]
        assert pin["source"] == event["source"] and acquisition["dependencies"]["publication"] == pin["publication"]
        assert {"sample", "sampling"} <= set(acquisition["unknown_dependencies"])
        receipts.append({"event":eid, "artifact_ref":event["artifact_ref"], "source":pin["source"],
                         "publication":pin["publication"], "identity":identity,
                         "unknown_dependencies":acquisition["unknown_dependencies"]})
    return {"case":case, "run_id":run, "receipts":receipts, "scientific_jobs":jobs, "host_turns":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private", type=Path, help="Private working directory outside the checkout")
    args = parser.parse_args()
    result = [verify(case,args.private.resolve()) for case in ("linear-peptides","macrocycle","fab-first","fab-successor")]
    print(json.dumps(result, ensure_ascii=False, indent=2))
