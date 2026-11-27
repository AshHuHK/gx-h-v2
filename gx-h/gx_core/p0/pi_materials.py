"""PI message attachments use the artifact store and existing scoped research-material grants."""
from pathlib import Path

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from ..orchestration import service
from ..persistence import store
from . import v621_assignment


REGISTERED = "pi.attachment.registered"
BRANCH_GRANTED = "pi.branch.material_granted"


def for_message(conn, run_id, message_id):
    rows = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind=? AND subject_id=? ORDER BY event_id",
                        (run_id, REGISTERED, message_id))
    return [store.loads(row, "payload_json") for row in rows]


def _branch(conn, run_id, branch_id):
    from . import research_dialogue
    return research_dialogue._target(conn, run_id, branch_id or "run")[1]


def _existing(conn, run_id, version, branch_id, ref):
    # Register only known Goal originals and PI-provided inputs. An artifact id is not a source grant.
    row = conn.execute("""SELECT f.value FROM runs r JOIN json_each(r.inputs_json,'$.files') f
        WHERE r.run_id=? AND json_extract(f.value,'$.artifact_ref')=?""", (run_id, ref)).fetchone()
    if row:
        item = store.loads(row, "value")
        return dict(name=Path(item["path"]).name, role=item.get("role") or "material", sha256=item["sha256"])
    row = conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND kind IN (?,?)
        AND json_extract(payload_json,'$.artifact_ref')=? AND json_extract(payload_json,'$.goal_version')=?
        AND (json_extract(payload_json,'$.branch_id') IS NULL OR json_extract(payload_json,'$.branch_id')=?)
        ORDER BY event_id LIMIT 1""", (run_id, v621_assignment.ASSIGNED, REGISTERED, ref, version, branch_id)).fetchone()
    if not row:
        raise ScopeDenied("Select an original registered for this Run and recipient")
    item = store.loads(row, "payload_json")
    return {key: item[key] for key in ("name", "role", "sha256")}


def register(conn, run_id, message_id, *, branch_id, attachments):
    """Called inside trusted message admission; no agent-facing path or file read is accepted."""
    if not conn.in_transaction:
        raise RuntimeError("Register PI attachments in the message transaction")
    if not isinstance(attachments, list):
        raise SchemaInvalid("Supply a list of PI attachments")
    if not attachments:
        return []
    sent = conn.execute("""SELECT c.goal_version FROM p0_operator_messages m JOIN p0_conversation_messages c USING(message_id)
        WHERE m.run_id=? AND m.message_id=?""", (run_id, message_id)).fetchone()
    if not sent:
        raise ScopeDenied("PI attachments require the received message in the selected Run")
    branch_id = _branch(conn, run_id, branch_id)
    assets = ArtifactService(conn)
    saved = for_message(conn, run_id, message_id)
    result = []
    for position, item in enumerate(attachments):
        if not isinstance(item, dict):
            raise SchemaInvalid("Attach a registered original or named UTF-8 text")
        if set(item) == {"artifact_ref"} and isinstance(item["artifact_ref"], str):
            ref = item["artifact_ref"]
            material = _existing(conn, run_id, sent[0], branch_id, ref)
        elif set(item) == {"name", "text"}:
            name, body = item["name"], item["text"]
            if (not isinstance(name, str) or not name.strip() or Path(name).name != name or
                    name in (".", "..") or "\x00" in name or "\\" in name or not isinstance(body, str)):
                raise SchemaInvalid("Supply a plain filename and UTF-8 text")
            raw = body.encode("utf-8")
            if len(raw) > service.MAX_COPIED_INPUT_BYTES:
                raise SchemaInvalid("The attachment exceeds the immutable Goal input bound")
            segment = ids.sha256_of_obj([message_id, position, name])
            published = assets.publish_bytes(namespace=run_id + "/pi-inputs", rel_path=segment,
                kind=service.INPUT_KIND, data=raw, run_id=run_id, media_type="text/plain; charset=utf-8",
                provenance={"name": name, "role": "material", "source_message_id": message_id,
                            "pi_private_branch": branch_id}, origin="INGEST", actor="PI")
            assets.discard_on_rollback(published)
            ref = published.artifact_ref
            material = dict(name=name, role="material", sha256=published.sha256)
        else:
            raise SchemaInvalid("Attach a registered original or named UTF-8 text")
        result.append(dict(**material, artifact_ref=ref, branch_id=branch_id, goal_version=sent[0],
                           source_kind="pi_material"))
    if saved:
        if saved != result:
            raise IdempotencyConflict("The message already carried different materials")
        return saved
    for material in result:
        store.append_event(conn, run_id=run_id, kind=REGISTERED, actor="PI", subject_kind="message",
                           subject_id=message_id, payload=material)
    return result


def grant(conn, run_id, message_id, *, clause_id, branch_id, artifact_refs):
    """Apply exactly the original message's selected recipient and immutable material identities."""
    if not conn.in_transaction:
        raise RuntimeError("Grant PI materials with their interpreted clause")
    if not isinstance(artifact_refs, list) or not artifact_refs or any(not isinstance(r, str) for r in artifact_refs):
        raise SchemaInvalid("Select the attached material references for this clause")
    branch_id = _branch(conn, run_id, branch_id)
    selected = {m["artifact_ref"]: m for m in for_message(conn, run_id, message_id) if m["branch_id"] == branch_id}
    if not set(artifact_refs) <= selected.keys():
        raise ScopeDenied("The requested attachments or recipient differ from the PI message")
    request = dict(run_id=run_id, message_id=message_id, clause_id=clause_id, branch_id=branch_id,
                   artifact_refs=list(dict.fromkeys(artifact_refs)))
    key = "pi.material.grant:" + ids.sha256_of_obj([run_id, message_id, clause_id])
    previous = store.begin_idempotent(conn, key, "pi.material.grant", request)
    if previous.replay:
        return previous.response
    goal = conn.execute("SELECT goal_version,status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    if goal["status"] != "OPEN" or any(selected[ref]["goal_version"] != goal["goal_version"] for ref in artifact_refs):
        raise StaleRevision("The material clause belongs to a previous or archived Goal")

    module = None
    if branch_id is not None:
        from . import research_dialogue
        if not research_dialogue._target(conn, run_id, branch_id)[2]:
            raise StaleRevision("The PI's material recipient has closed")
        module = conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND branch_id=?",
                              (run_id, branch_id)).fetchone()
        if module:
            from . import v62_catalog, v62_modules
            v62_modules.require_active(conn, run_id, module["scope"])
            decl = v62_catalog.resolve(conn, run_id, module["module_id"], module["module_version"])
            roles = {selected[ref]["role"] for ref in artifact_refs}
            if decl.get("published_inputs_only") or ("*" not in decl["input_roles"] and not roles <= set(decl["input_roles"])):
                raise ScopeDenied("This module's input roles exclude the supplied original")
    events = []
    for ref in request["artifact_refs"]:
        detail = dict(**selected[ref], clause_id=clause_id, message_id=message_id)
        if module:
            registration = conn.execute("""SELECT event_id FROM events WHERE run_id=? AND kind=? AND subject_id=?
                AND json_extract(payload_json,'$.artifact_ref')=?""", (run_id, REGISTERED, message_id, ref)).fetchone()
            detail.update(scope=module["scope"], module_instance_id=module["module_instance_id"],
                          assignment_event_id=registration[0])
            kind, subject, target = v621_assignment.GRANTED, "module", module["module_instance_id"]
        elif branch_id:
            kind, subject, target = BRANCH_GRANTED, "branch", branch_id
        else:
            kind, subject, target = v621_assignment.ASSIGNED, "artifact", ref
        events.append(store.append_event(conn, run_id=run_id, kind=kind, actor="PI", subject_kind=subject,
                                         subject_id=target, payload=detail))
    receipt = {**request, "grant_event_ids": events}
    store.complete_idempotent(conn, key, receipt)
    return receipt


def for_branch(conn, run_id, version, branch_id):
    rows = conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND kind=? AND subject_id=?
        AND json_extract(payload_json,'$.goal_version')=? ORDER BY event_id""", (run_id, BRANCH_GRANTED, branch_id, version))
    return [store.loads(row, "payload_json") for row in rows]


def private_originals(conn, run_id):
    rows = conn.execute("""SELECT a.artifact_ref FROM events e JOIN artifacts a ON a.run_id=e.run_id
        AND a.artifact_ref=json_extract(e.payload_json,'$.artifact_ref') WHERE e.run_id=? AND e.kind=?
        AND json_type(a.provenance_json,'$.pi_private_branch')='text'""", (run_id, REGISTERED))
    return {row[0] for row in rows}
