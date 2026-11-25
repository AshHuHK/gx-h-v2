"""Public, deterministic A1 Responses peer. Its only scientific input is the native request."""
import json

from native_probe_peer import declared_calls, wire_catalog
from official import brief_from, responses

QUALITY = "tl1a-coordinate-quality@1"
GEOMETRY = "deposited-chain-geometry@1"
MATERIALS = {
    "2RE9.cif": "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b",
    "2O0O.cif": "7e9381a6184ae8bc7f9c9423248c25133e135f0857814834cdd88ea8e9eb1103",
}
HUMAN = "Inventory the observable coordinate coverage in the two archived asymmetric-unit models. Report the source and scope of each measurement."


def answers(body):
    calls = {item["call_id"]: item for item in body["input"] if item.get("type") == "function_call"}
    completed = {}
    for item in body["input"]:
        if item.get("type") != "function_call_output" or item["call_id"] not in calls:
            continue
        call = calls[item["call_id"]]
        value = responses.output_json(item["output"])
        if isinstance(value, dict) and (value.get("error") or value.get("decision_refusal")):
            raise ValueError(f"{call['name']} refused: {value}")
        completed[(call["name"], json.loads(call["arguments"]).get("ref"))] = value
    return completed


def inventory_nodes(materials, branch):
    by_name = {m["name"]: m for m in materials}
    assert {n: material["sha256"] for n, material in by_name.items()} == MATERIALS
    return [
        {"op":"add_node", "node":"local:inventory", "branch":branch, "capability":QUALITY,
         "inputs":[by_name["2RE9.cif"]["artifact_ref"]], "parameters":{}, "depends_on":[],
         "question":"How many distinct residue positions are represented within the observed backbone union?"},
        {"op":"add_node", "node":"local:coverage", "branch":branch, "capability":GEOMETRY,
         "inputs":[by_name["2O0O.cif"]["artifact_ref"]], "parameters":{"mask":"common_ca"}, "depends_on":[],
         "question":"How many observed residue positions have shared C-alpha coordinates?"}]


def finding(result):
    if result.get("method") == QUALITY:
        count = result["mapping"]["observed_union_residues"]
        return f"2RE9 has {count} observed backbone-union residue positions in deposited model 1, entity 1, chains A/B/C."
    if result.get("method") == GEOMETRY and result.get("entry") == "2O0O" and result.get("mask") == "common_ca":
        return (f"2O0O has {result['selected_residues']} shared C-alpha positions among "
                f"{result['observed_union_residues']} observed backbone-union positions in those chains.")
    raise ValueError("Expected an authorized coordinate result")


def decide(ops, rationale, refs=(), watches=()):
    return "gx_decide_revision", {"decision":{"mode":"amend" if ops else "resolve", "ops":ops,
                "rationale":rationale, "evidence_refs":list(refs), "reconsider_when":list(watches)}}


def close(rationale, refs):
    return decide([{"op":"close_run", "reason":rationale, "claim_refs":[],
                    "unresolved":["A coordinate inventory characterizes these deposited models only."]}],
                  rationale, refs, [{"event":"goal_changed","subject_ref":"run"}])


def action(arm, brief, completed):
    module = brief.get("module")
    if arm == "module" and not module:
        reports = brief.get("module_reports", [])
        if reports:
            report, = reports
            return close("Received the scoped, sourced coordinate inventory. " +
                         " ".join(f["text"] for f in report["findings"]), [report["report_ref"]])
        if ("gx_open_module", None) in completed:
            return decide([], "Wait for the admitted module to complete its inventory round.",
                          watches=[{"event":"goal_changed","subject_ref":"run"}])
        catalog = brief["module_catalog"]
        assert "data_inventory" in json.dumps(catalog)
        return "gx_open_module", {
            "module_id":"data_inventory", "module_version":"1", "round":1, "operation_key":"measure-coordinate-coverage",
            "focus":"Inventory deposited coordinate coverage of the two input files.",
            "expected_delivery":"A sourced, separate coverage finding for each deposited coordinate file.",
            "input_refs":[m["artifact_ref"] for m in brief["materials"]], "capabilities":[QUALITY, GEOMETRY],
            "sub_budget":{"job_submissions":2,"cpu_seconds":90,"api_calls":3,"tokens":450000,"worker_tasks":4}}
    nodes = brief["nodes"]
    if not nodes:
        branch = module["branch_id"] if module else "local:direct"
        ops = inventory_nodes(brief["materials"], branch)
        if not module:
            ops.insert(0, {"op":"open_branch","branch":branch,
                           "hypothesis":"Deposited coordinates cover most observable chain positions.",
                           "alternative":"One or more chains has missing observations.",
                           "method_family":"coordinate inventory","discriminating_observation":"Measured residue coverage.",
                           "budget_limits":{"job_submissions":2,"cpu_seconds":90},
                           "stop_condition":"Return source-specific measured coverage."})
        return decide(ops, "Use the 2RE9-specific inventory and the shared-coordinate method approved for 2O0O.",
                      watches=[{"event":"job_settled","subject_ref":o["node"]} for o in ops if o["op"] == "add_node"])
    if any(not n.get("result_ref") for n in nodes):
        return decide([], "Wait for the already commissioned measurements.",
                      watches=[{"event":"job_settled","subject_ref":n["node_id"]} for n in nodes if not n.get("result_ref")])
    findings = []
    refs = []
    for node in nodes:
        ref = node["result_ref"]
        if ("gx_read_artifact", ref) not in completed:
            args = {"ref":ref}
            if module:
                args["operation_key"] = "read-" + node["node_id"]
            return "gx_read_artifact", args
        result = completed["gx_read_artifact", ref]
        refs.append(ref)
        findings.append({"text":finding(result),"source_refs":[ref],"kind":"observation"})
    if module:
        return "gx_submit_module_report", {"operation_key":"publish-inventory","status":"COMPLETE",
                "findings":findings,"artifact_refs":refs,"gaps":["Only deposited coordinates were measured."],
                "next_steps":["Use these coverage counts when comparing the two crystal models."]}
    return close(" ".join(f["text"] for f in findings), refs)


def answer(arm, body, ident):
    brief = brief_from(body)
    specs, exposed = wire_catalog(body)
    forbidden = [tool for tool in exposed if not (tool == "gx" or tool.startswith("gx_"))]
    assert not forbidden, forbidden
    assert not any(k in tool for tool in exposed for k in ("shell", "exec", "web_search", "read_file", "spawn"))
    name, args = action(arm, brief, answers(body))
    namespace, = [ns for ns, leaf in declared_calls(specs) if leaf == name]
    item = {"type":"function_call", "name":name, "call_id":"call-" + ident, "arguments":json.dumps(args)}
    if namespace:
        item["namespace"] = namespace
    return item, {"tool":name, "scope":brief.get("scope","run")}, brief, sorted(exposed)
