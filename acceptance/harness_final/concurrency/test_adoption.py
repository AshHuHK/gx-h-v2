"""Interleave real native callbacks on two SQLite connections to the same ordinary research Goal."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness_refactor.modules.research_study import ResearchStudy
from acceptance.v62a.interleavings_fixture import branch, draft
from gx_core.p0 import v6_decisions, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class ConcurrentDecision(unittest.TestCase):
    def test_worker_amendment_between_lead_prepare_and_adoption(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        spec = {"research_effort": "deep", "research_modules": ["ad_hoc@1"],
                "capabilities": ["fixture-grid@1"]}
        lead_study = self.enterContext(ResearchStudy(directory, spec_updates=spec))
        lead = lead_study.lead
        grant = lead_study.open_args("archive", caps=["fixture-grid@1"], inputs=lead_study.materials)
        grant.update(module_id="ad_hoc", focus="data", expected_delivery="Measure archive coverage.")
        worker_scope = lead_study.call(lead, "gx_open_module", grant, "commission")["scope"]
        # One Driver owns the state directory. Its second native session writes through
        # another connection at the point between the first prepare and SQLite adoption.
        other = ResearchStudy(directory, reopen=True)
        other.conn = self.enterContext(closing(store.open_store(other.state / "gx.db")))
        other.run_id, other.materials = lead_study.run_id, lead_study.materials
        other.roots = Roots(other.conn, run_id=other.run_id, cwd=other.state, token_budget=10)
        other.roots.client = lead_study.roots.client
        worker = other.next_actor(worker_scope)
        worker_draft = draft(other.node(worker, "measure", capability="fixture-grid@1",
                                        parameters={"operation": "same"}))
        lead_draft = draft(branch("independent"))
        prepare = v6_decisions.prepare
        interleaving = []

        def concurrent_write(conn, **args):
            self.assertFalse(conn.in_transaction)
            plan = prepare(conn, **args)
            if conn is lead_study.conn and not interleaving:
                interleaving.append(other.call(worker, v6_native.DECIDE, {"decision": worker_draft}, "worker-write"))
            return plan

        with patch.object(v6_decisions, "prepare", side_effect=concurrent_write):
            adopted = lead_study.call(lead, v6_native.DECIDE, {"decision": lead_draft}, "lead-write")
        self.assertEqual(adopted, lead_study.call(lead, v6_native.DECIDE, {"decision": lead_draft}, "lead-write"))
        self.assertEqual(adopted["base"], interleaving[0]["result_head"])
        self.assertEqual(adopted["result_head"]["version"], 2)
        graph = json.loads(lead_study.rows("SELECT graph_json FROM v6_graph_versions WHERE version=2")[0]["graph_json"])
        self.assertIn(interleaving[0]["aliases"]["local:measure"], graph["nodes"])
        self.assertEqual(graph["branches"][adopted["aliases"]["local:independent"]]["hypothesis"],
                         lead_draft["ops"][0]["hypothesis"])
        self.assertEqual(len(lead_study.rows("SELECT 1 FROM outbox WHERE topic='v6.node'")), 1)
        self.assertEqual(len(lead_study.rows("SELECT 1 FROM v6_decisions")), 2)
        self.assertFalse(lead_study.rows("SELECT 1 FROM events WHERE kind='v624.decision.rejected'"))


if __name__ == "__main__":
    unittest.main()
