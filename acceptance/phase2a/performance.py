"""Summarize actual Phase 2A cross-process receipts. Requires no live transport."""

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path


METRICS = {"workflow_ready_scan", "p0_driver_cycle", "job_reconcile", "db_transaction"}
PHASES = (("steady", "first"), ("restart", "first"), ("restart", "recovery"), ("restart", "idle"))
SQLITE_TIMES = ("duration_ms", "lock_wait_ms", "body_ms", "finish_ms")


def quantiles(values):
    """Nearest-rank sample percentiles, matching the V3 workload audit."""
    values = sorted(values)
    if not values:
        return {"n": 0}
    if not all(math.isfinite(value) and value >= 0 for value in values):
        raise ValueError("negative or non-finite measurement")
    return {
        "n": len(values),
        "p50": round(values[math.ceil(.50 * len(values)) - 1], 6),
        "p95": round(values[math.ceil(.95 * len(values)) - 1], 6),
        "max": round(values[-1], 6),
    }


def transaction_summary(writes):
    """Reduce measured writer attempts. Historical rows have only total and acquire."""
    groups = defaultdict(list)
    modern = []
    legacy = 0
    for row in writes:
        if "transaction_owner" not in row:
            legacy += 1
            continue
        groups[row["transaction_owner"], row["operation"]].append(row)
        modern.append(row)

    def distribution(rows):
        return {
            "attempts": len(rows),
            "outcomes": dict(Counter(row["commit_or_rollback"] for row in rows)),
            **{field: quantiles(row[field] for row in rows) for field in SQLITE_TIMES},
        }

    fields = ("transaction_owner", "operation", "run_id", "observed_ns", "commit_or_rollback",
              "rows_changed", *SQLITE_TIMES)
    return {
        "all": distribution(modern),
        "legacy_rows_without_phase_labels": legacy,
        "by_owner_operation": [
            {"transaction_owner": owner, "operation": operation, **distribution(rows)}
            for (owner, operation), rows in sorted(groups.items())
        ],
        "slow_samples": {
            field: [{key: row[key] for key in fields}
                    for row in sorted(modern, key=lambda r: r[field], reverse=True)[:5]]
            for field in SQLITE_TIMES
        },
    }


def summarize(samples):
    def durations(kind):
        return [s["duration_ms"] for s in samples if s["kind"] == kind]

    scans = [s for s in samples if s["kind"] == "workflow_ready_scan"]
    jobs = [s for s in samples if s["kind"] == "job_reconcile"]
    writes = [s for s in samples if s["kind"] == "db_transaction"]
    counts = Counter((s["ready_count"], s["parked_count"]) for s in scans)
    return {
        "ready_scan_ms": quantiles(durations("workflow_ready_scan")),
        "ready_and_parked_per_scan": [
            {"ready": pair[0], "parked": pair[1], "scans": n}
            for pair, n in sorted(counts.items())
        ],
        "selected_nodes": dict(sorted(Counter(
            s["selected_node"] for s in scans if s["selected_node"] is not None).items())),
        "driver_cycle_ms": quantiles(durations("p0_driver_cycle")),
        "job_reconcile_ms": quantiles(durations("job_reconcile")),
        "job_reconcile_with_changes_ms": quantiles(
            s["duration_ms"] for s in jobs if s["changed_jobs"] > 0),
        "job_reconcile_no_open_jobs_ms": quantiles(
            s["duration_ms"] for s in jobs if s["open_jobs"] == 0),
        "job_observations": {
            "open_rows_inspected": sum(s["open_jobs"] for s in jobs),
            "status_changes": sum(s["changed_jobs"] for s in jobs),
            "deferred_rows": sum(s["deferred_jobs"] for s in jobs),
        },
        "sqlite_writer_total_ms": quantiles(s["duration_ms"] for s in writes),
        "sqlite_writer_acquire_ms": quantiles(s["lock_acquire_ms"] for s in writes),
        "sqlite_writer_hold_through_commit_or_rollback_ms": quantiles(
            s["duration_ms"] - s["lock_acquire_ms"] for s in writes),
        **({"sqlite_transactions": transaction_summary(writes)} if any(
            "transaction_owner" in s for s in writes) else {}),
    }


def read_samples(case, trial, observed):
    rows = []
    for scenario, phase in PHASES:
        path = case / scenario / f"metrics-{phase}.jsonl"
        cut = observed["cases"][scenario]["cut_ns"]
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            row = json.loads(line)
            if row["kind"] in METRICS:
                # Timestamps mark emission. Exclude any call possibly spanning host shutdown.
                if row["observed_ns"] - math.ceil(row["duration_ms"] * 1_000_000) < cut:
                    continue
                rows.append({**row, "trial": trial, "scenario": scenario, "phase": phase,
                             "source_line": line_no})
    return rows


def case_facts(observed):
    idle = observed["idle"]
    if idle["wall_seconds"] <= 0:
        raise ValueError("idle observation has no elapsed time")
    wakes = idle["sleep_wakeups"]
    completed = observed["offline_deterministic_completions"]
    # Mechanical nodes in static_case.install. A2 is deliberately blocked by the planner.
    mechanical = {"A2", "B2", "B3", "C1"}
    return {
        "source": observed["source"],
        "runs": {scenario: facts["run_id"] for scenario, facts in observed["cases"].items()},
        "offline_completed_nodes": completed,
        "offline_mechanical_completions": {case: [node for node in nodes if node in mechanical]
                                           for case, nodes in completed.items()},
        "codex_spawns_after_stop": observed["codex_spawns_after_stop"],
        "duplicates": observed["duplicates"],
        "idle": {**{key: value for key, value in idle.items() if key != "sleep_wakeups"},
                 "cpu_percent_of_one_core": 100 * idle["process_cpu_seconds"] / idle["wall_seconds"],
                 "driver_cycles_per_second": idle["driver_cycles"] / idle["wall_seconds"],
                 "sleep_wakeups": len(wakes), "sleep_wakeups_per_second": len(wakes) / idle["wall_seconds"],
                 "actual_sleep_ms": quantiles(wake["elapsed_seconds"] * 1000 for wake in wakes)},
    }


def write_records(path, rows):
    with path.open("wb") as output:
        with gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0) as zipped:
            for row in rows:
                zipped.write((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return {"file": path.name, "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="cross_process.py case directories or metrics files")
    parser.add_argument("--transaction-metrics", action="store_true",
                        help="summarize writer attempts from GX metrics JSONL or JSONL.GZ files")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [p.resolve() for p in args.inputs]
    if len(cases) != len(set(cases)):
        parser.error("each input must be distinct")
    if args.transaction_metrics:
        writes = []
        for path in cases:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as stream:
                writes.extend(row for line in stream if (row := json.loads(line))["kind"] == "db_transaction")
        result = transaction_summary(writes)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return

    samples = []
    observations = []
    trials = []
    for trial, case in enumerate(cases, 1):
        observed = json.loads((case / "observations.json").read_text())
        trials.append(case_facts(observed))
        observations.append({"trial": trial, "observation": observed})
        samples.extend(read_samples(case, trial, observed))
    commits = {t["source"]["commit"] for t in trials}
    run_ids = [rid for t in trials for rid in t["runs"].values()]
    if len(commits) != 1 or len(set(run_ids)) != len(run_ids):
        raise ValueError("need independent Runs at the same commit")
    summaries = {"all": summarize(samples)}
    for scenario, phase in PHASES:
        summaries[f"{scenario}/{phase}"] = summarize([
            s for s in samples if (s["scenario"], s["phase"]) == (scenario, phase)])
    missing = METRICS - {s["kind"] for s in samples}
    if missing:
        raise ValueError(f"missing owner timings: {sorted(missing)}")
    if not summaries["all"]["job_reconcile_with_changes_ms"]["n"]:
        raise ValueError("missing a measured reconciliation which changed a Job")
    args.output.mkdir(parents=True, exist_ok=True)
    result = {
        "evidence_class": "SCRIPTED",
        "independent_runs": len(run_ids),
        "trials": trials,
        "window": "Only calls whose estimated start and observed end follow confirmed host stop.",
        "latency_unit": "milliseconds",
        "estimator": "nearest rank ceil(p*n), taken over individual calls",
        "raw_owner_samples": write_records(args.output / "owner-timings.jsonl.gz", samples),
        "raw_case_observations": write_records(args.output / "case-observations.jsonl.gz", observations),
        "distributions": summaries,
    }
    (args.output / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
