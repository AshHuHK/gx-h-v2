"""An acknowledged operator turn can finish and settle just before the GX driver stops."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.p0 import intent
from gx_core.p0.driver import Driver, enqueue
from gx_core.p0.root_delivery import RootDelivery
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from tests.test_p0_root_delivery import Peer


class ReceiptPeer(Peer):
    human_turn = Roots.human_turn

    def ensure_main(self):
        return "root"


class ColdOperatorInput(unittest.TestCase):
    def test_settled_terminal_input_is_consumed_once_on_idle_reopen(self):
        for state in ("PAUSED", "COMPLETED"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "gx.db"
                with closing(store.open_store(path)) as conn:
                    budget = budgets.create_root(conn, run_id=None, profile_name="p0", actor="test")
                    run = records.create_run(conn, objective="Review observed coordinates", project_ref="coordinates",
                        actor="test", budget_ref=budget, inputs={"inputs": {"p0": True, "p0_spec": {}}})
                    for status in ("AUDITING", "PLANNING", "ACTIVE"):
                        store.transition(conn, machine="run", subject_id=run, to_state=status, actor="test")
                    peer = ReceiptPeer(conn, run)
                    peer.delivery = RootDelivery(peer)
                    peer.active_turns = {}
                    warm = Driver(conn, Path(tmp), run)
                    warm._native = peer
                    text = "Set token budget to 90."
                    mid = enqueue(conn, run, {"kind": "text", "text": text}, text)
                    warm.messages()
                    turn, = peer.turns
                    envelope = {"threadId": "root", "turnId": turn["id"]}
                    peer.delivery.observe("turn/started", {"threadId": "root", "turn": {"id": turn["id"]}})
                    peer.delivery.observe("rawResponse/completed", {**envelope, "responseId": "response-1",
                                                                    "usage": {"totalTokens": 10}})
                    peer.delivery.observe("thread/tokenUsage/updated", {**envelope,
                        "tokenUsage": {"total": {"totalTokens": 10}}})
                    turn["status"] = "completed"
                    peer.delivery.observe("turn/completed", {"threadId": "root", "turn": turn})
                    store.transition(conn, machine="run", subject_id=run, to_state=state, actor="test")
                    self.assertEqual(budgets.state(conn, budget)["spent"], {"tokens": 10, "api_calls": 1})
                    self.assertIsNone(conn.execute("SELECT consumed_at FROM p0_human_inputs WHERE message_id=?",
                                                  (mid,)).fetchone()[0])
                    warm.close()

                with closing(store.open_store(path)) as db:
                    cold = Driver(db, Path(tmp), run)
                    peer.conn = db
                    peer.delivery = RootDelivery(peer)

                    def attach(driver):
                        # The cold driver can apply the terminal turn's GX effect before reading its reply.
                        self.assertIsNotNone(db.execute("SELECT consumed_at FROM p0_human_inputs WHERE message_id=?",
                                                       (mid,)).fetchone()[0])
                        driver._native = peer
                        return peer

                    with patch.object(Driver, "native", property(attach)):
                        self.assertEqual(cold.drive(2)["status"], state)
                        self.assertEqual(cold.drive(2)["status"], state)
                    item = intent.read_item(db, run, "tokens")
                    self.assertEqual((item["version"], item["value"]["max"]), (1, 90))
                    saved = db.execute("SELECT result_json FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
                    self.assertEqual(json.loads(saved[0])["status"], "committed")
                    self.assertEqual([m for m, _ in peer.sent], ["turn/start"])
                    self.assertEqual(budgets.state(db, budget)["spent"], {"tokens": 10, "api_calls": 1})
                    cold.close()
