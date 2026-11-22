"""Compare existing budget lines with reported and reserved native work."""
from .. import budgets
from ..domain import clock
from ..errors import BudgetExceeded, HoldActive
from ..persistence import store
from . import metering, waits


def _subtree(conn, ref):
    rows = conn.execute(
        "WITH RECURSIVE tree(ref,funding) AS (SELECT ?,NULL UNION ALL "
        "SELECT c.budget_ref,f.amounts_json FROM tree JOIN budgets c ON c.parent_ref=tree.ref "
        "JOIN budget_reservations f ON f.reservation_ref=?||c.budget_ref AND f.status='HELD') "
        "SELECT b.*,tree.funding FROM tree JOIN budgets b ON b.budget_ref=tree.ref",
        (ref,budgets.FUND_PREFIX)).fetchall()
    spent = sum(store.loads(r,"spent_json",{}).get("tokens",0) for r in rows)
    reserved = sum(store.loads(r,"reserved_json",{}).get("tokens",0)
                   -store.loads(r,"funding",{}).get("tokens",0) for r in rows)
    return {r["budget_ref"] for r in rows},spent,reserved


def _exposure(conn, active):
    roots = {}
    for row in active:
        held,lower = roots.get(row["thread_id"],(0,0))
        _,observed,_,_ = metering.observation(conn,row)
        if row["budget_status"]=="HELD":
            held += row["token_budget"] or 0
        else:
            observed = max(0,observed-(row["settled_tokens"] or 0))
        roots[row["thread_id"]] = (held,max(lower,observed))
    for root,(held,lower) in roots.items():
        total = conn.execute("SELECT sum(observed_total) FROM p0_meter_threads WHERE root_id=?",(root,)).fetchone()[0]
        if total is not None:
            settled = conn.execute("SELECT coalesce(sum(settled_tokens),0)+(SELECT coalesce(sum(delta),0) "
                "FROM p0_meter_adjustments WHERE root_id=?) FROM p0_meter_native_turns WHERE thread_id=?",
                (root,root)).fetchone()[0]
            roots[root] = (held,max(lower,total-settled))
    return (sum(lower for held,lower in roots.values()),sum(max(0,lower-held) for held,lower in roots.values()),
            sum(max(0,held-lower) for held,lower in roots.values()))


def _risk(conn, limit, spent, reserved, active, *, scope):
    observed, over_holds, unobserved = _exposure(conn,active)
    return {"scope":scope,"hard_tokens":limit,"settled_tokens":spent,"reserved_tokens":reserved,
            "observed_in_flight_tokens":observed,"observed_plus_settled":spent+observed,
            "committed_tokens":spent+reserved+over_holds,
            "available_tokens":None if limit is None else limit-spent-reserved-over_holds,
            "active_root_count":len({r["thread_id"] for r in active}),
            "unobserved_reserved_tokens":unobserved,
            "provider_maximum_overshoot_tokens":None,
            "maximum_exposure_note":"A GX grant bounds reserved units. Without an enforced provider response "
                                    "size or throughput bound, notification and interrupt latency have no finite token bound."}


def scopes(conn, run_id, ref):
    project = conn.execute("SELECT project_ref FROM runs WHERE run_id=?",(run_id,)).fetchone()[0]
    active = conn.execute("SELECT d.* FROM p0_meter_native_turns d JOIN runs r USING(run_id) WHERE r.project_ref=? "
        "AND (d.budget_status='HELD' OR (d.estimated_at IS NOT NULL AND EXISTS (SELECT 1 FROM "
        "p0_native_child_usage u JOIN p0_native_child_meters m USING(child_id) WHERE m.root_id=d.thread_id "
        "AND u.owner_turn=d.turn_id AND u.terminal=0)))",(project,)).fetchall()
    while ref:
        line = conn.execute("SELECT limits_json,parent_ref FROM budgets WHERE budget_ref=?",(ref,)).fetchone()
        refs,spent,held = _subtree(conn,ref)
        yield _risk(conn,store.loads(line,"limits_json",{}).get("tokens"),spent,held,
                    [r for r in active if r["budget_ref"] in refs],scope=ref)
        ref = line["parent_ref"]
    phase = budgets.project_budget_state(conn,project)
    if phase and "tokens" in phase["limits"]:
        yield _risk(conn,phase["limits"]["tokens"],phase["spent"]["tokens"],phase["reserved"]["tokens"],
                    active,scope="project:"+project)
    items = [item["value"]["max"] for item in budgets._formal_project_limits(conn,project)
             if item["value"]["resource"] == "tokens"]
    if items:
        used,held = budgets._project_usage(conn,project)
        yield _risk(conn,min(items),used["tokens"],held["tokens"],active,scope="intent:"+project)


def run_risk(conn, run_id):
    row = conn.execute("SELECT budget_ref FROM runs WHERE run_id=?",(run_id,)).fetchone()
    if not row or not row[0]:
        return {}
    return next(scopes(conn,run_id,row[0]))


def check_soft(conn, run_id, task_id):
    if not task_id or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_meter_turns'").fetchone():
        return
    opts = metering.policy(conn,run_id)
    if "soft_tokens" in opts:
        task = conn.execute("SELECT input_json FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if (task and store.loads(task,"input_json",{}).get("priority")=="low"
                and run_risk(conn,run_id).get("observed_plus_settled",0)>=opts["soft_tokens"]):
            raise HoldActive("low-priority native work is waiting at the soft token threshold",
                             details={"reason":"soft_budget","soft_tokens":opts["soft_tokens"]})


def allowance(conn, run_id, ref, binding):
    lines = list(scopes(conn,run_id,ref))
    check_soft(conn,run_id,binding.get("task_id"))
    constrained = [line for line in lines if line["hard_tokens"] is not None]
    if not constrained:
        return None
    crossed = [line for line in constrained if line["observed_in_flight_tokens"]
               and line["observed_plus_settled"]>=line["hard_tokens"]]
    if crossed:
        team = conn.execute("SELECT team_id FROM p0_teams WHERE run_id=? AND budget_ref=?",(run_id,ref)).fetchone()
        if team and all(line["scope"]==ref for line in crossed):
            raise HoldActive("the team's reported native use reached its token budget",
                             details={"reason":"budget_reserved","team_id":team[0],**crossed[0]})
        raise BudgetExceeded("reported native use reached the hard token budget",
                             details={"reason":"budget_exhausted","key":"tokens",**crossed[-1]})
    for line in constrained:
        if line["available_tokens"]<=0 and line["committed_tokens"]>line["settled_tokens"]+line["reserved_tokens"]:
            raise HoldActive("reported in-flight usage consumes the remaining token allowance",
                             details={"reason":"budget_reserved","key":"tokens",**line})
    return min(line["available_tokens"] for line in constrained)


def check(conn, run_id, root):
    row = conn.execute("SELECT budget_ref FROM p0_meter_native_turns WHERE run_id=? AND thread_id=? "
                       "ORDER BY sequence DESC LIMIT 1",(run_id,root)).fetchone()
    if not row:
        return
    for line in scopes(conn,run_id,row[0]):
        if line["hard_tokens"] is None or line["observed_plus_settled"] < line["hard_tokens"]:
            continue
        scope = line["scope"]
        rows = conn.execute("SELECT d.*,r.project_ref FROM p0_meter_native_turns d JOIN runs r USING(run_id) "
                            "WHERE d.budget_status='HELD' AND d.state='acknowledged' AND d.terminal_status IS NULL "
                            "AND r.project_ref=(SELECT project_ref FROM runs WHERE run_id=?)",(run_id,)).fetchall()
        refs = None if scope.startswith(("project:","intent:")) else _subtree(conn,scope)[0]
        affected = [r for r in rows if refs is None or r["budget_ref"] in refs]
        for r in affected:
            added = conn.execute("INSERT OR IGNORE INTO p0_meter_interrupts(thread_id,turn_id,run_id,reason,requested_at) "
                                 "VALUES(?,?,?,?,?)",(r["thread_id"],r["turn_id"],r["run_id"],scope,clock.now_iso()))
            if added.rowcount:
                waits.begin_delivery(conn,r["thread_id"],"resume:"+r["turn_id"],"budget")
        # A parent can be terminal while its funded native child is still running.
        for child in conn.execute("SELECT m.child_id,u.turn_id,d.budget_ref,d.run_id FROM p0_native_child_meters m "
                "JOIN p0_native_child_usage u USING(child_id) JOIN p0_meter_native_turns d "
                "ON d.thread_id=m.root_id AND d.turn_id=u.owner_turn JOIN runs r ON r.run_id=d.run_id "
                "WHERE m.metadata_verified=1 AND u.terminal=0 AND r.project_ref="
                "(SELECT project_ref FROM runs WHERE run_id=?)",(run_id,)):
            if refs is None or child["budget_ref"] in refs:
                conn.execute("INSERT OR IGNORE INTO p0_meter_interrupts(thread_id,turn_id,run_id,reason,requested_at) "
                             "VALUES(?,?,?,?,?)",(child["child_id"],child["turn_id"],child["run_id"],scope,clock.now_iso()))


def ended(conn, thread, turn, status):
    conn.execute("UPDATE p0_meter_interrupts SET terminal_status=?,ended_at=? WHERE thread_id=? AND turn_id=? "
                 "AND ended_at IS NULL",(status,clock.now_iso(),thread,turn))
