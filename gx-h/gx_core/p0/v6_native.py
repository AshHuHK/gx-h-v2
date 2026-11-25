"""Native Scientific Lead surface. Only the decision body comes from the model."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from .. import budgets, contracts, registry, schema
from ..domain import ids
from ..errors import BY_CODE, BudgetExceeded, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from ..plugins import goal as goal_plugins
from . import v6_claims, v6_delegation, v6_method_context, v62_native


DECIDE = "gx_decide_revision"
EVIDENCE_INSTRUCTIONS = (
    " Use the module catalogue to choose work that can discriminate among hypotheses. A report may "
    "justify changing a commissioned investigation or keeping it; cite its source and give your reason "
    "in the next decision. Treat method recommendations and predictions according to their recorded "
    "nature. To test a new prediction, stage the observable, pinned measurement, applicable data and "
    "criterion before its result is available. GX records the evidence maturity. Read research_progress "
    "for open gaps, outstanding work and convergence options. When entering convergence, name the "
    "surviving candidates, cite their evidence and identify the specification version."
)
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


@lru_cache(maxsize=None)
def _legacy_goal_tools(version=None):
    """Keep the original, smaller decision vocabulary in quick and pre-B briefs."""
    decision = deepcopy(v62_native.evidence_tool(DECIDE, version))
    claim = deepcopy(v6_claims.tool_spec())
    fields = decision["inputSchema"]["properties"]["decision"]["properties"]
    del fields["convergence"], fields["resolved_gaps"]
    fields["reconsider_when"]["items"]["properties"]["event"]["enum"].remove("module_report")
    del claim["inputSchema"]["properties"]["prediction"]
    return decision, claim


def callback_for(name, *, conn=None, run_id=None):
    handler = registry.native_goal_handler_for(name)
    if handler:
        return handler
    if conn is not None and name in goal_plugins.tools(conn, run_id):
        return call
    return None


def assigned(row):
    # Keep the research host restrictions even when an instance has no callable method.
    if (row.get("brief") or "").startswith(PREFIX):
        return True
    names = json.loads(row.get("tool_names_json") or "[]")
    return DECIDE in names or v62_native.REPORT in names


def tool_specs(read_tools, brief=None, *, conn=None, run_id=None):
    """Select grants when freezing the Root. Resume reuses its persisted tool names."""
    if brief and brief.get("evidence_policy") == "gx.evidence/6.2.3":
        read_tools = deepcopy(read_tools)
        for tool in read_tools:
            if tool["name"] == "gx_read_artifact":
                tool["inputSchema"]["properties"]["endpoint_offset"] = {"type":"integer", "minimum":0}
                tool["description"] += " For verified endpoints pass endpoint_offset=0 and follow next_endpoint_offset. Cite source ref#key."
    if brief and v62_native.module_scope(brief):
        return v62_native.module_tools(read_tools, brief, _legacy_goal_tools()[0])
    catalog = [spec for spec in read_tools if spec["name"] in ("gx_read_artifact", "gx_task_context")]
    evidence_run = bool(brief and brief.get("research_evidence_version"))
    version = (brief or {}).get("evidence_policy")
    catalog.extend((v62_native.evidence_tool(DECIDE, version), v62_native.evidence_tool(STAGE, version))
                   if evidence_run else _legacy_goal_tools(version))
    if brief is None or (brief.get("scope", "run") == "run" and brief.get("branch_delegation", True) and
                         any(b["status"] == "OPEN" for b in brief.get("branches", []))):
        catalog.append(v6_delegation.TOOL)
    if conn is not None:
        extras = goal_plugins.tools(conn, run_id)
    else:
        extras = goal_plugins.tools_for_catalog((brief or {}).get("capabilities", []))
    catalog.extend(tool.as_spec() for tool in extras.values())
    catalog.extend(v62_native.lead_tools(brief or {}))
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


def acknowledged_brief(conn, root, turn_id):
    """Return the immutable packet actually acknowledged on this native task thread.

    A resumed turn may receive a continuation message. Its thread still owns the
    earlier confirmed brief. A delivery intent or an unrelated message is insufficient.
    """
    digest = hashlib.sha256(root["brief"].encode("utf-8")).hexdigest()
    row = conn.execute("""SELECT d.event_id,d.turn_id,d.text_sha256 FROM p0_root_deliveries d
        WHERE d.run_id=? AND d.thread_id=? AND d.state='acknowledged' AND d.method='turn/start'
          AND d.text_sha256=? AND d.rowid<=(
              SELECT MAX(rowid) FROM p0_root_deliveries WHERE run_id=d.run_id AND thread_id=d.thread_id
              AND turn_id=? AND state='acknowledged' AND method='turn/start')
        ORDER BY d.rowid DESC LIMIT 1""", (root["run_id"], root["thread_id"], digest, turn_id)).fetchone()
    if row is not None:
        return brief(root), dict(row)
    return None


def current_context(conn, row):
    packet = brief(row)
    scope = packet.get("scope", "run")
    if v62_native.module_scope(packet):
        from . import v62_budget
        current = v62_budget.view_for_scope(conn, row["run_id"], scope)
    elif scope.startswith("branch:"):
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


@registry.native_goal_tool(v62_native.OPEN)
@registry.native_goal_tool(v62_native.REPORT)
@registry.native_goal_tool(v62_native.CANCEL)
@registry.native_goal_tool(v62_native.QUERY)
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
    if receipt and tool in (DECIDE, DELEGATE, v62_native.OPEN, v62_native.REPORT, v62_native.CANCEL):
        return saved_result(receipt, turn, request_sha)
    # Delegations and reports may be redelivered with a new call ID after
    # release. Their owners verify the original task, lease, thread, turn and
    # exact payload on replay, and the current live binding on first use.
    if tool not in (v62_native.OPEN, v62_native.REPORT):
        if roots.active_turns.get(thread) != turn:
            raise ScopeDenied("A new decision requires the active official Root turn")
        row = require_binding(roots.conn, roots.run_id, thread)
    if receipt:
        packet = brief(row)
        if tool in v62_native.SOURCE_TOOLS:
            v62_native.authorize_source(roots, row, p)
        elif v62_native.module_scope(packet):
            v62_native.authorize_read(roots.conn, row, packet, tool, turn)
        return saved_result(receipt, turn, request_sha)

    identity = dict(run_id=roots.run_id, task_id=row["task_id"], lease_id=row["lease_id"],
                    thread_id=thread, turn_id=turn, call_id=call_id)
    if roots.conn.in_transaction:
        raise RuntimeError("Prepare scientific decisions outside the GX writer")
    if tool in (v62_native.OPEN, v62_native.REPORT, v62_native.CANCEL, v62_native.QUERY):
        spec = (v62_native.evidence_tool(tool, brief(row).get("evidence_policy"))
                if tool == v62_native.REPORT else contracts.native_goal_tool_spec(tool))
        failures = schema.validate(args, spec["inputSchema"])
        if failures:
            details = {"category": "module_report_structure"} if tool == v62_native.REPORT else {}
            raise SchemaInvalid("Invalid arguments to " + tool + ": " + ", ".join(failures), details=details)
    if tool == DECIDE:
        try:
            prepared = v6_decisions.prepare(roots.conn, **identity, decision=args["decision"])
        except StaleRevision as exc:
            v6_decisions.reconsider_module_conflict(roots.conn, identity, exc)
            raise
        except (BudgetExceeded, SchemaInvalid) as exc:
            remember_refusal(roots, p, request_sha, exc)
            raise
    elif tool == v62_native.OPEN:
        from . import v62_modules
        prepared = v62_modules.prepare_open(roots.conn, **identity, payload=args)
    elif tool == v62_native.REPORT:
        from . import v62_reports
        prepared = v62_reports.prepare_report(roots.conn, **identity, payload=args)
    elif tool == DELEGATE:
        prepared = v6_delegation.prepare(roots.conn, **identity, payload=args)
    elif tool == STAGE:
        task = roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        answer = v6_claims.stage(roots.conn, **identity, context_ref=task[0], payload=args,
                                 allowed_refs=task_refs(roots.conn, row))
    elif tool not in (v62_native.CANCEL, v62_native.QUERY) and tool not in v62_native.SOURCE_TOOLS:
        task = roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        answer = v6_method_context.call_tool(roots.conn, tool, args, context_ref=task[0], **identity)

    if (tool in (v62_native.OPEN, v62_native.REPORT)
            and not isinstance(prepared, dict) and roots.active_turns.get(thread) != turn):
        raise ScopeDenied("A new delegation or report requires the active official Root turn")

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
            elif tool == v62_native.OPEN:
                answer = v62_modules.commit_open(roots.conn, prepared)
            elif tool == v62_native.REPORT:
                answer = v62_reports.commit_report(roots.conn, prepared)
                if answer["author"]["call_id"] == call_id:
                    roots.terminal_products.record(thread, turn, call_id, "GX_ACCEPTED")
            elif tool == v62_native.CANCEL:
                from . import v62_modules
                answer = v62_modules.cancel_module(roots.conn, **identity, payload=args)
            elif tool == v62_native.QUERY:
                from . import v62_catalog
                packet = brief(require_binding(roots.conn, roots.run_id, thread))
                def query(arguments):
                    return v62_catalog.query(roots.conn, roots.run_id, packet, arguments)
                answer = (v62_native.perform_read(roots, row, p, packet, query)
                          if v62_native.module_scope(packet) else query(args))
                if "execution_granted" in answer and args.get("reference"):
                    card = v62_catalog._public_card(answer["spec"], metric_contract=True)
                    answer["source_ref"] = v62_native.publish_method_card(roots, row, card)
            elif tool in v62_native.SOURCE_TOOLS:
                answer = v62_native.perform_source(roots, row, p)
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
    except StaleRevision as exc:
        if tool == DECIDE:
            v6_decisions.reconsider_module_conflict(roots.conn, identity, exc)
        raise
    except (BudgetExceeded, SchemaInvalid) as exc:
        if tool == DECIDE:
            remember_refusal(roots, p, request_sha, exc)
        raise


v62_native.register_tools(call)


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
