"""Request-bound controls. Local provider records a physical send in a second durable database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from acceptance.v623.live import deployment as prod
from acceptance.v623.live.physical import FundedTransport, FundingBlocked, MeteredReply, Quote


def body(n=7):
    return json.dumps({"maximum": n, "input": "reserved material"}).encode()


class LocalOwner:
    source = "SCRIPTED_LOOPBACK"
    deployment_id = "unpaid-crash-control"
    contract_id = "local-test-fixed-tariff"

    def __init__(self, directory, fault=""):
        self.path = Path(directory) / "test-provider.sqlite"
        self.fault = fault
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS sends(request_id TEXT PRIMARY KEY, quote_id TEXT, wire BLOB)")
            db.commit()

    def quote(self, ident, original):
        return Quote(ident, hashlib.sha256(original).hexdigest(), json.loads(original)["maximum"], self.contract_id, "q-" + ident)

    def send(self, ident, original, quote):
        if self.fault == "before_external_send":
            os._exit(92)
        total = quote.maximum_tokens + int(self.fault == "breach")
        usage = {"input_tokens": total - 1, "output_tokens": 1, "total_tokens": total,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 1}}
        payload = {"type": "response.completed", "response": {"id": "external-" + ident, "usage": usage}}
        wire = ("data: " + json.dumps(payload) + "\n\n").encode()
        # Duplicate send raises IntegrityError, even if the ledger later tries to count it only once.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO sends VALUES(?,?,?)", (ident, quote.quote_id, wire))
            db.commit()
        if self.fault == "after_send":
            os._exit(93)
        return MeteredReply(ident, quote.quote_id, wire)

    def lookup(self, ident):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT request_id,quote_id,wire FROM sends WHERE request_id=?", (ident,)).fetchone()
        return MeteredReply(*row) if row else None

    def count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT count(*) FROM sends").fetchone()[0]


def ledger(directory, fault="", *, slot_cap=20, batch_cap=40):
    return FundedTransport(Path(directory) / "ledger.sqlite", series="recovery-control", slots=["01", "02"],
                           slot_cap=slot_cap, batch_cap=batch_cap, owner=LocalOwner(directory, fault))


def attempt(ident="req", scope="lead", slot="01", n=7):
    return dict(slot=slot, scope=scope, request_id=ident, body=body(n))


def crash(directory, fault):
    meter = ledger(directory, fault)
    if fault == "after_reserve":
        meter.reserve(**attempt())
        os._exit(91)
    meter.request(**attempt())
    if fault == "after_settle":
        os._exit(94)


class Physical(unittest.TestCase):
    def test_process_death_before_send_after_send_after_settlement(self):
        for fault, status, code in (("after_reserve", "HELD", 91), ("before_external_send", "UNKNOWN", 92),
                                    ("after_send", "UNKNOWN", 93), ("after_settle", "SETTLED", 94)):
            with self.subTest(fault=fault), TemporaryDirectory() as tmp:
                proc = multiprocessing.get_context("spawn").Process(target=crash, args=(tmp, fault))
                proc.start()
                proc.join(10)
                self.assertEqual(proc.exitcode, code)
                meter = ledger(tmp)
                self.assertEqual(meter.report()["requests"][0]["state"], status)
                if fault == "before_external_send":
                    self.assertIsNone(meter.reconcile("req"))
                    with self.assertRaisesRegex(FundingBlocked, "pending"):
                        meter.request(**attempt())
                    with self.assertRaisesRegex(FundingBlocked, "reconciliation"):
                        meter.request(**attempt(ident="new-id"))
                    self.assertEqual(meter.report()["batch"]["held_tokens"], 7)
                    self.assertEqual(meter.owner.count(), 0)
                else:
                    first = meter.request(**attempt())
                    self.assertEqual(meter.request(**attempt()), first)
                    self.assertEqual(meter.reconcile("req"), first)
                    self.assertEqual(meter.owner.count(), 1)
                    self.assertEqual(meter.report()["batch"]["settled_tokens"], 7)
                    self.assertEqual(meter.report()["batch"]["held_tokens"], 0)
                self.assertIsNone(meter.report()["actual_provider_tokens"])

    def test_concurrent_slot_and_batch(self):
        for same_slot in (True, False):
            with self.subTest(same_slot=same_slot), TemporaryDirectory() as tmp:
                meter = ledger(tmp, slot_cap=1_000_000, batch_cap=4_000_000 if same_slot else 1_000_000)
                barrier = threading.Barrier(2)
                def race(pair):
                    barrier.wait()
                    try:
                        meter.request(**attempt(ident=pair[0], scope=pair[0], slot=pair[1], n=600_000))
                        return "SENT"
                    except FundingBlocked:
                        return "BLOCKED"
                with ThreadPoolExecutor(2) as pool:
                    rows = list(pool.map(race, [("lead", "01"), ("child", "01" if same_slot else "02")]))
                self.assertEqual(sorted(rows), ["BLOCKED", "SENT"])
                self.assertEqual(meter.owner.count(), 1)
                self.assertEqual(meter.report()["batch"]["settled_tokens"], 600_000)

    def test_missing_bills_retain_exposure_linked_late_receipt_no_duplicate(self):
        with TemporaryDirectory() as tmp:
            meter = ledger(tmp)
            genuine = meter.owner.send
            def lose_reply(*args):
                genuine(*args)
                raise TimeoutError
            with patch.object(meter.owner, "send", lose_reply), self.assertRaises(TimeoutError):
                meter.request(**attempt())
            with patch.object(meter.owner, "lookup", return_value=None):
                self.assertIsNone(meter.reconcile("req"))
                self.assertEqual(meter.report()["batch"]["held_tokens"], 7)
            for incorrect in (attempt(scope="child"),attempt(n=8),attempt(slot="02")):
                with self.assertRaises(FundingBlocked):
                    meter.request(**incorrect)
            # A different role can continue against available batch funds.
            meter.request(**attempt(ident="child", scope="child"))
            self.assertEqual(meter.report()["batch"]["settled_tokens"], 7)
            for _ in range(2):
                meter.reconcile("req")
            self.assertEqual([r["provider_receipt_id"] for r in meter.report()["requests"]], ["external-req", "external-child"])
            self.assertEqual((meter.report()["batch"]["settled_tokens"], meter.owner.count()), (14, 2))

    def test_breach_and_aliased_final_receipt(self):
        with TemporaryDirectory() as tmp:
            meter = ledger(tmp, "breach")
            with self.assertRaises(FundingBlocked):
                meter.request(**attempt())
            with self.assertRaises(FundingBlocked):
                meter.request(**attempt("more", "child"))
            self.assertEqual(meter.report()["batch"]["settled_tokens"], 8)
            self.assertEqual(meter.report()["batch"]["breaches"], 1)
        with TemporaryDirectory() as tmp:
            meter = ledger(tmp)
            wire = meter.request(**attempt())
            with patch.object(meter.owner, "send", return_value=MeteredReply("new", "q-new", wire)):
                with self.assertRaisesRegex(FundingBlocked, "another physical request"):
                    meter.request(**attempt(ident="new", scope="child"))
            self.assertEqual(meter.report()["batch"]["settled_tokens"], 7)
            self.assertEqual(meter.report()["batch"]["held_tokens"], 7)

    def test_more_physical_requests_than_turns_with_variable_bound(self):
        with TemporaryDirectory() as tmp:
            meter = ledger(tmp)
            for ident, n in (("start", 3), ("tool-followup", 4), ("final", 5)):
                meter.request(**attempt(ident, n=n))
            self.assertEqual(meter.report()["batch"]["admitted"], 3)
            self.assertEqual(meter.report()["batch"]["settled_tokens"], 12)
            self.assertEqual(meter.owner.count(), 3)

    def test_incomplete_bill_is_counted_and_missing_final_usage_remains_held(self):
        for event, usage, expected in (
                ("response.incomplete", {"input_tokens":3,"output_tokens":1,"total_tokens":4}, "SETTLED"),
                ("response.failed", None, "UNKNOWN")):
            with self.subTest(event=event), TemporaryDirectory() as tmp:
                meter = ledger(tmp)
                content = {"type":event, "response":{"id":"provider-attempt","usage":usage}}
                wire = ("data: " + json.dumps(content) + "\n\n").encode()
                reply = MeteredReply("req","q-req",wire)
                with patch.object(meter.owner, "send", return_value=reply):
                    if expected == "UNKNOWN":
                        with self.assertRaisesRegex(FundingBlocked, "reservation retained"):
                            meter.request(**attempt())
                    else:
                        self.assertEqual(meter.request(**attempt()), wire)
                row, = meter.report()["requests"]
                self.assertEqual(row["state"], expected)
                self.assertEqual(row["actual"], 4 if usage else None)

    def test_missing_or_wrong_quote_blocks_send(self):
        with TemporaryDirectory() as tmp:
            meter = ledger(tmp)
            for quoted in (None, Quote("req", "0" * 64, 2, "different", "wrong")):
                with patch.object(meter.owner, "quote", return_value=quoted):
                    with self.assertRaisesRegex(FundingBlocked, "HARD_CAP_UNAVAILABLE"):
                        meter.request(**attempt())
            self.assertEqual(meter.report()["batch"]["admitted"], 0)
            self.assertEqual(meter.owner.count(), 0)


class Approval(unittest.TestCase):
    grant_args = dict(series=prod.PUBLIC, candidate="a" * 40, protocol_sha256="b" * 64, model="gpt-6.2-astra", effort="max")

    def document(self):
        return dict(schema=prod.SCHEME, execution_authorized=True, **self.grant_args, cost_mode="HARD_TOKEN_CAP",
                    tokens_per_slot=1_000_000, tokens_whole_batch=4_000_000, all_physical_requests=True,
                    paid_judges=False, approval_ref="test-approval", deployment_id="test-installed", contract_id="test-meter",
                    account_ref="test-account", expires_utc=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    def test_raw_documents_and_caller_proposed_key_never_authorize(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "request.json"
            path.write_text(json.dumps(self.document()))
            self.assertFalse(prod._approval(**self.grant_args, approval=self.document())["covers_series"])
            self.assertFalse(prod._approval(**self.grant_args, approval=path, authority=path)["covers_series"])
            with self.assertRaisesRegex(FundingBlocked, "HARD_CAP_UNAVAILABLE"):
                prod.open_deployment()

    def test_exact_detached_approval_checked_with_real_signature(self):
        with TemporaryDirectory() as tmp:
            grant = Path(tmp) / "grant"
            key = Path(tmp) / "unpaid-test-key"
            signers = Path(tmp) / "test-issuer"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            signers.write_text("gx-live-pi " + key.with_suffix(".pub").read_text())
            grant.write_text(json.dumps(self.document()))
            subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", "gx-series-spending",
                            str(grant)], check=True)
            with patch.object(prod, "PI_SIGNERS", signers), patch.object(prod, "_owned", side_effect=lambda p: p == signers):
                self.assertTrue(prod._approval(**self.grant_args, approval=grant)["covers_series"])
                for key, replacement in (("series","other"), ("candidate", "c"*40), ("protocol_sha256", "d"*64), ("effort", "low")):
                    self.assertFalse(prod._approval(**(self.grant_args | {key:replacement}), approval=grant)["covers_series"])
                grant.write_text(grant.read_text() + " ")
                self.assertFalse(prod._approval(**self.grant_args, approval=grant)["covers_series"])

    def test_changed_caps_mode_or_expiry_cannot_be_accepted_even_from_operator_store(self):
        with TemporaryDirectory() as tmp:
            location = Path(tmp)
            file = location / (prod.PUBLIC + ".json")
            with patch.object(prod, "PI_DIRECTORY", location), patch.object(prod, "_owned", side_effect=lambda p:p == file):
                for field, value in (("cost_mode", "SOFT"), ("tokens_per_slot", 1_500_000), ("expires_utc", "2001-01-01T00:00:00Z"),
                                     ("all_physical_requests", False), ("paid_judges", True)):
                    file.write_text(json.dumps(self.document() | {field:value}))
                    self.assertFalse(prod._approval(**self.grant_args)["covers_series"])
                file.write_text(json.dumps(self.document()))
                self.assertTrue(prod._approval(**self.grant_args)["covers_series"])

    def test_normal_installed_binding_paid_path_with_isolated_test_authorities(self):
        """Synthetic deployment and PI test roots exercise true resolver I/O. Zero external requests."""
        with TemporaryDirectory() as tmp:
            loc = Path(tmp)
            evidence = loc / "provider-contract"
            evidence.write_bytes(b"synthetic isolated acceptance authority: complete per-request charge")
            proof = dict(verified=True, provider_origin="https://provider.example.org", account_ref="test-account",
                         contract_sha256=prod._sha(evidence), deployment_id="test-installed", contract_id="test-meter",
                         all_billable_tokens_bounded=True, free_lookup_by_original_request_id=True,
                         provider_evidence_ref="CONTROL-RECEIPT", model=self.grant_args["model"], efforts=["max"])
            # The installed package's free control and data planes are simulated strictly inside this temp dir.
            program = loc / "owner.py"
            program.write_text(
                "from acceptance.v623.test_live_funding import LocalOwner\n"
                "class Owner(LocalOwner):\n"
                "    deployment_id = 'test-installed'\n"
                "    contract_id = 'test-meter'\n"
                "    source = 'PRODUCTION'\n"
                "    def deployment_receipt(self): return " + repr(proof) + "\n"
                "    def model_catalog(self): return {'models': []}\n"
                "def open_owner(*, proxies): return Owner(" + repr(str(loc)) + ")\n")
            spec = {k:proof[k] for k in ("provider_origin", "account_ref", "deployment_id", "contract_id", "contract_sha256")}
            spec.update(schema="gx.priced-responses/1", adapter_sha256=prod._sha(program))
            manifest = loc / "binding.json"
            manifest.write_text(json.dumps(spec))
            approved = loc / (prod.PUBLIC + ".json")
            approved.write_text(json.dumps(self.document()))
            proxies = {"HTTP_PROXY":"http://127.0.0.1:7890","HTTPS_PROXY":"http://127.0.0.1:7890",
                       "ALL_PROXY":"socks5h://127.0.0.1:7891", "NO_PROXY":"127.0.0.1"}
            with patch.object(prod, "METER_DIRECTORY", loc), patch.object(prod, "PI_DIRECTORY", loc), \
                 patch.object(prod, "_owned", side_effect=lambda p:p.parent == loc), \
                 patch.object(prod.profiles, "clash_env", return_value=proxies):
                with patch.object(prod, "contract_evidence", return_value={"inspection":"UNIT_CONTROL"}):
                    pre = prod.preflight(**self.grant_args, binary="unused", probe_host=False)
                self.assertTrue(pre["ready"], pre)
                owner = prod.open_deployment()
                meter = FundedTransport(loc / "bound.sqlite", series=prod.PUBLIC, slots=["01","02","03","04"],
                                        owner=owner, authorization=pre["authorization"])
                reply = meter.request(**attempt())
                self.assertEqual(meter.reconcile("req"), reply)
                self.assertEqual(owner.lookup("req").wire, reply)
                self.assertEqual(owner.count(), 1)
                meter.reserve(**attempt("pending"))
                exported = json.loads(json.dumps(pre["authorization"]))
                frozen = FundedTransport(loc / "bound.sqlite", series=prod.PUBLIC, slots=["01","02","03","04"],
                                         owner=owner, authorization=exported)
                self.assertEqual(frozen.reconcile("req"), reply)
                with self.assertRaisesRegex(FundingBlocked, "only free reconciliation"):
                    frozen.request(**attempt("pending"))
                with self.assertRaisesRegex(FundingBlocked, "freshly verified"):
                    FundedTransport(loc / "self-asserted.sqlite", series=prod.PUBLIC, slots=["01"],
                                    owner=owner, authorization=exported)
                meter.expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
                with self.assertRaisesRegex(FundingBlocked, "expired"):
                    meter.request(**attempt("pending"))
                self.assertEqual(owner.count(), 1)
                self.assertEqual(meter.request(**attempt()), reply)
                with self.assertRaisesRegex(FundingBlocked, "Unverified production transport"):
                    FundedTransport(loc / "injected.sqlite", series=prod.PUBLIC, slots=["01"], owner=owner._owner,
                                    authorization=pre["authorization"])
                # Replacing code in place cannot reuse the old site-level pin.
                program.write_text(program.read_text().replace("'PRODUCTION'", "'SCRIPTED_LOOPBACK'"))
                with self.assertRaisesRegex(FundingBlocked, "pin mismatch"):
                    prod.open_deployment()
                manifest.write_text(json.dumps(spec | {"adapter_sha256":prod._sha(program)}))
                with self.assertRaisesRegex(FundingBlocked, "not the registered production"):
                    prod.open_deployment()
                manifest.write_text(json.dumps(spec | {"provider_origin":"https://127.0.0.1"}))
                with self.assertRaisesRegex(FundingBlocked, "pin mismatch"):
                    prod.open_deployment()

    def test_custom_provider_route_or_catalog_never_count_as_official(self):
        for custom in ({"openai_base_url":"http://127.0.0.1/v1"}, {"model_catalog_json":"local.json"},
                       {"model_providers":{"openai":{"base_url":"https://example.org"}}}):
            self.assertFalse(prod._allowed_config(custom))


if __name__ == "__main__":
    unittest.main()
