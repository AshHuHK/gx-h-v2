"""Exercise estimate risk on the existing delivery, budget, status and operator usage paths."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h"), str(ROOT / "gx-h/tests")]
from acceptance.closure.test_usage import UsageClosure
from acceptance.closure.profile import test_environment
from gx_core import budgets
from gx_core.domain import clock
from gx_core.p0 import metering
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import records, store
from test_p0_meter_recovery import Peer, TurnMeterTests


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def budget_events(conn, rid):
    return [{**dict(row), "payload_json": json.loads(row["payload_json"])}
            for row in conn.execute(
                "SELECT event_id,ts,kind,subject_id,payload_json FROM events WHERE run_id=? "
                "AND (kind LIKE 'budget.%' OR kind LIKE 'usage.%') ORDER BY event_id", (rid,))]


def exercise(directory, state, with_child):
    case = UsageClosure()
    f = case.f = TurnMeterTests()
    status, usage, events = {}, {}, {}
    grant, lower = (800, 1000) if with_child else (1000, 800)
    env = test_environment(state / "cli")

    def capture(phase):
        args = ["--state", str(state), "--run-id", f.run]
        for name, operation, snapshots in (("status", ["status"], status), ("usage", ["operator", "usage"], usage)):
            cmd = [sys.executable, str(ROOT / "bin/gx-p0"), *operation, *args]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            if result.returncode:
                raise RuntimeError(f"{name} failed with {result.returncode}: {result.stderr}")
            snapshots[phase] = json.loads(result.stdout)
            save(directory / (name + ".json"), snapshots)
        meter = status[phase]["metering"]
        assert meter == usage[phase]["result"]["metering"], phase
        ticket, = meter["turns"]
        return ticket, ticket["cost_assessment"]

    def child_raw(amount):
        f.delivery.observe_descendant("rawResponse/completed", {**child, "responseId":"delayed",
            "usage": None if amount is None else {"totalTokens":amount}})

    def child_sample(amount):
        f.delivery.observe_descendant("thread/tokenUsage/updated", {**child, "tokenUsage":{"total":{"totalTokens":amount}}})

    def late():
        if with_child:
            child_raw(800)
            child_sample(1200)
        else:
            f.raw(turn, "delayed", 1000)
            f.sample(turn, 1800)

    path = state / "gx.db"
    with closing(store.open_store(path)) as f.db:
        f.run = records.create_run(f.db, objective="Measure late native usage", project_ref="estimate-risk", actor="fixture",
                                   inputs={"inputs":{"p0":True, "p0_spec":{"metering":{"grace_seconds":30}}}})
        with store.transaction(f.db, transaction_owner="Acceptance", operation="configure_estimate_case", run_id=f.run):
            ref = budgets.create_root(f.db, run_id=f.run, profile_name="p0", actor="fixture",
                                      limits={"tokens":5000, "api_calls":3})
            f.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (ref, f.run))
        f.peer = Peer(f.db, f.run)
        f.peer.token_budget = grant
        f.delivery = RootDelivery(f.peer)
        turn = f.start()
        child = case.start_child(turn, "child", "child-turn") if with_child else None
        f.raw(turn, "first", 600 if with_child else 800)
        if child:
            f.delivery.observe_descendant("rawResponse/completed", {**child, "responseId":"first", "usage":{"totalTokens":400}})
            child_raw(None)
            child_sample(400)
        else:
            f.raw(turn, "delayed", None)
        f.sample(turn, 600 if child else 800)
        _, view = capture("in_flight")
        assert view["scoped_observed_lower_bound_tokens"] == lower and view["turn_grant_tokens"] == grant
        assert view["provider_confirmed_total_tokens"] is None and view["unobserved_tokens_upper_bound"] is None
        assert "ROOT_TERMINAL_UNOBSERVED" in view["evidence_gaps"]
        f.complete()
        clock.advance(29)
        metering.maintain(f.db, f.run)
        ticket, view = capture("before_grace")
        assert ticket["budget_status"] == "HELD" and view["original_estimate_tokens"] is None
        clock.advance(2)
        metering.maintain(f.db, f.run)
        ticket, view = capture("estimated")
        assert view["estimate_policy"] == "ESTIMATED_CONSERVATIVE" and view["risk"] == "ESTIMATE_POSSIBLY_UNDERSTATED"
        assert view["original_estimate_tokens"] == max(view["turn_grant_tokens"], view["observed_lower_bound_at_estimate_tokens"]) == 1000
        assert view["scoped_observed_lower_bound_tokens"] == view["observed_lower_bound_at_estimate_tokens"] == lower
        assert view["unobserved_tokens_upper_bound"] is None and view["provider_confirmed_total_tokens"] is None
        assert "INCOMPLETE_END_USAGE" in view["evidence_gaps"]
        assert ("CHILD_TERMINAL_UNOBSERVED" in view["evidence_gaps"]) == with_child
        assert status["estimated"]["metering"]["cost_report"]["possibly_understated_estimate_refs"] == [ticket["reservation_ref"]]
        events["estimated"] = budget_events(f.db, f.run)
        save(directory / "events.json", events)
        if child:
            f.delivery.observe_descendant("turn/completed", {**child, "turn":{"id":"child-turn", "status":"completed"}})

    with closing(store.open_store(path)) as f.db:
        f.peer.conn = f.db
        f.delivery = RootDelivery(f.peer)
        late()
        _, view = capture("late_confirmed")
        assert view["risk"] == "PROVIDER_CONFIRMED" and view["estimate_policy"] == "ESTIMATED_CONSERVATIVE"
        assert view["provider_confirmed_total_tokens"] == view["scoped_observed_lower_bound_tokens"] == 1800
        assert view["unobserved_tokens_upper_bound"] == 0 and view["evidence_gaps"] == []
        assert view["original_estimate_tokens"] == 1000 and view["observed_lower_bound_at_estimate_tokens"] == lower

    with closing(store.open_store(path)) as f.db:
        f.peer.conn = f.db
        f.delivery = RootDelivery(f.peer)
        late()
        metering.maintain(f.db, f.run)
        ticket, reopened = capture("reopened")
        assert reopened == view and ticket["usage_class"] == "PROVIDER_CONFIRMED"
        assert budgets.state(f.db, ref)["spent"] == {"tokens":1800, "api_calls":1}
        meter = status["reopened"]["metering"]
        assert meter["cost_report"]["possibly_understated_estimate_refs"] == []
        events["reopened"] = budget_events(f.db, f.run)
        save(directory / "events.json", events)
        original, = [e for e in events["estimated"] if e["kind"] == "budget.settled"]
        assert original["payload_json"]["actual"]["tokens"] == 1000 and original["payload_json"]["confidence"] == "ESTIMATED"
        assert [e for e in events["reopened"] if e["kind"] == "budget.settled"] == [original]
        correction, = meter["adjustments"]
        assert (correction["basis"], correction["delta"], correction["settlement_event_id"]) == (
            "TURN_RECEIPT", 800, original["event_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    result = {"status":"PASS", "evidence_class":"SCRIPTED", "cases":[]}
    for name in ("parent", "parent_with_child"):
        directory = output / name
        directory.mkdir()
        case = {"name":name, "status":"PASS", "evidence_class":"SCRIPTED",
                **{k + "_path":f"{name}/{k}.json" for k in ("status", "usage", "events")}}
        result["cases"].append(case)
        try:
            with tempfile.TemporaryDirectory() as state:
                exercise(directory, Path(state), name == "parent_with_child")
        except Exception:
            result["status"] = case["status"] = "FAIL"
            case["failure"] = traceback.format_exc()
            sys.stderr.write(case["failure"])
        finally:
            clock.reset()
    save(output / "result.json", result)
    print(json.dumps(result))
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Assertion checks require standard Python execution")
    raise SystemExit(main())
