"""Preregister and run the two-deposition adaptive geometry acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from study_score import HERE, ROOT, digest, geometry_oracle, live, score

CONFIG = HERE/"study-config.json"
WORKER = ROOT/"gx-h/plugins/installed/org.gx.protein/0.2.0/methods/deposited_geometry.py"
FITTER = ROOT/"gx-h/adapters/scientific/tl1a_geometry/worker.py"
PIN = "org.gx.protein@0.2.0"


def frozen_inputs():
    grant = json.loads(CONFIG.read_bytes())
    path_set = [HERE/name for name in ["study.py","study_score.py","study_scripted.py","study-goal.md",
                                        "study-config.json","study-rubric.json","data/provenance.json"]]
    path_set.extend([WORKER,WORKER.with_suffix(".json"),FITTER,ROOT/"acceptance/v6/native_witness.py",
                     ROOT/"acceptance/v6/live.py",ROOT/"acceptance/v6/reviewer/independent_geometry.py"])
    package = WORKER.parents[1]
    path_set.extend(p for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    path_set.append(ROOT / "gx-h/contracts/CONTRACT_SHA")
    for obj in grant["input_files"]:
        path = (HERE/obj["path"]).resolve(strict=True)
        if digest(path) != obj["sha256"]:
            raise ValueError("Grant content does not match deposited structure")
        path_set.append(path)
    return {str(p.relative_to(ROOT)):digest(p) for p in path_set}


def require_registration(registration):
    registered = json.loads(registration.read_bytes())
    if registered["inputs"] != frozen_inputs():
        raise ValueError("Inputs differ from preregistration")
    return registered


def offline_numerics(out):
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for item in json.loads(CONFIG.read_bytes())["input_files"]:
            data = (HERE/item["path"]).resolve()
            for mask in ("common_backbone","common_ca","ordered_backbone"):
                argv = [sys.executable,"-I","-B",str(WORKER),"--fitter",str(FITTER),"--structure",str(data),
                        "--mask",mask,"--out",str(folder)]
                subprocess.run(argv,check=True,timeout=20)
                observed = json.loads((folder/"geometry.json").read_bytes())
                check, = geometry_oracle(data,[observed])
                records.append({"measured":observed,"independent_check":check})
    live.save(out,{"source_class":"DIRECT_CPU_NUMERICAL_REHEARSAL","model_calls":0,"gx_jobs":0,"results":records})
    return 0


def execute(private, out, env, seconds, classification, registration):
    grant = json.loads(CONFIG.read_bytes())
    rc, pin = live.command(env,["plugin","verify",PIN])
    live.save(out/"protein-plugin.json",{"exit_code":rc,"result":pin})
    if rc: return 2
    state = private/"state"
    argv=["start","--state",state,"--run-key","v61-comparative-study","--goal",(HERE/"study-goal.md").read_text().strip(),
          "--goal-config",CONFIG]
    opened = live.invocation(env,argv,90,private/"start-invocation.json")
    rid=opened["stdout"].get("run_id")
    if not rid or opened["exit_code"]:
        live.save(out/"start-error.json",opened["stdout"]);return 3
    initial = live.snapshot(state/"gx.db",rid)
    assert not any(initial[k] for k in ("jobs","decisions","turns","workflow_id"))
    ledger=json.loads(initial["ledger"][0]["limits_json"])
    assert ledger == grant["run_budget"]
    live.save(out/"enrollment.json",{"run_id":rid,"source_class":classification,"registration_digest":digest(registration),
              "source_commit":subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip(),
              "initial_resources":ledger,"initial_graph":None,"paid_turns_before_run":0})
    _, run, resume = live.observe_changes(private,rid,env,seconds,out)
    if run["exit_code"] not in (0,75) or resume["exit_code"] not in (0,75):
        live.save(out/"error.json",{"run":run["stdout"].get("error"),"resume":resume["stdout"].get("error")})
        return 4
    result = score(private,classification)
    live.save(out/"result.json",result)
    return 0 if result["observed_evidence_to_graph_effect"] else 5


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_subparsers(dest="action",required=True)
    for action in ("preregister","numerics","score","loopback","live"):
        p=actions.add_parser(action)
        p.add_argument("--out",type=Path,required=True)
        if action != "preregister": p.add_argument("--registration",type=Path,required=True)
        if action in ("loopback","live","score"):
            p.add_argument("--private-dir",type=Path,required=True)
        if action in ("loopback","live"):
            p.add_argument("--seconds",type=int,default=480)
        if action=="loopback": p.add_argument("--binary",type=Path,required=True)
        if action=="score": p.add_argument("--source-class",choices=("LIVE_MODEL","SCRIPTED_OFFICIAL_LOOPBACK"),required=True)
    args=parser.parse_args()
    os.umask(0o077)
    out=args.out.resolve()
    if out.exists():parser.error("--out must be new")
    if args.action=="preregister":
        live.save(out,{"schema":"gx.v61.adaptive_study/1","frozen_at":live.now(),"inputs":frozen_inputs(),
                       "historical_observed_runs_in_denominator":0,"primary_live_denominator":1})
        return 0
    require_registration(args.registration)
    if args.action=="numerics":return offline_numerics(out)
    private=args.private_dir.resolve()
    if args.action=="score":
        live.save(out,score(private,args.source_class));return 0
    if private.exists() or private.is_relative_to(ROOT) or out.is_relative_to(private) or private.is_relative_to(out):
        parser.error("Use fresh independent external private and evidence directories")
    private.mkdir(parents=True,mode=0o700)
    out.mkdir(parents=True,mode=0o700)
    if args.action=="loopback":
        from study_scripted import peer
        with peer(private,out,args.binary.resolve()) as env:
            return execute(private,out,env,args.seconds,"SCRIPTED_OFFICIAL_LOOPBACK",args.registration)
    env=live.live_environment(private)
    if not live.preflight(env,out):return 2
    return execute(private,out,env,args.seconds,"LIVE_MODEL",args.registration)


if __name__=="__main__":
    sys.exit(main())
