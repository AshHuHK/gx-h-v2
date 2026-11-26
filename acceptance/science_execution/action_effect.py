"""Witness the successor's node withdrawal and method-version replacement on original records."""
import json

from acceptance.v623.live import action_witness
from gx_core.capabilities import manifest as capabilities, registry
from gx_core.p0 import manifest, v621_metrics, v623_comparison


def _cancelled(db, rid, accepted, item, node, card, method):
    spec = json.loads(node["spec_json"])
    if spec["capability"] != method["capability"] or capabilities.effective_parameters(
            card, spec["parameters"]) != capabilities.effective_parameters(card, method["parameters"]):
        return None
    # The settlement cites its commissioning decision. Link the later request through
    # the exact node, withdrawing decision and operation digest before accepting it.
    row = db.execute("""
        SELECT request.event_id AS requested, settled.event_id AS finished FROM events request
        JOIN events settled USING(run_id,subject_id)
        WHERE request.run_id=? AND request.subject_id=? AND request.kind='v6.node.cancel_requested'
          AND json_extract(request.payload_json,'$.decision_id')=?
          AND json_extract(request.payload_json,'$.op_digest')=?
          AND settled.kind='v6.node.settled' AND settled.new_state='CANCELLED'
          AND json_extract(settled.payload_json,'$.decision_ref')=?
          AND settled.prev_state='CANCEL_REQUESTED' AND request.event_id<=? AND settled.event_id>?
          AND coalesce(json_extract(settled.payload_json,'$.observed_job_status'),'')!='SUCCEEDED'
        """, (rid, node["node_id"], accepted["decision_ref"], item["op_digest"],
              node["decision_ref"], accepted["event_id"], accepted["event_id"])).fetchone()
    if row and node["status"] == "CANCELLED":
        return {"kind": "withdrawn_node", "ref": node["node_id"], "requested_event": row["requested"],
                "event_id": row["finished"], "decision_ref": accepted["decision_ref"]}
    return None


def _replaced(db, rid, accepted, node, binding, card):
    method = binding["method"]
    cap, version = method["capability"].rsplit("@", 1)
    if node["status"] != "SUCCEEDED" or node["decision_ref"] != accepted["decision_ref"] or not node["job_id"]:
        return None
    sealed = manifest.read_manifest(db, node["job_id"])
    admission, completion = sealed["admission"], sealed["completion"]
    actual = admission["capability"]
    owner = admission["owner"]
    executable = actual["id"] + "@" + actual["version"]
    if (owner.get("kind") != "graph_node" or owner.get("node_id") != node["node_id"]
            or owner.get("decision_ref") != accepted["decision_ref"] or owner.get("branch_id") != binding["branch"]
            or executable != json.loads(node["spec_json"])["capability"]
            or actual["id"] != cap or actual["version"] == version or admission["run_id"] != rid
            or not completion or completion["status"] != "SUCCEEDED"
            or [i["artifact_ref"] for i in admission["inputs"]] != binding["reference_refs"]
            or [i["sha256"] for i in admission["inputs"]] != binding["reference_sha256"]):
        return None
    endpoint = method["metric_id"]
    expected = next(o for o in card["outputs"] if o["name"] == method["output_name"])
    declared = next((o for o in admission["output_declarations"] if o["name"] == expected["name"]), {})
    roles = declared.get("source_roles", [i["role"] for i in admission["inputs"]])
    rule = expected.get("metric_specs", {}).get(endpoint)
    if (not rule or rule["unit"] != method["unit"] or declared.get("metric_specs", {}).get(endpoint) != rule
            or roles != expected.get("source_roles", capabilities.input_roles(card))):
        return None
    output = next((o for o in completion["outputs"] if o["output_name"] == expected["name"]), None)
    if not output:
        return None
    reading = next((m for m in v621_metrics.from_output(db, rid, output["artifact_ref"])
                    if m["schema"] == v621_metrics.SCHEMA and m["metric_id"] == endpoint and m["unit"] == rule["unit"]), None)
    end = db.execute("SELECT event_id FROM events WHERE run_id=? AND subject_id=? AND kind='job.transition' "
                     "AND new_state='SUCCEEDED' AND event_id>?", (rid, node["job_id"], accepted["event_id"])).fetchone()
    if end and reading:
        return {"kind": "completed_job", "ref": node["node_id"], "decision_ref": accepted["decision_ref"],
                "job_id": node["job_id"], "event_id": end["event_id"], "capability": executable,
                "result_ref": output["artifact_ref"], "metric_ref": reading["metric_ref"]}
    return None


def verified_change(db, rid, binding, receipt):
    """Check an accepted operation against its original comparison and a terminal effect."""
    if not receipt or not binding.get("reference_refs") or rid != binding["run_id"]:
        return None
    stored = db.execute("SELECT receipt_json FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                        (rid, receipt["decision_ref"])).fetchone()
    if not stored:
        return None
    accepted = json.loads(stored[0])
    if (accepted["event_id"] != receipt["event_id"] or accepted["scope"] != "run"
            or accepted["goal_version"] != binding["goal_version"] or accepted["event_id"] <= binding["initial_event"]):
        return None
    method = binding["method"]
    cap, version = method["capability"].rsplit("@", 1)
    card = registry.load_bundle(db, cap, version, expected_sha256=binding["method_manifest_sha256"])["manifest"]
    for item in accepted["resolved_ops"]:
        op = item["spec"]
        if op["op"] not in ("cancel_node", "add_node"):
            continue
        node = db.execute("SELECT * FROM v6_nodes WHERE run_id=? AND node_id=? AND branch_id=?",
                          (rid, op["node"], binding["branch"])).fetchone()
        if not node or json.loads(node["spec_json"])["inputs"] != binding["reference_refs"]:
            continue
        if op["op"] == "cancel_node":
            effect = _cancelled(db, rid, accepted, item, node, card, method)
        else:
            effect = _replaced(db, rid, accepted, node, binding, card)
        if effect:
            return effect
    return None


def _project_work(db, rid, binding, accepted, action, completed):
    """Compare effective settings and keep measurements assigned to another frozen comparison separate."""
    method = binding["method"]
    cap, version = method["capability"].rsplit("@", 1)
    card = registry.load_bundle(db, cap, version, expected_sha256=binding["method_manifest_sha256"])["manifest"]
    settings = capabilities.effective_parameters(card, method["parameters"])
    pinned = {**method, "parameters": settings}
    parallel = set()
    for other in v623_comparison.bindings(db, rid, binding["goal_version"], accepted["event_id"] - 1):
        candidate = other["method"]
        if (other.get("branch") == binding["branch"] or other.get("reference_refs") != binding["reference_refs"]
                or other.get("material_role") != binding.get("material_role")
                or other.get("method_manifest_sha256") != binding["method_manifest_sha256"]
                or candidate["capability"] != method["capability"]):
            continue
        if {**candidate, "parameters": capabilities.effective_parameters(card, candidate["parameters"])} == pinned:
            parallel.add(other["branch"])

    def project(jobs):
        result = []
        for job in jobs:
            if job.get("capability") == method["capability"]:
                job = {**job, "parameters": capabilities.effective_parameters(card, job["parameters"])}
                branch = job.get("branch", job.get("branch_id"))
                if (branch in parallel and job["parameters"] == settings
                        and job.get("input_sha256") == binding["reference_sha256"]):
                    continue
            result.append(job)
        return result

    return {**binding, "method": pinned}, {**action, "effects": project(action["effects"])}, project(completed)


def outcome_packet(db, trace, protocol, first):
    """Apply the successor's effect evidence to the original, frozen opportunity and actor."""
    packet, facts = action_witness.outcome_packet(trace, protocol, first)
    binding = trace.get("binding")
    if not facts["decision_ref"] or not binding or not binding.get("reference_refs"):
        return packet, facts
    window = next(w for w in trace["lead_windows"] if w["accepted"].get("decision_ref") == facts["decision_ref"])
    accepted, action = window["accepted"], window.get("action")
    if (not action or action["decision_ref"] != accepted["decision_ref"]
            or action["event_id"] != accepted["event_id"]):
        return packet, facts

    pinned, projected, jobs = _project_work(db, trace["run_id"], binding, accepted, action, trace["completed_jobs"])
    enacted, effect = action_witness.effect_for(projected, {**trace, "completed_jobs": jobs}, pinned,
                                               facts["stop_reason"], window)
    if enacted in ("NO_VERIFIED_EFFECT", "AUTOMATIC_ONLY"):
        change = verified_change(db, trace["run_id"], binding, accepted)
        # The frozen native extractor has already tied these effects to the driver's
        # actual graph and the original decision call on this turn.
        if change and any(e["kind"] == change["kind"] and e["ref"] == change["ref"] for e in action["effects"]):
            enacted, effect = "CHANGE", change
    if (effect and effect["kind"] == "completed_job" and "event_id" not in effect
            and enacted != facts["enacted"]):
        end = db.execute("SELECT event_id FROM events WHERE run_id=? AND subject_id=? AND kind='job.transition' "
                         "AND new_state='SUCCEEDED' AND event_id>?",
                         (trace["run_id"], effect["job_id"], accepted["event_id"])).fetchone()
        if end:
            effect = {**effect, "event_id": end[0]}
        else:
            enacted, effect = "NO_VERIFIED_EFFECT", None

    declared = accepted.get("comparison_action", {})
    aligned = bool(effect and declared.get("comparison_id") == binding["comparison_id"]
                   and enacted == declared.get("disposition"))
    facts.update(enacted=enacted, effect=effect, action_matches_intent=aligned)
    packet["lines"]["A"].update(effect=enacted, actual_event=bool(effect))
    return packet, facts
