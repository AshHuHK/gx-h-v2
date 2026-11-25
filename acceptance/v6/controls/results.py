"""Publish per-trial evidence for completed controls, including scientific failures."""

import argparse
import json
from pathlib import Path

from fixture import V6, file_sha, read, save
from review import comparisons


def one(directory):
    window = read(directory / "window.json")
    snapshots = {
        key: read(directory / window[key] / "capture.json") for key in ("before", "settled", "reopened")
    }
    rows = snapshots["reopened"]["rows"]
    did = window["decision_ref"]
    (decision,) = [r for r in rows["v6_decisions"] if r["decision_ref"] == did]
    protocol = read(directory / "intervention.json")
    body = json.loads(decision["request_json"])["decision"]
    receipt = json.loads(decision["receipt_json"])
    claims = [json.loads(r["document_json"]) for r in rows["hypothesis_revisions"]]
    claims = [r for r in claims if r["decision_id"] == did]
    accepted = set(n["node_id"] for n in rows["v6_nodes"] if n["decision_ref"] == did)
    jobs = []
    for job in rows["jobs"]:
        (candidate,) = [n for n in rows["v6_nodes"] if n["job_id"] == job["job_id"]]
        registration = json.loads(job["submit_intent_json"])
        result_ref = candidate["result_ref"]
        (artifact,) = [a for a in rows["artifacts"] if a["artifact_ref"] == result_ref]
        original_result = directory / window["reopened"] / "artifacts" / artifact["sha256"]
        if file_sha(original_result) != artifact["sha256"]:
            raise ValueError("Job result content changed")
        jobs.append(
            {
                "job_id": job["job_id"],
                "node_id": candidate["node_id"],
                "decision_id": candidate["decision_ref"],
                "in_scored_decision": candidate["node_id"] in accepted,
                "operation_key": candidate["operation_key"],
                "branch_id": candidate["branch_id"],
                "capability": job["capability_id"] + "@" + job["capability_version"],
                "status": job["status"],
                "admission_request_sha256": registration["request_sha256"],
                "result_ref": result_ref,
                "result_sha256": artifact["sha256"],
                "resource_measurement": json.loads(job["measured_json"]),
                "reservation": job["reservation_ref"],
            }
        )
    identities = {(n["thread_id"], n["call_id"]): n for n in rows["p0_v6_native_calls"]}
    raw = json.loads(identities[(decision["thread_id"], decision["call_id"])]["request_json"])
    if raw["turnId"] != decision["turn_id"] or raw["arguments"]["decision"] != body:
        raise ValueError("GX native call differs from accepted decision")
    status = (
        "FAIL"
        if protocol["packet_id"] == "p63"
        and any(j["in_scored_decision"] and j["capability"].startswith("tl1a-geometry@") for j in jobs)
        else "REVIEW"
    )
    scope = next(r for r in rows["p0_roots"] if r["thread_id"] == decision["thread_id"])
    permissions = json.loads(scope["effective_profile_json"])
    costs = snapshots["reopened"]["costs"]
    return {
        "packet_id": protocol["packet_id"],
        "evidence_map": window["evidence_map"],
        "run_id": snapshots["reopened"]["run_id"],
        "decision_ref": did,
        "thread_id": decision["thread_id"],
        "turn_id": decision["turn_id"],
        "call_id": decision["call_id"],
        "model": permissions["model"],
        "effort": permissions["effort"],
        "provider": permissions["provider"],
        "independent_numeric_check": comparisons(read(V6 / "inputs/initial.json"), protocol),
        "native_tool_arguments": raw["arguments"],
        "adopted_base": receipt["base"],
        "adopted_head": receipt["result_head"],
        "claim_revisions": [
            {
                "claim_ref": c["claim_ref"],
                "claim_revision_id": c["claim_revision_id"],
                "science": c["science"],
                "scope": c["scope"],
                "statement": c["statement"],
                "limitations": c["limitations"],
            }
            for c in claims
        ],
        "jobs": jobs,
        "ledger_at_reopen": costs,
        "test_of_neutral_commissioning": status,
        "additional_turns": [
            {
                "thread_id": r["thread_id"],
                "turn_id": r["turn_id"],
                "confidence": r["confidence"],
                "reservation_ref": r["reservation_ref"],
                "budget_status": r["budget_status"],
            }
            for r in rows["p0_meter_native_turns"]
            if r["turn_id"] != decision["turn_id"]
        ],
        "evidence_sha256": {
            **{k: file_sha(directory / window[k] / "capture.json") for k in snapshots},
            "rollout": file_sha(Path(window["rollout"])),
            "resume_invocation": file_sha(directory / window["resume_receipt"]),
            "packet": file_sha(directory / "intervention.json"),
        },
        "source_review_dir": str(directory),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("review_dir", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    worlds = [one(args.review_dir / t["slot"]) for t in read(args.review_dir / "plan.json")["worlds"]]
    if len({w["thread_id"] for w in worlds}) != 3 or len({w["run_id"] for w in worlds}) != 3:
        raise ValueError("Required three independently scoped control sessions")
    save(
        args.out,
        {
            "version": 1,
            "data": "synthetic matched controls with real isolated model decisions and actual Jobs",
            "worlds": sorted(worlds, key=lambda w: w["packet_id"]),
        },
    )
