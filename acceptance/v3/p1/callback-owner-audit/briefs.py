"""Measure the existing submit-time brief hash on the first eight waves' saved Root briefs."""
from contextlib import closing
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[3]
sys.path.insert(0,str(ROOT/"gx-h"))
from gx_core.domain.ids import sha256_of_obj

def quantiles(values):
    ordered=sorted(values)
    return {"n":len(ordered),"p50":statistics.median(ordered),"p95":ordered[math.ceil(.95*len(ordered))-1],
            "max":ordered[-1]}

def main():
    perf=HERE.parent.parent/"performance"
    sample=perf/"p1"/"eight-wave-callback-samples.jsonl.gz"
    evidence={"input_sha256":hashlib.sha256(sample.read_bytes()).hexdigest(),"repeats":32,"groups":{}}
    for arm,pin in (("baseline","61ed006"),("candidate","2476f2b")):
        original=subprocess.check_output(["git","-C",str(ROOT),"show",pin+":gx-h/gx_core/domain/ids.py"])
        assert hashlib.sha256(original).hexdigest()==hashlib.sha256((ROOT/"gx-h/gx_core/domain/ids.py").read_bytes()).hexdigest()
        selected=[x for x in map(json.loads,gzip.open(sample,"rt")) if x["arm"]==arm and x["tool"]=="gx_submit_product"]
        stage=Path(json.loads((perf/("gate0" if arm=="baseline" else "p1")/"start.json").read_text())["private_staging"])
        rows=[]
        for wave in sorted({s["wave"] for s in selected}):
            final_db=stage/f"state-{wave:02d}"/"gx.db"
            with closing(sqlite3.connect(final_db.as_uri()+"?mode=ro",uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                for sample_row in (s for s in selected if s["wave"]==wave):
                    brief,names=db.execute("SELECT brief,tool_names_json FROM p0_roots WHERE run_id=? AND thread_id=?",
                        (sample_row["run_id"],sample_row["thread_id"])).fetchone()
                    packet=json.loads(brief.split("\n",1)[1])
                    tools=packet["gx_tools"]
                    assert names is None or json.loads(names)==tools
                    digest=sha256_of_obj(brief)
                    at=time.process_time_ns()
                    for _ in range(32):
                        assert sha256_of_obj(brief)==digest
                    cpu=(time.process_time_ns()-at)/(32*1e3)
                    rows.append({"wave":wave,"run_id":sample_row["run_id"],"thread_id":sample_row["thread_id"],
                        "role":sample_row["role"],"bytes":len(brief.encode()),"brief_sha256":digest,"hash_cpu_us":cpu,
                        "tools":len(tools),"grid_tool": "gx_grid_measurement" in tools,
                        "workflow_context_keys":sorted(packet.get("workflow_context",{}))})
        evidence["groups"][arm]={"rows":rows,"bytes":quantiles([r["bytes"] for r in rows]),
            "hash_cpu_us":quantiles([r["hash_cpu_us"] for r in rows]),"grid_tool_present":sum(r["grid_tool"] for r in rows)}
    (HERE/"paid-briefs.json").write_text(json.dumps(evidence,indent=2,sort_keys=True)+"\n")
    print({k:{n:v for n,v in g.items() if n!="rows"} for k,g in evidence["groups"].items()})

if __name__=="__main__":
    main()
