"""Versioned, receipt-backed conditions on preplanned scientific Jobs."""
import json

from ..artifacts.service import ArtifactService
from ..domain import ids
from ..errors import GxError, HoldActive, SchemaInvalid, ScopeDenied
from ..persistence import store
from . import manifest


def _wait(gate, why):
    raise HoldActive("Waiting for a verified dispatch observation: " + why,
                     details={"reason": "evidence_pending", "condition": why, "gate": gate})


def _observation(conn, run_id, gate):
    record = conn.execute("""SELECT n.version,n.status,n.job_id,n.result_ref,n.spec_json,a.sha256 AS result_sha
        FROM v6_nodes n LEFT JOIN artifacts a ON a.artifact_ref=n.result_ref AND a.run_id=n.run_id
        WHERE n.run_id=? AND n.node_id=?""", (run_id, gate["producer_node"])).fetchone()
    if not record or record["status"] not in ("SUCCEEDED", "REUSED"):
        _wait(gate, "no successful producer receipt")
    if record["version"] != gate["producer_version"]:
        _wait(gate, "producer revision differs")
    if not record["job_id"] or not record["result_ref"] or not record["result_sha"]:
        _wait(gate, "missing immutable producer output")
    if gate["source_ref"] not in json.loads(record["spec_json"])["inputs"]:
        _wait(gate, "unrelated source assignment")
    return record


def _approved(conn, run_id, node_id, gate, reference):
    return conn.execute("""SELECT 1 FROM events WHERE run_id=? AND kind='v62.node.evidence_approved'
        AND subject_kind='node' AND subject_id=? AND json_extract(payload_json,'$.observation_ref')=?
        AND json_extract(payload_json,'$.gate_sha256')=? LIMIT 1""",
        (run_id, node_id, reference, ids.sha256_of_obj(gate))).fetchone() is not None


def _fact(document, pointer):
    if not pointer.startswith("/"):
        raise SchemaInvalid("An observation condition must name a JSON field using a JSON Pointer")
    value = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token[0] == "0"):
                raise KeyError(token)
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise KeyError(token)
    return value


def _source_pin(conn, run_id, gate, observation):
    proof = manifest.read_manifest(conn, observation["job_id"])
    source = next((i for i in proof["admission"]["inputs"] if i["artifact_ref"] == gate["source_ref"]), None)
    result = next((i for i in (proof["completion"] or {}).get("outputs", [])
                   if i["artifact_ref"] == observation["result_ref"]), None)
    if (proof["admission"]["run_id"] != run_id or not source or not result
            or result["sha256"] != observation["result_sha"]
            or result.get("runner_sha256", result["sha256"]) != result["sha256"]):
        _wait(gate, "Job manifest does not link the specified source and observed output")
    material = conn.execute("SELECT sha256 FROM artifacts WHERE artifact_ref=? AND run_id=?",
                            (gate["source_ref"], run_id)).fetchone()
    if not material or material[0] != source["sha256"]:
        _wait(gate, "source version has changed")
    return source["sha256"]


def require_ready(conn, node, spec):
    """Read the verified scientific result outside the Job admission transaction."""
    gate = spec.get("evidence_gate")
    if gate is None:
        return None
    observation = _observation(conn, node["run_id"], gate)
    try:
        source_sha = _source_pin(conn, node["run_id"], gate, observation)
        manifest.require_current(conn, observation["job_id"])
        artifacts = ArtifactService(conn)
        if not artifacts.verify(gate["source_ref"])["ok"]:
            _wait(gate, "source bytes changed")
        actual = _fact(artifacts.read_json(observation["result_ref"]), gate["pointer"])
    except HoldActive:
        raise
    except (GxError, KeyError, IndexError, ValueError, TypeError):
        _wait(gate, "missing or unreadable scientific observation")
    expected = gate["equals"]
    if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
        _wait(gate, "observed value does not satisfy the adopted condition")
    if gate["require_lead_review"] and not _approved(conn, node["run_id"], node["node_id"], gate, observation["result_ref"]):
        _wait(gate, "awaiting an evidence-citing Lead decision")
    return (observation["job_id"], observation["result_ref"], observation["result_sha"], source_sha)


def fence(conn, node, proof):
    """GX compares durable identities again under the writer before any new funding is committed."""
    gate = json.loads(node["spec_json"]).get("evidence_gate")
    if gate is None:
        return
    obs = _observation(conn, node["run_id"], gate)
    source = conn.execute("SELECT sha256 FROM artifacts WHERE run_id=? AND artifact_ref=?",
                          (node["run_id"], gate["source_ref"])).fetchone()
    if not source or proof != (obs["job_id"], obs["result_ref"], obs["result_sha"], source[0]):
        _wait(gate, "prepared observation has changed")
    manifest.require_current(conn, obs["job_id"])
    if gate["require_lead_review"] and not _approved(conn, node["run_id"], node["node_id"], gate, obs["result_ref"]):
        _wait(gate, "awaiting an evidence-citing Lead decision")


def prepare_dispatch(conn, request):
    node_id = (request.get("binding") or {}).get("v6_node_id")
    if not node_id:
        return None
    node = conn.execute("""SELECT node_id,run_id,spec_json FROM v6_nodes WHERE node_id=? AND run_id=?
                           AND json_type(spec_json,'$.evidence_gate')='object'""",
                        (node_id, request["run_id"])).fetchone()
    return require_ready(conn, node, json.loads(node["spec_json"])) if node else None


def fence_dispatch(conn, request, proof):
    if proof is not None:
        node = conn.execute("SELECT node_id,run_id,spec_json FROM v6_nodes WHERE node_id=? AND run_id=?",
                            (request["binding"]["v6_node_id"], request["run_id"])).fetchone()
        fence(conn, node, proof)


def record_approvals(conn, request, decision_ref):
    """Record a genuine Lead decision against one exact observation and the pending target."""
    requested = request["decision"].get("approve_nodes", ())
    if not requested:
        return
    if request["scope"] != "run":
        raise ScopeDenied("Only the Run's Lead can approve a reviewed scientific condition")
    citations = set(request["decision"]["evidence_refs"])
    task = conn.execute("SELECT visible_refs_json FROM v6_decision_tasks WHERE run_id=? AND task_id=?",
                        (request["run_id"], request["decision_task_id"])).fetchone()
    visible = set(json.loads(task[0]))
    for node_id in requested:
        row = conn.execute("SELECT spec_json FROM v6_nodes WHERE run_id=? AND node_id=? AND status='PENDING'",
                           (request["run_id"], node_id)).fetchone()
        gate = json.loads(row[0]).get("evidence_gate") if row else None
        if not gate or not gate["require_lead_review"]:
            raise SchemaInvalid("The named node is not waiting for a Lead review")
        observation = _observation(conn, request["run_id"], gate)
        reference = observation["result_ref"]
        if reference not in citations & visible:
            raise ScopeDenied("The Lead must cite the actual observed result from its frozen decision context")
        if not _approved(conn, request["run_id"], node_id, gate, reference):
            store.append_event(conn, run_id=request["run_id"], kind="v62.node.evidence_approved",
                               actor="v6.decision", subject_kind="node", subject_id=node_id,
                               payload={"observation_ref": reference, "decision_ref": decision_ref,
                                        "gate_sha256": ids.sha256_of_obj(gate)})
