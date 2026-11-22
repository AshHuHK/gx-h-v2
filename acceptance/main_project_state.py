"""Read the official main Root's own committed GX result using its read-only dynamic tool."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from e2e_grid import fixture_env
import e2e_responses

PACKAGE = Path(__file__).resolve().parents[1]


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    original = e2e_responses.fixture_action
    def respond(body, request_id):
        offered = [t for group in body["tools"] for t in (group.get("tools") or [group])]
        if any(t.get("name") == "gx_project_state" for t in offered):
            if not any(i.get("type") == "function_call_output" for i in body["input"]):
                return ({"type":"function_call","id":"f-"+request_id,"call_id":"c-"+request_id,
                         "name":"gx_project_state","arguments":"{}"}, {"tool":"gx_project_state","actor":"main"})
        return original(body, request_id)

    with TemporaryDirectory() as temp, patch.object(e2e_responses, "fixture_action", respond), \
            e2e_responses.local_responses(output / "requests.jsonl") as server:
        work = Path(temp)
        state = work / "state"
        env = fixture_env(work, server, shutil.which("codex"))
        def cli(*args):
            subprocess.run([str(PACKAGE/"bin/gx-p0"), *args, "--state", str(state)], env=env,
                           cwd=PACKAGE, capture_output=True, timeout=60, check=True)
        cli("start", "--demo-grid")
        cli("run", "--timeout", "50")
        with sqlite3.connect(state/"gx.db") as conn:
            data = [json.loads(row[0]) for row in conn.execute(
                "SELECT c.result_json FROM p0_root_calls c JOIN p0_roots r USING(thread_id) WHERE r.task_id IS NULL")]
        assert len(data) == 1 and data[0]["current_items"]["objective"]["version"] == 1
        assert data[0]["input_result"]["status"] == "committed"
        (output / "summary.json").write_text(json.dumps({"result":"PASS","read":data[0],
                "model":"loopback, official Codex 0.169.0, xhigh"}, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    run(p.parse_args().output)
