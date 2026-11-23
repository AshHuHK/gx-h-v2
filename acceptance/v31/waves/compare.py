"""Matched Run-level contrasts; retain campaign pacing and model uncertainty explicitly."""
from contextlib import closing
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

from runner import RELEASE, save, sha
from audit import wall
from audit_workload import quantile
from grid_equivalence import arm as scientific_results

HERE = Path(__file__).resolve().parent
HIST = RELEASE / "acceptance/v3/performance/p1"
PLAN = json.loads((HERE / "measurement-plan.json").read_bytes())


def stream(path):
    with gzip.open(path, "rt") as file:
        yield from (json.loads(line) for line in file)


def frozen_input(spec):
    fields = {k: v for k, v in spec.items() if k not in ("workflow_version", "plugins")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def scientific_signatures(work):
    return {key: {"routes": hashlib.sha256(json.dumps(row["routes"], sort_keys=True).encode()).hexdigest(),
                  "dispositions": {role: s["disposition"] for role, s in row["accepted"].items()}}
            for key, row in scientific_results(work).items()}


def historical():
    private = Path(json.loads((HIST / "start.json").read_bytes())["private_staging"])
    hashes = {row["workspace"]: row["sqlite_sha256"] for row in json.loads((HIST / "workspace-receipts.json").read_bytes())}
    original = {row["name"].removesuffix(".json"): frozen_input(row["specification"])
                for row in stream(HIST / "project-specifications.jsonl.gz")}
    assert original == PLAN["frozen_original_run_input_and_budgets_sha256_excluding_selected_version_and_plugin"]
    hist = json.loads((HIST / "summary.json").read_bytes())
    attr = json.loads((RELEASE / "acceptance/v31/attribution/historical-attribution.json").read_bytes())["arms"]["p1"]
    by_id = {}
    for row in stream(HIST / "receipts.jsonl.gz"):
        if row["kind"] == "run":
            run = row["data"]
            by_id[run["run_id"]] = {"run_id": run["run_id"], "run_key": run["run_key"], "status": run["status"],
                "tokens": run["settled_tokens"], "official_completed_responses": 0,
                "native_turns": sum(run["turns_by_confidence"].values())}
    for row in stream(RELEASE / "acceptance/v31/attribution/historical-responses.jsonl.gz"):
        if row["arm"] == "p1":
            by_id[row["run_id"]]["official_completed_responses"] += 1
    for name, expected in hashes.items():
        db = private / name / "gx.db"
        if any(sha(private / name / filename) != digest for filename, digest in expected.items()
               if filename == "gx.db" or (private / name / filename).exists()):
            raise ValueError("Original private workspace differs from its archived digest: " + name)
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            for rid, begin, end in conn.execute("SELECT run_id,created_at,updated_at FROM runs"):
                by_id[rid]["run_wall_seconds"] = wall(begin, end)
    runs = sorted(by_id.values(), key=lambda row: row["run_key"])
    return {"arm": "v3-p1", "per_run": runs, "science": scientific_signatures(private), "totals": {
        "tokens": hist["counts"]["ledger_spent_tokens"], "native_turns": hist["counts"]["billed_turns"],
        "official_completed_responses": attr["request_count"], "independent_review_decisions": hist["counts"]["scientific_decisions"],
        "first_reviews": sum(attr["reviewer_first_submissions"].values()),
        "first_reviews_rejected": attr["reviewer_first_submissions"].get("REJECTED", 0),
        "submitted_terminal_followon_class": attr["by_class"].get("POST_SUBMIT", {}).get("responses", 0),
        "estimate_settlements": hist["distributions"]["meter_confidence"].get("ESTIMATED", 0),
        "jobs_succeeded": hist["distributions"]["job"]["SUCCEEDED"], "observations_recomputed": hist["counts"]["recomputed_points"]},
        "first_request_input_tokens": attr["direct_rollout_first_inputs"], "request_classes": attr["by_class"],
        "callback_duration_ms": hist["callback_per_tool_ms"], "run_wall_seconds": quantile([r["run_wall_seconds"] for r in runs]),
        "campaign_wall_seconds": json.loads((HIST / "finished.json").read_bytes())["elapsed_monotonic_s"]}


def new(name):
    doc = json.loads((HERE / name / "measured.json").read_bytes())
    for row in doc["per_run"]:
        key = row["run_key"]
        spec = json.loads((HERE / name / ("spec-" + key + ".json")).read_bytes())
        expected = PLAN["frozen_original_run_input_and_budgets_sha256_excluding_selected_version_and_plugin"][key]
        if frozen_input(spec) != expected:
            raise ValueError("Frozen scientific inputs or original budgets changed: " + name + " " + key)
    doc["science"] = scientific_signatures(Path(doc["source"]["private"]))
    return doc


def paired(before, after):
    values = []
    before_map = {r["run_key"]: r for r in before["per_run"]}
    fields = ("tokens", "run_wall_seconds", "native_turns", "official_completed_responses")
    for current in after["per_run"]:
        key = current["run_key"]
        if key not in before_map:
            continue
        previous = before_map[key]
        values.append({"run_key": key, "reference_run_id": previous["run_id"], "candidate_run_id": current["run_id"],
                       "statuses": [previous["status"], current["status"]],
                       "same_grid_inputs_observations_and_frozen_evaluations":
                           before["science"][key]["routes"] == after["science"][key]["routes"],
                       "same_accepted_review_and_planner_dispositions":
                           before["science"][key]["dispositions"] == after["science"][key]["dispositions"],
                       **{field: {"before": previous[field], "after": current[field],
                                  "delta": round(current[field] - previous[field], 3)} for field in fields}})
    aggregate = {}
    for field in fields:
        b, a = (sum(v[field][k] for v in values) for k in ("before", "after"))
        aggregate[field] = {"before": round(b, 3), "after": round(a, 3), "delta": round(a-b, 3),
                            "change_percent": round(100 * (a-b) / b, 3) if b else None}
    result = {"reference": before["arm"], "candidate": after["arm"], "matched_runs": len(values),
              "matched_run_totals": aggregate, "per_run": values}
    if before["arm"] != "v3-p1":
        before_profiles, after_profiles = (official_settings(arm) for arm in (before, after))
        paired_roles = sorted(set(before_profiles) & set(after_profiles))
        result["official_session_controls"] = {key: {
            "before": before_profiles[key], "after": after_profiles[key],
            "same_base_instructions": before_profiles[key]["base"] == after_profiles[key]["base"],
            "same_registered_gx_schemas": before_profiles[key]["dynamic_tools"] == after_profiles[key]["dynamic_tools"],
            "same_persisted_additional_tool_catalogs": before_profiles[key]["additional_tools"] == after_profiles[key]["additional_tools"],
            "same_gx_tool_names": before_profiles[key]["tool_names"] == after_profiles[key]["tool_names"],
            "same_native_multiagent_version": before_profiles[key]["multi_agent"] == after_profiles[key]["multi_agent"],
            "same_gx_worker_prefix": before_profiles[key]["gx_prefix"] == after_profiles[key]["gx_prefix"]}
            for key in paired_roles}
    return result


def official_settings(arm):
    keys = {r["run_id"]: r["run_key"] for r in arm["per_run"]}
    profiles = {}
    for row in stream(HERE / arm["arm"] / "receipts.jsonl.gz"):
        if row["kind"] != "official_session" or row["role"] == "child":
            continue
        key = keys[row["run_id"]] + "/" + row["role"]
        profile = profiles.setdefault(key, {"base": set(), "dynamic_tools": set(), "additional_tools": set(), "multi_agent": set(),
                                            "gx_prefix": set(), "tool_names": set()})
        profile["base"].add(row["codex_base_instructions_sha256"])
        profile["dynamic_tools"].add(row["initial_dynamic_tools_sha256"])
        profile["additional_tools"].update(catalog["sha256"] for catalog in row["persisted_additional_tools"])
        profile["multi_agent"].add(str(row["multi_agent_version"]))
        profile["gx_prefix"].update(row["gx_developer_prefix_sha256"])
        profile["tool_names"].update(".".join(filter(None, (t["namespace"], t["tool"])))
                                     for t in row["initial_dynamic_tools"])
    return {key: {field: sorted(values) for field, values in profile.items()} for key, profile in profiles.items()}


def paid_study_overlap(acquisition_names):
    quality = []
    for freeze in sorted((RELEASE / "acceptance/v31/quality").glob("*/freeze.json")):
        if json.loads(freeze.read_bytes()).get("mode") != "live":
            continue
        commands = freeze.parent / "commands.jsonl"
        if not commands.exists():
            continue
        for cmd in map(json.loads, commands.read_text().splitlines()):
            if "run" in cmd["argv"] and cmd.get("wall_seconds"):
                end = datetime.fromisoformat(cmd["at"]).timestamp()
                quality.append((freeze.parent.name, end-cmd["wall_seconds"], end))
    overlaps = []
    for arm in acquisition_names:
        commands = HERE / arm / "commands.jsonl"
        if not commands.is_file():
            continue
        for cmd in map(json.loads, commands.read_text().splitlines()):
            if not cmd["label"].startswith("drive-"):
                continue
            end = datetime.fromisoformat(cmd["at"]).timestamp()
            start = end - cmd["wall_seconds"]
            for quality_arm, other_start, other_end in quality:
                seconds = min(end, other_end) - max(start, other_start)
                if seconds > 0:
                    overlaps.append({"acquisition": arm, "driver": cmd["label"], "quality_acquisition": quality_arm,
                                     "driver_window_overlap_seconds": round(seconds, 3)})
    return overlaps


if __name__ == "__main__":
    sources = {"v3-p1": historical()}
    for name in ("b0", "b1", "b2-legacy-instruction", "b2", "b3"):
        if (HERE / name / "measured.json").exists():
            sources[name] = new(name)
    contrasts = {label: paired(sources[old], sources[current])
        for label, old, current in (
            ("historical_to_b1", "v3-p1", "b1"),
            ("role_tools_and_delegation_with_legacy_instructions", "b1", "b2-legacy-instruction"),
            ("instruction_only", "b2-legacy-instruction", "b2"),
            ("combined_role_policy_and_gx_instructions", "b1", "b2"),
            ("immutable_preload", "b2", "b3"))
        if old in sources and current in sources}
    paid = [doc for name, doc in sources.items() if name != "v3-p1"]
    ledger = {"scope": "New execution-efficiency acquisitions only. The archived historical comparison and separately owned scientific-quality Runs are excluded.",
              "runs": sum(len(doc["per_run"]) for doc in paid),
              "charged_tokens": sum(doc["totals"]["tokens"] for doc in paid),
              "provider_observed_tokens_lower_bound": sum(doc["totals"]["provider_rollout_tokens"] for doc in paid),
              "estimated_turn_charges": sum(doc["totals"]["estimate_settlements"] for doc in paid),
              "unsettled_native_turns": sum(doc["totals"]["unsettled_turns"] for doc in paid),
              "maximum_admitted_tokens_sum_of_original_run_limits": sum(
                  doc["source"]["budget"]["maximum_run_tokens_this_acquisition"] for doc in paid)}
    save(HERE / "comparison.json", {"matched_inputs": "Coordinates, candidates, thresholds, proposal text and resource limits compare exactly to the 26 historical P1 Run specifications after removing workflow_version and plugins.",
          "temporal_metric": "Sum of matched per-Run created_at -> final updated_at intervals (overlapping west/east are not campaign wall). Final Run wall and profile are reported separately per Run.",
          "uncertainty": "The 13 historical workspaces were open-loop at ten-minute offsets. Current workspaces release after the prior driver's exit. No campaign-throughput treatment claim. Single paired-workspace treatments have model and provider timing variation.",
          "new_acquisition_spend": ledger, "other_known_paid_driver_overlap": paid_study_overlap(sources),
          "acquisitions": {k: {f: v[f] for f in ("totals", "first_request_input_tokens", "request_classes", "callback_duration_ms",
                            "run_wall_seconds", "campaign_wall_seconds")} for k, v in sources.items()},
          "contrasts": contrasts, "comparison_plan_sha256": sha(HERE / "measurement-plan.json")})
