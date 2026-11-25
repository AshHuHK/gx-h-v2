"""Stock subscription route admission, role-private auth reference and same-slot recovery."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v623.oauth import host


def snapshot(*, included=True, used=40, has_credits=False, balance="0", **extra):
    return {"ordinaryUsageAllowed": included, "rateLimits": {
        "limitId":"codex", "primary":{"usedPercent":used}, "credits":{
            "hasCredits":has_credits, "balance":balance, "unlimited":False}, **extra}}


class SubscriptionGate(unittest.TestCase):
    def test_included_limits_and_no_additional_credit_route(self):
        with self.subTest("included"):
            self.assertEqual(host._rates(snapshot(), "native")[0], [])
        for response, code in (
            (snapshot(included=None), "INCLUDED_USAGE_UNKNOWN"),
            (snapshot(included=False), "INCLUDED_USAGE_DISABLED"),
            (snapshot(used=100), "INCLUDED_RATE_LIMIT_REACHED"),
            (snapshot(has_credits=True), "ADDITIONAL_CREDIT_ROUTE_AVAILABLE"),
            (snapshot(balance="2.01"), "ADDITIONAL_CREDIT_ROUTE_AVAILABLE"),
            (snapshot(balance="not a number"), "CREDIT_ROUTE_UNVERIFIED"),
            (snapshot(rateLimitReachedType="workspaceOwnerCreditsDepleted"), "ADDITIONAL_CREDITS_REQUIRED"),
            (snapshot(spendControlReached=True), "PROVIDER_LIMIT_REACHED"),
        ):
            with self.subTest(code=code):
                self.assertIn(code, host._rates(response, "native")[0])
        additional = snapshot()
        additional["rateLimitsByLimitId"] = {"chosen-limit": {
            "normalModelSlug":"native", "secondary":{"usedPercent":100}}}
        self.assertIn("INCLUDED_RATE_LIMIT_REACHED", host._rates(additional, "native")[0])

    def fixture(self, lifecycle):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        real = tmp / "official-home"
        real.mkdir(mode=0o700)
        anchor = real / "auth.json"
        anchor.touch(mode=0o600)  # The host treats this only as an official auth-store path.
        self.enterContext(patch.dict(os.environ, {"HOME":str(tmp), "CODEX_HOME":str(real),
            "OPENAI_API_KEY":"unused-fixture", "CODEX_API_KEY":"unused-fixture",
            "OPENAI_BASE_URL":"http://invalid.invalid", "GX_P0_TEST":"1"}))
        self.enterContext(patch.object(host, "_pin", return_value=(Path("/local/pinned"), "digest")))
        self.enterContext(patch.object(host, "_proxy", return_value={"HTTPS_PROXY":"http://127.0.0.1:7890"}))
        self.enterContext(patch.object(host, "_inspect", side_effect=lifecycle))
        return tmp / "slot", anchor

    def test_role_home_outside_exported_slot_and_original_store_survives_reopen(self):
        ready = {"status":"READY","reason_codes":[],"account_ref":"fixed","subscription_usage_state":"INCLUDED_AVAILABLE"}
        slot, original = self.fixture([ready.copy() for _ in range(4)])
        for visit in range(2):
            with host.session(slot, Path("official"), {"model":"registered","effort":"max","account_ref":"fixed"}) as env:
                private = Path(env["CODEX_HOME"])
                self.assertFalse(private.is_relative_to(slot))
                self.assertEqual((private / "auth.json").resolve(), original)
                self.assertFalse(set(env) & {"OPENAI_API_KEY","CODEX_API_KEY","OPENAI_BASE_URL","GX_P0_TEST"})
                self.assertEqual(env["GX_P0_PROVIDER"], "openai")
                marker = private / "sessions/session.jsonl"
                if visit == 0:
                    marker.write_text("retained-official-history")
                self.assertEqual(marker.read_text(), "retained-official-history")
                self.assertTrue((slot / "codex/sessions/session.jsonl").is_file())
            self.assertFalse(any(f.name == "auth.json" or f.is_symlink() for f in slot.rglob("*")))
        self.assertEqual(json.loads((slot / "oauth-account.json").read_text())["account_ref"], "fixed")

    def test_switched_account_and_charge_request_refuse_entry(self):
        for incoming, code in [
            ({"status":"READY","reason_codes":[],"account_ref":"other"}, "SUBSCRIPTION_ACCOUNT_CHANGED"),
            ({"status":"BLOCKED","reason_codes":["ADDITIONAL_CREDITS_REQUIRED"],"account_ref":"fixed",
              "subscription_usage_state":"ADDITIONAL_CHARGE"}, "ADDITIONAL_CREDITS_REQUIRED"),
        ]:
            slot, _ = self.fixture([incoming])
            with self.assertRaisesRegex(host.HostBlocked, code):
                with host.session(slot, "official", {"model":"registered","effort":"max","account_ref":"fixed"}):
                    self.fail("A refused admission initiated work")
            self.assertEqual(json.loads((slot / "oauth-account-blocked.json").read_text())["status"], "BLOCKED")
            effective = json.loads((slot / "oauth-account.json").read_text())
            self.assertEqual((effective["status"], effective["account_ref"]), ("BLOCKED", "fixed"))


if __name__ == "__main__":
    unittest.main()
