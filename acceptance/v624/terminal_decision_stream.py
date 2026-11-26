"""Deliver an accepted Lead decision's SSE item and completion on opposite sides of the old one-second timeout."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

from decision_wire import batch, funding, run


GRACE_SECONDS = 4
DELAY_SECONDS = 2


def configure(work, rid):
    # Only the fresh, local fixture Run exists here. Stage its normal metering policy before dispatch.
    with closing(sqlite3.connect(work / "state/gx.db")) as db, db:
        assert not db.execute("SELECT 1 FROM p0_roots").fetchone()
        db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.metering.grace_seconds',?) "
                   "WHERE run_id=?", (GRACE_SECONDS, rid))


def terminal(work):
    with closing(batch.old.db_open(work)) as db:
        row = db.execute("SELECT recorded_at,interrupt_ack_at,call_id FROM p0_root_terminals").fetchone()
        return dict(row) if row else {}


def wait_for(work, field, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = terminal(work).get(field)
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError(f"Timed out waiting for {field}")


def check(work, case):
    from acceptance.v623.oauth.host import _pin
    from acceptance.v623.oauth.usage import reconcile

    _, binary_sha = _pin(batch.old.BINARY)
    witness = {}

    @contextmanager
    def split_stream(trace, ledger, slot, responses, action, *, scope_of):
        class Peer(responses.Peer):
            def do_POST(self):
                if self.path != "/v1/responses":
                    return self.send_error(404)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with self.server.lock:
                    self.server.request_count += 1
                    ident = f"fixture-{self.server.request_count}"
                item, _ = action(body, ident)
                self.server.record({"id": ident, "scope": scope_of(body), "call": item["call_id"]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(kind, **fields):
                    self.wfile.write(("data: " + json.dumps({"type": kind, **fields}) + "\n\n").encode())
                    self.wfile.flush()

                emit("response.created", response={"id": ident})
                emit("response.output_item.done", item=item)
                if "decision" in json.loads(item["arguments"]):
                    witness.update(response_id=ident, call_id=item["call_id"])
                    wait_for(work, "recorded_at", 10)
                    if case == "absent":
                        wait_for(work, "interrupt_ack_at", GRACE_SECONDS + 8)
                        return
                    time.sleep(DELAY_SECONDS)
                    witness["completion_write_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    emit("response.completed", response={"id": ident, "usage": ledger.cost.usage()})
                except (BrokenPipeError, ConnectionResetError):
                    witness["client_closed"] = True

        with responses.local_responses(trace) as server:
            server.RequestHandlerClass = Peer
            yield server

    with patch.object(funding, "offline_responses", split_stream):
        wire = json.loads(run(work, before_run=configure).read_text())
    with closing(batch.old.db_open(work)) as db:
        meter = dict(db.execute("SELECT budget_status,confidence,terminal_status,settled_tokens "
                                "FROM p0_meter_native_turns").fetchone())
        final = dict(db.execute(
            """SELECT m.response_id,m.link,g.outcome FROM p0_response_tools g
               LEFT JOIN p0_response_tools m ON m.source='MODEL' AND m.call_id=g.model_call_id
               AND m.thread_id=g.thread_id AND m.turn_id=g.turn_id AND m.tool=g.tool
               WHERE g.source='GX' AND g.call_id=?""", (witness["call_id"],)).fetchone())
        observed = db.execute("SELECT observed_at FROM p0_response_attribution WHERE response_id=?",
                              (witness["response_id"],)).fetchone()
        receipts = [list(r) for r in db.execute("SELECT response_id,tokens FROM p0_meter_responses ORDER BY response_id")]
    term = terminal(work)
    accounting = reconcile(work, wire["goal_run"])
    elapsed = (datetime.fromisoformat(term["interrupt_ack_at"]) - datetime.fromisoformat(term["recorded_at"])).total_seconds()
    outputs = []
    for file in (work / "codex/sessions").rglob("rollout-*.jsonl"):
        with file.open() as lines:
            for line in lines:
                p = json.loads(line).get("payload", {})
                if p.get("type") == "function_call_output" and p.get("call_id") == witness["call_id"]:
                    outputs.append(p.get("output"))
    result = {"case": case, "official_binary_sha256": binary_sha, "accepted_event": wire["adopted_events"],
              "run_id": wire["goal_run"], "real_model_calls": 0, "scripted_requests": wire["requests"],
              "metering_grace_seconds": GRACE_SECONDS, "delay_seconds": DELAY_SECONDS if case == "late" else None,
              "commit_to_interrupt_ack_seconds": elapsed, "terminal": term, "wire": witness, "meter": meter,
              "last_receipt_at": observed[0] if observed else None, "usage_status": accounting["usage_status"],
              "total_tokens": accounting["total_tokens"],
              "host_receipts": receipts, "final_call": final, "terminal_outputs": outputs}
    out = work / "terminal-stream.json"
    batch.save(out, result)
    print(json.dumps(result, sort_keys=True), flush=True)
    assert term["call_id"] == witness["call_id"] and wire["requests"] == 2 and final["outcome"] == "ACCEPTED"
    assert outputs == ["dynamic tool call was cancelled before receiving a response"]
    assert meter["terminal_status"] == "interrupted"
    if case == "late":
        assert final["response_id"] == witness["response_id"], "Interrupt lost the accepted decision's completion"
        assert len(receipts) == 2 and meter["confidence"] == "PROVIDER_CONFIRMED"
        assert meter["settled_tokens"] == sum(n for _, n in receipts)
        assert accounting["usage_status"] == "SETTLED" and accounting["total_tokens"] == meter["settled_tokens"]
        native_time = datetime.fromisoformat(observed[0])
        assert datetime.fromisoformat(witness["completion_write_at"]) <= native_time
        assert 0 <= (datetime.fromisoformat(term["interrupt_ack_at"]) - native_time).total_seconds() < 1
        assert DELAY_SECONDS <= elapsed < GRACE_SECONDS
    else:
        assert GRACE_SECONDS - .1 <= elapsed < GRACE_SECONDS + 2, "Missing receipt must drain only its configured grace"
        assert len(receipts) == 1 and not final["response_id"] and meter["confidence"] != "PROVIDER_CONFIRMED"
        assert accounting["usage_status"] == "UNKNOWN" and accounting["total_tokens"] is None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--case", choices=["late", "absent"], required=True)
    args = parser.parse_args()
    check(args.work.resolve(), args.case)
