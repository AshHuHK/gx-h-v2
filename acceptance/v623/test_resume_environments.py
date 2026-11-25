"""A resumed scientific thread restores and rechecks the original empty environment before use."""
import json
import unittest
from unittest.mock import Mock

from gx_core.errors import ScopeDenied
from tests import test_v6_native


class ResumedScientificLead(unittest.TestCase):
    def setUp(self):
        test_v6_native.ScientificLead.setUp(self)
        self.roots.active_turns.clear()
        self.roots.delivery.attached = Mock()
        self.roots.delivery.reconcile = Mock()

    def host(self, *, active=False, echo=None):
        original = {"id":self.thread,"cwd":self.row["cwd"],"source":"appServer"}
        echoes = [] if echo is None else echo

        def rpc(method, args):
            self.assertEqual(args["threadId"], self.thread)
            if method == "thread/resume":
                return {"thread":{**original,"environments":[{"id":"local"}],
                                  "turns":[{"id":"in-flight","status":"inProgress"}] if active else []},
                        "approvalPolicy":"never","sandbox":{"type":"readOnly","networkAccess":False},
                        "model":self.row["model"],"modelProvider":self.row["provider"],"reasoningEffort":self.row["effort"]}
            if method == "thread/settings/update":
                self.assertEqual(args, {"threadId":self.thread,"environments":[]})
                return {}
            if method == "thread/read":
                return {"thread":{**original,"environments":echoes}}
            self.fail(f"Unexpected native request {method}")
        self.roots._request = Mock(side_effect=rpc)

    def test_resume_restores_empty_selection_and_checks_echo(self):
        self.host()
        self.roots._resume(self.thread)
        self.assertEqual([c.args[0] for c in self.roots._request.call_args_list],
                         ["thread/resume","thread/settings/update","thread/read"])
        row=self.roots._binding(self.thread)
        self.assertEqual(json.loads(row["effective_profile_json"])["research_access"]["environments"],[])
        self.assertIn(self.thread,self.roots.loaded)

    def test_refuses_attaching_when_the_host_keeps_an_environment(self):
        self.host(echo=[{"id":"local"}])
        with self.assertRaisesRegex(ScopeDenied,"did not confirm empty"):
            self.roots._resume(self.thread)
        self.roots.delivery.attached.assert_not_called()

    def test_never_reconfigures_an_inflight_turn(self):
        self.host(active=True)
        with self.assertRaisesRegex(ScopeDenied,"active Scientific Lead turn"):
            self.roots._resume(self.thread)
        self.assertEqual(self.roots._request.call_count,1)
        self.roots.delivery.attached.assert_not_called()
