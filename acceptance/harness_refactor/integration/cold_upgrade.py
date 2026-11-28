"""Reopen a baseline-authored, closed research Goal after an in-place software upgrade."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[3]
BASE = "50d31f5792a178529a817ffd93f66ef9cbbb0be4"
SCOPE = {"capabilities": [], "capability_options": [], "research_modules": [],
         "input_files": [{"role":"material", "path":"reading.txt"}]}
QUESTION = "What can be established about this river reading's date?"


def close_old(folder):
    from acceptance.v622.test_close import SurveyedStudy, close_decision
    with SurveyedStudy(folder, reopen=True) as old:
        actor = old.next_actor("run")
        request = close_decision(old.materials)
        request["rationale"] = "The log supplies the month but leaves the day unspecified."
        old.call(actor, "gx_decide_revision", {"decision": request}, "archive")
        old.advance()


def check():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        app, work = folder / "release", folder / "project"
        app.mkdir()
        work.mkdir()
        with tempfile.TemporaryFile() as archive:
            subprocess.run(["git", "archive", BASE, "bin", "gx-h", "acceptance/v62a", "acceptance/v621", "acceptance/v622"], cwd=ROOT, stdout=archive, check=True)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as snapshot:
                snapshot.extractall(app, filter="data")
        env = {**os.environ, "GX_CODEX_OFFLINE":"1", "PYTHONDONTWRITEBYTECODE":"1",
               "PYTHONPATH":os.pathsep.join(map(str, (app/"gx-h",app/"gx-h/tests",app,app/"acceptance/v62a",ROOT)))}
        scope = work / "scope.json"
        scope.write_text(json.dumps(SCOPE))
        (work / "reading.txt").write_text("River record, April; day not provided.\n")
        command = [sys.executable,"-B",str(app/"bin/gx-p0"),"research","--state",str(work/"state"),
                   "--run-key","cold-archive","--goal",QUESTION,"--goal-config",str(scope),"--timeout","0.001"]
        def invoke(expected):
            run = subprocess.run(command,cwd=work,env=env,text=True,capture_output=True,timeout=45)
            if run.returncode != expected:
                raise RuntimeError(run.stderr or run.stdout)
            return json.loads(run.stdout)
        started = invoke(75)
        subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--close-old",str(work)],
                       cwd=work,env=env,check=True)
        original = invoke(0)
        def report(out):
            return hashlib.sha256(Path(out["report"]).read_bytes()).hexdigest()
        def counts():
            with closing(sqlite3.connect((work/"state/gx.db").as_uri()+"?mode=ro",uri=True)) as db:
                inputs=json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (started["run_id"],)).fetchone()[0])
                return (db.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (started["run_id"],)).fetchone()[0],
                        inputs["inputs"].get("harness_policy"), inputs["inputs"]["capability_catalog"])
        old_hash, old_counts = report(original), counts()
        for name in ("gx_core", "contracts", "config"):
            shutil.copytree(ROOT/"gx-h"/name, app/"gx-h"/name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        reopened = invoke(0)
        assert started["run_id"] == original["run_id"] == reopened["run_id"]
        assert old_hash == report(reopened)
        assert old_counts == counts() and old_counts[1:] == (None, [])
        assert reopened["invocation"]["added"] == {"host_turns":0, "jobs":0}
        return {"base":BASE, "run_id":started["run_id"], "phase":"CLOSED", "level":"SCRIPTED_CALLBACK",
                "same_report_sha256":old_hash,"same_run_events":old_counts[0],"old_harness_policy":None,
                "old_capability_catalog":[], "reopen_added":reopened["invocation"]["added"]}


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--close-old",type=Path)
    args=p.parse_args()
    if args.close_old:
        close_old(args.close_old)
    else:
        print(json.dumps(check(),indent=2))
