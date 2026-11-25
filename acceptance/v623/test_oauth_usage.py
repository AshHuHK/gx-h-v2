"""Offline OAuth usage reconciliation against GX records and sanitized official-shaped rollout fixtures."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from acceptance.v622.test_close import CloseStudy
from acceptance.v623.oauth import usage


def account(slot, state):
    path = slot / "oauth-account.json"
    path.write_text(json.dumps({"status":"READY" if state == "INCLUDED_AVAILABLE" else "BLOCKED",
                                "subscription_usage_state":state, "actual_provider_usd":None}))


def rollout(slot, thread, actor, *, turn, total, parent=None, owner=None, end=True):
    home = slot / "codex" / "sessions"
    home.mkdir(parents=True, exist_ok=True)
    path = home / ("rollout-fixture-" + thread + ".jsonl")
    source = {"subagent":{"thread_spawn":{"parent_thread_id":parent}}} if parent else "cli"
    header = {"type":"session_meta","payload":{"id":thread, "model_provider":actor["provider"], "source":source}}
    contexts = [header, {"type":"turn_context", "payload":{"turn_id":turn, "model":actor["model"],
                "effort":actor["effort"], "root_turn_id":owner or turn}},
                {"type":"response_item", "payload":{"content":[{"text":"PRIVATE-MATERIAL-NEVER-EXPORT"}]}}]
    if total is not None:
        contexts.append({"type":"event_msg", "payload":{"type":"token_count",
                         "info":{"total_token_usage":{"total_tokens":total}}}})
    if end:
        contexts.append({"type":"event_msg","payload":{"type":"task_complete","turn_id":turn}})
    path.write_text("".join(json.dumps(x) + "\n" for x in contexts))
    return path


class OauthUsage(unittest.TestCase):
    def setUp(self):
        self.slot = Path(self.enterContext(tempfile.TemporaryDirectory())) / "run"
        self.s = self.enterContext(CloseStudy(self.slot))
        self.lead = self.s.lead
        self.binding = self.s.roots._binding(self.lead.thread)
        self.delivery = self.s.roots.delivery
        self.rid = self.s.run_id
        account(self.slot, "INCLUDED_AVAILABLE")
        self.delivery.observe("turn/started", {"threadId":self.lead.thread, "turn":{"id":self.lead.turn}})

    def child(self):
        tid, t = "child-fixture", "child-turn"
        parent = self.lead.thread
        self.delivery.observe_descendant("item/completed", {"threadId":parent, "turnId":self.lead.turn,
                "item":{"id":"spawn","type":"subAgentActivity","kind":"started","agentThreadId":tid}})
        metadata = {"id":tid,"parentThreadId":parent,"sessionId":parent,
                    "source":{"subAgent":{"thread_spawn":{"parent_thread_id":parent}}},
                    "turns":[]}
        self.delivery.verify_descendants({tid:metadata})
        self.delivery.observe_descendant("turn/started", {"threadId":tid,"turn":{"id":t,"rootTurnId":self.lead.turn}})
        return tid, t

    def finish(self, thread, turn, count, child=False):
        observe = self.delivery.observe_descendant if child else self.delivery.observe
        observe("rawResponse/completed", {"threadId":thread,"turnId":turn,"responseId":"response-" + turn,
                                          "usage":{"totalTokens":count}})
        observe("thread/tokenUsage/updated", {"threadId":thread,"turnId":turn,
                                              "tokenUsage":{"total":{"totalTokens":count}}})
        observe("turn/completed", {"threadId":thread,"turn":{"id":turn,"status":"completed",
                                      "rootTurnId": self.lead.turn,"items":[],"itemsView":"full"}})
        return rollout(self.slot, thread, self.binding, turn=turn, total=count,
                       parent=self.lead.thread if child else None, owner=self.lead.turn if child else None)

    def reconcile(self):
        return usage.reconcile(self.slot, self.rid)

    def test_real_ledger_parent_child_late_terminal_then_cold_replay(self):
        child, child_turn = self.child()
        self.finish(self.lead.thread, self.lead.turn, 19)
        before = self.reconcile()
        self.assertNotEqual(before["status"], "SETTLED")
        self.assertFalse(before["can_continue"])
        self.assertIsNone(before["total_tokens"])
        self.finish(child, child_turn, 7, True)
        result = self.reconcile()
        self.assertEqual((result["status"], result["total_tokens"], result["can_continue"]), ("SETTLED", 26, True), result)
        self.assertEqual(len(result["turns"]), 1)
        self.assertEqual(len(result["turns"][0]["descendants"]), 1)
        self.assertIsNone(result["provider_usd"])
        out = self.slot / "prior.json"
        code = ("import json,os,signal,sys;from acceptance.v623.oauth.usage import reconcile;"
                "from pathlib import Path;Path(sys.argv[3]).write_text(json.dumps(reconcile(sys.argv[1],sys.argv[2])));"
                "os.kill(os.getpid(), signal.SIGKILL)")
        root = Path(__file__).resolve().parents[2]
        killed = subprocess.run([sys.executable, "-B", "-c", code, str(self.slot), self.rid, str(out)],
                                cwd=root, env={**os.environ, "PYTHONPATH":str(root)+os.pathsep+str(root/"gx-h")},timeout=15)
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        self.assertEqual(json.loads(out.read_text()), result)
        self.assertEqual(self.reconcile(), result)
        self.assertNotIn("PRIVATE-MATERIAL", json.dumps(result))

    def test_explicit_fee_stop_unknown_account_and_missing_usage_never_zero(self):
        pending = self.reconcile()
        self.assertIn(pending["status"], {"PENDING", "UNKNOWN"})
        self.assertIsNone(pending["total_tokens"])
        self.assertFalse(pending["can_continue"])
        outfile = self.finish(self.lead.thread, self.lead.turn, 11)
        account(self.slot, "ADDITIONAL_CHARGE")
        charged = self.reconcile()
        self.assertEqual((charged["status"], charged["total_tokens"]), ("ADDITIONAL_CHARGE", 11))
        self.assertIsNone(charged["provider_usd"])
        self.assertFalse(charged["can_continue"])
        account(self.slot, "UNKNOWN")
        self.assertFalse(self.reconcile()["can_continue"])
        account(self.slot, "INCLUDED_AVAILABLE")
        outfile.unlink()
        unknown = self.reconcile()
        self.assertEqual(unknown["status"], "UNKNOWN")
        self.assertIsNone(unknown["total_tokens"])
        self.assertIsNone(unknown["confirmed_tokens"])

    def test_stray_child_and_receipt_conflict_hold_the_original_request(self):
        path = self.finish(self.lead.thread, self.lead.turn, 9)
        extra = rollout(self.slot, "unregistered-child", self.binding, turn="unseen", parent=self.lead.thread,
                        owner=self.lead.turn, total=1)
        found = self.reconcile()
        self.assertEqual(found["status"], "UNKNOWN")
        self.assertFalse(found["can_continue"])
        self.assertTrue(any(r.get("reason") == "UNDISCOVERED_OFFICIAL_CHILD" for r in found["unresolved"]))
        extra.unlink()
        path.write_text(path.read_text().replace('"total_tokens": 9', '"total_tokens": 90'))
        self.assertEqual(self.reconcile()["status"], "UNKNOWN")
        before = self.s.rows("SELECT count(*) AS n FROM p0_root_deliveries")
        self.assertIsNone(self.reconcile()["total_tokens"])
        self.assertEqual(self.s.rows("SELECT count(*) AS n FROM p0_root_deliveries"), before)
