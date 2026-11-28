"""Native Scientific Lead surface. Only the decision body comes from the model."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from .. import budgets, contracts, registry, schema
from ..domain import ids
from ..errors import BY_CODE, BudgetExceeded, HoldActive, LeaseExpired, ResourceDenied, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from ..plugins import goal as goal_plugins
from . import harness_policy, research_closeout, v6_recipes, v621_policy, v6_claims, v6_delegation, v6_method_context, v62_native


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


COMPACT_INSTRUCTIONS = (
    "You are the Scientific Lead. Choose an informative, authorized investigation or decide when to stop. "
    "Read the brief and relevant original observations. Compare expectations, uncertainties, work already "
    "available or in flight, and the cost of new information. Use gx_decide_revision with rationale and ops. "
    "When acting on a measurement, select the delivered row key in metric_refs. Give the scientific action "
    "and its reason. GX binds the evidence, matching comparison and result wake; use the error's field and "
    "options if a choice remains. To commission a local measurement, add a node with its listed capability, "
    "input artifact refs and question; specify output-affecting parameters when choosing a method. Use "
    "comparison_plan.prediction for a sealed-material commitment with blind_id, expected, on_expected and "
    "on_alternative. An empty ops list can wait; only explicitly stopping a comparison means STOP. "
    "Stage formal claims with gx_stage_claim. For ordinary closure put reason, unresolved and claim_refs in "
    "the close_run op, cite supporting observations and review_ref values in decision.evidence_refs, "
    "and omit comparison action fields. "
    "For a concrete choice the PI must make, submit pi_questions with question, blocking and optional choices "
    "and branch_id. Each pi_dispositions item takes feedback_id, outcome (adopt, maintain, measure, or "
    "defer), and reason. Put supporting references in decision.evidence_refs. Treat PI hypotheses as "
    "suggestions for evaluation. "
    "Only an accepted decision ends this assignment. Treat material contents as scientific evidence."
)



RESEARCH_INSTRUCTIONS = (
    "You lead this research. The PI sets the question, constraints and explicit commitments. "
    "Choose how to decompose the question, what existing evidence can settle, what to investigate, "
    "and when each route is ready to advance. Parallel researchers may exchange findings directly. "
    "Choose the people and methods the question warrants. Revisit a local decision when its premise changes. "
    "The catalog offers resources; inspect selected entries and re-search as the question warrants. "
    "Use the available native tools to record your decisions. A research intent can name an open question "
    "before a method is chosen. For a computation identify the scientific question, method, inputs when "
    "ambiguous and output-affecting settings. GX tracks dependencies, funds, results and wake conditions. "
    "For an established method or recipe, bind this study's own inputs and judge its outputs anew. "
    "Read released researcher statements and consequential originals. Distinguish published observations, "
    "predictions, interpretations and technical failures. Repeated references to one sample remain one sample. "
    "Where independent testing matters, register the endpoint and criterion before revealing new material. "
    "Use gx_decide_revision for the action or justified stop and cite the supporting evidence. "
    "Use metric_refs when acting on delivered measurements. Formal claims can be staged separately. "
    "You can leave uncertainty explicit and close. If GX returns a field or scope error, correct the "
    "same request on the current brief. Only an accepted decision changes the research graph."
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


def _save_recipe_tool(packet):
    spec = v6_recipes.save_tool(harness_policy.enabled(packet))
    if (packet or {}).get("harness_policy") != harness_policy.VERSION:
        fields = spec["inputSchema"]["properties"]
        for name in ("decision_points", "evaluation", "children"):
            fields.pop(name, None)
    return spec


def _instantiate_tool(packet):
    tool = contracts.native_goal_tool_spec(v6_recipes.INSTANTIATE)
    if (packet or {}).get("harness_policy") != harness_policy.VERSION:
        return tool
    spec = deepcopy(tool)
    spec["inputSchema"]["properties"]["adopt"] = {"type":"boolean","description":
        "For a macro, adopt its logical dependencies now, ahead of any Jobs or modules. Omit for a read-only preview."}
    return spec


def _research_decision_tool(spec, brief):
    projected = deepcopy(spec)
    fields = projected["inputSchema"]["properties"]
    body = fields["decision"]
    if (brief or {}).get("harness_policy") != harness_policy.VERSION:
        for variant in body["properties"]["ops"]["items"]["oneOf"]:
            gate = variant["properties"].get("evidence_gate")
            if gate:
                for name in ("output_name", "source_role"):
                    gate["properties"].pop(name, None)
    if not harness_policy.enabled(brief):
        return projected
    if brief.get("scope") == "run":
        from . import v6_routes
        body = v6_routes.with_operations(body, reinterpretation=brief.get("harness_policy") == harness_policy.VERSION)
    fields["decision"] = harness_policy.inferable_fields(body)
    return projected


def has_open_branch(brief, modern):
    return bool(modern and brief.get("open_branch_available")) or any(
        branch["status"] == "OPEN" for branch in brief.get("branches", ()))


def has_recipe_source(brief, modern):
    return any(n.get("result_ref") for n in brief.get("nodes", ())) or bool(modern and (
        brief.get("recipe_source_available") or brief.get("module_reports") or brief.get("module_report_index")))


def tool_specs(read_tools, brief=None, *, conn=None, run_id=None):
    """Select grants when freezing the Root. Resume reuses its persisted tool names."""
    if brief and brief.get("evidence_policy") in v621_policy.LIVE_SCIENCE:
        read_tools = deepcopy(read_tools)
        for tool in read_tools:
            if tool["name"] == "gx_read_artifact":
                tool["inputSchema"]["properties"]["endpoint_offset"] = {"type":"integer", "minimum":0}
                tool["description"] += " For verified endpoints pass endpoint_offset=0 and follow next_endpoint_offset. Cite source ref#key."
    if brief and v62_native.module_scope(brief):
        decision = _legacy_goal_tools()[0]
        if brief.get("evidence_policy") == v621_policy.VERSION:
            decision = deepcopy(_legacy_goal_tools(v621_policy.VERSION)[0])
            fields = decision["inputSchema"]["properties"]["decision"]["properties"]
            for field in ("comparison_plan", "comparison_id", "disposition", "metric_refs", "affected_scope", "stop_reason"):
                fields.pop(field)
            decision["description"] = ("Submit a scientific reason and a permitted operation on this module's assigned "
                "branch. For a local Job provide its granted capability, input refs and scientific question. "
                "GX supplies unique branch/node fields and wakes on the result.")
        catalog = v62_native.module_tools(read_tools, brief, _research_decision_tool(decision, brief))
        return catalog + ([contracts.native_goal_tool_spec(research_closeout.LINK)] if harness_policy.enabled(brief) else [])
    catalog = [spec for spec in read_tools if spec["name"] in ("gx_read_artifact", "gx_task_context")]
    if harness_policy.enabled(brief or {}) and brief.get("scope","run") == "run":
        catalog = deepcopy(catalog)
        context = next((spec for spec in catalog if spec["name"] == "gx_task_context"),None)
        if context:
            context["description"] += (" Read the current peer findings, including folded forwards and original sources. "
                "For earlier findings pass before_topic_event with the oldest displayed event_id, optionally research_topic.")
            context["inputSchema"]["properties"].update(before_topic_event={"type":"integer","minimum":1},
                                                        research_topic={"type":"string","minLength":1,"maxLength":128})
    evidence_run = bool(brief and brief.get("research_evidence_version"))
    version = (brief or {}).get("evidence_policy")
    decision, claim = ((v62_native.evidence_tool(DECIDE, version), v62_native.evidence_tool(STAGE, version))
                       if evidence_run else _legacy_goal_tools(version))
    if harness_policy.enabled(brief):
        claim = deepcopy(claim)
        claim["inputSchema"]["properties"]["evidence_check"] = v6_claims.EVIDENCE_CHECK
    catalog.extend((_research_decision_tool(decision, brief), claim))
    if brief is None or (brief.get("scope", "run") == "run" and brief.get("branch_delegation", True) and
                         has_open_branch(brief, harness_policy.enabled(brief))):
        catalog.append(v6_delegation.TOOL)
    if conn is not None:
        extras = goal_plugins.tools(conn, run_id)
    else:
        extras = goal_plugins.tools_for_catalog((brief or {}).get("capabilities", []))
    catalog.extend(tool.as_spec() for tool in extras.values())
    view = (brief or {}).get("capability_view", {})
    if (brief or {}).get("scope") == "run" and (
            view.get("option_count") or view.get("available_options") or view.get("options_after_pi_interpretation")):
        catalog.append(contracts.native_goal_tool_spec("gx_select_capability"))
    catalog.extend(v62_native.lead_tools(brief or {}))
    if brief and brief.get("scope", "run") == "run" and version == v621_policy.VERSION:
        modern = harness_policy.enabled(brief)
        if modern:
            catalog.append(contracts.native_goal_tool_spec(v6_recipes.WATCH))
        if has_recipe_source(brief, modern):
            catalog.append(_save_recipe_tool(brief))
        if conn is not None and conn.execute("""SELECT 1 FROM v6_saved_recipes WHERE
                (json_extract(definition_json,'$.schema') != 'gx.research-recipe/3' OR ?)
                AND (json_extract(definition_json,'$.schema') != 'gx.research-recipe/4' OR ?) LIMIT 1""",
                (modern, brief.get("harness_policy") == harness_policy.VERSION)).fetchone():
            if not any(tool["name"] == v62_native.QUERY for tool in catalog):
                catalog.append(v62_native.query_tool(brief))
            catalog.append(_instantiate_tool(brief))

    if harness_policy.enabled(brief):
        catalog.append(contracts.native_goal_tool_spec(research_closeout.LINK))
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


def current_context(conn, row, *, topic_page=None):
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
    if scope == "run":
        from . import harness_policy, research_topics
        if harness_policy.enabled(packet):
            page = topic_page or {}
            before, topic = page.get("before_topic_event"), page.get("research_topic")
            if (set(page)-{"before_topic_event","research_topic"} or
                    (before is not None and (type(before) is not int or before < 1)) or
                    (topic is not None and (not isinstance(topic,str) or not 1<=len(topic)<=128))):
                raise SchemaInvalid("Select a research_topic and/or a positive before_topic_event")
            packet["research_topic_findings"] = research_topics.lead_preview(
                conn,row["run_id"],packet["goal_version"],task_refs(conn,row),before_event=before,topic=topic)
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


@registry.native_goal_tool(research_closeout.LINK)
@registry.native_goal_tool(v6_recipes.WATCH)
@registry.native_goal_tool(v6_recipes.SAVE)
@registry.native_goal_tool(v6_recipes.INSTANTIATE)
@registry.native_goal_tool(v62_native.OPEN)
@registry.native_goal_tool(v62_native.REPORT)
@registry.native_goal_tool(v62_native.CANCEL)
@registry.native_goal_tool("gx_select_capability")
@registry.native_goal_tool(v62_native.QUERY)
@registry.native_goal_tool(DECIDE)
@registry.native_goal_tool(STAGE)
@registry.native_goal_tool(DELEGATE)
def call(roots, p):
    from .v6_decisions import GraphAdvanced
    try:
        while True:
            try:
                return _call(roots, p)
            except GraphAdvanced:
                # The SQLite writer has rolled back. Revalidate the very same native
                # call against the new graph, including subject and evidence conflicts.
                continue
    except (BudgetExceeded, HoldActive, LeaseExpired, ResourceDenied, SchemaInvalid, ScopeDenied, StaleRevision) as error:
        if p["tool"] != DECIDE or not v621_policy.science_execution(roots.conn, roots.run_id):
            raise
        # Only an actual assigned callback can produce an operator decision-attempt receipt.
        row = roots._binding(p["threadId"])
        if (not row or not assigned(row) or DECIDE not in json.loads(row["tool_names_json"]) or
                not roots.conn.execute("""SELECT 1 FROM p0_root_deliveries
                    WHERE run_id=? AND thread_id=? AND turn_id=? AND state='acknowledged' AND method='turn/start'""",
                    (roots.run_id, p["threadId"], p["turnId"])).fetchone()):
            raise
        key = ids.sha256_of_obj([DECIDE, p["arguments"]])
        replay = remember_refusal(roots, p, key, error, publish=True)
        if replay is not None:
            return replay
        raise


def _call(roots, p):
    from . import v6_decisions
    from .roots import require_binding

    thread, turn, call_id, tool, args = (p[k] for k in ("threadId", "turnId", "callId", "tool", "arguments"))
    row = roots._binding(thread)
    if not row or not assigned(row) or tool not in json.loads(row["tool_names_json"]):
        raise ScopeDenied("This native thread has no delegated Scientific Lead tool")
    if tool == DECIDE and (set(args) != {"decision"} or not isinstance(args["decision"], dict)):
        raise SchemaInvalid("Submit a single decision object; GX supplies the native identity and base",
                            details={"reason": "DECISION_OBJECT_REQUIRED", "field": "decision"})

    receipt = roots.conn.execute("SELECT * FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                 (thread, call_id)).fetchone()
    request_sha = ids.sha256_of_obj([tool, args])
    if receipt and tool in (research_closeout.LINK, DECIDE, DELEGATE, v62_native.OPEN, v62_native.REPORT, v62_native.CANCEL):
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

    from . import research_window
    window = research_window.for_run(roots.conn, roots.run_id)
    research_window.require_completion(window)
    identity = dict(run_id=roots.run_id, task_id=row["task_id"], lease_id=row["lease_id"],
                    thread_id=thread, turn_id=turn, call_id=call_id)
    if roots.conn.in_transaction:
        raise RuntimeError("Prepare scientific decisions outside the GX writer")
    if tool in (v6_recipes.SAVE, v6_recipes.INSTANTIATE, v6_recipes.WATCH, v62_native.OPEN, v62_native.REPORT, v62_native.CANCEL, v62_native.QUERY):
        spec = (v62_native.evidence_tool(tool, brief(row).get("evidence_policy"))
                if tool == v62_native.REPORT else contracts.native_goal_tool_spec(tool))
        if tool == v62_native.QUERY and "pi_method_scope" in args:
            if brief(row).get("scope") != "run":
                raise ScopeDenied("Only the Run Lead interprets the PI method scope")
            spec = v62_native.query_tool(brief(row))
        if tool == v62_native.OPEN:
            spec = v62_native.open_tool(brief(row))
        elif tool == v6_recipes.INSTANTIATE:
            spec = _instantiate_tool(brief(row))
        elif tool == v6_recipes.SAVE:
            spec = _save_recipe_tool(brief(row))
        failures = schema.validate(args, spec["inputSchema"])
        if failures:
            details = {"category": "module_report_structure"} if tool == v62_native.REPORT else {}
            raise SchemaInvalid("Invalid arguments to " + tool + ": " + ", ".join(failures), details=details)
    if tool == v62_native.QUERY:
        from . import catalog_index, catalog_survey
        opted_in = roots.conn.execute("SELECT json_extract(inputs_json,'$.inputs.discovery_policy') "
                                      "FROM runs WHERE run_id=?", (roots.run_id,)).fetchone()
        if opted_in and opted_in[0] == catalog_survey.POLICY:
            catalog_index.installed_methods()
            from . import catalog_resources
            for name in catalog_resources.REFERENCE_FILES:
                catalog_resources.reference_index(name)
    if tool == "gx_select_capability":
        from . import capability_views
        packet = brief(row)
        failures = schema.validate(args, contracts.native_goal_tool_spec(tool)["inputSchema"])
        if failures:
            raise SchemaInvalid("Invalid capability selection: " + ", ".join(failures))
        if v62_native.module_scope(packet):
            from . import module_methods
            selected_descriptor = module_methods.prepare(roots.conn, identity, args, packet, row)
        else:
            chosen = capability_views.active_options(roots.conn, roots.run_id, json.loads(roots.conn.execute(
                "SELECT inputs_json FROM runs WHERE run_id=?", (roots.run_id,)
            ).fetchone()[0])["inputs"])
            if packet.get("scope") != "run" or args["method"] not in chosen:
                raise ScopeDenied("This method selection exceeds the PI's choices for this Goal")
            choice = chosen[args["method"]]
            answer = capability_views.change(roots.conn, roots.run_id, **{args["action"]:args["method"]},
                provider=choice.get("provider"), expected_digest=choice["digest"], expected_revision=args["revision"],
                expected_provider_digest=choice.get("provider_digest"),
                request_id=ids.sha256_of_obj([thread, turn, call_id]), actor=thread,
                authority=lambda: v6_claims.active_context(roots.conn, roots.run_id, row["task_id"], row["lease_id"],
                    roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?",
                                       (row["task_id"],)).fetchone()[0], thread, turn))
    elif tool == DECIDE:
        try:
            prepared = v6_decisions.prepare(roots.conn, **identity, decision=args["decision"])
        except StaleRevision as exc:
            if exc.details.get("reason") == "lead_graph_conflict":
                replay = remember_refusal(roots, p, request_sha, exc, conflicted_lead=identity,
                                          publish=v621_policy.science_execution(roots.conn, roots.run_id))
                if replay is not None:
                    return replay
            else:
                v6_decisions.reconsider_module_conflict(roots.conn, identity, exc)
            raise
        except (BudgetExceeded, SchemaInvalid) as exc:
            if not v621_policy.science_execution(roots.conn, roots.run_id):
                remember_refusal(roots, p, request_sha, exc)
            raise
    elif tool == v62_native.OPEN:
        from . import v62_modules
        prepared = v62_modules.prepare_open(roots.conn, **identity, payload=args)
    elif tool == v62_native.REPORT:
        from . import v62_reports
        prepared = v62_reports.prepare_report(roots.conn, **identity, payload=args)
    elif tool == v6_recipes.WATCH:
        v6_recipes.authorize(roots.conn, identity, tool, args)
    elif tool in (v6_recipes.SAVE, v6_recipes.INSTANTIATE):
        recipe_task = v6_recipes.authorize(roots.conn, identity, tool, args)
        if tool == v6_recipes.SAVE:
            prepared = v6_recipes.prepare(roots.conn, roots.run_id, args)
        else:
            answer = v6_recipes.instantiate(roots.conn, roots.run_id, args,
                visible_refs=set(json.loads(recipe_task["visible_refs_json"])))
    elif tool == research_closeout.LINK:
        failures = schema.validate(args, contracts.native_goal_tool_spec(research_closeout.LINK)["inputSchema"])
        if failures:
            raise SchemaInvalid("Invalid core material nomination: " + ", ".join(failures))
        _, task, _, _ = v6_decisions.authority(roots.conn, **{k:v for k,v in identity.items() if k != "call_id"})
        nomination = research_closeout.prepare_nomination(roots.conn, task, args)
    elif tool == DELEGATE:
        prepared = v6_delegation.prepare(roots.conn, **identity, payload=args)
    elif tool == STAGE:
        task = roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        answer = v6_claims.stage(roots.conn, **identity, context_ref=task[0], payload=args,
                                 allowed_refs=task_refs(roots.conn, row))
    elif tool not in (v62_native.CANCEL, v62_native.QUERY, "gx_select_capability") and tool not in v62_native.SOURCE_TOOLS:
        task = roots.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        answer = v6_method_context.call_tool(roots.conn, tool, args, context_ref=task[0], **identity)

    if (tool in (v62_native.OPEN, v62_native.REPORT)
            and not isinstance(prepared, dict) and roots.active_turns.get(thread) != turn):
        raise ScopeDenied("A new delegation or report requires the active official Root turn")

    adoptions = []
    recipe_action = (
        (tool == v62_native.OPEN and args["operation_key"].startswith("recipe-"))
        or (tool == DECIDE and any(op.get("recipe_decision") for op in args["decision"].get("ops", [])))
        or (tool == v6_recipes.INSTANTIATE and args.get("adopt"))
    )
    if recipe_action and harness_policy.current(roots.conn, roots.run_id):
        from . import recipe_instances
        version = brief(row)["goal_version"]
        if tool == v6_recipes.INSTANTIATE:
            adoptions = [recipe_instances.prepare(roots.conn, roots.run_id, version, args)]
        elif tool == v62_native.OPEN or not v62_native.module_scope(brief(row)):
            adoptions = recipe_instances.pending_adoptions(roots.conn,roots.run_id,version,tool,args)

    try:
        with store.transaction(roots.conn, transaction_owner="ScientificLead", operation="native_receipt", run_id=roots.run_id):
            # A second connection may have landed the same official call during preparation.
            existing = roots.conn.execute("SELECT * FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                          (thread, call_id)).fetchone()
            if existing:
                return saved_result(existing, turn, request_sha)
            research_window.require_completion(window)
            if tool == DECIDE:
                answer = v6_decisions.commit(roots.conn, prepared)
                if answer.get("outcome") != "core_evidence_available":
                    roots.terminal_products.record(thread, turn, call_id, "GX_ACCEPTED")
                    if v621_policy.science_execution(roots.conn, roots.run_id):
                        _submission_event(roots, p, accepted=answer["event_id"])
            elif tool == research_closeout.LINK:
                _, task, _, _ = v6_decisions.authority(roots.conn, **{k:v for k,v in identity.items() if k != "call_id"})
                answer = research_closeout.nominate(roots.conn, task, thread, nomination)
            elif tool == "gx_select_capability" and v62_native.module_scope(packet):
                answer = module_methods.commit(roots.conn, identity, args, packet, row, selected_descriptor)
            elif tool == v62_native.OPEN:
                answer = v62_modules.commit_open(roots.conn, prepared)
            elif tool == v6_recipes.WATCH:
                from . import research_joins
                v6_recipes.authorize(roots.conn, identity, tool, args)
                answer = research_joins.register(roots.conn, roots.run_id, args)
            elif tool in (v6_recipes.SAVE, v6_recipes.INSTANTIATE):
                v6_recipes.authorize(roots.conn, identity, tool, args)
                if tool == v6_recipes.SAVE:
                    answer = v6_recipes.publish(roots.conn, roots.run_id, *prepared)
            elif tool == v62_native.REPORT:
                answer = v62_reports.commit_report(roots.conn, prepared)
                if answer["author"]["call_id"] == call_id:
                    roots.terminal_products.record(thread, turn, call_id, "GX_ACCEPTED")
            elif tool == v62_native.CANCEL:
                from . import v62_modules
                answer = v62_modules.cancel_module(roots.conn, **identity, payload=args)
            elif tool == v62_native.QUERY:
                from . import catalog_survey, v62_catalog
                packet = brief(require_binding(roots.conn, roots.run_id, thread))
                if packet.get("catalog_discovery") and packet.get("full_brief_ref"):
                    snapshot, = roots.conn.execute("SELECT c.snapshot_json FROM research_contexts c "
                        "JOIN v6_decision_tasks t USING(context_ref) WHERE t.task_id=?", (row["task_id"],)).fetchone()
                    packet["catalog_discovery"] = json.loads(snapshot)["catalog_discovery"]
                from . import pi_method_scope
                interpret = "pi_method_scope" in args
                overview = args.get("kind", "all") == "all" and not args.get("offset") and not args.get("reference")
                if (overview and not interpret and packet.get("pi_method_scope", {}).get("status") == "pending"
                        and pi_method_scope.description(roots.conn, roots.run_id)["status"] == "pending"):
                    raise SchemaInvalid("Include pi_method_scope in the opening overview. Read the PI's Goal, quote "
                                        "its method clauses, or use [] when it places no method conditions.",
                                        details={"reason": "PI_METHOD_INTERPRETATION_REQUIRED"})
                if interpret:
                    if not overview:
                        raise SchemaInvalid("Interpret PI method scope in the opening overview (kind=all)")
                    v6_decisions.authority(roots.conn, **{k: v for k, v in identity.items() if k != "call_id"})
                    pi_method_scope.record(roots.conn, packet, args["pi_method_scope"], identity)
                    args = {k: v for k, v in args.items() if k != "pi_method_scope"}
                def query(arguments):
                    return v62_catalog.query(roots.conn, roots.run_id, packet, arguments)
                answer = (v62_native.perform_read(roots, row, p, packet, query)
                          if v62_native.module_scope(packet) else
                          catalog_survey.lead_query(roots.conn, packet, args, identity, query))
                if packet.get("pi_method_scope") and packet.get("scope") == "run":
                    answer["pi_method_scope"] = pi_method_scope.description(roots.conn, roots.run_id)
                if (packet.get("scope") == "run" and
                        answer.get("kind") == "method" and "execution_granted" in answer and args.get("reference")
                        and answer.get("status") != "reference_only"):
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
            if adoptions and (tool != DECIDE or answer.get("task_status") == "ACCEPTED"):
                for adoption in adoptions:
                    joined = recipe_instances.adopt(roots.conn, roots.run_id, adoption,
                                                    answer.get("aliases") if tool == DECIDE else None)
                    if tool == v6_recipes.INSTANTIATE:
                        answer["logical_join"] = {k:joined[k] for k in ("key","revision","state","registration_event","outcome_event")}
            record_call(roots.conn, p)
            roots.conn.execute("INSERT INTO p0_root_calls VALUES(?,?,?,?,?)",
                               (thread, call_id, turn, request_sha, ids.canonical_json(answer)))
            return answer
    except StaleRevision as exc:
        if tool == DECIDE:
            v6_decisions.reconsider_module_conflict(roots.conn, identity, exc)
        raise
    except (BudgetExceeded, SchemaInvalid) as exc:
        if tool == DECIDE and not v621_policy.science_execution(roots.conn, roots.run_id):
            remember_refusal(roots, p, request_sha, exc)
        raise


v62_native.register_tools(call)


def _submission_event(roots, params, *, error=None, accepted=None):
    # The public projection contains only categories and anonymous joins. The original
    # call and response stay with the private native owner and its exact retry identity.
    thread, turn, call_id = (params[k] for k in ("threadId", "turnId", "callId"))
    task_key = ids.sha256_of_obj([thread, turn])
    count = roots.conn.execute("""SELECT count(*) FROM events WHERE run_id=?
        AND kind='v624.decision.rejected' AND subject_id=?""", (roots.run_id, task_key)).fetchone()[0]
    common = {"submission": ids.sha256_of_obj([thread, turn, call_id]), "attempt": count + 1,
              "previous_rejections": count}
    if error is None:
        if not count:
            return
        kind = "v624.decision.corrected"
        common["decision_event"] = accepted
    else:
        kind = "v624.decision.rejected"
        field = error.details.get("public_field", error.details.get("field", "decision"))
        reason = error.details.get("reason", error.code)
        common.update(code=error.code,
                      reason=reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z_]{1,64}", reason) else error.code,
                      field=field if isinstance(field, str) and re.fullmatch(r"[a-z][a-z0-9_.\[\]]{0,80}",field) else "decision")
    store.append_event(roots.conn, run_id=roots.run_id, kind=kind, actor="gx",
                       subject_kind="decision_submission", subject_id=task_key, payload=common)


def remember_refusal(roots, params, request_sha, error, *, publish=False, conflicted_lead=None):
    """A refused candidate is auditable. It makes no graph, dispatch or budget change."""
    thread, turn, call_id = (params[k] for k in ("threadId", "turnId", "callId"))
    with store.transaction(roots.conn, transaction_owner="ScientificLead", operation="decision_refusal", run_id=roots.run_id):
        existing = roots.conn.execute("SELECT * FROM p0_root_calls WHERE thread_id=? AND call_id=?", (thread,call_id)).fetchone()
        if existing:
            return saved_result(existing, turn, request_sha) if publish or conflicted_lead is not None else None
        record_call(roots.conn, params)
        roots.conn.execute("INSERT INTO p0_root_calls VALUES(?,?,?,?,?)",
                           (thread, call_id, turn, request_sha, ids.canonical_json({"decision_refusal": error.to_payload()})))
        if publish:
            _submission_event(roots, params, error=error)
        if conflicted_lead is not None:
            # Keep the original refusal replayable once this brief loses its lease.
            from . import v6_decisions
            v6_decisions.reconsider_module_conflict(roots.conn, conflicted_lead, error)
