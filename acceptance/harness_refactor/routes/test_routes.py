"""Normal research admission, deterministic native callbacks and a real CPU launch crash."""
from contextlib import redirect_stderr, redirect_stdout
import json
import multiprocessing
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import SurveyedStudy
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid, ScopeDenied, StaleRevision
from gx_core.jobs import executor
from gx_core.p0 import v6_goal, v6_jobs, v6_routes

PRODUCT = Path(__file__).resolve().parents[3]


def resume(state, rid, log, *, crash):
    """Run the shipping CLI. Inject a fault only at the actual OS-launch boundary."""
    launch = executor.launch
    def observe(directory):
        if not crash:
            log.with_suffix(".duplicate").write_text(directory.name)
            raise AssertionError("Duplicate external process")
        handle = launch(directory)
        log.write_text(json.dumps({"job_id": directory.name, **handle}))
        os._exit(73)

    with log.with_suffix(".stdout").open("w") as output, redirect_stdout(output), redirect_stderr(output):
        with patch.object(executor, "launch", side_effect=observe), patch.dict(os.environ, {"GX_CODEX_OFFLINE": "1"}):
            sys.argv = [str(PRODUCT / "bin/gx-p0"), "resume", "--state", str(state), "--run-id", rid, "--timeout", "4"]
            runpy.run_path(sys.argv[0], run_name="__main__")


class RouteStudy(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        material = self.directory / "instrument.json"
        material.write_text(json.dumps({"fixture_version": "1", "synthetic": True, "method_id": "fixture-grid",
            "method_version": "1", "delay_seconds": 2 if "inflight" in self._testMethodName else 0.1, "candidates": [{"candidate_id": "two", "x": 2}]}))
        config = self.directory / "goal.json"
        config.write_text(json.dumps({"capabilities": ["fixture-grid@1"], "research_effort": "medium",
            "input_files": [{"path": str(material), "role": "material"}]}))
        started = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state",
            str(self.directory / "state"), "--run-key", "instrument-route", "--goal", "Check the numerical instrument archive.",
            "--goal-config", str(config), "--timeout", "0.001"], env={**os.environ, "GX_CODEX_OFFLINE": "1"},
            text=True, capture_output=True, timeout=30)
        self.assertTrue((self.directory / "state/gx.db").exists(), started.stderr)
        self.study = self.enterContext(SurveyedStudy(self.directory, reopen=True))
        self.lead = self.study.next_actor("run")

    def act(self, key, ops, evidence=(), **fields):
        return self.study.call(self.lead, "gx_decide_revision", {"decision": {
            "rationale": "Determine what the instrument archive establishes.", "ops": ops,
            "evidence_refs": list(evidence), **fields}}, key)

    def opened(self, *names):
        receipt = self.act("open", [dict(op="open_intent", intent="local:" + name, question="Check " + name)
                                   for name in names])
        return [receipt["aliases"]["local:" + name] for name in names]

    def next(self):
        self.lead = self.study.next_actor("run")

    def graph(self):
        return json.loads(self.study.rows("SELECT graph_json FROM v6_graph_versions ORDER BY version DESC LIMIT 1")[0]["graph_json"])

    def observation(self, name):
        rid = self.study.run_id
        ref = ArtifactService(self.study.conn).publish_bytes(namespace=rid + "/sources",
            kind="operator_source", rel_path=name + ".json", data=json.dumps({"observed": name}),
            run_id=rid, media_type="application/json", provenance={"source": "instrument fixture"}).artifact_ref
        v6_goal.record_observation(self.study.conn, rid, ref, source=name)
        return ref

    def save(self, study, **proof):
        directory = os.environ.get("GX_ROUTE_RECEIPTS")
        if not directory:
            return
        events = study.rows("SELECT event_id,kind,subject_id,payload_json FROM events WHERE "
                            "kind IN ('v6.route.revised','v6.node.settled','research.pi.feedback') ORDER BY event_id")
        for row in events:
            row["payload"] = json.loads(row.pop("payload_json"))
        result = dict(kind="FAULT_INJECTION" if "process_death" in self._testMethodName else "CONTROLLED_NATIVE", transport="controlled native transport; real GX and local CPU runner",
                      run=study.run_id, goal="Check the numerical instrument archive.", events=events, **proof)
        target = Path(directory) / (self._testMethodName + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2) + "\n")

    def test_evidence_only_resolution_and_reopen_preserve_other_route(self):
        first, second = self.opened("geometry", "format")
        self.assertEqual(self.graph()["intents"][first]["binding_state"], "INTENT_UNBOUND")
        self.next()
        evidence = self.study.materials[0]
        receipts = self.act("settle", [dict(op="resolve_intent", intent=first, revision=1, evidence_refs=[evidence]),
                                       dict(op="converge_intent", intent=second, revision=1, evidence_refs=[evidence])],
                            [evidence])
        self.assertEqual(self.graph()["intents"][first]["phase"], "RESOLVED")
        self.assertEqual(self.graph()["intents"][second]["phase"], "CONVERGED")
        self.assertEqual(self.study.rows("SELECT job_id FROM jobs"), [])
        # A later PI message reopens just the affected route using the public operator path.
        from gx_core.p0 import conversation, research_dialogue
        note = "The archive format specification changed."
        conversation.send(self.study.conn, self.study.run_id, note, "format-note")
        research_dialogue.add_feedback(self.study.conn, self.study.run_id, "format-note",
                                       goal_version=1, scope="run", text=note, kind="information")
        self.next()
        pi = self.study.brief(self.lead)["pi_dialogue"][0]["feedback_id"]
        fixed = self.graph()["intents"][first]
        with self.assertRaises(StaleRevision):
            self.act("stale", [dict(op="reopen_intent", intent=second, revision=1, feedback_id=pi)])
        self.act("reopen", [dict(op="reopen_intent", intent=second, revision=2, feedback_id=pi)],
                 pi_dispositions=[{"feedback_id": pi, "outcome": "adopt", "reason": "Recheck the affected format."}])
        self.assertEqual(self.graph()["intents"][first], fixed)
        self.assertEqual(self.graph()["intents"][second]["phase"], "OPEN")
        self.assertEqual(self.graph()["intents"][second]["revision"], 3)
        self.assertEqual(self.study.rows("SELECT job_id FROM jobs"), [])
        prior = self.study.rows("SELECT graph_json FROM v6_graph_versions WHERE version=?",
                                receipts["result_head"]["version"])[0]["graph_json"]
        self.assertEqual(json.loads(prior)["intents"][second]["phase"], "CONVERGED")
        self.save(self.study, routes=self.graph()["intents"], jobs=[])

    def test_atomic_binding_and_process_death_after_launch_recover_one_job(self):
        route, = self.opened("instrument")
        self.next()
        op = {"op": "add_node", "node": "local:read", "branch": "local:archive", "capability": "fixture-grid@1",
              "inputs": self.study.materials, "parameters": {"operation": "same"}, "question": "Read the instrument."}
        branch = {"op": "open_branch", "branch": "local:archive", "hypothesis": "The files agree.",
            "alternative": "The files differ.", "method_family": "numeric inventory",
            "discriminating_observation": "Check the input reading.", "stop_condition": "When the reading arrives.",
            "budget_limits": {"cpu_seconds": 120, "job_submissions": 3}}
        binding = dict(op="bind_intent", intent=route, revision=1, nodes=["local:read"])
        body = [branch, op, binding]
        receipt = self.act("bind", body)
        self.assertEqual(self.act("bind", body), receipt)
        node = receipt["aliases"]["local:read"]
        graph = self.graph()
        self.assertEqual(graph["intents"][route]["binding"], {"nodes": [node]})
        self.assertEqual(graph["nodes"][node]["intent_revision"], 2)
        self.assertEqual(self.study.rows("SELECT job_id FROM jobs"), [])
        launch = self.directory / "launch.json"
        self.study.stack.close()
        process = multiprocessing.get_context("spawn").Process(target=resume,
            args=(self.study.state, self.study.run_id, launch), kwargs={"crash": True})
        process.start()
        process.join(25)
        self.assertEqual(process.exitcode, 73, launch.with_suffix(".stdout").read_text())
        original = json.loads(launch.read_bytes())["job_id"]
        restored = self.directory / "resume.json"
        recovery = multiprocessing.get_context("spawn").Process(target=resume,
            args=(self.study.state, self.study.run_id, restored), kwargs={"crash": False})
        recovery.start()
        recovery.join(25)
        self.assertFalse(restored.with_suffix(".duplicate").exists())
        recovery_view = json.loads(restored.with_suffix(".stdout").read_text())
        self.assertEqual(recovery.exitcode, 75)  # Bounded public resume, next Codex decision held offline.
        self.assertEqual(recovery_view["driver"], "time_limit")
        self.assertEqual(recovery_view["waiting_for"]["kind"], "decision")
        with SurveyedStudy(self.directory, reopen=True) as restarted:
            settled, = restarted.rows("SELECT * FROM v6_nodes WHERE node_id=?", node)
            self.assertEqual(settled["job_id"], original)
            self.assertEqual(restarted.rows("SELECT job_id FROM jobs"), [{"job_id": original}])
            self.assertEqual(settled["status"], "SUCCEEDED")
            lead = restarted.next_actor("run")
            view = restarted.brief(lead)["research_intents"][0]
            self.assertEqual(view["intent"], route)
            self.assertEqual(view["executions"][0]["job_id"], original)
            self.assertEqual(view["executions"][0]["intent_revision"], 2)
            self.save(restarted, launch=json.loads(launch.read_bytes()), node=node, job_id=original,
                      result_ref=settled["result_ref"], route=view, additional_launches=0, process_exit=process.exitcode,
                      recovery_exit=recovery.exitcode, recovery_wait=recovery_view["waiting_for"])


    def test_late_evidence_locally_reopens_while_an_inflight_job_keeps_its_original_binding(self):
        running, settled = self.opened("read", "reference")
        self.next()
        base = self.study.materials[0]
        receipt = self.act("start", [
            {"op": "open_branch", "branch": "local:grid", "hypothesis": "The files agree.",
             "alternative": "The files differ.", "method_family": "comparison",
             "discriminating_observation": "Read the archive.", "stop_condition": "Once read.",
             "budget_limits": {"job_submissions": 3, "cpu_seconds": 120}},
            {"op": "add_node", "node": "local:old", "branch": "local:grid", "capability": "fixture-grid@1",
             "inputs": [base], "parameters": {"operation": "original"}, "question": "Read the instrument."},
            {"op": "bind_intent", "intent": running, "revision": 1, "nodes": ["local:old"]},
            {"op": "converge_intent", "intent": settled, "revision": 1, "evidence_refs": [base]}],
            [base], reconsider_when=[{"event": "evidence_added", "subject_ref": "run"}])
        old_node = receipt["aliases"]["local:old"]
        branch = receipt["aliases"]["local:grid"]
        v6_jobs.advance(self.study.conn, self.study.run_id)
        pin_sql = "SELECT version,job_id,operation_key,spec_json,status FROM v6_nodes WHERE node_id=?"
        original, = self.study.rows(pin_sql, old_node)
        self.assertEqual(original["status"], "RUNNING")
        ref = self.observation("independent-reference")
        self.next()
        with self.assertRaises(SchemaInvalid):
            self.act("old-reference", [{"op": "reopen_intent", "intent": settled, "revision": 2, "evidence_refs": [base]}], [base])
        self.act("new-reference", [{"op": "reopen_intent", "intent": settled, "revision": 2, "evidence_refs": [ref]}], [ref])
        self.assertEqual(self.study.rows(pin_sql, old_node), [original])
        self.assertEqual(self.graph()["intents"][running]["revision"], 2)
        self.next()
        with self.assertRaises(SchemaInvalid):
            self.act("move-old", [{"op": "bind_intent", "intent": running, "revision": 2, "nodes": [old_node]}])
        rebound = self.act("new-reading", [
            {"op": "add_node", "node": "local:new", "branch": branch, "capability": "fixture-grid@1",
             "inputs": [base], "parameters": {"operation": "follow-up"}, "question": "Reread for the new comparison."},
            {"op": "bind_intent", "intent": running, "revision": 2, "nodes": ["local:new"]}])
        new_node = rebound["aliases"]["local:new"]
        self.assertEqual(self.study.rows(pin_sql, old_node), [original])
        follow = self.study.settle(new_node)
        previous = self.study.settle(old_node)
        self.assertEqual(previous["job_id"], original["job_id"])
        self.assertNotEqual(previous["job_id"], follow["job_id"])
        self.assertEqual(json.loads(previous["spec_json"])["intent_revision"], 2)
        self.assertEqual(json.loads(follow["spec_json"])["intent_revision"], 3)
        self.assertEqual(self.graph()["intents"][running]["binding"], {"nodes": [new_node]})
        old_view = next(row for row in v6_routes.projection(self.study.conn, self.study.run_id, receipt["result_head"])
                        if row["intent"] == running)
        self.assertEqual([record["node_id"] for record in old_view["executions"]], [old_node])
        self.save(self.study, original=previous["job_id"], followup=follow["job_id"], counterevidence=ref)

    def test_module_binds_only_to_the_commissioned_route(self):
        first, second = self.opened("archive", "inference")
        self.next()
        grant = self.study.open_args("commission", caps=[])
        grant.update(module_id="ad_hoc", module_version="1", focus="research", route_intent=first,
                     expected_delivery="Compare the instrument files and report numerical discrepancies.",
                     sub_budget={"worker_tasks": 1, "api_calls": 1})
        issued = self.study.call(self.lead, "gx_open_module", grant, "commission")
        module = issued["module_instance_id"]
        with self.assertRaises(ScopeDenied):
            self.act("wrong", [{"op": "bind_intent", "intent": second, "revision": 1, "module_instance_id": module}])
        child = self.study.bind(issued["task_id"])
        with self.assertRaises((SchemaInvalid, ScopeDenied)):
            self.study.call(child, "gx_decide_revision", {"decision": {"rationale": "Local report only.", "ops": [
                {"op": "resolve_intent", "intent": first, "revision": 1, "evidence_refs": self.study.materials}
            ], "evidence_refs": self.study.materials}}, "child-cannot-change-graph")
        source = self.study.materials[0]
        self.study.call(child, "gx_read_artifact", {"ref": source, "operation_key": "read"}, "read")
        report = self.study.report(child, source)
        self.act("correct", [{"op": "bind_intent", "intent": first, "revision": 1, "module_instance_id": module}])
        assigned = self.graph()["intents"][first]
        self.assertEqual(assigned["binding"], {"module_instance_id": module})
        self.assertEqual(assigned["module_bindings"], [{"module_instance_id": module, "intent_revision": 2}])
        self.assertEqual(self.graph()["intents"][second]["binding_state"], "INTENT_UNBOUND")
        self.save(self.study, module=module, bound=assigned, original_report=report["report_ref"])

    def test_open_question_keeps_its_wake_until_its_own_resolution(self):
        waiting = {"event": "evidence_added", "subject_ref": "run"}
        other = {"event": "job_settled", "subject_ref": "run"}
        opened = self.act("declare", [{"op": "open_intent", "intent": "local:reference",
                                      "question": "Did the independent record agree?", "wake_condition": waiting},
                                     {"op": "open_intent", "intent": "local:header", "question": "What is recorded?"}],
                          reconsider_when=[other])
        first = opened["aliases"]["local:reference"]
        second = opened["aliases"]["local:header"]
        self.assertIn(waiting, opened["reconsider_when"])
        self.next()
        base = self.study.materials[0]
        settled = self.act("settle-header", [{"op": "resolve_intent", "intent": second,
            "revision": 1, "evidence_refs": [base]}], [base], reconsider_when=[other])
        self.assertIn(waiting, settled["reconsider_when"])
        self.assertIsNone(v6_goal.ensure_task(self.study.conn, self.study.run_id))
        ref = self.observation("second-reading")
        self.next()
        closed = self.act("settle-reference", [{"op": "resolve_intent", "intent": first, "revision": 1,
                         "evidence_refs": [ref]}], [ref], reconsider_when=[other])
        self.assertEqual(closed["reconsider_when"], [other])
        self.assertIsNone(v6_goal.ensure_task(self.study.conn, self.study.run_id))
        self.save(self.study, original_watch=waiting, final_watches=closed["reconsider_when"])
