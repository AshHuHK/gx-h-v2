"""Versioned, receipt-backed conditions on preplanned scientific Jobs."""
import json
import math

from ..artifacts.service import ArtifactService
from ..domain import ids
from ..errors import DependencyUnmet, GxError, HoldActive, SchemaInvalid, ScopeDenied
from ..persistence import store
from . import manifest, v621_policy


def _wait(gate, why):
    raise HoldActive("Waiting for a verified dispatch observation: " + why,
                     details={"reason": "evidence_pending", "condition": why, "gate": gate})


def _invalid(conn, run_id, gate, why, recovery):
    if not v621_policy.enabled(conn, run_id):
        _wait(gate, why)
    raise HoldActive("Cannot verify the dispatch observation: " + why,
                     details={"reason": "evidence_invalid", "condition": why, "recovery": recovery, "gate": gate})


class ConditionFalse(DependencyUnmet):
    """Only raised for a scalar actually read from the pinned, verified output."""


def _visible_result(conn, run_id, consumer, producer, gate):
    if not v621_policy.enabled(conn, run_id):
        return
    # A Run-level proposal can reference another branch's public node identity before
    # its measurement is published. Wait for the same report release used by Lead briefs.
    owner = conn.execute("""SELECT m.status,r.report_ref FROM v62_modules m
            JOIN v6_nodes n ON n.node_id=? AND n.run_id=m.run_id
            LEFT JOIN v62_module_reports r USING(module_instance_id,run_id)
            WHERE m.run_id=? AND m.branch_id=? AND n.branch_id!=m.branch_id""",
            (consumer, run_id, producer["branch_id"])).fetchone()
    if owner is None:
        return
    from . import v62_reports
    if producer["result_ref"] in v62_reports.released_evidence_refs(conn, run_id):
        return
    if owner["status"] in ("STALE", "CANCELLED") or (owner["report_ref"] and
            owner["report_ref"] in v62_reports.released_report_refs(conn, run_id)):
        _invalid(conn, run_id, gate, "module did not publish the required result",
                 "Use an authorized published observation or commission a new measurement")
    _wait(gate, "awaiting publication of the scoped producer result")


def _observation(conn, run_id, gate, consumer):
    record = conn.execute("""SELECT n.version,n.status,n.job_id,n.result_ref,n.spec_json,n.branch_id,a.sha256 AS result_sha
        FROM v6_nodes n LEFT JOIN artifacts a ON a.artifact_ref=n.result_ref AND a.run_id=n.run_id
        WHERE n.run_id=? AND n.node_id=?""", (run_id, gate["producer_node"])).fetchone()
    if not record:
        _invalid(conn, run_id, gate, "producer is missing", "Restore the source graph or revise the gate")
    if record["status"] not in ("SUCCEEDED", "REUSED"):
        if record["status"] in ("FAILED", "CANCELLED", "BLOCKED"):
            if v621_policy.enabled(conn, run_id):
                raise DependencyUnmet("Producer ended without an eligible output",
                                      details={"reason": "producer_unavailable", "gate": gate})
        _wait(gate, "no successful producer receipt")
    if record["version"] != gate["producer_version"]:
        _invalid(conn, run_id, gate, "producer revision differs", "Commission a node for the intended producer version")
    if not record["job_id"] or not record["result_ref"] or not record["result_sha"]:
        _invalid(conn, run_id, gate, "missing immutable producer output", "Restore the original Job completion and output")
    _visible_result(conn, run_id, consumer, record, gate)
    if gate["source_ref"] not in json.loads(record["spec_json"])["inputs"]:
        _invalid(conn, run_id, gate, "unrelated source assignment", "Revise the assignment to cite the actual measured input")
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
    if v621_policy.enabled(conn, run_id):
        from .v6_jobs import confirmed_result_identity
        confirmed_result_identity(conn, gate["producer_node"], proof)
    source = next((i for i in proof["admission"]["inputs"] if i["artifact_ref"] == gate["source_ref"]), None)
    result = next((i for i in (proof["completion"] or {}).get("outputs", [])
                   if i["artifact_ref"] == observation["result_ref"]), None)
    if (proof["admission"]["run_id"] != run_id or not source or not result
            or result["sha256"] != observation["result_sha"]
            or result.get("runner_sha256", result["sha256"]) != result["sha256"]):
        _invalid(conn, run_id, gate, "Job manifest does not link the specified source and observed output",
                 "Restore the original Job manifest and its recorded input and output")
    material = conn.execute("SELECT sha256 FROM artifacts WHERE artifact_ref=? AND run_id=?",
                            (gate["source_ref"], run_id)).fetchone()
    if not material or material[0] != source["sha256"]:
        _invalid(conn, run_id, gate, "source version has changed", "Restore the registered source or commission a new version")
    return source["sha256"], proof


def _scalar_kind(value):
    if type(value) in (int, float):
        return "number" if type(value) is int or math.isfinite(value) else None
    return type(value) if value is None or type(value) in (bool, str) else None


def require_ready(conn, node, spec):
    """Read the verified scientific result outside the Job admission transaction."""
    gate = spec.get("evidence_gate")
    if gate is None:
        return None
    observation = _observation(conn, node["run_id"], gate, node["node_id"])
    try:
        source_sha, manifest_proof = _source_pin(conn, node["run_id"], gate, observation)
        manifest.require_current(conn, observation["job_id"])
        artifacts = ArtifactService(conn)
        if not artifacts.verify(gate["source_ref"])["ok"]:
            _invalid(conn, node["run_id"], gate, "source bytes changed", "Restore the original source bytes by SHA-256")
        actual = _fact(artifacts.read_json(observation["result_ref"]), gate["pointer"])
    except HoldActive:
        raise
    except (GxError, KeyError, IndexError, ValueError, TypeError, OSError) as err:
        detail = err.code if isinstance(err, GxError) else type(err).__name__
        _invalid(conn, node["run_id"], gate, "missing or unreadable scientific observation",
                 "Check source authority, the recorded output digest and JSON Pointer (" + detail + ")")
    expected = gate["equals"]
    if v621_policy.enabled(conn, node["run_id"]):
        kind = _scalar_kind(actual)
        if kind is None or kind != _scalar_kind(expected):
            _invalid(conn, node["run_id"], gate, "observed field has an incompatible type",
                     "Check the field selector and its declared scalar type")
        if actual != expected:
            raise ConditionFalse("Fixed observation does not satisfy the adopted condition", details={
                "reason": "evidence_condition_false", "gate": gate, "observed": actual,
                "job_id": observation["job_id"], "result_ref": observation["result_ref"],
                "result_sha256": observation["result_sha"], "source_sha256": source_sha,
                "admission_sha256": manifest_proof["admission_hash"],
                "completion_sha256": manifest_proof["manifest_hash"]})
    elif actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
        _wait(gate, "observed value does not satisfy the adopted condition")
    if gate["require_lead_review"] and not _approved(conn, node["run_id"], node["node_id"], gate, observation["result_ref"]):
        _wait(gate, "awaiting an evidence-citing Lead decision")
    return (observation["job_id"], observation["result_ref"], observation["result_sha"], source_sha)


def fence(conn, node, proof, *, require_approval=True):
    """GX compares durable identities again under the writer before any new funding is committed."""
    gate = json.loads(node["spec_json"]).get("evidence_gate")
    if gate is None:
        return
    obs = _observation(conn, node["run_id"], gate, node["node_id"])
    source = conn.execute("SELECT sha256 FROM artifacts WHERE run_id=? AND artifact_ref=?",
                          (node["run_id"], gate["source_ref"])).fetchone()
    if not source or proof != (obs["job_id"], obs["result_ref"], obs["result_sha"], source[0]):
        _invalid(conn, node["run_id"], gate, "prepared observation has changed",
                 "Reconcile the pinned producer observation before admission")
    manifest.require_current(conn, obs["job_id"])
    if require_approval and gate["require_lead_review"] and not _approved(conn, node["run_id"], node["node_id"], gate, obs["result_ref"]):
        _wait(gate, "awaiting an evidence-citing Lead decision")


def fence_negative(conn, node, condition):
    """A short writer rechecks the immutable identities that established the negative."""
    if json.loads(node["spec_json"]).get("evidence_gate") != condition["gate"]:
        _wait(condition["gate"], "the adopted evidence condition has changed")
    proof = tuple(condition[k] for k in ("job_id", "result_ref", "result_sha256", "source_sha256"))
    fence(conn, node, proof, require_approval=False)
    digests = conn.execute("SELECT admission_sha256,completion_sha256 FROM p0_execution_manifests WHERE job_id=?",
                           (condition["job_id"],)).fetchone()
    if not digests or tuple(digests) != (condition["admission_sha256"], condition["completion_sha256"]):
        _invalid(conn, node["run_id"], condition["gate"], "producer manifest changed during settlement",
                 "Restore the original Job manifest and verify its digests")


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
        observation = _observation(conn, request["run_id"], gate, node_id)
        reference = observation["result_ref"]
        if reference not in citations & visible:
            raise ScopeDenied("The Lead must cite the actual observed result from its frozen decision context")
        if not _approved(conn, request["run_id"], node_id, gate, reference):
            store.append_event(conn, run_id=request["run_id"], kind="v62.node.evidence_approved",
                               actor="v6.decision", subject_kind="node", subject_id=node_id,
                               payload={"observation_ref": reference, "decision_ref": decision_ref,
                                        "gate_sha256": ids.sha256_of_obj(gate)})
