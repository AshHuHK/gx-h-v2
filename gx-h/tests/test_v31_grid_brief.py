"""Frozen grid evidence, wire-size bounds and live Intent/source updates at worker claim."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import ArtifactHashMismatch, ScopeDenied
from gx_core.orchestration.hooks import WorkflowHooks
from gx_core.p0 import grid_brief, roots, schema
from gx_core.persistence import records, store
from gx_core.plugins.runtime_context import TaskContext


class GridBrief(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.conn = store.open_store(self.directory / "test.db")
        self.addCleanup(self.conn.close)
        schema.install(self.conn)
        self.run = self.create_run("3")
        self.assets = ArtifactService(self.conn)
        self.documents = {}
        self.context = {"intent": {}, "supplement_sources": []}
        self.citations = []
        hooks = WorkflowHooks(build_task_context=lambda conn, run, task, attempt:
                              TaskContext({"task_id": task, **self.context}, self.citations))
        self.enterContext(patch("gx_core.p0.roots.hooks_for_run", return_value=hooks))
        self.enterContext(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=str(self.directory)))
        self.enterContext(patch("gx_core.hostobs.host_policy.p0_role_for_run",
                                return_value={"model": "test-model", "effort": "high", "provider": "test"}))

    def create_run(self, version):
        run = records.create_run(self.conn, objective="frozen grid", project_ref="brief-test", actor="fixture",
                                 workflow_id="science_grid", workflow_version=version)
        self.conn.execute("UPDATE runs SET status='ACTIVE' WHERE run_id=?", (run,))
        return run

    def publish(self, kind, body, *, run=None, serialized=None, media="application/json"):
        data = serialized if serialized is not None else ids.canonical_json(body)
        ref = self.assets.put(run_id=run or self.run, kind=kind, media_type=media,
                              content=data, origin="HOST_PUT", put_by="fixture").artifact_ref
        self.documents[ref] = body
        return ref

    def task(self, inputs, *, run=None):
        task = records.create_task(self.conn, run_id=run or self.run, actor="fixture", kind="research_round",
                                   role="evidence_reviewer", task_input=inputs,
                                   required_artifacts=["science_assessment"], max_attempts=3)
        store.transition(self.conn, machine="task", subject_id=task, to_state="READY",
                         actor="backend", reason="fixture")
        return task

    def start(self, task, *, run=None):
        root = roots.Roots(self.conn, run_id=run or self.run, cwd=self.directory)
        bound = root._claim(task)
        thread = "worker-" + task
        self.conn.execute("UPDATE p0_roots SET thread_id=? WHERE root_key=?", (thread, bound["root_key"]))
        self.conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (thread, bound["lease_id"]))
        root.active_turns[thread] = "turn"
        return root, roots.require_binding(self.conn, run or self.run, thread)

    def call(self, root, row, name, args=None):
        return root._call({"threadId": row["thread_id"], "turnId": "turn", "callId": ids.new_id("request"),
                           "tool": name, "arguments": args or {}})

    def packet(self, row):
        return json.loads(row["brief"].removeprefix(grid_brief.PREFIX))

    def test_verified_summary_once_on_wire_and_originals_are_readable(self):
        data = self.publish("grid_input", {"candidates": [1, 2, 3]})
        obs = self.publish("grid_observations", {"observations": [{"score": -.2}]})
        unused = self.publish("research_proposal", {"private": "irrelevant prior proposal"})
        evaluation = self.publish("score_evaluation", {"input_ref": obs, "threshold": -.3, "count_at_or_above": 1})
        proposal = self.publish("research_proposal", {"paths": [{"id": "west", "input_ref": data}]})
        evidence = self.publish("research_evidence", {"proposal_ref": proposal, "routes": [
            {"evaluation_ref": evaluation, "observations_ref": obs, "input_ref": data, "archived_ref": unused}]})
        inputs = {"output_kind": "science_assessment", "proposal_ref": proposal, "evidence_refs": [evidence]}
        self.context["grid_measurements"] = {"proposal_ref": proposal, "evidence_refs": [evidence]}
        task = self.task(inputs)
        root, row = self.start(task)
        packet = self.packet(row)
        actual = packet["preloaded_artifacts"]
        self.assertEqual(set(actual), {proposal, evidence, evaluation})
        for ref, entry in actual.items():
            self.assertEqual(entry["content"], self.assets.read_json(ref))
            self.assertEqual(entry["sha256"], hashlib.sha256(self.assets.read_bytes(ref)).hexdigest())
        self.assertNotIn("workflow_context", packet)
        self.assertEqual(set(packet["lazy_artifacts"]), {data, obs, unused})
        self.assertNotIn("irrelevant prior proposal", row["brief"])
        self.assertLessEqual(len(ids.canonical_json(actual).encode()), 16 * 1024)
        self.assertLessEqual(sum(self.assets.describe(ref)["bytes"] for ref in actual), 16 * 1024)
        settings = root._settings(row, new=True)
        self.assertNotIn(row["brief"], settings["developerInstructions"])
        messages = []
        def deliver(thread, text, *, event_id):
            messages.append(text)
            return {"turn_id": "turn"}
        with patch.object(root, "_materialize", return_value=row["thread_id"]), patch.object(root, "deliver", deliver):
            root.dispatch(task)
        self.assertEqual(messages, [row["brief"]])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p0_root_calls").fetchone()[0], 0)
        for ref in (obs, data):
            self.assertEqual(self.call(root, row, "gx_read_artifact", {"ref": ref}), self.documents[ref])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p0_root_calls").fetchone()[0], 2)
        reopened = roots.Roots(self.conn, run_id=self.run, cwd=self.directory)
        with patch.object(ArtifactService, "read_bytes", side_effect=AssertionError("reloaded frozen data")):
            self.assertEqual(reopened._claim(task)["brief"], row["brief"])

    def test_source_notification_and_new_attempt_change_only_new_brief(self):
        one = self.publish("operator_source", {"text": "first original", "provenance": {"locator": "x:1"}})
        two = self.publish("operator_source", {"text": "late original", "provenance": {"locator": "x:9"}})
        def source(ref):
            return {"source_id": "source-" + ref, "artifact_ref": ref, "round": 0,
                    "segments": [self.documents[ref]["text"]], **self.documents[ref]}
        self.context.update(intent={"format": {"version": 1}}, supplement_sources=[source(one)])
        self.citations.append(one)
        initial = self.publish("research_proposal", {"threshold": -.4})
        inputs = {"output_kind": "science_assessment", "supplement_sources": [source(one)], "proposal_ref": initial}
        task = self.task(inputs)
        root, original = self.start(task)
        packet = self.packet(original)
        self.assertEqual(packet["supplement_sources"][0]["artifact_ref"], one)
        self.assertNotIn("first original", original["brief"])
        self.assertEqual(packet["lazy_artifacts"][one]["sha256"], self.assets.describe(one)["sha256"])
        self.context.update(intent={"format": {"version": 2}}, supplement_sources=[source(one), source(two)])
        self.citations.append(two)
        with self.assertRaises(ScopeDenied):
            self.call(root, original, "gx_read_artifact", {"ref": two})
        fresh = self.call(root, original, "gx_task_context")
        self.assertEqual(fresh["intent"]["format"]["version"], 2)
        self.assertEqual(self.call(root, original, "gx_read_artifact", {"ref": two}), self.documents[two])
        self.assertEqual(root._claim(task)["brief"], original["brief"])
        revised = self.publish("research_proposal", {"threshold": -.2})
        with store.transaction(self.conn):
            store.release_lease(self.conn, original["lease_id"])
            store.transition(self.conn, machine="task", subject_id=task, to_state="READY", actor="backend",
                             reason="new input", extra_updates={"owner": None, "lease_id": None})
            self.conn.execute("UPDATE tasks SET input_json=? WHERE task_id=?",
                              (ids.canonical_json({**inputs, "proposal_ref": revised}), task))
        successor = root._claim(task)
        updated = self.packet(successor)
        self.assertEqual(updated["intent"], self.context["intent"])
        self.assertEqual([s["artifact_ref"] for s in updated["supplement_sources"]], [one, two])
        self.assertEqual(updated["lazy_artifacts"][two]["sha256"], self.assets.describe(two)["sha256"])
        self.assertEqual(packet["preloaded_artifacts"], {
            initial: {"sha256": self.assets.describe(initial)["sha256"], "content": self.documents[initial]}})
        self.assertEqual(updated["preloaded_artifacts"], {
            revised: {"sha256": self.assets.describe(revised)["sha256"], "content": self.documents[revised]}})
        self.assertNotIn("late original", successor["brief"])
        self.assertNotEqual(ids.sha256_of_obj(original["brief"]), ids.sha256_of_obj(successor["brief"]))
        self.assertEqual(root._row(original["root_key"])["brief"], original["brief"])

    def test_source_and_rendered_byte_caps_with_utf8_and_padding(self):
        limit = grid_brief.PRELOAD_BYTES
        refs = [self.publish("research_evidence", {"text": "测"*n}) for n in (6000, 3000, 2800, 100)]
        oversized, first, overflow, small = refs
        calls = []
        original = ArtifactService.read_bytes
        def bounded(service, ref, **kwargs):
            calls.append((ref, kwargs))
            return original(service, ref, **kwargs)
        with patch.object(ArtifactService, "read_bytes", bounded):
            _, row = self.start(self.task({"output_kind": "science_assessment", "evidence_refs": refs}))
        packet = self.packet(row)
        self.assertEqual(set(packet["preloaded_artifacts"]), {first, small})
        self.assertEqual(packet["lazy_artifacts"][oversized]["reason"], "item_byte_limit")
        self.assertEqual(packet["lazy_artifacts"][overflow]["reason"], "total_byte_limit")
        self.assertEqual([ref for ref, _ in calls], [first, small])
        for ref, kwargs in calls:
            self.assertEqual(kwargs, {"max_bytes": limit, "expected_sha256": self.assets.describe(ref)["sha256"]})
        self.assertLessEqual(len(ids.canonical_json(packet["preloaded_artifacts"]).encode()), limit)
        padded = self.publish("research_evidence", {}, serialized="{}" + " "*(limit-2))
        entry = self.packet(self.start(self.task({"output_kind": "science_assessment",
                                                  "evidence_refs": [padded]}))[1])["preloaded_artifacts"][padded]
        self.assertEqual(entry, {"content": {}, "sha256": self.assets.describe(padded)["sha256"]})
        wire = self.publish("research_evidence", {"text": "x"*(limit-24)})
        packet = self.packet(self.start(self.task({"output_kind": "science_assessment", "evidence_refs": [wire]}))[1])
        self.assertLess(self.assets.describe(wire)["bytes"], limit)
        self.assertEqual(packet["preloaded_artifacts"], {})
        self.assertEqual(packet["lazy_artifacts"][wire]["reason"], "total_byte_limit")

    def test_denied_or_changed_artifacts_never_supply_preload(self):
        other = self.create_run("3")
        foreign = self.publish("research_proposal", {"secret": "other-run-data"}, run=other)
        same_run = self.publish("research_proposal", {"secret": "uncited-hook-content"})
        self.context["supplement_sources"] = [{"source_id": "spoof", "artifact_ref": same_run}]
        root, row = self.start(self.task({"output_kind": "science_assessment", "proposal_ref": foreign}))
        self.assertEqual(self.packet(row)["lazy_artifacts"], {
            foreign: {"reason": "unavailable_to_task"}, same_run: {"reason": "unavailable_to_task"}})
        self.assertEqual(self.packet(row)["preloaded_artifacts"], {})
        for ref in (foreign, same_run):
            self.assertNotIn(self.assets.describe(ref)["sha256"], row["brief"])
            with self.assertRaises(ScopeDenied):
                self.call(root, row, "gx_read_artifact", {"ref": ref})
        ref = self.publish("research_proposal", {"title": "original"})
        info = self.assets.describe(ref)
        path = self.assets.resolve_path(info["namespace"], info["rel_path"])
        path.write_text('{"title":"tampered"}')
        task = self.task({"output_kind": "science_assessment", "proposal_ref": ref})
        with self.assertRaises(ArtifactHashMismatch):
            root._claim(task)
        self.assertIsNone(self.conn.execute("SELECT root_key FROM p0_roots WHERE task_id=?", (task,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "READY")

    def test_pinned_old_request_bytes(self):
        run = self.create_run("2")
        inputs = {"output_kind": "science_assessment", "instruction": "Inspect the frozen measures."}
        task = self.task(inputs, run=run)
        root, row = self.start(task, run=run)
        normalized = row["brief"].replace(task, "FIXED-TASK")
        # Captured independently from the pre-B3 implementation at 10ed415.
        expected = "7f6fbc232ce9ec1d3b4094595af8ab60121e2c3514cb6aaa2c4469c9f61d6d1b"
        self.assertEqual(hashlib.sha256(normalized.encode()).hexdigest(), expected)
        self.assertEqual(root._settings(row, new=True)["developerInstructions"],
                         roots.WORKER_INSTRUCTIONS + "\n" + row["brief"])
        self.assertFalse(grid_brief.includes_artifacts(row["brief"]))
        # Older @3 attempts also keep their original frozen transport on restart.
        self.conn.execute("UPDATE runs SET workflow_version='3' WHERE run_id=?", (run,))
        cold = roots.Roots(self.conn, run_id=run, cwd=self.directory)
        self.assertEqual(cold._claim(task)["brief"], row["brief"])
        self.assertEqual(cold._settings(row, new=False)["developerInstructions"],
                         root._settings(row, new=True)["developerInstructions"])
