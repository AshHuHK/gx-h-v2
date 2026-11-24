"""Replay the interrupted 1f8fc82 brief Turn from the retained official rollout and GX ledger."""
import argparse
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.p0 import metering
from gx_core.p0.root_delivery import RootDelivery, _recorded_usage
from gx_core.persistence import store


def evidence_frames(raw, case, call_id):
    records = [json.loads(line) for line in raw.splitlines()]
    target, = [r for r in records if r["type"] == "token_usage_record"
               and r["payload"]["response_id"] == call_id.removeprefix("call-")]
    cancel, = [r for r in records if (r["payload"].get("item") or {}).get("id") == call_id]
    if case == "missing_final_receipt":
        records.remove(target)
    elif case == "foreign_response":
        target["payload"]["root_turn_id"] = "other-turn"
    elif case == "wrong_thread_cumulative":
        target["payload"]["thread_token_usage"]["total_tokens"] -= 1
    elif case == "foreign_cancellation":
        cancel["payload"]["thread_id"] = "other-thread"
    elif case == "wrong_cancellation_reason":
        cancel["payload"]["item"]["error"] = "unrelated tool failure"
    elif case == "nonempty_cancelled_result":
        cancel["payload"]["item"]["content_items"] = [{"type":"inputText","text":"contradictory output"}]
    elif case == "later_unmeasured_response":
        records.insert(-1, {"type":"event_msg", "payload":{"type":"raw_response_completed",
                            "response_id":"later", "token_usage":None}})
    elif case == "later_model_item_without_usage":
        records.insert(-1, {"type":"response_item", "payload":{"type":"message","role":"assistant","content":[]}})
    elif case == "missing_abort":
        records.pop()
    else:
        return raw
    return b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in records)


def mirrored_controls(db, source, work):
    root, turn, call = db.execute("SELECT thread_id,turn_id,call_id FROM p0_meter_native_turns "
        "JOIN p0_root_terminals USING(thread_id,turn_id) WHERE confidence='PROVIDER_CONFIRMED'").fetchone()
    src, = source.glob("*" + root + ".jsonl.gz")
    raw = gzip.decompress(src.read_bytes())
    path = work / src.stem
    actual = {}
    for name in ("verified", "foreign_cancellation", "nonempty_cancelled_result"):
        path.write_bytes(evidence_frames(raw, name, call))
        proof = _recorded_usage(path, root, turn, root, turn, 0, terminal_call=call, prior=0)
        actual[name] = proof[0] if proof else None
    assert actual == {"verified":105, "foreign_cancellation":None, "nonempty_cancelled_result":None}
    return {"thread_id":root, "turn_id":turn, "call_id":call, "source_sha256":hashlib.sha256(raw).hexdigest(),
            "observations":actual}


def replay(source, result):
    ledger = gzip.decompress((source / "ledger.db.gz").read_bytes())
    result["ledger_sha256"] = hashlib.sha256(ledger).hexdigest()
    cases = ("public_only", "verified", "missing_final_receipt", "foreign_response", "wrong_thread_cumulative",
             "foreign_cancellation", "wrong_cancellation_reason", "nonempty_cancelled_result",
             "later_unmeasured_response", "later_model_item_without_usage", "missing_abort",
             "unmeasured_transport_response", "active_child")
    for case in cases:
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            (work / "gx.db").write_bytes(ledger)
            with closing(store.open_store(work / "gx.db")) as db:
                if case == "public_only":
                    result["mirrored_controls"] = mirrored_controls(db, source, work)
                ticket, = db.execute("SELECT * FROM p0_meter_native_turns WHERE confidence='ESTIMATED'").fetchall()
                root, turn, rid, ref = (ticket[k] for k in ("thread_id","turn_id","run_id","reservation_ref"))
                assert (ticket["terminal_status"], ticket["total_tokens"], ticket["measured_tokens"],
                        ticket["settled_tokens"]) == ("interrupted", 70, 105, 180000)
                call, = db.execute("SELECT call_id FROM p0_root_terminals WHERE thread_id=? AND turn_id=?",
                                   (root, turn)).fetchone()
                original, = db.execute("SELECT event_id,payload_json FROM events WHERE kind='budget.settled' "
                                       "AND json_extract(payload_json,'$.reservation_ref')=?", (ref,)).fetchall()
                src, = source.glob("*" + root + ".jsonl.gz")
                raw = gzip.decompress(src.read_bytes())
                result["rollout_sha256"] = hashlib.sha256(raw).hexdigest()
                path = work / src.stem
                content = evidence_frames(raw, case, call)
                path.write_bytes(content)
                before = metering.inspect(db, rid)
                with store.transaction(db, transaction_owner="Acceptance", operation="configure_brief_replay", run_id=rid):
                    if case != "public_only":
                        db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,"
                                   "'$.inputs.p0_spec.metering.rollout_diagnostics',json('true')) WHERE run_id=?", (rid,))
                    if case == "unmeasured_transport_response":
                        metering.response(db, root, turn, "additional-response", None)
                    if case == "active_child":
                        db.execute("INSERT INTO p0_native_child_meters(child_id,run_id,root_id,parent_id,owner_turn,"
                                   "metadata_verified,awaiting_turn) VALUES('late-child',?,?,?,?,1,0)", (rid,root,root,turn))
                        db.execute("INSERT INTO p0_native_child_usage(child_id,turn_id,owner_turn,prior_tokens) "
                                   "VALUES('late-child','child-turn',?,0)", (turn,))
                proof = _recorded_usage(path, root, turn, root, turn, 0, terminal_call=call, prior=0)
                # The retained ledger already contains the child inventory and terminal status from the real host.
                # Send only a minimal thread/read projection to the ordinary reconnect path.
                thread = {"id":root, "path":str(path), "turns":[{"id":turn,"status":"interrupted","itemsView":"notLoaded"}]}
                for _ in range(2):
                    RootDelivery(SimpleNamespace(conn=db, run_id=rid)).attached(root, thread)
                after = metering.inspect(db, rid)
                completed, = [t for t in after["turns"] if t["reservation_ref"] == ref]
                assert [tuple(e) for e in db.execute("SELECT event_id,payload_json FROM events WHERE kind='budget.settled' "
                        "AND json_extract(payload_json,'$.reservation_ref')=?", (ref,))] == [tuple(original)]
                adjustments = [a for a in after["adjustments"] if a["reservation_ref"] == ref]
                assert completed["settled_tokens"] == 180000
                if case == "verified":
                    assert proof[:2] == (105, 105) and completed["usage_class"] == "PROVIDER_CONFIRMED"
                    adj, = adjustments
                    assert (adj["delta"], adj["basis"], adj["settlement_event_id"]) == (-179895, "TURN_RECEIPT", original[0])
                    assert after["risk"]["settled_tokens"] == 210
                else:
                    assert completed["usage_class"] == "ESTIMATED" and not adjustments, case
                    assert after["risk"]["settled_tokens"] == before["risk"]["settled_tokens"] == 180105
                    if case not in ("public_only", "unmeasured_transport_response", "active_child"):
                        assert proof is None, case
                result["cases"][case] = {"status":"PASS","thread_id":root,"turn_id":turn,"call_id":call,
                    "source_sha256":hashlib.sha256(content).hexdigest(),"source_receipts":proof[4] if proof else None,
                    "original_event_id":original[0], "immutable_estimate_tokens":completed["settled_tokens"],
                    "turn_usage_class":completed["usage_class"], "adjustments":adjustments,
                    "run_settled_tokens":after["risk"]["settled_tokens"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Original review-b3 diagnostic directory")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = {"evidence_class":"ARCHIVED_OFFICIAL_HOST_REPLAY", "cases":{}}
    try:
        replay(args.source, result)
        result["status"] = "PASS"
    finally:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
