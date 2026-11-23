"""Split the terminal tool item and its provider completion on the official native SSE transport."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from terminal_code_mode_probe import MODE_SOURCES, MODEL, Model, _proxy_preflight, identity, trial
from native_probe_peer import Handler
from gx_core.p0.terminal_products import TerminalProducts


class Stream(Handler):
    def do_POST(self):
        if self.path.rstrip("/") != "/v1/responses":
            return self.send_error(404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        row, ordinal = self.server.record(body)
        item = self.server.item(row, ordinal)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def event(kind, **content):
            self.wfile.write(("data: " + json.dumps({"type": kind, **content}) + "\n\n").encode())
            self.wfile.flush()

        try:
            event("response.created", response={"id": row["id"]})
            event("response.output_item.done", item=item)
            row["sent_item_at"] = time.monotonic()
            if item.get("name") == "gx_submit_product" and self.server.case != "already_complete":
                assert self.server.committed.wait(7), "native host did not deliver the tool before response.completed"
                if self.server.case == "never_completes":
                    self.server.release_extra.wait(10)
                    return
                time.sleep(0.3)
            row["completion_write_at"] = time.monotonic()
            event("response.completed", response={"id": row["id"], "usage": row["usage"]})
        except (BrokenPipeError, ConnectionResetError):
            row["connection_closed"] = True


def run(directory, binary, entry, case, expected):
    mode = entry["tool_mode"]
    model = Model(case, mode)
    model.RequestHandlerClass = Stream
    missing = case in ("never_completes", "without_grace")
    grace = 0 if case == "without_grace" else TerminalProducts.response_grace_seconds
    with patch.object(TerminalProducts, "response_grace_seconds", grace):
        rec = trial(directory, binary, entry, case, mode, expected,
                    model=model, post_commit_delay=0, missing_final=missing)
    interrupt, = rec["interrupt_writes"]
    end = rec["requests"][-1]
    committed = rec["committed_at"]
    elapsed = interrupt["t"] - committed
    rec.update(response_grace_seconds=grace, commit_to_interrupt_seconds=elapsed)
    assert end["sent_item_at"] <= committed and len(rec["requests"]) == 2
    assert rec["terminal_receipt"]["interrupt_attempts"] == 1 and not rec["tool_responses_written"]
    if case.startswith("late"):
        native, = [r for r in rec["events"] if r["type"] == "rawResponse/completed" and r["id"] == end["id"]]
        assert committed <= rec["goal_complete_write"]["t"] <= end["completion_write_at"] <= native["t"] <= interrupt["t"]
    elif case == "never_completes":
        assert "completion_write_at" not in end and 0.95 * grace <= elapsed < grace + 1
    elif case == "without_grace":
        assert interrupt["t"] < end["completion_write_at"]
    else:
        assert end["completion_write_at"] <= committed and elapsed < grace
    return rec


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("terminal-stream.json"))
    args = parser.parse_args()
    _proxy_preflight()
    binary, sources = identity(MODE_SOURCES)
    entry = next(m for m in json.loads(args.catalog.read_bytes())["models"] if m["slug"] == MODEL)
    assert entry["tool_mode"] == "code_mode_only"
    cases = (("already_complete", "ACCEPTED"), ("late_accepted", "ACCEPTED"), ("late_rejected", "REJECTED"),
             ("never_completes", "ACCEPTED"), ("without_grace", "ACCEPTED"))
    with tempfile.TemporaryDirectory(prefix="gx-native-segmented-response-") as tmp, patch(
            "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
        rows = [run(Path(tmp) / case, binary, entry, case, expected) for case, expected in cases]
    result = {"success": True, "completed_utc": datetime.now(timezone.utc).isoformat(), "identity": sources,
              "model": MODEL, "loopback": True, "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "trials": rows}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps([(r["case"], r["commit_to_interrupt_seconds"], r["final_meter"]["confidence"]) for r in rows]))
