"""A browser is the only active GX process. Only the native transport is controlled."""
from contextlib import closing, contextmanager
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from acceptance.multiturn.test_pi_questions import trace
from acceptance.unified_multiturn.test_conversation_delivery import MainPeer
from acceptance.v622.test_close import CloseStudy, close_decision
from gx_core.p0 import cli, conversation
from gx_core.p0.roots import Roots
from gx_core.web import research
from gx_core.web.http import BASE, make_server


class ResearchPeer(Roots):
    """Retain the real native dispatch, lease, budget, start receipt and GX driver."""
    starts = None

    def __enter__(self):
        self.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
        return self

    def __exit__(self, *exc):
        pass

    def _materialize(self, row):
        return row["thread_id"] or self._bind(row, {"id": "peer-" + row["lease_id"],
                                                    "cwd": row["cwd"], "source": "appServer"})

    def _resume(self, thread_id, **kw):
        self.loaded.add(thread_id)

    def reconcile_meter(self, thread_id):
        pass

    def poll(self, seconds):
        return False

    def _request(self, method, params, **kw):
        tid = params["threadId"]
        if method == "thread/goal/get":
            return {"goal": None}
        if method == "thread/read":
            return {"thread": {"id": tid, "status": {"type": "idle"}}}
        if method == "turn/start":
            self.starts.append((tid, params["clientUserMessageId"]))
            return {"turn": {"id": "turn-" + tid}}
        raise AssertionError(method)


class WebRecovery(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with CloseStudy(work / "parent") as s:
            self.state, self.parent = s.state, s.run_id
            self.project = s.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (self.parent,)).fetchone()[0]
            s.call(s.lead, "gx_decide_revision", {"decision": close_decision()}, "close-parent")
            s.advance()
            tid, turn = s.lead.thread, s.lead.turn
            s.roots.delivery.observe("turn/completed", {"threadId": tid, "turn": {"id": turn, "status": "completed"}})
        self.enterContext(patch.dict(os.environ, {"GX_CODEX_OFFLINE": "0"}))
        self.db = self.enterContext(closing(cli.existing_db(self.state, readonly=True)))

    @contextmanager
    def browser(self):
        with make_server(self.state, self.project, 0, run_id=self.parent) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
            thread.start()
            def request(body=None):
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)) as client:
                    path = BASE + "/" + self.project
                    client.request("POST" if body else "GET", path + ("/messages" if body else ""),
                                   json.dumps(body) if body else None, {"Content-Type": "application/json"})
                    reply = client.getresponse()
                    self.assertEqual(reply.status, 202 if body else 200)
                    return json.loads(reply.read())
            try:
                yield request
            finally:
                server.shutdown()
                thread.join()

    def wait(self, check, description):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            result = check()
            if result:
                return result
            time.sleep(.04)
        self.fail(description)

    def test_transient_io_after_host_acceptance_no_manual_resend(self):
        calls, turns, attempts = [], [], []
        self.enterContext(patch("gx_core.p0.driver.Roots", lambda conn, **kw:
                                MainPeer(conn, **kw, requests=calls, history=turns)))
        original = conversation.process
        def delivery(conn, state, rid, mid):
            attempts.append(mid)
            if len(attempts) == 1:
                raise OSError("transient storage read")
            receipt = original(conn, state, rid, mid)
            if len(attempts) == 2:
                raise OSError("lost return after acknowledgment")
            return receipt
        body = {"run_id": self.parent, "client_message_id": "web-explain", "text": "Which evidence was used?"}
        with patch.object(conversation, "process", delivery), self.browser() as request:
            request(body)
            self.wait(lambda: len(attempts) >= 3, "GX did not retry the same durable message")
            self.assertEqual(request()["messages"][-1]["reply"]["text"], "Reply from the controlled host to web-explain")
            self.assertEqual(len(calls), 1)
            job_count = self.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            self.assertEqual(job_count, 0)
            trace("web-inbox-retry", run_id=self.parent, attempts=attempts, host_starts=len(calls),
                  turn_id=turns[0]["id"], message_id=body["client_message_id"], jobs=job_count)

    def test_cold_restart_after_creation_before_dispatch(self):
        starts = []
        self.enterContext(patch.object(ResearchPeer, "starts", starts))
        self.enterContext(patch("gx_core.p0.driver.Roots", ResearchPeer))
        body = {"run_id": self.parent, "client_message_id": "cold-followup",
                "text": "Examine uncertainty in deposited values.", "successor": {}}
        # Simulate the crash immediately after GX commits the admission receipt.
        # No pending inbox message remains by the time the replacement web process starts.
        with patch.object(research.Delivery, "_drive_successors", return_value=None), self.browser() as request:
            request(body)
            self.wait(lambda: request()["messages"][-1].get("successor_execution") == "created",
                      "The admitted child should be visible before scientific execution")
            self.assertEqual(conversation.pending(self.db, self.parent), [])
            self.assertEqual(starts, [])
        with self.browser() as request:
            self.wait(lambda: request()["messages"][-1].get("successor_execution") == "started",
                      "A fresh web process must pick up the admitted child without another POST")
            self.assertEqual(len(starts), 1)
            trace("web-cold-admission", parent=self.parent, child=request()["messages"][-1]["result"]["run_id"],
                  before_restart="created", after_restart="started", host_starts=starts, resent=False)

    def test_web_only_successor_cold_recovery_dispatches_exactly_once(self):
        starts = []
        self.enterContext(patch.object(ResearchPeer, "starts", starts))
        self.enterContext(patch("gx_core.p0.driver.Roots", ResearchPeer))
        body = {"run_id": self.parent, "client_message_id": "next-study", "text": "Compare the deposited values.",
                "successor": {}}
        def receipt():
            return self.db.execute("SELECT result_json FROM p0_operator_messages WHERE message_id='next-study' "
                                   "AND result_json IS NOT NULL").fetchone()
        with self.browser() as request:
            request(body)
            child = json.loads(self.wait(receipt, "No child admitted")[0])["run_id"]
            self.wait(lambda: starts, "The child exists but its first Lead turn never started")
            request(body)
        with self.browser() as request:
            request(body)
            self.wait(lambda: request()["messages"][-1].get("successor_execution") == "started",
                      "The Web timeline did not distinguish admission from acknowledged execution")
        self.assertEqual(len(starts), 1, starts)
        self.assertEqual(self.db.execute("SELECT count(*) FROM v6_goals").fetchone()[0], 2)
        delivered = [dict(row) for row in self.db.execute(
            "SELECT d.event_id,d.thread_id,d.turn_id,r.task_id FROM p0_root_deliveries d JOIN p0_roots r "
            "USING(run_id,thread_id) WHERE d.run_id=? AND d.method='turn/start' AND d.state='acknowledged'", (child,))]
        self.assertEqual(len(delivered), 1)
        trace("web-successor", parent=self.parent, child=child, message_id=body["client_message_id"],
              receipts=delivered, host_starts=starts)
