"""Compare existing budget lines with reported and reserved native work."""
from .. import budgets
from ..domain import clock
from ..errors import BudgetExceeded, HoldActive
from ..persistence import store
from . import metering, waits


def _subtree(conn, ref):
    rows = conn.execute(
        "WITH RECURSIVE tree(ref,funding,open) AS (SELECT ?,NULL,1 UNION ALL "
        "SELECT c.budget_ref,f.amounts_json,tree.open AND f.status='HELD' FROM tree JOIN budgets c ON c.parent_ref=tree.ref "
        "LEFT JOIN budget_reservations f ON f.reservation_ref=?||c.budget_ref) "
        "SELECT b.*,tree.funding,tree.open FROM tree JOIN budgets b ON b.budget_ref=tree.ref",
        (ref,budgets.FUND_PREFIX)).fetchall()
    # Closed children's balances already rolled up, while their unresolved exposure
    # continues to belong to the same parent. Keep the full ownership set.
    spent = sum(store.loads(r,"spent_json",{}).get("tokens",0) for r in rows if r["open"])
    reserved = sum(store.loads(r,"reserved_json",{}).get("tokens",0)
                   -store.loads(r,"funding",{}).get("tokens",0) for r in rows if r["open"])
    return {r["budget_ref"] for r in rows},spent,reserved


def _post_legacy_exposure(conn, root, last, extra):
    # The cash behind an old unitemized settlement is already present in spent.
    # A recorded scoped snapshot still bounds subsequent, independently observed growth.
    legacy = conn.execute("SELECT * FROM p0_meter_native_turns WHERE thread_id=? AND sequence<=? "
                          "AND budget_status='SETTLED' AND settled_tokens IS NULL ORDER BY sequence DESC LIMIT 1",
                          (root, last["sequence"])).fetchone()
    anchors = {root:legacy["total_tokens"]}
    for child in metering.children(conn, legacy):
        if child["metadata_verified"]:
            key = child["child_id"]
            old = anchors.get(key)
            if old is None or (child["total_tokens"] is not None and child["total_tokens"] > old):
                anchors[key] = child["total_tokens"]
    growth = 0
    for row in conn.execute("SELECT thread_id,observed_total FROM p0_meter_threads WHERE root_id=?", (root,)):
        key = row["thread_id"]
        if key not in anchors and conn.execute(
                "SELECT 1 FROM p0_native_child_usage u JOIN p0_meter_native_turns d ON d.thread_id=? "
                "AND d.turn_id=u.owner_turn WHERE u.child_id=? AND d.sequence>?", (root,key,legacy["sequence"])).fetchone():
            anchors[key] = 0
        baseline = anchors.get(key)
        if baseline is not None and row["observed_total"] is not None:
            growth += max(0, row["observed_total"] - baseline)
    paid_since = metering._prior_cost(conn, last, inclusive=True, after_sequence=legacy["sequence"])
    boundary = anchors[root]
    started_after = set()
    if any(c[1] == "started_after_legacy_sequence" for c in conn.execute("PRAGMA table_info(p0_meter_receipt_intervals)")):
        started_after = {r[0] for r in conn.execute("SELECT turn_id FROM p0_meter_receipt_intervals "
                        "WHERE thread_id=? AND started_after_legacy_sequence>=?", (root,legacy["sequence"]))}
    unreserved = sum(r["receipted"] for r in extra if r["turn_id"] in started_after
                     or (boundary is not None and r["total_tokens"] is not None and r["total_tokens"] - r["receipted"] >= boundary))
    return max(0, growth - paid_since), unreserved


def _exposure(conn, active, refs):
    roots = {}
    for row in active:
        held, lower = roots.get(row["thread_id"], (0, 0))
        _, observed, _, _ = metering.observation(conn, row, interval_floor=False)
        if row["budget_status"] == "HELD":
            held += row["token_budget"] or 0
        else:
            observed = max(0, observed - (row["settled_tokens"] or 0))
        roots[row["thread_id"]] = (held, lower + observed)
    observed = over_holds = unobserved = 0
    for root, (held, lower) in roots.items():
        unreserved = 0
        owners = {r[0] for r in conn.execute("SELECT DISTINCT budget_ref FROM p0_meter_native_turns WHERE thread_id=?",
                                              (root,))}
        if refs is None or owners <= refs:
            total = conn.execute("SELECT sum(observed_total) FROM p0_meter_threads WHERE root_id=?", (root,)).fetchone()[0]
            last = conn.execute("SELECT thread_id,sequence FROM p0_meter_native_turns WHERE thread_id=? "
                                "ORDER BY sequence DESC LIMIT 1", (root,)).fetchone()
            paid = metering._prior_cost(conn, last, inclusive=True)
            extra = metering.unreserved_root_turns(conn, root)
            if paid is None:
                residual, unreserved = _post_legacy_exposure(conn, root, last, extra)
            else:
                residual = max(0, total - paid) if total is not None else 0
                unreserved = sum(r["receipted"] for r in extra)
            lower = max(lower, residual - unreserved)
        observed += lower + unreserved
        over_holds += unreserved + max(0, lower - held)
        unobserved += max(0, held - lower)
    return observed, over_holds, unobserved


def _closed_exposure(conn, project):
    # A fully settled root can still publish previously unseen, unassigned thread usage.
    # Keep it in the common funding scope without attaching it to a completed Turn.
    late = ""
    if metering.has_receipt_adjustments(conn):
        late = "+ coalesce((SELECT delta FROM p0_meter_receipt_adjustments a WHERE a.reservation_ref=d.reservation_ref),0)"
    rows = conn.execute(
        "WITH observed AS (SELECT t.root_id,sum(t.observed_total) AS tokens FROM p0_meter_threads t "
        "JOIN runs r USING(run_id) WHERE r.project_ref=? GROUP BY t.root_id), "
        "closed AS (SELECT d.thread_id,max(d.sequence) AS sequence,group_concat(DISTINCT d.budget_ref) AS refs,"
        "count(*) FILTER(WHERE d.budget_status='SETTLED' AND d.settled_tokens IS NULL) AS unknown,"
        "sum(coalesce(d.settled_tokens,0)" + late + ") AS paid FROM p0_meter_native_turns d "
        "JOIN runs r USING(run_id) WHERE r.project_ref=? GROUP BY d.thread_id HAVING sum(d.budget_status='HELD')=0) "
        "SELECT c.thread_id,c.refs,c.sequence,c.unknown,o.tokens-c.paid-coalesce((SELECT sum(delta) FROM p0_meter_adjustments a "
        "WHERE a.root_id=c.thread_id),0) AS residual FROM closed c LEFT JOIN observed o ON o.root_id=c.thread_id "
        "WHERE c.unknown>0 OR residual>0 OR EXISTS (SELECT 1 FROM p0_meter_responses m WHERE m.thread_id=c.thread_id AND NOT EXISTS "
        "(SELECT 1 FROM p0_meter_native_turns d WHERE d.thread_id=m.thread_id AND d.turn_id=m.turn_id))", (project,project))
    closed = []
    for row in rows:
        root = row["thread_id"]
        extra = metering.unreserved_root_turns(conn, root)
        if row["unknown"]:
            residual, unreserved = _post_legacy_exposure(conn, root, row, extra)
        else:
            residual, unreserved = row["residual"] or 0, sum(r["receipted"] for r in extra)
        exposure = max(residual, unreserved)
        if exposure:
            closed.append((root, set(row["refs"].split(",")), exposure))
    return closed


def _risk(conn, limit, spent, reserved, active, *, scope, closed=(), refs=None):
    observed, over_holds, unobserved = _exposure(conn,active,refs)
    live = {r["thread_id"] for r in active}
    residual = sum(n for root, owners, n in closed if root not in live and (refs is None or owners <= refs))
    observed += residual
    over_holds += residual
    return {"scope":scope,"hard_tokens":limit,"settled_tokens":spent,"reserved_tokens":reserved,
            "observed_in_flight_tokens":observed,"observed_plus_settled":spent+observed,
            "committed_tokens":spent+reserved+over_holds,
            "available_tokens":None if limit is None else limit-spent-reserved-over_holds,
            "unfunded_closed_thread_tokens":residual, "active_root_count":len(live),
            "largest_active_turn_grant":max((r["token_budget"] or 0 for r in active),default=0),
            "unobserved_reserved_tokens":unobserved,
            "provider_maximum_overshoot_tokens":None,
            "maximum_exposure_note":"A GX grant bounds reserved units. Without an enforced provider response "
                                    "size or throughput bound, notification and interrupt latency have no finite token bound."}


def scopes(conn, run_id, ref):
    project = conn.execute("SELECT project_ref FROM runs WHERE run_id=?",(run_id,)).fetchone()[0]
    active = conn.execute("SELECT d.* FROM p0_meter_native_turns d JOIN runs r USING(run_id) WHERE r.project_ref=? "
        "AND (d.budget_status='HELD' OR (d.budget_status='RELEASED' AND d.attribution IS NOT 'TURN') OR (d.estimated_at IS NOT NULL AND EXISTS (SELECT 1 FROM "
        "p0_native_child_usage u JOIN p0_native_child_meters m USING(child_id) WHERE m.root_id=d.thread_id "
        "AND u.owner_turn=d.turn_id AND u.terminal=0)))",(project,)).fetchall()
    closed = _closed_exposure(conn, project)
    while ref:
        line = conn.execute("SELECT limits_json,parent_ref FROM budgets WHERE budget_ref=?",(ref,)).fetchone()
        refs,spent,held = _subtree(conn,ref)
        yield _risk(conn,store.loads(line,"limits_json",{}).get("tokens"),spent,held,
                    [r for r in active if r["budget_ref"] in refs],scope=ref,closed=closed,refs=refs)
        ref = line["parent_ref"]
    phase = budgets.project_budget_state(conn,project)
    if phase and "tokens" in phase["limits"]:
        yield _risk(conn,phase["limits"]["tokens"],phase["spent"]["tokens"],phase["reserved"]["tokens"],
                    active,scope="project:"+project,closed=closed)
    items = [item["value"]["max"] for item in budgets._formal_project_limits(conn,project)
             if item["value"]["resource"] == "tokens"]
    if items:
        used,held = budgets._project_usage(conn,project)
        yield _risk(conn,min(items),used["tokens"],held["tokens"],active,scope="intent:"+project,closed=closed)


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
    crossed = [line for line in constrained if (line["observed_in_flight_tokens"] or line["scope"]!=ref)
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
