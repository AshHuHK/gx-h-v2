"""Verify the frozen source inputs, public receipts and file inventory for the five paid arms."""
import json
from pathlib import Path
import tempfile

from runner import RELEASE, save, sha, stage_source

BASE = Path(__file__).resolve().parent
ARMS = ("b0", "b1", "b2-legacy-instruction", "b2", "b3")
PATCHES = {"b0": BASE / "b0-telemetry.patch",
           "b2-legacy-instruction": RELEASE / "acceptance/v31/b2/legacy-gx-instructions.patch"}


def load(path):
    return json.loads(path.read_bytes())


def main():
    comparison = load(BASE / "comparison.json")
    source_records = {}
    with tempfile.TemporaryDirectory(prefix="gx-v31-paid-snapshot-check-") as private:
        for arm in ARMS:
            directory = BASE / arm
            env, doc = load(directory / "environment.json"), load(directory / "measured.json")
            patches = [PATCHES[arm]] if arm in PATCHES else []
            stage = stage_source(RELEASE, env["source_commit"], Path(private) / arm, patches)
            assert stage["stage_sha256"] == env["stage_sha256"] and stage["overlays"] == env["overlays"], arm
            assert doc["finished"] and doc["completed_runs"] == env["waves_planned"] * len(env["lanes"]), arm
            assert doc["all_observed_provider_response_ids_retained"] and doc["all_workspace_ledgers_reconciled"], arm
            assert all(sha(directory / name) == value for name, value in doc["raw_files"].items()), arm
            totals = doc["totals"]
            assert totals["signed_scientific_finding_errors"] == totals["provenance_issues"] == 0, arm
            assert totals["unsettled_turns"] == totals["duplicate_jobs"] == totals["duplicate_product_receipts_or_refs"] == 0, arm
            commands = [json.loads(line) for line in (directory / "commands.jsonl").read_text().splitlines()]
            peers = {peer for cmd in commands for peer, count in cmd["clash_established_socket_samples"].items() if count}
            assert peers and peers <= {"127.0.0.1:7890", "127.0.0.1:7891"}, arm
            receipts = [cmd["label"] for cmd in commands if cmd["exit_code"] != 0]
            source_records[arm] = {**stage, "acquisition_runner_sha256": env["acquisition_runner_sha256"],
                "official_binary_sha256": env["binary_sha256"], "plugin_sha256": load(directory / "plugin.json")["sha256"],
                "profile_sha256": env["profile_sha256"], "workspaces": env["waves_planned"], "runs": doc["completed_runs"],
                "observed_codex_socket_peers": sorted(peers), "interrupted_launcher_invocations": receipts,
                "charged_tokens": totals["tokens"], "provider_observed_tokens": totals["provider_rollout_tokens"],
                "estimated_turns": totals["estimate_settlements"], "private_source_and_state": env["private"]}
    for pair in comparison["contrasts"].values():
        assert all(row["same_grid_inputs_observations_and_frozen_evaluations"] and row["same_accepted_review_and_planner_dispositions"]
                   for row in pair["per_run"]), pair["candidate"]
    for arm, filename in (("b1", "checkpoint-12.json"), ("b3", "first-wave.json")):
        checkpoint = load(BASE / arm / filename)
        assert len(checkpoint["response_attribution"]) == sum(v["official_completed_responses"] for v in checkpoint["per_run"])
        assert checkpoint["completed"] == 2 and {r["kind"] for r in checkpoint["receipts"]} >= {
            "run", "official_session", "artifact", "recomputed_numerical_result"}
    dependencies = [RELEASE / "acceptance/v3/performance" / name for name in
                    ("run_workload.py", "audit_workload.py", "grid_equivalence.py", "p1/summary.json",
                    "p1/workspace-receipts.json", "p1/receipts.jsonl.gz", "p1/native-session-hashes.json",
                    "p1/project-specifications.jsonl.gz")]
    dependencies.extend([PATCHES["b2-legacy-instruction"], RELEASE / "acceptance/v31/attribution/historical-attribution.json",
                         RELEASE / "acceptance/v31/attribution/historical-responses.jsonl.gz"])
    output = BASE / "evidence-index.json"
    files = {str(f.relative_to(BASE)): {"bytes": f.stat().st_size, "sha256": sha(f)}
             for f in sorted(BASE.rglob("*")) if f.is_file() and f != output and "__pycache__" not in f.parts}
    save(output, {"verified": True, "source_snapshots_rebuilt_from_git_and_named_test_overlays": source_records,
        "captured_dependency_sha256": {str(p.relative_to(RELEASE)): sha(p) for p in dependencies}, "files": files,
        "ledger": comparison["new_acquisition_spend"], "source_of_truth": {
            "cost": "GX workspace budget ledger. The 07-east missing final completion is retained at its original estimated charge.",
            "provider": "Pinned official Codex session rollouts with provider token usage, cross-checked against App Server observations.",
            "science": "Original public project specifications, immutable Jobs/Artifacts/evaluations and accepted reviewer/planner dispositions.",
            "last_b1_wave": "b1/checkpoint-12.json", "final_paired_preload_wave": "b3/first-wave.json",
            "candidate_revisions": "Every measured source archive is identified by its original commit and content hash; raw estimates and completed work are preserved."}})


if __name__ == "__main__":
    main()
