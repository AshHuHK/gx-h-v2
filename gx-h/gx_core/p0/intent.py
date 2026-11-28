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
RESOURCE_KEYS = {"job_submissions", "cpu_seconds", "gpu_minutes", "gpu_jobs", "worker_tasks", "api_calls", "paid_api_calls", "tokens"}
NATIVE_UNITS = {"api_calls": "gx_calls", "tokens": "reserved_tokens"}
ITEM_TYPES = BAN_TYPES | {"resource_limit", "objective", "soft_preference", "optimization_target",
                          "priority", "evaluation_spec", "data_source"}
OBJECTS = {"job": ("jobs", "job_id"), "evaluation": ("evaluations", "eval_id"),
           "task": ("tasks", "task_id"), "workflow": ("runs", "run_id")}
ROLES = {"execution", "evaluation", "context"}
CANCEL_ACK = "cancel_listed_jobs"
IMPACT_ACK = "apply_listed_impacts"


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
        conn.execute("INSERT INTO p0_intent_main VALUES(?,?) ON CONFLICT(run_id) DO UPDATE "
                     "SET thread_id=excluded.thread_id WHERE p0_intent_main.thread_id=''",
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
        if changed.rowcount:
            return True
        # A local bootstrap can precede the first real thread. Local receipts survive replacement.
        return bool(conn.execute("UPDATE p0_intent_main SET thread_id='' WHERE run_id=? AND thread_id=? "
                                 "AND NOT EXISTS(SELECT 1 FROM p0_human_inputs WHERE run_id=? "
                                 "AND (received_at IS NULL OR receipt_turn_id IS NOT NULL))",
                                 (run_id, thread_id, run_id)).rowcount)


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
        if not isinstance(value, dict) or not value:
            raise SchemaInvalid("An evaluation specification requires an object")
        if "eval_spec_id" in value and not (_keys(value, {"eval_spec_id", "version"}) and
                                             all(_text(v) for v in value.values())):
            raise SchemaInvalid("A frozen evaluator requires its exact reference and version")
        if "threshold" in value and not (_keys(value, {"threshold"}, {"evaluator"}) and _number(value["threshold"])):
            raise SchemaInvalid("A numeric evaluation requires a finite threshold")
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


def _check_job_ids(values):
    if not isinstance(values, list) or not values or not all(_text(v) for v in values) or len(values) != len(set(values)):
        raise SchemaInvalid("Provide a nonempty list of distinct Job IDs")


def _check_action(action):
    if not isinstance(action, dict):
        raise SchemaInvalid("Expected an operator input object")
    kind = action.get("kind")
    if kind in ("text", "proposal", "supplement"):
        options = ("source_ref",) if kind == "supplement" else ()
        if not (_keys(action, {"kind", "text"}, options) and _text(action["text"])
                and ("source_ref" not in action or _text(action["source_ref"]))):
            raise SchemaInvalid("Expected a user note with text and an optional supplement source")
    elif kind == "confirm":
        if not (_keys(action, {"kind", "change_id", "preview_sha256"}, {"acknowledge_consequences"}) and
                _text(action["change_id"]) and _text(action["preview_sha256"]) and
                ("acknowledge_consequences" not in action or action["acknowledge_consequences"] in (CANCEL_ACK, IMPACT_ACK))):
            raise SchemaInvalid("Confirmation must identify the exact preview")
    elif kind == "cancel_jobs":
        if not _keys(action, {"kind", "job_ids"}):
            raise SchemaInvalid("Cancellation must identify the Jobs to preview")
        _check_job_ids(action["job_ids"])
    elif kind == "change":
        if "cancel_job_ids" in action:
            _check_job_ids(action["cancel_job_ids"])
        if not (_keys(action, {"kind", "items"}, {"cancel_job_ids"}) and isinstance(action["items"], list) and action["items"]):
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
        raise SchemaInvalid("Unknown operator input kind")
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


def pending_previews(conn, run_id):
    rows = conn.execute("SELECT h.message_id,h.result_json FROM p0_intent_changes c JOIN p0_human_inputs h "
                        "ON h.message_id=c.source_message_id WHERE c.run_id=? AND c.status='awaiting_confirmation' "
                        "AND h.consumed_at IS NOT NULL ORDER BY c.rowid", (run_id,))
    return [{"message_id": row["message_id"], "result": json.loads(row["result_json"])} for row in rows]


def stage_input(conn, run_id, message_id, action):
    """Journal an action received from the trusted operator before sending it to Codex."""
    if not _text(message_id):
        raise SchemaInvalid("The client input requires a stable message ID")
    encoded = _check_action(action)
    with store.transaction(conn):
        if not conn.execute("SELECT 1 FROM p0_intent_main WHERE run_id=? AND thread_id<>''", (run_id,)).fetchone():
            raise PermissionDenied("The run has no registered main thread")
        if action["kind"] == "text":
            queued = conn.execute("SELECT message_text FROM p0_operator_messages WHERE run_id=? AND message_id=?",
                                  (run_id, message_id)).fetchone()
            if queued and queued["message_text"] != action["text"]:
                raise PermissionDenied("The text interpretation must use the exact submitted human message")
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


def _cap_balances(conn, run_id, diffs):
    from .intent_budget import limits_report
    return limits_report(conn, run_id, [(d["item_id"], d["new_value"]) for d in diffs
                                        if d["item_type"] == "resource_limit"])


def _impact(conn, run_id, change_id, diffs, *, commit, cancel_jobs):
    from .impact import apply_impact
    from .intent_budget import cancellation_preview, enqueue_cancellations
    from .workspace import intent_targets
    report = []
    for target, edits in intent_targets(conn, run_id, diffs):
        effects = apply_impact(conn, target, change_id, edits, commit=commit)
        from ..orchestration import run_mode
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (target,)).fetchone()
        effects.extend(run_mode.for_run(run).intent_impact(conn, target, edits, commit=commit))
        if target != run_id:
            for effect in effects:
                effect["run_id"] = target
        report.extend(effects)
    for row in report:
        if row["action"] == "interrupt":
            row["consequence"] = "The current attempt will be interrupted."
        elif row["object_type"] == "job" and row["action"] == "historical":
            row["consequence"] = "This Job's result will be retained as historical evidence."
    cancelled = cancellation_preview(conn, run_id, cancel_jobs)
    if commit:
        enqueue_cancellations(conn, run_id, change_id, cancelled)
    return report + cancelled


def _required_ack(impact):
    if any(row["action"] == "request_cancel" for row in impact):
        return CANCEL_ACK
    if any("consequence" in row for row in impact):
        return IMPACT_ACK
    return None


def _dispositions(impact):
    # Earlier previews already recorded the disposition, subject, status and exact
    # lease. The added user-facing description does not change that consequence.
    return [{k: v for k, v in row.items() if k != "consequence"} for row in impact
            if row["action"] in ("interrupt", "request_cancel", "withdraw") or
            (row["object_type"] == "job" and row["action"] == "historical")]


def _commit(conn, run_id, change, message_id, diffs, cancel_jobs):
    from .workspace import intent_targets
    now = clock.now_iso()
    for target, edits in intent_targets(conn, run_id, diffs):
        for diff in edits:
            current = conn.execute("SELECT COALESCE(MAX(version),0) FROM p0_intent_versions "
                                   "WHERE run_id=? AND item_id=?", (target, diff["item_id"])).fetchone()[0]
            if current != diff["old_version"]:
                raise StaleRevision(f"Preview for {diff['item_id']} expected {diff['old_version']}, now {current}")
            new = diff["new_value"]
            conn.execute("INSERT INTO p0_intent_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (target, diff["item_id"], diff["new_version"], diff["item_type"], ids.canonical_json(new),
                          ids.sha256_of_obj(new), "removed" if new is None else "active", change["change_id"],
                          message_id, now))
    impact = _impact(conn, run_id, change["change_id"], diffs, commit=True, cancel_jobs=cancel_jobs)
    conn.execute("UPDATE p0_intent_changes SET status='committed',confirmation_message_id=?,"
                 "impact_json=?,committed_at=? WHERE change_id=?",
                 (message_id if message_id != change["source_message_id"] else None,
                  ids.canonical_json(impact), now, change["change_id"]))
    store.append_event(conn, run_id=run_id, actor="operator", kind="intent.committed", subject_kind="change",
                       subject_id=change["change_id"], evidence_level="EXECUTION_OBSERVED",
                       payload={"message_id": message_id, "preview_sha256": change["preview_sha256"]})
    return {"status": "committed", "change_id": change["change_id"], "preview_sha256": change["preview_sha256"],
            "preview": diffs, "impact": impact, "resource_limits": _cap_balances(conn, run_id, diffs)}


def _goal_version(conn, run_id):
    row = conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row else None


def _change(conn, run_id, message_id, action, *, force_preview=False):
    from ..orchestration.service import hooks_for_run
    edits = action.get("items", [])
    if rejected := _unsupported_resource(edits) or hooks_for_run(conn, run_id).validate_intent(conn, run_id, edits):
        return rejected
    diffs = _changes(conn, run_id, edits)
    cancel_jobs = action.get("cancel_job_ids", action.get("job_ids", []))
    if not diffs and not cancel_jobs:
        return {"status": "unchanged", "preview": [], "impact": []}
    change_id = ids.new_id("request")
    existing_objective = bool(active_items(conn, run_id, "objective"))
    confirm = force_preview or bool(cancel_jobs) or any(_requires_confirmation(d) or
              (existing_objective and d["item_type"] == "objective") for d in diffs)
    # An ordinary cap edit permits existing work to finish. In a composite edit another
    # item may invalidate an attempt or its result, which must appear in the preview.
    check_composite = len(diffs) > 1 and any(d["item_type"] == "resource_limit" for d in diffs)
    impact = (_impact(conn, run_id, change_id, diffs, commit=False, cancel_jobs=cancel_jobs)
              if confirm or check_composite else [])
    consequences = [row for row in impact if "consequence" in row]
    exact = {"run_id": run_id, "message_id": message_id, "changes": diffs}
    goal_version = _goal_version(conn, run_id) if confirm or consequences else None
    if goal_version is not None:
        exact["goal_version"] = goal_version
    if consequences:
        exact["consequences"] = consequences
    digest = ids.sha256_of_obj(exact)
    conn.execute("INSERT INTO p0_intent_changes(change_id,run_id,source_message_id,status,"
                 "preview_json,preview_sha256,impact_json,created_at) VALUES(?,?,?,'awaiting_confirmation',?,?,?,?)",
                 (change_id, run_id, message_id, ids.canonical_json(diffs), digest, ids.canonical_json(impact),
                  clock.now_iso()))
    change = {"change_id": change_id, "source_message_id": message_id, "preview_sha256": digest}
    if confirm or consequences:
        result = {"status": "awaiting_confirmation", "change_id": change_id, "preview_sha256": digest,
                  "preview": diffs, "impact": impact, "resource_limits": _cap_balances(conn, run_id, diffs)}
        if goal_version is not None:
            result["goal_version"] = goal_version
        if ack := _required_ack(impact):
            result["required_confirmation"] = {"acknowledge_consequences": ack,
                "text": ("确认取消预览中列出的作业" if ack == CANCEL_ACK else "确认执行并接受预览中列出的后果"),
                "english": ("I confirm cancellation of the jobs listed in the preview" if ack == CANCEL_ACK
                            else "I accept the listed consequences and confirm this change")}
        return result
    return _commit(conn, run_id, change, message_id, diffs, cancel_jobs)


def _confirm(conn, run_id, turn_id, message_id, action):
    from .intent_text import was_displayed
    change = _preview_for_confirmation(conn, run_id, action)
    origin = conn.execute("SELECT receipt_turn_id,result_json FROM p0_human_inputs WHERE message_id=?",
                          (change["source_message_id"],)).fetchone()
    if change["source_message_id"] == message_id or (turn_id and origin["receipt_turn_id"] == turn_id):
        raise PermissionDenied("This change needs confirmation in a later human main-thread turn")
    if turn_id is None and not was_displayed(conn, change["change_id"], message_id):
        raise PermissionDenied("Display this exact preview before confirming it in a later local action.")
    if change["status"] == "committed":
        raise PermissionDenied("This change has already been committed")
    # Older previews still carry the exact per-item versions checked by _commit.
    preview_version = json.loads(origin["result_json"] or "{}").get("goal_version")
    if preview_version is not None and preview_version != _goal_version(conn, run_id):
        raise StaleRevision("The scientific objective changed. Request and inspect a fresh preview.")
    diffs = json.loads(change["preview_json"])
    original = json.loads(change["impact_json"])
    cancelled = [r["object_id"] for r in original if r["action"] == "request_cancel"]
    current = _impact(conn, run_id, change["change_id"], diffs, commit=False, cancel_jobs=cancelled)
    if _dispositions(original) != _dispositions(current):
        raise StaleRevision("The consequences changed. Request and inspect a fresh preview.")
    ack = _required_ack(current)
    if ack:
        human_kind = conn.execute("SELECT json_extract(action_json,'$.kind') FROM p0_human_inputs WHERE message_id=?",
                                  (message_id,)).fetchone()[0]
        # An exact structured confirmation of an ordinary intent replacement names
        # its bound digest. Budget side effects, explicit cancellations and deictic
        # natural-language confirmations require the additional displayed acknowledgment.
        designated = turn_id is None or human_kind == "text" or ack == CANCEL_ACK or any(d["item_type"] == "resource_limit" for d in diffs)
        if designated and (action.get("acknowledge_consequences") != ack or not was_displayed(conn, change["change_id"], message_id)):
            raise PermissionDenied("Display the exact preview, then explicitly confirm its listed consequences "
                                   "in a later human message.")
    return _commit(conn, run_id, change, message_id, diffs, cancelled)


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
    from ..orchestration.service import hooks_for_run
    receipt = hooks_for_run(conn, run_id).register_supplement(conn, run_id, source)
    return {"status": "supplement_registered", "source_id": source, **receipt}


def _receipt(conn, run_id, thread_id, turn_id, message_id):
    if not _text(thread_id) or not _text(turn_id):
        raise PermissionDenied("No official human turn receipt")
    row = conn.execute("""
        SELECT i.* FROM p0_human_inputs i JOIN p0_intent_main m ON i.run_id=m.run_id
        WHERE i.run_id=? AND m.thread_id=? AND i.message_id=?""", (run_id, thread_id, message_id)).fetchone()
    if row is None or (row["received_at"] and row["receipt_turn_id"] != turn_id):
        raise PermissionDenied("No matching main-thread operator input for this receipt")
    return row


def _apply_action(conn, run_id, turn_id, message_id, action):
    if action["kind"] in ("change", "cancel_jobs"):
        return _change(conn, run_id, message_id, action)
    if action["kind"] == "confirm":
        return _confirm(conn, run_id, turn_id, message_id, action)
    return _note(conn, run_id, message_id, action)


def _apply_local(conn, run_id, message_id, action, *, force_preview=False):
    """Write a trusted local operator input to the same versioned Intent journal."""
    from .intent_text import prepare
    encoded = _check_action(action)
    with store.transaction(conn):
        now = clock.now_iso()
        # The legacy FK uses this run anchor. An empty thread means no native thread has been bound.
        conn.execute("INSERT OR IGNORE INTO p0_intent_main(run_id,thread_id) VALUES(?,'')", (run_id,))
        conn.execute("INSERT OR IGNORE INTO p0_human_inputs(message_id,run_id,action_json,staged_at) "
                     "VALUES(?,?,?,?)", (message_id, run_id, encoded, now))
        row = conn.execute("SELECT * FROM p0_human_inputs WHERE message_id=?", (message_id,)).fetchone()
        if row["run_id"] != run_id or row["action_json"] != encoded or row["receipt_turn_id"]:
            raise IdempotencyConflict("A different input or native receipt owns this message")
        if row["consumed_at"]:
            return json.loads(row["result_json"])
        if action["kind"] == "text":
            action = prepare(conn, run_id, message_id, action["text"])
        if "status" in action:
            result = action
        elif force_preview:
            result = _change(conn, run_id, message_id, action, force_preview=True)
        else:
            result = _apply_action(conn, run_id, None, message_id, action)
        conn.execute("UPDATE p0_human_inputs SET received_at=?,consumed_at=?,result_json=? WHERE message_id=?",
                     (now, now, ids.canonical_json(result), message_id))
        return result


def accept_input(conn, run_id, thread_id, turn_id, message_id):
    """Journal the official receipt. Text waits for its main-thread proposal or terminal fallback."""
    from .intent_text import prepare
    with store.transaction(conn):
        row = _receipt(conn, run_id, thread_id, turn_id, message_id)
        if row["result_json"]:
            return json.loads(row["result_json"])
        conn.execute("UPDATE p0_human_inputs SET receipt_turn_id=?,received_at=?"
                     " WHERE message_id=? AND receipt_turn_id IS NULL", (turn_id, clock.now_iso(), message_id))
        action = json.loads(row["action_json"])
        if action["kind"] == "text":
            plan = prepare(conn, run_id, message_id, action["text"])
            result = {"status": "input_received", "interpretation": plan}
            if plan.get("kind") == "supplement":
                # A later decision writer must see evidence the operator already supplied,
                # including while the main model is still discussing this same input.
                result["registered_supplement"] = _note(conn, run_id, message_id, plan)
            conn.execute("UPDATE p0_human_inputs SET result_json=? WHERE message_id=?",
                         (ids.canonical_json(result), message_id))
            return result
    with store.transaction(conn):
        row = _receipt(conn, run_id, thread_id, turn_id, message_id)
        if row["consumed_at"]:
            return json.loads(row["result_json"])
        result = _apply_action(conn, run_id, turn_id, message_id, action)
        conn.execute("UPDATE p0_human_inputs SET consumed_at=?,result_json=? WHERE message_id=?",
                     (clock.now_iso(), ids.canonical_json(result), message_id))
        return result


def _check_candidate(candidate):
    # Unimplemented edit types are permitted in a proposal. Only edits which match
    # the complete, independently validated human action can reach the writer.
    if isinstance(candidate, dict) and candidate.get("kind") == "change":
        if not (_keys(candidate, {"kind", "items"}, {"cancel_job_ids"}) and
                isinstance(candidate["items"], list) and candidate["items"]):
            raise SchemaInvalid("Supply a candidate edit list. The call envelope identifies the human turn.")
        try:
            json.dumps(candidate, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SchemaInvalid("A proposal must contain finite JSON values") from exc
        return
    _check_action(candidate)
    if candidate["kind"] == "text":
        raise SchemaInvalid("A model proposal cannot create an operator input")


def process_text(conn, run_id, thread_id, turn_id, *, candidate=None):
    """The bound official call envelope identifies one human input. Tool arguments carry proposals only."""
    from .intent_text import clarify
    with store.transaction(conn):
        rows = conn.execute("SELECT i.* FROM p0_human_inputs i JOIN p0_intent_main m ON m.run_id=i.run_id "
                            "WHERE i.run_id=? AND m.thread_id=? AND i.receipt_turn_id=? LIMIT 2",
                            (run_id, thread_id, turn_id)).fetchall()
        if len(rows) != 1 or json.loads(rows[0]["action_json"])["kind"] != "text" or not rows[0]["received_at"]:
            raise PermissionDenied("This main-thread turn has no unique acknowledged raw operator message")
        row = rows[0]
        receipt = json.loads(row["result_json"])
        if row["consumed_at"]:
            return receipt
        authorized = receipt["interpretation"]
        remainder = receipt.get("unapplied_proposal")
        if candidate is not None:
            _check_candidate(candidate)
            if authorized.get("status") == "conversation":
                # A speculative edit cannot consume the PI turn before Main routes its actual clauses.
                receipt = {**receipt, "unapplied_proposal": candidate}
                encoded = ids.canonical_json(receipt)
                conn.execute("UPDATE p0_human_inputs SET result_json=? WHERE message_id=?", (encoded, row["message_id"]))
                conn.execute("UPDATE p0_operator_messages SET result_json=? WHERE run_id=? AND message_id=?",
                             (encoded, run_id, row["message_id"]))
                return receipt
            if candidate != authorized and candidate.get("kind") != "proposal":
                if candidate["kind"] == authorized.get("kind") == "change":
                    grounded = {ids.canonical_json(edit) for edit in authorized["items"]}
                    extra = [edit for edit in candidate["items"] if ids.canonical_json(edit) not in grounded]
                    extra_jobs = candidate.get("cancel_job_ids")
                    if extra or (extra_jobs and extra_jobs != authorized.get("cancel_job_ids")):
                        remainder = {"kind": "change", "items": extra}
                        if extra_jobs and extra_jobs != authorized.get("cancel_job_ids"):
                            remainder["cancel_job_ids"] = extra_jobs
                else:
                    remainder = candidate
            elif candidate != authorized:
                remainder = candidate
        if "registered_supplement" in receipt:
            result = receipt["registered_supplement"]
        elif "status" in authorized:
            result = authorized
        else:
            try:
                # A stale multi-item preview must roll back every write before returning clarification.
                with store.transaction(conn):
                    _check_action(authorized)
                    result = _apply_action(conn, run_id, turn_id, row["message_id"], authorized)
            except (ArtifactNotFound, PermissionDenied, SchemaInvalid, StaleRevision) as exc:
                result = clarify(exc.message)
        if remainder:
            result = {**result, "unapplied_proposal": remainder}
        encoded = ids.canonical_json(result)
        conn.execute("UPDATE p0_human_inputs SET consumed_at=?,result_json=? WHERE message_id=?",
                     (clock.now_iso(), encoded, row["message_id"]))
        conn.execute("UPDATE p0_operator_messages SET result_json=? WHERE run_id=? AND message_id=?",
                     (encoded, run_id, row["message_id"]))
        return result


def finish_text(conn, run_id, message_id):
    """Process a terminal human turn that ended without using the optional candidate tool."""
    row = conn.execute("SELECT m.thread_id,i.receipt_turn_id FROM p0_human_inputs i JOIN p0_intent_main m "
                       "ON m.run_id=i.run_id WHERE i.run_id=? AND i.message_id=? AND i.consumed_at IS NULL "
                       "AND i.receipt_turn_id IS NOT NULL AND json_extract(i.action_json,'$.kind')='text'",
                       (run_id, message_id)).fetchone()
    if row:
        return process_text(conn, run_id, row["thread_id"], row["receipt_turn_id"])
