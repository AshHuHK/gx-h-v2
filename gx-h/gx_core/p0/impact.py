"""Calculate a change against the objects visible in its SQLite writer transaction."""
from collections import defaultdict

from ..domain import ids, machines
from ..errors import ArtifactNotFound
from ..jobs import leases as job_leases
from ..persistence import store
from . import intent, science_inputs

_ADVISORY = {"soft_preference", "optimization_target", "priority", "resource_limit"}
_ACTIVE_ATTEMPT = {"CLAIMED", "RUNNING", "SUBMITTED", "VALIDATING"}


def _forbidden(value):
    return set((value or {}).get("forbidden_capabilities", []))


def _blocked(capability, bans):
    return capability in bans or capability.partition("@")[0] in bans


def _changed_methods(conn, run_id, change):
    """An added ban also applies to Jobs admitted before that item existed."""
    before = _forbidden(change["old_value"])
    after = _forbidden(change["new_value"])
    candidates = sorted(before ^ after)
    if not candidates:
        return
    rows = conn.execute(
        "SELECT * FROM jobs WHERE run_id=? AND (capability_id IN (SELECT value FROM json_each(?)) "
        "OR capability_id || '@' || capability_version IN (SELECT value FROM json_each(?)))",
        (run_id, store._j(candidates), store._j(candidates)),
    )
    for job in rows:
        method = f"{job['capability_id']}@{job['capability_version']}"
        if _blocked(method, before) != _blocked(method, after):
            yield job, _blocked(method, after)


def _dependencies(conn, run_id, changes):
    targets = defaultdict(dict)
    for item_id, change in changes.items():
        for row in conn.execute(
            "SELECT object_type, object_id, role FROM p0_intent_dependencies "
            "WHERE run_id=? AND item_id=?", (run_id, item_id),
        ):
            targets[row["object_type"], row["object_id"]][item_id] = row["role"]
        if change["item_type"] in science_inputs.ADVISORY and change["new_value"] is not None:
            # A newly added item cannot have a stored link. Apply the same role rule
            # as task creation for already commissioned, still-open assignments.
            for task in conn.execute("SELECT task_id,input_json FROM tasks WHERE run_id=? "
                                     "AND status NOT IN ('ACCEPTED','CANCELLED','EXHAUSTED')", (run_id,)):
                role = science_inputs.role_of(store.loads(task, "input_json", {}))
                if science_inputs.consumes(role, change["item_type"], change["new_value"]):
                    targets["task", task["task_id"]].setdefault(item_id, "context")
        if change["item_type"] not in intent.BAN_TYPES:
            continue
        # A newly introduced prohibition has no pre-existing item link. A task
        # whose structured method already selects it now has an execution dependency.
        before, after = _forbidden(change["old_value"]), _forbidden(change["new_value"])
        if after - before:
            for task in conn.execute(
                "SELECT task_id,input_json FROM tasks WHERE run_id=? "
                "AND status NOT IN ('ACCEPTED','CANCELLED') AND ("
                "json_type(input_json,'$.capability_id') IS NOT NULL OR "
                "json_type(input_json,'$.capability_version') IS NOT NULL OR "
                "json_type(input_json,'$.tool_and_skill_versions')='array' OR "
                "json_type(input_json,'$.method_sequence')='array')", (run_id,),
            ):
                if any(_blocked(m, after) and not _blocked(m, before) for m in _task_methods(task)):
                    targets["task", task["task_id"]][item_id] = "execution"
        for job, prohibited in _changed_methods(conn, run_id, change):
            targets["job", job["job_id"]][item_id] = "execution"
            if prohibited:
                # Only the actual Job holder is using this result. A former lease can
                # belong to a different attempt of the same durable Task.
                lease_id = job_leases.job_lease(conn, job)["lease_ref"]
                holder = conn.execute(
                    "SELECT task_id FROM tasks WHERE run_id=? AND lease_id=?",
                    (run_id, lease_id),
                ).fetchone()
                if holder:
                    targets["task", holder["task_id"]][item_id] = "executing_banned_job"
    return targets


def _task_methods(task):
    params = store.loads(task, "input_json", {})
    if not isinstance(params, dict):
        return []
    cap_id, version = params.get("capability_id"), params.get("capability_version")
    methods = [cap_id, version]
    if isinstance(cap_id, str) and isinstance(version, str) and "@" not in version:
        methods.append(f"{cap_id}@{version}")
    versions = params.get("tool_and_skill_versions")
    if isinstance(versions, list):
        methods.extend(versions)
    steps = params.get("method_sequence")
    if isinstance(steps, list):
        methods.extend(step.get("capability_version") for step in steps if isinstance(step, dict))
    return [method for method in methods if isinstance(method, str)]


def _incompatible_task(task, change, role):
    kind = change["item_type"]
    if role == "executing_banned_job":
        return True
    if role == "context" or kind in _ADVISORY:
        return False
    if kind in intent.BAN_TYPES:
        before, after = _forbidden(change["old_value"]), _forbidden(change["new_value"])
        return any(_blocked(method, after) and not _blocked(method, before) for method in _task_methods(task))
    return kind != "evaluation_spec" or role == "evaluation"


def _projected_items(conn, run_id, diffs):
    current = intent.active_items(conn, run_id)
    for change in diffs:
        key = change["item_id"]
        if change["new_value"] is None:
            current.pop(key, None)
        else:
            current[key] = {"item_id": key, "version": change["new_version"],
                            "item_type": change["item_type"], "value": change["new_value"]}
    return current


def _release_attempt(conn, task, change_id, items, *, reason="the attempt's Intent dependency changed"):
    status = task["status"]
    if task["lease_id"]:
        store.release_lease(conn, task["lease_id"])
    fields = {"owner": None, "lease_id": None, "brief_ref": None, "brief_sha256": None}
    # SUBMITTED normally reaches VALIDATING in its original transaction. Retain
    # that edge when recovering an older submitted task before returning it to READY.
    steps = []
    if status == "SUBMITTED":
        steps.append("VALIDATING")
    if status in {"RUNNING", "SUBMITTED", "VALIDATING"}:
        steps.append("RETRYABLE")
    for state in [*steps, "READY"]:
        store.transition(
            conn, machine="task", subject_id=task["task_id"], to_state=state,
            actor="backend", bump=False, reason=reason,
            evidence_level="EXECUTION_OBSERVED", payload={"change_id": change_id, "item_ids": items,
                                                         "attempt": task["attempt"]},
            extra_updates=fields if state == "READY" else None,
        )


def _task_action(conn, task, changes, roles, commit, change_id, result):
    incompatible = any(_incompatible_task(task, changes[key], role) for key, role in roles.items())
    if task["status"] in machines.terminal_states("task"):
        return "historical" if incompatible else "keep"
    if task["status"] not in _ACTIVE_ATTEMPT:
        return "refresh"
    action = "interrupt" if incompatible else "notify"
    lease_id = task["lease_id"]
    result.update(attempt=task["attempt"], lease_id=lease_id)
    if commit:
        if incompatible:
            _release_attempt(conn, task, change_id, sorted(roles))
        if lease_id:
            topic = "p0.intent.notification"
            result["outbox_id"] = store.enqueue_outbox(
                conn, topic=topic, idempotency_key=f"{topic}:{change_id}:{lease_id}",
                payload={"run_id": task["run_id"], "task_id": task["task_id"], "lease_id": lease_id,
                         "attempt": task["attempt"], "action": action, "change_id": change_id,
                         "item_ids": sorted(roles), "items": [_notice_item(changes[k]) for k in sorted(roles)],
                         "instruction": science_inputs.INSTRUCTION},
            )
    return action


def _notice_item(change):
    return {"item_id": change["item_id"], "item_type": change["item_type"], "version": change["new_version"],
            "value": change["new_value"], "status": "removed" if change["new_value"] is None else "active"}


def notification_content(conn, payload):
    """Reconstruct legacy notices from their immutable change, independent of delivery time."""
    if "items" in payload or "source" in payload or not payload.get("change_id"):
        return payload
    change = conn.execute("SELECT preview_json FROM p0_intent_changes WHERE run_id=? AND change_id=?",
                          (payload["run_id"], payload["change_id"])).fetchone()
    items = [_notice_item(item) for item in store.loads(change, "preview_json", [])
             if item["item_id"] in payload.get("item_ids", ())]
    return {**payload, "items": items, "instruction": science_inputs.INSTRUCTION} if items else payload


def enqueue_reevaluation(conn, run_id, eval_id, item_id, version, value, change_id):
    """Schedule one successor evaluation of this original measurement under this version."""
    topic = "p0.intent.reevaluate"
    key = f"{topic}:{eval_id}:{item_id}:{version}"
    return store.enqueue_outbox(conn, topic=topic, idempotency_key=key, payload={
        "run_id": run_id, "change_id": change_id, "object_type": "evaluation", "object_id": eval_id,
        "item_id": item_id, "item_version": version, "value": value,
    })


def _evaluation_action(conn, original, changes, roles, commit, change_id, result):
    specs = [changes[key] for key, role in roles.items() if role == "evaluation"
             and changes[key]["item_type"] == "evaluation_spec" and changes[key]["new_value"] is not None]
    if not specs:
        return "historical"
    if commit:
        outbox = [enqueue_reevaluation(conn, original["run_id"], original["eval_id"], spec["item_id"],
                                       spec["new_version"], spec["new_value"], change_id)
                  for spec in specs]
        result["outbox_ids"] = outbox
        result["outbox_id"] = outbox[0]
    return "reevaluate"


def apply_impact(conn, run_id, change_id, diffs, *, commit):
    """Return observed state and disposition, applying effects only on the commit path.

    The caller stores this result with the version changes and private human receipt.
    Task/Workflow links track the current brief. Job/Evaluation links and their
    immutable artifacts retain the inputs under which the measurement was made.
    """
    if not conn.in_transaction:
        raise RuntimeError("Intent impact requires the version writer's transaction")
    changes = {change["item_id"]: change for change in diffs}
    report = []
    projected = None
    for (kind, obj), roles in sorted(_dependencies(conn, run_id, changes).items()):
        table, column = intent.OBJECTS[kind]
        row = conn.execute(f"SELECT * FROM {table} WHERE run_id=? AND {column}=?", (run_id, obj)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"Intent dependency on missing {kind} {obj!r} in run {run_id!r}")
        status = row["verdict"] if kind == "evaluation" else row["status"]
        result = {"object_type": kind, "object_id": obj, "status": status,
                  "items": [{"item_id": key, "role": role, "new_version": changes[key]["new_version"]}
                            for key, role in sorted(roles.items())]}
        if kind == "task":
            action = _task_action(conn, row, changes, roles, commit, change_id, result)
        elif kind == "evaluation":
            action = _evaluation_action(conn, row, changes, roles, commit, change_id, result)
        elif kind == "job":
            if projected is None:
                projected = _projected_items(conn, run_id, diffs)
            action = "keep" if intent.job_current(conn, obj, items=projected) else "historical"
        else:
            action = "historical" if status in machines.terminal_states("run") else "refresh"
        result["action"] = action
        if commit and kind in {"task", "workflow"} and action not in {"keep", "historical"}:
            for key in roles:
                change = changes[key]
                conn.execute(
                    "INSERT INTO p0_intent_dependencies "
                    "(run_id,object_type,object_id,item_id,item_version,item_hash,role) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id,object_type,object_id,item_id) DO UPDATE SET "
                    "item_version=excluded.item_version, item_hash=excluded.item_hash",
                    (run_id, kind, obj, key, change["new_version"], ids.sha256_of_obj(change["new_value"]),
                     "execution" if roles[key] == "executing_banned_job" else roles[key]),
                )
        report.append(result)
    return report
