"""Audit real GX and Codex receipts, without exporting prompts, identity or credentials."""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import shutil

from run_workload import EFFORT, LANES, MODEL, WAVE_OFFSETS, save, sha

TOKEN_FIELDS = ("input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens",
                "reasoning_output_tokens","total_tokens")


def stamp(value):
    return datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()


def utc(t):
    return datetime.fromtimestamp(t,tz=timezone.utc).isoformat()


def quantile(numbers):
    vals = sorted(n for n in numbers if n is not None)
    if not vals:
        return {"n":0}
    return {"n":len(vals),**{key:round(vals[max(0,math.ceil(frac*len(vals))-1)],4) for key,frac in
            (("p50",.5),("p95",.95),("p99",.99),("max",1))}}


def occupancy(intervals):
    edges=defaultdict(int)
    for start,end in intervals:
        if end >= start:
            edges[start]+=1
            edges[end]-=1
    by_active=Counter()
    active = peak = 0
    ordered=sorted(edges)
    for i,t in enumerate(ordered):
        active += edges[t]
        peak=max(peak,active)
        if i+1<len(ordered):
            by_active[active]+=ordered[i+1]-t
    return {"intervals":len(intervals),"peak_concurrent":peak,"start":utc(ordered[0]) if ordered else None,
            "end":utc(ordered[-1]) if ordered else None,
            "span_s":round(ordered[-1]-ordered[0],3) if ordered else 0,
            "busy_s":round(sum(v for k,v in by_active.items() if k>0),3),
            "worker_seconds":round(sum(k*v for k,v in by_active.items()),3),
            "seconds_by_concurrent_count":{str(k):round(v,3) for k,v in sorted(by_active.items())}}


def rows(c,sql,args=()):
    return [dict(r) for r in c.execute(sql,args)]


def db_file(p):
    c=sqlite3.connect(p.resolve().as_uri()+"?mode=ro",uri=True,timeout=8)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def write_compressed(path,records):
    # A stable gzip header lets a repeated read-only audit of the same evidence reproduce its bytes.
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="",fileobj=raw,mode="wb",mtime=0,compresslevel=6) as gz:
            with io.TextIOWrapper(gz,encoding="utf-8",newline="\n") as text:
                for row in records:
                    text.write(json.dumps(row,sort_keys=True,separators=(",",":"))+"\n")


def records_in(path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def labeled(label,values):
    return [dict(kind=label,data=v) for v in values]


def read_artifact(state,row):
    path=state/"artifacts"/row["namespace"]/row["rel_path"]
    if not path.resolve().is_relative_to((state/"artifacts").resolve()):
        raise ValueError("Artifact path is outside the staged store")
    return path.read_bytes()


def verify_science(c,state,receipts,errors):
    artifacts=rows(c,"SELECT artifact_ref,run_id,job_id,kind,namespace,rel_path,bytes,sha256 FROM artifacts ORDER BY rowid")
    by_ref={row["artifact_ref"]:row for row in artifacts}
    bodies={}
    counts=Counter()
    for row in artifacts:
        data=read_artifact(state,row)
        digest=hashlib.sha256(data).hexdigest()
        if (len(data), digest)!=(row["bytes"],row["sha256"]):
            errors.append("artifact hash/size: "+row["artifact_ref"])
        if row["kind"] in {"research_evidence","research_proposal","grid_input","synthetic_grid","score_evaluation","research_report"}:
            bodies[row["artifact_ref"]]=json.loads(data)
        receipts.extend(labeled("artifact",[{"ref":row["artifact_ref"],"run_id":row["run_id"],"job_id":row["job_id"],
                        "artifact_kind":row["kind"],"bytes":len(data),"sha256":digest}]))
        counts["verified_artifact_bytes"]+=len(data)
        counts["verified_artifacts"]+=1
    for row in artifacts:
        if row["kind"]!="research_evidence":
            continue
        ref=row["artifact_ref"]
        evidence=bodies[ref]
        proposal=bodies[evidence["proposal_ref"]]
        for route in evidence["routes"]:
            if not route["observations_ref"] or not route["evaluation_ref"]:
                errors.append("missing numerical result: "+route["job_id"])
                continue
            observed=bodies[route["observations_ref"]]["observations"]
            target=bodies[route["input_ref"]]["candidates"]
            evaluation=bodies[route["evaluation_ref"]]
            cutoff=proposal["threshold"]
            maximum=max(p["score"] for p in observed)
            above=sum(p["score"]>=cutoff for p in observed)
            expected=[(p["candidate_id"],p["x"],-(p["x"]-2)**2) for p in target]
            if expected!=[(p["candidate_id"],p["x"],p["score"]) for p in observed]:
                errors.append("recomputed coordinates or scores: "+route["job_id"])
            if (evaluation["input_ref"]!=route["observations_ref"]
                    or evaluation["input_sha256"]!=by_ref[route["observations_ref"]]["sha256"]
                    or evaluation["threshold"]!=cutoff or evaluation["observations"]!=len(target)
                    or evaluation["count_at_or_above"]!=above or evaluation["max_score"]!=maximum):
                errors.append("frozen evaluation: "+route["job_id"])
            if not c.execute("SELECT 1 FROM evaluations WHERE eval_id=? AND run_id=?",
                             (route["eval_id"], row["run_id"])).fetchone():
                errors.append("missing evaluation owner: "+route["job_id"])
            receipts.extend(labeled("recomputed_numerical_result",[{"run_id":row["run_id"],"job_id":route["job_id"],
                "evidence_ref":ref,"evaluation_ref":route["evaluation_ref"],"eval_id":route["eval_id"],
                "threshold":cutoff,"coordinates":len(target),"at_or_above":above,"max_score":maximum}]))
            counts["evaluated_jobs"]+=1
            counts["recomputed_points"]+=len(target)
    return counts


def queue_events(c,state_name,receipts,errors):
    ready={}
    samples=[]
    claims=Counter()
    for row in rows(c,"SELECT ts,run_id,subject_id,prev_state,new_state FROM events WHERE kind='task.transition' ORDER BY event_id"):
        task=row["subject_id"]
        if row["new_state"]=="READY":
            ready[task]=row["ts"]
        if row["new_state"]=="CLAIMED":
            claims[task]+=1
            before=ready.pop(task,None)
            if before is None:
                errors.append("claim without preceding READY: "+task)
                continue
            duration=1000*(datetime.fromisoformat(row["ts"].replace("Z","+00:00"))-
                           datetime.fromisoformat(before.replace("Z","+00:00"))).total_seconds()
            samples.append(duration)
            receipts.extend(labeled("queue_ready_to_claim",[{"workspace":state_name,"task_id":task,"run_id":row["run_id"],
                "attempt_index":claims[task],"ready":before,"claimed":row["ts"],"duration_ms":round(duration,4)}]))
        elif row["prev_state"]=="READY" and row["new_state"]!="CLAIMED" and task in ready:
            receipts.extend(labeled("queue_ended_without_claim",[{"workspace":state_name,"task_id":task,
                "run_id":row["run_id"],"ready":ready.pop(task),"ended":row["ts"],"state":row["new_state"]}]))
    for task,at in ready.items():
        receipts.extend(labeled("queue_right_censored",[{"workspace":state_name,"task_id":task,"ready":at}]))
    return samples


def read_native_rollouts(work,official,children,errors):
    native=[]
    sources=[]
    unowned=[]
    for log in sorted((work/"codex-home/sessions").rglob("*.jsonl")):
        thread=None
        seen={}
        response_usage={}
        session_errors=Counter()
        for row in records_in(log):
            p=row.get("payload",{})
            if row["type"]=="session_meta":
                thread=p["id"]
            if row["type"]=="event_msg" and p.get("type") in {"error","stream_error","turn_aborted","task_aborted"}:
                session_errors[p["type"]]+=1
            turn=p.get("turn_id")
            if not turn:
                continue
            record=seen.setdefault(turn,{"thread_id":thread,"turn_id":turn})
            if row["type"]=="turn_context":
                record.update(model=p.get("model"),effort=p.get("effort"))
            if row["type"]=="event_msg":
                event=p.get("type")
                if event=="task_started":
                    record["started"]=row["timestamp"]
                elif event in {"task_complete","turn_aborted","task_aborted"}:
                    record.update(ended=row["timestamp"],native_terminal_event=event,
                                  native_duration_ms=p.get("duration_ms"),time_to_first_token_ms=p.get("time_to_first_token_ms"))
            if row["type"]=="token_usage_record":
                usage={k:p["usage"].get(k) or 0 for k in TOKEN_FIELDS}
                response=p["response_id"]
                if response in response_usage and usage!=response_usage[response][1]:
                    errors.append("inconsistent native response token counters: "+response)
                response_usage[response]=(turn,usage)
        if thread not in official and thread not in children:
            unowned.append(log.name)
            continue
        sources.append({"file":log.name,"thread_id":thread,"bytes":log.stat().st_size,"sha256":sha(log),"session_error_events":session_errors})
        for turn,entry in seen.items():
            own=[usage for id_,usage in response_usage.values() if id_==turn]
            entry.update(native_responses=len(own),tokens={k:sum(u[k] for u in own) for k in TOKEN_FIELDS},
                         actor="root" if thread in official else "child")
            if "started" in entry:
                native.append(entry)
    return native,sources,unowned


def pulse(work):
    data=Counter()
    by_status=Counter()
    for db in sorted(work.glob("state-*/gx.db")):
        with closing(db_file(db)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='p0_meter_native_turns'").fetchone():
                continue
            data["workspaces"]+=1
            for r in c.execute("SELECT status,count(*) FROM runs GROUP BY status"):
                by_status[r[0]]+=r[1]
            data["jobs_succeeded"]+=c.execute("SELECT count(*) FROM jobs WHERE status='SUCCEEDED'").fetchone()[0]
            data["turns_held"]+=c.execute("SELECT count(*) FROM p0_meter_native_turns WHERE budget_status='HELD'").fetchone()[0]
            data["turns_provider_confirmed"]+=c.execute("SELECT count(*) FROM p0_meter_native_turns WHERE confidence='PROVIDER_CONFIRMED'").fetchone()[0]
            data["settled_native_tokens"]+=c.execute("SELECT coalesce(sum(settled_tokens),0) FROM p0_meter_native_turns").fetchone()[0]
            data["rejected_submissions"]+=c.execute("SELECT count(*) FROM submissions WHERE result='REJECTED'").fetchone()[0]
    return {**data,"run_statuses":by_status,"checked_at":datetime.now(timezone.utc).isoformat()}


def acquire(work,out):
    finish=json.loads((out/"finished.json").read_bytes())
    setup=json.loads((out/"environment.json").read_bytes())
    errors=[]
    warnings=[]
    metrics=defaultdict(list)
    for row in records_in(work/"metrics.jsonl"):
        metrics[row["kind"]].append(row)
    with (out/"gx-metrics.jsonl.gz").open("wb") as raw:
        with gzip.GzipFile(filename="",fileobj=raw,mode="wb",mtime=0,compresslevel=6) as gz:
            with (work/"metrics.jsonl").open("rb") as original:
                shutil.copyfileobj(original,gz)
    receipts=[]
    workspaces=[]
    totals=Counter()
    distributions=defaultdict(Counter)
    ready_samples=[]
    meters=[]
    all_job_intervals=[]
    official={}
    children=set()
    end_states=[]
    source_hashes={}
    spec_hashes={}
    science_spec_hashes={}
    spec_extensions={}
    expected_fields={"project","objective","max_rounds","proposal","turn_token_budget","workflow","workflow_version",
                     "max_parallel_roots","workspace","workspace_budget","run_budget","teams"}
    for f in sorted((work/"specifications").glob("*.json")):
        spec_hashes[f.name]=sha(f)
        documents=json.loads(f.read_bytes())
        scientific_input={key:documents[key] for key in sorted(expected_fields)}
        scientific_bytes=json.dumps(scientific_input,sort_keys=True,separators=(",",":")).encode()
        science_spec_hashes[f.name]=hashlib.sha256(scientific_bytes).hexdigest()
        spec_extensions[f.name]={k:v for k,v in documents.items() if k not in expected_fields}
    for f in sorted((work/"install").rglob("*")):
        if f.is_file():
            source_hashes[str(f.relative_to(work/"install"))]=sha(f)
    save(out/"input-hashes.json",{"specifications":spec_hashes,"science_and_budget_fields":science_spec_hashes,
         "additional_spec_fields":spec_extensions,"source_files":source_hashes})
    for db in sorted(work.glob("state-*/gx.db")):
        state=db.parent
        with closing(db_file(db)) as c:
            check=c.execute("PRAGMA integrity_check").fetchone()[0]
            fks=rows(c,"PRAGMA foreign_key_check")
            if check!="ok" or fks:
                errors.append("SQLite integrity or foreign key: "+state.name)
            runs=rows(c,"SELECT run_id,run_key,status,workflow_id,workflow_version,workflow_sha256 FROM runs "
                        "JOIN p0_workspace_runs USING(run_id) ORDER BY run_key")
            roots=rows(c,"SELECT r.run_id,r.root_key,r.thread_id,r.task_id,r.role,r.attempt,r.lease_id,r.provider,"
                         "r.model,r.effort,r.tool_names_json,r.effective_profile_json,l.principal,l.issued_at,l.released_at "
                         "FROM p0_roots r LEFT JOIN leases l USING(lease_id) ORDER BY r.rowid")
            bills=rows(c,"SELECT m.run_id,m.thread_id,m.turn_id,m.reservation_ref,m.token_budget,m.budget_status,"
                        "m.terminal_status,m.terminal_at,m.confidence,m.total_tokens,m.measured_tokens,m.settled_tokens,"
                        "b.created_at,b.settled_at FROM p0_meter_native_turns m JOIN budget_reservations b USING(reservation_ref)")
            jobs=rows(c,"SELECT job_id,run_id,idempotency_key,scheduler_kind,scheduler_ref,job_attempt,exit_code,status,"
                        "capability_id,capability_version,created_at,submitted_at,finished_at,outcome_json,measured_json FROM jobs ORDER BY rowid")
            rootfund=c.execute("SELECT b.budget_ref,b.limits_json,b.spent_json,b.reserved_json FROM p0_workspace w "
                               "JOIN budgets b ON b.budget_ref=w.budget_ref").fetchone()
            spent=json.loads(rootfund["spent_json"])
            limit=json.loads(rootfund["limits_json"])
            reserved=json.loads(rootfund["reserved_json"])
            adjustments=rows(c,"SELECT root_id,delta,created_at FROM p0_meter_adjustments")
            childrows=rows(c,"SELECT child_id,root_id,owner_turn,metadata_verified FROM p0_native_child_meters")
            children.update(r["child_id"] for r in childrows)
            accepts=rows(c,"SELECT t.run_id,s.task_id,t.role,s.attempt,s.submission_id,s.result,s.lease_id,l.principal,s.created_at,"
                         "s.validation_json FROM submissions s JOIN tasks t USING(task_id) JOIN leases l ON l.lease_id=s.lease_id ORDER BY s.rowid")
            for s in accepts:
                validation=json.loads(s.pop("validation_json"))
                s["refusal_codes"]=[v.get("code") for v in validation.get("records",[]) if v.get("code")]
                distributions["submission"][s["result"]]+=1
                distributions["refusal_codes"].update(s["refusal_codes"])
            taskrows=rows(c,"SELECT task_id,run_id,role,status,attempt,attempts_consumed FROM tasks")
            waits=rows(c,"SELECT reason,started_at,ended_at,root_key FROM p0_attempt_waits")
            for t in taskrows:
                distributions["task_status"][t["status"]]+=1
                totals["attempts_consumed"]+=t["attempts_consumed"]
                totals["retried_tasks"]+=int(t["attempt"]>1)
            for t in waits:
                distributions["persisted_wait_reason"][t["reason"]]+=1
            for r in roots:
                if not r["task_id"]:
                    continue
                official[r["thread_id"]]=r
                r["advertised_tools"]=json.loads(r.pop("tool_names_json") or "[]")
                profile=json.loads(r["effective_profile_json"] or "{}")
                if (profile.get("provider"),profile.get("model"),profile.get("effort"))!=("openai",MODEL,EFFORT):
                    errors.append("unexpected effective Root profile: "+r["root_key"])
                if r["principal"]!=r["thread_id"]:
                    errors.append("claim principal mismatch: "+r["root_key"])
                distributions["root_role"][r["role"]]+=1
            for j in jobs:
                outcome=json.loads(j.pop("outcome_json") or "{}")
                measurement=json.loads(j.pop("measured_json") or "{}")
                manifest=c.execute("SELECT admission_json,admission_sha256,completion_sha256 FROM p0_execution_manifests "
                                   "WHERE job_id=?",(j["job_id"],)).fetchone()
                if manifest:
                    cap=json.loads(manifest["admission_json"])["capability"]
                    j.update(admission_sha256=manifest["admission_sha256"],completion_sha256=manifest["completion_sha256"],
                             admitted_capability=cap)
                    governed=c.execute("SELECT manifest_sha256,status FROM capabilities WHERE capability_id=? AND version=?",
                                       (cap["id"],cap["version"])).fetchone()
                    if not governed or governed["manifest_sha256"]!=cap["sha256"]:
                        errors.append("admitted capability differs from canonical registry: "+j["job_id"])
                    else:
                        j["canonical_capability_status"]=governed["status"]
                else:
                    errors.append("missing execution manifest: "+j["job_id"])
                j["cpu_seconds"]=measurement.get("cpu_seconds")
                j["wall_seconds"]=measurement.get("wall_seconds")
                j["network_mode"]=outcome.get("network",{}).get("mode")
                real=outcome.get("runner") or {}
                start,end=real.get("started_at"),real.get("finished_at")
                if start and end:
                    all_job_intervals.append((stamp(start),stamp(end)))
                j["sidecar_sha256"]={f.name:sha(f) for f in sorted((state/"jobs"/j["job_id"]).glob("*"))
                                      if f.is_file() and f.name in {"launch.claim","outcome.json","spec.json","runner.json","status.json"}}
                if j["status"]=="SUCCEEDED" and (j["exit_code"]!=0 or not j["scheduler_ref"] or "outcome.json" not in j["sidecar_sha256"]):
                    errors.append("missing Job receipt: "+j["job_id"])
                distributions["job"][j["status"]]+=1
                totals["job_retries"]+=max(j["job_attempt"]-1,0)
                totals["job_cpu_seconds"]+=j["cpu_seconds"] or 0
            duplicate_ops=c.execute("SELECT count(*)-count(distinct idempotency_key) FROM jobs").fetchone()[0]
            if duplicate_ops:
                errors.append("duplicated Job key: "+state.name)
            totals["duplicate_job_keys"]+=duplicate_ops
            for run in runs:
                distributions["run"][run["status"]]+=1
            for bill in bills:
                distributions["meter_confidence"][bill["confidence"]]+=1
                distributions["turn_terminal"][bill["terminal_status"] or "pending"]+=1
                distributions["budget_status"][bill["budget_status"]]+=1
            raw_resp=c.execute("SELECT count(*),coalesce(sum(tokens),0),coalesce(sum(conflicting),0) FROM p0_meter_responses").fetchone()
            totals["gx_provider_response_receipts"]+=raw_resp[0]
            totals["gx_provider_raw_reported_tokens"]+=raw_resp[1]
            totals["gx_conflicting_provider_response_receipts"]+=raw_resp[2]
            totals.update(verify_science(c,state,receipts,errors))
            ready_samples.extend(queue_events(c,state.name,receipts,errors))
            totals["runs"]+=len(runs)
            totals["roots"]+=len(roots)
            totals["billed_turns"]+=len(bills)
            totals["jobs"]+=len(jobs)
            totals["ledger_spent_tokens"]+=spent.get("tokens",0)
            totals["ledger_spent_api_calls"]+=spent.get("api_calls",0)
            totals["workspace_token_ceiling"]+=limit.get("tokens",0)
            totals["workspace_turn_ceiling"]+=limit.get("api_calls",0)
            totals["meter_signed_adjustments"]+=sum(a["delta"] for a in adjustments)
            totals["settled_native_tokens"]+=sum(b["settled_tokens"] or 0 for b in bills)
            totals["measured_native_tokens"]+=sum(b["measured_tokens"] or 0 for b in bills)
            totals["transport_recoveries"]+=c.execute("SELECT count(*) FROM events WHERE kind='p0.transport_recovery'").fetchone()[0]
            totals["outbox_with_error"]+=c.execute("SELECT count(*) FROM outbox WHERE last_error IS NOT NULL").fetchone()[0]
            totals["outbox_retries"]+=c.execute("SELECT coalesce(sum(max(attempts-1,0)),0) FROM outbox").fetchone()[0]
            totals["official_turn_failures"]+=sum(b["terminal_status"]=="failed" for b in bills)
            totals["scientific_decisions"]+=c.execute("SELECT count(*) FROM science_decisions").fetchone()[0]
            totals["scientific_settlements"]+=c.execute("SELECT count(*) FROM science_settlements").fetchone()[0]
            totals["reviewed_reports"]+=c.execute("SELECT count(*) FROM artifacts WHERE kind='research_report'").fetchone()[0]
            totals["unsettled_native_turns"]+=sum(b["budget_status"]=="HELD" for b in bills)
            totals["cost_adjustments"]+=len(adjustments)
            totals["provider_backoffs"]+=c.execute("SELECT count(*) FROM p0_provider_limits WHERE bucket LIKE 'turn:%' OR bucket LIKE 'start:%'").fetchone()[0]
            sidecars={f.name:sha(f) for f in sorted(state.glob("gx.db*")) if f.name!="gx.db-shm"}
            recovery_rows=rows(c,"SELECT run_id,ts,reason FROM events WHERE kind='p0.transport_recovery' ORDER BY event_id")
            for recovery in recovery_rows:
                next_useful=c.execute("SELECT min(submissions.created_at) FROM submissions JOIN tasks USING(task_id) "
                                      "WHERE run_id=? AND submissions.created_at>? AND result='ACCEPTED'",
                                      (recovery["run_id"],recovery["ts"])).fetchone()
                recovery["next_accepted_submission"]=next_useful[0] if next_useful else None
                if recovery["next_accepted_submission"]:
                    recovery["recovery_to_accepted_seconds"]=round(stamp(recovery["next_accepted_submission"])-stamp(recovery["ts"]),3)
            receipts.extend(labeled("transport_recovery",recovery_rows))
            for run in runs:
                rid=run["run_id"]
                paid=[m for m in bills if m["run_id"]==rid]
                owned=[t for t in accepts if t["run_id"]==rid]
                run["turns_by_confidence"]=dict(Counter(m["confidence"] for m in paid))
                by_confidence=Counter()
                for m in paid:
                    by_confidence[m["confidence"]]+=m["settled_tokens"] or 0
                run["settled_tokens_by_confidence"]=dict(by_confidence)
                run["settled_tokens"]=sum(m["settled_tokens"] or 0 for m in paid)
                run["submissions"]=dict(Counter(s["result"] for s in owned))
                run["jobs"]=dict(Counter(j["status"] for j in jobs if j["run_id"]==rid))
                run["job_attempts"]=sum(j["job_attempt"] for j in jobs if j["run_id"]==rid)
            workspaces.append({"workspace":state.name,"integrity":check,"foreign_key_violations":len(fks),"runs":runs,
                     "root_budget":{"ref":rootfund["budget_ref"],"limits":limit,"spent":spent,"reserved":reserved},
                     "sqlite_sha256":sidecars,"turns":len(bills),"jobs":len(jobs)})
            meters.extend(bills)
            end_states.extend(r["status"] for r in runs)
            for kind,group in (("run",runs),("task",taskrows),("root",roots),("turn_meter",bills),("job",jobs),("submission",accepts),
                               ("adjustment",adjustments),("child",childrows),("attempt_wait",waits)):
                receipts.extend(labeled(kind,group))
    native,session_hashes,unowned=read_native_rollouts(work,official,children,errors)
    totals["official_session_error_events"]=sum(sum(s["session_error_events"].values()) for s in session_hashes)
    if unowned:
        warnings.append(str(len(unowned))+" official session files had no bound GX Root or child")
    roots=[r for r in native if r["actor"]=="root"]
    intervals=[(stamp(r["started"]),stamp(r["ended"])) for r in roots if r.get("ended")]
    actual=occupancy(intervals)
    admissions=occupancy([(stamp(m["created_at"]),stamp(m["settled_at"])) for m in meters if m.get("settled_at")])
    journal={r["turn_id"]:r for r in roots}
    for m in meters:
        n=journal.get(m["turn_id"])
        if not n or not n.get("ended"):
            warnings.append("no complete official native interval for "+str(m["turn_id"]))
            continue
        if (n.get("model"),n.get("effort"))!=(MODEL,EFFORT):
            errors.append("unexpected official turn context: "+m["turn_id"])
    token_counts=Counter()
    for n in native:
        token_counts.update(n["tokens"])
    if totals["ledger_spent_tokens"]!=totals["settled_native_tokens"]+totals["meter_signed_adjustments"]:
        warnings.append("root ledger and turn settlements differ; check terminal funding and open holds")
    if totals["settled_native_tokens"]+totals["meter_signed_adjustments"]!=token_counts["total_tokens"]:
        warnings.append("GX settlements and observed native response token totals differ")
    commands=list(records_in(out/"commands.jsonl"))
    launches={v["label"]:v for v in commands if "command" in v}
    exits={v["label"]:v for v in commands if "exit_code" in v}
    command_failures=[(k,v["exit_code"]) for k,v in exits.items() if v["exit_code"]!=0]
    if command_failures:
        warnings.append("CLI failures: "+str(command_failures))
    app_intervals=[(stamp(v["at"]),stamp(exits[k]["ended"])) for k,v in launches.items()
                   if k.startswith("drive-") and k in exits]
    arrivals=list(records_in(out/"arrivals.jsonl"))
    completed=distributions["turn_terminal"]["completed"]
    elapsed=finish["elapsed_monotonic_s"]
    throughput={k:round(n*3600/elapsed,4) for k,n in {"completed_native_turns_per_hour":completed,
                        "completed_runs_per_hour":distributions["run"]["COMPLETED"],
                        "accepted_products_per_hour":distributions["submission"]["ACCEPTED"],
                        "succeeded_jobs_per_hour":distributions["job"]["SUCCEEDED"]}.items()}
    timing={k:quantile([r.get("duration_ms") for r in values]) for k,values in metrics.items() if any("duration_ms" in v for v in values)}
    timing["sqlite_write_lock"]=quantile(r.get("lock_acquire_ms") for r in metrics["db_transaction"])
    timing["ready_to_claim"]=quantile(ready_samples)
    timing["native_root_turn"]=quantile(r.get("native_duration_ms") for r in roots)
    timing["native_time_to_first_token"]=quantile(r.get("time_to_first_token_ms") for r in roots)
    callback_tools=defaultdict(list)
    for cb in metrics["p0_callback"]:
        callback_tools[cb.get("tool") or "(unspecified)"].append(cb.get("duration_ms"))
    arr_epochs=[]
    for i,entry in enumerate(arrivals):
        start=stamp(entry["utc"])
        end=stamp(arrivals[i+1]["utc"]) if i+1<len(arrivals) else stamp(finish["ended"])
        active=occupancy([(max(a,start),min(b,end)) for a,b in intervals if max(a,start)<min(b,end)])
        arr_epochs.append({"wave":entry["wave"],"utc":entry["utc"],"interval_s":round(end-start,3),
                           "native_root_busy_s":active["busy_s"],"peak_native_roots":active["peak_concurrent"]})
    totals["queue_right_censored"]=sum(r["kind"]=="queue_right_censored" for r in receipts)
    totals["queue_ended_without_claim"]=sum(r["kind"]=="queue_ended_without_claim" for r in receipts)
    dispatch_endpoints=defaultdict(list)
    for point in metrics.get("p0_dispatch",()):
        if point.get("task_id") and point.get("observed_ns"):
            dispatch_endpoints[point["task_id"]].append(point["observed_ns"])
    for values in dispatch_endpoints.values():
        values.sort()
    ready_probe_counts=[]
    for entry in receipts:
        if entry["kind"]!="queue_ready_to_claim":
            continue
        row=entry["data"]
        times=dispatch_endpoints.get(row["task_id"],())
        started=round(stamp(row["ready"])*1_000_000_000)
        ended=round(stamp(row["claimed"])*1_000_000_000)
        count=bisect_left(times,ended)-bisect_left(times,started)
        row["dispatches_returned_while_ready"]=count
        ready_probe_counts.append(count)
    totals["dispatches_returned_while_ready"]=sum(ready_probe_counts)
    totals["claimed_attempts"]=len(ready_probe_counts)
    receipts.extend(labeled("official_native_turn",native))
    write_compressed(out/"receipts.jsonl.gz",receipts)
    save(out/"native-session-hashes.json",session_hashes)
    save(out/"workspace-receipts.json",workspaces)
    complete=(setup.get("codex")=="codex-cli 0.169.0" and len(workspaces)==len(WAVE_OFFSETS)
              and len(spec_hashes)==len(WAVE_OFFSETS)*len(LANES) and actual["span_s"]>=7200
              and actual["peak_concurrent"]>=2 and completed>0 and len(roots)==len(meters)
              and all(ep["native_root_busy_s"]>0 for ep in arr_epochs) and not errors
              and totals["evaluated_jobs"]==sum(len(w["runs"])*2 for w in workspaces))
    result={"gate_measurement":"PASS" if complete else "INCOMPLETE","source_commit":setup["source_commit"],
        "software_under_test":"GX release archive and Codex 0.169.0","real_mode":True,"model":MODEL,"effort":EFFORT,
        "run_utc":{"first_release":finish["start"],"ended":finish["ended"],"elapsed_monotonic_s":round(elapsed,3)},
        "native_root_work":actual,"budget_reservations":admissions,"coordinator_command_intervals":occupancy(app_intervals),
        "cpu_job_execution":occupancy(all_job_intervals),"root_busy_fraction_of_elapsed":round(actual["busy_s"]/elapsed,5),
        "elapsed_without_active_native_root_s":round(elapsed-actual["busy_s"],3),"wave_activity":arr_epochs,
        "counts":dict(sorted(totals.items())),"distributions":dict(distributions),"native_response_tokens":dict(token_counts),
        "native_responses":sum(n["native_responses"] for n in native),"native_child_turns":len(native)-len(roots),
        "throughput":throughput,"latency_ms":timing,
        "dispatch_activity":{"total":len(metrics.get("p0_dispatch",())),"claims":len(ready_probe_counts),
            "dispatches_per_actual_claim":round(len(metrics.get("p0_dispatch",()))/len(ready_probe_counts),3) if ready_probe_counts else None,
            "returned_during_ready_to_claim_interval":sum(ready_probe_counts),
            "per_interval_count":quantile(ready_probe_counts)},
        "callback_per_tool_ms":{k:quantile(v) for k,v in sorted(callback_tools.items())},
        "gx_telemetry_events":{k:len(v) for k,v in metrics.items()},"command_exits":Counter(v["exit_code"] for v in exits.values()),
        "checks":{"all_scheduled_inputs_admitted":len(end_states)==len(WAVE_OFFSETS)*len(LANES),
                  "all_scientific_runs_completed":all(s=="COMPLETED" for s in end_states),
                  "all_native_reservations_settled":totals["unsettled_native_turns"]==0,
                  "all_expected_jobs_evaluated":totals["evaluated_jobs"]==len(WAVE_OFFSETS)*len(LANES)*2,
                  "native_vs_ledger_tokens_equal":totals["ledger_spent_tokens"]==token_counts["total_tokens"]},
        "errors":errors,"warnings":warnings,
        "raw_private_metrics_sha256":sha(work/"metrics.jsonl"),
        "evidence_sha256":{name:sha(out/name) for name in ("commands.jsonl","arrivals.jsonl","environment.json","oauth-readiness.json",
                "start.json","finished.json","gx-metrics.jsonl.gz","receipts.jsonl.gz","native-session-hashes.json","input-hashes.json","workspace-receipts.json")}}
    save(out/"summary.json",result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private",required=True,type=Path)
    p.add_argument("--evidence",type=Path)
    args=p.parse_args()
    if args.evidence:
        r=acquire(args.private,args.evidence)
        print(json.dumps({k:r[k] for k in ("gate_measurement","run_utc","checks","errors","warnings")},indent=2))
        if r["gate_measurement"]!="PASS":
            raise SystemExit(1)
    else:
        print(json.dumps(pulse(args.private),indent=2))


if __name__=="__main__":
    main()
