"""P0 runs in one project database share formal project Intent and one funded budget."""
import json
import math
import uuid

from .. import budgets
from ..domain import ids, machines
from ..errors import IdempotencyConflict, SchemaInvalid
from ..persistence import store


PROJECT_INTENT_TYPES = frozenset({
    "objective", "resource_limit", "method_ban", "hard_constraint",
    "soft_preference", "optimization_target", "priority",
})


def _has_workspace_schema(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_workspace_runs'").fetchone() is not None


def _run_query(conn):
    if _has_workspace_schema(conn):
        select = ("SELECT r.*, w.run_key FROM runs r "
                  "LEFT JOIN p0_workspace_runs w ON w.run_id=r.run_id ")
    else:
        select = "SELECT r.*, NULL AS run_key FROM runs r "
    return select + "WHERE json_extract(r.inputs_json,'$.inputs.p0')=1"


def list_runs(conn):
    """Return real run records in creation order, including unmodified v2 databases."""
    return conn.execute(_run_query(conn) + " ORDER BY r.rowid").fetchall()


def _check_key(key):
    if not isinstance(key, str) or not key.strip() or key != key.strip():
        raise ValueError("A run key must be nonempty text with no leading or trailing whitespace")


def find_run(conn, *, run_key=None, run_id=None):
    if run_key is not None and run_id is not None:
        raise ValueError("Select one run by its run key or its run ID")
    if run_key is not None:
        _check_key(run_key)
        return conn.execute(_run_query(conn) + " AND run_key=?", (run_key,)).fetchone()
    if run_id is not None:
        row = conn.execute(_run_query(conn) + " AND r.run_id=?", (run_id,)).fetchone()
        if row:
            return row
        raise ValueError(f"Unknown run ID in this workspace: {run_id!r}")
    rows = conn.execute(_run_query(conn) + " LIMIT 2").fetchall()
    if len(rows) > 1:
        raise ValueError("This workspace has multiple runs. Select one with --run-id.")
    return rows[0] if rows else None


def key_for_run(conn, run_id):
    return find_run(conn, run_id=run_id)["run_key"]


def start_key(project_ref, run_key=None):
    if run_key is None:
        return "p0-start:" + project_ref
    _check_key(run_key)
    return "p0-workspace-start:" + ids.sha256_of_obj([project_ref, run_key])


def _limits(spec, name):
    if name not in spec:
        return {}
    return budgets.check_amounts(spec[name], what=name)


def _root_cap(spec):
    settings = spec.get("workspace", {})
    if not isinstance(settings, dict):
        raise SchemaInvalid("workspace must be a configuration object")
    cap = settings.get("max_parallel_roots", 10)
    if type(cap) is not int or not 1 <= cap <= 10:
        raise SchemaInvalid("workspace.max_parallel_roots must be an integer from 1 through 10")
    return cap


def max_parallel_roots(conn):
    if _has_workspace_schema(conn):
        group = conn.execute("SELECT max_parallel_roots FROM p0_workspace WHERE singleton=1").fetchone()
        if group:
            return group[0]
    runs = list_runs(conn)
    return _root_cap(store.loads(runs[0], "inputs_json")["inputs"].get("p0_spec", {})) if runs else 10


def budget_summary(conn):
    """Separate work already charged or held from funds earmarked for member runs."""
    if not _has_workspace_schema(conn):
        return None
    # An independently running coordinator can settle a hold between the two reads.
    conn.execute("SAVEPOINT p0_workspace_summary")
    try:
        group = conn.execute("SELECT w.*,b.limits_json,b.spent_json,b.reserved_json FROM p0_workspace w "
                             "JOIN budgets b ON b.budget_ref=w.budget_ref").fetchone()
        if group is None:
            return None
        limits = store.loads(group, "limits_json")
        spent, reserved = budgets._project_usage(conn, group["project_ref"], lines=limits)
        return {"workspace_id": group["workspace_id"], "budget_ref": group["budget_ref"],
                "max_parallel_roots": group["max_parallel_roots"], "limits": limits, "spent": spent,
                "reserved": reserved, "available": budgets._available(limits, spent, reserved),
                "unallocated": budgets._available(limits, store.loads(group, "spent_json"),
                                                 store.loads(group, "reserved_json"))}
    finally:
        conn.execute("RELEASE p0_workspace_summary")


def _inherit_intent(conn, anchor_id, run_id, spec):
    from . import intent
    for edit in spec.get("intent_items", []):
        if edit["item_type"] not in PROJECT_INTENT_TYPES or edit["item_id"] == "objective":
            continue
        previous = conn.execute("SELECT MAX(version) FROM p0_intent_versions WHERE run_id=? AND item_id=?",
                                (anchor_id, edit["item_id"])).fetchone()[0]
        if previous and intent._changes(conn, anchor_id, [{**edit, "expected_version": previous}]):
            raise SchemaInvalid(f"Initial item {edit['item_id']!r} conflicts with current project Intent. "
                                "Submit a project Intent change to update it.")
    types = sorted(PROJECT_INTENT_TYPES - {"objective"} if spec.get("predecessor") else PROJECT_INTENT_TYPES)
    marks = ",".join("?" for _ in types)
    conn.execute(
        "INSERT INTO p0_intent_versions "
        "SELECT ?,item_id,version,item_type,value_json,sha256,status,change_id,source_message_id,created_at "
        f"FROM p0_intent_versions WHERE run_id=? AND item_type IN ({marks})",
        (run_id, anchor_id, *types))


def release_finished(conn, *, actor="gx-p0-workspace"):
    """Return idle allotments of terminal runs. Uncertain or nested work keeps its hold."""
    if not _has_workspace_schema(conn):
        return
    terminal = sorted(machines.terminal_states("run"))
    marks = ",".join("?" for _ in terminal)
    with store.transaction(conn):
        from . import team_admission
        team_admission.release_finished(conn, actor=actor)
        rows = conn.execute(
            "SELECT f.reservation_ref FROM p0_workspace_runs w JOIN runs r ON r.run_id=w.run_id "
            "JOIN budget_reservations f ON f.reservation_ref=? || r.budget_ref "
            f"WHERE r.status IN ({marks}) AND f.status=? AND NOT EXISTS ("
            "SELECT 1 FROM budget_reservations pending WHERE pending.budget_ref=r.budget_ref "
            "AND pending.status=?) ORDER BY r.rowid", (budgets.FUND_PREFIX, *terminal, budgets.HELD, budgets.HELD)
        ).fetchall()
        for row in rows:
            budgets.release(conn, row["reservation_ref"], actor=actor, reason="Run finished and all work settled")


def attach_run(conn, run_id, *, project_ref, run_key, profile_name, spec, actor):
    """Bind the new run inside run_start's creation transaction and return its budget ref."""
    if not conn.in_transaction:
        raise RuntimeError("Workspace admission belongs in the run creation transaction")
    others = [r for r in list_runs(conn) if r["run_id"] != run_id]
    if any(other["project_ref"] != project_ref for other in others):
        raise SchemaInvalid("All runs in this workspace must use the same project")
    if run_key is not None and any(other["run_key"] == run_key for other in others):
        raise IdempotencyConflict("This run key is already bound to another start request")
    if others and run_key is None:
        raise SchemaInvalid("Use an explicit --run-key to add a run to an existing workspace")
    root_limits = _limits(spec, "workspace_budget")
    run_limits = _limits(spec, "run_budget")
    cap = _root_cap(spec)
    if run_key is None and not (root_limits or run_limits):
        # The v2 allocation, run ID and idempotency record retain their original meaning.
        return budgets.create_root(conn, run_id=run_id, profile_name=profile_name, actor=actor)
    if run_key is not None:
        _check_key(run_key)
    group = conn.execute("SELECT * FROM p0_workspace WHERE singleton=1").fetchone()
    if group is None:
        if len(others) > 1:
            raise SchemaInvalid("This database has multiple unbound legacy P0 runs")
        if others:
            legacy = others[0]
            root, anchor_id = legacy["budget_ref"], legacy["run_id"]
            cap = _root_cap(store.loads(legacy, "inputs_json")["inputs"].get("p0_spec", {}))
        else:
            root = budgets.create_root(conn, run_id=None, profile_name=profile_name, actor=actor,
                                       limits={**budgets.profile(profile_name)["limits"], **root_limits})
            anchor_id = run_id
        conn.execute("INSERT INTO p0_workspace(singleton,workspace_id,project_ref,budget_ref,intent_run_id,"
                     "max_parallel_roots) VALUES(1,?,?,?,?,?)", (project_ref, project_ref, root, anchor_id, cap))
        if others:
            conn.execute("INSERT INTO p0_workspace_runs(run_id,workspace_id) VALUES(?,?)", (anchor_id, project_ref))
    else:
        root, anchor_id = group["budget_ref"], group["intent_run_id"]
        cap = group["max_parallel_roots"]
    if "max_parallel_roots" in spec.get("workspace", {}) and cap != _root_cap(spec):
        raise SchemaInvalid("The supplied workspace.max_parallel_roots differs from this project's existing limit")
    release_finished(conn, actor=actor)
    if predecessor := spec.get("predecessor"):
        # A closed study may still have an unsettled turn. Its hold stays intact,
        # but unused allotment can fund a follow-up under the same workspace cap.
        from ..budgets.allotment import set_run_limit
        previous = find_run(conn, run_id=predecessor["parent_run_id"])
        previous_ref = previous["budget_ref"]
        inherited_caps = budgets.state(conn, previous_ref)["limits"]
        funding = conn.execute("SELECT status,amounts_json FROM budget_reservations WHERE reservation_ref=?",
                               (budgets.FUND_PREFIX + previous_ref,)).fetchone()
        if funding and funding["status"] == budgets.HELD:
            parent_state = budgets.state(conn, root)
            previous_state = budgets.state(conn, previous_ref)
            for key, held in store.loads(funding, "amounts_json").items():
                free = previous_state["available"].get(key, 0)
                if free > 0 and parent_state["available"].get(key, 0) <= 0:
                    transfer = math.ceil(free / 2) if isinstance(free, int) else free / 2
                    set_run_limit(conn, previous["run_id"], previous_ref, key=key,
                                  limit=held-transfer, actor=actor)
    parent = budgets.state(conn, root)
    if any(parent["limits"].get(key) != value for key, value in root_limits.items()):
        raise SchemaInvalid("The supplied workspace_budget differs from the existing project budget")
    available = {key: max(0, value) for key, value in parent["available"].items()}
    if run_key is not None and not predecessor:
        # Leave the remainder available for more runs. An explicit line takes precedence.
        available = {key: math.ceil(value / 2) if isinstance(value, int) else value / 2
                     for key, value in available.items()}
    # A run with a tighter profile cannot draw beyond that profile even if the project can.
    profile = budgets.profile(profile_name)["limits"]
    if any(key not in profile or value > profile[key] for key, value in run_limits.items()):
        raise SchemaInvalid("run_budget must fit within this run's budget profile")
    allotment = {key: min(value, profile[key]) for key, value in available.items() if key in profile}
    if predecessor:
        allotment = {k: min(v, inherited_caps[k]) for k, v in allotment.items() if k in inherited_caps}
    child = budgets.open_child(conn, parent_ref=root, subject_kind="run", subject_id=run_id,
                               limits={**allotment, **run_limits}, actor=actor)
    conn.execute("UPDATE budgets SET run_id=? WHERE budget_ref=?", (run_id, child["budget_ref"]))
    conn.execute("INSERT INTO p0_workspace_runs(run_id,run_key,workspace_id) VALUES(?,?,?)",
                 (run_id, run_key, project_ref))
    if run_id != anchor_id:
        _inherit_intent(conn, anchor_id, run_id, spec)
    return child["budget_ref"]


def bootstrap_action(conn, run_id, action):
    """Replay the captured bootstrap, or initialize just the items this run still needs."""
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "gx-p0-bootstrap/" + run_id))
    queued = conn.execute("SELECT action_json FROM p0_operator_messages WHERE message_id=? AND run_id=? "
                          "UNION ALL SELECT action_json FROM p0_human_inputs WHERE message_id=? AND run_id=? LIMIT 1",
                          (message_id, run_id, message_id, run_id)).fetchone()
    if queued:
        return json.loads(queued["action_json"])
    group = conn.execute("SELECT g.intent_run_id FROM p0_workspace_runs w JOIN p0_workspace g "
                         "USING(workspace_id) WHERE w.run_id=?", (run_id,)).fetchone()
    if not group or group["intent_run_id"] == run_id:
        return action
    inherited = {row["item_id"] for row in conn.execute("SELECT DISTINCT item_id FROM p0_intent_versions WHERE "
                 "run_id=? AND item_type IN (" + ",".join("?" for _ in PROJECT_INTENT_TYPES) + ")",
                 (run_id, *PROJECT_INTENT_TYPES))}
    run = find_run(conn, run_id=run_id)
    successor = store.loads(run, "inputs_json")["inputs"]["p0_spec"].get("predecessor")
    if "objective" not in inherited and not successor:
        raise ValueError("The first run's initial project Intent is pending. Resume its start before this run.")
    return {**action, "items": [edit for edit in action["items"]
                                if edit["item_type"] not in PROJECT_INTENT_TYPES or edit["item_id"] not in inherited]}


def intent_targets(conn, run_id, diffs):
    """Apply the origin's whole edit and each other member's project-scoped edits."""
    yield run_id, diffs
    shared = [diff for diff in diffs if diff["item_type"] in PROJECT_INTENT_TYPES]
    if not shared or not _has_workspace_schema(conn):
        return
    if not conn.execute("SELECT 1 FROM p0_workspace_runs WHERE run_id=?", (run_id,)).fetchone():
        return
    origin = find_run(conn, run_id=run_id)
    successor = store.loads(origin, "inputs_json")["inputs"]["p0_spec"].get("predecessor")
    siblings = conn.execute("SELECT other.run_id, r.inputs_json, g.status goal_status FROM p0_workspace_runs member JOIN p0_workspace_runs other "
                            "USING(workspace_id) JOIN runs r ON r.run_id=other.run_id "
                            "LEFT JOIN v6_goals g ON g.run_id=other.run_id "
                            "WHERE member.run_id=? AND other.run_id<>member.run_id "
                            "ORDER BY other.rowid", (run_id,))
    for other in siblings:
        if other["goal_status"] in {"CLOSING", "CLOSED"}:
            continue
        distinct_goal = successor or store.loads(other, "inputs_json")["inputs"].get("p0_spec", {}).get("predecessor")
        edits = [d for d in shared if not distinct_goal or d["item_type"] != "objective"]
        if edits:
            yield other["run_id"], edits
