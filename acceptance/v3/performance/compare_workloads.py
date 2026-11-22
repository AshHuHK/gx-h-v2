"""Compare matched real acquisitions. Flag bounds for investigation and retain numerical denominators."""
import argparse
from collections import Counter
import json
from pathlib import Path

from run_workload import save


def read(folder,name):
    return json.loads((folder/name).read_bytes())


def diff(base,candidate):
    if base is None or candidate is None:
        return {"baseline":base,"candidate":candidate,"absolute":None,"percent":None}
    return {"baseline":base,"candidate":candidate,"absolute":round(candidate-base,5),
            "percent":round((candidate-base)*100/base,4) if base else None}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline",required=True,type=Path)
    p.add_argument("--candidate",required=True,type=Path)
    p.add_argument("--output",required=True,type=Path)
    args=p.parse_args()
    base,cand=args.baseline,args.candidate
    bs,cs=[read(d,"summary.json") for d in (base,cand)]
    bh,ch=[read(d,"input-hashes.json") for d in (base,cand)]
    be,ce=[read(d,"environment.json") for d in (base,cand)]
    identical=["binary_sha256","codex","model","provider","effort","python","sqlite","os","kernel","machine",
               "cpu","logical_cpus","cpu_affinity","memory_kib","clash_endpoints","wave_offsets_seconds",
               "lanes","workspace_root_cap","team_root_cap"]
    invariants={k:be[k]==ce[k] for k in identical}
    invariants["scientific_budget_input_sha_by_run_key"]=bh["science_and_budget_fields"]==ch["science_and_budget_fields"]
    invariants["baseline_spec_extensions_empty"]=all(not ext for ext in bh["additional_spec_fields"].values())
    treatment=read(cand,"treatment.json") if (cand/"treatment.json").is_file() else {}
    cases=treatment.get("runs",[])
    expected_keys={name.removesuffix(".json") for name in ch["specifications"]}
    invariants["plugin_treatment_observed_for_every_candidate_run"]=bool(treatment.get("public_selection")) and (
        {v.get("run_key") for v in cases}==expected_keys and all(v.get("verified") is True for v in cases))
    invariants["candidate_extra_fields_match_declared_plugin_selection"]=bool(treatment) and all(
        extras==treatment.get("expected_extra_spec_fields") for extras in ch["additional_spec_fields"].values())
    invariants["distinct_product_commits"]=be["source_commit"]!=ce["source_commit"]
    invariants["native_multi_root_span_both"]=all(s["native_root_work"]["span_s"]>=7200 and
            s["native_root_work"]["peak_concurrent"]>=2 for s in (bs,cs))
    invariants["all_measurement_checks"]=all(s["gate_measurement"]=="PASS" for s in (bs,cs))
    for k in ("all_expected_jobs_evaluated","all_native_reservations_settled","native_vs_ledger_tokens_equal"):
        invariants[k]=bs["checks"][k] and cs["checks"][k]
    affected=[]
    latency={}
    for field in sorted(set(bs["latency_ms"])|set(cs["latency_ms"])):
        b=bs["latency_ms"].get(field,{})
        c=cs["latency_ms"].get(field,{})
        delta={k:diff(b.get(k),c.get(k)) for k in ("n","p50","p95","p99","max")}
        d=delta["p95"]
        if field in {"p0_dispatch","p0_callback","db_transaction"} and d["absolute"] is not None and (
            d["absolute"]>5 and d["percent"] is not None and d["percent"]>20):
            affected.append({"metric":field,"observation":"p95 increased over 20% and 5 ms","delta":d})
        latency[field]=delta
    throughput={k:diff(bs["throughput"].get(k),cs["throughput"].get(k))
                for k in sorted(set(bs["throughput"])|set(cs["throughput"]))}
    for k in ("completed_native_turns_per_hour","completed_runs_per_hour"):
        if throughput[k]["percent"] is not None and throughput[k]["percent"] < -10:
            affected.append({"metric":k,"observation":"rate decreased over 10%","delta":throughput[k]})
    fields=("settled_native_tokens","meter_signed_adjustments","unsettled_native_turns","attempts_consumed",
            "retried_tasks","transport_recoveries","provider_backoffs","job_retries","duplicate_job_keys",
            "official_turn_failures","verified_artifacts","evaluated_jobs","recomputed_points","scientific_decisions")
    counts={k:diff(bs["counts"].get(k,0),cs["counts"].get(k,0)) for k in fields}
    case=[]
    bruns={r["run_key"]:r for w in read(base,"workspace-receipts.json") for r in w["runs"]}
    cruns={r["run_key"]:r for w in read(cand,"workspace-receipts.json") for r in w["runs"]}
    for key in sorted(set(bruns)|set(cruns)):
        b=bruns.get(key,{})
        c=cruns.get(key,{})
        case.append({"run_key":key,"status":[b.get("status"),c.get("status")],
            "tokens":diff(b.get("settled_tokens"),c.get("settled_tokens")),
            "accepted_products":diff(b.get("submissions",{}).get("ACCEPTED",0),c.get("submissions",{}).get("ACCEPTED",0)),
            "rejected_products":diff(b.get("submissions",{}).get("REJECTED",0),c.get("submissions",{}).get("REJECTED",0)),
            "jobs":[b.get("jobs"),c.get("jobs")]})
    callback_deltas={}
    for tool in sorted(set(bs["callback_per_tool_ms"])|set(cs["callback_per_tool_ms"])):
        b=bs["callback_per_tool_ms"].get(tool,{})
        c=cs["callback_per_tool_ms"].get(tool,{})
        callback_deltas[tool]={k:diff(b.get(k),c.get(k)) for k in ("n","p50","p95","p99")}
    baseline_waves={wave["wave"]:wave for wave in bs["wave_activity"]}
    wave_deltas=[]
    for c in cs["wave_activity"]:
        b=baseline_waves.get(c["wave"],{})
        match={"wave":c["wave"],"root_busy_seconds":diff(b.get("native_root_busy_s"),c.get("native_root_busy_s"))}
        for k in ("db_transaction","p0_dispatch","p0_callback"):
            previous=b.get("gx_elapsed_window_latency_ms",{}).get(k,{})
            following=c.get("gx_elapsed_window_latency_ms",{}).get(k,{})
            match[k+"_p95_ms"]=diff(previous.get("p95"),following.get("p95"))
        wave_deltas.append(match)
    dispositions=Counter()
    for entry in case:
        dispositions[tuple(entry["status"])]+=1
    report={"source_commits":[bs["source_commit"],cs["source_commit"]],"predeclared_regression":"p95 >20% AND >5 ms, volume loss >10%",
            "input_checks":invariants,"specification_extensions_by_arm":{"baseline":bh["additional_spec_fields"],
                "candidate":ch["additional_spec_fields"]},"elapsed_s":diff(bs["run_utc"]["elapsed_monotonic_s"],cs["run_utc"]["elapsed_monotonic_s"]),
            "native_active_seconds":diff(bs["native_root_work"]["busy_s"],cs["native_root_work"]["busy_s"]),
            "native_peak_root_concurrency":diff(bs["native_root_work"]["peak_concurrent"],cs["native_root_work"]["peak_concurrent"]),
            "throughput":throughput,"latency_ms":latency,"counts":counts,"per_run":case,
            "matched_arrival_windows":wave_deltas,"callbacks_by_tool_ms":callback_deltas,
            "paired_run_status_counts":[{"baseline":k[0],"candidate":k[1],"count":v} for k,v in dispositions.items()],
            "thresholds_to_explain":affected,"unmatched_or_failed_inputs":[k for k,v in invariants.items() if not v],
            "comparison_status":"REVIEW" if affected or not all(invariants.values()) else "WITHIN_MEASURED_BOUNDS"}
    save(args.output,report)
    print(json.dumps({k:report[k] for k in ("comparison_status","thresholds_to_explain","unmatched_or_failed_inputs")},indent=2))


if __name__=="__main__":
    main()
