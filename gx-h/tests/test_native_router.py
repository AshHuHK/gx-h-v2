"""Replay native App Server frames against the real router and SQLite dispatch."""
from collections import deque
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest

from gx_core.hostobs import app_server_client as app
from gx_core.hostobs import host_policy, native_app_server
from gx_core.native_release import native_state
from gx_core.persistence import store


ROOT = "00000000-0000-4000-8000-000000000071"
TURN = "turn-router-test"
PROJECT = "native-router-project"


class ProtocolPeer:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.written = []

    def read_line(self, timeout):
        if not self.messages:
            raise app.AppServerDisconnected("End of test stream")
        return json.dumps(self.messages.popleft()).encode() + b"\n"

    def write_line(self, data, timeout):
        self.written.append(json.loads(data))

    def close(self):
        pass


class RouterFixture(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="gx-router-"))
        self.state = stack.enter_context(native_state(Path(directory) / "state", env={}))
        self.conn = store.open_store(self.state.db_path)
        stack.callback(self.conn.close)

    def run_session(self, *, arguments=None, prompt="Compare the two windows.", profile="native_core",
                    project_continuation=False):
        policy, _ = host_policy.native_codex_policy(self.state.directory)
        model = policy["pinned_model"]["id"]
        effort = policy["native_coordinator_observation"]["default_effort"]
        thread = {"id": ROOT, "sessionId": ROOT, "parentThreadId": None, "forkedFromId": None,
                  "cliVersion": "0.169.0", "model": model, "reasoningEffort": effort}
        arguments = arguments or {
            "objective": "Summarize both queue windows.", "project_ref": PROJECT,
            "budget_profile": "cpu_only", "workflow": {"id": "module_collaboration", "version": "1"},
            "inputs": {"modules": [{"key": "a", "question": "How did window A change?"},
                                  {"key": "b", "question": "How did window B change?"}]},
            "idempotency_key": "router-test-start",
        }
        peer = ProtocolPeer([
            {"id": 1, "result": {"userAgent": "test-codex"}},
            {"id": 2, "result": {"config": {"mcp_servers": {},
                "experimental_thread_store": {"type": "local"}}, "origins": {}}},
            {"method": "thread/started", "params": {"thread": thread}},
            {"id": 3, "result": {"thread": thread, "model": model, "reasoningEffort": effort}},
            {"method": "turn/started", "params": {"threadId": ROOT, "turn": {"id": TURN, "status": "inProgress"}}},
            {"id": 4, "result": {"turn": {"id": TURN}}},
            {"id": 901, "method": "item/tool/call", "params": {"threadId": ROOT, "turnId": TURN,
                "callId": "call-start", "tool": "gx_run_start", "arguments": arguments}},
            {"method": "item/completed", "params": {"threadId": ROOT,
                 "item": {"type": "dynamicToolCall", "id": "call-start"}}},
            {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": TURN, "status": "completed"}}},
        ])

        def client(**options):
            return app.AppServerClient(transport=peer, on_notification=options["on_notification"],
                on_server_request=options["on_server_request"], default_timeout=1,
                max_inbound_message_bytes=options["max_inbound_message_bytes"])

        session = native_app_server.run_session(prompt, state=self.state, conn=self.conn,
            project_ref=arguments["project_ref"], max_wall_seconds=10, max_turns=1, max_workers=2,
            tool_policy={"allow_mutations": True, "max_callbacks": 1, "tool_profile": profile},
            project_continuation=project_continuation, client_factory=client)
        reply, = [row["result"] for row in peer.written if row.get("id") == 901]
        result = json.loads(reply["contentItems"][0]["text"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(session["status"], "COMPLETED", session)
        self.assertEqual(session["GX_calls"], 1)
        return peer.written, result["run_id"], session["resumed"]


class NativeRouterResume(RouterFixture):
    def test_same_root_reopens_through_thread_resume_and_reuses_run(self):
        opened, first_run, first_resumed = self.run_session()
        self.assertFalse(first_resumed)
        start, = [row for row in opened if row.get("method") == "thread/start"]
        names = {tool["name"] for tool in start["params"]["dynamicTools"]}
        self.assertIn("gx_run_start", names)
        resumed, same_run, second_resumed = self.run_session()
        self.assertTrue(second_resumed)
        self.assertEqual(same_run, first_run)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertFalse(any(row.get("method") == "thread/start" for row in resumed))
        resume, = [row for row in resumed if row.get("method") == "thread/resume"]
        self.assertEqual(resume["params"]["threadId"], ROOT)
        self.assertIs(resume["params"]["excludeTurns"], True)
        self.assertNotIn("dynamicTools", resume["params"])
