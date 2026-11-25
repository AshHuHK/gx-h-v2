"""Scripted decisions using only the received Codex request and authorized tool results."""
import json
import re


METHOD = "deposited-chain-geometry@1"
INVENTORY = "data_inventory"
SURVEY = "method_workflow_survey"
BASELINE = "common_backbone"
FALLBACK = "common_ca"
PER_ENTRY = "Deposited model 1, entity 1, asymmetric-unit chains A/B/C only."
GOAL = (
    "Plan a descriptive measurement on the archived 2O0O crystal coordinates. The provisional "
    "follow-up is the shared four-atom backbone. For a deep investigation, delegate a pilot "
    "coverage inventory on 2RE9 before choosing the follow-up: if fewer than 60 percent of the "
    "observed positions survive the inventory selection, reassess the atom selection for 2O0O. "
    "For a methods survey, check applicability using the supplied public workflow card. "
    "Report the actual atom selection and coverage, with its source. "
    + PER_ENTRY
)
BASELINE_RATIONALE = (
    "The provisional follow-up is common_backbone on 2O0O. Hold the follow-up until the scoped "
    "report arrives, then decide whether the planned selection remains informative."
)
COVERAGE = re.compile(r"2RE9 inventory (\w+) retained (\d+) of (\d+) observed positions\.")


def decide(ops, reason, refs=(), watch=None):
    return "gx_decide_revision", {"decision": {
        "mode": "amend" if ops else "resolve", "ops": ops, "rationale": reason,
        "evidence_refs": list(refs),
        "reconsider_when": [watch or {"event": "goal_changed", "subject_ref": "run"}],
    }}


def branch(alias, mask):
    return {
        "op": "open_branch", "branch": alias,
        "hypothesis": f"The {mask} selection covers at least 60 percent of observed 2O0O positions.",
        "alternative": "Coverage of that atom selection falls below the threshold.",
        "method_family": f"2O0O {mask} coverage",
        "discriminating_observation": "Count selected residues and observed-union residue positions.",
        "budget_limits": {"job_submissions": 1, "cpu_seconds": 30},
        "stop_condition": "Close after recording the measured coverage for the selected atoms.",
    }


def geometry(alias, inputs, mask, owner):
    return {"op": "add_node", "node": alias, "branch": owner, "capability": METHOD,
            "inputs": [inputs], "parameters": {"mask": mask}, "depends_on": [],
            "question": f"How many observed positions are shared under the {mask} selection?"}


def one_material(brief, role):
    material, = [m for m in brief["materials"] if m["role"] == role]
    return material["artifact_ref"]


def read(ref):
    return "gx_read_artifact", {"ref": ref, "operation_key": f"read-{ref}"}


def historical_calls(items, unpack):
    issued = {v["call_id"]: v for v in items if v.get("type") == "function_call"}
    result = {}
    for v in items:
        if v.get("type") != "function_call_output" or v["call_id"] not in issued:
            continue
        call = issued[v["call_id"]]
        args = json.loads(call["arguments"])
        body = unpack(v["output"])
        if isinstance(body, dict) and ("error" in body or "decision_refusal" in body):
            raise ValueError(f"{call['name']} returned {body}")
        result[call["name"], args.get("ref")] = body
    return result


def report(finding, sources, gaps=()):
    return "gx_submit_module_report", {
        "operation_key": "publish-scoped-finding", "status": "COMPLETE",
        "findings": [{"text": finding, "source_refs": list(sources)}],
        "artifact_refs": list(sources), "gaps": list(gaps), "next_steps": [],
    }


def module_step(brief, replies, inventory_mask):
    module = brief["module"]
    if module["module_id"] == SURVEY:
        # The exact public resource and its exclusion are available only through this grant.
        ref = one_material(brief, "workflow_spec")
        if ("gx_read_artifact", ref) not in replies:
            return read(ref)
        card = replies["gx_read_artifact", ref]
        candidates = [c for c in card["candidates"] if c["kind"] == "goal_method"]
        eligible = [c["id"] for c in candidates if "2O0O" in c["supported_entries"]]
        excluded = [c["id"] for c in candidates if c["supported_entries"] == ["2RE9"]]
        if eligible != [METHOD] or excluded != ["tl1a-coordinate-quality@1"]:
            raise ValueError("Unexpected eligibility in the supplied workflow specification")
        return report("The public workflow card permits deposited-chain-geometry@1 on 2O0O. "
                      "Exclude tl1a-coordinate-quality@1 on 2O0O because its released scope is 2RE9.",
                      [ref])

    if module["module_id"] != INVENTORY:
        raise ValueError("Unexpected assigned module")
    if not brief["nodes"]:
        op = geometry("local:pilot", one_material(brief, "structure_2RE9"), inventory_mask, module["branch_id"])
        return decide([op], "Measure shared coverage within the pilot deposition under the assigned selection.",
                      watch={"event": "job_settled", "subject_ref": op["node"]})
    node, = brief["nodes"]
    ref = node["result_ref"]
    if not ref:
        return decide([], "Wait for the commissioned pilot.", watch={"event": "job_settled", "subject_ref": node["node_id"]})
    if ("gx_read_artifact", ref) not in replies:
        return read(ref)
    result = replies["gx_read_artifact", ref]
    if result["entry"] != "2RE9" or result["mask"] != inventory_mask:
        raise ValueError("Pilot output does not match the commissioned selection")
    n, total = result["selected_residues"], result["observed_union_residues"]
    finding = f"2RE9 inventory {result['mask']} retained {n} of {total} observed positions."
    gaps = ["Pilot coverage falls below the planned 60 percent threshold."] if 5*n < 3*total else []
    return report(finding, [ref], gaps)


def open_module(brief, identity):
    inventory = identity == INVENTORY
    return "gx_open_module", {
        "module_id": identity, "module_version": "1", "round": 1, "operation_key": "scope-the-pilot",
        "focus": ("Measure coverage of a pilot atom selection on 2RE9." if inventory else
                  "Survey suitability of the registered methods on 2O0O using the supplied workflow specification."),
        "expected_delivery": ("A sourced position count and denominator under the chosen selection." if inventory else
                              "A sourced shortlist and an explicit exclusion reason."),
        "input_refs": [one_material(brief, "structure_2RE9" if inventory else "workflow_spec")],
        "capabilities": [METHOD] if inventory else [],
        "sub_budget": {"job_submissions": 1 if inventory else 0, "cpu_seconds": 45 if inventory else 0,
                       "worker_tasks": 4, "api_calls": 4, "tokens": 400000},
    }


def close_or_stage(brief, replies, obs):
    value = obs["observation"]
    assert value["entry"] == "2O0O"
    n, total = value["selected_residues"], value["observed_union_residues"]
    finding = f"The 2O0O {value['mask']} selection retained {n} of {total} observed positions."
    ref = obs["artifact_ref"]
    previous = replies.get(("gx_stage_claim", None))
    if previous is None:
        return "gx_stage_claim", {
            "statement": finding, "scope": PER_ENTRY, "level": "open" if brief.get("research_evidence_version") else "limited_support",
            "criteria": "Selected residues divided by the observed-union count in the deposited coordinate file.",
            "limitations": "A single deposited model does not estimate population or biological variation.",
            "rationale": "The commissioned output reports the counts. Its legacy method declaration gives no output nature.",
            "evidence_refs": [ref],
        }
    return decide([{"op": "close_run", "reason": finding, "claim_refs": [previous["claim_ref"]],
                    "unresolved": ["The deposition does not establish independent sampling."]}], finding, [ref])


def lead_step(brief, replies):
    # The experiment condition is deliberately absent from this function's inputs.
    target = one_material(brief, "structure_2O0O")
    for obs in brief["observations"]:
        if (obs.get("observation") or {}).get("entry") == "2O0O":
            return close_or_stage(brief, replies, obs)
    effort = brief.get("research_effort", "quick")
    active = [b for b in brief["branches"] if b["status"] == "OPEN"]
    if effort == "quick":
        assert not brief.get("module_catalog") and not brief.get("module_reports")
        b = "local:direct"
        op = geometry("local:measure", target, BASELINE, b)
        return decide([branch(b, BASELINE), op], "Measure directly on 2O0O using the registered method.",
                      watch={"event": "job_settled", "subject_ref": op["node"]})
    reports = brief.get("module_reports", [])
    if not reports:
        identity = INVENTORY if effort == "deep" else SURVEY
        if ("gx_open_module", None) not in replies:
            assert identity in json.dumps(brief["module_catalog"])
            return open_module(brief, identity)
        return decide([branch("local:provisional", BASELINE)], BASELINE_RATIONALE)
    published, = reports
    provisional, = [b for b in active if b["spec"].get("method_family") == f"2O0O {BASELINE} coverage"]
    text = published["findings"][0]["text"]
    mask, label = BASELINE, "Maintain"
    if effort == "deep":
        found = COVERAGE.fullmatch(text)
        if not found:
            raise ValueError("The delivered report lacks selected and observed pilot positions")
        _, n, total = found.groups()
        if 5*int(n) < 3*int(total):
            mask, label = FALLBACK, "Change"
    elif "permits deposited-chain-geometry@1 on 2O0O" not in text:
        raise ValueError("The methods survey did not establish source applicability")
    reason = (f"{text} Change the provisional 2O0O measurement to {mask} to maximize shared-position "
              "coverage after the pilot shortfall." if label == "Change" else
              f"{text} Maintain the provisional 2O0O {mask} measurement; the report permits the original scope.")
    ops, owner = [], provisional["branch_id"]
    if label == "Change":
        ops.append({"op": "stop_branch", "branch": owner, "reason": reason})
        owner = "local:revised"
        ops.append(branch(owner, mask))
    job = geometry("local:follow-up", target, mask, owner)
    return decide([*ops, job], reason, [published["report_ref"]],
                  watch={"event": "job_settled", "subject_ref": job["node"]})


def response(body, ident, inventory_mask, marker, messages, unpack, catalog, declared):
    delivered = [t for it in reversed(body["input"]) if it.get("role") in {"user","developer"}
                 for t in messages(it) if marker in t]
    if not delivered:
        raise ValueError("Official host has not delivered a GX research brief")
    raw = delivered[0].split(marker, 1)[1]
    brief, end = json.JSONDecoder().raw_decode(raw)
    text = marker + raw[:end]
    specs, tools = catalog(body)
    assert all(t == "gx" or t.startswith("gx_") for t in tools)
    replies = historical_calls(body["input"], unpack)
    tool, args = (module_step(brief, replies, inventory_mask) if brief.get("module") else lead_step(brief, replies))
    namespace, = [ns for ns, name in declared(specs) if name == tool]
    call = {"type": "function_call", "name": tool, "arguments": json.dumps(args), "call_id": "call-" + ident}
    if namespace:
        call["namespace"] = namespace
    return call, {"brief_text": text, "brief": brief, "tool": tool, "args": args,
                  "tools": sorted(tools), "model": body["model"], "namespace": namespace,
                  "model_reasoning_effort": (body.get("reasoning") or {}).get("effort")}
