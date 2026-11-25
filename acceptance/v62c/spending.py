"""Offline boundary probe for the public pilot. There is no paid transport or enrollment here."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v62a.interleavings_fixture import Scenario
from acceptance.v62c.rehearse import grant
from gx_core import budgets
from gx_core.errors import BudgetExceeded, HoldActive
from gx_core.p0 import meter_limits


def scope(scenario, ref):
    return next(meter_limits.scopes(scenario.conn, scenario.run_id, ref))


def wire_response(delivery, thread, turn, tokens):
    # Exercise the same raw-response and scoped snapshot handlers as an App Server session.
    envelope = {"threadId": thread, "turnId": turn}
    delivery.observe("turn/started", {"threadId": thread, "turn": {"id": turn}})
    response = {**envelope, "responseId": "fixture-response", "usage": {"totalTokens": tokens}}
    snapshot = {**envelope, "tokenUsage": {"total": {"totalTokens": tokens}}}
    terminal = {"threadId": thread, "turn": {"id": turn, "status": "completed", "items": []}}
    delivery.observe("rawResponse/completed", response)
    delivery.observe("thread/tokenUsage/updated", snapshot)
    delivery.observe("turn/completed", terminal)
    # Repeat delivery after settlement. This must leave exactly one debit.
    delivery.observe("rawResponse/completed", response)
    delivery.observe("thread/tokenUsage/updated", snapshot)
    delivery.observe("turn/completed", terminal)


def probe(directory, architecture):
    spec = grant(architecture)
    cap = spec["run_budget"]["tokens"]
    turn_budget = spec["turn_token_budget"]
    with Scenario(directory, spec_updates=spec) as s:
        s.roots.token_budget = turn_budget
        run_budget = budgets.budget_for(s.conn, run_id=s.run_id)
        prior = cap - turn_budget
        budgets.spend(s.conn, budget_ref=run_budget, subject_kind="fixture_history",
                      subject_id="earlier-work", amounts={"tokens": prior}, actor="offline-spend-probe")
        actor = s.lead
        if architecture == "delegation_available":
            args = s.open_args("last-module", inputs=[s.materials[-1]], caps=spec["capabilities"],
                               limits={"tokens": turn_budget})
            args.update(module_version="2")
            module = s.call(s.lead, "gx_open_module", args, "last-module")
            actor = s.bind(module["task_id"])
        requests = []
        turn = "scripted-final-turn"

        def peer(method, params):
            if method == "thread/read":
                return {"thread": {"id": params["threadId"], "status": {"type": "idle"}}}
            if method != "turn/start":
                raise AssertionError(f"Unexpected scripted transport method {method}")
            requests.append(sorted(params))
            return {"turn": {"id": turn}}

        delivery = s.roots.delivery
        with patch.object(s.roots, "_request", side_effect=peer):
            receipt = delivery.deliver(actor.thread, "Boundary probe", event_id="last-eligible-turn")
            at_limit = scope(s, run_budget)
            if at_limit["committed_tokens"] != cap or at_limit["available_tokens"] != 0:
                raise AssertionError(at_limit)
            try:
                delivery.deliver(s.lead.thread, "Another turn while funds are held", event_id="in-flight-probe")
            except (BudgetExceeded, HoldActive) as exc:
                refused_while_held = exc.code
            else:
                raise AssertionError("Unfunded concurrent turn reached transport")
            # Synthetic final response exceeds the last grant, as permitted by the current wire.
            wire_response(delivery, actor.thread, turn, turn_budget + 200)
            settled = scope(s, run_budget)
            try:
                delivery.deliver(s.lead.thread, "Another turn after settlement", event_id="after-cap")
            except (BudgetExceeded, HoldActive) as exc:
                refused_after_settlement = exc.code
            else:
                raise AssertionError("Spent-through ledger admitted another turn")
        metered = s.conn.execute(
            "SELECT reservation_ref, budget_status, settled_tokens, confidence FROM p0_meter_native_turns "
            "WHERE thread_id=? AND turn_id=?", (actor.thread, turn)).fetchone()
        settlement_count, = s.conn.execute(
            "SELECT count(*) FROM events WHERE kind='budget.settled' "
            "AND json_extract(payload_json,'$.reservation_ref')=?", (metered["reservation_ref"],)).fetchone()
        if len(requests) != 1 or settlement_count != 1 or metered["budget_status"] != "SETTLED":
            raise AssertionError((requests, settlement_count, dict(metered)))
        return {"architecture": architecture, "charged_role": "lead" if actor == s.lead else "module",
                "run_id": s.run_id, "reservation_ref": metered["reservation_ref"],
                "prior_scripted_spend": prior, "turn_grant": receipt["token_budget"],
                "final_scripted_usage": metered["settled_tokens"], "settlement_count": settlement_count,
                "held_at_dispatch": at_limit["reserved_tokens"],
                "gx_turn_start_parameter_names": requests[0],
                "in_flight_admission_refusal": refused_while_held,
                "after_settlement_admission_refusal": refused_after_settlement,
                "slot_limit": cap, "settled_subtree_tokens": settled["settled_tokens"],
                "slot_overshoot": settled["settled_tokens"] - cap,
                "provider_maximum_overshoot_tokens": at_limit["provider_maximum_overshoot_tokens"]}


def preflight(work):
    path = Path(__file__).with_name("protocol.json")
    protocol = json.loads(path.read_bytes())
    rows = [probe(Path(work) / arch, arch) for arch in ("single", "delegation_available")]
    if any(r["slot_overshoot"] <= 0 for r in rows):
        raise AssertionError("Reassess provider bounds: the registered boundary counterexample changed")
    budget = protocol["budget"]
    if budget["paid_calls_authorized"] or budget["per_request_billable_ceiling_verified"]:
        raise ValueError("Protocol assertions conflict with this preflight; review operator scope and provider evidence")
    caps = budget["tokens_per_slot"] * len(protocol["slots"])
    if budget["tokens_whole_public_batch"] != caps:
        raise ValueError("Batch ceiling and individual slot grants are inconsistent")
    return {"series": protocol["series"], "protocol_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "classification": "OFFLINE_COUNTEREXAMPLE", "verification": "PASS", "paid_gate": "BLOCKED",
            "authorization": "AWAITING_EXACT_PUBLIC_FOUR_SLOT_AUTHORIZATION", "paid_enrollments": 0,
            "public_live_attempts_in_this_probe": 0, "private_prior_denominator": protocol["historical_private_denominator"],
            "paid_provider_calls": 0, "official_provider_tokens": None, "official_provider_amount": None,
            "accounting_unit": "Provider total input plus output. Cached input is a subset of input.",
            "proposed_caps": {"per_slot": budget["tokens_per_slot"], "all_four_public_slots": caps},
            "scripted_cases": rows,
            "illustrative_batch_if_three_other_slots_spend_their_cap": caps + rows[0]["slot_overshoot"],
            "limitation": "GX reserves one allowance per native turn. The final in-flight response can exceed "
                          "it before reconciliation or interruption. No finite provider maximum is established. "
                          "The four independent ledgers therefore cannot guarantee the proposed aggregate ceiling.",
            "paid_prerequisite": "A verified pre-dispatch maximum for each billable request, input and output "
                                 "included, reserved across the slot and batch for all active roles, and "
                                 "slot-specific spending authorization."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-paid-ready", action="store_true",
                        help="Return status 2 if current evidence blocks paid scheduling.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as work:
        result = preflight(work)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if args.require_paid_ready and result["paid_gate"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
