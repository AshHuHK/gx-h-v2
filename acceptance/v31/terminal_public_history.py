"""Read a closed official fixture session through public JSON-RPC, then attach after Goal completion."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT / "acceptance"), str(ROOT / "acceptance/v3/performance")]

from e2e_grid import fixture_env
from e2e_responses import local_responses
from gx_core.hostobs.app_server_client import AppServerClient
from run_workload import OFFICIAL_BINARY_SHA256


def verify(original):
    with closing(sqlite3.connect((original / "state/gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        thread, turn, call = db.execute(
            "SELECT thread_id,turn_id,call_id FROM p0_root_terminals WHERE reason='GX_ACCEPTED' ORDER BY rowid LIMIT 1"
        ).fetchone()
        reported = dict(
            db.execute(
                "SELECT response_id,tokens FROM p0_meter_responses WHERE thread_id=? AND turn_id=?",
                (thread, turn),
            )
        )
    binary = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    with binary.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").hexdigest() == OFFICIAL_BINARY_SHA256

    with tempfile.TemporaryDirectory(prefix="gx-v31-passive-history-") as temp:
        work = Path(temp)
        with local_responses(work / "requests.jsonl") as server:
            env = fixture_env(work, server, str(binary))
            home = work / "codex"
            # Preserve an isolated fixture identity, including the native thread-history database.
            # Only Codex reads the contents to answer the observation requests below.
            for path in (original / "codex").iterdir():
                if path.name == "sessions":
                    shutil.copytree(path, home / path.name)
                elif path.is_file() and ".sqlite" in path.name:
                    shutil.copyfile(path, home / path.name)

            events = []

            def receive(frame):
                if frame.message.get("method") in (
                    "thread/tokenUsage/updated",
                    "turn/started",
                    "turn/completed",
                ):
                    events.append(frame.message)

            argv = [str(binary), "-c", "features.goals=true", "app-server", "--listen", "stdio://"]
            client = AppServerClient(
                argv, cwd=str(work), env=env, on_notification=receive, default_timeout=8
            ).start()
            try:
                client.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "gx_audit", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                client.notify("initialized")
                t = client.request("thread/read", {"threadId": thread, "includeTurns": True})["thread"]
                (original_turn,) = [entry for entry in t["turns"] if entry["id"] == turn]
                assert original_turn["status"] == "interrupted" and t["status"]["type"] == "notLoaded"
                page = client.request(
                    "thread/items/list",
                    {"threadId": thread, "turnId": turn, "sortDirection": "asc", "limit": 64},
                )
                assert not page.get("nextCursor") and all(i["turnId"] == turn for i in page["data"])
                items = [entry["item"] for entry in page["data"]]
                (leaf,) = [item for item in items if item["id"] == call]
                assert (
                    leaf["tool"],
                    leaf["namespace"],
                    leaf["status"],
                    leaf["success"],
                    leaf["contentItems"],
                ) == ("gx_submit_product", "gx", "failed", False, [])
                goal = client.request("thread/goal/get", {"threadId": thread})["goal"]
                assert goal["status"] == "complete"
                resumed = client.request(
                    "thread/resume",
                    {
                        "threadId": thread,
                        "modelProvider": "gx_loopback",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "config": {"features.goals": True},
                    },
                )["thread"]
                until = time.monotonic() + 1
                while time.monotonic() < until:
                    client.run_until(lambda frame: True, timeout=2, poll_interval=0.05)
                (usage,) = [
                    m["params"]
                    for m in events
                    if m["method"] == "thread/tokenUsage/updated" and m["params"]["turnId"] == turn
                ]
                assert usage["threadId"] == thread and usage["tokenUsage"]["total"][
                    "totalTokens"
                ] == sum(reported.values())
                assert (
                    not any(m["method"] == "turn/started" for m in events) and server.request_count == 0
                )
                return {
                    "binary_sha256": OFFICIAL_BINARY_SHA256,
                    "fixture_state": original.name,
                    "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "official_source": "only public JSON-RPC messages; fixture native files copied while closed",
                    "thread_id": thread,
                    "turn_id": turn,
                    "call_id": call,
                    "unloaded_thread_status": t["status"],
                    "original_turn_status": original_turn["status"],
                    "public_items": [
                        {"id": v["id"], "type": v["type"], "tool": v.get("tool")} for v in items
                    ],
                    "public_terminal_leaf": {k: v for k, v in leaf.items() if k != "arguments"},
                    "goal_before_attach": goal["status"],
                    "resumed_thread": resumed["id"],
                    "resumed_turns": [{"id": v["id"], "status": v["status"]} for v in resumed["turns"]],
                    "provider_reply_tokens_saved_by_gx": reported,
                    "public_terminal_snapshot": usage,
                    "new_turns": 0,
                    "new_model_requests": server.request_count,
                }
            finally:
                client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--completed-fixture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    observed = verify(args.completed_fixture.resolve())
    args.out.write_text(json.dumps(observed, sort_keys=True, indent=2) + "\n")
