"""Register and run the four public scripted slots through the official host and normal Goal CLI."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from importlib.metadata import version
import sqlite3
import subprocess
import sys
import time
from threading import Event
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"acceptance/v62b/engineering"), str(ROOT/"gx-h")]
from run import MARKER, BINARY, archive, declared_calls, fixture_env, replay_report, responses, wire_catalog
from peer import historical_calls
from acceptance.v62c import materials, pilot_peer

P = json.loads((HERE/"protocol.json").read_text())
GOAL = (HERE/"goal.md").read_text().strip()
CARD = HERE/"method-card.json"
ASSAY = P["assay"]


def db_open(slot):
    conn = sqlite3.connect((slot/"state/gx.db").resolve().as_uri()+"?mode=ro",uri=True,timeout=5)
    conn.row_factory=sqlite3.Row
    return conn


def grant(architecture):
    caps = {"tokens":P["budget"]["tokens_per_slot"],"api_calls":36,"worker_tasks":24,"job_submissions":4,
            "cpu_seconds":300,"wall_hours":1,"gpu_jobs":0,"gpu_minutes":0}
    return {"project":"public-coordinate-coverage","branch_delegation":False,"research_effort":"deep","evidence_policy":"gx.evidence/6.2.1",
            "research_modules":[] if architecture=="single" else ["data_inventory@2"],
            "plugins":["org.gx.protein@0.3.0"],"capabilities":[ASSAY["capability"]],
            "initial_job_limit":4,"turn_token_budget":32768,"workspace_budget":caps,"run_budget":caps,
            "input_files":[{"path":str(materials.PARENT),"role":"reference","sha256":materials.PARENT_SHA},
                           {"path":str(CARD),"role":"workflow_spec","sha256":archive.sha256(CARD)}]}


def action(wire,path,body,ident,assignment_complete):
    if len(wire)>=70:
        raise ValueError("Public rehearsal exceeded its registered local request ceiling")
    messages=(t for obj in reversed(body["input"]) if obj.get("role") in {"user","developer"}
              for t in responses.message_texts(obj) if MARKER in t)
    raw=next(messages).split(MARKER,1)[1]
    brief,end=json.JSONDecoder().raw_decode(raw)
    specs,tools=wire_catalog(body)
    pending = brief["scope"]=="run" and any(
        b.get("spec",{}).get("method_family")=="registered reference comparison" for b in brief.get("branches",[]))
    if pending and not pilot_peer.ref_for(brief, "structure"):
        # Synchronize the operator cut after the accepted plan. This stale brief carries no specimen.
        if not assignment_complete.wait(timeout=20):
            raise RuntimeError("Operator failed to publish the prospective assignment")
        return {"type":"message","id":"msg-"+ident,"role":"assistant","content":[
            {"type":"output_text","text":"Awaiting the specimen in my next brief."}]},{"scope":"run","tool":"operator_cut"}

    try:
        replies=historical_calls(body["input"], responses.output_json)
    except ValueError as exc:
        # Finish the superseded lease normally if the precise in-flight callback lost its authority.
        if (not brief.get("module") or pilot_peer.ref_for(brief,"structure") or
                "SCOPE_DENIED: this root no longer owns the current task attempt" not in str(exc)):
            raise
        return {"type":"message","id":"msg-"+ident,"role":"assistant","content":[
            {"type":"output_text","text":"This assignment was superseded."}]},{
                "scope":brief["scope"],"tool":"superseded_lease"}

    tool,args=pilot_peer.step(brief,replies)
    if brief["scope"]=="run" and tool=="gx_read_artifact":
        args.pop("operation_key",None)
    ns,= [namespace for namespace,name in declared_calls(specs) if name==tool]
    call_id="call-"+ident
    wire.append({"call_id":call_id,"utc":datetime.now(timezone.utc).isoformat(),"brief":brief,
                 "brief_text":MARKER+raw[:end],"tools":sorted(tools),"tool":tool,"args":args,"namespace":ns,
                 "model":body["model"],"effort":(body.get("reasoning") or {}).get("effort"),
                 "tool_results":[{"tool":name,"ref":ref,"value_sha256":materials.digest(json.dumps(data,sort_keys=True).encode())}
                                 for (name,ref),data in replies.items()]})
    materials.save(path,wire)
    call={"call_id":call_id,"type":"function_call","name":tool,"arguments":json.dumps(args)}
    if ns: call["namespace"]=ns
    return call,{"scope":brief["scope"],"tool":tool}


def state_hash(slot,rid):
    tables={"jobs":"job_id,status,reservation_ref,measured_json","v6_decisions":"decision_ref,task_id,receipt_json",
            "v62_modules":"module_instance_id,scope,status","v62_module_reports":"module_instance_id,report_ref,receipt_json",
            "v62_module_wakes":"wake_key,event_id","v6_graph_versions":"version,sha256","hypothesis_revisions":None}
    with closing(db_open(slot)) as db:
        frozen = {t:[list(r) for r in db.execute(f"SELECT {cols} FROM {t} WHERE run_id=? ORDER BY rowid",(rid,))]
                  for t,cols in tables.items() if cols}
        frozen["claims"]=[list(r) for r in db.execute("SELECT r.revision,r.sha256 FROM hypothesis_revisions r "
            "JOIN hypotheses h USING(hypothesis_id) WHERE h.run_id=? ORDER BY r.rowid",(rid,))]
    return materials.digest(json.dumps(frozen,sort_keys=True).encode())


def run_slot(work,slot_spec,oracle,binary):
    number=slot_spec["slot"];code=slot_spec["code"];arch=slot_spec["architecture"]
    slot=work/number;slot.mkdir(mode=0o700)
    wire=[];commands=[];raw=slot/"raw";raw.mkdir()
    materials.save(slot/"grant.json",grant(arch))
    profile={"default":{"provider":"gx_loopback","model":P["model"],"effort":P["model_effort"],"multi_agent":False}}
    materials.save(slot/"profile.json",profile)
    start=time.monotonic()
    assignment_complete=Event()
    with responses.local_responses(raw/"requests.jsonl") as peer, patch.object(responses,"fixture_action",lambda b,i:action(wire,raw/"wire.json",b,i,assignment_complete)):
        env=fixture_env(slot,peer,str(binary))
        # Declare this local scripted provider through the host's released model catalog mechanism.
        # The immutable live provider must advertise its own model and reasoning effort before admission.
        home=Path(env["CODEX_HOME"])
        template=archive.read(ROOT/"vendor/codex/codex-rs/models-manager/models.json")["models"][0]
        assert P["model_effort"] in {row["effort"] for row in template["supported_reasoning_levels"]}
        catalog=home/"scripted-catalog.json"
        materials.save(catalog,{"models":[{**template,"slug":P["model"],"display_name":P["model"],"tool_mode":"direct","use_responses_lite":False}]})
        cfg=home/"config.toml"
        cfg.write_text("model_catalog_json = "+json.dumps(str(catalog))+"\n"+cfg.read_text())
        env.update(GX_P0_PROFILE=str(slot/"profile.json"),PATH=str(Path(sys.executable).parent)+os.pathsep+env["PATH"])
        assert env["GX_P0_TEST_PROVIDER"]=="gx_loopback" and not (slot/"codex/auth.json").exists()
        def cli(verb,*args,offline=False):
            argv=[sys.executable,"-B",str(ROOT/"bin/gx-p0"),verb,"--state",str(slot/"state"),*map(str,args)]
            proc=subprocess.run(argv,cwd=slot,env={**env,**({"GX_CODEX_OFFLINE":"1"} if offline else {})},capture_output=True,text=True,timeout=P["time_limit_seconds"]+20)
            commands.append({"argv":argv,"exit":proc.returncode,"stdout":proc.stdout,"stderr":proc.stderr})
            materials.save(raw/"commands.json",commands)
            if proc.returncode:
                raise RuntimeError(f"{verb} exited {proc.returncode}: {(proc.stderr or proc.stdout)[-900:]} {str([x.get('error') for x in peer.rows if 'error' in x])[:500]}")
            return json.loads(proc.stdout)

        status=cli("start","--goal",GOAL,"--run-key","public-coverage","--goal-config",slot/"grant.json")
        rid=status["run_id"];assert not peer.request_count and not status["graph_head"]
        with closing(db_open(slot)) as db:
            parent,=db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=?",(rid,materials.PARENT_SHA)).fetchone()
        assert parent in status["materials"]
        materials.save(slot/"parent.json",materials.parent_acquisition())
        args=("--run-id",rid,"--source","archive-reference","--artifact-ref",parent,"--sha256",materials.PARENT_SHA,"--acquisition",slot/"parent.json")
        parent_receipt=cli("observe",*args)
        assert cli("observe",*args)==parent_receipt
        metadata={**materials.parent_acquisition(),"processing":["coordinate-record withholding/"+oracle[code]["transform_sha256"]],
                  "derived_from":[{"artifact_ref":parent,"sha256":materials.PARENT_SHA}]}
        materials.save(slot/"assigned-acquisition.json",metadata)
        with ThreadPoolExecutor(max_workers=1) as workers:
            first_run=workers.submit(cli,"run","--run-id",rid,"--timeout",P["time_limit_seconds"])
            deadline=time.monotonic()+P["time_limit_seconds"]
            while time.monotonic()<deadline:
                with closing(db_open(slot)) as conn:
                    first=conn.execute("SELECT d.decision_ref FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id,run_id) WHERE d.run_id=? AND t.scope='run' ORDER BY d.rowid LIMIT 1",(rid,)).fetchone()
                    module=conn.execute("SELECT module_instance_id FROM v62_modules WHERE run_id=? AND status='OPEN'",(rid,)).fetchone()
                if first: break
                if first_run.done(): raise RuntimeError(f"Driver ended before initial plan: {first_run.result().get('waiting_for')}")
                time.sleep(.025)
            if not first: raise RuntimeError("Deadline without accepted initial plan")
            assert bool(module)==(arch=="delegation_available")
            assign=("--run-id",rid,"--after-decision",first[0],"--source","assigned-specimen","--role","structure",
                    "--input",work/"custody"/code/"specimen.cif","--sha256",oracle[code]["sha256"],
                    "--acquisition",slot/"assigned-acquisition.json","--media-type","chemical/x-mmcif")
            if module:assign+=("--module-instance",module[0])
            try:
                assigned=cli("assign-material",*assign)
            except Exception:
                cli("stop","--run-id",rid,offline=True)
                raise
            finally:
                assignment_complete.set()
            materials.save(slot/"assignment.json",assigned)
            final=first_run.result()
        if final["goal_status"]=="OPEN":
            remaining=int(deadline-time.monotonic())
            if remaining<=0: raise RuntimeError("Deadline exhausted before resumption")
            final=cli("run","--run-id",rid,"--timeout",remaining)
        if final["goal_status"]!="CLOSED": raise RuntimeError(f"Goal remained open: {final.get('waiting_for')}")
        fingerprint=state_hash(slot,rid)
        before=len(wire)
        replay=replay_report(slot,rid,wire) if arch=="delegation_available" else {"redeliveries":0}
        cold=cli("resume","--run-id",rid,"--timeout",5,offline=True)
        assert final["graph_head"]==cold["graph_head"] and state_hash(slot,rid)==fingerprint and before==len(wire)
        assert final["resources"]==cold["resources"]
        assert all(c["model"]==P["model"] and c["effort"]==P["model_effort"] for c in wire)
        assert not [row["error"] for row in peer.rows if row.get("error")], "Unexpected native peer failure"
        initial=wire[0]["brief"]
        for x in oracle.values():
            assert x["sha256"] not in json.dumps(initial) and f"{x['selected']}/{x['observed']}" not in json.dumps(initial)
        turn_sets={role:{r["brief_text"] for r in wire if ("run" if r["brief"]["scope"]=="run" else "module")==role} for role in ("run","module")}
        with closing(db_open(slot)) as conn:
            jobs=[dict(x) for x in conn.execute("SELECT job_id,status,capability_id,capability_version,measured_json FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))]
            counts={r:len(ts) for r,ts in turn_sets.items()}
            claims=[json.loads(x[0]) for x in conn.execute("SELECT document_json FROM hypothesis_revisions JOIN hypotheses USING(hypothesis_id) WHERE run_id=? ORDER BY revision",(rid,))]
        assert len(jobs)==2 and all(j['status']=='SUCCEEDED' for j in jobs)
        assert len(claims)==2
        actual_scopes=sorted({row["scope"] for row in peer.rows})
        accounting={}
        for scope in actual_scopes:
            rows=[r for r in peer.rows if r["scope"]==scope]
            # These input/output counts are emitted by the pinned local Responses fixture.
            assert all(r["scripted_provider_tokens"]==35 for r in rows)
            accounting[scope]={"requests":len(rows),"fixture_input_tokens":30*len(rows),"fixture_output_tokens":5*len(rows),
                               "fixture_cached_input_tokens":0,"fixture_total_tokens":sum(r["scripted_provider_tokens"] for r in rows),
                               "actual_provider_input_tokens":None,"actual_provider_output_tokens":None,
                               "actual_provider_cached_input_tokens":None,"actual_usd":None}
        summary={"status":"PASS","series":P["series"],"classification":"SCRIPTED_REHEARSAL","slot":number,"architecture":arch,
                 "run_id":rid,"initial_plan_ref":first[0],"initial_brief":initial,"initial_tools":wire[0]["tools"],
                 "parent_acquisition":parent_receipt,"assignment":assigned,"jobs":jobs,"claims":claims,"resources":final["resources"],
                 "role_usage":{r:{"turns":counts[r],"brief_utf8_bytes":sum(len(b.encode()) for b in ts),
                 "scripted_model_requests":sum(1 for c in peer.rows if (c["scope"]=="run")== (r=="run")),
                 "scripted_response_tokens":sum(row["scripted_provider_tokens"] for row in peer.rows
                    if (row.get("scope")=="run")== (r=="run"))} for r,ts in turn_sets.items()},
                 "cold_state_sha256":fingerprint,"cold_report_replay":replay,"wall_seconds":round(time.monotonic()-start,3),
                 "accounting_by_scope":accounting,
                 "native_admin_cutpoints":[{k:row[k] for k in ("id","scope","tool","scripted_provider_tokens")} for row in peer.rows if row["tool"] in ("operator_cut","superseded_lease")],
                 "paid_model_calls":0,"provider_tokens":None,"provider_usd":None}
        materials.save(slot/"summary.json",summary)
        return summary


def ready(work,binary,slots):
    """Verify the public material commitment and lock all offline settings before enrollment."""
    known=archive.read(ROOT/"release-assets/lock.json")["linux_x64"]["binary_sha256"]
    if archive.sha256(binary)!=known: raise ValueError("Pinned Codex binary differs")
    oracle=materials.prepare(work/"custody")
    for code,v in oracle.items():
        assert all(v[k]==h for k,h in P["materials"][code].items())
    settings={name:archive.sha256(HERE/name) for name in ("method-card.json","goal.md","protocol.json","pilot_peer.py",
              "materials.py","rehearse.py","scoring.py","witness.py","rate.py","parity.py","run-public.sh") if (HERE/name).is_file()}
    for path in ("gx-h/contracts/CONTRACT_SHA","gx-h/gx_core/p0/v6_native.py","gx-h/gx_core/p0/v62_native.py",
                 "gx-h/gx_core/p0/v621_policy.py"):
        settings[path]=archive.sha256(ROOT/path)
    files={p for pattern in ("gx-h/gx_core/**/*.py","gx-h/contracts/**/*.json","gx-h/plugins/installed/org.gx.protein/0.3.0/**/*",
                             "gx-h/plugins/installed/org.gx.data-inventory/2.0.0/**/*", "acceptance/v62b/engineering/*.py",
                             "acceptance/e2e_responses.py","bin/gx-p0","release-assets/lock.json")
           for p in ROOT.glob(pattern) if p.is_file() and "__pycache__" not in p.parts}
    runtime={str(path.relative_to(ROOT)):archive.sha256(path) for path in sorted(files)}
    materials.save(work/"runtime-pins.json",runtime)
    frozen={"series":P["series"],"source_commit":subprocess.check_output(["git","-C",ROOT,"rev-parse","HEAD"],text=True).strip(),
            "research_effort":P["research_effort"],"wire_model":P["model"],"wire_effort":P["model_effort"],
            "provider":"gx_loopback","future_provider":"openai","sampling":P["sampling"],"scope":"SCRIPTED_REHEARSAL",
            "binary_sha256":known,"runtime_pins_sha256":archive.sha256(work/"runtime-pins.json"),
            "python":platform.python_version(),"biopython":version("biopython"),"source_pins":settings,"protocol_sha256":archive.sha256(HERE/"protocol.json"),
            "endpoints_sha256":archive.sha256(work/"custody/oracle.json"),"slots":slots,
            "grants":{a:grant(a) for a in ("single","delegation_available")},
            "production_paid_batch_ready":False,"reason":"No public spending authorization or verified atomic upper bound for the final provider request."}
    for name in ("org.gx.protein@0.3.0","org.gx.data-inventory@2.0.0"):
        proc=subprocess.run([sys.executable,"-B",str(ROOT/"bin/gx-p0"),"plugin","verify",name],cwd=work,capture_output=True,text=True)
        if proc.returncode:raise ValueError(f"Plugin not ready: {name}: {proc.stderr[-500:]}")
        frozen.setdefault("plugins",{})[name]=json.loads(proc.stdout)
    materials.save(work/"registration.json",frozen)
    return oracle


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work",type=Path,required=True)
    ap.add_argument("--binary",type=Path,default=BINARY)
    ap.add_argument("--slot",choices=tuple(x["slot"] for x in P["slots"]),help="Run one separate diagnostic enrollment")
    args=ap.parse_args();work=args.work.resolve();binary=args.binary.resolve()
    if work.exists() or work.is_relative_to(ROOT):ap.error("Provide a fresh directory outside the checkout")
    work.mkdir(mode=0o700,parents=True)
    slots=[s for s in P["slots"] if args.slot is None or args.slot==s["slot"]]
    oracle=ready(work,binary,slots)
    result={"series":P["series"],"classification":"SCRIPTED_REHEARSAL","paid_model_calls":0,"provider_tokens":None,
            "official_provider_usd":None,"public_live_slots_enrolled":0,"original_private_slots_enrolled":0,
            "registration_sha256":archive.sha256(work/"registration.json"),"cases":{}}
    for s in slots:
        number=s["slot"]
        try:
            summary=run_slot(work,s,oracle,binary)
            result["cases"][number]={k:v for k,v in summary.items() if k not in ("initial_brief","resources","claims","jobs")}
            result["cases"][number]["job_count"]=len(summary["jobs"])
        except Exception as error:
            result["cases"][number]={"status":"FAIL","reason":f"{type(error).__name__}: {error}"}
            materials.save(work/"result.json",result)
            raise
        materials.save(work/"result.json",result)
        print(number,"PASS",flush=True)
    if len(slots)==4:
        pins=json.loads((work/"runtime-pins.json").read_text())
        changed=[name for name,digest in pins.items() if archive.sha256(ROOT/name)!=digest]
        if changed:
            raise RuntimeError(f"Candidate runtime changed during enrollment: {changed}")
    print("Official-host scripted engineering trials finished. Scoring reads each frozen state independently.")


if __name__=="__main__":
    main()
