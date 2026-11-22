"""Verify every candidate Run's public binding against its actual worker, Job and method receipts."""
import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import time

from audit_workload import db_file, rows, records_in
from run_workload import GRID_PLUGIN, LANES, WAVE_OFFSETS, save, sha, utc

WORKFLOW="science_grid@2"
METHOD="fixture-grid@1"
TOOL="gx_grid_measurement"


def native_tools(work):
    by_thread={}
    calls={}
    for p in sorted((work/"codex-home/sessions").rglob("*.jsonl")):
        with p.open(encoding="utf-8") as f:
            thread=None
            for line in f:
                r=json.loads(line)
                payload=r.get("payload") or {}
                if r.get("type")=="session_meta":
                    thread=payload["id"]
                    by_thread[thread]=sorted(t["name"] for t in payload.get("dynamic_tools",[]))
                item=payload.get("item") or {}
                if (thread and r.get("type")=="event_msg" and payload.get("type")=="item_completed"
                        and item.get("type")=="DynamicToolCall" and item.get("tool")==TOOL):
                    call_id=item["id"]
                    calls.setdefault(thread,{})[call_id]={"call_id":call_id,"turn_id":payload.get("turn_id"),
                        "at":r["timestamp"],"official_status":item.get("status"),"official_success":item.get("success")}
    return by_thread,calls


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private",type=Path,required=True)
    p.add_argument("--evidence",type=Path,required=True)
    p.add_argument("--runtime",type=Path,required=True)
    p.add_argument("--first-wave",action="store_true",help="Only verify the closed first pair")
    args=p.parse_args()
    work,out,runtime=args.private.resolve(),args.evidence.resolve(),args.runtime.resolve()
    output=out/("first-wave-treatment.json" if args.first_wave else "treatment.json")
    stage=work/"install"
    shipped=json.loads((stage/"gx-h/config/shipped.json").read_bytes())
    catalog=json.loads((stage/"gx-h/config/workflow_packages.json").read_bytes())
    adapters=[e["dispatch_preparation"][WORKFLOW] for e in catalog["packages"]
              if WORKFLOW in e.get("dispatch_preparation",{})]
    if len(adapters)!=1:
        raise RuntimeError("One pinned release dispatch adapter is required for the composed grid")
    adapter=adapters[0]
    mod=adapter["function"].split(":")[0]
    source=stage/"gx-h"/Path(*mod.split("."))
    module_file=source.with_suffix(".py") if source.with_suffix(".py").is_file() else source/"__init__.py"
    if sha(module_file)!=adapter["sha256"]:
        raise RuntimeError("Dispatch adapter differs from the release pin")
    operator=json.loads((out/"operator-plugin-verify.json").read_bytes())
    selected=next(v for v in operator["plugins"] if v["id"]+"@"+v["version"]==GRID_PLUGIN)
    observed,official_calls=native_tools(work)
    counts=Counter(v["run_id"] for v in records_in(work/"metrics.jsonl") if v.get("kind")=="p0_callback" and v.get("tool")==TOOL)
    env={**os.environ,"CODEX_HOME":str(work/"codex-home"),"GX_P0_PROFILE":str(work/"profile.json"),
         "GX_P0_CODEX":str(runtime/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"),
         "PYTHONDONTWRITEBYTECODE":"1"}
    env.pop("GX_METRICS_FILE",None)
    issues=[]
    starts=[]
    receipts=[]
    commands=[]
    states=[work/"state-00"] if args.first_wave else sorted(work.glob("state-*"))
    root_profile=json.loads((out/"environment.json").read_bytes())
    for state in states:
        with closing(db_file(state/"gx.db")) as c:
            local=rows(c,"SELECT run_id,run_key,workflow_id,workflow_version,workflow_sha256,inputs_json FROM runs "
                         "JOIN p0_workspace_runs USING(run_id) ORDER BY run_key")
            before=c.execute("SELECT count(*) FROM p0_meter_native_turns").fetchone()[0]
        argv=[str(runtime/".venv/bin/python"),str(stage/"bin/gx-p0"),"status","--state",str(state)]
        begin=utc()
        t=time.monotonic()
        ctl=subprocess.run(argv,cwd=stage,env=env,capture_output=True,text=True,timeout=60)
        log=work/(state.name+"-verify-status.out")
        err=work/(state.name+"-verify-status.err")
        log.write_text(ctl.stdout)
        err.write_text(ctl.stderr)
        commands.append({"command":argv,"at":begin,"exit_code":ctl.returncode,"duration_s":round(time.monotonic()-t,3),
                         "stdout_sha256":sha(log),"stderr_sha256":sha(err)})
        if ctl.returncode:
            issues.append("public status failed for "+state.name)
            continue
        document=json.loads(ctl.stdout)
        statuses={v["run_id"]:v for v in document.get("runs",[document])}
        with closing(db_file(state/"gx.db")) as c:
            if c.execute("SELECT count(*) FROM p0_meter_native_turns").fetchone()[0]!=before:
                issues.append("new model Turn during read-only proof for "+state.name)
            for run in local:
                rid,key=run["run_id"],run["run_key"]
                source_spec=json.loads(run["inputs_json"])
                start=json.loads((work/("start-"+key+".out")).read_bytes())
                binding=statuses[rid].get("plugin_binding",{})
                packages={v["id"]+"@"+v["version"]:v for v in binding.get("packages",[])}
                methods={v["capability_version"]:v for v in binding.get("capabilities",[])}
                method=methods.get(METHOD,{})
                cap=c.execute("SELECT manifest_sha256,status FROM capabilities WHERE capability_id='fixture-grid' AND version='1'").fetchone()
                asset=c.execute("SELECT sha256,kind FROM artifacts WHERE artifact_ref=?",(method.get("content_artifact_ref"),)).fetchone()
                all_jobs=rows(c,"SELECT j.job_id,j.status,m.admission_json,m.admission_sha256,m.completion_sha256 "
                               "FROM jobs j JOIN p0_execution_manifests m USING(job_id) WHERE j.run_id=? ORDER BY j.job_id",(rid,))
                for j in all_jobs:
                    admission=json.loads(j.pop("admission_json"))
                    j["capability"]=admission["capability"]
                roots=rows(c,"SELECT thread_id,role,attempt,tool_names_json FROM p0_roots "
                             "WHERE run_id=? AND task_id IS NOT NULL ORDER BY rowid",(rid,))
                for root in roots:
                    root["gx_advertised"]=TOOL in json.loads(root.pop("tool_names_json") or "[]")
                    root["official_advertised"]=TOOL in observed.get(root["thread_id"],[])
                    root["invocations"]=list(official_calls.get(root["thread_id"],{}).values())
                    for item in root["invocations"]:
                        reply=c.execute("SELECT turn_id,result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                        (root["thread_id"],item["call_id"])).fetchone()
                        item["gx_call_receipt_matches_turn"]=bool(reply and reply["turn_id"]==item["turn_id"])
                        if reply and item["official_success"]:
                            value=json.loads(reply["result_json"])
                            item["returned_evidence"]={k:value.get(k) for k in
                                ("path_id","job_id","proposal_ref","evidence_ref","observations_ref","evaluation_ref")}
                            owned=c.execute("SELECT run_id FROM jobs WHERE job_id=?",(value.get("job_id"),)).fetchone()
                            item["job_belongs_to_run"]=bool(owned and owned["run_id"]==rid)
                            if not item["gx_call_receipt_matches_turn"] or not item["job_belongs_to_run"]:
                                issues.append("successful native tool result has a mismatched GX receipt: "+item["call_id"])
                workflow=binding.get("workflow",{})
                chk={
                    "public_start_final_and_durable_receipts_match": bool(binding) and binding==start.get("plugin_binding")==source_spec.get("plugin_binding"),
                    "actual_public_requested_selection":source_spec["inputs"]["p0_spec"].get("plugins")==[GRID_PLUGIN],
                    "exact_verified_plugin_package":packages.get(GRID_PLUGIN,{}).get("sha256")==selected["sha256"],
                    "exact_original_graph":workflow=={"id":run["workflow_id"],"version":run["workflow_version"],"sha256":run["workflow_sha256"]}
                         and workflow["sha256"]==shipped["workflows"][WORKFLOW],
                    "dispatch_adapter_verified":binding.get("dispatch_preparation")==adapter,
                    "canonical_portable_method": method.get("portable_sha256")==shipped["capabilities"][METHOD],
                    "canonical_workspace_method":bool(cap and asset and asset["kind"]=="asset_content" and method.get("manifest_sha256")==cap["manifest_sha256"]==asset["sha256"]),
                    "all_scientific_jobs_used_method":len(all_jobs)==2 and all(j["capability"]=={"id":"fixture-grid","version":"1","sha256":method.get("manifest_sha256")} and j["completion_sha256"] for j in all_jobs),
                    "tool_bound_in_gx_and_official_root":bool(roots) and all(r["gx_advertised"] and r["official_advertised"] for r in roots),
                }
                receipt={"run_id":rid,"run_key":key,"status":statuses[rid]["status"],"plugin_binding":binding,
                         "roots":roots,"jobs":all_jobs,"actual_grid_measurement_callbacks":counts[rid],
                         "checks":chk,"verified":all(chk.values())}
                receipts.append(receipt)
                if not receipt["verified"]:
                    issues.extend(f'{key} {k}' for k,v in chk.items() if not v)
                starts.append(key)
    expected=len(LANES) if args.first_wave else len(WAVE_OFFSETS)*len(LANES)
    if len(set(starts))!=expected:
        issues.append(f"expected {expected} distinct Run bindings, obtained {len(set(starts))}")
    report={"captured":utc(),"source_commit":root_profile["source_commit"],"public_selection":"public GX Run specification",
            "expected_extra_spec_fields":{"plugins":[GRID_PLUGIN]},"operator_package":selected,"released_dispatch_adapter":adapter,
            "status_commands":commands,"runs":receipts,"actual_grid_measurement_callbacks":sum(r["actual_grid_measurement_callbacks"] for r in receipts),
            "official_grid_calls_completed":sum(len(r["invocations"]) for v in receipts for r in v["roots"]),
            "official_grid_calls_successful":sum(i["official_success"] is True for v in receipts for r in v["roots"] for i in r["invocations"]),
            "errors":issues,"result":"PASS" if not issues else "FAIL"}
    save(output,report)
    print(json.dumps({"result":report["result"],"runs":len(receipts),"callbacks":report["actual_grid_measurement_callbacks"],"errors":issues},indent=2))
    if issues:
        raise SystemExit(1)


if __name__=="__main__":
    main()
