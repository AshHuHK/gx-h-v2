"""Task team attribution, carved-out budgets and admission on the workspace's SQLite writer."""
from collections import Counter

from .. import budgets, config, contracts
from ..domain import clock, ids, machines
from ..errors import ArtifactNotFound, HoldActive, IdempotencyConflict, SchemaInvalid
from ..persistence import store


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise SchemaInvalid(f"{label} must be a positive integer")
    return value


def configure(conn, run_id, spec):
    """Install the operator's team allocations after the run has received its funding."""
    if "max_parallel_roots" in spec:
        _positive(spec["max_parallel_roots"], "run max_parallel_roots")
    definitions = spec.get("teams", [])
    if not isinstance(definitions, list):
        raise SchemaInvalid("teams must be a list of named budget allocations")
    seen, assigned = set(), set()
    with store.transaction(conn):
        for entry in definitions:
            if not isinstance(entry, dict):
                raise SchemaInvalid("each team must specify an identifier, allocation and root limit")
            key, alloc = entry.get("team_id"), entry.get("limits")
            roles = entry.get("roles", [])
            name = entry.get("name", key)
            maximum = _positive(entry.get("max_parallel_roots"), "team max_parallel_roots")
            if (not isinstance(key, str) or not key.strip() or key in seen or not isinstance(name,str)
                    or not name.strip() or not isinstance(alloc, dict) or not alloc or not isinstance(roles,list)
                    or not all(isinstance(r,str) and contracts.is_label("worker_role", r) for r in roles)
                    or len(set(roles)) != len(roles) or assigned.intersection(roles)):
                raise SchemaInvalid("teams need distinct identifiers, unique role mappings and a resource allocation")
            seen.add(key)
            assigned.update(roles)
            previous = conn.execute("SELECT t.*,b.limits_json FROM p0_teams t JOIN budgets b USING(budget_ref) "
                                    "WHERE t.run_id=? AND t.team_id=?", (run_id,key)).fetchone()
            if previous:
                if (name,maximum,sorted(roles),alloc) != (previous["name"],previous["max_parallel_roots"],
                        store.loads(previous,"roles_json"),store.loads(previous,"limits_json")):
                    raise IdempotencyConflict("this team's original allocation or role mapping differs")
                continue
            overlap = conn.execute("SELECT value FROM p0_teams t,json_each(t.roles_json) WHERE t.run_id=? "
                                   "AND value IN (SELECT value FROM json_each(?)) LIMIT 1",
                                   (run_id,ids.canonical_json(roles))).fetchone()
            if overlap:
                raise SchemaInvalid(f"role {overlap[0]} already has a team in this run")
            parent = budgets.budget_for(conn,run_id=run_id)
            child = budgets.open_child(conn,parent_ref=parent,subject_kind="team",subject_id=key,
                                      limits=alloc,actor="gx:operator")
            now = clock.now_iso()
            conn.execute("INSERT INTO p0_teams VALUES(?,?,?,'active',?,?,?,?,?)", (run_id,key,name,child["budget_ref"],
                         maximum,ids.canonical_json(sorted(roles)),now,now))


def release_finished(conn, *, actor="gx-p0-team"):
    """Return unused Team allocations after the run ends and the last original reservation settles."""
    terminal = sorted(machines.terminal_states("run"))
    marks = ",".join("?" for _ in terminal)
    with store.transaction(conn):
        rows = conn.execute(
            "SELECT f.reservation_ref FROM p0_teams t JOIN runs r USING(run_id) "
            "JOIN budget_reservations f ON f.reservation_ref=? || t.budget_ref "
            f"WHERE r.status IN ({marks}) AND f.status=? AND NOT EXISTS ("
            "SELECT 1 FROM budget_reservations held WHERE held.budget_ref=t.budget_ref "
            "AND held.status=?) ORDER BY t.rowid",
            (budgets.FUND_PREFIX, *terminal, budgets.HELD, budgets.HELD)).fetchall()
        for row in rows:
            budgets.release(conn, row["reservation_ref"], actor=actor, reason="Run finished and team work settled")


def selected_team(conn, run_id, role, explicit=None):
    if explicit is not None:
        row = conn.execute("SELECT * FROM p0_teams WHERE run_id=? AND team_id=?", (run_id,explicit)).fetchone()
        if not row:
            raise ArtifactNotFound("the named team does not belong to this run")
        return row
    return conn.execute("SELECT t.* FROM p0_teams t WHERE t.run_id=? AND EXISTS (SELECT 1 FROM "
                        "json_each(t.roles_json) WHERE value=?)", (run_id,role)).fetchone()


def team_funding(conn, run_id, task_id, fallback_budget_ref, *, admit=False):
    if task_id is None:
        return fallback_budget_ref
    row = conn.execute("SELECT t.budget_ref,t.status,t.team_id FROM p0_task_teams a JOIN p0_teams t USING(run_id,team_id) "
                       "WHERE a.task_id=? AND a.run_id=?", (task_id,run_id)).fetchone()
    if admit and row and row["status"] == "paused":
        raise HoldActive("this task's team is paused", details={"reason":"team_paused","team_id":row["team_id"]})
    return row["budget_ref"] if row else fallback_budget_ref


def attach(conn, task_id, team, explicit):
    if team:
        conn.execute("INSERT INTO p0_task_teams VALUES(?,?,?,?,?)", (task_id,team["run_id"],team["team_id"],
                     clock.now_iso(),"explicit" if explicit else "role"))


def team_status(conn, run_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_teams'").fetchone():
        return []
    result = []
    occupied = _occupancy(conn, None)
    for r in conn.execute("SELECT * FROM p0_teams WHERE run_id=? ORDER BY team_id",(run_id,)):
        row = dict(r)
        row["roles"] = store.loads(r,"roles_json")
        del row["roles_json"]
        row["budget"] = budgets.state(conn,r["budget_ref"])
        row["task_ids"] = [q[0] for q in conn.execute("SELECT task_id FROM p0_task_teams WHERE run_id=? "
                                               "AND team_id=? ORDER BY task_id",(run_id,r["team_id"]))]
        row["active_roots"] = sum(item["run_id"] == run_id and item["team_id"] == r["team_id"] for item in occupied)
        result.append(row)
    return result


def defer_unavailable_slot(conn, root, *, ending_reason, ending_key):
    """When an external control or meter completes, queue an idle attempt behind current occupants."""
    if root["wait_job_id"] or root["run_status"] == "PAUSED":
        return
    if ending_reason == "metering":
        measured = conn.execute("SELECT 1 FROM budget_reservations WHERE reservation_ref=? AND status=?",
                                (ending_key, budgets.SETTLED)).fetchone()
        if not measured:
            return  # Per-delivery metering waits are already inside their turn admission.
    elif ending_reason != "operator_pause":
        return
    if conn.execute("SELECT 1 FROM p0_attempt_waits WHERE root_key=? AND ended_at IS NULL "
                    "AND (reason<>? OR wait_key<>?) LIMIT 1",
                    (root["root_key"], ending_reason, ending_key)).fetchone():
        return
    try:
        require_capacity(conn, dict(root))
    except HoldActive:
        from . import waits
        waits.begin(conn, root["root_key"], "team_capacity", "slot:" + root["root_key"])


def resume_if_admitted(conn, root):
    """Keep the original attempt through a control or meter handoff until it can reclaim capacity."""
    waits_to_finish = conn.execute(
        "SELECT w.reason,w.wait_key,t.status,t.team_id FROM p0_attempt_waits w "
        "LEFT JOIN p0_task_teams a ON a.task_id=? LEFT JOIN p0_teams t USING(run_id,team_id) "
        "WHERE w.root_key=? AND w.ended_at IS NULL AND ((w.reason='team_paused' AND w.wait_key=t.team_id) "
        "OR (w.reason='team_capacity' AND w.wait_key=?)) ORDER BY w.wait_id",
        (root["task_id"], root["root_key"], "slot:" + root["root_key"])).fetchall()
    if not waits_to_finish:
        return
    if waits_to_finish[0]["status"] == "paused":
        raise HoldActive("this task's team is paused",
                         details={"reason": "team_paused", "team_id": waits_to_finish[0]["team_id"]})
    if not root["wait_job_id"]:
        require_capacity(conn, dict(root))
    from . import waits
    for wait in waits_to_finish:
        waits.finish(conn, root["root_key"], wait["reason"], wait["wait_key"])


def team_control(conn, run_id, team_id, action):
    if action not in ("status","pause","resume"):
        raise SchemaInvalid("team action must be status, pause or resume")
    if action == "status":
        current = next((t for t in team_status(conn, run_id) if t["team_id"] == team_id), None)
        if current is None:
            raise ArtifactNotFound("this run has no such team")
        return current
    from . import waits
    with store.transaction(conn):
        team = conn.execute("SELECT status FROM p0_teams WHERE run_id=? AND team_id=?", (run_id,team_id)).fetchone()
        if not team:
            raise ArtifactNotFound("this run has no such team")
        target = "paused" if action=="pause" else "active"
        if team[0] != target:
            conn.execute("UPDATE p0_teams SET status=?,updated_at=? WHERE run_id=? AND team_id=?",
                         (target,clock.now_iso(),run_id,team_id))
            for root in conn.execute("SELECT r.* FROM p0_roots r JOIN tasks t ON r.lease_id=t.lease_id "
                                     "JOIN p0_task_teams a ON a.task_id=t.task_id WHERE a.run_id=? AND a.team_id=?",
                                     (run_id,team_id)).fetchall():
                if target == "paused":
                    waits.begin(conn, root["root_key"], "team_paused", team_id)
                else:
                    try:
                        resume_if_admitted(conn, root)
                    except HoldActive:
                        # A sibling took the slot. Dispatch will finish the same wait when it is available.
                        continue
            store.append_event(conn,run_id=run_id,actor="operator",kind="p0.team."+action,subject_kind="team",
                               subject_id=team_id,evidence_level="EXECUTION_OBSERVED",payload={"status":target})
        return next(t for t in team_status(conn,run_id) if t["team_id"]==team_id)


def _occupancy(conn, excluded):
    # A claimed worker owns a slot before its thread exists. A paid turn may
    # still run after submission releases its lease. Count their union once.
    return conn.execute("""
        SELECT r.run_id, a.team_id FROM p0_roots r JOIN runs p ON p.run_id=r.run_id
        LEFT JOIN tasks t ON t.task_id=r.task_id AND r.lease_id=t.lease_id
        LEFT JOIN leases l ON l.lease_id=t.lease_id
        LEFT JOIN p0_task_teams a ON a.task_id=r.task_id
        LEFT JOIN p0_teams g ON g.run_id=a.run_id AND g.team_id=a.team_id
        WHERE r.root_key!=? AND (
            (r.wait_job_id IS NULL AND t.status IN ('CLAIMED','RUNNING','SUBMITTED','VALIDATING')
             AND l.released_at IS NULL AND l.expires_at>? AND p.status!='PAUSED'
             AND COALESCE(g.status,'active')='active'
             AND NOT EXISTS (SELECT 1 FROM p0_attempt_waits w WHERE w.root_key=r.root_key AND w.ended_at IS NULL))
            OR EXISTS (SELECT 1 FROM p0_root_deliveries d LEFT JOIN p0_root_turn_usage u
                       ON u.thread_id=d.thread_id AND u.turn_id=d.turn_id
                       WHERE d.thread_id=r.thread_id AND d.state IN ('sending','acknowledged')
                       AND ((r.wait_job_id IS NULL AND d.method='turn/start' AND u.terminal_status IS NULL)
                           OR (r.wait_job_id IS NOT NULL AND d.method IN ('turn/start','turn/steer')
                               AND d.event_id=('wake:'||r.lease_id||':'||r.wait_job_id))))
        )""",(excluded or "",clock.now_iso())).fetchall()


def _capacity(conn, binding):
    if not conn.in_transaction:
        raise RuntimeError("Root admission belongs in the lease or turn writer transaction")
    run_id = binding["run_id"]
    run = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    inputs = store.loads(run,"inputs_json",{}).get("inputs",{})
    from . import workspace
    limit = inputs.get("p0_spec", {}).get("max_parallel_roots", config.limits()["run_budget"]["max_parallel_workers"])
    workspace_limit = workspace.max_parallel_roots(conn)
    team = conn.execute("SELECT g.* FROM p0_task_teams a JOIN p0_teams g USING(run_id,team_id) WHERE "
                        "a.run_id=? AND a.task_id=?", (run_id,binding.get("task_id"))).fetchone()
    if team and team["status"]=="paused":
        raise HoldActive("this task's team is paused",details={"reason":"team_paused","team_id":team["team_id"]})
    occupied = _occupancy(conn,binding.get("root_key"))
    run_count = sum(r["run_id"]==run_id for r in occupied)
    gates = [("workspace",len(occupied),workspace_limit),("run",run_count,limit)]
    if team:
        n = sum(r["run_id"]==run_id and r["team_id"]==team["team_id"] for r in occupied)
        gates.append(("team",n,team["max_parallel_roots"]))
    for scope,used,bound in gates:
        if used >= bound:
            raise HoldActive(f"{scope} has {used} active roots at its limit of {bound}",details={
                "reason":"team_capacity","scope":scope,"limit":bound,"occupied":used,"team_id":team["team_id"] if team else None})
    return (limit, run_count), occupied


def require_capacity(conn, binding):
    """Gate one Root's admission in the writer that takes its slot."""
    bounds, _ = _capacity(conn, binding)
    return bounds


def claim_capacity(conn, run_id, task_id):
    """An overdue task can keep a slot only if its team could actually claim it."""
    bounds, occupied = _capacity(conn, {"run_id":run_id, "task_id":task_id})
    used = Counter(row["team_id"] for row in occupied if row["run_id"] == run_id)
    blocked = [team["team_id"] for team in conn.execute(
        "SELECT team_id,status,max_parallel_roots FROM p0_teams WHERE run_id=?", (run_id,))
        if team["status"] == "paused" or used[team["team_id"]] >= team["max_parallel_roots"]]
    unavailable = frozenset(row[0] for row in conn.execute(
        "SELECT a.task_id FROM p0_task_teams a JOIN tasks t USING(task_id) WHERE a.run_id=? AND t.status='READY' "
        "AND a.team_id IN (SELECT value FROM json_each(?))", (run_id, ids.canonical_json(blocked)))) if blocked else frozenset()
    return bounds, unavailable
