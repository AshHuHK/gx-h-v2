"""Prospectively matched static, single-lead and multibranch research runs."""
import argparse
from contextlib import ExitStack
from functools import partial
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from common import HERE, PACKAGE, ROOT, STUDY, digest, grant, save

sys.path[:0] = [str(ROOT/"acceptance/v6/engineering"),str(ROOT/"acceptance"),str(ROOT/"gx-h"),str(ROOT),str(STUDY)]
import e2e_responses
from e2e_grid import fixture_env
from peer import action
from score import reference, score, snapshot
from acceptance.v6 import live
import study_score

ARMS = ("static","single","multi")
ASSIGNMENTS = {"single":"One logical Scientific Lead controls one research branch. Do not delegate scientific decisions.",
               "multi":"The lead may open parallel branches and choose whether to delegate."}


def protocol():
    selected = grant()
    files = [p for p in HERE.iterdir() if p.suffix == ".py"]
    files += [STUDY/"study-goal.md",STUDY/"study-config.json",STUDY/"study-rubric.json",
              STUDY/"data/provenance.json",STUDY/"study_score.py",ROOT/"acceptance/v6/native_witness.py",
              ROOT/"gx-h/contracts/CONTRACT_SHA", ROOT/"gx-h/adapters/scientific/tl1a_geometry/worker.py",ROOT/"acceptance/v6/reviewer/independent_geometry.py"]
    files += list((ROOT/"gx-h/plugins/installed/org.gx.protein/0.2.0").rglob("*.py"))
    files += list((ROOT/"gx-h/plugins/installed/org.gx.protein/0.2.0").rglob("*.json"))
    return {"version":1,"question":(STUDY/"study-goal.md").read_text().strip(),
            "model":"gpt-6.2-astra","effort":"xhigh","temperature":"provider default",
            "frozen_files":{str(p.relative_to(ROOT)):digest(p) for p in sorted(files)},
            "coordinates":{r["role"]:r["sha256"] for r in selected["input_files"]},
            "design":{"static":"Fixed three-mask static Engine benchmark, with the released selection definitions on both deposits.",
                      **ASSIGNMENTS},
            "capabilities":selected["capabilities"], "per_arm_cumulative_limits":selected["run_budget"],
            "first_decision_job_cap":selected["initial_job_limit"], "denominator":dict.fromkeys(ARMS,1),
            "missing_runs":"Retain enrollment and report incomplete/failed separately. Never replace a completed live run."}


def ensure_plan(work):
    work.mkdir(mode=0o700,parents=True,exist_ok=True)
    plan = protocol()
    path = work/"preregistration.json"
    if path.exists():
        stored = json.loads(path.read_bytes())
        if plan != {k:v for k,v in stored.items() if k != "registered_at"}:
            raise ValueError("Pre-registered comparison inputs, verifier or producer changed")
    else:
        save(path,{**plan,"registered_at":datetime.now(timezone.utc).isoformat()})
    return plan


def invoke(program,env,args,history,dest,timeout=180):
    res=subprocess.run([sys.executable,"-B",str(program),*map(str,args)],capture_output=True,
                       env=env,cwd=dest,text=True,timeout=timeout)
    try:
        body=json.loads(res.stdout)
    except ValueError:
        body={}
    history.append({"argv":[str(i) for i in args],"exit":res.returncode,
                    "run_id":body.get("run_id"),"status":body.get("status")})
    save(dest/"commands.json",history)
    if res.returncode not in (0,75):
        if env.get("GX_P0_TEST_PROVIDER")=="gx_loopback" or env.get("GX_CODEX_OFFLINE"):
            detail=res.stderr.strip()[-1500:]
        else:
            detail="see the operator-private state and official status"
        raise RuntimeError(f"{args[0]} returned {res.returncode}: {detail}")
    return body


def execute(arm, mode, work, binary, plan, seconds):
    dest=work/arm
    dest.mkdir(mode=0o700,exist_ok=False)
    state, original = dest/"state", grant()
    budget = original["run_budget"]
    program=ROOT/"bin/gx-p0"
    history=[]
    with ExitStack() as stack:
        if arm=="static":
            program=HERE/"static_recipe.py"
            env={**os.environ,"HOME":str(dest),"GX_COMPARISON_CONFIG":str(dest/"config"),"GX_CODEX_OFFLINE":"1"}
            from static_recipe import STATIC_ID
            static={"project":original["project"],"objective":plan["question"],"max_rounds":1,
                    "workflow":STATIC_ID,"workflow_version":"1","plugins":[PACKAGE],
                    "run_budget":budget,"workspace_budget":budget,"input_files":original["input_files"]}
            save(dest/"spec.json",static)
            start=["--spec",dest/"spec.json"]
        else:
            if mode=="rehearse":
                if not binary or digest(binary)!=json.loads((ROOT/"release-assets/lock.json").read_bytes())["linux_x64"]["binary_sha256"]:
                    raise ValueError("Supply the released official Codex binary")
                peer=stack.enter_context(e2e_responses.local_responses(dest/"loopback-requests.jsonl"))
                stack.enter_context(patch.object(e2e_responses,"fixture_action",partial(action,arm)))
                home=dest/"home"
                home.mkdir(mode=0o700)
                env=fixture_env(home,peer,str(binary))
                # The host can also refresh its optional Git plugin catalog during startup.
                # Loopback decisions need no Internet transport, including that refresh.
                env["GIT_ALLOW_PROTOCOL"]="file"
            else:
                from acceptance.v6.live import live_environment, preflight
                env=live_environment(dest)
                if not preflight(env,dest):
                    raise ValueError("Official provider/Clash preflight failed")
            original["intent_items"]=[{"item_id":"arm-assignment","item_type":"soft_preference",
                                       "value":ASSIGNMENTS[arm]}]
            save(dest/"grant.json",original)
            start=["--goal",plan["question"],"--goal-config",dest/"grant.json"]
        env["PATH"]=str(Path(sys.executable).parent)+os.pathsep+env.get("PATH","")
        opened=invoke(program,env,["start","--state",state,"--run-key","comparison-"+arm,*start],history,dest)
        if opened["status"] not in ("COMPLETED","STOPPED"):
            invoke(program,env,["run","--state",state,"--run-id",opened["run_id"],"--timeout",str(seconds)],history,dest,timeout=seconds+90)
        before=snapshot(state)
        if before["status"] not in ("COMPLETED","STOPPED"):
            save(dest/"snapshot-incomplete.json",before)
            raise RuntimeError(f"{arm} remains {before['status']}")
        if arm != "static":
            live.capture(state/"gx.db",opened["run_id"],dest/"settled")
        invoke(program,{**env,"GX_CODEX_OFFLINE":"1"},["resume","--state",state,"--run-id",opened["run_id"],
               "--timeout","2"],history,dest)
        after=snapshot(state)
        if before!=after:
            raise ValueError("Cold resume changed an arm's measured scientific state")
        if arm != "static":
            live.capture(state/"gx.db",opened["run_id"],dest/"reopened")
        save(dest/"snapshot.json",after)
        return after


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("prepare","rehearse","live","score"))
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--binary",type=Path)
    parser.add_argument("--seconds",type=int,default=480,help="Per-arm driver window in seconds")
    parser.add_argument("--arm",choices=ARMS,help="Enroll just the selected arm in this registered cohort")
    parser.add_argument("--interpretation",type=Path,help="Blind per-arm mapping of native prose claim ID to coded statements")
    args=parser.parse_args()
    os.umask(0o077)
    if args.seconds < 1 or args.seconds > 3600:
        parser.error("Use a positive window within the enrolled one hour per-arm allowance")
    work=args.work.resolve()
    if work.is_relative_to(ROOT):
        parser.error("--work must resolve outside the release checkout")
    if work.exists() and (not work.is_dir() or work.stat().st_mode & 0o077):
        parser.error("--work must be a private directory accessible only to its owner")
    plan=ensure_plan(work)
    if args.mode=="prepare":
        print(work/"preregistration.json")
        return
    sys.path.insert(0,str(ROOT))
    modal=work/"decision-source.json"
    if args.mode=="score":
        mode=json.loads(modal.read_bytes())["mode"]
    else:
        if modal.exists() and json.loads(modal.read_bytes())["mode"] != args.mode:
            raise ValueError("Run a different inference source in a separate cohort")
        mode=args.mode
        save(modal,{"mode":mode,"source":"scripted Responses peer" if mode=="rehearse" else "official OpenAI"})
    expected=reference(grant()["input_files"])
    manual=json.loads(args.interpretation.read_bytes()) if args.interpretation else {}
    result_path=work/"results.json"
    arms=json.loads(result_path.read_bytes())["arms"] if result_path.exists() else {}
    selected=(args.arm,) if args.arm else ARMS
    if args.mode != "score" and any((work/arm).exists() for arm in selected):
        raise ValueError("This cohort already enrolled a selected arm. Re-score the original observation.")
    for arm in selected:
        if args.mode=="score" and not (work/arm/"state/gx.db").exists():
            continue
        try:
            snap=(snapshot(work/arm/"state") if args.mode=="score" else
                  execute(arm,mode,work,args.binary.resolve() if args.binary else None,plan,args.seconds))
            if snap["status"] not in ("STOPPED","COMPLETED"):
                raise ValueError(f"Enrolled arm remains {snap['status']}")
            m=score(snap,expected,mode,manual.get(arm))
            assert snap["objective"]==plan["question"] and snap["materials"]==plan["coordinates"]
            assert snap["workspace_limits"] == plan["per_arm_cumulative_limits"]
            for key,limit in plan["per_arm_cumulative_limits"].items():
                assert snap["cost"]["lines"][key]["limit"]==limit,(key,snap["cost"]["lines"][key])
            if arm=="static":
                assert snap["workflow"]=="v61_benchmark_fixed_geometry" and not snap["decisions"]
            else:
                assert not snap["workflow"] and snap["decisions"]
            assigned=arm!="single" or (len(snap["branches"])==1 and
                        all(s["scope"]=="run" for s in snap["scopes"]))
            bad=[r for r in m["independent_checks"].values() if r["differences"]]
            if arm == "static":
                adaptation={"source":"registered static stages","verified_changes":0,"decisions_with_stimulus":0,
                            "rate":None,"interpretation":"This fixed recipe has no scientific decision task."}
            else:
                assay=study_score.score(work/arm,"LIVE_MODEL" if mode=="live" else "SCRIPTED_OFFICIAL_LOOPBACK")
                save(work/arm/"evidence-to-graph.json",assay)
                eligible=[d for d in assay["decisions"] if d["prospective_conditions"]]
                evidence_changes=sum(d["verified_graph_effect"] for d in eligible)
                adaptation={"source":"original Codex rollout and GX graph/job receipts","verified_changes":evidence_changes,
                            "decisions_with_stimulus":len(eligible),"rate":evidence_changes/len(eligible) if eligible else None,
                            "initial_admissions":assay["first_revision_nodes"],
                            "physical_cross_branch_overlap":assay["cross_branch_physical_overlap"],
                            "delegations":assay["actual_delegations"]}
            valid=assigned and not bad
            arms[arm]={"status":"PASS" if valid else "FAIL", "satisfies_assignment":assigned,"new_information":adaptation,"run_id":snap["run_id"],
                        "branch_count":len(snap["branches"]),"decision_scopes":snap["scopes"],**m}
        except Exception as exc:
            arms[arm]={"status":"FAIL","error":str(exc)}
            if (work/arm/"state/gx.db").exists():
                try:
                    partial_state=snapshot(work/arm/"state")
                    arms[arm].update(run_id=partial_state["run_id"],observed_status=partial_state["status"],
                                     resource_usage=partial_state["cost"])
                except (ValueError, KeyError, OSError, TypeError):
                    pass
        save(result_path,{"source":mode, "preregistration_sha256":digest(work/"preregistration.json"),
                                 "planned_arms":len(ARMS), "arms":{k:arms.get(k,{"status":"NOT RUN"}) for k in ARMS}})
        print(arm, arms[arm]["status"],str(arms[arm].get("error",""))[:500], flush=True)
    return 0 if all(arms.get(k,{}).get("status")=="PASS" for k in selected) else 1


if __name__=="__main__":
    raise SystemExit(main())
