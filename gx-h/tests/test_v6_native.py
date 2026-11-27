"""Authenticated Scientific Lead callbacks over real GX state, with a simulated Host transport."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.artifacts.service import ArtifactService
from gx_core.errors import ScopeDenied
from gx_core.hostobs.app_server_client import NO_TOOL_RESPONSE, ReceivedMessage
from gx_core.native_release import native_state
from gx_core.p0 import cli, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class ScientificLead(unittest.TestCase):
    def setUp(self, *, evidence_policy=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.enterContext(native_state(directory))
        self.conn = self.enterContext(closing(store.open_store(directory / "gx.db")))
        self.run = cli.start(self.conn, directory, {"project": "isolated-study", "mode": "goal",
            "objective": "Which observations would distinguish these measurements?", "capabilities": [],
            **({"evidence_policy": evidence_policy, "historical_replay": True} if evidence_policy else {})},
            run_key="native-regression")["run_id"]
        self.task = self.conn.execute("SELECT task_id FROM v6_decision_tasks WHERE run_id=?", (self.run,)).fetchone()[0]
        self.enterContext(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=temp.name))
        self.roots = Roots(self.conn, run_id=self.run, cwd=directory)
        self.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
        self.row = self.roots._claim(self.task)
        self.thread = self.roots._bind(self.row, {"id": "isolated-thread", "cwd": self.row["cwd"], "source": "appServer"})
        self.turn = "official-turn"
        self.roots.active_turns[self.thread] = self.turn
        self.conn.execute("""INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,
            human,interrupt,state,method,turn_id) VALUES(?,'start',?,'client',?,0,0,'acknowledged','turn/start',?)""",
            (self.run, self.thread, hashlib.sha256(self.row["brief"].encode()).hexdigest(), self.turn))
        self.body = {"mode":"resolve", "rationale":"Wait for an authorized measurement before interpreting.",
                     "evidence_refs":[], "reconsider_when":[{"event":"evidence_added","subject_ref":"run"}],"ops":[]}

    def params(self, call="choose", tool=v6_native.DECIDE, arguments=None):
        return {"threadId": self.thread, "turnId": self.turn, "callId": call, "tool": tool,
                "arguments": {"decision": self.body} if arguments is None else arguments,
                **({"namespace": "gx"} if tool == v6_native.DECIDE else {})}

    def test_isolated_roots_only_present_authorized_sources_and_tools(self):
        original = ArtifactService(self.conn).put(run_id=self.run, kind="reviewer_only",
            media_type="application/json", content='{"reviewer_internal_answer": 42}',
            origin="HOST_PUT", put_by="reviewer").artifact_ref
        self.assertEqual(sorted(Path(self.row["cwd"]).iterdir()), [])
        new = self.roots._settings(self.row, new=True)
        self.assertEqual(new["environments"], [])
        self.assertEqual(new["sandbox"], "read-only")
        self.assertIs(new["config"]["features.goals"], False)
        self.assertIs(new["config"]["tools.experimental_request_user_input.enabled"], False)
        with self.assertRaisesRegex(ScopeDenied, "zero attached execution environments"):
            self.roots._verify_settings(self.row, {"thread": {"environments": [{"id":"local"}]}}, new)
        self.assertEqual(new["config"]["web_search"], "disabled")
        for key in ("features.shell_tool", "features.unified_exec", "features.multi_agent", "features.memories",
                    "features.view_image", "features.apps", "features.plugins", "skills.include_instructions"):
            self.assertIs(new["config"][key], False, key)
        self.assertEqual({t["name"] for t in new["dynamicTools"]},
                         {"gx", "gx_read_artifact", "gx_task_context", "gx_stage_claim"})
        self.assertNotIn(original, self.row["brief"])
        packet = self.roots._call(self.params("context","gx_task_context",{}))
        self.assertIn("available", packet.pop("current_budget"))
        self.assertEqual(v6_native.brief(self.row), packet)
        self.assertNotIn("reviewer_internal_answer", json.dumps(packet))
        for forbidden, arguments in (("gx_read_artifact", {"ref": original}), ("gx_job_submit", {}),
                                     ("gx_propose_intent", {}), ("gx_submit_product", {})):
            with self.subTest(tool=forbidden), self.assertRaises(ScopeDenied):
                self.roots._call(self.params("forbidden", forbidden, arguments))

    def test_refused_candidate_is_replayable_and_can_be_corrected_in_the_same_turn(self):
        bad = self.params("unsatisfied", arguments={"decision": {**self.body, "mode": "amend"}})
        for _ in range(2):
            answer = self.roots._callback(ReceivedMessage(1, {"method":"item/tool/call","params":bad}))
            self.assertFalse(answer["success"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v6_decisions").fetchone()[0], 0)
        candidate = self.conn.execute("SELECT request_json,result_json FROM p0_v6_native_calls "
                                      "JOIN p0_root_calls USING(thread_id,call_id)").fetchone()
        self.assertEqual(json.loads(candidate["request_json"]), bad)
        self.assertIn("decision_refusal", json.loads(candidate["result_json"]))
        result = self.roots._call(self.params())
        self.assertEqual(result["outcome"], "maintained")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p0_v6_native_calls").fetchone()[0], 2)

    def test_live_identity_and_replay_after_terminal_preserve_one_decision(self):
        request = self.params()
        for changed in ({"threadId":"unbound-child"}, {"turnId":"superseded"}, {"namespace":None}):
            with self.subTest(changed=changed), self.assertRaises(ScopeDenied):
                self.roots._call({**request, **changed})
        with patch.object(self.roots, "_request", side_effect=AssertionError("nested native RPC")):
            self.assertIs(self.roots._callback(ReceivedMessage(1, {"method":"item/tool/call", "params":request})),
                          NO_TOOL_RESPONSE)
        self.roots.active_turns.clear()
        receipt = self.roots._call(request)
        self.assertEqual(receipt["outcome"], "maintained")
        self.assertEqual(receipt["task_status"], "ACCEPTED")
        self.assertIsNone(receipt["result_head"])
        with self.assertRaises(ScopeDenied):
            self.roots._call({**request, "arguments":{"decision":{**self.body, "rationale":"different content"}}})
        with self.assertRaises(ScopeDenied):
            self.roots._call({**request, "callId":"a-new-request"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v6_decisions").fetchone()[0],1)
        raw = self.conn.execute("SELECT request_json FROM p0_v6_native_calls").fetchone()
        self.assertEqual(json.loads(raw[0]), request)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0],0)
        self.assertEqual(self.conn.execute("SELECT turn_id FROM p0_root_terminals").fetchone()[0], self.turn)


if __name__ == "__main__":
    unittest.main()
