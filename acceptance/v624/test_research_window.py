"""Durable admission cutoffs. Only the native peer is scripted; budgets, Goal and CPU Jobs use GX."""
from contextlib import closing
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import QUALITY, branch, draft
from gx_core.domain import clock
from gx_core.p0 import manifest as execution_manifest
from gx_core.errors import HoldActive, SchemaInvalid, VersionImmutable
from gx_core.p0 import driver, research_window, v6_decisions, v6_goal, v6_jobs
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import store
from tests import test_p0_root_delivery as native
from tests import test_v6_jobs as jobs


def times():
    now = clock.now().timestamp()
    return dict(start=now-120, new_work_until=now+120, drain_until=now+480, reconcile_until=now+600)


def assign_test_window(conn, rid, window):
    # Synthetic writer-level fixtures bypass the real Goal admission, covered separately below.
    conn.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.research_window',json(?)) "
                 "WHERE run_id=?", (json.dumps(window), rid))


class RegisteredClock(unittest.TestCase):
    def setUp(self):
        self.addCleanup(clock.reset)

    def test_boundaries_and_validation(self):
        window = dict(zip(research_window.DEADLINES, [100, 200, 300, 400]))
        research_window.validate(window)
        self.assertEqual([research_window.phase(window, now=t) for t in (99, 100, 199, 200, 299, 300, 399, 400)],
                         ["BEFORE_START", "OPEN", "OPEN", "DRAIN", "DRAIN", "RECONCILE", "RECONCILE", "ENDED"])
        for bad in (None, {**window, "start": True}, {**window, "drain_until": 200},
                    {**window, "start": float("nan")}, {**window, "reconcile_until": float("inf")},
                    {**window, "relative": True}):
            with self.subTest(bad=bad), self.assertRaises(SchemaInvalid):
                research_window.validate(bad)
        self.assertEqual(research_window.phase(None, now=401), "OPEN")

    def peer(self):
        fixture = native.RootDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        assign_test_window(fixture.conn, fixture.run, times())
        fixture.delivery = RootDelivery(fixture.peer)
        return fixture

    def test_lost_ack_at_cutoff_recovers_and_late_usage_settles_original_once(self):
        f = self.peer()
        clock.advance(119)
        f.peer.drop = True
        with self.assertRaises(EOFError):
            f.delivery.deliver("root", "original work", event_id="original")
        # GX is disconnected after the peer committed the request and before saving its reply.
        db = f.conn.execute("PRAGMA database_list").fetchone()[2]
        f.conn.close()
        clock.advance(2)
        f.conn = store.open_store(Path(db))
        self.addCleanup(f.conn.close)
        f.peer.conn = f.conn
        f.delivery = RootDelivery(f.peer)
        self.assertEqual(research_window.phase(f.delivery.window), "DRAIN")
        f.peer.drop = False
        recovered = f.delivery.deliver("root", "original work", event_id="original")
        self.assertEqual(recovered["turn_id"], "turn-1")
        with self.assertRaises(HoldActive):
            f.delivery.deliver("root", "follow-up steer", event_id="closed-steer")
        self.assertEqual(f.budget()["reserved"], {"api_calls": 1, "tokens": 100})
        clock.advance(400)
        self.assertEqual(research_window.phase(f.delivery.window), "RECONCILE")
        f.finish("turn-1", 16)
        self.assertEqual(f.delivery.deliver("root", "original work", event_id="original"), recovered)
        with self.assertRaises(HoldActive):
            f.delivery.deliver("root", "new start", event_id="closed-start")
        self.assertEqual([method for method, _ in f.peer.sent], ["turn/start"])
        self.assertEqual(f.budget()["spent"], {"api_calls": 1, "tokens": 16})

    def test_cutoff_between_grant_and_wire_releases_unused_hold(self):
        f = self.peer()
        prepare = f.delivery._prepare
        def after_grant(*args):
            receipt = prepare(*args)
            clock.advance(121)
            return receipt
        with patch.object(f.delivery, "_prepare", side_effect=after_grant), self.assertRaises(HoldActive):
            f.delivery.deliver("root", "too late", event_id="grant-before-cutoff")
        self.assertEqual(f.peer.sent, [])
        self.assertFalse(any(f.budget()["reserved"].values()))
        self.assertFalse(any(f.budget()["spent"].values()))
        self.assertEqual(f.delivery._get("grant-before-cutoff")["state"], "ready")

    def test_no_terminal_usage_preserves_hold_and_never_resubmits(self):
        f = self.peer()
        first = f.delivery.deliver("root", "original work", event_id="one")
        clock.advance(605)
        self.assertEqual(research_window.phase(f.delivery.window), "ENDED")
        self.assertEqual(f.delivery.deliver("root", "original work", event_id="one"), first)
        with self.assertRaises(HoldActive):
            f.delivery.deliver("root", "replacement", event_id="two")
        self.assertEqual(f.budget()["reserved"], {"api_calls": 1, "tokens": 100})
        self.assertFalse(any(f.budget()["spent"].values()))
        self.assertEqual(len(f.peer.sent), 1)

    def test_inflight_decision_lands_in_drain_without_new_job_and_reopens_with_original_clock(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state-owner"
        window = times()
        with CloseStudy(directory, spec_updates={"research_window": window, "research_modules": []}) as s:
            actor = s.lead
            node = s.node(actor, "count", s.materials[0], branch_id="local:one", capability=QUALITY)
            route = {**branch("one"), "budget_limits": {"job_submissions": 1, "cpu_seconds": 120}}
            decision = draft(route, node)
            clock.advance(121)
            receipt = s.call(actor, "gx_decide_revision", {"decision": decision}, "original")
            nid = receipt["aliases"]["local:count"]
            s.advance()
            self.assertEqual(s.rows("SELECT status,job_id FROM v6_nodes WHERE node_id=?", nid),
                             [{"status": "PENDING", "job_id": None}])
            self.assertFalse(s.rows("SELECT 1 FROM jobs"))
        with CloseStudy(directory, reopen=True) as s:
            self.assertEqual(research_window.for_run(s.conn, s.run_id), window)
            current = driver.project(s.conn, s.run_id)
            spec = json.loads(current["inputs_json"])["inputs"]["p0_spec"]
            with self.assertRaises(VersionImmutable):
                v6_goal.start(s.conn, {**spec, "research_window":{**window, "drain_until": window["drain_until"]+1}},
                              run_key=current["run_key"], current=current)
            clock.advance(361)
            with closing(driver.Driver(s.conn, s.state, s.run_id)) as d:
                final = d.drive(1)
            self.assertEqual((final["research_window_phase"], final["driver"]), ("RECONCILE", "research_drain_ended"))
            self.assertEqual(final["research_window"], window)
            self.assertEqual(s.call(actor, "gx_decide_revision", {"decision": decision}, "original"), receipt)
            self.assertFalse(s.rows("SELECT 1 FROM jobs"))
            self.assertEqual(len(s.rows("SELECT 1 FROM v6_decisions")), 1)

    def test_cpu_admission_crossing_cutoff_rolls_back_row_and_hold(self):
        f = jobs.DynamicJobs()
        f.setUp()
        self.addCleanup(f.doCleanups)
        assign_test_window(f.conn, f.run_id, times())
        f.new("crossing")
        manifest = execution_manifest.admit

        def after_manifest(*args, **kwargs):
            manifest(*args, **kwargs)
            clock.advance(121)

        with patch.object(execution_manifest, "admit", side_effect=after_manifest) as attempted:
            counts = v6_jobs.advance(f.conn, f.run_id)
        attempted.assert_called_once()
        self.assertEqual((counts["dispatched"], counts["waiting"]), (0, 1))
        self.assertEqual(f.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(f.conn.execute("SELECT count(*) FROM budget_reservations WHERE subject_kind='job'").fetchone()[0], 0)
        self.assertEqual(f.state("crossing")["status"], "PENDING")

    def test_decision_prepared_in_drain_cannot_commit_after_it(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "original-run"
        with CloseStudy(directory, spec_updates={"research_window": times(), "research_modules": []}) as s:
            prepare = v6_decisions.prepare
            clock.advance(121)

            def cross_drain(*args, **kwargs):
                prepared = prepare(*args, **kwargs)
                clock.advance(360)
                return prepared

            with patch.object(v6_decisions, "prepare", side_effect=cross_drain), self.assertRaises(HoldActive):
                s.call(s.lead, "gx_decide_revision", {"decision": draft(branch("one"))}, "cross-drain")
            self.assertFalse(s.rows("SELECT 1 FROM v6_decisions"))
            self.assertFalse(s.rows("SELECT 1 FROM jobs"))

    def test_previously_admitted_cpu_work_settles_across_cutoff(self):
        f = jobs.DynamicJobs()
        f.setUp()
        self.addCleanup(f.doCleanups)
        assign_test_window(f.conn, f.run_id, times())
        f.new("admitted")
        v6_jobs.advance(f.conn, f.run_id)
        initial, = f.conn.execute("SELECT job_id FROM jobs").fetchall()
        f.new("waiting", operation="new-computation")
        clock.advance(121)
        f.connect()
        f.settle("admitted")
        for _ in range(2):
            v6_jobs.advance(f.conn, f.run_id)
        completed = f.conn.execute("SELECT status,job_id,result_ref FROM v6_nodes WHERE node_id='admitted'").fetchone()
        self.assertEqual((completed["status"], completed["job_id"]), ("SUCCEEDED", initial[0]))
        self.assertTrue(completed["result_ref"])
        self.assertEqual(f.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        waiting = f.conn.execute("SELECT status,job_id FROM v6_nodes WHERE node_id='waiting'").fetchone()
        self.assertEqual(tuple(waiting), ("PENDING", None))


if __name__ == "__main__":
    unittest.main()
