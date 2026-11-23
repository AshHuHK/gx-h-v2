"""Check that independent proof of an interrupted Turn releases its transport recovery slot."""

import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))

from gx_core.hostobs.app_server_client import AppServerClient, AppServerTimeout
from gx_core.p0.terminal_products import TerminalProducts
from gx_core.persistence import store


class DelayedAcknowledgements:
    """The scripted peer applies every original interrupt immediately; only ACK delivery is delayed."""

    def __init__(self):
        self.inbound = deque()
        self.delayed = []
        self.interrupted = {}
        self.delay = True
        self.drop_sends = 0
        self.undelivered = []

    def write_line(self, line, timeout):
        request = json.loads(line)
        method, params = request["method"], request["params"]
        thread = params["threadId"]
        if method == "thread/goal/get":
            result = {"goal": {"status": "complete"}}
        elif method == "thread/turns/list":
            result = {
                "data": [
                    {
                        "id": f"turn-{thread}",
                        "status": ("interrupted" if thread in self.interrupted else "inProgress"),
                    }
                ],
                "nextCursor": None,
            }
        elif method == "turn/interrupt":
            assert params["turnId"] == f"turn-{thread}"
            if self.drop_sends:
                self.drop_sends -= 1
                self.undelivered.append(request["id"])
                return
            assert thread not in self.interrupted, f"duplicate interrupt for {thread}"
            self.interrupted[thread] = request["id"]
            result = {}
        else:
            raise AssertionError(method)

        response = (json.dumps({"id": request["id"], "result": result}) + "\n").encode()
        if self.delay and method == "turn/interrupt":
            self.delayed.append(response)
        else:
            self.inbound.append(response)

    def read_line(self, timeout):
        if not self.inbound:
            raise AppServerTimeout("simulated lost transport receipt")
        return self.inbound.popleft()

    def close(self):
        pass


def run():
    now = [100.0]
    with sqlite3.connect(":memory:", isolation_level=None) as conn, patch(
        "gx_core.p0.terminal_products.time", SimpleNamespace(monotonic=lambda: now[0])
    ):
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE p0_roots(thread_id TEXT PRIMARY KEY,run_id TEXT,goal_desired TEXT);"
            "CREATE TABLE p0_root_turn_usage(thread_id TEXT,turn_id TEXT,terminal_status TEXT);"
        )
        conn.executescript((ROOT / "gx-h/gx_core/p0/zz_v31_terminal_products.sql").read_text())
        wire = DelayedAcknowledgements()
        client = AppServerClient(transport=wire).start()

        def observe(method, params):
            assert method == "turn/completed" and params["turn"]["status"] == "interrupted"
            conn.execute(
                "INSERT INTO p0_root_turn_usage VALUES(?,?,?)",
                (params["threadId"], params["turn"]["id"], params["turn"]["status"]),
            )

        run_id = "scope-a"
        owner = SimpleNamespace(
            conn=conn,
            run_id=run_id,
            run={"inputs_json": "{}"},
            active_turns={},
            client=client,
            _request=client.request,
            delivery=SimpleNamespace(observe=observe, reconcile=lambda thread: None),
        )
        terminal = TerminalProducts(owner)
        terminal.retry_seconds = 0
        rows = []

        def trial(thread, passes=3):
            turn = f"turn-{thread}"
            owner.active_turns[thread] = turn
            with store.transaction(conn):
                conn.execute("INSERT INTO p0_roots VALUES(?,?,NULL)", (thread, run_id))
                terminal.record(thread, turn, f"submit-{thread}", "GX_ACCEPTED")
            for _ in range(passes):
                terminal.flush()
                now[0] += 30
            closed = conn.execute(
                "SELECT terminal_status FROM p0_root_turn_usage WHERE thread_id=? AND turn_id=?",
                (thread, turn),
            ).fetchone()
            snapshot = {
                "thread": thread,
                "sent": thread in wire.interrupted,
                "pending": (thread, turn) in terminal.due,
                "native_history": closed[0] if closed else None,
                "outstanding_request_ids": list(client.pending_ids),
            }
            rows.append(snapshot)
            assert (
                snapshot["sent"]
                and not snapshot["pending"]
                and snapshot["native_history"] == "interrupted"
            ), snapshot

        # Eight lost transport ACKs saturated the original global per-method limit even after all
        # eight Turns had been independently proved terminal and removed from the delivery queue.
        for index in range(1, 9):
            trial(str(index))
        wire.delay = False
        trial("9")
        assert len(wire.delayed) == 8
        # These retired responses can arrive in reverse order on a healthy stream.
        # Verify they cannot be assigned to the active request, emitted as tool responses, or close the client.
        wire.inbound.extend(reversed(wire.delayed))
        trial("10")
        assert not client.pending_ids and not wire.inbound
        # After two temporarily undelivered sends, official history still shows only this
        # exact Turn active. The first healthy retry must be allowed without restarting GX.
        wire.drop_sends = 2
        trial("11", passes=20)
        assert len(wire.undelivered) == 2 and not client.pending_ids
        sources = (
            "acceptance/v31/terminal_late_receipts.py",
            "gx-h/gx_core/hostobs/app_server_client.py",
            "gx-h/gx_core/p0/terminal_products.py",
        )
        return {
            "command": "python -B acceptance/v31/terminal_late_receipts.py --output acceptance/v31/terminal-late-receipts.json",
            "source_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "worktree_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
            "simulated_seconds_per_flush": 30,
            "input": "in-memory JSONL peer with the production AppServerClient and TerminalProducts",
            "initial_dropped_interrupt_acks": len(wire.delayed),
            "temporarily_undelivered_sends_then_recovered": len(wire.undelivered),
            "history_confirmed_turns": len(rows),
            "healthy_successors": ["9", "10"],
            "late_responses_reinjected_reverse_order": len(wire.delayed),
            "pending_requests_at_end": list(client.pending_ids),
            "turns": rows,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("PASS", args.output)
