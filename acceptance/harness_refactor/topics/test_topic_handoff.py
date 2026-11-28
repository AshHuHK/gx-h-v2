"""Public native callbacks with separate leased researchers; transport is controlled."""
import unittest
from unittest.mock import patch

from gx_core.errors import ScopeDenied, SchemaInvalid
from gx_core.p0 import cli
from gx_core.persistence import store
from tests import test_researcher_handoff as existing


class TopicStudy(unittest.TestCase):
    attach = existing.Researchers.attach
    bind = existing.Researchers.bind
    call = existing.Researchers.call
    open = existing.Researchers.open

    def setUp(self):
        start = cli.start
        def ordinary(conn, path, spec, **kwargs):
            return start(conn, path, {**spec, "harness_policy": "gx.harness/2",
                                      "discovery_policy": "gx.catalog/1",
                                      "research_modules":[*spec["research_modules"],"data_inventory@1"]}, **kwargs)
        from tests import test_v62_sources
        with patch.object(cli, "start", side_effect=ordinary):
            test_v62_sources.SourceGoal.setUp(self)
        self.src = self.refs["article.nxml"]

    def hint(self, actor, action, key, **fields):
        return self.call(actor, "gx_send_research_hint", {
            "topic":"station coverage", "action":action, "operation_key":key, **fields})

    def test_late_subscriber_cold_cursor_and_distinct_lineage(self):
        _, a = self.open(self.lead, [self.src])
        first = self.hint(a,"publish","question",question="Does the field window cover noon?")
        self.assertEqual([first["hint_id"]],first["origin_hint_ids"])
        _, b = self.open(self.lead, [self.src])
        sub = self.hint(b,"subscribe","join")
        self.assertGreaterEqual(sub["high_water"],first["event_id"])
        self.assertEqual([first["hint_id"]],[h["hint_id"] for h in sub["entries"]])
        forwarded = self.hint(b,"forward","forward",parent_hint_ids=[first["hint_id"]])
        self.assertEqual(forwarded["origin_hint_ids"],first["origin_hint_ids"])
        measured = self.hint(b,"publish","conditions",interpretation="The field record covers the west site.",
                             source_refs=[self.src])
        _, c = self.open(self.lead,[self.src])
        sub_c = self.hint(c,"subscribe","join")
        self.assertEqual({first["hint_id"],measured["hint_id"]},{h["hint_id"] for h in sub_c["entries"]})
        question, observation = sub_c["entries"]
        self.assertEqual(question["origin_hint_id"],first["hint_id"])
        self.assertEqual(len(question["forwarded_by"]),1)
        self.assertEqual(observation["originals"][0]["artifact_ref"],self.src)
        self.assertEqual(observation["originals"][0]["sha256"],self.conn.execute(
            "SELECT sha256 FROM artifacts WHERE artifact_ref=?",(self.src,)).fetchone()[0])
        joined = self.hint(c,"publish","synthesis",interpretation="Window and site are separate limitations.",
                           parent_hint_ids=[forwarded["hint_id"],measured["hint_id"]])
        self.assertEqual(set(joined["origin_hint_ids"]),{first["hint_id"],measured["hint_id"]})
        self.conn.close()
        self.conn = store.open_store(self.path)
        self.attach()
        self.roots.active_turns[b["threadId"]] = b["turnId"]
        live = self.call(b,"gx_task_context",{})["research_topics"][0]
        self.assertEqual([joined["hint_id"]],[h["hint_id"] for h in live["entries"]])
        self.assertGreater(live["cursor"],sub["high_water"])
        self.assertIsNone(live["entries"][0]["origin_hint_id"])
        self.assertEqual(live["entries"][0]["original_source_refs"],[self.src])
        self.assertEqual(self.call(b,"gx_task_context",{})["research_topics"][0]["entries"],[])
        self.assertEqual(self.hint(b,"subscribe","join"),sub)

    def test_visibility_even_for_summary_and_inherited_sources(self):
        _, a = self.open(self.lead,[self.src])
        _, b = self.open(self.lead,[self.refs["note.txt"]])
        restricted = self.hint(a,"publish","limited",interpretation="Only the western station was sampled.",
                               source_refs=[self.src])
        self.assertEqual(self.hint(b,"subscribe","sub")["entries"],[])
        with self.assertRaises(ScopeDenied):
            self.hint(b,"forward","guess",parent_hint_ids=[restricted["hint_id"]])
        with self.assertRaises(ScopeDenied):
            self.hint(b,"publish","foreign",question="Can the restricted file be checked?",source_refs=[self.src])
        _, c = self.open(self.lead,[self.src])
        self.hint(c,"subscribe","sub")
        fork = self.hint(c,"publish","derived",question="Should the east be sampled separately?",
                         parent_hint_ids=[restricted["hint_id"]])
        state = self.call(b,"gx_task_context",{})
        self.assertEqual(state["research_topics"][0]["entries"],[])
        self.assertNotIn(fork["hint_id"],str(state["research_topics"]))

    def test_correction_retraction_and_native_delivery_are_idempotent(self):
        _, a = self.open(self.lead,[self.src])
        _, b = self.open(self.lead,[self.src])
        self.hint(b,"subscribe","sub")
        args=dict(interpretation="The time stamp was noon.",source_refs=[self.src])
        first=self.hint(a,"publish","time",**args)
        self.assertEqual(self.hint(a,"publish","time",**args),first)
        with self.assertRaises(ScopeDenied):
            self.hint(b,"retract","other",parent_hint_ids=[first["hint_id"]])
        fix=self.hint(a,"correct","correct",parent_hint_ids=[first["hint_id"]],
                      interpretation="The time stamp was midnight.",source_refs=[self.src])
        self.assertEqual(fix["origin_hint_ids"],first["origin_hint_ids"])
        state=self.call(b,"gx_task_context",{})["research_topics"][0]
        self.assertEqual([h["hint_id"] for h in state["entries"]],[fix["hint_id"]])
        self.hint(a,"retract","withdraw",parent_hint_ids=[fix["hint_id"]])
        state=self.call(b,"gx_task_context",{})["research_topics"][0]
        self.assertEqual(state["entries"][0]["action"],"retract")
        _, d=self.open(self.lead,[self.src])
        self.assertEqual(self.hint(d,"subscribe","late")["entries"],[])
        pending=self.conn.execute("SELECT payload_json FROM outbox WHERE topic='research.topic.notice'").fetchall()
        self.assertTrue(pending)
        self.assertNotIn("midnight",str([p[0] for p in pending]))

    def test_legacy_and_isolated_roles(self):
        _, a=self.open(self.lead,[self.src])
        isolated=self.call(self.lead,"gx_open_module",{
            "module_id":"offline_literature_mechanism","module_version":"1","operation_key":"blind",
            "focus":"read independently","round":1,"input_refs":[self.src],"expected_delivery":"An independent conclusion",
            "capabilities":[],"sub_budget":{"api_calls":2,"tokens":3000,"worker_tasks":3},"communication":"isolated"})
        b=self.bind(isolated["task_id"])
        self.hint(a,"publish","public",question="Are station clocks comparable?")
        with self.assertRaises(ScopeDenied):
            self.hint(b,"subscribe","forbidden")
        packet=self.call(b,"gx_task_context",{})
        self.assertFalse(packet.get("research_topics"))
        self.assertFalse(packet.get("available_research_topics"))
        with self.assertRaises(ScopeDenied):
            self.call(a,"gx_send_research_hint",{"operation_key":"address","recipient_scope":isolated["scope"],
                                                  "text":"Read noon first","source_refs":[self.src]})

    def test_driver_delivers_one_coalesced_wake_without_lead_or_materials(self):
        from unittest.mock import Mock
        from gx_core.p0 import research_topics, v62_modules
        from gx_core.p0.driver import Driver
        _, sender = self.open(self.lead,[self.src])
        opened, reader = self.open(self.lead,[self.src])
        self.hint(reader,"subscribe","join")
        module = self.conn.execute("SELECT * FROM v62_modules WHERE scope=? AND run_id=?",
                                   (opened["scope"],self.run)).fetchone()
        task = v62_modules._task(self.conn,module)
        self.assertFalse(v62_modules._pending_input(self.conn,module,task))
        first = self.hint(sender,"publish","one",question="Did calibration precede measurement?")
        second = self.hint(sender,"publish","two",interpretation="Both dates were recorded.",source_refs=[self.src])
        self.assertTrue(v62_modules._pending_input(self.conn,module,task))
        driver = Driver(self.conn,self.directory,self.run)
        self.addCleanup(driver.close)
        native = Mock()
        with patch.object(driver,"_native_call",side_effect=lambda owner,key,operation: operation(native)):
            self.assertTrue(driver.deliveries((research_topics.NOTICE,)))
            driver.deliveries((research_topics.NOTICE,))
        native.deliver.assert_called_once()
        self.assertEqual(native.deliver.call_args.args[0],reader["threadId"])
        self.assertNotIn(self.src,str(native.deliver.call_args))
        self.assertNotIn("calibration",str(native.deliver.call_args))
        page = self.call(reader,"gx_task_context",{})["research_topics"][0]
        self.assertEqual([first["hint_id"],second["hint_id"]],[m["hint_id"] for m in page["entries"]])
        self.assertFalse(v62_modules._pending_input(self.conn,module,task))
        self.assertEqual(self.call(self.lead,"gx_task_context",{})["research_topic_findings"][-1]["hint_id"],
                         second["hint_id"])

    def test_only_author_can_revise_history_and_private_correction_withdraws_visible_ancestor(self):
        from gx_core.p0 import conversation, pi_materials
        opened = self.call(self.lead,"gx_open_module",{
            "module_id":"data_inventory","module_version":"1","operation_key":"pi-field",
            "focus":"Inspect field records","round":1,"input_refs":[self.src],"expected_delivery":"A dated reading",
            "capabilities":[],"sub_budget":{"api_calls":2,"tokens":3000,"worker_tasks":3}})
        author = self.bind(opened["task_id"])
        _, reader = self.open(self.lead,[self.src])
        first = self.hint(author,"publish","public",interpretation="The site is open.",source_refs=[self.src])
        self.hint(reader,"subscribe","join")
        target = opened["scope"]
        conversation.send(self.conn,self.run,"Inspect the supplemental dated field entry.","report-17",
                          branch_id=target,attachments=[{"name":"restricted.txt","text":"The gauge moved to a restricted site."}])
        ref = pi_materials.for_message(self.conn,self.run,"report-17")[0]["artifact_ref"]
        with store.transaction(self.conn,transaction_owner="TopicStudy",operation="route_pi_material",run_id=self.run) as tx:
            pi_materials.grant(tx,self.run,"report-17",clause_id="clause-r",branch_id=target,artifact_refs=[ref])
        correction = self.hint(author,"correct","limited",parent_hint_ids=[first["hint_id"]],
                               interpretation="The original scope was too broad.")
        self.assertEqual(self.call(reader,"gx_task_context",{})["research_topics"][0]["entries"],
                         [{"topic":"station coverage","action":"withdrawn","replaces":first["hint_id"],
                           "event_id":correction["event_id"]}])
        history = self.hint(reader,"history","versions",parent_hint_ids=[first["hint_id"]])["entries"]
        self.assertEqual([(m["hint_id"],m["current"]) for m in history],[(first["hint_id"],False)])
        private = self.hint(author,"history","versions",parent_hint_ids=[first["hint_id"]])["entries"]
        self.assertEqual([(m["hint_id"],m["current"]) for m in private],
                         [(first["hint_id"],False),(correction["hint_id"],True)])
        with self.assertRaises(ScopeDenied):
            self.hint(reader,"history","no",parent_hint_ids=[correction["hint_id"]])
        _, third = self.open(self.lead,[self.src])
        self.assertFalse(self.hint(third,"subscribe","late")["entries"])

    def test_multiple_replay_pages_survive_live_publication_and_same_callback_rechecks_visibility(self):
        from gx_core.p0 import research_topics
        _, author = self.open(self.lead,[self.src])
        _, reader = self.open(self.lead,[self.src])
        with patch.object(research_topics,"PAGE_SIZE",1):
            first = self.hint(author,"publish","first",question="Which field notebook?",source_refs=[self.src])
            second = self.hint(author,"publish","second",question="Where was the clock?")
            params = {"action":"subscribe","topic":"station coverage","operation_key":"same"}
            joined = self.call(reader,"gx_send_research_hint",params,"native-subscription")
            self.assertTrue(joined["has_more"])
            third = self.hint(author,"publish","third",question="When was it calibrated?")
            self.assertGreater(third["event_id"],joined["high_water"])
            replay = self.call(reader,"gx_task_context",{})["research_topics"][0]
            self.assertTrue(replay["has_more"])
            self.assertEqual(replay["cursor"],joined["high_water"])
            live = self.call(reader,"gx_task_context",{})["research_topics"][0]
            self.assertEqual([p["entries"][0]["hint_id"] for p in (joined,replay,live)],
                             [first["hint_id"],second["hint_id"],third["hint_id"]])
            self.assertFalse(live["has_more"])
            old = self.call(self.lead,"gx_task_context",{"before_topic_event":third["event_id"],
                                                        "research_topic":"station coverage"})
            self.assertEqual(old["research_topic_findings"][0]["hint_id"],second["hint_id"])
            # Simulate the material grant being narrower when either kind of replay arrives.
            with patch.object(research_topics,"visible_refs",return_value=set()):
                for call_id in ("native-subscription","different-callback"):
                    with self.assertRaises(ScopeDenied):
                        self.call(reader,"gx_send_research_hint",params,call_id)


class JobWake(unittest.TestCase):
    def test_normal_research_peer_wakes_a_successor_while_its_own_cpu_job_is_outstanding(self):
        import json
        import os
        from pathlib import Path
        import tempfile
        from acceptance.harness_refactor.modules.research_study import ResearchStudy
        from gx_core.p0 import v62_modules, v6_jobs

        root = Path(__file__).resolve().parents[3]
        method = "archival-peptide-coverage@1"
        source = root/"acceptance/science_assets/modalities/peptide/materials/1UAO.cif"
        spec = {"research_effort":"medium","research_modules":["ad_hoc@1"],"evidence_policy":"gx.evidence/6.2.4",
                "input_files":[{"role":"original","path":str(source)}],"plugins":[],"capabilities":[],
                "capability_collections":["archived_structure"]}
        with tempfile.TemporaryDirectory() as tmp, ResearchStudy(Path(tmp),spec_updates=spec) as study:
            def agent(name):
                args = study.open_args(name,caps=[],inputs=study.materials)
                args.update(module_id="ad_hoc",focus="Archive coverage",expected_delivery="Report original coverage.",
                            method_choices=[method])
                opened = study.call(study.lead,"gx_open_module",args,"open-"+name)
                return study.next_actor(opened["scope"])
            subscriber = agent("coverage")
            sender = agent("dates")
            topic = {"topic":"record coverage"}
            study.call(subscriber,"gx_send_research_hint",{**topic,"action":"subscribe","operation_key":"sub"},"sub")
            study.call(subscriber,"gx_select_capability",{"method":method,"action":"enable","revision":0},"local")
            decision = study.submit_node(subscriber,"inventory",capability=method)
            node = decision["aliases"]["local:inventory"]
            v6_jobs.advance(study.conn,study.run_id)
            admitted = study.conn.execute("SELECT status,job_id FROM v6_nodes WHERE node_id=?",(node,)).fetchone()
            self.assertEqual(admitted["status"],"RUNNING")
            self.assertTrue(admitted["job_id"])
            self.assertFalse(v62_modules.advance(study.conn,study.run_id))
            msg = study.call(sender,"gx_send_research_hint",{**topic,"action":"publish","operation_key":"date",
                "question":"Does the archived record identify the date?"},"date")
            v62_modules.advance(study.conn,study.run_id)
            successor = study.next_actor(subscriber.scope)
            self.assertNotEqual(subscriber.task_id,successor.task_id)
            current = study.call(successor,"gx_task_context",{},"peer-context")
            self.assertEqual(current["research_topics"][0]["entries"][0]["hint_id"],msg["hint_id"])
            again = study.call(successor,"gx_task_context",{},"peer-context-again")
            self.assertEqual(again["research_topics"][0]["entries"],[])
            self.assertEqual(study.conn.execute("SELECT job_id FROM v6_nodes WHERE node_id=?",(node,)).fetchone()[0],
                             admitted["job_id"])
            settled = study.settle(node)
            self.assertEqual(settled["status"],"SUCCEEDED")
            if output := os.environ.get("GX_TOPIC_RECEIPT"):
                receipt = {"entry":"bin/gx-p0 research","native_transport":"controlled callback","run_id":study.run_id,
                           "sender_task":sender.task_id,"subscribed_task":subscriber.task_id,"successor_task":successor.task_id,
                           "job_id":admitted["job_id"],"job_status_at_inbox_scheduling":admitted["status"],
                           "hint_id":msg["hint_id"],"hint_event":msg["event_id"],"inbox_cursor":current["research_topics"][0]["cursor"],
                           "second_read":again["research_topics"][0]["entries"],"settled":settled["status"],
                           "result":settled["result_ref"]}
                Path(output).write_text(json.dumps(receipt,indent=2)+"\n")
