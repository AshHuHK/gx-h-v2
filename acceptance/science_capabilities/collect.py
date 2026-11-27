"""Archive a closed native study and check that the same Run reexports without compute."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.p0.research_page import complete_decision
from gx_core.p0 import research_routes


def check(work, build, rid, destination):
    state = work / "state"
    src = state / "research" / rid
    first = json.loads((src / "result.json").read_bytes())
    report_hash = hashlib.sha256((src / "REPORT.md").read_bytes()).hexdigest()
    argv = [sys.executable, "-B", str(build/"acceptance/science_capabilities/gx_oauth.py"),
            str(work), "research", "--state", str(state), "--run-id", rid]
    export = json.loads(subprocess.run(argv,check=True,capture_output=True,text=True).stdout)
    if export["invocation"]["action"] != "export_closed" or any(export["invocation"]["added"].values()):
        raise RuntimeError("A closed Run requested new work")
    result = json.loads((src / "result.json").read_bytes())
    if result["final_decision"] != first["final_decision"] or hashlib.sha256((src/"REPORT.md").read_bytes()).hexdigest()!=report_hash:
        raise RuntimeError("Closed report or decision changed during reexport")
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as c:
        c.row_factory = sqlite3.Row
        turns = [dict(r) for r in c.execute("""SELECT DISTINCT r.role,r.provider,r.model,r.effort,r.thread_id,d.turn_id
            FROM p0_root_deliveries d JOIN p0_roots r ON r.run_id=d.run_id AND r.thread_id=d.thread_id
            WHERE d.run_id=? AND d.state='acknowledged' AND d.method='turn/start' AND d.turn_id IS NOT NULL
            ORDER BY d.event_id""",(rid,))]
        spans = {}
        for root in c.execute("SELECT * FROM p0_roots WHERE run_id=?", (rid,)):
            for span in research_routes._turns(c,rid,state,root):
                spans[(span["thread_id"],span["turn_id"])] = {k:span[k] for k in ("classification","start","end")}
        for turn in turns:
            turn.update(spans.get((turn["thread_id"],turn["turn_id"]), {}))
        decision = result["final_decision"]
        if not any(t.get("classification")=="NATIVE_MODEL" and (t["thread_id"],t["turn_id"]) == (decision["thread_id"],decision["turn_id"]) for t in turns):
            raise RuntimeError("No official acknowledged turn for the closing decision")
        reads = [dict(r) for r in c.execute("""SELECT event_id,kind,subject_id AS ref,ts FROM events
            WHERE run_id=? AND kind IN ('v62.module.tool_completed','v62.source.extracted','v62.source.reviewed',
              'v621.material.assigned','v62.module.reported','v62.module.report_released','v6.recipe.saved')
            ORDER BY event_id""",(rid,))]
    destination.mkdir(exist_ok=True,parents=True)
    shutil.copyfile(src/"REPORT.md",destination/"REPORT.md")
    (destination/"DECISION.md").write_text(complete_decision(result),encoding="utf-8")
    # The normal command contains deployment-local file paths. Evidence and original report keep their identity.
    result["invocation"]={k:v for k,v in export["invocation"].items() if k!="command"}
    (destination/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    commit = subprocess.check_output(["git","rev-parse","HEAD"],cwd=build,text=True).strip()
    ledger=dict(run_id=rid,release=commit,report_sha256=report_hash,turns=turns,source_events=reads,
                decision_ref=decision["decision_ref"],commit_event=result["closure"]["committed_event"],
                close_event=result["closure"]["closed_event"],reexport_added=export["invocation"]["added"])
    (destination/"receipt.json").write_text(json.dumps(ledger,ensure_ascii=False,indent=2)+"\n")
    print(rid, len(turns),"official turns",len(result["jobs"]),"Jobs",ledger["close_event"])


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("work","build","destination"):
        parser.add_argument(name,type=Path)
    parser.add_argument("run_id")
    args=parser.parse_args()
    check(args.work.resolve(),args.build.resolve(),args.run_id,args.destination.resolve())
