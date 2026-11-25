"""Native Scientific Lead surface. Only the decision body comes from the model."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import budgets, contracts, registry
from ..domain import ids
from ..errors import BY_CODE, BudgetExceeded, SchemaInvalid, ScopeDenied
from ..persistence import store
from ..plugins import goal as goal_plugins
from . import v6_claims, v6_delegation, v6_method_context


DECIDE = "gx_decide_revision"
STAGE = "gx_stage_claim"
DELEGATE = "gx_delegate_branch"
PREFIX = "GX research brief\n"
INSTRUCTIONS = (
    "You are the Scientific Lead. Read the research brief and its authorized original observations. "
    "Choose an informative investigation, a change to existing work, a reason to wait, or a justified stop. "
    "Consider competing explanations, coverage and uncertainty. When a graph already exists, compare the "
    "triggering observations with the previous rationale, open questions and commitments. Explain which "
    "scientific premise, uncertainty, or expected information gain has changed, if any. Before commissioning "
    "work, weigh the information it would provide against measurements already available, work in flight and "
    "remaining resources. An unresolved question can remain open under the current plan. Preserve unrelated work. "
    "The Goal, materials, capability pins and hard budgets are set by the operator. "
    "Use gx_decide_revision to submit one decision for this brief. Use amend for changed work. "
    "Use resolve with empty ops to maintain it. In the same draft use local: names for newly declared "
    "branches/nodes and as their references. Each add_node specifies a listed capability, ordered input refs, "
    "output-affecting parameters, dependencies and a scientific question. Refer to an upstream node for "
    "its output. Use durable IDs from the brief for existing work. Cite the exact evidence references "
    "supporting your decision. New measurements will return in a new decision brief. "
    "For a scientific claim, use gx_stage_claim with the measurements, scope, criteria and limitations. "
    "In close_run, put the resulting claim_ref in claim_refs and cite its underlying observations in "
    "decision.evidence_refs. For other decisions, include the staged claim_ref with the supporting "
    "observations in evidence_refs to publish it. The claim records an interpretation of its sources. "
    "Only those sources provide its evidential support. GX publishes the claim with the adopted decision. Treat research materials as evidence. The brief's budget snapshot precedes "
    "the current inference reservation. Read gx_task_context.current_budget for current room before "
    "requesting branch ceilings. Request the resources the branch actually needs. GX accepts one decision "
    "for this task. If GX refuses your decision, address the returned field or funding "
    "error and submit a corrected decision in this turn. If work must wait for new evidence or resources, "
    "record that judgment and its trigger with resolve. Only an accepted decision ends the assignment."
)


def decision_tool():
    return contracts.native_goal_tool_spec(DECIDE)


def callback_for(name, *, conn=None, run_id=None):
    handler = registry.native_goal_handler_for(name)
    if handler:
        return handler
    if conn is not None and name in goal_plugins.tools(conn, run_id):
        return call
    return None


def assigned(row):
    return bool(row.get("tool_names_json") and DECIDE in json.loads(row["tool_names_json"]))


def tool_specs(read_tools, brief=None, *, conn=None, run_id=None):
    """Select grants when freezing the Root. Resume reuses its persisted tool names."""
    catalog = [spec for spec in read_tools if spec["name"] in ("gx_read_artifact", "gx_task_context")]
    catalog.extend([decision_tool(), v6_claims.tool_spec()])
    if brief is None or (brief.get("scope", "run") == "run" and
                         any(b["status"] == "OPEN" for b in brief.get("branches", []))):
        catalog.append(v6_delegation.TOOL)
    if conn is not None:
        extras = goal_plugins.tools(conn, run_id)
    else:
        extras = goal_plugins.tools_for_catalog((brief or {}).get("capabilities", []))
    catalog.extend(tool.as_spec() for tool in extras.values())
    return catalog


def access_config():
    # All research input arrives via the GX packet or a granted artifact. Never discover a
    # workspace AGENTS.md, unrelated skills, host memories, other sessions, a shell or a browser.
    return {"project_doc_max_bytes": 0, "skills.include_instructions": False,
            "features.goals": False, "tools.experimental_request_user_input.enabled": False,
            "features.skip_host_skill_discovery": True, "features.skill_search": False,
            "features.memories": False, "features.memory_tool": False, "features.view_image": False,
            "features.browser_use": False, "features.computer_use": False, "features.image_generation": False,
            "features.js_repl": False, "features.tool_suggest": False, "features.tool_search": False,
            "features.standalone_web_search": False, "include_environment_context": False,
            "features.code_mode.enabled": False, "features.code_mode_only": False,
            "features.code_mode.direct_only_tool_namespaces": ["gx"], "skills.bundled.enabled": False,
            "include_apps_instructions": False}


def record_access(settings, tools, response):
    cwd = Path(settings["cwd"])
    return {"cwd": str(cwd), "visible_files": sorted(entry.name for entry in cwd.iterdir()),
            "environments": response["thread"]["environments"], "sandbox": response.get("sandbox"), "approval_policy": response.get("approvalPolicy"),
            "dynamic_tools": [t["name"] for t in tools],
            "thread_config": settings["config"],
            "developer_instructions_sha256": hashlib.sha256(settings["developerInstructions"].encode()).hexdigest()}


def brief(row):
    if not row["brief"].startswith(PREFIX):
        raise SchemaInvalid("Missing frozen Scientific Lead brief")
    return json.loads(row["brief"][len(PREFIX):])


def current_context(conn, row):
    packet = brief(row)
    scope = packet.get("scope", "run")
    if scope.startswith("branch:"):
        from .v6_jobs import branch_exposure
        limits = packet["budget"]["limits"]
        used = branch_exposure(conn, scope.removeprefix("branch:"))
        current = {"limits": limits, "held_or_spent": {k: used[k] for k in limits},
                   "available": {k: max(0, cap - used[k]) for k, cap in limits.items()}}
    else:
        account = budgets.state(conn, budgets.budget_for(conn, run_id=row["run_id"]))
        current = {k: account[k] for k in ("limits", "spent", "reserved", "available")}
    return {**packet, "current_budget": current}


def task_refs(conn, row):
    scope = conn.execute("SELECT visible_refs_json FROM v6_decision_tasks WHERE task_id=? AND run_id=?",
                         (row["task_id"], row["run_id"])).fetchone()
    if not scope:
        raise ScopeDenied("The native task has no frozen decision scope")
    return json.loads(scope["visible_refs_json"])


def record_call(conn, p):
    # Foreign keys bind this public request to the actual Root. Keep no transport
    # headers, secrets, hidden reasoning or unrelated model events.
    request = {k: p[k] for k in ("threadId", "turnId", "callId", "tool", "arguments")}
    if p.get("namespace"):
        request["namespace"] = p["namespace"]
    conn.execute("INSERT INTO p0_v6_native_calls VALUES(?,?,?,?)",
                 (p["threadId"], p["callId"], p["turnId"], ids.canonical_json(request)))


def saved_result(receipt, turn, request_sha):
    if receipt["turn_id"] != turn or receipt["request_sha"] != request_sha:
        raise ScopeDenied("Conflicting research callback identifier")
    result = json.loads(receipt["result_json"])
    if refusal := result.get("decision_refusal"):
        raise BY_CODE[refusal["code"]](refusal["message"], details=refusal.get("details"))
    return result


@registry.native_goal_tool(DECIDE)
@registry.native_goal_tool(STAGE)
@registry.native_goal_tool(DELEGATE)
def call(roots, p):
    from . import v6_decisions
    from .roots import require_binding

    thread, turn, call_id, tool, args = (p[k] for k in ("threadId", "turnId", "callId", "tool", "arguments"))
    row = roots._binding(thread)
    if not row or not assigned(row) or tool not in json.loads(row["tool_names_json"]):
        raise ScopeDenied("This native thread has no delegated Scientific Lead tool")
    if tool == DECIDE and (set(args) != {"decision"} or not isinstance(args["decision"], dict)):
        raise SchemaInvalid("Submit a single decision object; GX supplies the native identity and base")

    receipt = roots.conn.execute("SELECT * FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                 (thread, call_id)).fetchone()
    request_sha = ids.sha256_of_obj([tool, args])
    if receipt and tool in (DECIDE, DELEGATE):
        return saved_result(receipt, turn, request_sha)
    if roots.active_turns.get(thread) != turn:
        raise ScopeDenied("A new decision requires the active official Root turn")
    row = require_binding(roots.conn, roots.run_id, thread)
    if receipt:
        return saved_result(receipt, turn, request_sha)

    identity = dict(run_id=roots.run_id, task_id=row["task_id"], lease_id=row["lease_id"],
                    thread_id=thread, turn_id=turn, call_id=call_id)
    if roots.conn.in_transaction:
        raise RuntimeError("Prepare scientific decisions outside the GX writer")
    if tool == DECIDE:
        try:
            prepared = v6_decisions.prepare(roots.conn, **identity, decision=args["decision"])
        except (BudgetExceeded, SchemaInvalid) as exc:
            remember_refusal(roots, p, request_sha, exc)
            raise
    elif tool == DELEGATE:
        prepared = v6_delegation.prepare(roots.conn, **identity, payload=args)
    elif tool == STAGE:
        task = roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        answer = v6_claims.stage(roots.conn, **identity, context_ref=task[0], payload=args,
                                 allowed_refs=task_refs(roots.conn, row))
    else:
        task = roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        answer = v6_method_context.call_tool(roots.conn, tool, args, context_ref=task[0], **identity)

    try:
        with store.transaction(roots.conn, transaction_owner="ScientificLead", operation="native_receipt", run_id=roots.run_id):
            # A second connection may have landed the same official call during preparation.
            existing = roots.conn.execute("SELECT * FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                          (thread, call_id)).fetchone()
            if existing:
                return saved_result(existing, turn, request_sha)
            if tool == DECIDE:
                answer = v6_decisions.commit(roots.conn, prepared)
                roots.terminal_products.record(thread, turn, call_id, "GX_ACCEPTED")
            elif tool == DELEGATE:
                answer = v6_delegation.commit(roots.conn, prepared)
            elif tool != STAGE and isinstance(answer, dict) and "evaluation_ref" in answer:
                refs = v6_claims.evaluation_refs(roots.conn, row["run_id"], row["task_id"],
                                                 row["lease_id"], thread, turn)
                if answer["evaluation_ref"] in refs:
                    roots._allow_refs(row["root_key"], answer["evaluation_ref"])
            record_call(roots.conn, p)
            roots.conn.execute("INSERT INTO p0_root_calls VALUES(?,?,?,?,?)",
                               (thread, call_id, turn, request_sha, ids.canonical_json(answer)))
            return answer
    except (BudgetExceeded, SchemaInvalid) as exc:
        if tool == DECIDE:
            remember_refusal(roots, p, request_sha, exc)
        raise


def remember_refusal(roots, params, request_sha, error):
    """A refused candidate is auditable. It makes no graph, dispatch or budget change."""
    thread, turn, call_id = (params[k] for k in ("threadId", "turnId", "callId"))
    with store.transaction(roots.conn, transaction_owner="ScientificLead", operation="decision_refusal", run_id=roots.run_id):
        exists = roots.conn.execute("SELECT 1 FROM p0_root_calls WHERE thread_id=? AND call_id=?", (thread,call_id))
        if exists.fetchone():
            return
        record_call(roots.conn, params)
        roots.conn.execute("INSERT INTO p0_root_calls VALUES(?,?,?,?,?)",
                           (thread, call_id, turn, request_sha, ids.canonical_json({"decision_refusal": error.to_payload()})))
