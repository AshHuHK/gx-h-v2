"""Kill the local cohort operator after durable GX start but before its receipt, then cold-resume."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from acceptance.science_execution import diagnostic as diag
from acceptance.v622.public.enrollment import read
from acceptance.v62c import rehearse
from contextlib import closing


def interrupt(work):
    original=diag.native.native_cli
    def crashpoint(*args):
        cli=original(*args)
        def call(verb,*values,**options):
            response=cli(verb,*values,**options)
            if verb=="start":
                os._exit(42)
            return response
        return call
    with patch.object(diag.native,"native_cli",crashpoint):
        diag.main(["run","--host","offline","--work",str(work),"--slot","01"])


def check(work):
    exe=[sys.executable,"-B",str(ROOT/"acceptance/science_execution/diagnostic.py")]
    def cli(verb,*args):
        return json.loads(subprocess.check_output([*exe,verb,"--host","offline","--work",str(work),*args],text=True))
    cli("register")
    child=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--interrupt","--work",str(work)])
    if child.returncode!=42:
        raise ValueError("The cold control did not reach the post-start crashpoint")
    slot=work/"01"
    first=read(slot/"first-attempt.json")
    with closing(rehearse.db_open(slot)) as db:
        before=[r[0] for r in db.execute("SELECT run_id FROM runs")]
        before_turns=db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0]
    if len(before)!=1 or first.get("run_id") or before_turns or cli("status","--slot","01")["status"]!="ACTIVE":
        raise ValueError("Expected the original GX Run with no operator receipt or native turn yet")
    recovered=cli("resume","--slot","01")
    final=read(slot/"first-attempt.json")
    if (before!=[recovered["run_id"]] or final!={**first,"run_id":before[0]} or recovered["status"]!="CLOSED"
            or recovered["accounting"]!="SETTLED"):
        raise ValueError("Recovery did not close the original enrolled Run on its original clock")
    with closing(rehearse.db_open(slot)) as db:
        after=[r[0] for r in db.execute("SELECT run_id FROM runs")]
        assignments=db.execute("SELECT count(*) FROM events WHERE kind='v621.material.assigned'").fetchone()[0]
        jobs=[r[0] for r in db.execute("SELECT status FROM jobs ORDER BY rowid")]
    if before!=after or assignments!=1 or jobs!=["SUCCEEDED","SUCCEEDED"]:
        raise ValueError("Cold resume added a Run, assignment or duplicate experiment")
    return {"status":"PASS","cut":"GX start committed before operator saved its Run ID","original_run_id":before[0],
            "first_attempt_clock":first["research_window"],"original_native_deliveries_at_crash":before_turns,
            "recovered_case":recovered,"assignments":assignments,"job_states":jobs,"paid_model_calls":0}


def check_expired(work):
    cli=[sys.executable,"-B",str(ROOT/"acceptance/science_execution/diagnostic.py")]
    subprocess.run([*cli,"register","--work",str(work)],check=True,stdout=subprocess.DEVNULL)
    killed=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--interrupt","--work",str(work)])
    if killed.returncode!=42:
        raise ValueError("Expiry replay missed its registered crashpoint")
    slot=work/"01"
    window=read(slot/"first-attempt.json")["research_window"]
    def original():
        with closing(rehearse.db_open(slot)) as db:
            return dict(db.execute("SELECT run_id,status FROM runs").fetchone())
    before=original()
    with patch.object(diag.time,"time",return_value=window["drain_until"]+1):
        audit=diag.operate(work,"01","reconcile","offline",rehearse.BINARY,None)
        if original()!=before:
            raise ValueError("Receipt-only recovery changed the active Run")
        recovered=diag.operate(work,"01","resume","offline",rehearse.BINARY,None)
        repeated=diag.operate(work,"01","resume","offline",rehearse.BINARY,None)
    after=original()
    receipts=list(slot.glob("stop-*.json"))
    if (after!={"run_id":before["run_id"],"status":"PAUSED"} or len(receipts)!=1 or recovered!=repeated
            or read(receipts[0])["original_deliveries"] or read(slot/"first-attempt.json")["research_window"]!=window
            or recovered["scientific_status"]!="TIMEOUT" or recovered["total_tokens"] is not None):
        raise ValueError("Expired recovery restarted work, erased unknowns or repeated the stop")
    return {"status":"PASS","clock":"outer operator advanced to drain_until + 1 s after real process death",
            "original_run":before,"after_read_only_reconcile":before,"after_original_resume":after,
            "receipt_only":audit,"bounded_stop":read(receipts[0]),"original_attempts":1,"paid_model_calls":0}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--interrupt",action="store_true")
    parser.add_argument("--expired",action="store_true")
    args=parser.parse_args()
    if args.interrupt:
        interrupt(args.work.resolve())
    else:
        print(json.dumps((check_expired if args.expired else check)(args.work.resolve()),indent=2))
