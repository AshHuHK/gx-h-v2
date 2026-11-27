"""Ordinary GX admission rechecks a stale official account hold on its original worker."""
import unittest
from contextlib import ExitStack, closing
from unittest.mock import patch

from gx_core.domain import clock
from gx_core.errors import HoldActive
from gx_core.hostobs.app_server_client import AppServerResponseError, ReceivedMessage
from gx_core.p0 import activity, driver
from gx_core.persistence import store
from tests import test_v21_activity


class ProviderRefresh(unittest.TestCase):
    def setUp(self):
        self.fixture = test_v21_activity.ActiveRoot()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.f = self.fixture.f
        f.db.execute("UPDATE p0_roots SET provider='openai' WHERE root_key=?", (f.binding["root_key"],))
        f.binding = f.roots._binding(f.thread)
        self.enterContext(patch("gx_core.hostobs.host_policy.p0_provider_for_run", return_value="openai"))
        self.mono = 10
        self.enterContext(patch.object(driver.time, "monotonic", side_effect=lambda: self.mono))
        fixed = clock.now()
        self.enterContext(patch.object(clock, "_source", return_value=fixed))
        self.reset = int(fixed.timestamp()) + 5 * 86400
        self.reads = []
        self.reply = lambda: self.account(used=0)
        self.stack = self.enterContext(ExitStack())
        self.connect()

    def window(self, used, *, bucket="codex", model=None):
        return {"limitId": bucket, "normalModelSlug": model or self.f.binding["model"],
                "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": self.reset}}

    def account(self, used, *, allowed=True):
        row = self.window(used)
        return {"rateLimits": row, "rateLimitsByLimitId": {"codex": row}, "ordinaryUsageAllowed": allowed}

    def connect(self):
        f = self.f
        f.roots.account_provider = "openai"
        request = f.roots._request

        def official(method, params, *, timeout=None):
            if method == "account/rateLimits/read":
                self.assertFalse(f.db.in_transaction)
                self.assertEqual(params, {"excludeResetCreditDetails": True})
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 5)
                self.reads.append(clock.now_iso())
                return self.reply()
            return request(method, params)

        self.stack.enter_context(patch.object(f.roots, "_request", side_effect=official))
        self.stack.enter_context(patch.object(f.roots, "poll", return_value=False))
        worker = self.stack.enter_context(closing(driver.Driver(f.db, f.directory, f.run)))
        worker._native = f.roots
        worker.offline = False
        # The peer supplies all official RPCs. Keep the real admission, claim, lease,
        # delivery and budget owners; there is no unrelated workflow to advance.
        for name in ("lazy", "drive_science"):
            self.stack.enter_context(patch.object(driver.reactor, name))
        for name in ("deliveries", "reconcile_costs"):
            self.stack.enter_context(patch.object(worker, name, return_value=False))
        self.passes = self.stack.enter_context(closing(worker.passes(self.mono + 3000, shared=True)))
        next(self.passes)

    def tick(self, seconds=1):
        self.mono += seconds
        clock.advance(seconds)
        next(self.passes)

    def prime(self):
        self.fixture.window(**self.window(100))
        # A newer official reading can free this attempt days before resetsAt.
        self.tick(301)
        self.tick()

    def assert_blocked(self, binding):
        with self.assertRaises(HoldActive) as held:
            activity.require_provider_capacity(self.f.db, binding, "next-start")
        self.assertEqual(held.exception.details["reason"], "provider_backoff")

    def test_confirmed_recovery_starts_once_without_releasing_other_buckets(self):
        f = self.f
        other = {**f.binding, "model": "other-model"}
        activity.account_update(f.db, "openai", self.window(100, bucket="additional", model=other["model"]))
        activity.account_update(f.db, "other-provider", self.window(100))
        self.prime()
        self.tick()
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(len(f.peer.sent), 1)
        turn = f.peer.turns[0]["id"]
        row = f.roots.delivery._get("start:" + f.binding["root_key"])
        self.assertEqual((row["run_id"], row["turn_id"], row["state"]), (f.run, turn, "acknowledged"))
        f.assert_owned()
        self.assert_blocked(other)
        self.assert_blocked({**f.binding, "provider": "other-provider"})

    def test_full_or_unreadable_snapshot_remains_bounded_across_restart(self):
        f = self.f
        self.reply = lambda: self.account(used=100)
        self.prime()
        self.assertEqual(len(self.reads), 1)
        self.stack.close()
        f.reopen()
        self.connect()
        self.tick(15)
        self.assertEqual(len(self.reads), 1)

        def unavailable():
            raise AppServerResponseError(ReceivedMessage(1, {"id": 1, "error": {"code": -32000,
                                                                              "message": "temporarily unavailable"}}))

        self.reply = unavailable
        self.tick(301)
        self.tick()
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(f.peer.sent, [])
        self.assert_blocked(f.binding)
        self.stack.close()
        f.reopen()
        self.connect()
        self.tick(15)
        self.assertEqual(len(self.reads), 2)
        self.reply = lambda: self.account(used=0)
        self.tick(301)
        self.tick()
        self.assertEqual((len(self.reads), len(f.peer.sent)), (3, 1))
        f.assert_owned()

    def test_zero_without_included_usage_permission_keeps_original_hold(self):
        f = self.f
        self.reply = lambda: self.account(used=0, allowed=None)
        self.prime()
        self.assertEqual(len(self.reads), 1)
        self.assert_blocked(f.binding)
        self.reply = lambda: self.account(used=0, allowed=False)
        self.tick(301)
        self.assertEqual(len(self.reads), 2)
        self.assert_blocked(f.binding)
        self.assertFalse(f.peer.sent)
        f.assert_owned()

    def test_named_window_takes_precedence_over_other_legacy_snapshot(self):
        f = self.f
        reply = self.account(used=100)
        reply["rateLimits"] = self.window(0, bucket="other", model="different-model")
        self.reply = lambda: reply
        self.prime()
        self.assertEqual(len(self.reads), 1)
        self.assert_blocked(f.binding)
        self.assertFalse(f.peer.sent)

    def test_refusal_observed_during_the_read_remains_enforced(self):
        f = self.f
        with closing(store.open_store(f.directory / "test.db")) as peer:
            def stale_response():
                with store.transaction(peer):
                    activity.rejected_start(peer, f.binding, "newer-refusal")
                return self.account(used=0)

            self.reply = stale_response
            self.prime()
        self.assertEqual(len(self.reads), 1)
        self.assert_blocked(f.binding)
        self.assertFalse(f.peer.sent)
        f.assert_owned()

    def test_account_notification_during_read_wins_for_the_same_bucket(self):
        f = self.f
        with closing(store.open_store(f.directory / "test.db")) as peer:
            def stale_response():
                activity.account_update(peer, "openai", self.window(100))
                return self.account(used=0)

            self.reply = stale_response
            self.prime()
        self.assertEqual(len(self.reads), 1)
        self.assert_blocked(f.binding)
        self.assertFalse(f.peer.sent)
