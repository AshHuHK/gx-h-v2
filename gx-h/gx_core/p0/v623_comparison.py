"""Bind a provisional scientific comparison to its accepted opening decision."""
from __future__ import annotations

import json

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied


def bindings(conn, run_id, version, cursor=None):
    sql = """SELECT json_extract(receipt_json,'$.comparison') FROM v6_decisions
             WHERE run_id=? AND json_extract(receipt_json,'$.goal_version')=?
             AND json_extract(receipt_json,'$.comparison') IS NOT NULL"""
    args = [run_id, version]
    if cursor is not None:
        sql += " AND json_extract(receipt_json,'$.event_id')<=?"
        args.append(cursor)
    return [json.loads(row[0]) for row in conn.execute(sql+" ORDER BY rowid", args)]


def _registered_spec(conn, run_id):
    data = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    files = data.get("files", [])
    for f in files:
        if f.get("role") != "workflow_spec" or not f.get("artifact_ref"):
            continue
        try:
            body = ArtifactService(conn).read_json(f["artifact_ref"])
        except (SchemaInvalid, ValueError, UnicodeError):
            continue
        registration = body.get("comparison_registration") if isinstance(body, dict) else None
        if not isinstance(registration, dict) or registration.get("schema") != "gx.public-comparison-plan/1":
            continue
        assay = registration["assay"]
        method = {key: assay[key] for key in ("capability","parameters","output_name","metric_id","unit")}
        sources = [r["sha256"] for r in files if r.get("role")==registration.get("reference_role")]
        return {"method":method, "material_role":registration["material_role"], "reference_sha256":sources,
                "method_card_ref":f["artifact_ref"], "method_card_sha256":f["sha256"]}
    return None


def prepare(conn, task, decision):
    """Read the immutable operator method card once, outside decision adoption."""
    if task["scope"]!="run" or decision.get("comparison_id") or decision.get("disposition"):
        return None
    if bindings(conn,task["run_id"], task["goal_version"]):
        return None
    spec = _registered_spec(conn,task["run_id"])
    proposed = decision.get("comparison_plan")
    if not spec and proposed:
        spec = {k:proposed[k] for k in ("method","material_role")}
    if not spec:
        return None
    from . import v622_measurement
    m = spec["method"]
    pin = v622_measurement.pin(conn,task["run_id"],{"observable":"/metrics/"+m["metric_id"],"method":{
        "capability":m["capability"],"output":m["output_name"],"parameters":m["parameters"],
        "metric_id":m["metric_id"],"unit":m["unit"],"input_role":spec["material_role"]}})
    if pin["measurement_spec"]["missing_parameters"]:
        raise SchemaInvalid("The comparison requires the effective parameters of its registered method")
    return {**spec, "measurement_spec_sha256":pin["measurement_spec_sha256"],
            "method_manifest_sha256":pin["manifest_sha256"]}


def adopt(conn, request, task, decision_ref, resolved_ops, event_id, *, registered=None):
    body = request["decision"]
    rid, version = request["run_id"], task["goal_version"]
    action_id = body.get("comparison_id")
    proposed = body.get("comparison_plan")
    if task["scope"] != "run":
        if proposed or action_id or body.get("disposition"):
            raise ScopeDenied("Only the Lead binds or reconsiders the project comparison")
        return {}
    if action_id or body.get("disposition"):
        found = next((b for b in bindings(conn,rid,version,task["event_cursor"])
                      if b["comparison_id"]==action_id),None)
        if not found or proposed or body.get("disposition") not in ("MAINTAIN","CHANGE","STOP"):
            raise SchemaInvalid("The decision must identify an existing comparison in its Goal and brief")
        if (body["disposition"]=="STOP") != bool(body.get("stop_reason")):
            raise SchemaInvalid("A stop requires its specific cause")
        return {"comparison_action":{k:body[k] for k in ("comparison_id","disposition","affected_scope",
                "metric_refs","stop_reason","rationale","evidence_refs") if k in body}}
    if bindings(conn,rid,version) or not proposed and conn.execute(
            """SELECT 1 FROM v6_decisions WHERE run_id=? AND json_extract(receipt_json,'$.scope')='run'
               AND json_extract(receipt_json,'$.goal_version')=? LIMIT 1""",(rid,version)).fetchone():
        return {}
    if not registered and not proposed:
        return {}
    branches = [(op["spec"]["branch"], body["ops"][op["source_index"]]["branch"])
                for op in resolved_ops if op["spec"]["op"]=="open_branch"]
    if proposed:
        branch = next((actual for actual,alias in branches if proposed["branch"] in (actual,alias)),None)
        if not branch:
            raise SchemaInvalid("The comparison plan must refer to a branch opened by this decision")
        if registered and any(registered[k]!=proposed[k] for k in ("method","material_role")):
            raise SchemaInvalid("The proposed comparison differs from its registered method and material role")
        endpoint = registered or {k:proposed[k] for k in ("method","material_role")}
    else:
        branch = branches[0][0] if len(branches)==1 else None
        endpoint = registered
    binding = {"run_id":rid,"goal_version":version,"decision_ref":decision_ref,"initial_event":event_id,
               "branch":branch,"opened_branches":[b[0] for b in branches],"method":endpoint["method"],
               **{k:v for k,v in endpoint.items() if k!="method"}}
    return {"comparison": {"comparison_id":"cmp_"+ids.sha256_of_obj(binding), **binding}}
