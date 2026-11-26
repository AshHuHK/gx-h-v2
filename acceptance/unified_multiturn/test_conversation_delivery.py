"""A1. Real GX CLI, SQLite, native lock and delivery journal with a controlled App Server peer."""
from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy, close_decision
from gx_core.native_release import native_state
from gx_core.p0 import cli, conversation, workspace
from gx_core.p0.driver import Driver
from gx_core.p0.root_delivery import RootDelivery
from tests.test_p0_root_delivery import Peer


class MainPeer(Peer):
    """Shares the official wire history across driver lifetimes, but forgets loaded threads."""
    def __init__(self, conn, *, run_id, history, requests, **_):
        super().__init__(conn, run_id)
        self.turns, self.sent = history, requests
        self.delivery = RootDelivery(self)
        self.active_turns = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def ensure_main(self):
        return "root"

    def _resume(self, tid):
        self.active_turns = {tid:t["id"] for t in self.turns if t["status"] == "inProgress"}

    def human_turn(self, text, *, before_start, on_started, message_id):
        before_start("root", message_id)
        turn = self.delivery.deliver("root", text, event_id="human:"+message_id, human=True, message_id=message_id)
        on_started("root", turn["turn_id"], message_id)
        self._resume("root")
        return turn["turn_id"]

    def poll(self, _):
        for turn in self.turns:
            if turn["status"] == "inProgress":
                mid = turn["items"][0]["clientId"]
                turn["items"].append({"type":"agentMessage","text":"Reply from the controlled host to "+mid})
                turn["status"] = "completed"
                self.delivery.observe("thread/tokenUsage/updated", {"threadId":"root","turnId":turn["id"],
                                    "tokenUsage":{"total":{"totalTokens":10*len(self.turns)}}})
                self.delivery.observe("turn/completed", {"threadId":"root","turn":turn})
                self.active_turns.clear()
                return True
        return False


class ConversationDelivery(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))/"state"
        self.turns, self.calls = [], []
        self.enterContext(patch.dict(os.environ, {"GX_CODEX_OFFLINE":"0"}))
        self.enterContext(patch("gx_core.p0.driver.Roots", lambda conn, **kw:
                           MainPeer(conn, **kw, history=self.turns, requests=self.calls)))

    def cmd(self, name, *args):
        out = StringIO()
        with redirect_stdout(out):
            status = cli.main([name,"--state",str(self.state),*args])
        self.assertEqual(status, 0, out.getvalue())
        return json.loads(out.getvalue())

    def test_busy_cold_driver_inbox_and_paused_readback(self):
        rid = self.cmd("start","--goal","Track heat diffusion.","--run-key","heat")["run_id"]
        self.cmd("stop","--run-id",rid)
        with native_state(self.state), closing(cli.existing_db(self.state, readonly=False)) as db:
            a = conversation.send(db,rid,"What did we observe?","first")
            old = Driver(db,self.state,rid)
            try:
                old.messages()  # Original host accepted turn. GX exits before the agent response.
            finally:
                old.close()
            pending = self.cmd("message","--run-id",rid,"--message-id","later","--reply-to","first",
                               "--text","And what is uncertain?")
            self.assertEqual(pending["status"], "queued_for_active_driver")
            self.assertEqual(conversation.pending(db,rid),["first","later"])
        # An independent caller resumes the original busy turn, then admits the queued one.
        receipt = self.cmd("message","--run-id",rid,"--message-id","later","--reply-to","first",
                           "--text","And what is uncertain?")
        with closing(cli.existing_db(self.state,readonly=True)) as saved:
            detail = [tuple(r) for r in saved.execute("SELECT state,turn_id,budget_status,terminal_status,confidence FROM p0_meter_native_turns")]
        self.assertEqual(receipt["status"], "answered",(receipt,detail,self.calls,self.turns))
        view = self.cmd("conversation","--run-id",rid)
        self.assertEqual(view["conversation_id"],a["conversation_id"])
        self.assertEqual([m["reply"]["turn_id"] for m in view["messages"]],["turn-1","turn-2"])
        self.assertEqual(view["messages"][1]["reply_to"],"first")
        self.cmd("message","--run-id",rid,"--message-id","first","--text","What did we observe?")
        self.assertEqual(len(self.calls),2)
        with closing(cli.existing_db(self.state,readonly=True)) as db:
            self.assertEqual(db.execute("SELECT status FROM runs WHERE run_id=?",(rid,)).fetchone()[0],"PAUSED")
            self.assertEqual(conversation.pending(db,rid),[])
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE run_id=?",(rid,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='p0.conversation.replied'").fetchone()[0],2)

    def test_sealed_run_can_answer_after_its_allotment_was_returned(self):
        with CloseStudy(self.state.parent/"closed") as study:
            rid = study.run_id
            study.call(study.lead,"gx_decide_revision",{"decision":close_decision()},"close")
            study.advance()
            workspace.release_finished(study.conn)
            self.state = self.state.parent/"closed"/"state"
        with closing(cli.existing_db(self.state,readonly=True)) as db:
            before = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        response = self.cmd("message","--run-id",rid,"--message-id","explain","--text","Why did the study close?")
        self.assertEqual(response["status"],"answered",response)
        with closing(cli.existing_db(self.state,readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0],before)
            self.assertEqual(db.execute("SELECT status FROM v6_goals WHERE run_id=?",(rid,)).fetchone()[0],"CLOSED")
