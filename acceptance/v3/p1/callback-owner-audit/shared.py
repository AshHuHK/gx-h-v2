"""Time the exact callback and its local sync/SQLite owners on a private directory of the study filesystem."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

from run import HERE, PINS, ROLES, TOOLS, quantiles, sha

def child():
    # Import shared fixture/owner definitions with their original argument ABI.
    _, _, src, seed, arm, cpu, shared = sys.argv
    sys.argv = [sys.argv[0], "sample", src, seed, arm, cpu]
    import owner_trace as core
    from gx_core.hostobs.app_server_client import ReceivedMessage
    from gx_core.persistence import store
    seed, shared = Path(seed), Path(shared)
    fixtures = json.loads((seed/"fixtures.json").read_text())
    os.sched_setaffinity(0,{int(cpu)})
    seeds, directories = {}, {}
    for role in ROLES:
        dst = shared/arm/role
        shutil.copytree(seed/role, dst)
        seeds[role] = {p.relative_to(dst/"artifacts") for p in (dst/"artifacts").rglob("*")}
        directories[role] = dst
    real_connect = sqlite3.connect
    sql_times = None
    class Timed(sqlite3.Connection):
        def execute(self, query, *parameters):
            if query in ("BEGIN IMMEDIATE","COMMIT"):
                start = time.perf_counter_ns()
                try:
                    return super().execute(query,*parameters)
                finally:
                    sql_times[query] = time.perf_counter_ns()-start
            return super().execute(query,*parameters)
    def traced_connect(*a,**kw):
        return real_connect(*a,**kw,factory=Timed)
    print('{"ready":true}',flush=True)
    for line in sys.stdin:
        results = []
        for role,tool,traced in json.loads(line):
            dst, f = directories[role], fixtures[role]
            shutil.copyfile(seed/role/"gx.db",dst/"gx.db")
            msg = json.loads((seed/(role+".json")).read_text())[tool]
            assert sha(core.payload(msg))==f["envelopes"][tool]["sha256"]
            sql_times={}
            if traced:
                sqlite3.connect=traced_connect
            try:
                db=store.open_store(dst/"gx.db")
            finally:
                sqlite3.connect=real_connect
            with closing(db):
                for table in ("tasks","outbox"):
                    store._columns(db,table)
                root=core.owner(db,f["run"])
                root.active_turns[f["thread"]]=f["turn"]
                received=ReceivedMessage(1,msg)
                syncs=[]
                sql_times.clear()
                real_sync=os.fsync
                def timed_sync(fd):
                    kind="directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                    begin=time.perf_counter_ns()
                    try:
                        return real_sync(fd)
                    finally:
                        syncs.append((kind,time.perf_counter_ns()-begin))
                if traced:
                    os.fsync=timed_sync
                before=db.total_changes
                wall, cpu = time.perf_counter_ns(), time.process_time_ns()
                try:
                    reply=root._callback(received)
                finally:
                    elapsed,used=time.perf_counter_ns()-wall,time.process_time_ns()-cpu
                    os.fsync=real_sync
                assert reply["success"],reply
                result=json.loads(reply["contentItems"][0]["text"])
                if tool=="gx_submit_product":
                    assert result["task_status"]=="ACCEPTED"
                else:
                    assert len(result["routes"])==2
                assert db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0]==0
                record={"role":role,"tool":tool,"traced":traced,"wall_ns":elapsed,"cpu_ns":used,"writes":db.total_changes-before,
                        "request_sha256":f["envelopes"][tool]["sha256"]}
                if traced:
                    assert set(sql_times)=={"BEGIN IMMEDIATE","COMMIT"} and len(syncs)==(13 if tool=="gx_submit_product" else 0)
                    record.update(sqlite_ns=sql_times.copy(),artifact_fsyncs=syncs)
                results.append(record)
            # Restore the exact original artifact population and empty-directory shape.
            for path in sorted((dst/"artifacts").rglob("*"),key=lambda p:len(p.parts),reverse=True):
                if path.relative_to(dst/"artifacts") not in seeds[role]:
                    path.rmdir() if path.is_dir() else path.unlink()
            assert not (dst/"gx.db-wal").exists()
        print(json.dumps(results,separators=(",",":")),flush=True)


def parent():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--v21",type=Path,required=True)
    ap.add_argument("--p1",type=Path,required=True)
    ap.add_argument("--storage",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--blocks",type=int,default=4)
    ap.add_argument("--per-block",type=int,default=2)
    args=ap.parse_args()
    repos={"v21":args.v21.resolve(),"p1":args.p1.resolve()}
    cpu=sorted(os.sched_getaffinity(0))[len(os.sched_getaffinity(0))//2]
    info={"pins":PINS,"utc_start":datetime.now(timezone.utc).isoformat(),"cwd":os.getcwd(),"command":[sys.executable,*sys.argv],
        "script_sha256":{f:sha((HERE/f).read_bytes()) for f in ("run.py","owner_trace.py","shared.py")},
        "cpu":cpu,"python":sys.version,"sqlite":sqlite3.sqlite_version,"strace":shutil.which("strace"),
        "storage":str(args.storage.resolve()),"storage_type":subprocess.check_output(["findmnt","-T",str(args.storage),"-n","-o","FSTYPE"],text=True).strip()}
    rows=[]
    with tempfile.TemporaryDirectory(dir="/tmp",prefix="gx-callback-src-") as temp, tempfile.TemporaryDirectory(
            dir=args.storage.resolve(),prefix="callback-owner-diagnostic-") as data, ExitStack() as stack:
        temp, data=Path(temp),Path(data); stages={}
        info["disposable_directory"]=str(data)
        for arm,pin in PINS.items():
            stage=temp/arm
            stage.mkdir()
            archive=subprocess.check_output(["git","-C",str(repos[arm]),"archive",pin,"bin","gx-h","examples"])
            with tarfile.open(fileobj=io.BytesIO(archive),mode="r:") as tar:
                tar.extractall(stage,filter="data")
            stages[arm]=stage
        spec_gz=subprocess.check_output(["git","-C",str(repos["p1"]),"show",PINS["p1"]+":acceptance/v3/performance/gate0/project-specifications.jsonl.gz"])
        spec=next(s for s in map(json.loads,gzip.decompress(spec_gz).splitlines()) if s["name"]=="00-east.json")
        (temp/"spec.json").write_text(json.dumps(spec["specification"]))
        env={**os.environ,"GX_SCOPE":"operator","GX_PROJECTS":"*","GX_P0_CODEX":str(temp/"no-transport"),
             "TMPDIR":str(temp),"PYTHONDONTWRITEBYTECODE":"1","PYTHONHASHSEED":"0"}
        subprocess.run([sys.executable,"-B",str(HERE/"owner_trace.py"),"prepare",str(stages["p1"]),str(temp)],
                       cwd=stages["p1"],env={**env,"PYTHONPATH":str(stages["p1"]/"gx-h")},check=True)
        info["fixtures"]=json.loads((temp/"fixtures.json").read_text())
        children={}
        for arm,stage in stages.items():
            proc=subprocess.Popen([sys.executable,"-B",str(HERE/"shared.py"),"sample",str(stage),str(temp),arm,
                str(cpu),str(data)],cwd=stage,env={**env,"PYTHONPATH":str(stage/"gx-h")},stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            children[arm]=proc
            stack.callback(proc.wait);stack.callback(proc.stdin.close);stack.callback(proc.stdout.close)
            assert json.loads(proc.stdout.readline())["ready"]
        rng=random.Random(403)
        for block in range(-1,args.blocks):
            plans=[(r,t,False) for r in ROLES for t in TOOLS]*(1 if block<0 else args.per_block)
            if block>=0:
                plans.extend((r,t,True) for r in ROLES for t in TOOLS)
            rng.shuffle(plans)
            for arm in (list(PINS) if block%2==0 else list(PINS)[::-1]):
                child=children[arm]
                child.stdin.write(json.dumps(plans)+"\n");child.stdin.flush()
                batch=json.loads(child.stdout.readline())
                rows.extend(dict(row,block=block,arm=arm,index=i) for i,row in enumerate(batch))
    info.update(utc_finish=datetime.now(timezone.utc).isoformat(),disposable_directory_removed=not data.exists())
    summary={}
    for tool in TOOLS:
        for arm in PINS:
            group=[r for r in rows if r["block"]>=0 and r["tool"]==tool and r["arm"]==arm]
            bare=[r for r in group if not r["traced"]]; probes=[r for r in group if r["traced"]]
            summary[arm+"."+tool]={"bare_wall":quantiles(r["wall_ns"] for r in bare),"bare_cpu":quantiles(r["cpu_ns"] for r in bare),
                "timed_artifact_fsync":quantiles(sum(v for k,v in r["artifact_fsyncs"]) for r in probes),
                "timed_begin_immediate":quantiles(r["sqlite_ns"]["BEGIN IMMEDIATE"] for r in probes),
                "timed_commit":quantiles(r["sqlite_ns"]["COMMIT"] for r in probes)}
    args.out.write_text(json.dumps({"environment":info,"summary":summary,"samples":rows},sort_keys=True,indent=2)+"\n")
    print(json.dumps(summary))


if __name__=="__main__":
    child() if len(sys.argv)>1 and sys.argv[1]=="sample" else parent()
