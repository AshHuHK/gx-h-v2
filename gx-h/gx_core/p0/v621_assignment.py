"""Assign a later original to a Goal and, optionally, one existing module."""
import hashlib
import json
from pathlib import Path

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, StaleRevision
from ..orchestration import service
from ..persistence import store
from . import v6_goal, v62_modules


ASSIGNED = "v621.material.assigned"
GRANTED = "v621.module.material_granted"


def _material(row):
    record = store.loads(row, "payload_json")
    return {k: record[k] for k in ("artifact_ref", "sha256", "name", "role")}


def for_lead(conn, run_id, goal_version, cursor):
    rows = conn.execute("""SELECT event_id,payload_json FROM events WHERE run_id=? AND kind=?
        AND event_id<=? AND json_extract(payload_json,'$.goal_version')=? ORDER BY event_id""",
        (run_id, ASSIGNED, cursor, goal_version))
    return [{**_material(row), "assignment_event_id": row["event_id"]} for row in rows]


def for_module(conn, module, *, cursor=None):
    rows = conn.execute("""SELECT event_id,payload_json FROM events WHERE run_id=? AND subject_id=?
        AND kind=? AND (? IS NULL OR event_id<=?) ORDER BY event_id""",
        (module["run_id"], module["module_instance_id"], GRANTED, cursor, cursor))
    return [{**_material(row), "grant_event_id": row["event_id"],
             "assignment_event_id": store.loads(row, "payload_json")["assignment_event_id"]} for row in rows]


def matches(conn, run_id, material):
    return conn.execute("""SELECT 1 FROM events e JOIN v6_goals g USING(run_id)
        WHERE e.event_id=? AND e.run_id=? AND e.kind=? AND e.subject_id=?
        AND json_extract(e.payload_json,'$.goal_version')=g.goal_version
        AND json_extract(e.payload_json,'$.sha256')=?""",
        (material["assignment_event_id"], run_id, ASSIGNED, material["artifact_ref"], material["sha256"])).fetchone()


def _bytes(path):
    path = Path(path).resolve()
    if service._forbidden(path) or not path.is_file():
        raise SchemaInvalid("Assigned material must be an authorized local file")
    with path.open("rb") as source:
        content = source.read(service.MAX_COPIED_INPUT_BYTES + 1)
    if len(content) > service.MAX_COPIED_INPUT_BYTES:
        raise SchemaInvalid("Assigned material exceeds the immutable Goal input bound")
    return content


def assign(conn, run_id, *, after_decision, source, path, sha256, role, metadata_path, actor,
           module_instance=None, media_type="application/octet-stream"):
    """Register bytes, acquisition, and the exact recipient grant in one operator transaction."""
    from . import v621_policy, v622_sources, v62_catalog, v62b_handoff
    if not v621_policy.enabled(conn, run_id):
        raise ScopeDenied("This Goal uses an earlier scientific evidence policy")
    if any(not isinstance(s, str) or not s.strip() for s in (source, role, media_type, after_decision)):
        raise SchemaInvalid("Supply a material role, media type, source and accepted decision")
    if role in v62b_handoff._GX_RESULT_ROLES:
        raise SchemaInvalid("A supplied material needs an original input role")
    raw = _bytes(path)
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise SchemaInvalid("Assigned material differs from the supplied SHA-256")
    metadata = json.loads(_bytes(metadata_path))
    identity = v622_sources.prepare(conn, run_id, source, metadata, sha256, media_type, raw=raw)
    name = Path(path).name
    key = "v621.assign:" + ids.sha256_of_obj([run_id, source])
    request = dict(decision_ref=after_decision, source=source, sha256=sha256, role=role, name=name,
                   acquisition=metadata, module_instance_id=module_instance, media_type=media_type)
    with store.transaction(conn, transaction_owner="ScientificLead", operation="assign_material", run_id=run_id):
        idem = store.begin_idempotent(conn, key, ASSIGNED, request)
        if idem.replay:
            return idem.response
        goal = conn.execute("SELECT goal_version,status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
        latest = conn.execute("""SELECT d.decision_ref,t.goal_version FROM v6_decisions d
            JOIN v6_decision_tasks t USING(task_id) WHERE d.run_id=? AND t.scope='run' ORDER BY d.rowid DESC LIMIT 1""",
            (run_id,)).fetchone()
        if not goal or goal["status"] != "OPEN" or not latest or tuple(latest) != (after_decision, goal["goal_version"]):
            raise StaleRevision("This material must follow the current accepted Run Lead decision")
        module = None
        if module_instance is not None:
            module = v62_modules.require_active(conn, run_id, "module:" + module_instance)
            decl = v62_catalog.resolve(conn, run_id, module["module_id"], module["module_version"])
            if decl.get("published_inputs_only") or ("*" not in decl["input_roles"] and role not in decl["input_roles"]):
                raise ScopeDenied("The delegated module cannot receive this original material role")
        assets = ArtifactService(conn)
        original = assets.publish_bytes(namespace=run_id + "/assignments", rel_path=ids.sha256_of_obj([source, sha256]),
            kind=service.INPUT_KIND, data=raw, run_id=run_id, media_type=media_type, expected_sha256=sha256,
            origin="INGEST", actor=actor, provenance={"role": role, "name": name, "source": source})
        assets.discard_on_rollback(original)
        observation = v6_goal.record_observation(conn, run_id, original.artifact_ref, source=source,
                                                  acquisition=metadata, prepared_identity=identity)
        material = dict(artifact_ref=original.artifact_ref, sha256=sha256, name=name, role=role)
        detail = dict(**material, source=source, goal_version=goal["goal_version"], decision_ref=after_decision,
                      acquisition_event_id=observation["event_id"])
        assignment = store.append_event(conn, run_id=run_id, kind=ASSIGNED, actor=actor, subject_kind="artifact",
                                        subject_id=original.artifact_ref, payload=detail)
        grant = None
        if module is not None:
            prior = v62_modules._task(conn, module)
            grant = store.append_event(conn, run_id=run_id, kind=GRANTED, actor=actor, subject_kind="module",
                subject_id=module_instance, payload=dict(**detail, assignment_event_id=assignment, scope=module["scope"],
                    module_instance_id=module_instance, previous_task_id=prior["task_id"] if prior else None))
            if prior and prior["status"] in v6_goal.ACTIVE_TASKS:
                v6_goal.cancel_decision(conn, run_id, prior["task_id"], reason="A new scoped material grant was assigned")
        lead = v6_goal._open(conn, run_id)
        if lead:
            v6_goal.cancel_decision(conn, run_id, lead["task_id"], reason="Operator assigned a new material")
        receipt = dict(**detail, run_id=run_id, event_id=assignment, module_event_id=grant,
                       module_instance_id=module_instance)
        store.complete_idempotent(conn, key, receipt)
        return receipt
