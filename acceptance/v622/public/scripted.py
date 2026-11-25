"""Scripted research choices and post-run checks for the four public engineering controls."""
from __future__ import annotations

import json
from pathlib import Path
from acceptance.v62c import pilot_peer as old


BEHAVIORS = {"standard", "direct", "late", "narrow", "unchanged"}
BRANCH_METHOD = "registered reference comparison"


def reference_branch(brief):
    return next((b for b in brief.get("branches", [])
                 if b.get("spec", {}).get("method_family") == BRANCH_METHOD), None)


def late_commission(brief, assigned):
    return "gx_open_module", {
        "module_id": "data_inventory", "module_version": "2", "round": 1,
        "operation_key": "commission-assigned-measurement",
        "focus": "Measure shared coordinate completeness in the supplied specimen.",
        "expected_delivery": "Identify the scoped source, named output and verified coverage.",
        "input_refs": [old.ref_for(brief, "workflow_spec"), assigned],
        "capabilities": [old.METHOD],
        "sub_budget": {"worker_tasks": 6, "api_calls": 8, "tokens": 320000,
                       "cpu_seconds": 45, "job_submissions": 1},
    }


def peer_step(brief, replies, behavior):
    """Choose tools from the researcher's actual material and native tool replies."""
    if behavior not in BEHAVIORS:
        raise ValueError(f"Unknown scripted research behavior {behavior!r}")
    # Present the same GX-verified readings to the existing study peer on either policy.
    packet = dict(brief)
    for key in ("module_reports", "observations"):
        packet[key] = [{**row, "metrics": row.get("verified_metrics", row.get("metrics", []))}
                       for row in brief.get(key, [])]
    name, args = old.step(packet, replies)
    if brief.get("module") or behavior == "standard":
        return name, args
    branch = reference_branch(brief)
    specimen = old.ref_for(brief, "structure")
    if not branch:
        if name == "gx_open_module":
            return old.decision([old.branch()], old.PLAN)
        return name, args
    if not specimen:
        return name, args

    # A new Lead can choose delegation after discovering what was actually assigned.
    specimen_sha = next(m["sha256"] for m in brief["materials"] if m["artifact_ref"] == specimen)
    target = next(((source, metric) for source, metric in old.current_result(packet)
                   if metric["scope"]["input_sha256"] == specimen_sha), None)
    if behavior == "late" and not target and not brief.get("module_reports") and name == "gx_decide_revision":
        if not brief.get("module_catalog"):
            raise ValueError("Late commission requires a visible module grant")
        prior = (brief.get("previous_decision") or {}).get("reconsider_when", [])
        opened = replies.get(("gx_open_module", None))
        watch = next((e for e in prior if e.get("event") == "module_report"), {})
        if not opened and not watch:
            return late_commission(brief, specimen)
        subject = opened["module_instance_id"] if opened else watch["subject_ref"]
        return old.decision([], "Await the commissioned measurement on this source.",
                            event="module_report", subject=subject)

    if name != "gx_decide_revision":
        return name, args
    decision = args["decision"]
    if behavior in ("direct", "narrow", "unchanged") and not target and not decision["ops"]:
        already_commissioned = any(
            n.get("commission", {}).get("inputs") == [specimen] and
            n["commission"].get("capability") == old.METHOD and
            n["commission"].get("parameters") == {"mask": "common_backbone"}
            for n in brief.get("nodes", []))
        if not already_commissioned:
            op = old.measure(branch["branch_id"], specimen, "assay")
            return old.decision([op], "Measure the assigned source directly using the registered endpoint.",
                                event="job_settled", subject=op["node"])
    if target and any(op["op"] == "stop_branch" for op in decision["ops"]):
        source, _metric = target
        if behavior == "narrow":
            claim = replies["gx_stage_claim", None]["claim_ref"]
            why = ("Shared backbone coverage is below the registered threshold. End this comparison here "
                   "and restrict the conclusion to coordinate completeness in the supplied file.")
            op = {"op": "close_run", "claim_refs": [claim], "reason": why,
                  "unresolved": ["The planned complete-backbone comparison lacks coverage; "
                                 "the unobserved positions and their biological effects remain undetermined."]}
            return old.decision([op], why, [source])
        if behavior == "unchanged":
            op = old.measure(branch["branch_id"], old.ref_for(brief, "reference"), "comparison")
            return old.decision([op], "CHANGE: The delivered coverage falls below the threshold. "
                                "I describe this as a change of plan.", decision["evidence_refs"],
                                event="job_settled", subject=op["node"])
    return name, args


def verify_standard(work):
    """Fixed positive controls are checked after the generic batch finishes all its enrolled Runs."""
    work = Path(work)
    scores = json.loads((work / "scores.json").read_bytes())
    results = json.loads((work / "result.json").read_bytes())
    slots = ("01", "02", "03", "04")
    if set(scores["cases"]) != set(slots):
        raise ValueError("Standard rehearsal requires the four distinct registered public assignments")
    checks = {}
    for slot, condition, delegated in zip(slots, ("MAINTAIN","CHANGE","CHANGE","MAINTAIN"),
                                          (False,True,False,True), strict=True):
        scored = scores["cases"][slot]
        if (not scored["science_pass"] or scored["opportunity"] != "DELIVERED" or
            scored["decision"] != condition or not scored["action_realized"] or
            scored["delegation"] != ("DEMONSTRATED" if delegated else "NOT_APPLICABLE")):
            raise ValueError(f"Scripted control diverged from its preregistered behavior in {slot}")
        observed = results["cases"][slot]
        checks[slot] = {"run_id": scored["run_id"], "condition": condition, "delegation": scored["delegation"],
                        "opportunity": scored["opportunity"], "status": observed["status"]}
    return {"classification":"SCRIPTED_REHEARSAL", "standard_controls":checks, "paid_model_calls":0}
