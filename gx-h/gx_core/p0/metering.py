"""Official cumulative token accounts and provisional native-turn settlements."""
from .. import budgets
from ..domain import clock
from ..errors import SchemaInvalid
from ..persistence import store
from . import waits

ACTOR = "p0-native-meter"


def policy(conn, run_id):
    row = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    spec = store.loads(row, "inputs_json", {}).get("inputs", {}).get("p0_spec", {}) if row else {}
    return validate_policy(spec.get("metering", {}))


def validate_policy(value):
    if not isinstance(value, dict) or value.keys() - {"grace_seconds", "soft_tokens", "rollout_diagnostics"}:
        raise SchemaInvalid("metering accepts grace_seconds, soft_tokens and rollout_diagnostics")
    for name, amount in value.items():
        if name=="rollout_diagnostics" and type(amount) is bool:
            continue
        if name=="rollout_diagnostics":
            raise SchemaInvalid("metering.rollout_diagnostics requires a boolean")
        if type(amount) is not int or amount < 0:
            raise SchemaInvalid(f"metering.{name} requires a nonnegative integer")
    return {"grace_seconds":30, **value}


def rollout_diagnostics(conn, run_id):
    explicit = policy(conn,run_id).get("rollout_diagnostics")
    if explicit is not None:
        return explicit
    return bool(conn.execute("SELECT 1 FROM p0_meter_legacy_runs WHERE run_id=?",(run_id,)).fetchone())


def observe(conn, run_id, root, thread, turn, total, *, source="thread/tokenUsage/updated"):
    """Preserve the high-water mark. Late packets never reduce an account or re-open a bill."""
    if type(total) is not int or total < 0:
        return
    now = clock.now_iso()
    conn.execute("INSERT OR IGNORE INTO p0_meter_threads(thread_id,run_id,root_id) VALUES(?,?,?)",
                 (thread,run_id,root))
    old = conn.execute("SELECT * FROM p0_meter_threads WHERE thread_id=?", (thread,)).fetchone()
    if (old["run_id"],old["root_id"]) != (run_id,root):
        return
    if old["observed_total"] == total:
        return
    sealed = (thread == root and conn.execute(
        "SELECT 1 FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=? "
        "AND confidence='PROVIDER_CONFIRMED' AND total_tokens IS NOT NULL AND total_tokens<>?",
        (thread,turn,total)).fetchone())
    if sealed or (old["observed_total"] is not None and total < old["observed_total"]):
        conn.execute("UPDATE p0_meter_threads SET anomaly_total=?,anomaly_at=? WHERE thread_id=? "
                     "AND anomaly_total IS NOT ?", (total,now,thread,total))
        return
    conn.execute("UPDATE p0_meter_threads SET observed_total=?,observed_at=?,observation_source=?,"
                 "confidence=CASE WHEN confidence='ESTIMATED' THEN confidence ELSE 'OBSERVED' END WHERE thread_id=?",
                 (total,now,source,thread))


def _row(conn, reservation):
    return conn.execute("SELECT * FROM p0_meter_native_turns WHERE reservation_ref=?", (reservation,)).fetchone()


def response(conn, thread, turn, response_id, tokens):
    if not isinstance(response_id,str) or not response_id:
        return
    conn.execute("INSERT INTO p0_meter_responses(thread_id,response_id,turn_id,tokens) VALUES(?,?,?,?) "
        "ON CONFLICT(thread_id,response_id) DO UPDATE SET conflicting=conflicting OR turn_id<>excluded.turn_id "
        "OR (tokens IS NOT NULL AND excluded.tokens IS NOT NULL AND tokens<>excluded.tokens), "
        "tokens=coalesce(tokens,excluded.tokens)",(thread,response_id,turn,tokens))


def _terminal_response(conn, thread, turn, call):
    return conn.execute(
        "SELECT a.response_id,a.request_ordinal,a.conflicting FROM p0_response_tools m "
        "JOIN p0_response_attribution a USING(thread_id,turn_id,response_id) "
        "JOIN p0_root_terminals e ON e.thread_id=m.thread_id AND e.turn_id=m.turn_id AND e.call_id=m.call_id "
        "WHERE m.thread_id=? AND m.turn_id=? AND m.source='MODEL' AND m.call_id=? "
        "AND m.namespace='gx' AND m.tool='gx_submit_product'", (thread, turn, call)).fetchone()


def terminal_response_observed(conn, thread, turn, call):
    """The linked raw completion arrived, including a provider completion with null usage."""
    return _terminal_response(conn, thread, turn, call) is not None


def terminal_response_cost(conn, thread, turn, call):
    """Exact public raw receipts through the direct terminal call, with continuous live coverage."""
    final = _terminal_response(conn, thread, turn, call)
    if not final or final['conflicting'] or final['request_ordinal'] is None:
        return None
    rows = conn.execute(
        'SELECT a.request_ordinal,a.response_id,a.total_tokens,a.conflicting,m.tokens,m.conflicting AS raw_conflict '
        'FROM p0_response_attribution a LEFT JOIN p0_meter_responses m USING(thread_id,turn_id,response_id) '
        'WHERE a.thread_id=? AND a.turn_id=? ORDER BY a.request_ordinal', (thread, turn)).fetchall()
    raw_count = conn.execute('SELECT count(*) FROM p0_meter_responses WHERE thread_id=? AND turn_id=?',
                             (thread, turn)).fetchone()[0]
    if len(rows) != final['request_ordinal'] or raw_count != len(rows) or rows[-1]['response_id'] != final['response_id']:
        return None
    total = 0
    for ordinal, row in enumerate(rows, 1):
        if (row['request_ordinal'] != ordinal or row['conflicting'] or row['raw_conflict']
                or row['total_tokens'] is None or row['tokens'] != row['total_tokens']):
            return None
        total += row['total_tokens']
    return total


def _raw_lower(conn, thread, turn):
    return conn.execute("SELECT coalesce(sum(tokens),0) FROM p0_meter_responses WHERE thread_id=? "
                        "AND turn_id=? AND conflicting=0",(thread,turn)).fetchone()[0]


def terminal(conn, root, turn):
    now = clock.now_iso()
    conn.execute("INSERT INTO p0_meter_turns(reservation_ref,terminal_at) "
                 "SELECT reservation_ref,? FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=? "
                 "AND budget_status='HELD' AND terminal_status IS NOT NULL ON CONFLICT(reservation_ref) DO UPDATE "
                 "SET terminal_at=coalesce(terminal_at,excluded.terminal_at)", (now,root,turn))


def children(conn, row):
    # A child can serve multiple turns or report after its parent becomes idle. Include
    # each thread's cumulative counter once, only through the specified paid turn.
    return conn.execute(
        "SELECT u.*,m.metadata_verified,m.awaiting_turn,d.sequence FROM p0_native_child_meters m "
        "JOIN p0_native_child_usage u USING(child_id) JOIN p0_meter_native_turns d "
        "ON d.thread_id=m.root_id AND d.turn_id=u.owner_turn WHERE d.thread_id=? AND d.sequence<=?",
        (row["thread_id"],row["sequence"])).fetchall()


def _prior_cost(conn, row, *, inclusive):
    op = "<=" if inclusive else "<"
    return conn.execute(
        "SELECT coalesce(sum(coalesce(settled_tokens,0) + coalesce((SELECT sum(a.delta) "
        "FROM p0_meter_adjustments a WHERE a.through_reservation=d.reservation_ref),0)),0) "
        "FROM p0_meter_native_turns d WHERE thread_id=? AND sequence"+op+"?",
        (row["thread_id"],row["sequence"])).fetchone()[0]


def observation(conn, row):
    members = {}
    eligible = bool(row["children_known"])
    lower = max(row["measured_tokens"] or 0,_raw_lower(conn,row["thread_id"],row["turn_id"]))
    for u in children(conn, row):
        if not u["metadata_verified"]:
            eligible = False
            continue
        if u["total_tokens"] is not None:
            members[u["child_id"]] = max(members.get(u["child_id"],0),u["total_tokens"])
        eligible &= bool(u["terminal"] and u["children_known"] and u["total_tokens"] is not None)
        if u["owner_turn"] == row["turn_id"]:
            lower += max(u["measured_tokens"] or 0,_raw_lower(conn,u["child_id"],u["turn_id"]))
    pending = conn.execute(
        "SELECT 1 FROM p0_native_child_meters m JOIN p0_meter_native_turns d ON "
        "d.thread_id=m.root_id AND d.turn_id=m.owner_turn WHERE d.thread_id=? AND d.sequence<=? "
        "AND (m.metadata_verified=0 OR m.awaiting_turn=1) LIMIT 1", (row["thread_id"],row["sequence"])).fetchone()
    cumulative = None if row["total_tokens"] is None else row["total_tokens"] + sum(members.values())
    if cumulative is not None:
        lower = max(lower, cumulative - _prior_cost(conn,row,inclusive=False))
    return cumulative, max(0,lower), eligible and not pending, members


def _reconcile(conn, row, *, confirmed=False, late=False):
    if not row or row["budget_status"] != "SETTLED":
        return
    estimates = conn.execute("SELECT * FROM p0_meter_native_turns WHERE thread_id=? "
        "AND sequence<=? AND estimated_at IS NOT NULL ORDER BY sequence", (row["thread_id"],row["sequence"])).fetchall()
    if not estimates:
        if confirmed:
            cumulative,_,complete,members = observation(conn,row)
            if complete and cumulative is not None:
                _checkpoint(conn,row,members)
        return
    # Do not revise an earlier prefix using snapshots or payments of later turns.
    latest = conn.execute("SELECT max(sequence) FROM p0_meter_native_turns WHERE thread_id=? "
                          "AND budget_status='SETTLED'", (row["thread_id"],)).fetchone()[0]
    if latest != row["sequence"]:
        return
    cumulative,_,complete,members = observation(conn,row)
    if not complete or cumulative is None:
        return
    frozen = row["estimate_snapshot"]
    if not confirmed and not (late and cumulative > (frozen if frozen is not None else 0)):
        return  # Resume can repeat the last intermediate count, including zero.
    delta = cumulative - _prior_cost(conn,row,inclusive=True)
    if delta and len({e["budget_ref"] for e in estimates}) != 1:
        return  # An aggregate cannot decide how much belongs to two distinct funding owners.
    if delta:
        # Only the provisional portion of the same stream is refundable.
        portions = []
        remaining = delta
        for estimate in reversed(estimates):
            original = estimate["reservation_ref"]
            adjusted = conn.execute("SELECT coalesce(sum(delta),0) FROM p0_meter_adjustments WHERE reservation_ref=?",
                                    (original,)).fetchone()[0]
            part = max(remaining,-(estimate["settled_tokens"]+adjusted))
            if part:
                portions.append((original,part))
                remaining -= part
            if not remaining:
                break
        if remaining:
            return
        for original,part in portions:
            inserted = conn.execute("INSERT OR IGNORE INTO p0_meter_adjustments "
                "(reservation_ref,through_reservation,root_id,reported_total,delta,created_at) VALUES(?,?,?,?,?,?)",
                (original,row["reservation_ref"],row["thread_id"],cumulative,part,clock.now_iso()))
            if inserted.rowcount:
                key = f"{row['reservation_ref']}:{cumulative}:{original}"
                budgets.adjust_settled_tokens(conn,original,delta=part,adjustment_id=key,actor=ACTOR)
    conn.executemany("UPDATE p0_meter_turns SET confidence='PROVIDER_RECONCILED',"
                     "attribution='UNATTRIBUTED_THREAD_USAGE' WHERE reservation_ref=?",
                     ((e["reservation_ref"],) for e in estimates))
    _checkpoint(conn,row,members)


def _checkpoint(conn, row, members):
    for thread, total in {row["thread_id"]:row["total_tokens"], **members}.items():
        conn.execute("UPDATE p0_meter_threads SET reconciled_total=max(coalesce(reconciled_total,0),?), "
                     "confidence='PROVIDER_RECONCILED' WHERE thread_id=?", (total,thread))


def late_cumulative(conn, root, turn):
    row = conn.execute("SELECT * FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=? "
                       "AND budget_status='SETTLED'", (root,turn)).fetchone()
    if row and row["estimated_at"] and row["total_tokens"] is not None and row["prior_tokens"] is not None:
        exact = row["measured_tokens"] == row["total_tokens"]-row["prior_tokens"]
        if exact:
            exact = all(u["terminal"] and u["measured_tokens"] is not None and u["prior_tokens"] is not None
                        and u["total_tokens"] is not None and u["total_tokens"]-u["prior_tokens"]==u["measured_tokens"]
                        for u in children(conn,row) if u["owner_turn"]==turn)
        _reconcile(conn,row,confirmed=exact,late=True)


def confirm(conn, reservation, amount):
    budgets.settle(conn,reservation,actual={"tokens":amount,"api_calls":1},actor=ACTOR,
                   confidence="PROVIDER_CONFIRMED")
    conn.execute("UPDATE p0_meter_turns SET confidence='PROVIDER_CONFIRMED',settled_tokens=?,"
                 "observed_lower_bound=?,attribution='TURN' WHERE reservation_ref=?", (amount,amount,reservation))
    row = _row(conn,reservation)
    _reconcile(conn,row,confirmed=True)
    waits.meter_settled(conn,row["thread_id"],row["turn_id"])


def settle_cumulative(conn, root, turn):
    """A live resumed thread may publish cumulative usage without raw response frames."""
    row = conn.execute("SELECT * FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=? "
                       "AND budget_status='HELD' AND terminal_status='completed'",(root,turn)).fetchone()
    if not row or row["prior_tokens"] is None or row["total_tokens"] is None or row["total_tokens"]<=row["prior_tokens"]:
        return
    cumulative,_,complete,_ = observation(conn,row)
    if not complete or cumulative is None:
        return
    previous = conn.execute("SELECT * FROM p0_meter_native_turns WHERE thread_id=? AND sequence<? "
                            "ORDER BY sequence DESC LIMIT 1",(root,row["sequence"])).fetchone()
    before = observation(conn,previous)[0] if previous else 0
    # The delta can contain unresolved earlier work. Keep its turn attribution empty.
    amount = max(0,cumulative-(before if before is not None else row["prior_tokens"]))
    budgets.settle(conn,row["reservation_ref"],actual={"tokens":amount,"api_calls":1},
                   actor=ACTOR,confidence="PROVIDER_RECONCILED")
    conn.execute("UPDATE p0_meter_turns SET confidence='PROVIDER_RECONCILED',settled_tokens=?,"
                 "attribution='UNATTRIBUTED_THREAD_USAGE' WHERE reservation_ref=?", (amount,row["reservation_ref"]))
    _reconcile(conn,_row(conn,row["reservation_ref"]),confirmed=True)
    waits.meter_settled(conn,root,turn)

def maintain(conn, run_id):
    """DB-only grace processing, also usable with an offline or paused native connection."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_meter_turns'").fetchone():
        return
    outstanding = ("FROM p0_meter_native_turns WHERE run_id=? AND budget_status='HELD' "
                   "AND state='acknowledged' AND terminal_status IS NOT NULL")
    oldest = conn.execute("SELECT terminal_at "+outstanding+" ORDER BY terminal_at LIMIT 1",(run_id,)).fetchone()
    if not oldest:
        return
    grace = policy(conn,run_id)["grace_seconds"]
    if oldest["terminal_at"] and clock.age_seconds(oldest["terminal_at"])<grace:
        return
    with store.transaction(conn, transaction_owner="Usage", operation="maintain", run_id=run_id):
        rows = conn.execute("SELECT * "+outstanding+" ORDER BY sequence",(run_id,)).fetchall()
        for row in rows:
            if not row["terminal_at"]:
                terminal(conn,row["thread_id"],row["turn_id"])
                row = _row(conn,row["reservation_ref"])
            if clock.age_seconds(row["terminal_at"]) < grace:
                continue
            cumulative,lower,_,_ = observation(conn,row)
            estimate = max(row["token_budget"],lower)
            budgets.settle(conn,row["reservation_ref"],actual={"tokens":estimate,"api_calls":1},
                           actor=ACTOR,confidence="ESTIMATED")
            conn.execute("UPDATE p0_meter_turns SET confidence='ESTIMATED',estimated_at=?,settled_tokens=?,"
                         "observed_lower_bound=?,estimate_snapshot=?,attribution='PROVISIONAL_TURN' "
                         "WHERE reservation_ref=?", (clock.now_iso(),estimate,lower,cumulative,row["reservation_ref"]))
            conn.execute("INSERT INTO p0_meter_threads(thread_id,run_id,root_id,confidence) "
                         "VALUES(?,?,?,'ESTIMATED') ON CONFLICT(thread_id) DO UPDATE SET confidence='ESTIMATED'",
                         (row["thread_id"],run_id,row["thread_id"]))
            waits.meter_settled(conn,row["thread_id"],row["turn_id"])
            from . import meter_limits
            meter_limits.check(conn,run_id,row["thread_id"])


def inspect(conn, run_id, *, include_responses=False):
    from . import meter_limits, response_attribution
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_meter_turns'").fetchone():
        return {"threads":[],"turns":[],"adjustments":[],"interrupts":[],"risk":{}}
    def rows(sql):
        return [dict(r) for r in conn.execute(sql,(run_id,))]
    result = {"threads":rows("SELECT * FROM p0_meter_threads WHERE run_id=? ORDER BY thread_id"),
              "turns":rows("SELECT * FROM p0_meter_native_turns WHERE run_id=? ORDER BY sequence"),
              "adjustments":rows("SELECT a.* FROM p0_meter_adjustments a JOIN p0_meter_native_turns d ON "
                                 "a.reservation_ref=d.reservation_ref WHERE d.run_id=? ORDER BY a.adjustment_id"),
              "interrupts":rows("SELECT * FROM p0_meter_interrupts WHERE run_id=? ORDER BY requested_at"),
              "risk":meter_limits.run_risk(conn,run_id), "token_unit":"provider totalTokens, including cached input once",
              "provider_money_cost":None}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_response_attribution'").fetchone():
        if include_responses:
            result["response_attribution"] = response_attribution.report(conn, run_id)
            # A host-created extra Goal Turn has no GX reservation. Its thread-wide
            # cumulative counter does not establish that Turn's incremental cost.
            result["unreserved_native_turns"] = rows(
                "SELECT u.thread_id,u.turn_id,u.terminal_status,u.total_tokens AS reported_thread_total,"
                "count(m.response_id) AS observed_response_count,"
                "sum(CASE WHEN m.conflicting=0 THEN m.tokens END) AS observed_response_lower_bound "
                "FROM p0_root_turn_usage u JOIN p0_roots r USING(thread_id) LEFT JOIN p0_meter_responses m "
                "ON m.thread_id=u.thread_id AND m.turn_id=u.turn_id WHERE r.run_id=? AND NOT EXISTS "
                "(SELECT 1 FROM p0_meter_native_turns d WHERE d.thread_id=u.thread_id AND d.turn_id=u.turn_id) "
                "GROUP BY u.thread_id,u.turn_id ORDER BY u.thread_id,u.rowid")
        else:
            result["attributed_response_count"] = conn.execute(
                "SELECT count(*) FROM p0_response_attribution WHERE run_id=?", (run_id,)).fetchone()[0]
    for thread in result["threads"]:
        total = thread["observed_total"]
        thread["unresolved_delta"] = None if total is None else total - (thread["reconciled_total"] or 0)
    return result
