"""Internal owner tests. From the candidate repo: PYTHONPATH=.:gx-h python -B /path/to/this.py -v."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario

from gx_core.errors import ScopeDenied, StaleRevision
from gx_core.p0 import v6_claims, v6_decisions, v6_goal, v62_modules
from gx_core.persistence import store
from tests import test_v62_graph as fixture
from tests.test_v6_decisions import bind_native_fixture, branch, draft


class Base(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ModuleGraph()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.start()

    def cold(self):
        self.f.db.close()
        self.f.db = store.open_store(self.f.directory / "gx.db")
        self.addCleanup(self.f.db.close)
        return self.f.db

    def worker_write(self, grant, actor):
        return v6_decisions.adopt(self.f.db, **actor, decision=draft(self.f.node(grant)))

    def graph(self):
        head = v6_decisions.head(self.f.db, self.f.run)
        return json.loads(self.f.db.execute(
            "SELECT graph_json FROM v6_graph_versions WHERE run_id=? AND sha256=?",
            (self.f.run, head["sha256"]),
        ).fetchone()[0])

    def stage(self):
        db = self.f.db
        actor = self.f.lead
        context = db.execute(
            "SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (actor["task_id"],)
        ).fetchone()[0]
        material = self.f.material
        payload = dict(
            level="open", statement="A follow-up sample has detectable coverage.",
            scope="The registered source in the follow-up sample.",
            criteria="Apply the registered threshold to the coverage measurement.",
            limitations="Only this sample and observation apply.",
            rationale="Register the rule before follow-up measurements are available.",
            evidence_refs=[material],
            prediction=dict(target="experimental_observation", data_refs=[material],
                            observable="/measurement/value", operator="gt", threshold=0.0,
                            measurement_schema="follow-up.coverage/1"),
        )
        return v6_claims.stage(db, **dict(actor, call_id="register"),
                               context_ref=context, allowed_refs=[material], payload=payload)


class OpeningSemantics(Base):
    def test_cold_same_parent_can_finish_opening_round(self):
        first, worker = self.f.module("left")
        write = self.worker_write(first, worker)
        db = self.cold()
        v6_goal.ensure_task(db, self.f.run)
        second, _ = self.f.module("right")
        self.assertEqual(second["parent_task_id"], first["parent_task_id"])
        self.assertNotEqual(second["branch_id"], first["branch_id"])
        v6_decisions.adopt(db, **self.f.lead, decision=draft())
        self.assertIn(write["aliases"]["local:measure"], self.graph()["nodes"])
        self.assertEqual(2, db.execute(
            "SELECT count(*) FROM v62_modules WHERE run_id=? AND round=1 AND parent_task_id=?",
            (self.f.run, self.f.lead["task_id"]),
        ).fetchone()[0])

    def test_scientific_registration_survives_private_write_and_replays_once(self):
        first, worker = self.f.module("left")
        self.f.module("right")
        proposal = self.stage()
        self.assertEqual([], v6_claims.current(self.f.db, self.f.run))
        work = self.worker_write(first, worker)
        db = self.cold()
        v6_goal.ensure_task(db, self.f.run)
        decision = dict(draft(), evidence_refs=[self.f.material, proposal["claim_ref"]])
        saved = v6_decisions.adopt(db, **self.f.lead, decision=decision)
        self.assertEqual(saved, v6_decisions.adopt(db, **self.f.lead, decision=decision))
        claim, = v6_claims.current(db, self.f.run)
        self.assertEqual(proposal["claim_ref"], claim["claim_ref"])
        self.assertEqual("preregistered", claim["maturity"]["stage"])
        self.assertEqual(1, db.execute(
            "SELECT count(*) FROM events WHERE run_id=? AND kind='v6.claim.adopted'", (self.f.run,),
        ).fetchone()[0])
        self.assertIn(work["aliases"]["local:measure"], self.graph()["nodes"])


class EarlyWakeSemantics(unittest.TestCase):
    def test_early_release_is_still_usable_by_same_awakened_lead_after_sibling_write(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        scenario = self.enterContext(Scenario(Path(directory) / "run"))
        source = scenario.materials[0]
        watcher = scenario.open("watched", inputs=[source], wake_on=["counterevidence"])
        peer = scenario.open("sibling", inputs=[source])
        a, b = (scenario.bind(grant["task_id"]) for grant in (watcher, peer))
        opening = scenario.lead.task_id
        scenario.call(scenario.lead, "gx_decide_revision", {"decision": draft()}, "finish-open")
        scenario.call(a, "gx_read_artifact", {"ref": source, "operation_key": "read"}, "read")
        finding = {"text": "The scoped source contradicts the fixture's assumption.",
                   "source_refs": [source], "kind": "counterevidence"}
        report = scenario.report(a, source, findings=[finding])
        ref = report["report_ref"]
        wake, = scenario.rows(
            "SELECT payload_json FROM events WHERE kind='v62.module.wake' AND event_id=?",
            report["wake_event_id"],
        )
        self.assertEqual("counterevidence", json.loads(wake["payload_json"])["reason"])
        awakened = scenario.next_actor("run")
        self.assertNotEqual(opening, awakened.task_id)
        self.assertIn(ref, scenario.brief(awakened)["evidence_refs"])
        delivered, = scenario.rows("SELECT brief FROM p0_roots WHERE lease_id=?", awakened.lease_id)
        self.assertIn(ref, delivered["brief"])
        scenario.submit_node(b, "peer-measurement")
        v6_goal.ensure_task(scenario.conn, scenario.run_id)
        scenario.call(awakened, "gx_decide_revision",
                      {"decision": dict(draft(), evidence_refs=[ref])}, "use-early-report")
        decision, = scenario.rows(
            "SELECT request_json FROM v6_decisions WHERE task_id=? AND turn_id=?",
            awakened.task_id, awakened.turn,
        )
        self.assertIn(ref, json.loads(decision["request_json"])["decision"]["evidence_refs"])


class ExistingGuards(Base):
    def test_registration_control_without_intervening_child_write(self):
        proposal = self.stage()
        decision = dict(draft(), evidence_refs=[self.f.material, proposal["claim_ref"]])
        v6_decisions.adopt(self.f.db, **self.f.lead, decision=decision)
        claim, = v6_claims.current(self.f.db, self.f.run)
        self.assertEqual(proposal["claim_ref"], claim["claim_ref"])
        self.assertEqual("preregistered", claim["maturity"]["stage"])

    def test_open_module_blocks_already_prepared_close_even_without_head_change(self):
        db = self.f.db
        close = draft(dict(op="close_run", reason="Close after work is settled.", claim_refs=[], unresolved=[]))
        plan = v6_decisions.prepare(db, **self.f.lead, decision=close)
        self.f.module("intervening")
        self.assertIsNone(v6_decisions.head(db, self.f.run))
        with self.assertRaises(ScopeDenied):
            v6_decisions.commit(db, plan)
        self.assertIsNone(v6_decisions.head(db, self.f.run))
        self.assertEqual("OPEN", db.execute(
            "SELECT status FROM v6_goals WHERE run_id=?", (self.f.run,)
        ).fetchone()[0])

    def test_old_lead_amend_never_erases_durable_child_node(self):
        first, worker = self.f.module("left")
        db = self.f.db
        plan = v6_decisions.prepare(db, **self.f.lead, decision=draft(branch("independent")))
        saved = self.worker_write(first, worker)
        child_id = saved["aliases"]["local:measure"]
        try:
            lead_result = v6_decisions.commit(db, plan)
        except (ScopeDenied, StaleRevision):
            self.assertEqual(saved["result_head"], v6_decisions.head(db, self.f.run))
        else:
            self.assertIn(lead_result["aliases"]["local:independent"], self.graph()["branches"])
            self.assertEqual(saved["result_head"], self.graph()["parent"])
        self.assertIn(child_id, self.graph()["nodes"])

    def test_module_retry_from_cold_state_converges_without_duplicate_dispatch(self):
        db = self.f.db
        left, a = self.f.module("left")
        right, b = self.f.module("right")
        v6_decisions.adopt(db, **self.f.lead, decision=draft())
        plan_a = v6_decisions.prepare(db, **a, decision=draft(self.f.node(left)))
        plan_b = v6_decisions.prepare(db, **b, decision=draft(self.f.node(right)))
        saved_a = v6_decisions.commit(db, plan_a)
        try:
            saved_b = v6_decisions.commit(db, plan_b)
        except StaleRevision as error:
            v6_decisions.reconsider_module_conflict(db, b, error)
            db = self.cold()
            v62_modules.advance(db, self.f.run)
            new_task = db.execute(
                "SELECT task_id FROM v6_decision_tasks JOIN tasks USING(task_id) "
                "WHERE v6_decision_tasks.run_id=? AND scope=? AND status='READY'",
                (self.f.run, right["scope"]),
            ).fetchone()
            self.assertIsNotNone(new_task, "the right module must continue after reconnect")
            b = bind_native_fixture(db, self.f.run, self.f.directory, task_id=new_task[0])
            saved_b = self.worker_write(right, b)
        db = self.cold()
        self.assertEqual(saved_b, self.worker_write(right, b))
        expected = {item["aliases"]["local:measure"] for item in (saved_a, saved_b)}
        self.assertEqual(expected, set(self.graph()["nodes"]))
        self.assertEqual(2, db.execute("SELECT count(*) FROM outbox WHERE topic='v6.node'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
