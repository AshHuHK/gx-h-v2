"""Versioned research intent from the operator client's acknowledged main-thread input.

Only the operator ingress calls stage_input. Its App Server transport calls accept_input
after acknowledging the exact staged input on the main thread. Neither is a model tool.
The client may replay the same receipt after restart. Receipt capture survives a stale
proposal or lost reply; authorization is consumed in the same transaction as its effect.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from ..capabilities.manifest import CAPABILITY_ID, parse_capability_version
from ..domain import clock, ids
from ..errors import ArtifactNotFound, IdempotencyConflict, PermissionDenied, SchemaInvalid, StaleRevision
from ..persistence import store

BAN_TYPES = {"hard_constraint", "method_ban"}
RESOURCE_KEYS = {"job_submissions", "cpu_seconds", "gpu_minutes", "gpu_jobs", "worker_tasks", "api_calls", "tokens"}
NATIVE_UNITS = {"api_calls": "gx_calls", "tokens": "reserved_tokens"}
ITEM_TYPES = BAN_TYPES | {"resource_limit", "objective", "soft_preference", "optimization_target",
                          "priority", "evaluation_spec", "data_source"}
OBJECTS = {"job": ("jobs", "job_id"), "evaluation": ("evaluations", "eval_id"),
           "task": ("tasks", "task_id"), "workflow": ("runs", "run_id")}
ROLES = {"execution", "evaluation", "context"}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _keys(obj, required, optional=()):
    return isinstance(obj, Mapping) and set(required) <= obj.keys() <= set(required) | set(optional)


def _item(row):
    if row is None:
        raise ArtifactNotFound("Intent item version does not exist")
    result = dict(row)
    result["value"] = json.loads(result.pop("value_json"))
    if result["item_type"] == "resource_limit" and result["value"] is not None:
        value = result["value"]
        result["effective_unit"] = value.get("unit", NATIVE_UNITS.get(value["resource"], value["resource"]))
    return result


def read_item(conn, run_id, item_id, version=None):
    sql = "SELECT * FROM p0_intent_versions WHERE run_id=? AND item_id=?"
    params = (run_id, item_id)
    if version is not None:
        sql += " AND version=?"
        params += (version,)
    return _item(conn.execute(sql + " ORDER BY version DESC LIMIT 1", params).fetchone())


def active_items(conn, run_id, item_types=None):
    sql = """SELECT v.* FROM p0_intent_versions v WHERE run_id=? AND status='active'
             AND NOT EXISTS (SELECT 1 FROM p0_intent_versions newer WHERE newer.run_id=v.run_id
                             AND newer.item_id=v.item_id AND newer.version>v.version)"""
    params = [run_id]
    if item_types is not None:
        types = [item_types] if isinstance(item_types, str) else list(item_types)
        if not types:
            return {}
        sql += " AND v.item_type IN (" + ",".join("?" for _ in types) + ")"
        params.extend(types)
    rows = conn.execute(sql + " ORDER BY item_id", params)
    return {row["item_id"]: _item(row) for row in rows}


current_items = active_items


def snapshot(conn, run_id, object_type, object_id):
    rows = conn.execute("""
        SELECT v.*, d.role FROM p0_intent_dependencies d JOIN p0_intent_versions v
        ON (d.run_id,d.item_id,d.item_version,d.item_hash)=(v.run_id,v.item_id,v.version,v.sha256)
        WHERE d.run_id=? AND d.object_type=? AND d.object_id=? ORDER BY d.item_id""",
        (run_id, object_type, object_id))
    return {row["item_id"]: _item(row) for row in rows}


def bind_dependencies(conn, run_id, object_type, object_id, item_roles):
    """Capture the items this object uses at creation. Retries preserve its original pins."""
    if not isinstance(object_type, str) or object_type not in OBJECTS or not isinstance(item_roles, Mapping):
        raise SchemaInvalid("Unknown dependency object or invalid item roles")
    table, key = OBJECTS[object_type]
    with store.transaction(conn):
        if not conn.execute(f"SELECT 1 FROM {table} WHERE run_id=? AND {key}=?",
                            (run_id, object_id)).fetchone():
            raise ArtifactNotFound("The dependency owner is absent from this run")
        bound = snapshot(conn, run_id, object_type, object_id)
        for item_id, role in item_roles.items():
            if not isinstance(role, str) or role not in ROLES:
                raise SchemaInvalid("Dependency role must be execution, evaluation or context")
            if item_id in bound:
                if bound[item_id]["role"] != role:
                    raise IdempotencyConflict("This dependency was registered with a different role")
                continue
            item = read_item(conn, run_id, item_id)
            if item["status"] != "active":
                raise StaleRevision("Cannot bind a new object to a removed intent item")
            conn.execute("INSERT INTO p0_intent_dependencies VALUES(?,?,?,?,?,?,?)",
                         (run_id, object_type, object_id, item_id, item["version"], item["sha256"], role))
        return snapshot(conn, run_id, object_type, object_id)


def bind_evaluation(conn, run_id, eval_id, item_id, version):
    """Bind a new original evaluation to its measured specification across concurrent edits."""
    from .impact import enqueue_reevaluation
    with store.transaction(conn):
        if not conn.execute("SELECT 1 FROM evaluations WHERE run_id=? AND eval_id=?", (run_id, eval_id)).fetchone():
            raise ArtifactNotFound("Evaluation absent from this run")
        pin = read_item(conn, run_id, item_id, version)
        if pin["item_type"] != "evaluation_spec" or pin["status"] != "active":
            raise SchemaInvalid("An evaluation needs the active specification from its original plan")
        conn.execute("INSERT INTO p0_intent_dependencies VALUES(?,'evaluation',?,?,?,?,'evaluation')"
                     " ON CONFLICT(run_id,object_type,object_id,item_id) DO NOTHING",
                     (run_id, eval_id, item_id, version, pin["sha256"]))
        bound = snapshot(conn, run_id, "evaluation", eval_id)[item_id]
        if bound["version"] != version or bound["role"] != "evaluation":
            raise IdempotencyConflict("This evaluation was measured under a different specification")
        current = read_item(conn, run_id, item_id)
        if current["version"] > version and current["status"] == "active":
            enqueue_reevaluation(conn, run_id, eval_id, item_id, current["version"], current["value"],
                                 current["change_id"])
        return bound


def forbidden_capability(capability_id, version, items):
    for item in items.values():
        if item["item_type"] in BAN_TYPES:
            forbidden = item["value"]["forbidden_capabilities"]
            if capability_id in forbidden or f"{capability_id}@{version}" in forbidden:
                return True
    return False


def job_current(conn, job_id, *, items=None):
    """Admission checks project's current bans. Historical expenses retain original meaning."""
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if job is None:
        raise ArtifactNotFound("Unknown Job")
    current = active_items(conn, job["run_id"]) if items is None else items
    if forbidden_capability(job["capability_id"], job["capability_version"], current):
        return False
    for key, pin in snapshot(conn, job["run_id"], "job", job_id).items():
        if pin["role"] == "execution" and pin["item_type"] not in BAN_TYPES | {
                "resource_limit", "evaluation_spec", "soft_preference", "priority"}:
            if key not in current or pin["version"] != current[key]["version"]:
                return False
    return True


def bind_main(conn, run_id, thread_id):
    if not _text(thread_id):
        raise SchemaInvalid("Main thread requires the thread/start response ID")
    with store.transaction(conn):
        if not conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            raise ArtifactNotFound("Unknown run")
        conn.execute("INSERT INTO p0_intent_main VALUES(?,?) ON CONFLICT(run_id) DO NOTHING",
                     (run_id, thread_id))
        saved = conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (run_id,)).fetchone()
        if saved[0] != thread_id:
            raise IdempotencyConflict("The run is bound to a different main thread")
    return thread_id


def unbind_empty_main(conn, run_id, thread_id):
    """Release a lost, never-used thread only when its exact binding has no user inputs."""
    with store.transaction(conn):
        changed = conn.execute("DELETE FROM p0_intent_main WHERE run_id=? AND thread_id=? AND NOT EXISTS"
                               " (SELECT 1 FROM p0_human_inputs WHERE run_id=?)", (run_id, thread_id, run_id))
        return changed.rowcount == 1


def _validate_value(item_type, value):
    if value is None:
        return
    if item_type in BAN_TYPES:
        if not (_keys(value, {"forbidden_capabilities"}) and isinstance(value["forbidden_capabilities"], list)
                and all(_text(entry) for entry in value["forbidden_capabilities"])):
            raise SchemaInvalid("A hard capability rule requires a list of forbidden capability identifiers")
        for cap in value["forbidden_capabilities"]:
            if "@" in cap:
                parse_capability_version(cap)
            elif not CAPABILITY_ID.fullmatch(cap):
                raise SchemaInvalid("A hard rule needs a capability ID or a pinned capability version")
    elif item_type == "resource_limit":
        if not (_keys(value, {"resource", "max"}, {"unit"}) and isinstance(value["resource"], str)
                and ("unit" not in value or _text(value["unit"]))
                and value["resource"] in RESOURCE_KEYS
                and _number(value["max"]) and value["max"] >= 0):
            raise SchemaInvalid("A resource limit requires a supported resource and nonnegative finite max")
    elif item_type == "evaluation_spec":
        pinned = _keys(value, {"eval_spec_id", "version"}) and all(_text(v) for v in value.values())
        threshold = _keys(value, {"threshold"}, {"evaluator"}) and _number(value["threshold"])
        if not (pinned or threshold):
            raise SchemaInvalid("A scoring specification requires a frozen spec reference or a numeric threshold")
        evaluator = value.get("evaluator")
        if evaluator is not None:
            from ..plugins.evaluator import installed_plugin_ref
            if not isinstance(evaluator, dict) or not _text(evaluator.get("package_version")):
                raise SchemaInvalid("A scoring plugin requires its published package version and content pin")
            try:
                installed = installed_plugin_ref(evaluator["package_version"])
            except (ValueError, OSError) as exc:
                raise SchemaInvalid("The requested scoring plugin version is unavailable") from exc
            if evaluator != installed:
                raise SchemaInvalid("The requested scoring plugin does not match its published content pin")
    elif item_type == "objective" and not _text(value):
        raise SchemaInvalid("A research objective requires nonempty text")


def _check_action(action):
    if not isinstance(action, dict):
        raise SchemaInvalid("Expected an operator input object")
    kind = action.get("kind")
    if kind in ("proposal", "supplement"):
        options = ("source_ref",) if kind == "supplement" else ()
        if not (_keys(action, {"kind", "text"}, options) and _text(action["text"])
                and ("source_ref" not in action or _text(action["source_ref"]))):
            raise SchemaInvalid("Expected a user note with text and an optional supplement source")
    elif kind == "confirm":
        if not (_keys(action, {"kind", "change_id", "preview_sha256"}) and
                _text(action["change_id"]) and _text(action["preview_sha256"])):
            raise SchemaInvalid("Confirmation must identify the exact preview")
    elif kind == "change":
        if not (_keys(action, {"kind", "items"}) and isinstance(action["items"], list) and action["items"]):
            raise SchemaInvalid("An explicit change requires nonempty item edits")
        seen = set()
        for edit in action["items"]:
            if not (_keys(edit, {"item_id", "item_type", "expected_version", "value"})
                    and _text(edit["item_id"]) and edit["item_id"] not in seen
                    and isinstance(edit["item_type"], str) and edit["item_type"] in ITEM_TYPES
                    and type(edit["expected_version"]) is int and edit["expected_version"] >= 0):
                raise SchemaInvalid("Each edit must identify one distinct item, type, expected version and new value")
            seen.add(edit["item_id"])
            _validate_value(edit["item_type"], edit["value"])
    else:
        raise SchemaInvalid("Input kind must be proposal, supplement, change or confirm")
    try:
        return json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaInvalid("Operator input must contain finite JSON values") from exc


def _preview_for_confirmation(conn, run_id, action):
    change = conn.execute("SELECT * FROM p0_intent_changes WHERE run_id=? AND change_id=?",
                          (run_id, action["change_id"])).fetchone()
    if change is None or change["preview_sha256"] != action["preview_sha256"]:
        raise PermissionDenied("The confirmation does not name this run's exact preview")
    return change


def stage_input(conn, run_id, message_id, action):
    """Journal an action received from the trusted operator before sending it to Codex."""
    if not _text(message_id):
        raise SchemaInvalid("The client input requires a stable message ID")
    encoded = _check_action(action)
    with store.transaction(conn):
        if not conn.execute("SELECT 1 FROM p0_intent_main WHERE run_id=?", (run_id,)).fetchone():
            raise PermissionDenied("The run has no registered main thread")
        if action["kind"] == "confirm":
            _preview_for_confirmation(conn, run_id, action)
        conn.execute("INSERT INTO p0_human_inputs(message_id,run_id,action_json,staged_at)"
                     " VALUES(?,?,?,?) ON CONFLICT(message_id) DO NOTHING",
                     (message_id, run_id, encoded, clock.now_iso()))
        saved = conn.execute("SELECT * FROM p0_human_inputs WHERE message_id=?", (message_id,)).fetchone()
        if saved["run_id"] != run_id or saved["action_json"] != encoded:
            raise IdempotencyConflict("This client message ID already identifies a different operator input")
        return {"message_id": message_id, "status": "staged"}


def _unsupported_resource(edits):
    """No atomic group may change if its requested physical units cannot be enforced."""
    for edit in edits:
        value = edit["value"]
        if edit["item_type"] != "resource_limit" or value is None:
            continue
        resource, requested = value["resource"], value.get("unit")
        supported = NATIVE_UNITS.get(resource, resource)
        if requested == supported or (requested is None and resource not in NATIVE_UNITS):
            continue
        if resource == "api_calls":
            explanation = "GX counts admitted calls, including native turn/start. Upstream provider requests can differ."
        elif resource == "tokens":
            explanation = "GX reserves a token allowance and records reported usage. Native turns can exceed their projection."
        else:
            explanation = f"The ledger measures {resource}."
        return {"status": "unsupported", "resource": resource, "requested_unit": requested,
                "supported_unit": supported, "message": f"{explanation} To set the GX limit, specify unit={supported}."}
    return None


def _changes(conn, run_id, edits):
    diffs = []
    for edit in sorted(edits, key=lambda e: e["item_id"]):
        key, kind, expected = edit["item_id"], edit["item_type"], edit["expected_version"]
        new = edit["value"]
        try:
            previous = read_item(conn, run_id, key)
        except ArtifactNotFound:
            previous = None
        actual = previous["version"] if previous else 0
        if actual != expected:
            raise StaleRevision(f"Intent item {key} is at version {actual}, expected {expected}")
        if previous and previous["item_type"] != kind:
            raise SchemaInvalid("The type of an existing item is immutable")
        if kind == "evaluation_spec" and isinstance(new, dict) and "eval_spec_id" in new:
            spec = conn.execute("SELECT status FROM eval_specs WHERE eval_spec_id=? AND version=? AND namespace=?",
                                (new["eval_spec_id"], new["version"], run_id)).fetchone()
            if not spec or spec["status"] != "FROZEN":
                raise SchemaInvalid("The selected evaluation specification has no frozen version")
        old = previous["value"] if previous else None
        if new is not None and kind in BAN_TYPES:
            prohibited = set(new["forbidden_capabilities"])
            new = {"forbidden_capabilities": sorted(v for v in prohibited
                                                     if "@" not in v or v.split("@")[0] not in prohibited)}
        if ((kind in {"resource_limit", "evaluation_spec"} and old == new)
                or ids.canonical_json(old) == ids.canonical_json(new)):
            continue
        diffs.append({"item_id": key, "item_type": kind, "old_version": actual,
                      "new_version": actual + 1, "old_value": old, "new_value": new})
    return diffs


def _requires_confirmation(diff):
    old, new, kind = diff["old_value"], diff["new_value"], diff["item_type"]
    if old is None:
        return False
    if kind == "objective":
        return True
    if kind == "resource_limit":
        if new is None or new["resource"] != old["resource"]:
            return True
        # Rows written before explicit units measured the original GX ledger.
        native_unit = NATIVE_UNITS.get(old["resource"], old["resource"])
        return new.get("unit", native_unit) != old.get("unit", native_unit) or new["max"] > old["max"]
    if kind in BAN_TYPES:
        if new is None:
            return True
        banned = set(new["forbidden_capabilities"])
        return any(cap not in banned and cap.split("@", 1)[0] not in banned
                   for cap in old["forbidden_capabilities"])
    return False


def _commit(conn, run_id, change, message_id, diffs):
    from .impact import apply_impact
    now = clock.now_iso()
    for diff in diffs:
        # BEGIN IMMEDIATE serializes this re-read and the version insert with job completion.
        current = conn.execute("SELECT COALESCE(MAX(version),0) FROM p0_intent_versions"
                               " WHERE run_id=? AND item_id=?", (run_id, diff["item_id"])).fetchone()[0]
        if current != diff["old_version"]:
            raise StaleRevision(f"Preview for {diff['item_id']} expected {diff['old_version']}, now {current}")
        new = diff["new_value"]
        conn.execute("INSERT INTO p0_intent_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (run_id, diff["item_id"], diff["new_version"], diff["item_type"], ids.canonical_json(new),
                      ids.sha256_of_obj(new), "removed" if new is None else "active", change["change_id"],
                      message_id, now))
    impact = apply_impact(conn, run_id, change["change_id"], diffs, commit=True)
    conn.execute("UPDATE p0_intent_changes SET status='committed',confirmation_message_id=?,"
                 "impact_json=?,committed_at=? WHERE change_id=?",
                 (message_id if message_id != change["source_message_id"] else None,
                  ids.canonical_json(impact), now, change["change_id"]))
    store.append_event(conn, run_id=run_id, actor="operator", kind="intent.committed", subject_kind="change",
                       subject_id=change["change_id"], evidence_level="EXECUTION_OBSERVED",
                       payload={"message_id": message_id, "preview_sha256": change["preview_sha256"]})
    return {"status": "committed", "change_id": change["change_id"], "preview_sha256": change["preview_sha256"],
            "preview": diffs, "impact": impact}


def _change(conn, run_id, message_id, action):
    from .impact import apply_impact
    if rejected := _unsupported_resource(action["items"]):
        return rejected
    diffs = _changes(conn, run_id, action["items"])
    if not diffs:
        return {"status": "unchanged", "preview": [], "impact": []}
    change_id = ids.new_id("request")
    # The digest commits the run, source message and old/new values/versions, independent of impact count.
    digest = ids.sha256_of_obj({"run_id": run_id, "message_id": message_id, "changes": diffs})
    existing_objective = any(i["item_type"] == "objective" for i in active_items(conn, run_id).values())
    confirm = any(_requires_confirmation(d) or (existing_objective and d["item_type"] == "objective")
                  for d in diffs)
    impact = apply_impact(conn, run_id, change_id, diffs, commit=False) if confirm else []
    conn.execute("INSERT INTO p0_intent_changes(change_id,run_id,source_message_id,status,"
                 "preview_json,preview_sha256,impact_json,created_at) VALUES(?,?,?,'awaiting_confirmation',?,?,?,?)",
                 (change_id, run_id, message_id, ids.canonical_json(diffs), digest, ids.canonical_json(impact),
                  clock.now_iso()))
    if confirm:
        return {"status": "awaiting_confirmation", "change_id": change_id, "preview_sha256": digest,
                "preview": diffs, "impact": impact}
    return _commit(conn, run_id, {"change_id": change_id, "source_message_id": message_id, "preview_sha256": digest},
                   message_id, diffs)


def _confirm(conn, run_id, turn_id, message_id, action):
    change = _preview_for_confirmation(conn, run_id, action)
    origin = conn.execute("SELECT receipt_turn_id FROM p0_human_inputs WHERE message_id=?",
                          (change["source_message_id"],)).fetchone()
    if origin["receipt_turn_id"] == turn_id:
        raise PermissionDenied("This change needs confirmation in a later human main-thread turn")
    if change["status"] == "committed":
        raise PermissionDenied("This change has already been committed")
    return _commit(conn, run_id, change, message_id, json.loads(change["preview_json"]))


def _note(conn, run_id, message_id, action):
    if action["kind"] == "proposal":
        return {"status": "needs_clarification", "message": "Which intent item should change, and what new value do you want?"}
    source = ids.new_id("source")
    note = action["text"]
    now = clock.now_iso()
    provenance = {"run_id": run_id, "client_message_id": message_id}
    if "source_ref" in action:
        provenance["source_ref"] = action["source_ref"]
    conn.execute("INSERT INTO sources(source_id,namespace,kind,text_coverage,origin,provenance_json,"
                 "segments_count,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
                 (source, run_id, "operator_note", "FULL_TEXT", "human_input", ids.canonical_json(provenance), now, now))
    conn.execute("INSERT INTO source_segments(source_id,segment_id,ordinal,kind,text,text_sha256,char_count)"
                 " VALUES(?, 'note', 0, 'note', ?, ?, ?)",
                 (source, note, hashlib.sha256(note.encode("utf-8")).hexdigest(), len(note)))
    return {"status": "supplement_registered", "source_id": source}


def _receipt(conn, run_id, thread_id, turn_id, message_id):
    if not _text(turn_id):
        raise PermissionDenied("No official human turn receipt")
    row = conn.execute("""
        SELECT i.* FROM p0_human_inputs i JOIN p0_intent_main m ON i.run_id=m.run_id
        WHERE i.run_id=? AND m.thread_id=? AND i.message_id=?""", (run_id, thread_id, message_id)).fetchone()
    if row is None or (row["receipt_turn_id"] and row["receipt_turn_id"] != turn_id):
        raise PermissionDenied("No matching main-thread operator input for this receipt")
    return row


def accept_input(conn, run_id, thread_id, turn_id, message_id):
    """Apply a trusted transport acknowledgement; never accepts action data from the caller."""
    with store.transaction(conn):
        _receipt(conn, run_id, thread_id, turn_id, message_id)
        conn.execute("UPDATE p0_human_inputs SET receipt_turn_id=?,received_at=?"
                     " WHERE message_id=? AND receipt_turn_id IS NULL", (turn_id, clock.now_iso(), message_id))
    with store.transaction(conn):
        row = _receipt(conn, run_id, thread_id, turn_id, message_id)
        if row["consumed_at"]:
            return json.loads(row["result_json"])
        action = json.loads(row["action_json"])
        if action["kind"] == "change":
            result = _change(conn, run_id, message_id, action)
        elif action["kind"] == "confirm":
            result = _confirm(conn, run_id, turn_id, message_id, action)
        else:
            result = _note(conn, run_id, message_id, action)
        conn.execute("UPDATE p0_human_inputs SET consumed_at=?,result_json=? WHERE message_id=?",
                     (clock.now_iso(), ids.canonical_json(result), message_id))
        return result
