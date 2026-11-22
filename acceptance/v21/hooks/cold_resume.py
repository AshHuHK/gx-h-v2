"""Make a nonterminal v2.0 checkpoint with its original engine, continue it with v2.1, and compare both owners."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[2]
BASE = "2314eed"
PROBE = """
import json,sys
from pathlib import Path
from gx_core.orchestration import service
from gx_core.p0.driver import context
from gx_core.p0 import schema
from gx_core.persistence import store

path=Path(sys.argv[1])
conn=store.open_store(path)
schema.install(conn)
ctx=context(conn)
if sys.argv[2]=="original":
    result=service.run_start(ctx, {"objective":"Review calibration readings from the acquisition module.",
        "project_ref":"cold-module","budget_profile":"p0","workflow":{"id":"module_collaboration","version":"1"},
        "inputs":{"modules":[{"key":"readings","question":"Which observations are available?"}]},
        "idempotency_key":"start-original"})
    rid=result["run_id"]
    service.run_control(ctx, {"run_id":rid,"action":"pause","idempotency_key":"pause-original","reason":"cold handoff"})
else:
    rid=conn.execute("SELECT run_id FROM runs").fetchone()[0]
    before=conn.execute("SELECT status FROM runs WHERE run_id=?",(rid,)).fetchone()[0]
    assert before=="PAUSED"
    service.run_control(ctx, {"run_id":rid,"action":"continue","idempotency_key":"resume-original","reason":"continue saved task"})
run=conn.execute("SELECT * FROM runs WHERE run_id=?",(rid,)).fetchone()
flow=service._pinned_workflow(conn,run)
service.require_pins(run,flow)
cp=store.latest_checkpoint(conn,rid)
tasks=[dict(row) for row in conn.execute("SELECT task_id,role,status,input_json FROM tasks WHERE run_id=?",(rid,))]
installed={row["workflow_id"]+"@"+row["version"]:row["digest"] for row in service.default_registry().describe()}
print(json.dumps({"installed_workflows":installed,"run":rid,"status":run["status"],"workflow":run["workflow_id"],"version":run["workflow_version"],
                  "pin":run["workflow_sha256"],"installed":flow.digest(), "node":cp["node"],"checkpoint":cp["checkpoint_id"],
                  "tasks":tasks,"jobs":conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?",(rid,)).fetchone()[0]}))
"""


def main(output):
    with tempfile.TemporaryDirectory(prefix="gx-v21-v20-") as temporary:
        work=Path(temporary)
        archive=work/"v20.tar"
        with archive.open("wb") as target:
            subprocess.run(["git","archive",BASE,"gx-h"],cwd=PACKAGE,stdout=target,check=True)
        with tarfile.open(archive) as source:
            source.extractall(work,filter="data")
        db=work/"old.db"
        def execute(harness, mode):
            return json.loads(subprocess.check_output([sys.executable,"-B","-c",PROBE,str(db),mode],
                                                      cwd=harness,text=True))
        original=execute(work/"gx-h","original")
        resumed=execute(PACKAGE/"gx-h","new")
        assert original["status"]=="PAUSED" and resumed["status"]=="WAITING_CLIENT", (original,resumed)
        for key in ("run","workflow","version","pin","installed","node","tasks","jobs"):
            assert original[key]==resumed[key], (key,original,resumed)
        assert resumed["tasks"] and all(t["status"]=="READY" for t in resumed["tasks"])
        for ref, pin in original["installed_workflows"].items():
            assert resumed["installed_workflows"][ref]==pin, ref
        sha=subprocess.check_output(["git","rev-parse",BASE],cwd=PACKAGE,text=True).strip()
        output.write_text(json.dumps({"original_code":sha,"original":original,"cold_resume":resumed},
                                     indent=2,sort_keys=True)+"\n")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=HERE/"cold-resume.json")
    main(parser.parse_args().output)
