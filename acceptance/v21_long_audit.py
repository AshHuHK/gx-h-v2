"""Read-only evidence and metric extraction for the two-run official staged acceptance."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def percentile(samples, field):
    values = sorted(r[field] for r in samples if r.get(field) is not None)
    if not values:
        return {"n":0}
    return {**{p:round(values[max(0,math.ceil(len(values)*q)-1)],4) for p,q in (
        ("p50",.5),("p95",.95),("p99",.99),("max",1))}, "n":len(values)}


def distribution(values):
    return dict(Counter(values))


def scan(conn, sql, args=()):
    return [dict(row) for row in conn.execute(sql,args)]


def seconds(start, end):
    return (datetime.fromisoformat(end.replace("Z","+00:00"))-datetime.fromisoformat(start.replace("Z","+00:00"))).total_seconds()


def audit(site, traffic, rounds):
    result = {"runs":{}}
    with closing(site.db()) as db:
        workspace = db.execute("SELECT project_ref,budget_ref FROM p0_workspace").fetchone()
        result["workspace"] = dict(workspace)
        exposures = []
        for rid, budget, version, pin in db.execute("SELECT run_id,budget_ref,workflow_version,workflow_sha256 FROM runs ORDER BY rowid"):
            run = result["runs"][rid] = {"budget_ref":budget,"workflow":"science_grid@"+version,"workflow_sha256":pin}
            teams = scan(db,"SELECT team_id,status,budget_ref,max_parallel_roots FROM p0_teams WHERE run_id=?",(rid,))
            require(len(teams)==1,"missing configured team")
            team, = teams
            run["team"] = team
            roots = scan(db,"SELECT r.thread_id,r.task_id,r.attempt,r.lease_id,r.role,t.attempts_consumed,t.status,a.team_id FROM p0_roots r LEFT JOIN tasks t ON t.task_id=r.task_id LEFT JOIN p0_task_teams a ON a.task_id=t.task_id WHERE r.run_id=?",(rid,))
            require(len(roots)==2*rounds and len({r['thread_id'] for r in roots})==len(roots),"duplicate or missing worker threads")
            workers = {r["task_id"]:r for r in roots if r["task_id"]}
            require(all(r["attempt"]==1 and r["attempts_consumed"]==0 and r["team_id"]==team["team_id"] for r in workers.values()),"retry consumed or worker belongs to different team")
            run.update(root_count=len(roots),attempts_consumed=sum(w["attempts_consumed"] for w in workers.values()))
            tasks = scan(db,"SELECT s.task_id,s.result,s.submission_id,s.lease_id,l.principal,l.issued_at,s.created_at,t.stage FROM submissions s JOIN tasks t USING(task_id) JOIN leases l ON s.lease_id=l.lease_id WHERE t.run_id=? ORDER BY s.rowid",(rid,))
            require(len(tasks)==2*rounds and all(t["result"]=="ACCEPTED" and t["principal"]==workers[t["task_id"]]["thread_id"] for t in tasks),"submission ownership or completeness")
            run["accepted_by_stage"] = distribution(r["stage"] for r in tasks)
            ages = [seconds(t["issued_at"],t["created_at"]) for t in tasks]
            run["longest_owned_submission_seconds"] = round(max(ages),3)
            jobs = scan(db,"SELECT job_id,idempotency_key,scheduler_ref,status,measured_json,job_attempt FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))
            require(len(jobs)==rounds+1 and all(j["status"]=="SUCCEEDED" and j["scheduler_ref"] and j["job_attempt"]==1 for j in jobs),"missing or failed local CPU jobs")
            require(all(len({j[col] for j in jobs})==len(jobs) for col in ("job_id","idempotency_key","scheduler_ref")),"duplicate job/operation/scheduler identity")
            run["jobs"] = [{k:v for k,v in j.items() if k!="measured_json"} for j in jobs]
            run["job_usage"] = [json.loads(j["measured_json"]) for j in jobs]
            arts = scan(db,"SELECT kind,artifact_ref,sha256,namespace,rel_path,bytes FROM artifacts WHERE run_id=?",(rid,))
            reports = []
            by_ref = {a["artifact_ref"]:a for a in arts}
            def read(ref):
                material=by_ref[ref]
                return json.loads((site.state/"artifacts"/material["namespace"]/material["rel_path"]).read_bytes())
            for a in arts:
                payload = (site.state/"artifacts"/a["namespace"]/a["rel_path"]).read_bytes()
                require(len(payload)==a["bytes"] and hashlib.sha256(payload).hexdigest()==a["sha256"],"artifact mismatch")
                if a["kind"]=="research_report":
                    reports.append((a,json.loads(payload)))
            require(len(reports)==1,"one research report per run")
            a,report = reports[0]
            run.update(artifacts_rehashed=len(arts),report_ref=a["artifact_ref"],report_sha256=a["sha256"],stop_reason=report["stop_reason"])
            numerical = []
            for entry in report["rounds"]:
                prop=read(entry["proposal_ref"])
                evidence=read(entry["evidence_ref"])
                require(evidence["proposal_ref"]==entry["proposal_ref"],"route and frozen proposal disagree")
                for route in evidence["routes"]:
                    observed=read(route["observations_ref"])
                    points=observed["observations"]
                    numeric_input=read(route["input_ref"])
                    require([(p["candidate_id"],p["x"]) for p in points]==
                            [(p["candidate_id"],p["x"]) for p in numeric_input["candidates"]],"measured candidates differ")
                    require(all(p["score"]==-(p["x"]-2)**2 for p in points),"independent score recomputation")
                    evaluation=read(route["evaluation_ref"])
                    threshold=prop["threshold"]
                    above=sum(p["score"]>=threshold for p in points)
                    require(evaluation["input_ref"]==route["observations_ref"] and
                            evaluation["input_sha256"]==by_ref[route["observations_ref"]]["sha256"] and
                            evaluation["threshold"]==threshold and evaluation["count_at_or_above"]==above and
                            evaluation["max_score"]==max(p["score"] for p in points),"incorrect threshold evaluation")
                    numerical.append({"round":entry["round"],"route":route["path_id"],"n":len(points),
                                      "threshold":threshold,"max_score":evaluation["max_score"],"at_or_above":above})
            run["numerical_evaluations"] = numerical
            run["points_recomputed"] = sum(row["n"] for row in numerical)
            decisions = scan(db,"SELECT round_index,actions_json,decision_ref,submission_id,reviewer_submission_id FROM science_decisions WHERE run_id=? ORDER BY round_index",(rid,))
            require([d["decision_ref"] for d in decisions]==[r["artifact_ref"] for r in report["rounds"]] and len(decisions)==rounds,"round history mismatch")
            actions = [[a["action"] for a in json.loads(d["actions_json"])] for d in decisions]
            require(sorted(actions[0])==["continue","prune"] and actions[1]==["pivot"] and actions[-1]==["stop"],"reviewed branch, pivot and stop")
            run["actions_by_round"] = actions
            settlements = scan(db,"SELECT job_id,eval_id,kind,direction,submission_id,manifest_hash FROM science_settlements WHERE run_id=? ORDER BY round_index,path_id",(rid,))
            require(len(settlements)==len(jobs) and {s["job_id"] for s in settlements}=={j["job_id"] for j in jobs} and all(s["kind"]=="valid_scientific_outcome" and s["eval_id"] and s["manifest_hash"] for s in settlements),"a local job lacks evaluated outcome")
            run["scientific_outcomes"] = distribution(s["direction"] for s in settlements)
            hb = scan(db,"SELECT subject_id,actor,payload_json,ts FROM events WHERE run_id=? AND kind='task.lease_renewed' ORDER BY event_id",(rid,))
            run["lease_renewals"] = len(hb)
            measured = [(r["subject_id"],r["actor"]) for r in hb if r["subject_id"] in workers and r["actor"]=="codex:"+workers[r["subject_id"]]["thread_id"]]
            run["official_thread_renewals"] = len(measured)
            run["distinct_renewed_tasks"] = len({key for key,_ in measured})
            turn = scan(db,"SELECT n.*,b.created_at,b.settled_at FROM p0_meter_native_turns n JOIN budget_reservations b USING(reservation_ref) WHERE run_id=?",(rid,))
            require(len(turn)==len(roots) and all(r["budget_status"]=="SETTLED" and r["terminal_status"]=="completed" for r in turn),"missing or duplicate paid delivery")
            tcost = {r["thread_id"]:r for r in turn}
            require(all(tcost[w["thread_id"]]["budget_ref"]==team["budget_ref"] for w in workers.values()),"worker turns charged wrong team")
            run["native_turns"] = len(turn)
            run["turn_confidence"] = distribution(r["confidence"] for r in turn)
            run["adjustments"] = db.execute("SELECT count(*) FROM p0_meter_adjustments WHERE root_id IN (SELECT thread_id FROM p0_roots WHERE run_id=?)",(rid,)).fetchone()[0]
            # Budget settlement events exclude child funding transfers and therefore avoid hierarchical double counting.
            actual = scan(db,"SELECT json_extract(e.payload_json,'$.actual.tokens') AS tokens,json_extract(e.payload_json,'$.actual.api_calls') AS api_calls FROM events e JOIN budget_reservations b ON b.reservation_ref=json_extract(e.payload_json,'$.reservation_ref') JOIN p0_root_deliveries d ON d.reservation_ref=b.reservation_ref WHERE e.kind='budget.settled' AND d.run_id=?",(rid,))
            run["charged_native_tokens"] = sum(r["tokens"] or 0 for r in actual)
            run["charged_api_calls"] = sum(r["api_calls"] or 0 for r in actual)
            require(run["charged_api_calls"]==len(turn),"wrong turn count in settled debit events")
            for r in turn:
                exposures.extend([(r["created_at"],1,r["token_budget"],rid),(r["settled_at"],-1,-r["token_budget"],rid)])
        active = tokens = highest = concurrent = 0
        for at,sign,amount,rid in sorted(exposures):
            active+=sign
            tokens+=amount
            highest=max(highest,tokens)
            concurrent=max(concurrent,active)
        result["peak_held_native_tokens"] = highest
        result["max_simultaneous_native_reservations"] = concurrent
        require(concurrent==2,"two runs never held simultaneous native turns")
        result["provider_limits"] = scan(db,"SELECT provider,bucket,source FROM p0_provider_limits")
        result["waits"] = scan(db,"SELECT reason,count(*) AS count FROM p0_attempt_waits GROUP BY reason")
        result["foreign_key_violations"] = len(list(db.execute("PRAGMA foreign_key_check")))
        require(result["foreign_key_violations"]==0,"foreign-key violation")
        result["original_business_hashes"] = business_hashes(db)
    result["requests"] = len(traffic)
    result["requests_with_errors"] = sum("error" in r for r in traffic)
    result["observed_peers"] = sorted({r["peer"] for r in traffic})
    require(result["observed_peers"]==["127.0.0.1"] and result["requests_with_errors"]==0,"provider endpoint or fixture failure")
    require(sum(r["charged_native_tokens"] for r in result["runs"].values())==35*len(traffic),"ledger did not reconcile every scripted response")
    by_kind=defaultdict(list)
    with (site.work/"metrics.jsonl").open() as log:
        for line in log:
            data=json.loads(line)
            by_kind[data["kind"]].append(data)
    result["metrics"]={k:{"count":len(v), "duration_ms":percentile(v,"duration_ms")} for k,v in by_kind.items()}
    result["db_lock_acquire_ms"] = percentile(by_kind["db_transaction"],"lock_acquire_ms")
    return result


def business_hashes(conn):
    """Compare complete journal rows across cold terminal replay, excluding diagnostic telemetry."""
    tables = ("runs","p0_workspace_runs","p0_teams","p0_task_teams","jobs","p0_execution_manifests",
              "tasks","leases","submissions","science_decisions","science_settlements","artifacts","evaluations",
              "p0_roots","p0_root_calls","p0_root_deliveries","p0_root_turn_usage","p0_meter_turns",
              "p0_meter_adjustments","p0_attempt_waits","budgets","budget_reservations")
    digests={}
    for name in tables:
        records = [json.dumps(tuple(r), separators=(",",":"),ensure_ascii=False) for r in conn.execute("SELECT * FROM "+name)]
        ordered = "\n".join(sorted(records)).encode()
        digests[name] = {"rows":len(records),"sha256":hashlib.sha256(ordered).hexdigest()}
    return digests


def compare_install(stage, package):
    """Hash product sources in an isolated staged installation and the later checked-out candidate."""
    import subprocess
    roots=("bin","codex_adapter","gx-h/gx_core","gx-h/workflows","gx-h/config","gx-h/contracts",
           "gx-h/adapters","gx-h/runtime","gx-h/examples")
    def inventory(base):
        files={}
        for top in roots:
            for path in (base/top).rglob("*"):
                if path.is_file() and not any(p in {"__pycache__","node_modules",".pytest_cache"} for p in path.parts):
                    files[str(path.relative_to(base))]=hashlib.sha256(path.read_bytes()).hexdigest()
        return files
    old,new=inventory(stage),inventory(package)
    def digest(files):
        return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    changed={k:{"long_stage":old.get(k),"checkout":new.get(k)} for k in sorted(old.keys()|new.keys()) if old.get(k)!=new.get(k)}
    return {"staged_product_inventory_sha256":digest(old),"current_product_inventory_sha256":digest(new),
            "compared_directories":roots,"staged_files":len(old),"checkout_files":len(new),
            "checkout_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=package,text=True).strip(),
            "tracked_product_changes":subprocess.check_output(["git","status","--short","--",*roots],cwd=package,text=True).splitlines(),
            "changed_files":changed}


if __name__=="__main__":
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser(description="Compare the long run's actual staged code with the current product")
    parser.add_argument("--stage",type=Path,required=True,help="The isolated cohort's install directory")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    pkg=Path(__file__).resolve().parents[1]
    args.output.write_text(json.dumps(compare_install(args.stage.resolve(),pkg),sort_keys=True,indent=2)+"\n")
