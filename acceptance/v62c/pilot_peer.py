"""Public scripted investigator. Inputs are the actual native brief and granted tool responses."""
import json

METHOD = "deposited-chain-geometry@2"
PLAN = "Plan to compare common-backbone positions with the archived reference. Wait for the newly assigned specimen and measure its completeness before committing the reference Job."
KEEP = "The delivered completeness passes the registered threshold. Maintain the comparison and commission the planned reference measurement."
CHANGE = "The delivered completeness is below the registered threshold. Withdraw the comparison because shared backbone positions insufficiently cover the assigned specimen."


def decision(ops, why, refs=(), event="evidence_added", subject="run"):
    return "gx_decide_revision", {"decision": {"mode": "amend" if ops else "resolve", "ops": ops, "rationale": why,
            "evidence_refs": list(refs), "reconsider_when": [{"event": event, "subject_ref": subject}]}}


def measure(owner, ref, alias, mask="common_backbone"):
    return {"op":"add_node","node":"local:"+alias,"branch":owner,"capability":METHOD,"inputs":[ref],
            "parameters":{"mask":mask},"depends_on":[],"question":"Measure shared backbone completeness in the scoped source."}


def ref_for(brief, role):
    return next((r["artifact_ref"] for r in brief["materials"] if r["role"] == role), None)


def branch():
    return {"op":"open_branch","branch":"local:reference","hypothesis":"The proposed common-backbone comparison is informative.",
            "alternative":"Insufficient shared coverage restricts the comparison.","method_family":"registered reference comparison",
            "discriminating_observation":"Shared complete backbone positions relative to the observed universe.",
            "budget_limits":{"job_submissions":2,"cpu_seconds":60},
            "stop_condition":"Withdraw when the assigned source does not support this comparison."}


def current_result(brief):
    items = [(r.get("report_ref"),r) for r in brief.get("module_reports",[])]
    items += [(o.get("artifact_ref"),o) for o in brief.get("observations",[])]
    for source,item in items:
        for metric in item.get("metrics",[]):
            if metric.get("metric_id")=="coverage":
                yield source,metric


def op_read(ref):
    return "gx_read_artifact", {"ref":ref,"operation_key":"inspect-"+ref}


def step(brief, replies):
    assigned = ref_for(brief, "structure")
    workflow = ref_for(brief, "workflow_spec")
    module = brief.get("module")
    branches = [b for b in brief.get("branches",[]) if b.get("spec",{}).get("method_family") == "registered reference comparison"]
    if module:
        if not assigned:
            return decision([],"Await the operator's assigned material in this scope.")
        if ("gx_read_artifact",assigned) not in replies:
            return op_read(assigned)
        nodes = brief["nodes"]
        if not nodes:
            op=measure(module["branch_id"], assigned, "assay")
            return decision([op],"Measure the newly received material with the registered method.",event="job_settled",subject=op["node"])
        node=nodes[0]
        if not node.get("result_ref"):
            return decision([],"Await the admitted Job.",event="job_settled",subject=node["node_id"])
        ref=node["result_ref"]
        if ("gx_read_artifact",ref) not in replies:
            return op_read(ref)
        return "gx_submit_module_report", {"operation_key":"report-measurement","status":"COMPLETE",
               "findings":[{"text":"Assigned material completeness is {{metric:coverage}}.","source_refs":[ref]}],
               "artifact_refs":[ref],"gaps":[],"next_steps":[]}
    reference = ref_for(brief,"reference")
    if not branches:
        if ("gx_query_catalog",None) not in replies:
            return "gx_query_catalog", {"kind":"method","reference":METHOD}
        source_ref=replies["gx_query_catalog",None]["source_ref"]
        if ("gx_read_artifact",source_ref) not in replies:
            return op_read(source_ref)
        if ("gx_read_artifact",workflow) not in replies:
            return op_read(workflow)
        if brief.get("module_catalog") and ("gx_open_module",None) not in replies:
            return "gx_open_module", {"module_id":"data_inventory","module_version":"2","round":1,
                   "focus":"Measure completeness when the assigned coordinate file is delivered.",
                   "expected_delivery":"Verified selected/observed measure for the assigned material.",
                   "input_refs":[workflow],"capabilities":[METHOD],"operation_key":"commission-measurement",
                   "sub_budget":{"worker_tasks":6,"api_calls":8,"tokens":320000,"cpu_seconds":45,"job_submissions":1}}
        return decision([branch()],PLAN)
    active = branches[0]
    facts = list(current_result(brief))
    assignment = next((r for r in brief["materials"] if r["role"]=="structure"),{})
    target = next(((ref,m) for ref,m in facts if m["scope"]["input_sha256"]==assignment.get("sha256")),None)
    if assigned and ("gx_read_artifact",assigned) not in replies:
        return op_read(assigned)
    # Branch state and actual Job results determine closure, independently of the assigned condition.
    measured_reference = next(((ref,m) for ref,m in facts if m["scope"]["input_sha256"] != assignment.get("sha256")),None)
    if measured_reference:
        return closing(replies, measured_reference)
    if target:
        ref,m=target
        staged=replies.get(("gx_stage_claim",None))
        if not staged:
            return stage(ref, "Assigned specimen")
        grounds=[ref,staged["claim_ref"]]
        enough=m["numerator"]*5 >= m["denominator"]*3
        if enough:
            op=measure(active["branch_id"], reference, "follow-up")
            return decision([op],KEEP,grounds,event="job_settled",subject=op["node"])
        narrower = {**branch(), "branch":"local:narrower", "method_family":"restricted C-alpha descriptive reference",
                    "budget_limits":{"job_submissions":1,"cpu_seconds":45}}
        job = measure(narrower["branch"],reference,"restricted-reference","common_ca")
        return decision([{"op":"stop_branch","branch":active["branch_id"],"reason":CHANGE},narrower,job],
                        CHANGE,grounds,event="job_settled",subject=job["node"])
    if not assigned or (brief.get("module_catalog") and not brief.get("module_reports")):
        return decision([],"Await the scoped measurement before committing the comparison.")
    if ("gx_read_artifact",assigned) not in replies:
        return op_read(assigned)
    op=measure(active["branch_id"], assigned, "assay")
    return decision([op],"Measure the newly assigned specimen using the registered completeness endpoint.",
                    event="job_settled",subject=op["node"])


def stage(ref, label="Reference file"):
    return "gx_stage_claim", {"statement":label+" completeness for the registered atom selection is {{metric:coverage}}.",
            "level":"open","scope":"Archive coordinate completeness for the cited file and atom selection.",
            "criteria":"Positions meeting the named atom-selection requirement divided by observed positions.",
            "limitations":"The archived data do not establish independent samples.",
            "rationale":"The cited named output is a bounded analysis of the archived coordinates.",
            "evidence_refs":[ref]}


def closing(replies, target):
    if not target:
        raise ValueError("No delivered measurement for the closing claim")
    ref,metric=target
    staged=replies.get(("gx_stage_claim",None))
    if not staged:
        return stage(ref)
    why="The executed measurement is recorded and its scope remains limited to the archived coordinates."
    return decision([{"op":"close_run","reason":why,"claim_refs":[staged["claim_ref"]],
                      "unresolved":["Sampling independence is unknown."]}],why,[ref])
