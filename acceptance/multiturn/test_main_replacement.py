"""Exercise Main recovery on a reopened GX database with the native RPC boundary scripted."""
import tempfile
import unittest
from pathlib import Path

from acceptance.v62a.interleavings_fixture import Scenario
from gx_core.errors import OutcomeUnknown, ScopeDenied
from gx_core.hostobs.app_server_client import AppServerResponseError, ReceivedMessage
from gx_core.p0 import conversation, intent, pi_clauses
from gx_core.p0.driver import Driver


class NativeMain:
    """Script only host responses; use the product delivery, binding and operator journal."""
    def __init__(self):
        self.threads = {}
        self.sent = []
        self.missing = set()
        self.disappear_on_send = set()

    def request(self, method, args):
        thread = args.get("threadId")
        if method == "turn/start" and thread in self.disappear_on_send:
            self.missing.add(thread)
        if thread in self.missing:
            raise AppServerResponseError(ReceivedMessage(1, {"id": 42, "error": {
                "code": -32600, "message": f"thread not found: {thread}"}}))
        if method in ("thread/start", "thread/resume"):
            if method == "thread/start":
                thread = "physical-" + str(len(self.threads) + 1)
                self.threads[thread] = {"id":thread, "cwd":args["cwd"], "source":"appServer",
                                        "status":{"type":"idle"}, "turns":[]}
            return {"thread":self.threads[thread], "model":args["model"], "modelProvider":args["modelProvider"],
                    "reasoningEffort":args["config"]["model_reasoning_effort"], "approvalPolicy":"never",
                    "sandbox":{"type":"readOnly", "networkAccess":False}}
        if method == "thread/read":
            return {"thread":self.threads[thread]}
        if method == "thread/turns/list":
            return {"data":self.threads[thread]["turns"], "nextCursor":None}
        if method == "thread/items/list":
            turn = next(t for t in self.threads[thread]["turns"] if t["id"] == args["turnId"])
            return {"data":[{"turnId":turn["id"], "item":item} for item in turn["items"]], "nextCursor":None}
        if method == "turn/start":
            message = args["clientUserMessageId"]
            self.sent.append((thread,message))
            return {"turn":{"id":"turn-"+message}}
        raise AssertionError(method)

    def attach(self, s, *, modern=True):
        driver = Driver(s.conn, s.state, s.run_id)
        driver.offline = False
        driver._native = s.roots
        s.roots.main_tools = [{"name":"gx_project_state", "type":"function", "description":"Read the project.",
                               "inputSchema":{"type":"object"}}]
        if modern:
            s.roots.main_tools.append(pi_clauses.TOOL)
        s.roots.main_call = driver.main_read
        s.roots._request = self.request
        return driver

    def complete(self, s, driver, thread, mid, response, *, meter=False):
        turn = {"id":"turn-"+mid, "status":"completed", "items":[{"type":"agentMessage", "text":response}]}
        if meter:
            receipt = {"threadId":thread, "turnId":turn["id"]}
            s.roots.delivery.observe("turn/started", {"threadId":thread, "turn":{"id":turn["id"]}})
            s.roots.delivery.observe("rawResponse/completed", {**receipt, "responseId":"response-"+mid,
                                                               "usage":{"totalTokens":3}})
            s.roots.delivery.observe("thread/tokenUsage/updated", {**receipt, "tokenUsage":{"total":{"totalTokens":3}}})
        self.threads[thread]["turns"].append(turn)
        s.roots.delivery.observe("turn/completed", {"threadId":thread, "turn":turn})
        driver.finish_main(mid, wait=0)


class MainReplacement(unittest.TestCase):
    def test_delivered_main_survives_loss_and_idle_upgrade(self):
        for reason in ("missing", "upgrade"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                host = NativeMain()
                with Scenario(directory, spec_updates={"input_files":[], "capabilities":[], "plugins":[]}) as s:
                    driver = host.attach(s, modern=(reason == "missing"))
                    sent = conversation.send(s.conn, s.run_id, "Why retain route B?", "earlier")
                    driver.messages()
                    old, = host.threads
                    s.roots.active_turns[old] = "turn-earlier"
                    s.roots._call({"threadId":old,"turnId":"turn-earlier","tool":"gx_project_state",
                                   "callId":"earlier-read","arguments":{}})
                    s.roots.active_turns.pop(old)
                    host.complete(s, driver, old, "earlier", "The comparison still has an open question.")
                    self.assertEqual(conversation.timeline(s.conn, s.run_id)["messages"][0]["status"], "answered")
                    if reason == "missing":
                        host.missing.add(old)
                with Scenario(directory, reopen=True) as cold:
                    driver = host.attach(cold)
                    later = conversation.send(cold.conn, cold.run_id, "And what did you say then?", "later", reply_to="earlier")
                    driver.messages()
                    new = next(key for key in host.threads if key != old)
                    self.assertEqual(host.sent, [(old,"earlier"), (new,"later")])
                    self.assertEqual(later["conversation_id"], sent["conversation_id"])
                    cold.roots.active_turns[new] = "turn-later"
                    restored = cold.roots._call({"threadId":new,"turnId":"turn-later","tool":"gx_project_state",
                                                 "callId":"recover","arguments":{}})
                    history = restored["recent_conversation"]
                    self.assertEqual([item["message_id"] for item in history], ["earlier","later"])
                    self.assertEqual((history[0]["main_reply"], history[1]["reply_to"]),
                                     ("The comparison still has an open question.", "earlier"))
                    self.assertIn("gx_route_pi_message", [t["name"] for t in cold.roots._tools_for(cold.roots._binding(new))])
                    self.assertEqual(cold.rows("SELECT state FROM p0_roots WHERE thread_id=?",old),
                                     [{"state":"archived_main"}])
                    self.assertEqual(cold.rows("SELECT call_id FROM p0_root_calls WHERE thread_id=?",old),
                                     [{"call_id":"earlier-read"}])
                    self.assertEqual(cold.rows("PRAGMA foreign_key_check"), [])
                    self.assertEqual(cold.rows("SELECT count(*) AS n FROM jobs"), [{"n":0}])
                    cold.roots.active_turns[old] = "turn-earlier"
                    with self.assertRaises(ScopeDenied):
                        cold.roots._call({"threadId":old,"turnId":"turn-earlier","tool":"gx_project_state",
                                          "callId":"late-write","arguments":{}})

    def test_live_host_loses_loaded_main_before_read_or_before_start(self):
        for failure in ("read", "send", "uncertain"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                host = NativeMain()
                with Scenario(Path(temp), spec_updates={"input_files":[], "capabilities":[], "plugins":[]}) as s:
                    driver = host.attach(s)
                    conversation.send(s.conn, s.run_id, "Which question is still open?", "first")
                    driver.messages()
                    old, = host.threads
                    host.complete(s, driver, old, "first", "The comparison remains open.", meter=True)
                    conversation.send(s.conn, s.run_id, "Please explain that comparison.", "followup", reply_to="first")
                    row, = s.rows("SELECT * FROM p0_operator_messages WHERE message_id='followup'")
                    if failure == "uncertain":
                        intent.stage_input(s.conn, s.run_id, "followup",
                                           {"kind":"text","text":row["message_text"]})
                        s.roots.delivery._prepare(old, row["message_text"], "human:followup", True, False,
                                                  "followup", None)
                    if failure == "send":
                        host.disappear_on_send.add(old)
                    else:
                        host.missing.add(old)
                    with self.assertRaises(AppServerResponseError):
                        driver._human_turn(s.roots, row)
                    self.assertNotIn(old, s.roots.loaded)
                    if failure == "uncertain":
                        with self.assertRaises(OutcomeUnknown):
                            driver._human_turn(s.roots, row)
                        self.assertEqual(host.sent, [(old,"first")])
                    else:
                        self.assertEqual(driver._human_turn(s.roots, row), "turn-followup")
                        new, = [t for t in host.threads if t != old]
                        self.assertEqual(host.sent, [(old,"first"), (new,"followup")])
                        self.assertEqual(s.rows("SELECT thread_id FROM p0_root_deliveries WHERE event_id='human:followup'"),
                                         [{"thread_id":new}])

    def test_lost_thread_with_unresolved_send_holds_and_ack_crash_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            host = NativeMain()
            with Scenario(work, spec_updates={"input_files":[], "capabilities":[], "plugins":[]}) as s:
                driver = host.attach(s)
                conversation.send(s.conn, s.run_id, "Which result is available?", "pending")
                old = s.roots.ensure_main()
                intent.bind_main(s.conn,s.run_id,old)
                intent.stage_input(s.conn,s.run_id,"pending",{"kind":"text","text":"Which result is available?"})
                # GX has committed the send intent. Neither the reply nor any PI effect is durable yet.
                s.roots.delivery._prepare(old,"Which result is available?","human:pending",True,False,"pending",None)
                host.missing.add(old)
            with Scenario(work, reopen=True) as cold:
                driver = host.attach(cold)
                with self.assertRaises(OutcomeUnknown):
                    cold.roots.ensure_main()
                self.assertEqual(list(host.threads), [old])
                self.assertEqual(cold.rows("SELECT state FROM p0_root_deliveries WHERE event_id='human:pending'"),
                                 [{"state":"sending"}])
                cold.roots.delivery._ack("human:pending","turn-pending")
                row, = cold.rows("SELECT * FROM p0_operator_messages WHERE message_id='pending'")
                self.assertEqual(driver._human_turn(cold.roots,row), "turn-pending")
                self.assertEqual(cold.rows("SELECT receipt_turn_id FROM p0_human_inputs WHERE message_id='pending'"),
                                 [{"receipt_turn_id":"turn-pending"}])
                self.assertEqual(host.sent, [])
                with self.assertRaises(OutcomeUnknown):
                    cold.roots.ensure_main()
