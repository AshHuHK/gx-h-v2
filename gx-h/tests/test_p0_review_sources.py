"""Worker inputs, actual independent judgments and decisions across a late source."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from examples.science_judgment import judge
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import ScopeDenied
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, driver, impact, intent, roots, science_inputs, science_v2
from gx_core.persistence import store
from gx_core.teams.service import backend_ctx


class SourceDuringReview(unittest.TestCase):
    note_text = "补充：先前对照可能受污染。原始对照记录为 control-log-7。"

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.enterContext(native_state(self.directory))
        self.conn = store.open_store(self.state.db_path)
        self.addCleanup(lambda: self.conn.close())
        spec = cli.demo_spec()
        self.rounds = 2 if "priority" in self._testMethodName else 1
        if self.rounds == 2:
            spec["proposal"]["threshold"] = -100.0
        def bind(run):
            intent.bind_main(self.conn, run, "main")
            self.message(cli.initial_action(spec), "initial", run)
        self.run = service.run_start(driver.context(self.conn), {
            "objective": spec["objective"], "project_ref": "late-source", "budget_profile": "p0",
            "workflow": {"id":"science_grid","version":"2"},
            "inputs": {"p0":True,"proposal":spec["proposal"],"max_rounds":self.rounds,"p0_spec":spec},
            "idempotency_key":"start"}, prepare_p0=bind)["run_id"]
        for row in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (self.run,)).fetchall():
            JobService(self.conn).wait(row["job_id"], timeout_seconds=15)
        self.advance()
        self.attach()
        self.original_jobs = self.count("jobs")
        self.version = self.count("p0_intent_versions")
        self.reads, self.products = [], []

    def attach(self):
        self.root = roots.Roots(self.conn, run_id=self.run, cwd=self.directory)
        self.drv = driver.Driver(self.conn, self.directory, self.run)
        self.drv._native = self.root
        self.root.deliver = lambda tid, text, **kw: self.notices.append({"thread":tid, "text":text, **kw})
        self.notices = []

    def restart(self):
        self.conn.close()
        self.conn = store.open_store(self.state.db_path)
        self.attach()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (self.run,)).fetchone()[0]

    def advance(self):
        gx = backend_ctx(self.conn)
        reactor.lazy(gx, self.run)
        reactor.drive_science(gx, self.run)

    def message(self, action, name, run=None):
        run = run or self.run
        intent.stage_input(self.conn, run, name, action)
        return intent.accept_input(self.conn, run, "main", "human-turn-"+name, name)

    def task(self, role, status=None):
        return self.conn.execute("SELECT * FROM tasks WHERE run_id=? AND role=? "
            "AND (? IS NULL OR status=?) ORDER BY rowid DESC LIMIT 1", (self.run, role, status, status)).fetchone()

    def claim(self, task, thread_name=None):
        row = self.root._claim(task["task_id"])
        if row is None:
            return None
        thread = row["thread_id"] or thread_name or "worker-"+row["root_key"]
        self.conn.execute("UPDATE p0_roots SET thread_id=? WHERE root_key=?", (thread, row["root_key"]))
        self.conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (thread, row["lease_id"]))
        self.root.active_turns[thread] = "work-"+thread
        return roots.require_binding(self.conn, self.run, thread)

    def call(self, binding, name, args=None):
        tid = binding["thread_id"]
        return self.root._call({"threadId":tid,"turnId":self.root.active_turns[tid],"callId":ids.new_id("request"),
                                "tool": name, "arguments": args or {}})

    @staticmethod
    def packet(binding):
        return json.loads(binding["brief"].split("\n", 1)[1])

    def draft(self, binding, disposition=None):
        inputs = self.packet(binding)["input"]
        case = self
        class AssignedArtifacts:
            def read_json(self, ref):
                result = case.call(binding, "gx_read_artifact", {"ref":ref})
                case.reads.append(ref)
                return result
        reader = AssignedArtifacts()
        body = judge(reader, inputs)
        if disposition:
            context = self.call(binding,"gx_task_context")
            field = "source_reviews" if inputs["output_kind"] == "science_assessment" else "source_decisions"
            body[field] = []
            for source in context["supplement_sources"]:
                original = reader.read_json(source["artifact_ref"])
                self.assertEqual(original["text"], self.note_text)
                self.assertEqual(original["provenance"]["source_ref"], "control-log-7")
                reason = {"accepted":"The control log documents possible contamination, so I withhold interpretation of its measurement.",
                          "rejected":"The cited control is absent from this independent numerical fixture. Its observed scores remain interpretable.",
                          "pending":"The source supplies no matching sample identifier. The control's effect remains unresolved."}[disposition]
                body[field].append({"source_id":original["source_id"],"artifact_ref":source["artifact_ref"],
                    "disposition":disposition,"reason":reason,"evidence_refs":inputs["evidence_refs"]})
                if disposition == "accepted" and field == "source_reviews":
                    body["findings"][0].update(supported=None, qualifying_source_ids=[original["source_id"]], reason=reason)
        return body

    def submit(self, binding, body):
        result = self.call(binding, "gx_submit_product", {"product":body})
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE task_id=?",
                                          (binding["task_id"],)).fetchone()[0], "ACCEPTED", result)
        self.products.append(result["artifact_ref"])
        return result["artifact_ref"]

    def note(self):
        action = {"kind":"supplement","text":self.note_text,"source_ref":"control-log-7"}
        reply = self.message(action,"supplement")
        self.assertEqual(self.message(action,"supplement"), reply)
        self.assertEqual(reply["review_status"],"pending")
        self.assertEqual(self.count("p0_intent_versions"), self.version)
        return reply

    def waiting(self):
        status, = science_inputs.source_status(self.conn, self.run)
        self.assertEqual(status["disposition"], "pending")
        self.assertIsNone(status["decision"])
        self.assertIsNone(status["review"])
        self.assertFalse(science_v2.current_evidence(self.conn, self.run))

    def finish(self, prior, note, disposition):
        self.advance()
        planner = self.task("planner")
        self.assertIsNone(self.claim(planner))
        replacement = self.task("evidence_reviewer", "READY")
        self.assertIsNotNone(replacement)
        self.assertIsNone(self.claim(planner))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks WHERE role='evidence_reviewer' "
                                          "AND run_id=?", (self.run,)).fetchone()[0],2)
        successor = self.claim(replacement)
        packet = self.packet(successor)["input"]
        self.assertEqual(packet["previous_assessment_ref"],prior)
        self.assertEqual([s["source_id"] for s in packet["supplement_sources"]],[note["source_id"]])
        updated = self.submit(successor,self.draft(successor,disposition))
        self.drv.deliveries()
        reader = self.claim(planner)
        decision = self.draft(reader,disposition)
        self.assertEqual(decision["assessment_ref"],updated)
        final = self.submit(reader,decision)
        self.advance()
        self.drv.deliveries()
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE run_id=?", (self.run,)).fetchone()[0],"COMPLETED")
        result, = science_inputs.source_status(self.conn,self.run)
        self.assertEqual(result["disposition"],disposition)
        self.assertEqual(result["review"]["product_ref"],updated)
        self.assertEqual(result["decision"]["product_ref"],final)
        self.assertEqual(self.count("jobs"),self.original_jobs)
        self.assertEqual(self.count("p0_intent_versions"),self.version)
        history = science_inputs.review_history(self.conn,self.run)
        self.assertEqual([(r["artifact_ref"],r["source_ids"],r["covers_current_sources"]) for r in history],
                         [(updated,[note["source_id"]],True),(prior,[],False)])
        self.assertEqual(history[0]["evidence_refs"],history[1]["evidence_refs"])
        self.assertEqual(self.count("science_decisions"),1)
        self.export({"source":result,"reviews":history,"accepted_decision":decision,
                     "note":note,"operator_text":self.note_text,"unchanged_jobs":self.original_jobs})

    def export(self, data):
        if dest := os.environ.get("GX_P0_SOURCE_EVIDENCE"):
            directory = Path(dest)
            directory.mkdir(exist_ok=True, parents=True)
            data.update(run_id=self.run, worker_read_artifacts=self.reads, submitted=self.products,
                        outbox=[dict(r) for r in self.conn.execute("SELECT outbox_id,payload_json,consumed_at "
                                     "FROM outbox WHERE run_id=? AND topic LIKE 'p0.%'", (self.run,))])
            (directory / (self._testMethodName+".json")).write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")

    def test_source_commits_before_old_review_is_handed_in(self):
        reviewer = self.claim(self.task("evidence_reviewer"))
        old = self.draft(reviewer)
        note = self.note()
        self.drv.deliveries()
        self.assertIn(note["source_id"], self.notices[-1]["text"])
        prior = self.submit(reviewer,old)
        self.waiting()
        self.finish(prior,note,"accepted")

    def test_source_commits_after_old_review_and_while_decision_is_running(self):
        reviewer = self.claim(self.task("evidence_reviewer"))
        prior = self.submit(reviewer,self.draft(reviewer))
        self.advance()
        planner = self.claim(self.task("planner"))
        note = self.note()
        with self.assertRaises(ScopeDenied):
            self.call(planner,"gx_task_context")
        self.waiting()
        self.finish(prior,note,"rejected")

    def test_cold_restart_between_source_commit_and_processing(self):
        reviewer = self.claim(self.task("evidence_reviewer"))
        prior = self.submit(reviewer,self.draft(reviewer))
        note = self.note()
        self.assertIsNone(self.conn.execute("SELECT consumed_at FROM outbox WHERE outbox_id=?",
                                           (note["review_outbox_id"],)).fetchone()[0])
        self.restart()
        self.waiting()
        self.drv.deliveries()
        self.finish(prior,note,"pending")

    def change(self, kind, key, version, value):
        return self.message({"kind":"change","items":[{"item_id":key,"item_type":kind,
                "expected_version":version,"value":value}]}, f"{key}-{version}")

    def test_new_items_reach_idle_and_active_turns_and_result_text(self):
        from tests.test_p0_root_delivery import Peer
        worker = self.claim(self.task("evidence_reviewer"), "root")
        peer = Peer(self.conn, self.run)
        self.root._request = peer._request
        self.root._goal_for_budget = lambda *args: None
        self.root.loaded.add("root")
        self.root.active_turns.clear()
        del self.root.deliver
        first, second = "brief", "中文解释每项结果"
        a = self.change("soft_preference","format",0,first)
        self.drv.deliveries()
        self.assertEqual(peer.sent[0][0],"turn/start")
        self.root.active_turns["root"] = peer.turns[-1]["id"]
        b = self.change("soft_preference","format",1,second)
        self.drv.deliveries()
        self.assertEqual(peer.sent[-1][0],"turn/steer")
        sent = [json.loads(params["input"][0]["text"].split("GX updated this task. ")[1])
                for method, params in peer.sent]
        self.assertEqual([packet["items"][0]["value"] for packet in sent], [first, second])
        # Legacy ID-only replay reconstructs the occurrence at its original version.
        old = {k:v for k,v in sent[0].items() if k not in ("items","instruction")}
        self.assertEqual(impact.notification_content(self.conn,old)["items"], sent[0]["items"])
        self.assertEqual(sent[0]["task_id"],worker["task_id"])
        context = self.call(worker,"gx_task_context")
        self.assertEqual((context["intent"]["format"]["value"],context["intent"]["format"]["version"]),(second,2))
        product = self.draft(worker)
        product["intent_responses"] = [{"item_id":"format","version":2,"reason":"每项结果根据独立门槛给出中文解释。"}]
        for finding in product["findings"]:
            finding["reason"] = "原始观测达到冻结的数值门槛。" if finding["supported"] else "原始观测未达到冻结的数值门槛。"
        self.submit(worker,product)
        self.advance()
        planner = self.claim(self.task("planner"))
        self.assertEqual(self.packet(planner)["input"]["intent_items"]["format"]["value"], second)
        self.change("soft_preference","format",2,None)
        latest = self.call(planner,"gx_task_context")["intent"]["format"]
        self.assertEqual((latest["status"],latest["version"],latest["value"]),("removed",3,None))
        result = self.submit(planner,self.draft(planner))
        self.advance()
        self.export({"changes":[a,b],"worker_notice_payloads":sent,"worker_submitted_product":product,
                     "removed_item":latest,"decision_ref":result})

    def rank(self, reverse):
        reviewer = self.claim(self.task("evidence_reviewer"))
        findings = self.draft(reviewer)
        self.assertTrue(all(x["supported"] for x in findings["findings"]))
        self.submit(reviewer,findings)
        self.advance()
        names = [x["path_id"] for x in findings["findings"]]
        order = names[::-1] if reverse else names
        self.change("priority","compare_first",0,{"path_order":order})
        self.change("optimization_target","compare_cost",0,"Maximize information from a single follow-up grid within budget.")
        bad = self.change("optimization_target","unimplemented",0,{"opaque_formula":"?"})
        self.assertEqual(bad["status"],"unsupported")
        planner = self.claim(self.task("planner"))
        inputs = self.packet(planner)["input"]
        evidence_ref, = inputs["evidence_refs"]
        reading = self.call(planner, "gx_read_artifact", {"ref":evidence_ref})
        selected = inputs["intent_items"]["compare_first"]["value"]["path_order"][0]
        observed = next(row for row in reading["routes"] if row["path_id"] == selected)
        points = self.call(planner, "gx_read_artifact", {"ref":observed["observations_ref"]})["observations"]
        best = max(points, key=lambda p:p["score"])
        next_grid = {"synthetic":True,"method_id":"fixture-grid","method_version":"1",
                     "candidates":[{"candidate_id":"refined","x":best["x"]+0.125}]}
        decision = {"proposal_ref":inputs["proposal_ref"], "assessment_ref":inputs["assessment_ref"],
                    "continue_paths":[selected],"next_grids":{selected:next_grid},"stop_reason":None,
                    "reason":f"Both routes met the cutoff. Use the remaining grid near {selected}'s best point.",
                    "intent_responses":[{"item_id":"compare_first","version":1,"reason":f"Test {selected} first in the requested order.",
                                         "evidence_refs":[evidence_ref]},
                                        {"item_id":"compare_cost","version":1,"reason":"Both paths qualified. Spend a single grid around one observed maximum.",
                                         "evidence_refs":[evidence_ref]}]}
        self.submit(planner,decision)
        self.advance()
        checkpoint = store.loads(store.latest_checkpoint(self.conn,self.run),"state_json")
        proposal = ArtifactService(self.conn).read_json(checkpoint["outputs"]["proposal"]["proposal_ref"])
        self.assertEqual([p["id"] for p in proposal["paths"]],[selected])
        self.assertEqual(self.count("jobs"),self.original_jobs+1)
        for row in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (self.run,)).fetchall():
            JobService(self.conn).wait(row["job_id"], timeout_seconds=15)
        self.export({"alternatives":names,"priority":order,"selection":selected,"next_proposal":proposal,
                     "worker_decision":decision,"unsupported":bad})

    def test_priority_first_route(self):
        self.rank(False)

    def test_priority_second_route_same_candidates(self):
        self.rank(True)

    def test_accepted_decision_races_source_before_final_checkpoint(self):
        reviewer = self.claim(self.task("evidence_reviewer"))
        prior = self.submit(reviewer,self.draft(reviewer))
        self.advance()
        planner = self.claim(self.task("planner"))
        first_decision = self.submit(planner,self.draft(planner))
        note = self.note()
        self.waiting()
        self.drv.deliveries()
        replacement = self.task("planner","READY")
        self.assertNotEqual(replacement["task_id"],planner["task_id"])
        self.finish(prior,note,"rejected")
        old, = self.conn.execute("SELECT artifact_refs_json FROM submissions WHERE task_id=? AND result='ACCEPTED'",
                                (planner["task_id"],)).fetchall()
        self.assertIn(first_decision,json.loads(old[0]))

    def test_source_between_completed_report_checkpoint_and_run_close(self):
        from unittest.mock import patch
        reviewer = self.claim(self.task("evidence_reviewer"))
        prior = self.submit(reviewer,self.draft(reviewer))
        self.advance()
        planner = self.claim(self.task("planner"))
        old_decision = self.submit(planner,self.draft(planner))
        with patch("gx_core.orchestration.engine.Engine._finish", return_value={"status":"ACTIVE"}):
            self.advance()
        checkpoint = store.loads(store.latest_checkpoint(self.conn,self.run),"state_json")
        self.assertIn("report",checkpoint["completed"])
        old_report = checkpoint["outputs"]["report"]["artifact_ref"]
        note = self.note()
        self.drv.deliveries()
        self.finish(prior,note,"rejected")
        replaced = self.conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='science.decision_reconsidered'",
                                     (self.run,)).fetchall()
        self.assertEqual(len(replaced),1)
        self.assertEqual(json.loads(replaced[0][0])["previous_decision"]["decision_ref"],old_decision)
        previous = ArtifactService(self.conn).read_json(old_report)
        self.assertEqual(previous["rounds"][-1]["artifact_ref"],old_decision)
        final = store.loads(store.latest_checkpoint(self.conn,self.run),"state_json")
        self.assertNotEqual(final["outputs"]["report"]["artifact_ref"],old_report)

    def test_acknowledged_natural_supplement_cannot_be_overtaken_by_scientific_close(self):
        reviewer = self.claim(self.task("evidence_reviewer"))
        prior = self.submit(reviewer,self.draft(reviewer))
        self.advance()
        planner = self.claim(self.task("planner"))
        self.submit(planner,self.draft(planner))
        # The transport has acknowledged the human turn. Neither its candidate tool
        # nor terminal fallback has run when the driver next advances the science.
        text = self.note_text
        mid = "ack-before-close"
        driver.enqueue(self.conn,self.run,{"kind":"text","text":text},text,mid)
        self.root.ensure_main = lambda: "main"
        def acknowledge(value, *, before_start, on_started, message_id):
            before_start("main",message_id)
            on_started("main","official-ack-before-close",message_id)
        self.root.human_turn = acknowledge
        self.drv.messages()
        self.advance()
        self.assertNotEqual(self.conn.execute("SELECT status FROM runs WHERE run_id=?",(self.run,)).fetchone()[0],"COMPLETED")
        intent.finish_text(self.conn,self.run,mid)
        note = json.loads(self.conn.execute("SELECT result_json FROM p0_human_inputs WHERE message_id=?",(mid,)).fetchone()[0])
        self.assertEqual(note["status"],"supplement_registered")
        self.assertEqual(len(science_inputs.sources_for(self.conn,self.run)),1)
        self.drv.deliveries()
        # The natural note's exact text and client message ID provide its provenance.
        original_draft = self.draft
        def draft(binding, disposition=None):
            if not disposition:
                return original_draft(binding)
            body = original_draft(binding)
            source, = self.call(binding,"gx_task_context")["supplement_sources"]
            original = self.call(binding,"gx_read_artifact",{"ref":source["artifact_ref"]})
            self.assertEqual(original["text"],text)
            self.assertEqual(original["provenance"]["client_message_id"],mid)
            inputs = self.packet(binding)["input"]
            field = "source_reviews" if inputs["output_kind"] == "science_assessment" else "source_decisions"
            body[field] = [{"source_id":source["source_id"],"artifact_ref":source["artifact_ref"],
                            "reason":"The unidentified control cannot be linked to these numerical grid observations.",
                            "disposition":"rejected","evidence_refs":inputs["evidence_refs"]}]
            return body
        self.draft = draft
        self.finish(prior,note,"rejected")
