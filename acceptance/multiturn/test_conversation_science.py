"""Main recovers the public justification by author, route, version and event time."""
import json
from pathlib import Path
import tempfile
import unittest

from gx_core.errors import SchemaInvalid
from gx_core.orchestration.run_mode import GOAL
from gx_core.p0 import conversation_science, v6_decisions
from gx_core.persistence import store
from tests import test_v62_graph as fixture
from tests.test_v6_decisions import branch, draft


class ScientificMemory(unittest.TestCase):
    def test_main_retrieves_commissioned_route_and_lead_reason_after_cold_reopen(self):
        case = fixture.ModuleGraph()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.start()
        grant, worker = case.module("coverage")
        decision = draft(branch())
        decision["rationale"] = "Retain a separate comparison to expose disagreement between coordinates."
        first = v6_decisions.adopt(case.db, **case.lead, decision=decision)
        private = draft(case.node(grant))
        private["rationale"] = "Private reasoning by the module before it releases a report."
        later = v6_decisions.adopt(case.db, **worker, decision=private)
        case.db.close()
        with store.open_store(case.directory / "gx.db") as db:
            self.addCleanup(db.close)
            self.assertEqual(GOAL.latest_decision(db, case.run, [])["decision_ref"], first["decision_ref"])
            branch_id = first["resolved_ops"][0]["spec"]["branch"]
            view = conversation_science.project(db, case.run, branch_id=branch_id)
            found, = view["lead_decisions"]
            self.assertEqual((found["rationale"], found["author_scope"]), (decision["rationale"], "run"))
            self.assertTrue(found["route_evidence"]["operations_on_route"])
            self.assertNotIn(private["rationale"], json.dumps(view))
            with self.assertRaises(SchemaInvalid):
                conversation_science.project(db, case.run, decision_ref=later["decision_ref"])
            with self.assertRaises(SchemaInvalid):
                conversation_science.project(db, case.run, branch_id=branch_id, before_event=first["event_id"]-1)
            self.assertEqual(conversation_science.project(db, case.run, branch_id=grant["scope"])["released_reports"], [])

    def test_source_is_attributed_to_released_report_only_after_its_actual_wake(self):
        from acceptance.v622.test_close import CloseStudy
        from acceptance.v62a.interleavings_fixture import draft as lead_draft
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "research"
        with CloseStudy(path) as study:
            source = study.materials[-1]
            module = study.open("inventory", round=1, inputs=[source], caps=[])
            worker = study.bind(module["task_id"])
            study.call(worker, "gx_read_artifact", {"ref":source, "operation_key":"read"}, "read")
            study.call(study.lead, "gx_decide_revision", {"decision":lead_draft()}, "wait")
            published = study.report(worker, source, text="The supplied inventory was inspected.")
            view = conversation_science.project(study.conn, study.run_id, branch_id=module["scope"])
            report, = view["released_reports"]
            self.assertEqual(report["report_ref"], published["report_ref"])
            self.assertEqual(report["author_scope"], module["scope"])
            self.assertEqual(report["findings"][0]["source_refs"], [source])
            previous = conversation_science.project(study.conn, study.run_id,
                before_event=report["release_event"]-1)
            self.assertEqual(previous["released_reports"], [])
