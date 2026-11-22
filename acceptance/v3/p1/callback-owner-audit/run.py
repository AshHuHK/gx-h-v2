"""Isolate callback owner work on the same committed SQLite bytes and artifact files."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
PINS = {"v21":"61ed0067448658e02d181b3cb85d7f68e39b6616", "p1":"2476f2bdd74b53e3833ce35d08287120e4fdf0c8"}
ROLES = ("evidence_reviewer", "planner")
TOOLS = ("gx_read_artifact", "gx_submit_product")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def quantiles(ns):
    ms = sorted(x/1e6 for x in ns)
    return {"n":len(ms), "p50_ms":statistics.median(ms), "p95_ms":ms[math.ceil(.95*len(ms))-1], "max_ms":ms[-1]}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--v21",required=True,type=Path)
    ap.add_argument("--p1",required=True,type=Path)
    ap.add_argument("--out",type=Path,default=HERE)
    ap.add_argument("--blocks",type=int,default=6)
    ap.add_argument("--per-block",type=int,default=3)
    ap.add_argument("--seed",type=int,default=403)
    args=ap.parse_args()
    out=args.out.resolve()
    out.mkdir(exist_ok=True,parents=True)
    repo={"p1":args.p1.resolve(),"v21":args.v21.resolve()}
    env={**os.environ,"GX_SCOPE":"operator","GX_PROJECTS":"*", "PYTHONDONTWRITEBYTECODE":"1","PYTHONHASHSEED":"0"}
    cpu=sorted(os.sched_getaffinity(0))[len(os.sched_getaffinity(0))//2]
    details={"pins":PINS,"utc_start":datetime.now(timezone.utc).isoformat(),"python":sys.version,
             "sqlite":sqlite3.sqlite_version,"platform":platform.platform(),"interpreter_sha256":sha(Path(sys.executable).read_bytes()),
             "command":[sys.executable,*sys.argv],"cpu":cpu,"blocks":args.blocks,"samples_per_block":args.per_block,
             "archive_sha256":{},"diagnostic_sha256":{n:sha((HERE/n).read_bytes()) for n in ("run.py","owner_trace.py")}}
    samples=[]
    rng=random.Random(args.seed)
    with tempfile.TemporaryDirectory(prefix="gx-callback-owner-",dir="/tmp") as tmp,ExitStack() as stack:
        work=Path(tmp)
        stages={}
        for arm,pin in PINS.items():
            archive=subprocess.check_output(["git","-C",str(repo[arm]),"archive",pin,"bin","gx-h","examples"])
            details["archive_sha256"][arm]=sha(archive)
            stage=work/arm
            stage.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive),mode="r:") as tree:
                tree.extractall(stage,filter="data")
            stages[arm]=stage
        archived=subprocess.check_output(["git","-C",str(repo["p1"]),"show",
            PINS["p1"]+":acceptance/v3/performance/gate0/project-specifications.jsonl.gz"])
        spec=next(row for row in map(json.loads,gzip.decompress(archived).splitlines()) if row["name"]=="00-east.json")
        (work/"spec.json").write_text(json.dumps(spec["specification"]))
        details["base_scientific_spec_sha256"]=spec["original_file_sha256"]
        env.update({"TMPDIR":str(work),"GX_P0_CODEX":str(work/"no-native-transport")})
        def command(arm,mode):
            return [sys.executable,"-B",str(HERE/"owner_trace.py"),mode,str(stages[arm]),str(work),arm,str(cpu)]
        subprocess.run(command("p1","prepare"),cwd=stages["p1"],env={**env,"PYTHONPATH":str(stages["p1"]/"gx-h")},check=True)
        details["fixtures"]=json.loads((work/"fixtures.json").read_text())
        workers={}
        for arm, stage in stages.items():
            child=subprocess.Popen(command(arm,"sample"),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,
                                   cwd=stage,env={**env,"PYTHONPATH":str(stage/"gx-h")})
            workers[arm]=child
            stack.callback(child.wait)
            stack.callback(child.stdin.close)
            stack.callback(child.stdout.close)
            assert json.loads(child.stdout.readline())["ready"]
        for block in range(-1,args.blocks):
            samples_count=1 if block==-1 else args.per_block
            plans=[(r,t,False) for r in ROLES for t in TOOLS]*samples_count
            if 0 <= block < 3:
                plans += [(r,t,True) for r in ROLES for t in TOOLS]
            rng.shuffle(plans)
            for arm in (list(PINS) if block%2==0 else list(PINS)[::-1]):
                child=workers[arm]
                child.stdin.write(json.dumps(plans)+"\n")
                child.stdin.flush()
                reply=child.stdout.readline()
                if not reply:
                    raise RuntimeError(f"trace worker {arm} failed in block {block}")
                rows=json.loads(reply)
                assert len(rows)==len(plans)
                samples.extend(dict(row,arm=arm,block=block,index=i) for i,row in enumerate(rows))
    details["utc_finish"]=datetime.now(timezone.utc).isoformat()
    (out/"environment.json").write_text(json.dumps(details,indent=2,sort_keys=True)+"\n")
    with gzip.open(out/"samples.jsonl.gz","wt") as f:
        for row in samples:
            print(json.dumps(row,separators=(",",":")),file=f)
    summary={}
    for role in ROLES:
        for tool in TOOLS:
            case=role+"."+tool
            summary[case]={}
            for arm in PINS:
                all_rows=[r for r in samples if r["role"]==role and r["tool"]==tool and r["arm"]==arm and r["block"]>=0]
                bare=[r for r in all_rows if not r["traced"]]
                prof=[r["profile"] for r in all_rows if r["traced"]]
                assert len({r["request_sha256"] for r in all_rows})==1
                assert len({r["writes"] for r in all_rows})==1
                summary[case][arm]={"wall":quantiles(r["wall_ns"] for r in bare),"cpu":quantiles(r["cpu_ns"] for r in bare),
                    "db_changes":bare[0]["writes"],"request_sha256":bare[0]["request_sha256"],"traces":len(prof),
                    "sql_total":sorted({p["sql_total"] for p in prof}),"sql_shape_orders":sorted({p["sql_order_sha256"] for p in prof}),
                    "python_fsync_count":sorted({p["python_fsync_count"] for p in prof}),
                    "python_open_counts":prof[0]["python_open_counts"],"call_counts":{k:v["calls"] for k,v in prof[0]["calls"].items()}}
    (out/"summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n")
    for key,group in summary.items():
        print(key,[(a,row["wall"],row["cpu"],row["db_changes"],row["sql_total"]) for a,row in group.items()])


if __name__=="__main__":
    main()
