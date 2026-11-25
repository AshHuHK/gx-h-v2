"""A1. Exercise public Goal delegation and a direct quick control with the same real CPU methods."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance/v6/engineering"))
from official import archive, fixture_env, frozen_state, responses
from module_peer import GEOMETRY, HUMAN, MATERIALS, QUALITY, answer, finding
sys.path.insert(0, str(ROOT / "acceptance/v61/adaptive"))
from study_score import geometry_oracle

EXAMPLE = ROOT / "examples/v62a-data-inventory"
PIN = ROOT / "release-assets/lock.json"


def cli(work, env, action, args, out):
    call = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), action, "--state", str(work / "state"), *map(str,args)]
    proc = subprocess.run(call, env=env, cwd=work, capture_output=True, text=True, timeout=120)
    archive.save(out / (action + ".json"), {"argv":call, "exit":proc.returncode,
                                            "stdout":proc.stdout,"stderr":proc.stderr})
    if proc.returncode:
        raise RuntimeError(f"{action} exited {proc.returncode}: {(proc.stderr or proc.stdout)[-1000:]}")
    return json.loads(proc.stdout)


def read_assets(conn, work, rid):
    jobs = []
    granted = set(json.loads(conn.execute("SELECT materials_json FROM v6_goals WHERE run_id=?",(rid,)).fetchone()[0]))
    inputs = {m["sha256"]:m for m in conn.execute(
        "SELECT artifact_ref,sha256 FROM artifacts WHERE run_id=? AND kind='run_input'",(rid,)) if m["artifact_ref"] in granted}
    for row in conn.execute("""SELECT n.node_id, n.branch_id, n.exec_key, n.operation_key, n.result_ref,
               j.job_id,j.status,j.capability_id,j.measured_json,j.reservation_ref,r.sha256,r.namespace,r.rel_path,
               d.thread_id AS commissioned_thread,d.turn_id AS commissioned_turn, a.reused_from
               FROM v6_nodes n JOIN jobs j ON j.job_id=n.job_id JOIN artifacts r ON r.artifact_ref=n.result_ref AND r.job_id=j.job_id
               JOIN v6_decisions d ON d.decision_ref=n.decision_ref AND d.run_id=n.run_id
               JOIN v6_job_attempts a ON a.node_id=n.node_id AND a.run_id=n.run_id
               WHERE n.run_id=? AND j.run_id=n.run_id ORDER BY j.capability_id""", (rid,)):
        job = dict(row)
        assert job.pop("status") == "SUCCEEDED" and job["reservation_ref"] and job["exec_key"]
        path = work/"state/artifacts"/job.pop("namespace")/job.pop("rel_path")
        assert archive.sha256(path) == job["sha256"]
        value = archive.read(path)
        assert value["input_sha256"] in inputs
        source = inputs[value["input_sha256"]]
        _, = [m for m in MATERIALS if MATERIALS[m] == value["input_sha256"]]
        material = conn.execute("SELECT namespace,rel_path FROM artifacts WHERE artifact_ref=? AND run_id=?",
                                (source["artifact_ref"],rid)).fetchone()
        coord = work / "state/artifacts" / material["namespace"] / material["rel_path"]
        assert archive.sha256(coord) == value["input_sha256"]
        if value["method"] == QUALITY:
            from Bio.PDB.MMCIF2Dict import MMCIF2Dict
            cif = MMCIF2Dict(str(coord))
            columns = ["group_PDB","pdbx_PDB_model_num","label_entity_id","label_asym_id","label_atom_id","label_seq_id","occupancy"]
            pos = {chain:set() for chain in ("A","B","C")}
            for group, model, entity, chain, atom, number, occupancy in zip(*(cif["_atom_site."+k] for k in columns)):
                if group == "ATOM" and model == "1" and entity == "1" and chain in pos and atom in {"C","CA","N","O"} and float(occupancy)>0:
                    pos[chain].add(number)
            union = len(set().union(*pos.values()))
            assert value["mapping"]["observed_union_residues"] == union
            assert {k:v["observed_residues"] for k,v in value["per_chain"].items()} == {k:len(v) for k,v in pos.items()}
            job["independent_check"] = {"parser":"Bio.PDB.MMCIF2Dict","union_residues":union,
                                         "per_chain_residues":{k:len(v) for k,v in pos.items()}}
        else:
            assert value["method"] == GEOMETRY and value["entry"] == "2O0O"
            job["independent_check"], = geometry_oracle(coord,[value])
        job.update(input_ref=source["artifact_ref"], input_sha256=value["input_sha256"], finding=finding(value))
        outcome = archive.read(work/"state/jobs"/job["job_id"]/"outcome.json")
        assert outcome["started"] and outcome["tool_pid"] > 0 and outcome["exit_code"] == 0
        job["observed_cpu_seconds"] = json.loads(job.pop("measured_json"))["cpu_seconds"]
        job["tool_pid"] = outcome["tool_pid"]
        jobs.append(job)
    assert sorted(x["capability_id"]+"@1" for x in jobs) == sorted((QUALITY,GEOMETRY))
    return jobs


def snapshot(work, rid, arm, trace):
    with closing(sqlite3.connect((work/"state/gx.db").as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        def rows(sql):
            return [dict(r) for r in db.execute(sql,(rid,))]
        jobs = read_assets(db,work,rid)
        requests = {r["call_id"]:r for r in trace}
        roots = rows("""SELECT r.thread_id,d.turn_id,r.task_id,r.lease_id,r.brief,r.effective_profile_json,t.scope,t.context_ref,t.base_json,
                     m.confidence,m.budget_status,m.settled_tokens,m.observed_lower_bound,m.reservation_ref
                     FROM p0_roots r JOIN v6_decision_tasks t USING(task_id)
                     JOIN p0_root_deliveries d ON d.thread_id=r.thread_id
                     JOIN p0_meter_native_turns m ON m.thread_id=r.thread_id AND m.turn_id=d.turn_id
                     WHERE r.run_id=? ORDER BY m.sequence""")
        calls = rows("""SELECT c.call_id,c.thread_id,c.turn_id FROM p0_root_calls c
                        JOIN p0_roots r USING(thread_id) WHERE r.run_id=? ORDER BY c.rowid""")
        assert {c["call_id"] for c in calls} == set(requests)
        official_calls = rows("""SELECT n.call_id,n.request_json FROM p0_v6_native_calls n
                              JOIN p0_roots r USING(thread_id) WHERE r.run_id=?""")
        for record in official_calls:
            observed = json.loads(record["request_json"])
            requested = requests[record["call_id"]]
            assert (observed["tool"],observed.get("namespace"),observed["arguments"]) == (
                requested["tool"], requested["namespace"], requested["args"])
        assert all(r["namespace"] == "gx" for r in trace if r["tool"] in ("gx_decide_revision","gx_submit_module_report"))
        assert set(c["call_id"] for c in official_calls) >= {
            r["call_id"] for r in trace if r["tool"] != "gx_read_artifact"}
        turns = []
        for root in roots:
            access = json.loads(root["effective_profile_json"])
            native = access["research_access"]
            assert native["environments"] == [] and native["sandbox"]["type"] == "readOnly"
            assert native["sandbox"]["networkAccess"] is False and native["approval_policy"] == "never"
            cwd = Path(native["cwd"]).resolve()
            assert native["visible_files"] == [] and not cwd.is_relative_to(ROOT)
            assert cwd.is_dir() and not list(cwd.iterdir()) and cwd.stat().st_mode & 0o777 == 0o700
            conf = native["thread_config"]
            assert conf["features.shell_tool"] is False and conf["features.unified_exec"] is False
            assert conf["web_search"] == "disabled" and conf["agents.enabled"] is False
            assert access["provider"] == "gx_loopback" and root["budget_status"] == "SETTLED"
            items = [requests[c["call_id"]] for c in calls if c["thread_id"] == root["thread_id"]]
            assert items and all(set(r["tools"])-{"gx"} == set(native["dynamic_tools"]) for r in items)
            frozen = json.loads(root["brief"].split("\n",1)[1])
            assert all(frozen == r["brief"] for r in items)
            chosen = [r["tool"] for r in items]
            issued = [j for j in jobs if j["commissioned_turn"] == root["turn_id"] and j["commissioned_thread"] == root["thread_id"]]
            turns.append({
                "scope":root["scope"], "context_ref":root["context_ref"], "task_id":root["task_id"],
                "lease_id":root["lease_id"],"thread_id":root["thread_id"],"turn_id":root["turn_id"],
                "base":json.loads(root["base_json"]) if root["base_json"] else None,
                "frozen_brief_utf8_bytes":len(root["brief"].encode()),
                "frozen_brief_sha256":hashlib.sha256(root["brief"].encode()).hexdigest(),
                "frozen_brief_token_count":None,"provider_prompt_tokens":None,
                "advertised_tools":native["dynamic_tools"],"tool_summaries":len(native["dynamic_tools"]),
                "visible_method_summaries":len(frozen.get("capabilities", [])),
                "visible_module_summaries":len(frozen.get("module_catalog", [])), "calls":chosen, "response_requests":len(items),
                "scripted_response_tokens":sum(r["scripted_provider_tokens"] for r in items),
                "token_source":"credential-free scripted loopback receipt",
                "metering":{"reservation_ref":root["reservation_ref"],"confidence":root["confidence"],
                            "budget_status":root["budget_status"],"booked_tokens":root["settled_tokens"],
                            "observed_lower_bound":root["observed_lower_bound"]},
                "model":access["model"],"model_reasoning_effort":access["effort"],
                "commissioned_jobs":[j["job_id"] for j in issued], "measured_job_cpu_seconds":sum(j["observed_cpu_seconds"] for j in issued),
                "gpu_jobs":0,"external_monetary_spend":None,
                "host":{"provider":access["provider"],"isolated_cwd":Path(native["cwd"]).name,
                        "cwd_mode":oct(Path(native["cwd"]).stat().st_mode & 0o777), "visible_files":native["visible_files"],
                        "sandbox":native["sandbox"],"environments":native["environments"],
                        "approval_policy":native["approval_policy"],"shell_tool":conf["features.shell_tool"],
                        "unified_exec":conf["features.unified_exec"],"web_search":conf["web_search"]}})
        assert len({t["thread_id"] for t in turns}) == len(turns) and len(turns)>1
        assert len({t["host"]["isolated_cwd"] for t in turns}) == len(turns)
        first = trace[0]["brief"]
        assert first["head"] is None and not first["nodes"] and not first["observations"]
        assert {x["sha256"] for x in first["materials"]} == set(MATERIALS.values())
        assert all(j["finding"] not in json.dumps(first) for j in jobs)
        versions = rows("SELECT version,sha256,parent_sha FROM v6_graph_versions WHERE run_id=? ORDER BY version")
        result = {"run_id":rid,"turns":turns,"jobs":jobs,"graph_versions":versions}
        if arm=="module":
            mask = [r for r in trace if r["brief"].get("scope","run") == "run"]
            nonempty = [(r,rep) for r in mask for rep in r["brief"].get("module_reports",[])]
            last, projection = nonempty[0]
            row, = rows("SELECT * FROM v62_module_reports WHERE run_id=?")
            module, = rows("SELECT module_instance_id,goal_version,scope,module_id,module_version,round,status,branch_id,open_key,receipt_json,budget_ref FROM v62_modules WHERE run_id=?")
            assert module["status"] == "COMPLETE" and module["module_id"] == "data_inventory"
            assert all(j["branch_id"] == module["branch_id"] for j in jobs)
            assert len({t["lease_id"] for t in turns if t["scope"]==module["scope"]}) >= 2
            assert projection["report_ref"] == row["report_ref"] and row["module_instance_id"] == module["module_instance_id"]
            full = db.execute("SELECT * FROM artifacts WHERE run_id=? AND artifact_ref=?",(rid,row["report_ref"])).fetchone()
            p = work / "state/artifacts" / full["namespace"] / full["rel_path"]
            assert archive.sha256(p) == full["sha256"]
            bundle = archive.read(p)
            author = bundle["author"]
            writer, = [t for t in turns if t["turn_id"] == author["turn_id"]]
            assert all(writer[k] == author[k] for k in ("context_ref","lease_id","task_id","thread_id"))
            assert set(bundle["artifact_refs"]) == {j["result_ref"] for j in jobs}
            for job in jobs:
                source, = [r for r in bundle["sources"] if r["artifact_ref"] == job["result_ref"]]
                assert source["sha256"] == job["sha256"]
                assert any(rec.get("exec_key") == job["exec_key"] and rec["job_id"] == job["job_id"] for rec in source["receipts"])
                assert any(rec.get("tool") == "gx_read_artifact" and rec["source_sha256"] == job["sha256"] for rec in source["receipts"])
            expected = {(j["finding"],j["result_ref"]) for j in jobs}
            assert set((f["text"],ref) for f in projection["findings"] for ref in f["source_refs"]) == expected
            assert row["report_ref"] in last["args"]["decision"]["evidence_refs"]
            assert all(not r["brief"].get("observations") and not any(j["finding"] in json.dumps(r["brief"]) for j in jobs)
                       for r in mask if r["monotonic"]<last["monotonic"])
            first_open = next(r for r in trace if r["tool"]=="gx_open_module")
            submitted = next(r for r in trace if r["tool"]=="gx_submit_module_report")
            events = []
            for event in rows("""SELECT event_id,kind,subject_id,payload_json FROM events WHERE run_id=? AND
                 (kind LIKE 'v62.module.%' OR kind IN ('v6.decision.committed','v6.node.settled','v6.run.closed'))
                 ORDER BY event_id"""):
                detail = json.loads(event.pop("payload_json"))
                keep = ("round","scope","reason","module_scope","operation_key","module_instance_id","decision_ref",
                        "base","result_head","artifact_ref","result_ref","exec_key","job_id","wake_key")
                events.append({**event, **{k:detail[k] for k in keep if k in detail}})
            kinds = [e["kind"] for e in events]
            assert kinds.count("v62.module.opened") == kinds.count("v62.module.reported") == kinds.count("v62.module.wake") == 1
            assert kinds.count("v62.module.tool_completed") == 2
            assert kinds.index("v62.module.reported") < kinds.index("v62.module.wake") < kinds.index("v6.run.closed")
            account = dict(db.execute("SELECT parent_ref,limits_json,spent_json,reserved_json FROM budgets WHERE budget_ref=?", (module["budget_ref"],)).fetchone())
            for k in ("limits","spent","reserved"):
                account[k] = json.loads(account.pop(k+"_json"))
            module["open_receipt"] = json.loads(module.pop("receipt_json"))
            result.update(module=module,module_account=account,events=events,round_wakes=rows("SELECT * FROM v62_module_wakes WHERE run_id=? ORDER BY event_id"),
                          report={"artifact_ref":row["report_ref"],"sha256":full["sha256"],"version":row["version"],
                                  "operation_key":row["operation_key"],"author":author,"sources":bundle["sources"],"published_projection":projection},
                          first_delivered_to_lead_at_call=last["call_id"],
                          delegation_to_submission_seconds=round(submitted["monotonic"]-first_open["monotonic"],3))
            assert len(result["round_wakes"])==1
        return result


def run_arm(work, binary, grant, arm):
    work.mkdir(mode=0o700)
    out = work / "raw"
    out.mkdir()
    trace = []
    def respond(body, ident):
        item, detail, brief, tools = answer(arm, body, ident)
        if len(trace)>=24:
            raise RuntimeError("Exceeded the scripted public test's Response request ceiling")
        trace.append({"call_id":item["call_id"],"tool":item["name"],"namespace":item.get("namespace"),"args":json.loads(item["arguments"]),
                      "brief":brief,"tools":tools,"monotonic":time.monotonic()})
        archive.save(out / "wire.json",trace)
        return item, detail

    cfg = json.loads(grant.read_bytes())
    for material in cfg["input_files"]:
        p = (grant.parent / material["path"]).resolve()
        assert archive.sha256(p) == MATERIALS[p.name] == material["sha256"]
        material["path"] = str(p)
    if arm == "quick":
        cfg.pop("research_effort",None)
        cfg.pop("research_modules",None)
    archive.save(work / "grant.json", cfg)
    with responses.local_responses(out/"requests.jsonl") as peer, patch.object(responses,"fixture_action",respond):
        env = fixture_env(work,peer,str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        assert not any(k in env for k in ("OPENAI_API_KEY","GH_TOKEN","GITHUB_TOKEN"))
        started = cli(work,env,"start",["--goal",HUMAN,"--goal-config",work/"grant.json","--run-key","coordinate-inventory"],out)
        rid = started["run_id"]
        assert started["graph_head"] is None and peer.request_count == 0
        try:
            start = time.monotonic()
            final = cli(work,env,"run",["--run-id",rid,"--timeout","65"],out)
            wall = round(time.monotonic()-start,3)
            assert final["status"] == "STOPPED" and final["goal_status"] == "CLOSED", (final["status"],final.get("waiting_for"))
            # Use actual HTTP server usage rows; a conservative ledger booking can exceed a fixture response.
            wire_usage = {"call-"+r["id"]:r["scripted_provider_tokens"] for r in peer.rows}
            assert set(wire_usage)=={r["call_id"] for r in trace}
            for item in trace:
                item["scripted_provider_tokens"]=wire_usage[item["call_id"]]
            archive.save(out/"wire.json",trace)
            before = snapshot(work,rid,arm,trace)
            seen = peer.request_count
            reopened = cli(work,{**env,"GX_CODEX_OFFLINE":"1"},"resume",["--run-id",rid,"--timeout","5"],out)
            assert snapshot(work,rid,arm,trace) == before
            assert reopened["graph_head"] == final["graph_head"] and reopened["resources"] == final["resources"]
            assert peer.request_count == seen and len(trace)==seen
            return {**before,"costs":final["resources"],"wall_seconds":wall,
                    "cold_resume":{"graph_head":reopened["graph_head"],"additional_jobs":0,"additional_requests":0},
                    "paid_model_calls":0,"provider_tokens":None,"external_paid_services":[],
                    "scripted_responses":seen, "paid_usd":None}
        finally:
            if (work/"state/gx.db").is_file():
                frozen_state(work/"state",out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work",type=Path,required=True,help="Fresh private directory outside the checkout")
    p.add_argument("--binary",type=Path,default=ROOT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
    p.add_argument("--arm", choices=("both","quick","module"),default="both")
    args=p.parse_args()
    work=args.work.resolve()
    if work.is_relative_to(ROOT) or work.exists():
        p.error("Supply a fresh directory outside the source checkout")
    work.mkdir(mode=0o700,parents=True)
    binary=args.binary.resolve()
    sha=archive.sha256(binary)
    assert sha==archive.read(PIN)["linux_x64"]["binary_sha256"]
    version_home=work/"binary-home"
    version_home.mkdir(mode=0o700)
    receipt={"case":"A1","status":"FAIL","paid_model_calls":0,"provider_tokens":None,
             "source":"Official Codex App Server + credential-free localhost scripted Responses + real JobService CPU subprocesses",
             "source_head":subprocess.check_output(["git","-C",ROOT,"rev-parse","HEAD"],text=True).strip(),
             "script_sha256":{f.name:archive.sha256(f) for f in (Path(__file__).resolve(),Path(__file__).with_name("module_peer.py"))},
             "source_tokenizer":"No model tokenizer was invoked; exact UTF-8 sizes of delivered frozen briefs are reported, prompt token counts unobserved.",
             "source_changes":subprocess.check_output(["git","-C",ROOT,"status","--short"],text=True).splitlines(),
             "binary_sha256":sha,"binary_version":subprocess.check_output([binary,"--version"],env={"HOME":str(version_home),"CODEX_HOME":str(version_home),"PATH":os.defpath},text=True).strip(),
             "arms":{}}
    try:
        for arm in (("quick","module") if args.arm=="both" else (args.arm,)):
            result=run_arm(work/arm,binary,EXAMPLE/"grant.json",arm)
            raw_files = (work/arm/"raw").iterdir()
            result["raw_files_sha256"] = {str(f.relative_to(work)):archive.sha256(f) for f in sorted(raw_files) if f.is_file()}
            result["grant_sha256"] = archive.sha256(work/arm/"grant.json")
            receipt["arms"][arm]=result
        if args.arm=="both":
            arms=receipt["arms"]
            quick_outputs = {(j["input_sha256"],j["sha256"]) for j in arms["quick"]["jobs"]}
            delegated_outputs = {(j["input_sha256"],j["sha256"]) for j in arms["module"]["jobs"]}
            assert quick_outputs == delegated_outputs
            def counts(a):
                return {"native_turns":len(a["turns"]),"response_requests":a["scripted_responses"],
                        "fixture_tokens":sum(t["scripted_response_tokens"] for t in a["turns"]),
                        "metered_tokens_including_estimates":sum(t["metering"]["booked_tokens"] for t in a["turns"]),
                        "sum_frozen_brief_bytes":sum(t["frozen_brief_utf8_bytes"] for t in a["turns"]),
                        "jobs":len(a["jobs"]),"cpu_seconds":sum(j["observed_cpu_seconds"] for j in a["jobs"]),
                        "wall_seconds":a["wall_seconds"]}
            q, m = counts(arms["quick"]), counts(arms["module"])
            receipt["comparison"]={"quick":q,"module":m,"module_minus_quick":{k:round(m[k]-q[k],6) for k in m},
                                   "scope":"One engineering run per arm on this host; no comparative inference on scientific utility."}
        receipt["status"]="PASS"
    finally:
        archive.save(work/"result.json",receipt)
    print(json.dumps({"status":receipt["status"],"receipt":str(work/"result.json"),"sha256":archive.sha256(work/"result.json")}))


if __name__=="__main__":
    main()
