"""Drive the original Main turn for a queued PI input, without stepping the scientific workflow."""
import json
import time

from ..domain import ids
from ..native_release import native_state, ReleaseStateError
from ..persistence import store
from . import conversation, metering
from .driver import Driver, native_waits


def start_successor(conn, directory, rid, mid, text, options, *, native_owner):
    from . import successor
    result = successor.request(conn, directory, rid, mid, text, options, native_owner=native_owner)
    if result["status"] == "successor_started":
        with store.transaction(conn, transaction_owner="Conversation", operation="record_successor", run_id=rid) as tx:
            changed = tx.execute("UPDATE p0_operator_messages SET result_json=? WHERE run_id=? AND message_id=? "
                                 "AND result_json IS NULL", (ids.canonical_json(result), rid, mid))
            if changed.rowcount:
                store.append_event(tx, kind="p0.conversation.successor", actor="gx-operator", run_id=rid,
                                   subject_kind="message", subject_id=mid, payload=result)
    return result


def _result(conn, rid, mid):
    row = conn.execute("SELECT m.message_id,m.result_json,m.rejected,r.response,r.status,r.turn_id "
                       "FROM p0_operator_messages m LEFT JOIN p0_main_replies r USING(message_id) "
                       "WHERE m.run_id=? AND m.message_id=?", (rid, mid)).fetchone()
    if row is None:
        raise ValueError("No PI message with this ID on the selected Run")
    if row["rejected"]:
        return {"message_id":mid, "status":"rejected", "reason":row["rejected"]}
    result = json.loads(row["result_json"]) if row["result_json"] else None
    if result and result.get("status") == "successor_started":
        return {"message_id":mid, "status":"successor_started", "result":result}
    if row["turn_id"]:
        return {"message_id":mid, "status":"answered" if row["response"] else "ended_without_reply",
                "reply":{"text":row["response"],"turn_id":row["turn_id"],"status":row["status"]},
                "result":result}
    return {"message_id":mid, "status":"main_received" if result else "received", "result":result or "pending"}


def process(conn, directory, run_id, message_id):
    initial = _result(conn, run_id, message_id)
    if initial["status"] not in ("main_received","received"):
        return initial
    try:
        with native_state(directory):
            driver = Driver(conn, directory, run_id)
            try:
                return _drain(driver, message_id)
            finally:
                driver.close()
    except ReleaseStateError as exc:
        if "another native client" in str(exc):
            return {**initial, "status":"queued_for_active_driver"}
        raise


def _drain(driver, target):
    db, rid = driver.conn, driver.rid
    # Work behind a busy Main is bound to its original input. A cold owner resumes the
    # official thread (which replays terminal history) before polling for a new reply.
    attached = set()
    while True:
        result = _result(db, rid, target)
        if result["status"] not in ("main_received","received"):
            return result
        metering.maintain(db, rid)
        driver.messages()
        turns = db.execute("SELECT m.message_id,d.thread_id FROM p0_operator_messages m "
            "JOIN p0_root_deliveries d ON d.run_id=m.run_id AND d.event_id='human:'||m.message_id "
            "LEFT JOIN p0_main_replies r USING(message_id) WHERE m.run_id=? AND r.message_id IS NULL "
            "AND d.turn_id IS NOT NULL AND m.rejected IS NULL ORDER BY m.rowid", (rid,)).fetchall()
        for turn in turns:
            tid = turn["thread_id"]
            if tid not in attached and not driver.offline:
                driver._native_call("reply", turn["message_id"], lambda native: native._resume(tid))
                attached.add(tid)
            driver.finish_main(turn["message_id"], wait=0)
        result = _result(db, rid, target)
        if result["status"] not in ("main_received","received"):
            return result
        waits = native_waits(db, rid)
        if driver.offline or driver._exhausted or any(w["owner"] in ("message","reply","connection") for w in waits):
            return {**result, "waits":waits}
        # A genuinely busy turn keeps its owning App Server until it drains. An
        # existing cost hold with no native work can be picked up on the next wake.
        if not turns and (driver._native is None or not driver._native.active_turns):
            return result
        if not driver._poll(0.15):
            time.sleep(0.03)
