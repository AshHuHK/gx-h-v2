"""Run the previous release's three compatibility groups and the newly affected contracts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs",type=Path)
    args=parser.parse_args()
    logs=args.logs.resolve()
    logs.mkdir(parents=True,exist_ok=True)
    prior=json.loads((ROOT/"acceptance/unified_multiturn/evidence/controlled/observations.json").read_text())
    suites={s["group"]:s["modules"] for s in prior["pinned_rerun"]}
    suites["science_capabilities"]=["acceptance.science_capabilities."+file.stem for file in sorted(
        (ROOT/"acceptance/science_capabilities").glob("test_*.py"))]
    suites["boundaries"]=["tests.test_acquired_documents","tests.test_science_capability_views","tests.test_v6_named_ports",
                          "tests.test_source_decision_receipts","tests.test_v61_schema","tests.test_v61_native_contract","tests.test_v2_fencing",
                          "acceptance.unified_multiturn.test_live_receipts","acceptance.unified_multiturn.test_web_delivery_recovery"]
    env=dict(os.environ,GX_CODEX_OFFLINE="1",PYTHONDONTWRITEBYTECODE="1",
             PYTHONPATH=os.pathsep.join(str(ROOT/path) for path in (".","gx-h","gx-h/tests","acceptance/v62a")))
    def check(pair):
        name,modules=pair
        path=logs/(name+".log")
        start=time.monotonic()
        with path.open("w") as output:
            run=subprocess.run([sys.executable,"-B","-m","unittest","-v",*modules],cwd=ROOT,env=env,
                               stdout=output,stderr=subprocess.STDOUT)
        content=path.read_text()
        count=re.search(r"^Ran (\d+) tests? in ",content,re.M)
        skips=re.findall(r"\.\.\. skipped ",content)
        summary=dict(group=name,modules=modules,exit=run.returncode,tests=int(count[1]) if count else None,
                     skipped=len(skips),seconds=round(time.monotonic()-start,3))
        print(name,summary["tests"],"tests, exit",run.returncode,flush=True)
        return summary
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(check,suites.items()))
    receipt={"candidate":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
             "python":sys.version.split()[0],"suites":results}
    (logs/"release-tests.json").write_text(json.dumps(receipt,indent=2)+"\n")
    return any(r["exit"]!=0 or r["tests"] is None for r in results)


if __name__=="__main__":
    raise SystemExit(main())
