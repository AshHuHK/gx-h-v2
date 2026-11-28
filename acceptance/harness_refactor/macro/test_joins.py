"""A Lead subscribes to evidence produced by ordinary multi-output CPU Jobs."""
from contextlib import closing
from unittest.mock import patch
import unittest

from gx_core.errors import SchemaInvalid
from gx_core.persistence import store
from gx_core.p0 import harness_policy, research_joins, v6_decisions, v6_goal
from tests import test_v6_named_ports as named


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.case = named.NamedOutputs()
        start = v6_goal.start
        def current_goal(conn, spec, **kwargs):
            return start(conn, dict(spec, harness_policy=harness_policy.VERSION), **kwargs)
        with patch.object(v6_goal, "start", side_effect=current_goal):
            self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.db, self.run = self.case.conn, self.case.run
        result = v6_decisions.adopt(self.db, **self.case.call, decision=self.case.graph([]))
        self.node = result["aliases"]["local:split"]

    def spec(self, mode="all", *names):
        return {"key": "bounds", "mode": mode, "question": "Are both bounds available?",
                "ports": [{"slot": str(i), "alternatives": [{"node_id": self.node, "output_name": name}]}
                          for i, name in enumerate(names or ("next.json", "optional.json"))]}

    def test_exact_named_outputs_wake_once_after_restart(self):
        spec = self.spec()
        saved = research_joins.register(self.db, self.run, spec)
        self.assertEqual(saved["state"], "WAITING")
        self.assertEqual(research_joins.register(self.db, self.run, spec), saved)
        self.case.settle([self.node], self.db)
        with closing(store.open_store(self.case.db)) as reopened:
            v6_goal.ensure_task(reopened, self.run)
            row = research_joins.lookup(reopened, self.run, "bounds")
            self.assertEqual(row["state"], "SATISFIED")
            self.assertEqual(row["outcome"]["satisfied_count"], 2)
            proofs = [port["evidence"][0] for port in row["outcome"]["ports"]]
            self.assertCountEqual([p["output_name"] for p in proofs], ["next.json", "optional.json"])
            self.assertEqual(len(set(p["artifact_ref"] for p in proofs)), 2)
            research_joins.tick(reopened, self.run)
            self.assertEqual(research_joins.register(reopened, self.run, spec), row)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM events WHERE kind='research.join.ready'").fetchone()[0], 1)

    def test_any_alternatives_required_and_k(self):
        self.case.settle([self.node], self.db)
        for mode, threshold, required, expected in [
            ("all", None, False, "UNREACHABLE"), ("any", None, False, "SATISFIED"),
            ("any", None, True, "UNREACHABLE"), ("k-of-n", 2, False, "SATISFIED"),
            ("k-of-n", 3, False, "UNREACHABLE")]:
            spec = self.spec(mode, "mean.json", "missing", "absent")
            spec["key"] += mode + str(threshold) + str(required)
            spec["ports"][1]["alternatives"].append({"node_id": self.node, "output_name": "next.json"})
            spec["ports"][2]["required"] = required
            if threshold:
                spec["k"] = threshold
            row = research_joins.register(self.db, self.run, spec)
            self.assertEqual(row["state"], expected, spec)
            self.assertFalse(row["outcome"]["ports"][2]["evidence"])

    def test_shared_result_cannot_stand_for_two_deliveries(self):
        self.case.settle([self.node], self.db)
        overlap = self.spec("all", "mean.json", "mean.json")
        bad = research_joins.register(self.db, self.run, overlap)
        self.assertEqual((bad["state"], bad["outcome"]["satisfied_count"]), ("UNREACHABLE", 1))
        overlap["ports"][0]["alternatives"].append({"node_id": self.node, "output_name": "next.json"})
        rerouted = research_joins.register(self.db, self.run, dict(overlap, revision=2))
        self.assertEqual(rerouted["state"], "SATISFIED")
        self.assertEqual(len({p["counted_artifact_ref"] for p in rerouted["outcome"]["ports"]}), 2)

    def test_two_reconcilers_commit_one_wake(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        spec = self.spec()
        saved = research_joins.register(self.db, self.run, spec)
        self.case.settle([self.node], self.db)
        barrier = Barrier(2, timeout=10)
        def reconcile(_):
            with closing(store.open_store(self.case.db)) as db:
                barrier.wait()
                research_joins.tick(db, self.run)
                return research_joins.lookup(db, self.run, "bounds")
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.map(reconcile, range(2))
        self.assertEqual((a["outcome_event"], a["registration_event"]), (b["outcome_event"], saved["registration_event"]))
        self.assertIsNotNone(a["outcome_event"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='research.join.ready'").fetchone()[0], 1)

    def test_damaged_output_is_an_engineering_gap(self):
        from gx_core.artifacts import ArtifactService
        from gx_core.p0 import v62_outputs
        nodes = self.case.settle([self.node], self.db)
        files = v62_outputs.published_outputs(self.db, self.run, nodes[self.node]["job_id"])
        ref = next(o["artifact_ref"] for o in files if o["output_name"] == "next.json")
        ArtifactService(self.db).path_of(ref).write_bytes(b"damaged acceptance artifact")
        outcome = research_joins.register(self.db, self.run, self.spec("all", "next.json"))["outcome"]
        self.assertEqual((outcome["state"], outcome["satisfied_count"]), ("UNREACHABLE", 0))
        source, = outcome["ports"][0]["sources"]
        self.assertEqual(source["state"], "UNVERIFIED")
        self.assertIn("digest", source["reason"])

    def test_resource_retry_stays_possible_until_the_agreed_deadline(self):
        from gx_core.errors import BudgetExceeded
        from gx_core.p0 import v6_jobs
        node = self.db.execute("SELECT * FROM v6_nodes WHERE node_id=?", (self.node,)).fetchone()
        v6_jobs._record_wait(self.db, node, BudgetExceeded("Waiting for two CPU threads",
                            details={"rule": "cpu.threads_free", "needed": 2}))
        saved = research_joins.register(self.db, self.run, dict(self.spec("any", "next.json"), timeout_seconds=3))
        self.assertEqual(saved["state"], "WAITING")
        research_joins.tick(self.db, self.run, now=saved["deadline"] + 1)
        outcome = research_joins.lookup(self.db, self.run, "bounds")["outcome"]
        self.assertEqual(outcome["still_possible_count"], 1)
        retry = outcome["ports"][0]["sources"][0]["retry"]
        self.assertEqual((retry["rule"], retry["needed"]), ("cpu.threads_free", 2))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0], 0)

    def test_deadline_and_revision(self):
        spec = dict(self.spec(), timeout_seconds=3)
        old = research_joins.register(self.db, self.run, spec)
        self.assertGreater(research_joins.next_deadline_seconds(self.db, self.run), 0)
        research_joins.tick(self.db, self.run, now=old["deadline"] + 1)
        self.assertEqual(research_joins.lookup(self.db, self.run, "bounds")["state"], "DEADLINE")
        changed = dict(spec, question="Can we check after recalibration?")
        with self.assertRaises(SchemaInvalid):
            research_joins.register(self.db, self.run, changed)
        self.assertEqual(research_joins.register(self.db, self.run, dict(changed, revision=2))["state"], "WAITING")
        self.assertEqual(research_joins.lookup(self.db, self.run, "bounds", revision=1)["state"], "DEADLINE")

    def test_unknown_external_job_wakes_with_context_at_deadline(self):
        from gx_core.p0 import v6_jobs
        # Pause at a real admitted Job; leave its runner to complete normally on later poll.
        with patch.object(v6_jobs, "_finish", return_value=False):
            v6_jobs.advance(self.db, self.run)
        node = self.db.execute("SELECT job_id FROM v6_nodes WHERE node_id=?", (self.node,)).fetchone()
        self.assertIsNotNone(node[0])
        self.db.execute("UPDATE jobs SET status='OUTCOME_UNKNOWN' WHERE job_id=?", (node[0],))
        spec = dict(self.spec("any", "next.json"), timeout_seconds=5)
        saved = research_joins.register(self.db, self.run, spec)
        self.assertEqual(saved["state"], "WAITING")
        research_joins.tick(self.db, self.run, now=saved["deadline"] + 1)
        out = research_joins.lookup(self.db, self.run, "bounds")["outcome"]
        self.assertEqual((out["state"], out["still_possible_count"]), ("DEADLINE", 1))
        status = out["ports"][0]["sources"][0]
        self.assertEqual((status["job_state"], status["job_id"]), ("OUTCOME_UNKNOWN", node[0]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0], 1)


class ModuleJoinTests(unittest.TestCase):
    def test_partial_per_port_source_private_work_and_lead_wake(self):
        from pathlib import Path
        import os
        import tempfile
        from acceptance.v622.test_close import actual_packet
        from acceptance.v62a.interleavings_fixture import draft, save
        from acceptance.harness_refactor.modules.research_study import ResearchStudy
        from gx_core.artifacts import ArtifactService
        from gx_core.errors import ScopeDenied

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        specfile, datafile = folder / "count.json", folder / "temperatures.json"
        save(specfile, {"script": "{'count': count(data['A'])}", "output_schema": {
            "type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"],
            "additionalProperties": False}})
        save(datafile, [6.5, 7.0, 7.5, 8.0])
        second_sample = folder / "second-sample.json"
        save(second_sample, [12.5, 11.5])
        settings = {"research_effort": "deep", "research_modules": ["data_inventory@1"],
                    "capabilities": ["artifact-analysis@1"], "plugins": [],
                    "input_files": [{"path": str(specfile), "role": "spec"}, {"path": str(datafile), "role": "temperatures"},
                                    {"path": str(second_sample), "role": "temperatures"}]}
        s = self.enterContext(ResearchStudy(folder / "research", spec_updates=settings))
        def open_module(key, **kwargs):
            return s.call(s.lead, "gx_open_module", dict(s.open_args(key, caps=["artifact-analysis@1"], inputs=s.materials, wake_on=[]),
                expected_delivery="Determine the size of the archived sample and identify missing measurements.", **kwargs), key)
        opened = open_module("count")
        isolated = open_module("independent", communication="isolated")

        def watch(key, target, name):
            return s.call(s.lead, "gx_watch_join", {"key": key, "mode": "all", "question": "What data are available?",
                "ports": [{"slot": "measurement", "alternatives": [{**target, "output_name": name}]}]}, "watch-" + key)
        with self.assertRaises(ScopeDenied):
            watch("isolated", {"module_instance_id": isolated["module_instance_id"]}, "report")
        target = {"module_instance_id": opened["module_instance_id"]}
        watcher = watch("count", target, "result.json")
        self.assertEqual(watcher["state"], "WAITING")
        report_watcher = watch("authored", target, "report")
        self.assertEqual(report_watcher["state"], "WAITING")
        watch("standard_deviation", target, "std.json")

        worker = s.bind(opened["task_id"])
        op = dict(s.node(worker, "count", capability="artifact-analysis@1"), inputs=s.materials[:2],
                  question="How many observations were archived?")
        second_op = dict(op, node="local:other", inputs=[s.materials[0], s.materials[2]])
        submitted = s.call(worker, "gx_decide_revision", {"decision": draft(op, second_op,
            watches=[{"event": "job_settled", "subject_ref": "run"}])}, "record-count")
        node_id = submitted["aliases"]["local:count"]
        result = s.settle(node_id)["result_ref"]
        other = s.settle(submitted["aliases"]["local:other"])["result_ref"]
        self.assertEqual(ArtifactService(s.conn).read_json(result), {"count": 4})
        self.assertEqual(ArtifactService(s.conn).read_json(other), {"count": 2})
        # Even an exact module-internal node ID cannot expose the raw result before the worker's report.
        lead = s.next_actor("run")
        self.assertEqual(watch("internal", {"node_id": node_id}, "result.json")["state"], "WAITING")
        s.call(lead, "gx_decide_revision", {"decision": draft(watches=[{"event": "goal_changed", "subject_ref": "run"}])}, "await-evidence")
        worker = s.next_actor(opened["scope"])
        report = s.report(worker, result, status="PARTIAL", artifact_refs=[result, other],
                          text="A count was calculated on the supplied records.",
                          gaps=["The archive lacks the corresponding uncertainty estimates."])
        next_lead = s.next_actor("run")
        brief = s.brief(next_lead)
        joins = {r["key"]: r for r in brief["research_joins"]}
        self.assertEqual({k:v["state"] for k,v in joins.items()}, {"count": "SATISFIED", "authored": "SATISFIED",
            "internal": "SATISFIED", "standard_deviation": "UNREACHABLE"})
        evidence = joins["count"]["outcome"]["ports"][0]["evidence"]
        self.assertEqual({e["artifact_ref"] for e in evidence}, {result, other})
        self.assertEqual({e["report_ref"] for e in evidence}, {report["report_ref"]})
        exact, = joins["internal"]["outcome"]["ports"][0]["evidence"]
        self.assertEqual(exact["artifact_ref"], result)
        self.assertEqual(joins["authored"]["outcome"]["ports"][0]["evidence"][0]["source_refs"], [result])
        self.assertIn(result, brief["evidence_refs"])
        packet = actual_packet(s, next_lead)
        self.assertTrue(packet["packet"].get("research_joins") or packet["packet"].get("research_joins_available"))
        if destination := os.getenv("GX_JOIN_RECEIPT"):
            save(Path(destination), {"entry": "gx-p0 research", "transport": "scripted native callbacks",
                 "model_calls": 0, "run_id": s.run_id, "lead_task": next_lead.task_id, "lead_turn": next_lead.turn,
                 "lead_packet_sha256": packet["text_sha256"], "source_report": report["report_ref"],
                 "module_instance_id": opened["module_instance_id"], "isolated_sibling": isolated["module_instance_id"],
                 "measurements": [{**proof, "value": ArtifactService(s.conn).read_json(proof["artifact_ref"])}
                                  for proof in evidence], "joins": joins})
        self.assertEqual(s.conn.execute("SELECT status FROM v62_modules WHERE module_instance_id=?",
                                        (isolated["module_instance_id"],)).fetchone()[0], "OPEN")
        s.advance()
        self.assertEqual(s.conn.execute("SELECT count(*) FROM events WHERE kind='research.join.ready'").fetchone()[0], 4)
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (s.run_id,)).fetchone()[0], 2)
