"""Local operator decisions on existing native usage, with durable age-based notices."""
import hashlib
import json
import os
from pathlib import Path

from .. import budgets
from ..domain import clock
from ..errors import SchemaInvalid, StaleRevision
from ..persistence import store
from . import metering, waits

DECISIONS = ("attach-evidence", "reconcile", "accept-exposure", "keep-unresolved", "release-hold")
SOURCES = ("native-receipt", "codex-rollout")
LEVELS = ("notice", "warning", "critical")


def check(action):
    required = {"kind", "reservation_ref", "decision", "reason"}
    decision = action.get("decision")
    if decision == "attach-evidence":
        required.update(("thread_id","turn_id","response_id"))
    elif decision == "release-hold":
        required.update(("acceptance_event_id","approve_release"))
    allowed = required | ({"source"} if decision == "attach-evidence" else set())
    if (decision not in DECISIONS or not required <= action.keys() <= allowed or
            any(not isinstance(action.get(k),str) or not action[k].strip() for k in
                required - {"approve_release","acceptance_event_id"})):
        raise SchemaInvalid("usage resolution needs a reservation, decision, reason and the decision's exact references")
    if decision == "release-hold" and (type(action["acceptance_event_id"]) is not int or action["approve_release"] is not True):
        raise SchemaInvalid("explicitly approve release and reference the prior exposure acceptance event")
    if action.get("source","native-receipt") not in SOURCES:
        raise SchemaInvalid("select native-receipt or codex-rollout as evidence source")


def _installed(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_usage_releases'").fetchone())


def _cases(conn, run_id, ref=None):
    installed = _installed(conn)
    date = "m.unresolved_at" if installed and any(c[1]=="unresolved_at" for c in
            conn.execute("PRAGMA table_info(p0_meter_turns)")) else "NULL"
    disputed = (" OR EXISTS(SELECT 1 FROM p0_meter_receipt_intervals i WHERE i.disputed=1 AND "
                "i.thread_id=d.thread_id AND i.turn_id=d.turn_id)") if installed else ""
    if installed:
        disputed += (" OR EXISTS(SELECT 1 FROM p0_native_child_meters cm JOIN p0_native_child_usage cu USING(child_id) "
                     "WHERE cm.root_id=d.thread_id AND cm.metadata_verified=1 AND cu.owner_turn=d.turn_id AND "
                     "(EXISTS(SELECT 1 FROM p0_meter_receipt_intervals i WHERE i.thread_id=cu.child_id AND i.turn_id=cu.turn_id AND i.disputed=1) "
                     "OR EXISTS(SELECT 1 FROM p0_meter_responses x WHERE x.thread_id=cu.child_id AND x.turn_id=cu.turn_id AND x.conflicting=1)))")
    target = " AND d.reservation_ref=?" if ref is not None else ""
    rows = conn.execute(
        "SELECT d.*,b.amounts_json,b.created_at," + date + " AS unresolved_at,coalesce(" + date +
        ",m.terminal_at,b.settled_at,b.created_at) AS since FROM p0_meter_native_turns d "
        "JOIN budget_reservations b USING(reservation_ref) LEFT JOIN p0_meter_turns m USING(reservation_ref) "
        "WHERE d.run_id=? " + target + " AND (d.confidence='UNRESOLVED' OR (d.confidence='HISTORICAL_SETTLED' AND d.settled_tokens IS NULL) "
        "OR (d.budget_status='HELD' AND d.state='sending') " + disputed + " OR EXISTS(SELECT 1 FROM p0_meter_responses x "
        "WHERE x.thread_id=d.thread_id AND x.turn_id=d.turn_id AND x.conflicting=1)) ORDER BY d.sequence",
        (run_id,ref) if ref is not None else (run_id,))
    return [dict(r) for r in rows]


def _latest(conn, run_id, ref):
    return conn.execute("SELECT event_id,actor,ts,reason,payload_json FROM events WHERE run_id=? "
                        "AND kind='usage.operator_resolution' AND subject_id=? ORDER BY event_id DESC LIMIT 1",
                        (run_id,ref)).fetchone()


def _facts(conn, row):
    if row["turn_id"]:
        _, lower, _, _ = metering.observation(conn,row,interval_floor=False)
    else:
        lower = 0
    amounts = store.loads(row, "amounts_json", {})
    return {"budget_status":row["budget_status"], "usage_confidence":row["confidence"], "funding":row["budget_ref"],
            "thread_id":row["thread_id"], "turn_id":row["turn_id"], "delivery_state":row["state"],
            "held_amounts":amounts if row["budget_status"] == "HELD" else {}, "original_reservation":amounts,
            "confirmed_lower_bound":lower, "settled_tokens":row["settled_tokens"],
            "unknown_tokens":0 if row["confidence"]=="PROVIDER_CONFIRMED" and row["attribution"]=="TURN" else None}


def _notices(conn, run_id, rows):
    if not rows:
        return [], None
    thresholds = metering.policy(conn,run_id)["unresolved_alert_seconds"]
    emitted = {(r[0],r[1]):r[2] for r in conn.execute("SELECT subject_id,json_extract(payload_json,'$.threshold_seconds'),event_id "
               "FROM events WHERE run_id=? AND kind='usage.unresolved_alert'", (run_id,))}
    notices = []
    next_due = None
    for row in rows:
        age = max(0,clock.age_seconds(row["since"]))
        ref = row["reservation_ref"]
        for level, threshold in zip(LEVELS,thresholds):
            event = emitted.get((ref,threshold))
            remaining = max(0,threshold-age)
            if event is None:
                next_due = remaining if next_due is None else min(next_due,remaining)
            if remaining == 0:
                notices.append({"warning_id":f"usage:{ref}:{threshold}", "level":level, "threshold_seconds":threshold,
                    "reservation_ref":ref, "first_unresolved_at":row["since"], "event_id":event,
                    "action":"gx-p0 operator usage-resolve", "decisions":list(DECISIONS)})
    return notices, next_due


def next_alert_seconds(conn, run_id):
    if not _installed(conn):
        return None
    return _notices(conn,run_id,_cases(conn,run_id))[1]


def due_alerts(conn, run_id):
    if not _installed(conn):
        return []
    return [a for a in _notices(conn,run_id,_cases(conn,run_id))[0] if a["event_id"] is None]


def emit_alerts(conn, run_id, due):
    for alert in due:
        ref, threshold = alert["reservation_ref"], alert["threshold_seconds"]
        row = _cases(conn,run_id,ref)
        if (row and row[0]["since"]==alert["first_unresolved_at"] and clock.age_seconds(row[0]["since"])>=threshold
                and threshold in metering.policy(conn,run_id)["unresolved_alert_seconds"]
                and not conn.execute("SELECT 1 FROM events WHERE run_id=? AND subject_id=? AND kind='usage.unresolved_alert' "
                                     "AND json_extract(payload_json,'$.threshold_seconds')=?",(run_id,ref,threshold)).fetchone()):
            store.append_event(conn,kind="usage.unresolved_alert",actor=metering.ACTOR,run_id=run_id,
                subject_kind="native_turn",subject_id=alert["reservation_ref"],payload={k:v for k,v in alert.items() if k!="event_id"})


def inspect(conn, run_id):
    rows = _cases(conn,run_id)
    alerts, _ = _notices(conn,run_id,rows)
    cases = []
    for row in rows:
        ref = row["reservation_ref"]
        latest = _latest(conn,run_id,ref)
        history = {"event_id":latest["event_id"],"actor":latest["actor"],"reason":latest["reason"],
                   "decision":store.loads(latest,"payload_json")["decision"]} if latest else None
        cases.append({"reservation_ref":ref,"first_unresolved_at":row["since"],"age_seconds":max(0,int(clock.age_seconds(row["since"]))),
                      "reason":row["metering_reason"] or "Awaiting verifiable native usage",
                      "last_operator_decision":history,**_facts(conn,row)})
    return cases, alerts


def _scope(conn, row, thread, turn):
    if (thread,turn) == (row["thread_id"],row["turn_id"]):
        return False,row["prior_tokens"]
    child = conn.execute("SELECT u.prior_tokens FROM p0_native_child_usage u JOIN p0_native_child_meters m USING(child_id) "
                         "WHERE m.run_id=? AND m.root_id=? AND m.metadata_verified=1 AND u.owner_turn=? AND u.child_id=? AND u.turn_id=?",
                         (row["run_id"],row["thread_id"],row["turn_id"],thread,turn)).fetchone()
    if not child:
        raise SchemaInvalid("the evidence thread and Turn do not belong to this reservation")
    return True,child["prior_tokens"]


def prepare_history(conn, run_id, action):
    """Read a host-bound history outside the SQLite writer, then recheck its binding on apply."""
    from .profiles import CODEX_VERSION
    from .root_delivery import _recorded_usage
    ref,thread,turn = (action[k] for k in ("reservation_ref","thread_id","turn_id"))
    row = metering._row(conn,ref)
    if not row or row["run_id"] != run_id:
        raise SchemaInvalid("unknown reservation for this run")
    _, prior = _scope(conn,row,thread,turn)
    bound = conn.execute("SELECT path FROM p0_usage_host_histories WHERE run_id=? AND root_id=? AND thread_id=?",
                         (run_id,row["thread_id"],thread)).fetchone()
    if not bound or prior is None:
        raise SchemaInvalid("the official host has not supplied an attributable history for this thread")
    path = Path(bound["path"])
    try:
        with path.open("rb") as stream:
            original = os.fstat(stream.fileno())
            header = json.loads(stream.readline())
        meta = header["payload"]
        if (header["type"] != "session_meta" or meta.get("id") != thread or meta.get("session_id") != row["thread_id"]
                or meta.get("cli_version") != CODEX_VERSION):
            raise ValueError("session identity or runtime differs")
        call = conn.execute("SELECT call_id FROM p0_root_terminals WHERE thread_id=? AND turn_id=?",
                            (row["thread_id"],row["turn_id"])).fetchone()
        proof = _recorded_usage(path,thread,turn,row["thread_id"],row["turn_id"],0,
                                terminal_call=call[0] if call else None,prior=prior)
        if not proof or action["response_id"] not in proof[4] or proof[1]-prior != proof[0]:
            raise ValueError("complete scoped token usage is unavailable")
        cost,total,end,_,receipts = proof
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            current = os.fstat(stream.fileno())
            if (original.st_dev,original.st_ino,original.st_mtime_ns)!=(current.st_dev,current.st_ino,current.st_mtime_ns):
                raise ValueError("history changed during verification")
            remaining = end
            while remaining:
                chunk = stream.read(min(65536,remaining))
                if not chunk:
                    raise ValueError("history ended before the verified terminal boundary")
                digest.update(chunk)
                remaining -= len(chunk)
        return {"source":"codex-rollout","path":str(path),"thread_id":thread,"turn_id":turn,
                "response_id":action["response_id"],"cumulative":total,"tokens":cost,"receipts":receipts,"end_offset":end,
                "prefix_sha256":digest.hexdigest()}
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        raise SchemaInvalid("the host-bound official history does not verify: " + str(exc)) from exc


def _attach(conn, row, action, proof):
    thread,turn,rid = (action[k] for k in ("thread_id","turn_id","response_id"))
    child,prior = _scope(conn,row,thread,turn)
    if proof:
        bound = conn.execute("SELECT path FROM p0_usage_host_histories WHERE run_id=? AND root_id=? AND thread_id=?",
                             (row["run_id"],row["thread_id"],thread)).fetchone()
        if (not bound or bound[0]!=proof["path"] or prior is None or proof["cumulative"]-prior!=proof["tokens"]
                or metering.receipt_conflict(conn,row["thread_id"],row["turn_id"])):
            raise StaleRevision("the original native history or its funding boundary changed")
        existing = conn.execute("SELECT response_id,turn_id,tokens,conflicting FROM p0_meter_responses WHERE thread_id=? "
            "AND (turn_id=? OR response_id IN (SELECT value FROM json_each(?)))",
            (thread,turn,json.dumps(list(proof["receipts"]))))
        if any(r["turn_id"] != turn or r["conflicting"] or r["response_id"] not in proof["receipts"] or
               (r["tokens"] is not None and r["tokens"] != proof["receipts"][r["response_id"]]) for r in existing):
            raise SchemaInvalid("the official history conflicts with previously recorded response IDs or amounts")
        table,key = ("p0_native_child_usage","child_id") if child else ("p0_root_turn_usage","thread_id")
        local = conn.execute(f"SELECT total_tokens,measured_tokens FROM {table} WHERE {key}=? AND turn_id=?",(thread,turn)).fetchone()
        if not local or (local["total_tokens"] or 0)>proof["cumulative"] or (local["measured_tokens"] or 0)>proof["tokens"]:
            raise SchemaInvalid("the native history precedes a newer scoped observation")
        for response_id,tokens in proof["receipts"].items():
            metering.response(conn,thread,turn,response_id,tokens)
        conn.execute(f"UPDATE {table} SET total_tokens=?,measured_tokens=? WHERE {key}=? AND turn_id=?",
                     (proof["cumulative"],proof["tokens"],thread,turn))
        metering.observe(conn,row["run_id"],row["thread_id"],thread,turn,proof["cumulative"],source="official operator-attached history")
        reference = {k:v for k,v in proof.items() if k!="receipts"}
    else:
        evidence = conn.execute("SELECT r.tokens,a.observed_at FROM p0_meter_responses r "
            "JOIN p0_response_attribution a USING(thread_id,turn_id,response_id) WHERE a.run_id=? AND a.root_id=? "
            "AND r.thread_id=? AND r.turn_id=? AND r.response_id=? AND r.conflicting=0 AND a.conflicting=0 "
            "AND r.tokens IS NOT NULL AND r.tokens=a.total_tokens",
            (row["run_id"],row["thread_id"],thread,turn,rid)).fetchone()
        if not evidence:
            raise SchemaInvalid("the native host has no matching verified receipt for those IDs")
        reference = {"source":"native-receipt","thread_id":thread,"turn_id":turn,"response_id":rid,**dict(evidence)}
    metering.recover_late_response_cost(conn,thread,turn,child=child,attached=True)
    return reference


def perform(ctx, run_id, mid, action, proof=None):
    conn,ref,decision = ctx.conn,action["reservation_ref"],action["decision"]
    selected = _cases(conn,run_id,ref)
    if not selected:
        raise SchemaInvalid("the selected run has no unresolved native reservation with that reference")
    row = selected[0]
    before = _facts(conn,row)
    latest = _latest(conn,run_id,ref)
    evidence = None
    if decision == "attach-evidence":
        evidence = _attach(conn,row,action,proof)
    elif decision == "reconcile" and row["turn_id"]:
        metering.reconcile_turn(conn,row["thread_id"],row["turn_id"])
    elif decision == "release-hold":
        acceptance = store.loads(latest,"payload_json",{}) if latest else {}
        if (before["budget_status"]!="HELD" or not latest or latest["event_id"]!=action["acceptance_event_id"]
                or acceptance.get("decision")!="accept-exposure" or acceptance.get("after")!=before):
            raise StaleRevision("a current exposure acceptance for this exact hold and evidence is required")
        budgets.release(conn,ref,actor=ctx.actor,reason=action["reason"])
        lower = before["confirmed_lower_bound"]
        call = int(row["state"]=="acknowledged" and bool(row["turn_id"]))
        conn.execute("INSERT INTO p0_usage_releases(reservation_ref,known_tokens,known_api_calls) VALUES(?,?,?)",(ref,lower,call))
        conn.execute("UPDATE p0_meter_turns SET settled_tokens=?,confidence='UNRESOLVED' WHERE reservation_ref=?",(lower,ref))
        budgets.record_released_native_usage(conn,ref,actual={"tokens":lower,"api_calls":call},previous={},
            key="operator-release:"+ref,actor=ctx.actor,run_id=run_id)
        waits.meter_settled(conn,row["thread_id"],row["turn_id"])
    raw = metering._row(conn,ref)
    after = _facts(conn,{**row,**dict(raw)})
    payload = {"message_id":mid,"decision":decision,"prior_decision_event_id":latest["event_id"] if latest else None,
               "before":before,"after":after,"evidence":evidence,"operator_subject":ctx.principal.subject}
    if decision == "release-hold":
        payload.update(acceptance_event_id=action["acceptance_event_id"],explicit_approval=True,known_api_calls=call)
    event = store.append_event(conn,kind="usage.operator_resolution",actor=ctx.actor,run_id=run_id,subject_kind="native_turn",
        subject_id=ref,prev_state=before["budget_status"],new_state=after["budget_status"],reason=action["reason"],payload=payload)
    if decision == "release-hold":
        conn.execute("UPDATE p0_usage_releases SET operator_event_id=? WHERE reservation_ref=?", (event,ref))
    return {"run_id":run_id,"reservation_ref":ref,"status":after["usage_confidence"],"operator_event_id":event,**payload}
