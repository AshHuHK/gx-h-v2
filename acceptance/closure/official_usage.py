"""Measure interrupted and delegated native turns with the release-locked official host."""
import argparse
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "acceptance"), str(ROOT / "gx-h")]
from gx_core.domain import clock
from gx_core.p0 import driver, metering
from gx_core.persistence import store

GRACE_SECONDS = 30


def database_path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def balance(conn, thread, turn):
    row = conn.execute("SELECT reservation_ref,terminal_at,budget_status,confidence,token_budget,terminal_status,"
                       "observed_lower_bound,measured_tokens,total_tokens,settled_tokens,estimated_at "
                       "FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=?", (thread, turn)).fetchone()
    events = [dict(e) for e in conn.execute(
        "SELECT event_id,kind,payload_json FROM events WHERE json_extract(payload_json,'$.reservation_ref')=? "
        "AND kind='budget.settled' ORDER BY event_id", (row["reservation_ref"],))]
    return {**dict(row), "settlements": [{**e, "payload_json": json.loads(e["payload_json"])} for e in events],
            "read_at": clock.now_iso()}


def observe_driver(db, run, thread, turn):
    """Observe the same live database before grace, at its deadline and on a fresh Driver."""
    with closing(store.open_store(db)) as conn, patch.dict(os.environ, {driver.OFFLINE: "1"}):
        grace = metering.policy(conn, run)["grace_seconds"]
        before = balance(conn, thread, turn)
        assert before["budget_status"] == "HELD" and before["estimated_at"] is None and not before["settlements"], before
        assert clock.age_seconds(before["terminal_at"]) < grace, before
        original = driver.project(conn, run)["status"]
        if original != "PAUSED":
            with store.transaction(conn):
                store.transition(conn, machine="run", subject_id=run, to_state="PAUSED", actor="usage-probe")
        try:
            with closing(driver.Driver(conn, db.parent, run)) as owner:
                owner.drive(0.15)
            warm = balance(conn, thread, turn)
            assert warm["budget_status"] == "HELD" and not warm["settlements"], warm
            assert clock.age_seconds(warm["terminal_at"]) < grace
            remaining = max(0, grace - clock.age_seconds(warm["terminal_at"]))
            with closing(driver.Driver(conn, db.parent, run)) as owner:
                owner.drive(remaining + 5)
            after = balance(conn, thread, turn)
            assert after["budget_status"] == "SETTLED" and after["confidence"] == "ESTIMATED" and after["estimated_at"], after
            assert clock.parse(after["estimated_at"]) >= clock.parse(after["terminal_at"]) + timedelta(seconds=grace)
            settlement, = after["settlements"]
            assert settlement["payload_json"]["confidence"] == "ESTIMATED"
            assert settlement["payload_json"]["actual"]["tokens"] == after["settled_tokens"]
            assert after["settled_tokens"] >= max(after["observed_lower_bound"], after["measured_tokens"] or 0)
            with closing(driver.Driver(conn, db.parent, run)) as reopened:
                reopened.drive(0.2)
            replay = balance(conn, thread, turn)
            assert replay["settlements"] == after["settlements"] and replay["settled_tokens"] == after["settled_tokens"]
        finally:
            if original != "PAUSED":
                with store.transaction(conn):
                    store.transition(conn, machine="run", subject_id=run, to_state=original, actor="usage-probe")
    return {"grace_seconds": grace, "before": before, "pre_grace_driver": warm, "post_grace_driver": after,
            "reopened_driver": replay, "native_offline_during_maintenance": True}


def reconcile_natural(roots, trial):
    assert trial["fault"] == "none" and not trial["injected_receipt_faults"]
    raw = [r for r in trial["events"] if r["method"] == "rawResponse/completed" and
           r["thread"] == trial["thread_id"] and r["turn"] == trial["turn_id"]]
    assert raw and all(r["response_id"] for r in raw)
    assert len({r["response_id"] for r in raw}) == len(raw)
    assert not trial["cold_usage"]["snapshots"] and not trial["cold_usage"]["new_model_requests"]
    scope = (trial["thread_id"], trial["turn_id"])
    observed = roots.conn.execute("SELECT response_id,tokens,conflicting FROM p0_meter_responses "
                                  "WHERE thread_id=? AND turn_id=?", scope).fetchall()
    assert not any(row["conflicting"] for row in observed)
    wire_costs = {r["response_id"]: r["usage"]["totalTokens"] if r["usage"] else None for r in raw}
    assert wire_costs == {row["response_id"]: row["tokens"] for row in observed}

    # The legacy protocol fixture is a bare @2 run: setting $.inputs.p0 before its
    # synthetic REJECTED verdict would commission the project-only scientific rejection
    # trigger. Expose this already completed fixture to the project Driver only while
    # testing its real reservation. Restore the fixture settings for the next trial.
    conn = roots.conn
    original = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (roots.run_id,)).fetchone()[0]
    enrollment = json.loads(original)
    enrollment["inputs"]["p0"] = True
    with store.transaction(conn):
        conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (json.dumps(enrollment), roots.run_id))
    try:
        result = observe_driver(database_path(conn), roots.run_id, trial["thread_id"], trial["turn_id"])
    finally:
        with store.transaction(conn):
            conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (original, roots.run_id))
    result["project_fixture_enrolled_after_terminal_submission"] = True
    return result


def native_receipts(conn, run):
    return [dict(r) for r in conn.execute(
        "SELECT t.thread_id,t.root_id,r.turn_id,r.response_id,r.tokens,r.conflicting "
        "FROM p0_meter_responses r JOIN p0_meter_threads t USING(thread_id) WHERE t.run_id=? "
        "ORDER BY r.rowid", (run,))]


def snapshot(conn, destination):
    with closing(sqlite3.connect(destination)) as backup:
        conn.backup(backup)
    return {"name": destination.name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def run(runtime, output):
    from acc1_child_late import measure as child_measurement
    from acc1_interrupted_meter import InterruptedPeer, measure as interrupted_measurement
    from acc1_protocol import fixture, binary_facts, run_case, _proxy_preflight
    _proxy_preflight()
    lock = json.loads((ROOT / "release-assets/lock.json").read_text())["linux_x64"]
    binary = runtime / "vendor/codex-runtime/package" / lock["binary"]
    host = binary_facts(binary)
    assert host["sha256"] == lock["binary_sha256"]
    results = {"class": "OFFICIAL_HOST_LOOPBACK", "binary": host, "cases": {}, "snapshots": [], "wire_controls": {}}
    for case, expected in (("two_samples", [35, 35]), ("missing_last_usage", [35, None])):
        with fixture(binary) as probe:
            wire = run_case(probe, case)
        observed = [f for f in wire["official_frames"] if f["method"] == "rawResponse/completed"
                    and (f["thread"], f["turn"]) == (wire["root_thread"], wire["original_turn"])]
        assert [r["usage"]["totalTokens"] if r["usage"] else None for r in observed] == expected
        assert len({r["responseId"] for r in observed}) == 2 and wire["new_provider_requests_during_replay"] == 0
        results["wire_controls"][case] = wire
    with fixture(binary) as probe:
        child = child_measurement(probe)
        assert child["passed"] and all(child["checks"].values())
        with closing(store.open_store(probe.directory / "gx.db")) as conn:
            receipts = native_receipts(conn, child["run"])
            parent = child["root"]
            turn = child["original_turn"]
            initial = balance(conn, parent, turn)
            child_tokens = sum(r["tokens"] for r in receipts if r["thread_id"] != parent)
            own_tokens = sum(r["tokens"] for r in receipts if (r["thread_id"], r["turn_id"]) == (parent, turn))
            assert initial["confidence"] == "PROVIDER_CONFIRMED" and not initial["estimated_at"]
            assert len(initial["settlements"]) == 1 and initial["settled_tokens"] == own_tokens + child_tokens == 253
            assert own_tokens == 70 and child_tokens == 183 and not any(r["conflicting"] for r in receipts)
            assert len({(r["thread_id"], r["response_id"]) for r in receipts}) == len(receipts)
            results["cases"]["U01"] = {"status": "PASS", "run": child["run"], "parent_turn": turn,
                                        "ledger": initial, "receipts": receipts}
            results["cases"]["U06"] = {"status": "PASS", "parent_thread": parent, "parent_tokens": own_tokens,
                                        "child_tokens": child_tokens, "single_settlement": initial["settlements"],
                                        "official_child": child}
            results["snapshots"].append(snapshot(conn, output.with_name(output.stem + "-child.db")))
    with fixture(binary, peer_type=InterruptedPeer) as probe:
        try:
            interruption = interrupted_measurement(probe, metering_grace=GRACE_SECONDS)
        finally:
            probe.peer.release.set()
        assert interruption["passed"], interruption["checks"]
        db = probe.directory / "gx.db"
        requests_before = len(probe.peer.rows)
        maintained = observe_driver(db, interruption["run"], interruption["thread"], interruption["turn"])
        assert len(probe.peer.rows) == requests_before
        with closing(store.open_store(db)) as conn:
            results["snapshots"].append(snapshot(conn, output.with_name(output.stem + "-interrupted.db")))
            receipts = native_receipts(conn, interruption["run"])
        assert len(receipts) == 1 and receipts[0]["tokens"] == 35 and not receipts[0]["conflicting"]
        assert receipts[0]["turn_id"] == interruption["turn"] and receipts[0]["response_id"]
        results["cases"]["U02"] = {
            "status": "PASS", "scenario": "operator interrupts an unfinished second response after the first response completed",
            "official_interruption": interruption, "raw_receipts": receipts, "grace_and_cold_driver": maintained,
            "additional_model_requests": len(probe.peer.rows) - requests_before,
        }
    results["passed"] = True
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT.parent / "gx-h-v-3")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() or any(args.output.with_name(args.output.stem + suffix).exists()
                                for suffix in ("-child.db", "-interrupted.db")):
        parser.error("choose a new output path")
    result = run(args.runtime.resolve(), args.output)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"cases": list(result["cases"]), "sha256": result["binary"]["sha256"]}))
