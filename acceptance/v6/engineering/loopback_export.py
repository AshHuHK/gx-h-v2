"""Exercise the raw exporter on an official host with the existing local scripted peer."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from contextlib import closing
from unittest.mock import patch

import official

V6 = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(V6))
import export
from live import invocation, save
from project_window import make_bundle


def run(work, binary):
    work.mkdir(parents=True, exist_ok=False)
    run_out, observations = work / "runner", work / "observer"
    run_out.mkdir()
    observations.mkdir()
    db = work / "state/gx.db"
    original = official.scripted
    def sampled(brief):
        if brief["observations"]:
            export.capture(db, brief["run_id"], observations / "before")
        return original(brief)
    with patch.object(official,"scripted", sampled):
        proof = official.run(work,run_out,binary.resolve())
    save(run_out / "result.json",proof)
    run = proof["run_id"]
    with closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as conn:
        decision, thread = conn.execute("SELECT decision_ref, thread_id FROM v6_decisions WHERE run_id=? "
                                        "ORDER BY rowid DESC LIMIT 1",(run,)).fetchone()
    export.capture(db, run, observations / "settled")
    invocation({**os.environ, "HOME":str(work), "GX_CODEX_OFFLINE":"1"},
               ["resume","--state",db.parent,"--run-id",run,"--timeout","2"],45, observations/"resume.json")
    export.capture(db, run, observations / "reopened")
    rollout, = (work/"codex/sessions").rglob("*"+thread+"*.jsonl")
    save(observations/"window.json",{"before":"before","settled":"settled","reopened":"reopened",
         "decision_ref":decision,"rollout":str(rollout),"resume_receipt":"resume.json"})
    result = export.publish_dir(work/"bundle", lambda dest:make_bundle(dest,"observed",[observations/"window.json"]))
    save(work/"result.json",{"evidence_classes":["SCRIPTED","OFFICIAL_HOST_LOOPBACK","REAL_COMPUTE"],
                             **result})
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--binary",type=Path,required=True)
    args = ap.parse_args()
    print(json.dumps(run(args.out.resolve(),args.binary)))
