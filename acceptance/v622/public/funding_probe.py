"""Exercise the formal batch's actual local request boundary, recovery and pooled budgets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from http.client import HTTPConnection
from pathlib import Path
import argparse
import json
import threading

from acceptance import e2e_responses
from .funding import BatchFunding, FundingBlocked, ScriptedCost, check_slot_receipts, offline_responses, require_backend


SLOTS = ("01", "02", "03", "04")


def _request(server, scope, request):
    body = json.dumps({"model": "pinned-script", "scope": scope, "request": request, "input": [],
                       "reasoning": {"effort": "max"}})
    with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=8)) as client:
        client.request("POST", "/v1/responses", body, headers={"Content-Type": "application/json"})
        reply = client.getresponse()
        raw = reply.read()
    return reply.status, [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: ")]


def _host(trace, fund, slot, action=None):
    if action is None:
        def action(body, ident):
            return {"type": "message", "id": "m-" + ident, "role": "assistant", "content": []}, {"scope": body["scope"]}
    return offline_responses(trace, fund, slot, e2e_responses, action, scope_of=lambda body: body["scope"])


def _assert_cap(report):
    for line in [report["batch"], *report["slots"].values()]:
        assert 0 <= line["settled_tokens"] + line["held_tokens"] <= line["cap"], line


def _concurrent(directory):
    costs = ScriptedCost(800_000, 200_000, 150_000)
    spec = dict(series="four-slot-at-exact-limit", slots=SLOTS, scripted_cost=costs)
    path = directory / "concurrent.sqlite"
    fund = BatchFunding(path, **spec)
    for slot in SLOTS[:-1]:
        with _host(directory / (slot + ".jsonl"), fund, slot) as host:
            status, events = _request(host, "run", "wrapper")
            assert status == 200 and events[-1]["response"]["usage"]["total_tokens"] == 1_000_000
    started, release = threading.Event(), threading.Event()
    def pause(body, ident):
        started.set()
        if not release.wait(6):
            raise TimeoutError()
        return {"type": "message", "id": "m-" + ident, "role": "assistant", "content": []}, {"scope": body["scope"]}
    with _host(directory/"competition.jsonl", fund, SLOTS[-1], pause) as host:
        with ThreadPoolExecutor(max_workers=2) as pool:
            contender = threading.Barrier(2)
            def compete(scope):
                contender.wait(timeout=6)
                return _request(host, scope, "same-turn-follow-up")
            futures = [pool.submit(compete, scope) for scope in ("run", "module/child-1")]
            try:
                assert started.wait(6)
                during = BatchFunding(path, **spec).report()
                assert (during["batch"]["settled_tokens"], during["batch"]["held_tokens"]) == (3_000_000, 1_000_000)
                # Distinct connections in the threads have contended on the same final funds.
                assert during["slots"]["04"]["available_tokens"] == 0
            finally:
                release.set()
            statuses = sorted(f.result(timeout=8)[0] for f in futures)
            assert statuses == [200, 403]
        assert host.request_count == 1
    final = fund.report()
    assert all(line["settled_tokens"] == 1_000_000 for line in final["slots"].values())
    assert final["batch"]["settled_tokens"] == 4_000_000
    assert final["requests"][-1]["raw_provider_receipt"]["response"]["usage"]["input_tokens_details"] == {
        "cached_tokens":150_000}
    _assert_cap(during)
    _assert_cap(final)
    return {"statuses": statuses, "while_in_flight": during, "closed": final}


def _cross_slot(directory):
    # With the ordinary four equal grants the slot caps imply the batch cap. A tighter batch cap
    # independently exercises that second boundary while both slots still have enough local credit.
    fund = BatchFunding(directory/"cross-slot.sqlite", series="shared-balance", slots=SLOTS, slot_cap=70, batch_cap=35)
    barrier = threading.Barrier(2)
    def attempt(slot):
        with _host(directory/(slot+"-cross.jsonl"), fund, slot) as host:
            barrier.wait(timeout=5)
            return _request(host, "run" if slot == "01" else "module/child-2", "next")[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(attempt, ["01", "02"]))
    report = fund.report()
    assert statuses == [200,403] and report["batch"]["available_tokens"] == 0
    assert [v["available_tokens"] for v in report["slots"].values()].count(70) == 3
    _assert_cap(report)
    return {"statuses": statuses, "report": report}


def _oversized(directory):
    costs = ScriptedCost(32_768,200)
    out = {}
    for label, cap in (("too_short",32_768), ("sufficient",32_968)):
        fund = BatchFunding(directory/(label+".sqlite"), series=label, slots=SLOTS, slot_cap=cap, scripted_cost=costs)
        with _host(directory/(label+".jsonl"), fund, "01") as host:
            code, _ = _request(host, "module/last", "final-response")
            report = fund.report()
            assert (code, host.request_count, report["batch"]["settled_tokens"]) == (
                (403,0,0) if cap == 32_768 else (200,1,32_968))
            _assert_cap(report)
            out[label] = {"code": code, "provider_dispatches": host.request_count, "report": report}
    return {"old_turn_reservation": 32_768, "inclusive_maximum":32_968, **out}


def _disconnect(directory):
    spec = dict(series="lost-receipt", slots=SLOTS, slot_cap=70)
    path = directory/"disconnect.sqlite"
    fund = BatchFunding(path, **spec)
    def fail(body, ident):
        raise TimeoutError("No provider completion was received")
    with _host(directory/"uncertain.jsonl",fund,"01",fail) as host:
        first, events = _request(host, "module/work", "completion-lost")
        assert first == 400 and not events
        trace = list(host.rows)
    reopened = BatchFunding(path, **spec)
    prior = reopened.report()
    assert prior["batch"]["settled_tokens"] == 0 and prior["batch"]["held_tokens"] == 35
    assert prior["slots"]["01"]["final_scripted_tokens"] is None
    row, = prior["requests"]
    assert row["raw_provider_receipt"] is None and row["state"] == "UNKNOWN"
    held_report = check_slot_receipts(reopened, "01", trace, require_reconciled=False)[row["scope"]]
    assert held_report["fixture_total_tokens"] is None and held_report["held_tokens"] == 35
    try:
        check_slot_receipts(reopened, "01", trace)
    except FundingBlocked:
        pass
    else:
        raise AssertionError("Missing bill reported a settled slot")
    assert not reopened.reserve("01", row["scope"], row["request_id"], row["payload_sha256"])
    with _host(directory/"finish.jsonl",reopened,"01") as host:
        result, reply = _request(host, "run", "known-cleanup")
        assert result == 200
        denied, _ = _request(host, "module/work","retry")
        assert denied == 403 and host.request_count == 1
        trace.extend(host.rows)
    # Simulate independently recovered provider output, keeping unknown exposure until this point.
    receipt = {"type":"response.completed", "response":{"id":row["request_id"], "usage":reopened.cost.usage()}}
    try:
        reopened.settle(row["request_id"], reply[-1])
    except ValueError:
        pass
    else:
        raise AssertionError("Receipt for the other physical request debited this hold")
    assert reopened.settle(row["request_id"],receipt)
    assert not BatchFunding(path, **spec).settle(row["request_id"],receipt)
    assert not reopened.settle(reply[-1]["response"]["id"], reply[-1])
    final = reopened.report()
    assert final["batch"]["settled_tokens"] == 70 and final["batch"]["held_tokens"] == 0
    assert len({r["provider_receipt_id"] for r in final["requests"]}) == 2
    assert final["batch"]["final_scripted_tokens"] == 70
    by_scope = check_slot_receipts(reopened, "01", trace)
    assert all(v["fixture_total_tokens"] == 35 and v["held_tokens"] == 0 for v in by_scope.values())
    for bad in ([*trace, trace[0]], [{**trace[0], "payload_sha256":"f"*64}, *trace[1:]], trace[1:]):
        try:
            check_slot_receipts(reopened, "01", bad)
        except FundingBlocked:
            pass
        else:
            raise AssertionError("An incorrect or incomplete HTTP witness reconciled")
    _assert_cap(prior)
    _assert_cap(final)
    return {"unknown_after_restart": prior, "by_scope_before_last_receipt": held_report,
            "by_scope_after_receipt": by_scope, "denied_third_call": denied,
            "final":final, "same_receipt_debited_again":False}


def run(work):
    work = Path(work)
    work.mkdir(exist_ok=False, parents=True)
    try:
        require_backend("openai")
    except FundingBlocked as error:
        reason = str(error)
    else:
        raise AssertionError("The live preflight accepted a backend with no enforceable bound")
    fund = BatchFunding(work/"unsupported.sqlite", series="no-contract",slots=SLOTS)
    try:
        fund.reserve("01","run","unused",sha256(b"body").hexdigest(),backend="openai")
    except FundingBlocked:
        assert not fund.report()["requests"]
    else:
        raise AssertionError("An unsupported live provider received an admission")
    disconnected = _disconnect(work)
    result = {"schema":"gx.v622.funding-controls/1", "status":"PASS", "paid_provider_calls":0,
              "paid_state":"BLOCKED","paid_authorized":False,"live_provider_tokens":None,"live_provider_usd":None,
              "cases":{"concurrency":_concurrent(work), "batch_atomic":_cross_slot(work),
                       "oversized":_oversized(work), "disconnect":disconnected,
                       "idempotent":{"settlements":len(disconnected["final"]["requests"]),
                                     "settled_tokens":disconnected["final"]["batch"]["settled_tokens"],
                                     "duplicate_debits":0},
                       "unknown_upper":{"status":"BLOCKED","admitted":0,"reason":reason}}}
    (work/"result.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True,help="Fresh directory outside the checkout")
    args = parser.parse_args()
    if args.work.resolve().is_relative_to(Path(__file__).resolve().parents[3]):
        parser.error("Choose an external directory")
    print(run(args.work)["status"])
