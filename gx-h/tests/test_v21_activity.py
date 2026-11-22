"""Official-shaped events through Roots, a reopened SQLite database and the real lease owner."""
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import unittest

from gx_core.domain import clock
from gx_core.errors import HoldActive, LeaseExpired, ScopeDenied
from gx_core.hostobs.app_server_client import AppServerResponseError, ReceivedMessage
from gx_core import budgets
from gx_core.p0 import activity, waits
from gx_core.p0.roots import require_binding
from gx_core.persistence import records, store
from gx_core.teams.service import default_lease_seconds
from tests import test_p0_wait_leases


class ActiveRoot(unittest.TestCase):
    def setUp(self):
        self.f = test_p0_wait_leases.WaitLeases()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.roots.account_provider = self.f.binding["provider"]
        self.duration = default_lease_seconds()

    def emit(self, method, **params):
        self.f.roots._observe(SimpleNamespace(message={"method":method, "params":params}))

    def start(self):
        turn = self.f.roots.dispatch(self.f.task)["turn_id"]
        self.emit("turn/started", threadId=self.f.thread, turn={"id":turn, "status":"inProgress"})
        return turn

    def usage(self, turn, n):
        self.emit("thread/tokenUsage/updated", threadId=self.f.thread, turnId=turn,
                  tokenUsage={"total":{"totalTokens":n}})

    def window(self, **snapshot):
        self.emit("account/rateLimits/updated", rateLimits={"limitId":"codex", **snapshot})

    def renewals(self):
        return [json.loads(r[0]) for r in self.f.db.execute("SELECT payload_json FROM events"
                    " WHERE kind='task.lease_renewed' AND subject_id=? ORDER BY event_id", (self.f.task,))]

    def test_activity_extends_one_authoritative_attempt_across_three_lease_periods(self):
        turn = self.start()
        original = self.f.expiry()
        for step in range(1, 13):
            clock.advance(self.duration / 4)
            self.usage(turn, step)
            self.assertEqual(require_binding(self.f.db, self.f.run, self.f.thread)["lease_id"], self.f.binding["lease_id"])
        self.assertGreater(self.f.expiry(), original)
        self.assertEqual(len(self.renewals()), 6)
        self.assertEqual({r["lease_id"] for r in self.renewals()}, {self.f.binding["lease_id"]})
        self.f.assert_owned()

    def test_quiet_turn_requires_a_fresh_official_snapshot_outside_sqlite(self):
        turn = self.start()
        clock.advance(self.duration * 0.7)
        self.f.roots.heartbeats.poll()
        self.assertEqual(len(self.renewals()), 1)
        clock.advance(self.duration * 0.7)
        self.f.peer.turns[0]["status"] = "completed"
        self.f.roots.heartbeats.next_probe = 0
        self.f.roots.heartbeats.poll()
        self.assertEqual(len(self.renewals()), 1)
        self.assertEqual(self.f.roots.active_turns.get(self.f.thread), turn)

    def test_wrong_turn_expiry_and_intent_fence_each_reject_a_heartbeat(self):
        turn = self.start()
        clock.advance(self.duration * 0.7)
        self.usage("unrelated", 3)
        self.assertFalse(self.renewals())
        self.f.db.execute("UPDATE leases SET principal='another-thread' WHERE lease_id=?", (self.f.binding["lease_id"],))
        self.usage(turn, 5)
        self.assertFalse(self.renewals())
        self.f.db.execute("UPDATE leases SET principal=? WHERE lease_id=?", (self.f.thread,self.f.binding["lease_id"]))
        clock.advance(self.duration)
        self.usage(turn, 6)
        self.assertFalse(self.renewals())
        with self.assertRaises(LeaseExpired):
            require_binding(self.f.db, self.f.run, self.f.thread)
        self.f.roots._claim(self.f.task)
        self.usage(turn, 7)
        self.assertFalse(self.renewals())
        with self.assertRaises(ScopeDenied):
            require_binding(self.f.db, self.f.run, self.f.thread)

    def test_parked_job_and_paused_run_keep_wait_protection_without_active_heartbeat(self):
        turn = self.start()
        self.f.job()
        with store.transaction(self.f.db):
            store.transition(self.f.db, machine="run", subject_id=self.f.run, to_state="PAUSED", actor="operator")
        clock.advance(self.duration * 0.8)
        self.usage(turn, 5)
        self.assertFalse(self.renewals())
        self.assertTrue(waits.is_waiting(self.f.db, self.f.binding["root_key"]))
        self.f.assert_owned()

    def test_sparse_quota_windows_survive_reopen_and_only_pause_their_provider(self):
        f = self.f
        now = int(clock.now().timestamp())
        self.window(primary={"usedPercent":100, "resetsAt":now+2*self.duration})
        self.window(secondary={"usedPercent":20, "resetsAt":None})
        with self.assertRaises(HoldActive) as denial:
            f.roots.dispatch(f.task)
        self.assertEqual(denial.exception.details["reason"], "provider_backoff")
        self.assertFalse(f.peer.sent)
        self.assertTrue(waits.is_waiting(f.db, f.binding["root_key"]))
        activity.require_provider_capacity(f.db, {**f.binding,"provider":"another"}, "elsewhere")
        sibling = records.create_run(f.db, objective="independent experiment", project_ref="test", actor="test")
        with self.assertRaises(HoldActive):
            activity.require_provider_capacity(f.db, {**f.binding,"run_id":sibling}, "other-run")
        f.reopen()
        clock.advance(self.duration+10)
        with self.assertRaises(HoldActive):
            f.roots.dispatch(f.task)
        f.assert_owned()
        clock.advance(self.duration)
        turn = f.roots.dispatch(f.task)["turn_id"]
        self.assertTrue(turn)
        self.assertEqual(len(f.peer.sent), 1)
        self.assertFalse(waits.is_waiting(f.db, f.binding["root_key"]))
        f.assert_owned()

    def test_full_window_without_reset_allows_a_bounded_probe_after_restart(self):
        f = self.f
        self.window(primary={"usedPercent":100,"resetsAt":None})
        with self.assertRaises(HoldActive) as denied:
            activity.require_provider_capacity(f.db, f.binding, "next")
        reset = denied.exception.details["resets_at"]
        f.reopen()
        clock.advance(10)
        f.roots.account_provider = f.binding["provider"]
        self.window(primary={"usedPercent":100,"resetsAt":None})
        with self.assertRaises(HoldActive) as replayed:
            activity.require_provider_capacity(f.db, f.binding, "next")
        self.assertEqual(reset, replayed.exception.details["resets_at"])
        clock.advance(reset-clock.now().timestamp()+1)
        activity.require_provider_capacity(f.db, f.binding, "next")

    def test_new_unknown_provider_cap_and_saved_quota_are_independent(self):
        f = self.f
        f.db.execute("UPDATE runs SET agent_policy_json=? WHERE run_id=?",
                     (json.dumps({"p0_unknown_provider_parallel_turns":1}), f.run))
        self.start()
        with self.assertRaises(HoldActive):
            activity.require_provider_capacity(f.db, f.binding, "another-turn")
        f.db.execute("UPDATE runs SET inputs_json=? WHERE run_id=?",
                     (json.dumps({"inputs":{"p0":True,"p0_spec":{"max_parallel_roots":2}}}), f.run))
        activity.require_provider_capacity(f.db, f.binding, "another-turn")
        self.window(primary={"usedPercent":100,"resetsAt":int(clock.now().timestamp())+120})
        with self.assertRaises(HoldActive):
            activity.require_provider_capacity(f.db, f.binding, "another-turn")

    def test_full_provider_leaves_new_task_ready_without_creating_a_root(self):
        f = self.f
        self.window(primary={"usedPercent":100, "resetsAt":int(clock.now().timestamp())+600})
        new = records.create_task(f.db, run_id=f.run, actor="test", kind="read", role="evidence_reviewer")
        store.transition(f.db, machine="task", subject_id=new, to_state="READY", actor="backend")
        with self.assertRaises(HoldActive):
            f.roots._claim(new)
        row = f.db.execute("SELECT status,lease_id,attempt,attempts_consumed FROM tasks WHERE task_id=?", (new,)).fetchone()
        self.assertEqual(tuple(row), ("READY",None,0,0))
        self.assertIsNone(f.db.execute("SELECT root_key FROM p0_roots WHERE task_id=?", (new,)).fetchone())

    def test_terminal_429_defers_same_owner_without_agent_failure_on_replayed_callback(self):
        f = self.f
        turn = self.start()
        item = f.peer.turns[0]
        item.update(status="failed", error={"message":"provider limit", "codexErrorInfo":{"httpConnectionFailed":{"httpStatusCode":429}}})
        self.emit("turn/completed", threadId=f.thread, turn=item)
        self.emit("turn/completed", threadId=f.thread, turn=item)
        self.assertTrue(waits.deferred_turn(f.db, f.binding["root_key"], turn))
        self.assertEqual(f.db.execute("SELECT count(*) FROM p0_provider_limits WHERE source='turn/completed'").fetchone()[0], 1)
        self.assertEqual(f.db.execute("SELECT count(*) FROM p0_attempt_waits WHERE reason='provider_backoff'").fetchone()[0], 1)
        f.assert_owned()
        with self.assertRaises(HoldActive) as denied:
            activity.require_provider_capacity(f.db, f.binding, "next")
        self.assertEqual(denied.exception.details["reason"], "provider_backoff")
        self.assertFalse(activity.rate_refusal({"codexErrorInfo":{"httpConnectionFailed":{"httpStatusCode":401}}}))

    def test_post_fence_replay_only_reads_the_original_acknowledged_wake(self):
        f = self.f
        f.job()
        f.finish_job()
        receipt = f.roots.wake_job(f.task, "external-1")
        store.release_lease(f.db, f.binding["lease_id"])
        calls, changed = len(f.peer.sent), f.db.total_changes
        self.assertEqual(f.roots.wake_job(f.task, "external-1"), receipt)
        self.assertEqual((len(f.peer.sent), f.db.total_changes), (calls, changed))
        with self.assertRaises((ScopeDenied, LeaseExpired)):
            require_binding(f.db, f.run, f.thread)

    def test_explicit_start_quota_refusal_releases_hold_and_retries_the_original_delivery(self):
        f = self.f
        request = f.roots._request
        def limited(method, params):
            if method == "turn/start":
                raise AppServerResponseError(ReceivedMessage(1, {"id":1, "error":{"code":-32000,
                        "message":"limit", "data":{"codexErrorInfo":"rateLimitExceeded"}}}))
            return request(method, params)
        f.roots._request = limited
        with self.assertRaises(HoldActive) as denied:
            f.roots.dispatch(f.task)
        self.assertEqual(denied.exception.details["reason"], "provider_backoff")
        original = f.roots.delivery._get("start:" + f.binding["root_key"])
        self.assertEqual((original["state"], original["reservation_ref"]), ("ready", None))
        self.assertEqual(budgets.state(f.db, f.budget)["reserved"], {})
        self.assertTrue(waits.is_waiting(f.db, f.binding["root_key"]))
        f.reopen()
        clock.advance(self.duration+1)
        self.assertTrue(f.roots.dispatch(f.task)["turn_id"])
        replay = f.roots.delivery._get(original["event_id"])
        self.assertEqual((replay["state"], replay["client_id"]), ("acknowledged", original["client_id"]))
        self.assertEqual(len(f.peer.sent), 1)
        f.assert_owned()

    def test_official_system_error_after_quota_only_retries_its_last_acknowledged_turn(self):
        f = self.f
        turn = self.start()
        item = f.peer.turns[0]
        item.update(status="failed", error={"codexErrorInfo":"rateLimitExceeded"})
        self.emit("turn/completed", threadId=f.thread, turn=item)
        request = f.roots._request
        def history(method, params):
            if method == "thread/read":
                return {"thread":{"id":f.thread, "status":{"type":"systemError"}}}
            return request(method, params)
        f.roots._request = history
        self.assertIsNone(f.roots.delivery.active(f.thread))
        with self.assertRaises(HoldActive):
            activity.require_provider_capacity(f.db, f.binding, "resume:"+turn)
        item["id"] = "another-failed-turn"
        with self.assertRaises(HoldActive):
            f.roots.delivery.active(f.thread)
        item["id"] = turn
        f.db.execute("UPDATE p0_root_deliveries SET state='sending' WHERE thread_id=?", (f.thread,))
        with self.assertRaises(HoldActive):
            f.roots.delivery.active(f.thread)

    def test_official_quota_terminal_before_start_ack_survives_cold_history(self):
        f = self.f
        turn = self.start()
        key = "start:" + f.binding["root_key"]
        f.db.execute("UPDATE p0_root_deliveries SET state='sending',turn_id=NULL WHERE run_id=? AND event_id=?",
                     (f.run,key))
        ended = {"id":turn, "status":"failed", "error":{"codexErrorInfo":"usageLimitExceeded"}}
        self.emit("turn/completed", threadId=f.thread, turn=ended)
        self.assertTrue(waits.deferred_turn(f.db, f.binding["root_key"], turn))
        with store.transaction(f.db):
            f.roots.delivery._ack(key, turn)
        f.reopen()
        f.roots._ended_turn(f.thread, {"id":turn,"status":"failed"})
        self.assertEqual(f.db.execute("SELECT count(*) FROM p0_attempt_waits WHERE reason='provider_backoff'").fetchone()[0], 1)
        f.assert_owned()

    def test_opt_outs_and_required_notifications_exist_in_the_locked_official_schema(self):
        archive = Path(__file__).resolve().parents[2] / "release-assets/codex-schema-v0.169.0-experimental.tar.gz"
        with tarfile.open(archive) as source:
            def methods(name):
                variants = json.load(source.extractfile("./" + name + ".json"))["oneOf"]
                return {v["properties"]["method"]["enum"][0] for v in variants}
            negotiated = json.load(source.extractfile("./v1/InitializeParams.json"))
            self.assertIn("optOutNotificationMethods", negotiated["definitions"]["InitializeCapabilities"]["properties"])
            notifications = methods("ServerNotification")
            self.assertIn("item/tool/call", methods("ServerRequest"))
        required = {"turn/started","turn/completed","thread/started","thread/tokenUsage/updated",
                    "account/rateLimits/updated","item/started","item/completed","error"}
        self.assertTrue(set(activity.OPT_OUT) <= notifications)
        self.assertTrue(required <= notifications)
        self.assertTrue(required.isdisjoint(activity.OPT_OUT))
