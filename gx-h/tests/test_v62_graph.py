"""Exercise module-specific graph fences using the ordinary Goal and delegation owners."""
import json
from pathlib import Path
import tempfile
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.errors import BudgetExceeded, ScopeDenied, StaleRevision
from gx_core.p0 import schema, v6_decisions, v6_delegation, v6_goal, v62_modules
from gx_core.persistence import store
from tests.test_v6_decisions import bind_native_fixture, branch, draft


class ModuleGraph(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.db = store.open_store(self.directory / "gx.db")
        self.addCleanup(self.db.close)
        schema.install(self.db)
        path = Path(__file__).resolve().parents[2] / "examples/v62a-data-inventory/grant.json"
        self.spec = json.loads(path.read_bytes())
        for file in self.spec["input_files"]:
            file["path"] = str((path.parent / file["path"]).resolve())
        self.spec.update(mode="goal", objective="Which deposited chains are represented?")

    def start(self, **changes):
        spec = {**self.spec, **changes}
        self.run = v6_goal.start(self.db, spec, run_key="graph", current=None)["run_id"]
        self.lead = bind_native_fixture(self.db, self.run, self.directory)
        self.material = json.loads(self.db.execute(
            "SELECT snapshot_json FROM research_contexts WHERE run_id=?", (self.run,)).fetchone()[0])["materials"][0]["artifact_ref"]

    def module(self, key, *, jobs=1):
        payload = dict(module_id="data_inventory", module_version="1", focus="data",
                       expected_delivery="Describe observed coverage.", round=1, input_refs=[self.material],
                       capabilities=["tl1a-coordinate-quality@1"], operation_key=key,
                       sub_budget={"tokens": 4000, "api_calls": 3, "worker_tasks": 4,
                                   "cpu_seconds": 60, "job_submissions": jobs})
        grant = v62_modules.open_module(self.db, **{**self.lead, "call_id": key}, payload=payload)
        actor = bind_native_fixture(self.db, self.run, self.directory, task_id=grant["task_id"])
        return grant, actor

    def node(self, grant):
        return {"op": "add_node", "branch": grant["branch_id"], "node": "local:measure",
                "capability": "tl1a-coordinate-quality@1", "depends_on": [], "inputs": [self.material],
                "parameters": {}, "question": "What coverage was measured?"}

    def test_module_revalidates_old_empty_base_after_unrelated_lead_revision(self):
        self.start()
        grant, actor = self.module("inventory")
        lead = v6_decisions.adopt(self.db, **self.lead, decision=draft(branch("independent")))
        result = v6_decisions.adopt(self.db, **actor, decision=draft(self.node(grant)))
        self.assertIsNone(result["original_base"])
        self.assertEqual(lead["result_head"], result["adopted_base"])
        self.assertEqual(2, result["result_head"]["version"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM outbox WHERE topic='v6.node'").fetchone()[0])
        self.assertEqual(result, v6_decisions.adopt(self.db, **actor, decision=draft(self.node(grant))))

    def test_reserved_branch_cannot_be_used_by_a_peer_and_open_instance_fences_closure(self):
        self.start()
        grant, actor = self.module("left")
        other, peer = self.module("right")
        with self.assertRaises(ScopeDenied):
            v6_decisions.adopt(self.db, **peer, decision=draft(self.node(grant)))
        close = draft({"op": "close_run", "reason": "done", "claim_refs": [], "unresolved": []})
        for identity in (actor, self.lead):
            with self.subTest(scope=identity["task_id"]), self.assertRaises(ScopeDenied):
                v6_decisions.adopt(self.db, **identity, decision=close)
        self.assertIsNone(v6_decisions.head(self.db, self.run))
        self.assertNotEqual(grant["branch_id"], other["branch_id"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM v6_decisions").fetchone()[0])

    def test_run_lead_cannot_spend_or_redelegate_the_materialized_module_branch(self):
        self.start()
        grant, actor = self.module("inventory")
        v6_decisions.adopt(self.db, **self.lead, decision=draft())
        v6_decisions.adopt(self.db, **actor, decision=draft(self.node(grant)))
        source = ArtifactService(self.db).put(run_id=self.run, content='{"instrument":"new"}',
            kind="observation", media_type="application/json", put_by="test", origin="INGEST").artifact_ref
        v6_goal.record_observation(self.db, self.run, source, source="independent-observation")
        task = v6_goal.ensure_task(self.db, self.run)
        self.assertIsNotNone(task)
        parent = bind_native_fixture(self.db, self.run, self.directory, task_id=task)
        with self.assertRaises(ScopeDenied):
            v6_decisions.adopt(self.db, **parent, decision=draft(self.node(grant)))
        with self.assertRaises(ScopeDenied):
            v6_delegation.prepare(self.db, **parent, payload={"branch_id": grant["branch_id"],
                                                             "question": "Use the outstanding module budget"})
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM outbox WHERE topic='v6.node'").fetchone()[0])
        self.assertEqual("OPEN", self.db.execute("SELECT status FROM v62_modules").fetchone()[0])

    def test_same_prepared_base_competing_writes_require_fresh_module_snapshot(self):
        self.start()
        left, a = self.module("left")
        right, b = self.module("right")
        winner = v6_decisions.prepare(self.db, **a, decision=draft(self.node(left)))
        loser = v6_decisions.prepare(self.db, **b, decision=draft(self.node(right)))
        v6_decisions.commit(self.db, winner)
        with self.assertRaises(StaleRevision) as refusal:
            v6_decisions.commit(self.db, loser)
        v6_decisions.reconsider_module_conflict(self.db, b, refusal.exception)
        self.assertEqual(self.db.execute("SELECT status FROM tasks WHERE task_id=?", (b["task_id"],)).fetchone()[0], "CANCELLED")
        self.assertEqual(self.db.execute("SELECT status FROM v62_modules WHERE scope=?", (right["scope"],)).fetchone()[0], "OPEN")
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE topic='v6.node'").fetchone()[0], 1)

    def test_human_first_graph_limit_applies_to_module_first_commit(self):
        self.start(initial_job_limit=0)
        grant, actor = self.module("inventory")
        with self.assertRaises(BudgetExceeded):
            v6_decisions.adopt(self.db, **actor, decision=draft(self.node(grant)))
        self.assertIsNone(v6_decisions.head(self.db, self.run))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
