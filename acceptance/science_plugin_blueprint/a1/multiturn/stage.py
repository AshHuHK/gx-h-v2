"""Deliver one original update at a time through the public GX CLI after the previous Lead decision."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from operate import AUTH_HELPER, BINARY


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cli(work, *args):
    release = work/"release"
    env = dict(os.environ, GX_P0_CODEX=str(release/BINARY))
    cmd = [sys.executable,str(release/AUTH_HELPER),str(work/"official"), *map(str,args)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout)[-3000:])
    return json.loads(proc.stdout)


def wait_for_response(work, phase, event=0):
    dbpath = work/"state"/"gx.db"
    while True:
        if dbpath.exists():
            with closing(sqlite3.connect(dbpath.as_uri()+"?mode=ro", uri=True)) as db:
                db.row_factory=sqlite3.Row
                goal=db.execute("SELECT run_id,status FROM v6_goals").fetchone()
                if goal and goal["status"] != "OPEN":
                    raise RuntimeError("Goal status before "+phase+" update: "+goal["status"])
                if not goal:
                    time.sleep(2)
                    continue
                # The decision uses the exact frozen context of the query. Multiple updates may have
                # reached the same context; preserve the full trigger list and its actual cursor.
                rows=db.execute("""SELECT e.event_id survey_event,e.payload_json,d.decision_ref,
                        json_extract(d.receipt_json,'$.event_id') decision_event
                        FROM events e JOIN v6_decisions d ON d.run_id=e.run_id
                        AND d.context_ref=json_extract(e.payload_json,'$.context_ref')
                        WHERE e.kind='research.catalog.surveyed' AND e.run_id=?
                        ORDER BY e.event_id""",(goal["run_id"],)).fetchall()
                for row in rows:
                    body=json.loads(row["payload_json"])
                    if phase=="original" or event in body["trigger_events"]:
                        return goal["run_id"], {"survey":row["survey_event"],"survey_context":body,
                                  "decision_ref":row["decision_ref"],"decision_event":row["decision_event"]}
        time.sleep(2)


def persist(work, name, doc):
    dest=work/(name+".json")
    tmp=dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n")
    tmp.replace(dest)


def main(work):
    if (work/"dispatch_order.json").is_file():
        return
    gates={}
    rid,gates["original"]=wait_for_response(work,"original")
    shared=["--state",work/"state","--run-id",rid]
    result=work/"update_1.json"
    if not result.exists():
        raw=HERE/"second.json"
        resp=cli(work,"assign-material",*shared,"--after-decision",gates["original"]["decision_ref"],
                 "--source","simulated-pond-trial-7","--input",raw,"--sha256",hashlib.sha256(raw.read_bytes()).hexdigest(),
                 "--role","record","--acquisition",HERE/"second-origin.json","--media-type","application/json")
        persist(work,"update_1",dict(utc=stamp(),method="assign-material",receipt=resp))
    update=json.loads(result.read_text())["receipt"]["event_id"]
    _,gates["new_logger"]=wait_for_response(work,"new logger",update)
    result=work/"update_2.json"
    if not result.exists():
        text=(HERE/"pi-update.txt").read_text().strip()
        resp=cli(work,"message",*shared,"--text",text,"--message-id","pond-dispatch-rule",
                 "--reply-to",gates["new_logger"]["decision_ref"],"--queue-only")
        persist(work,"update_2",dict(utc=stamp(),method="message",text=text,receipt=resp))
    while True:
        with closing(sqlite3.connect((work/"state"/"gx.db").as_uri()+"?mode=ro",uri=True)) as db:
            event=db.execute("""SELECT event_id FROM events WHERE kind IN ('research.pi.feedback','research.pi.answered',
                        'v6.intent.changed') AND event_id>? ORDER BY event_id LIMIT 1""",
                         (gates["new_logger"]["decision_event"],)).fetchone()
            if event: break
            if db.execute("SELECT status FROM v6_goals WHERE run_id=?",(rid,)).fetchone()[0]!="OPEN":
                raise RuntimeError("Goal closed while awaiting PI feedback")
        time.sleep(2)
    _,gates["pi_rule"]=wait_for_response(work,"PI rule",event[0])
    result=work/"update_3.json"
    control=["observe",*shared,"--source","simulated-pond-custody-register-v1","--scope","run",
             "--input",HERE/"custody.json","--media-type","application/json","--acquisition",HERE/"custody-origin.json"]
    if not result.exists():
        resp=cli(work,*control)
        persist(work,"update_3",dict(utc=stamp(),method="observe",receipt=resp))
    if not (work/"repeat_observation.json").exists():
        original=json.loads(result.read_text())["receipt"]
        second=cli(work,*control)
        persist(work,"repeat_observation",{"utc":stamp(),"same_event":second["event_id"]==original["event_id"],
                "same_source":second["artifact_ref"]==original["artifact_ref"],"event_id":second["event_id"],
                "artifact_ref":second["artifact_ref"]})
    persist(work,"dispatch_order",gates)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study",type=Path,help="The multiturn case directory")
    main(parser.parse_args().study.resolve())
