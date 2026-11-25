"""Verify the frozen evidence for V6.2.3. Production and scientific conclusions have separate gates."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = "e6520f53ca19ef8eae3ec0915af74e9bfc6dcdca"
OLD_CANDIDATE = "a0d167cc34a51b2c687e038510372c84971616a0"
PUBLIC = {"2RE9":"8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b",
          "2O0O":"7e9381a6184ae8bc7f9c9423248c25133e135f0857814834cdd88ea8e9eb1103"}


def require(value, why):
    if not value:
        raise ValueError(why)


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle,"sha256").hexdigest()


def historical_claim(e):
    require(e["policy"]=="gx.evidence/6.2.3" and e["external_paid_calls"]==0, "Unattributed historical claim")
    require(e["original"]["publication"]["direction"]=="opposed" and e["external"]["level"] in ("open","insufficient")
            and e["external"]["publication"]["retrospective_data_refs"], "Known readout gained prospective support")


def delivered(host):
    require(host["final_status"]=="CLOSED" and host["raw_counts"]==[49,164] and host["actual_paid_calls"]==0,
            "Expected an actual closed CPU Job with independently checked endpoint")
    kinds={r["receipt"]["delivery_kind"] for r in host["endpoint_events"]}
    require(kinds=={"brief","artifact_read"} and host["native_decisions"][-1]["endpoint_use"]["used_metric_refs"]
            and host["official_model_request_report"] and host["official_model_request_tool_result"],
            "Reporting or indexing alone does not establish native delivery and use")


def public_method(d, commit, archive):
    require(d["mode"]=="PUBLIC_RETROSPECTIVE_CPU" and d["source_commit"]==commit and d["status"]=="PASS"
            and digest(archive)==d["archive_sha256"], "Requires a replayed archive of registered public materials")
    require({i["entry"] for i in d["named_outputs"]}==set(PUBLIC) and d["reproduced_native_decisions"]>0 and
            d["published_claims"]>0, "Missing a deposit or its traceable decisions")


def clears_negative(fn, record):
    try:
        fn(record)
    except ValueError as err:
        return str(err)
    raise ValueError("An insufficient evidence packet was accepted")


def collect(dir):
    sha=read(dir/"source.json")["candidate"]
    require(re.fullmatch("[a-f0-9]{40}",sha) and subprocess.check_output(
        ["git","-C",ROOT,"merge-base",BASE,sha],text=True).strip()==BASE,"Unknown candidate source")
    result={}
    def verify(name, fn):
        try:
            result[name]={"status":"PASS","observed":fn()}
        except FileNotFoundError as exc:
            result[name]={"status":"NOT_RUN","reason":"Missing "+str(exc.filename)}
        except (ValueError, TypeError, KeyError, IndexError, OSError) as exc:
            result[name]={"status":"FAIL","reason":str(exc)}
    def command(folder,name,rev=sha,logname=None,exits=(0,)):
        d=read(folder/(name+".command.json"))
        require(d["exit_code"] in exits and d["source_commit"]==rev and d["argv"] and
                d["log_sha256"]==digest(folder/(logname or d["log"])), "Unverified command: "+name)
        return d
    def original():
        folder=dir/"historical"
        command(folder,"baseline",BASE)
        require(read(folder/"baseline.log")=={"V6.2.2":"PASS","V6.2C_OFFLINE":"PASS","V6.2C_PAID":"BLOCKED"},
                "Previous V6.2.2 release index no longer verifies in its original checkout")
        archives={}
        for group in ("standard","formal"):
            command(folder,group,BASE)
            d=read(folder/(group+".log")); old=ROOT/"acceptance/v622/receipts"/group
            require(d["source_commit"]==OLD_CANDIDATE and d["status"]=="PASS" and d["reproduced_slots"]==4
                    and d["archive_sha256"]==digest(old/"public-raw-replay.tar.gz")
                    and d["scores_sha256"]==digest(old/"scores.json"),"Original scores no longer reproduce")
            archives[group]=d["scores_sha256"]
        return archives
    def migration():
        old=read(dir/"upgrade/capture.json")
        check=read(dir/"upgrade/check.json")
        restored=read(dir/"upgrade/rollback.json")
        require(check["source_commit"]==sha and check["baseline"]==old["source_commit"]==restored["source_commit"]==BASE and
                old["run_id"]==check["run_id"]==restored["run_id"] and restored["candidate"]==sha and
                all(row["status"]=="PASS" and row["policy"]=="gx.evidence/6.2.2" for row in (old,check,restored)),
                "Run or policy changed during migration")
        require(check["report_replay"]["redeliveries"]==2 and check["additional_jobs"]==check["additional_turns"]==
                check["additional_charges"]==0 and check["original_jobs"]>=2 and old["scientific_sha256"]==
                restored["scientific_sha256"]==check["scientific_sha256"] and
                check["cold_reopens"]==[old["scientific_sha256"]]*2, "Cold open changed the original science")
        return {"run_id":old["run_id"],"jobs":check["original_jobs"],"turns":check["original_turns"],
                "scientific_sha256":check["scientific_sha256"]}
    def inherit():
        folder=dir/"compat";overall=command(folder,"inherited",exits=(0,1));command(folder,"v622")
        groups=read(folder/"groups.json")
        require(groups["source_commit"]==sha and not groups["working_tree_at_start"] and
                {r["group"] for r in groups["groups"]}=={"discovery","repairs","score-controls","archive","v6","a1","a2-through-a5","a6","a7","b4","c4"},
                "Missing legacy test group or clean source identity")
        original_failures=[g["group"] for g in groups["groups"] if g["status"]!="PASS"]
        require(original_failures==(["a7"] if overall["exit_code"] else []), "Additional unqualified inherited failures")
        for g in groups["groups"]:
            require(g["source_commit"]==sha and g["steps"] and all(digest(folder/s["log"])==s["log_sha256"]
                    and (s["exit_code"]==0 or g["group"] in original_failures) for s in g["steps"]),
                    "Unverified inherited group "+g["group"])
        followup=None
        if original_failures:
            step=read(folder/"a7-successor/receipt.json")
            qual=read(folder/"a7-successor/results/A7.json")
            quick=qual["quick"]
            require(step["source_commit"]!=sha and subprocess.check_output(["git","-C",ROOT,"merge-base",sha,
                    step["source_commit"]],text=True).strip()==sha and step["group"]=="a7" and step["status"]=="PASS"
                    and all(s["exit_code"]==0 and digest(folder/"a7-successor"/Path(s["log"]).name)==s["log_sha256"]
                            for s in step["steps"]) and qual["status"]=="PASS" and qual["case"]=="A7" and
                    quick["paid_model_calls"]==quick["cold_extra_jobs"]==quick["cold_extra_native_calls"]==0 and
                    {"historical_replay","evidence_policy"}.isdisjoint(quick["original_goal_fields"]),
                    "No successful original-schema A7 upgrade on the successor")
            followup={"source_commit":step["source_commit"],"status":qual["status"],"original_task":quick["original_task_id"],
                      "run_id":quick["run_id"],"original_driver":quick["original_driver_sha256"]}
        discovery=read(folder/"discovery.json")
        total=sum(v["tests"] for v in discovery.values())
        skipped=read(folder/"skipped.json")
        require(total>=646 and len(skipped)==sum(int(n) for g in discovery.values() for n in
                re.findall(r"skipped=(\d+)",g["unittest"])) and "Ran 37 tests" in
                (folder/"v622-repairs.log").read_text() and (folder/"v622-repairs.log").read_text().strip().endswith("OK"),
                "Incomplete discovery or old policy tests")
        return {"groups":len(groups["groups"]),"original_failures":original_failures,"a7_followup":followup,"discovered":total,"passed":total-len(skipped),"skipped":skipped,"v622":37}
    def cases():
        eng=dir/"engineering";raw=command(eng,"product",exits=(0,1));command(eng,"endpoint-host")
        logs=(eng/"product.log").read_text()
        observed = {"original_candidate_exit":raw["exit_code"]}
        if raw["exit_code"]:
            require("FAILED (errors=1)" in logs and "The requested material role is absent" in logs and
                    "ERROR: test_accepted_stop_binds_initial_plan_and_wrong_comparison_rolls_back" in logs,
                    "Unqualified candidate regression")
            fixed = read(eng/"fixture-recheck.json")
            revised = (eng/"fixture-recheck.log").read_text()
            frozen_file = "acceptance/v623/test_decision_action.py"
            revised_blob = subprocess.check_output(["git","-C",ROOT,"show",fixed["fixture_commit"]+":"+frozen_file])
            original_blob = subprocess.check_output(["git","-C",ROOT,"show",sha+":"+frozen_file])
            require(fixed["runtime_commit"]==sha and fixed["exit_code"]==0 and
                    fixed["old_fixture_sha256"]==hashlib.sha256(original_blob).hexdigest() and
                    fixed["new_fixture_sha256"]==hashlib.sha256(revised_blob).hexdigest() and
                    fixed["changed_test_files"]==[frozen_file] and fixed["log_sha256"]==digest(eng/"fixture-recheck.log") and
                    revised.strip().endswith("OK") and "test_accepted_stop_binds_initial_plan_and_wrong_comparison_rolls_back" in revised,
                    "Fixture correction did not requalify the unchanged runtime")
            observed.update(fixture_commit=fixed["fixture_commit"],revised_test_count=int(re.search(r"\nRan (\d+) tests? in",revised)[1]))
        else:
            require(logs.strip().endswith("OK"),"Unfinished or skipped current regressions")
        e=read(eng/"E1.json");historical_claim(e)
        supplied=read(eng/"chronology/supplied.json")
        pair=supplied["historical_and_prospective"]
        require(pair["retrospective_data_refs"] and pair["prospective_data_refs"] and supplied["unknown"]["timing_unknown_data_refs"]
                and not supplied["both_retrospective"]["unmeasured_data_refs"] and
                len(supplied["both_retrospective"]["retrospective_data_refs"])==2, "Material states were collapsed")
        generated=read(eng/"chronology/generated.json"); job=read(eng/"E4-job.json")
        require(generated["variants"] and generated["seed"] is not None and job["jobs_after_reconcile"]==1
                and job["cold_coverage"][0]["qualification"]=="prospective", "Recoverable prospective Job proof is missing")
        host=read(eng/"endpoint-host.json"); delivered(host)
        require(host["source_commit"]==sha,"Native delivery trace from a different candidate")
        c=read(eng/"revisions/revision-conflict.json");inv=read(eng/"inventory/nonprotein-inventory.json")
        require(c["operations"] and all(o["exit_code"]!=0 for o in c["operations"]) and not c["observations"]
                and inv["real_cpu_jobs"]==1 and inv["module"]["version"]=="3.0.0", "Revision or nonprotein inventory proof missing")
        required={"E1":"E1.json","E2":"chronology/intrinsic.json","E3":"chronology/cross-run.json","E4":"E4-job.json",
                  "D1":"endpoint-host.json","D2":"delivery/explicit-unread.json","C1":"inventory/nonprotein-inventory.json"}
        a=deepcopy(e); a["external"]["level"]="limited_support"
        b=deepcopy(host); b["official_model_request_tool_result"]={}
        observed["weak_evidence_rejected"]={"post_hoc_support":clears_negative(historical_claim,a),
                                            "missing_native_read":clears_negative(delivered,b)}
        return {**observed,"tests":int(re.search(r"\nRan (\d+) tests",logs)[1]),"cases":{k:{"file":"engineering/"+f,"sha256":digest(eng/f)}
                    for k,f in required.items()},"controlled_run":e["run_id"],"native_host_run":host["goal_run"]}
    def live():
        d=read(dir/"live/verification.json");h=read(dir/"live/hard-preflight.json")
        f=read(dir/"live/provider-preflight.json")
        require(d["source_commit"]==sha and h["candidate"]==sha and f["candidate"]==sha and
                d["status"]=="PASS" and d["paid_inference_requests"]==d["actual_live_slots_started"]==0 and
                set(d["original_runs"])=={"01","02","03","04"}, "Expected all four original loopback Runs")
        require(d["physical_totals"]["admitted"]>0 and d["physical_totals"]["unresolved_requests"]==0 and
                d["physical_totals"]["held_tokens"]==0 and h["status"]==f["status"]=="BLOCKED" and
                "HARD_CAP_UNAVAILABLE" in h["reason_codes"] and "AUTHORIZATION_MISSING" in h["reason_codes"],
                "A loopback funding receipt cannot attest production capacity or spending approval")
        replay=read(dir/"live/replay.json")
        require(replay["source_commit"]==sha and replay["original_runs"]==4 and replay["packets_reproduced"]==24 and
                replay["archive_sha256"]==digest(dir/"live/public-loopback.tar.gz") and
                replay["verification_sha256"]==digest(dir/"live/verification.json"), "Raw four-slot replay differs")
        g=read(dir/"live/funding-tests.json"); owner=read(dir/"live/installed-owner.json")
        require(g["candidate"]==owner["candidate"]==sha and g["status"]=="PASS" and g["passed"]==g["tests"]>=12
                and g["failed"]==0 and owner["fixture_sends"]==1 and owner["paid_provider_requests"]==0,
                "Missing positive owner and bounded concurrency controls")
        term=read(dir/"live/terminal-crash.json"); pause=read(dir/"live/pause-crash-command.json")
        require(term["source_commit"]==sha and term["before_sha256"]==term["after_sha256"] and
                term["duplicate_charges"]==term["duplicate_jobs"]==term["duplicate_requests"]==0 and
                term["paid_provider_calls"]==0 and pause["source_commit"]==sha, "Missing native cold-resume receipts")
        successor=read(dir/"live/pause-successor.json")
        require(successor["source_commit"]!=sha and successor["status"]=="PASS" and successor["operator_pause_resumed"]
                and successor["before_sha256"]==successor["after_sha256"] and successor["duplicate_charges"]==
                successor["duplicate_jobs"]==successor["duplicate_requests"]==0, "Follow-up pause resume is unproven")
        return {"rehearsal":"PASS","reason_codes":h["reason_codes"],"terminal_crash":"PASS",
                "successor_pause":{"status":"PASS","source_commit":successor["source_commit"],"run_id":successor["run_id"]},
                "paused_crash":"PASS" if pause["exit"]==0 else "FAIL","hard_live_started":0,"hard_live_planned":4,
                "review":d["independent_science_grades"]}
    def study():
        d=read(dir/"study/replay.json");public_method(d,sha,dir/"study/gx-study.zip")
        proof=read(dir/"independent/result.json")
        cells={(x["entry"],x["mask"]):(x["job_id"],x["material_sha256"],x["output_sha256"]) for x in d["named_outputs"]}
        require(len(cells)==4 and proof["run_id"]==d["run_id"] and proof["status"]=="PASS" and
                {p["entry"]:p["sha256"] for p in proof["registered_inputs"]}==PUBLIC, "Original sources or study Run differ")
        for item in proof["covered"]:
            name=(item["entry"],item["mask"])
            require(item["status"]=="PASS" and PUBLIC[name[0]]==item["input_sha256"] and cells[name]==
                    (item["job_id"],item["input_sha256"],item["product_sha256"]) and
                    read(dir/"independent"/item["comparison"])["status"]=="PASS", "Independent result changed")
        require({(x["entry"],x["mask"]) for x in proof["covered"]}==set(cells), "Missing independent numerical cell")
        synthetic={**d,"mode":"SYNTHETIC_FIXTURE"}
        negative=clears_negative(lambda r:public_method(r,sha,dir/"study/gx-study.zip"),synthetic)
        return {"run_id":d["run_id"],"actor_classification":d["mode"],"weak_evidence_rejected":negative,"reproduced_decisions":d["reproduced_native_decisions"],
                "native_publications":d["published_claims"],"archive":d["archive_sha256"]}
    for name,fn in (("v622_original_archives",original),("cold_upgrade_and_rollback",migration),
                    ("inherited_regressions",inherit),("v623_product",cases),("hard_pilot",live),("numeric_study",study)):
        verify(name,fn)
    def gate(names):
        values={result[n]["status"] for n in names}
        return "FAIL" if "FAIL" in values else "NOT_RUN" if "NOT_RUN" in values else "PASS"
    trees={name:subprocess.check_output(["git","-C",ROOT,"rev-parse",sha+":"+name],text=True).strip()
           for name in ("gx-h/gx_core","gx-h/contracts","gx-h/config","gx-h/plugins/installed",
                        "acceptance/v623/live","acceptance/v623/study")}
    release_paths={"baseline","compat","engineering","historical","independent","live","study","upgrade","source.json"}
    method=result["numeric_study"]["status"]
    hard=result["hard_pilot"]["status"]
    return {"schema":"gx.v623.acceptance/1","candidate":sha,"baseline":BASE,"input_git_objects":trees,"checks":result,
            "decisions":{"v623_research_correctness":gate(tuple(result)[:4]),
              "production_hard_entry":"BLOCKED" if hard=="PASS" else hard,
              "hard_public_live":"NOT_RUN" if hard=="PASS" else hard,"public_numeric_method":method,
              "research_answer":{"observational_analysis":method,"real_model_autonomy_in_this_cpu_run":"NOT_RUN"}},
            "files":{p.relative_to(dir).as_posix():digest(p) for p in sorted(dir.rglob("*"))
                     if p.is_file() and p.relative_to(dir).parts[0] in release_paths}}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("action",choices=("build","verify"))
    ap.add_argument("--evidence",type=Path,default=ROOT/"acceptance/v623/evidence")
    ap.add_argument("--require-complete",action="store_true")
    args=ap.parse_args(); directory=args.evidence.resolve(); out=collect(directory);path=directory/"index.json"
    if args.action=="build":
        path.write_text(json.dumps(out,indent=2,sort_keys=True,ensure_ascii=False)+"\n")
    else:
        require(read(path)==out,"Source, evidence or historical replay pins differ from index")
    print(json.dumps(out["decisions"],sort_keys=True))
    if args.require_complete and not all(r["status"]=="PASS" for r in out["checks"].values()):
        for key,r in out["checks"].items():
            if r["status"]!="PASS": print(key,r)
        raise SystemExit(1)


if __name__=="__main__":
    main()
