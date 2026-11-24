"""Positions in an existing static Workflow checkpoint. The Engine owns all writes."""
from __future__ import annotations


def wait_record(node, pending, ref, since, round_index, controller_ran):
    payload = pending.get("payload") or {}
    kind = pending["kind"]
    if kind == "approval" or payload.get("waiting_state") == "WAITING_APPROVAL":
        status, owner = "WAITING_HUMAN", "operator"
    elif kind == "external_job" or payload.get("waiting_state") == "WAITING_EXTERNAL_JOB":
        status, owner = "WAITING_JOB", "job"
    elif payload.get("wait_for") == "child_runs":
        status, owner = "WAITING_CHILD_RUN", "workflow"
    elif payload.get("coordinator_action") or payload.get("wait_for") in ("decision", "case_selection"):
        status, owner = "WAITING_DECISION", "coordinator"
    elif kind == "stage" and not pending.get("tasks"):
        status, owner = "WAITING_STAGE", "workflow"
    else:
        status, owner = "WAITING_AGENT_TASK", "task"
    references = list(pending.get("tasks", ())) + list(pending.get("job_refs", ()))
    references.extend(pending.get("required_artifacts") or ())
    references.extend(payload.get("approval_refs") or ())
    references.extend(value for value in (pending.get("approval_ref"), payload.get("handoff_ref")) if value)
    return {"node": node, "interrupt": pending, "interrupt_ref": ref, "status": status, "owner": owner,
            "references": list(dict.fromkeys(references)) or [ref], "since": since, "reason": pending["reason"],
            "next_retry_at": payload.get("next_retry_at"), "round": round_index, "controller_ran": controller_ran}


def waits(checkpoint):
    if checkpoint is None:
        return {}
    state = checkpoint.state
    if "node_waits" in state:
        return dict(state["node_waits"])
    # Historical checkpoints kept exactly one wait, with the same owner predicates.
    pending, ref = state.get("pending_interrupt"), state.get("interrupt_ref") or checkpoint.interrupt_ref
    if pending and ref:
        return {checkpoint.node: wait_record(checkpoint.node, pending, ref, checkpoint.created_at,
                                             state.get("round", 0), state.get("controller_ran") or {})}
    return {}


def node_states(workflow, state):
    completed = set(state.get("completed", ()))
    waits = state.get("node_waits", {})
    failures = state.get("failed_nodes", {})
    positions = {}
    for node in workflow.order:
        if node in waits:
            positions[node] = waits[node]["status"]
        elif node in failures:
            positions[node] = "FAILED"
        elif node in completed:
            positions[node] = "COMPLETED"
        elif completed.issuperset(workflow.node(node).deps):
            positions[node] = "READY"
        else:
            positions[node] = "BLOCKED"
    return positions
