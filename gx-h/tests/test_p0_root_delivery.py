"""Delivery fault tests using disk GX budgets and the pinned App Server wire shapes."""
import tempfile
import unittest
from pathlib import Path

from gx_core import budgets
from gx_core.errors import HoldActive, IdempotencyConflict, OutcomeUnknown, ScopeDenied
from gx_core.hostobs.app_server_client import AppServerResponseError, ReceivedMessage
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import records, store


class Peer:
    def __init__(self, conn, run):
        self.conn, self.run_id = conn, run
        self.token_budget = 100
        self.turns = []
        self.sent = []
        self.drop = False
        self.race = False
        self.task = None

    def _binding(self, thread_id):
        return ({"run_id": self.run_id, **(self.task or {})} if thread_id == "root" else None)

    def _request(self, method, params):
        assert not self.conn.in_transaction
        active = next((t for t in self.turns if t["status"] == "inProgress"), None)
        if method == "thread/read":
            return {"thread": {"id": "root", "status": {"type": "active" if active else "idle"}}}
        if method == "thread/turns/list":
            return {"data": self.turns[::-1], "nextCursor": None}
        self.sent.append((method, params))
        if method == "turn/interrupt":
            self.assert_active(active, params["turnId"])
            active["status"] = "interrupted"
            return {}
        if method == "turn/steer":
            self.assert_active(active, params["expectedTurnId"])
            if self.race:
                active["status"] = "completed"
                raise AppServerResponseError(ReceivedMessage(4, {"id": 2, "error": {
                    "code": -32600, "message": "no active turn to steer"}}))
        else:
            assert method == "turn/start" and not active
            active = {"id": f"turn-{len(self.turns)+1}", "status": "inProgress", "items": []}
            self.turns.append(active)
        active["items"].append({"type": "userMessage", "clientId": params["clientUserMessageId"],
                                "content": params["input"]})
        if self.drop:
            raise EOFError("server accepted before client received the reply")
        return {"turn": active} if method == "turn/start" else {"turnId": active["id"]}

    @staticmethod
    def assert_active(active, expected):
        assert active["id"] == expected

    def _goal_for_budget(self, thread_id, hold):
        assert thread_id == "root" and hold == self.token_budget


class RootDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.conn = store.open_store(Path(temporary.name) / "gx.db")
        self.addCleanup(self.conn.close)
        sql = Path(__file__).resolve().parents[1] / "gx_core/p0/root_delivery.sql"
        self.conn.executescript(sql.read_text())
        self.run = records.create_run(self.conn, objective="Synthetic measurements", project_ref="p0", actor="test")
        self.ref = budgets.create_root(self.conn, run_id=self.run, profile_name="p0", actor="test",
                                       limits={"tokens": 300, "api_calls": 5})
        self.conn.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (self.ref, self.run))
        self.peer = Peer(self.conn, self.run)
        self.delivery = RootDelivery(self.peer)

    def budget(self):
        return budgets.state(self.conn, self.ref)

    def finish(self, turn_id, *totals):
        for n in totals:
            self.delivery.observe("thread/tokenUsage/updated", {"threadId": "root", "turnId": turn_id,
                                  "tokenUsage": {"total": {"totalTokens": n}, "last": {"totalTokens": 7}}})
        turn = next(t for t in self.peer.turns if t["id"] == turn_id)
        turn["status"] = "completed"
        self.delivery.observe("turn/completed", {"threadId": "root", "turn": turn})

    def test_cumulative_usage_duplicates_and_idle_second_operator_turn(self):
        send = self.delivery.deliver
        first = send("root", "measure", event_id="before", human=True, message_id="operator-1")
        self.assertEqual(self.budget()["reserved"], {"api_calls": 1, "tokens": 100})
        with self.assertRaises(HoldActive):
            send("root", "confirm", event_id="after", human=True, message_id="operator-2")
        self.finish(first["turn_id"], 10, 22)
        second = send("root", "confirm", event_id="after", human=True, message_id="operator-2")
        self.finish(second["turn_id"], 41)
        self.assertEqual(second, send("root", "confirm", event_id="after", human=True, message_id="operator-2"))
        self.assertEqual(len(self.peer.sent), 2)
        self.assertEqual(self.budget()["spent"], {"tokens": 41, "api_calls": 2})
        with self.assertRaises(IdempotencyConflict):
            send("root", "different", event_id="after", human=True, message_id="operator-2")

    def test_ack_loss_requires_original_client_id_and_exact_message(self):
        self.peer.drop = True
        with self.assertRaises(EOFError):
            self.delivery.deliver("root", "human operator input", event_id="1", human=True, message_id="real")
        self.delivery = RootDelivery(self.peer)
        original = self.peer.turns[0]["items"][0]
        for altered in [
            {**original, "type": "agentMessage"},
            {**original, "clientId": "wrong"},
            {**original, "content": [{"type": "text", "text": "different human input"}]},
        ]:
            self.peer.turns[0]["items"] = [altered]
            with self.assertRaises(OutcomeUnknown):
                self.delivery.deliver("root", "human operator input", event_id="1", human=True, message_id="real")
        self.peer.turns[0]["items"] = [original]
        recovered = self.delivery.deliver("root", "human operator input", event_id="1", human=True, message_id="real")
        self.assertEqual(recovered["turn_id"], "turn-1")
        self.assertEqual(len(self.peer.sent), 1)
        self.finish("turn-1")
        self.assertEqual(self.budget()["reserved"], {"api_calls": 1, "tokens": 100})
        self.finish("turn-1", 16)
        self.assertEqual(self.budget()["spent"], {"api_calls": 1, "tokens": 16})

    def test_steer_uses_observed_turn_then_falls_back_after_definite_idle_race(self):
        send = self.delivery.deliver
        first = send("root", "first", event_id="one")
        steer = send("root", "project addition", event_id="two")
        self.assertEqual((steer["method"], steer["turn_id"]), ("turn/steer", first["turn_id"]))
        self.assertEqual(self.peer.sent[-1][1]["expectedTurnId"], first["turn_id"])
        self.peer.race = True
        with self.assertRaises(HoldActive):
            send("root", "new evaluation", event_id="three")
        self.finish(first["turn_id"], 20)
        third = send("root", "new evaluation", event_id="three")
        self.assertEqual(third["method"], "turn/start")
        self.assertEqual(len(self.budget()["reservations"]), 2)
        self.assertEqual(len(self.peer.sent), 4)

    def test_interrupt_requires_frozen_attempt_and_never_targets_a_new_turn(self):
        t = records.create_task(self.conn, run_id=self.run, kind="research_round",
                                role="evidence_reviewer", actor="test")
        lease = store.issue_lease(self.conn, task_id=t, owner="root", worker_role="evidence_reviewer",
                                  seconds=600, attempt=1)
        self.conn.execute("UPDATE tasks SET attempt=1, lease_id=?,status='CLAIMED' WHERE task_id=?",
                          (lease["lease_id"], t))
        self.peer.task = {"task_id": t, "attempt": 1, "lease_id": lease["lease_id"]}
        self.delivery.deliver("root", "evaluate", event_id="launch")
        with self.assertRaises(ScopeDenied):
            self.delivery.deliver("root", "", event_id="cancel", interrupt=True)
        store.release_lease(self.conn, lease["lease_id"])
        dropped = self.delivery.deliver("root", "", event_id="cancel", interrupt=True)
        self.assertEqual(dropped["turn_id"], "turn-1")
        self.delivery.deliver("root", "", event_id="cancel", interrupt=True)
        self.assertEqual([method for method, _ in self.peer.sent], ["turn/start", "turn/interrupt"])


if __name__ == "__main__":
    unittest.main()
