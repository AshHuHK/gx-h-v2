"""Run the preregistered archival delivery through official GX processes and recover the same state."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[3]
LIVE = PRODUCT.parent / "harness-refactor-live"
WORK = LIVE / "late-water"
RELEASE = LIVE / "hydro/release"
STATE = WORK / "state"
MSG = "filing-nwis-april-second-station"
REGISTRATION = HERE / "preregistered.json"
REG = json.loads(REGISTRATION.read_text())
sys.path.insert(0, str(PRODUCT))
from acceptance.v623.oauth import host


def now():
    return datetime.now(timezone.utc).isoformat()


def save(name, value):
    (WORK/(name+".json")).write_text(json.dumps(value, indent=2, ensure_ascii=False)+"\n")


def rows(db, query, *values):
    return [dict(x) for x in db.execute(query, values)]


def snapshot():
    database = STATE/"gx.db"
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=3) as db:
            db.row_factory = sqlite3.Row
            goals = rows(db,"SELECT run_id,goal_version,status FROM v6_goals")
            if not goals:
                return None
            m = rows(db,"SELECT module_instance_id,branch_id,scope,focus,expected_delivery,status FROM v62_modules ORDER BY rowid")
            for module in m:
                module["jobs"] = [j[0] for j in db.execute("SELECT job_id FROM v6_nodes WHERE branch_id=? AND job_id IS NOT NULL",(module["branch_id"],))]
            return {"stamp":now(),"goal":goals[0],"modules":m,
                "jobs":rows(db,"SELECT node_id,version,branch_id,operation_key,job_id,result_ref,status FROM v6_nodes ORDER BY rowid"),
                "decisions":rows(db,"SELECT d.decision_ref,t.scope,json_extract(d.request_json,'$.decision.rationale') rationale FROM v6_decisions d JOIN v6_decision_tasks t ON t.task_id=d.task_id ORDER BY d.rowid"),
                "messages":rows(db,"SELECT message_id,result_json,rejected FROM p0_operator_messages WHERE message_id=?",MSG),
                "messages_from_workers":rows(db,"SELECT event_id,kind,subject_id FROM events WHERE kind IN ('v62.module.report','research.topic.message','research.hint.sent','v62.module.reported') ORDER BY event_id"),
                "event":db.execute("SELECT coalesce(max(event_id),0) FROM events").fetchone()[0]}
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "database is locked" in str(exc):
            return None
        raise


def invoke(who, command, *, wait=False):
    stdout = (WORK/(who+".stdout")).open("a")
    stderr = (WORK/(who+".stderr")).open("a")
    try:
        proc = subprocess.Popen([*CALL,*map(str,command)],stdout=stdout,stderr=stderr)
    finally:
        stdout.close()
        stderr.close()
    if wait:
        return proc.wait(timeout=45)
    save(who+"-process", {"pid":proc.pid,"argv":list(map(str,command)),"stamp":now()})
    return proc


def main():
    WORK.mkdir(parents=True,exist_ok=True,mode=0o700)
    existing = snapshot() if (STATE/"gx.db").exists() else None
    if existing and ((WORK/"queued.json").exists() or existing["messages"]):
        raise ValueError("The second station was already filed; use the persisted Run ID to resume.")
    for name, expected in REG["main_inputs"].items():
        assert hashlib.sha256((HERE/name).read_bytes()).hexdigest()==expected
    assert json.loads((RELEASE/"field-deployment.json").read_text())==REG["deployment"]
    for port in (7890,7891):
        with socket.create_connection(("127.0.0.1",port),timeout=3):
            pass
    if existing:
        old = json.loads((WORK/"first-driver-process.json").read_text())
        if (Path("/proc")/str(old["pid"])/"cmdline").is_file():
            raise ValueError("The existing first driver is still alive.")
        save("observer-recovery", {"stamp":now(),"first_driver":old["pid"],"before":existing,
             "reason":"Read-only observer requested an absent status column and exited. Original driver no longer present; resume original Run with the corrected observer."})
        driver = invoke("continued-first-driver", ["resume","--state",STATE,"--run-id",existing["goal"]["run_id"],"--timeout","1200"])
        elapsed = max(0, (datetime.now(timezone.utc)-datetime.fromisoformat(old["stamp"])).total_seconds())
    else:
        save("entry", {"stamp":now(),"protocol_sha256":hashlib.sha256(REGISTRATION.read_bytes()).hexdigest(),
                       "binary_sha256":PIN,"deployment":REG["deployment"],"model":"gpt-6.2-astra","effort":"max"})
        driver = invoke("first-driver",["research","--state",STATE,"--run-key",REG["run_key"],
            "--goal-file",HERE/"question.md","--goal-config",HERE/"goal.json","--timeout","1200"])
        elapsed = 0
    start = time.monotonic() - elapsed
    first_open = None
    while True:
        view = snapshot()
        if not view:
            if driver.poll() is not None:
                raise RuntimeError("Driver stopped before admission; inspect private first-driver stderr")
            time.sleep(1)
            continue
        if view["goal"]["status"] != "OPEN":
            save("before-early-close",view)
            return
        current = [m for m in view["modules"] if m["status"]=="OPEN"]
        if current and first_open is None:
            first_open=time.monotonic()
        early = bool(view["decisions"] or view["messages_from_workers"])
        if current and (early or time.monotonic()-first_open>=150) or time.monotonic()-start>=330:
            break
        if driver.poll() is not None:
            raise RuntimeError("Driver exited before filing the second material")
        time.sleep(1)
    rank = lambda m: sum(t in (m["focus"]+" "+m["expected_delivery"]).lower() for t in (
                          "source","data","archive","provenance","metadata","qualifier","coverage"))
    recipient = max(current,key=rank) if current else None
    save("pre-crash",dict(view,selected_recipient=recipient))
    driver.kill()
    code = driver.wait(timeout=15)
    save("disconnect",{"old_driver":driver.pid,"exit":code,"stamp":now(),"reason":"preregistered GX-only crash"})
    run = view["goal"]["run_id"]
    cmd = ["message","--state",STATE,"--run-id",run,"--message-id",MSG,"--text",(HERE/"message.txt").read_text().strip(),
           "--attachment",HERE/"later-station.json","--queue-only"]
    if recipient:
        cmd += ["--branch-id",recipient["branch_id"]]
    rc = invoke("queue",cmd,wait=True)
    if rc:
        raise RuntimeError(f"Public message delivery failed with exit {rc}; resume by reissuing the same message ID")
    save("queued",snapshot())
    resumed = invoke("resumed-driver",["resume","--state",STATE,"--run-id",run,"--timeout","1200"])
    resumed.wait()
    save("after-resume",dict(snapshot(),driver_exit=resumed.returncode))


if __name__ == "__main__":
    official = PRODUCT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    binary, PIN = host._pin(os.environ.get("GX_P0_CODEX") or official)
    CALL = [sys.executable,"-B",str(PRODUCT/"acceptance/science_assets/deployment/start_native.py"),
            str(RELEASE),str(binary),str(WORK)]
    main()
