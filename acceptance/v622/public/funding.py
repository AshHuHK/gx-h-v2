"""Admission at the public pilot's provider-request boundary, shared across its isolated GX runs."""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid


class FundingBlocked(ValueError):
    """Admission stopped before invoking a backend."""


def require_backend(backend):
    if backend not in ("offline", "loopback"):
        raise FundingBlocked("BLOCKED: the production Codex transport has no verified maximum for "
                             "all billable input and output of a request or turn; this batch has no paid authorization")


def _integer(value, field):
    if type(value) is not int or value < 0 or value >= 2**62:
        raise ValueError(f"Expected a nonnegative integer for {field}")
    return value


def _json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ScriptedCost:
    """The local provider itself emits this fixed, all-inclusive tariff on every response."""
    input_tokens: int = 30
    output_tokens: int = 5
    cached_input_tokens: int = 0

    def usage(self):
        incoming = _integer(self.input_tokens, "input_tokens")
        outgoing = _integer(self.output_tokens, "output_tokens")
        cached = _integer(self.cached_input_tokens, "cached_input_tokens")
        if cached > incoming or incoming + outgoing <= 0:
            raise ValueError("Cached input is a subset of input, and a fixture request must have a positive bound")
        return {"input_tokens": incoming, "input_tokens_details": {"cached_tokens": cached},
                "output_tokens": outgoing, "total_tokens": _integer(incoming + outgoing, "request maximum")}


class BatchFunding:
    """Exactly one row per physical request accounts for both the slot and batch in one transaction."""
    def __init__(self, path, *, series, slots, slot_cap=1_000_000, batch_cap=4_000_000, scripted_cost=None):
        self.path = Path(path).resolve()
        self.cost = scripted_cost or ScriptedCost()
        self.maximum = self.cost.usage()["total_tokens"]
        self.slots = tuple(row["slot"] if isinstance(row, dict) else row for row in slots)
        if (not isinstance(series, str) or not series or not self.slots or
                any(not isinstance(s, str) or not s for s in self.slots) or len(set(self.slots)) != len(self.slots)):
            raise ValueError("Expected a named batch and unique registered slot IDs")
        self.slot_cap = _integer(slot_cap, "slot cap")
        self.batch_cap = _integer(batch_cap, "batch cap")
        self.contract = {"series": series, "slots": self.slots, "tokens_per_slot": self.slot_cap,
                         "tokens_whole_batch": self.batch_cap, "scripted_tariff": asdict(self.cost),
                         "transport": "offline", "schema": "gx.public-request-budget/1"}
        with self._db(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS enrollment(id INTEGER PRIMARY KEY CHECK(id=1), contract TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS requests(
                request_id TEXT PRIMARY KEY, slot TEXT NOT NULL, scope TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, maximum INTEGER NOT NULL, admitted_utc TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('HELD','UNKNOWN','SETTLED','BREACH')),
                provider_receipt_id TEXT UNIQUE, receipt_json TEXT, actual INTEGER, settled_utc TEXT, reason TEXT)""")
            encoded = _json(self.contract)
            db.execute("INSERT OR IGNORE INTO enrollment(id,contract) VALUES(1,?)", (encoded,))
            if db.execute("SELECT contract FROM enrollment WHERE id=1").fetchone()[0] != encoded:
                raise FundingBlocked("This ledger is already bound to a different pilot, tariff or cap")

    @contextmanager
    def _db(self, *, write=False):
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with db:
                yield db

    @staticmethod
    def _totals(db, slot=None):
        clause, args = (" WHERE slot=?", (slot,)) if slot is not None else ("", ())
        row = db.execute("""SELECT count(*) AS admitted,
                coalesce(sum(coalesce(actual,0)),0) AS settled_tokens,
                coalesce(sum(CASE WHEN actual IS NULL THEN maximum ELSE 0 END),0) AS held_tokens,
                count(CASE WHEN actual IS NULL THEN 1 END) AS unresolved_requests,
                count(CASE WHEN state='BREACH' THEN 1 END) AS breaches FROM requests""" + clause, args).fetchone()
        return dict(row)

    def reserve(self, slot, scope, request_id, payload_sha256, *, backend="offline"):
        """A true return is permission to send once. An identical replay never dispatches again."""
        require_backend(backend)
        if (slot not in self.slots or not isinstance(scope, str) or not scope or
                not isinstance(request_id, str) or not request_id or not _digest(payload_sha256)):
            raise FundingBlocked("Missing registered scope, request identity or payload digest")
        with self._db(write=True) as db:
            prior = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if (prior["slot"], prior["scope"], prior["payload_sha256"]) != (slot, scope, payload_sha256):
                    raise FundingBlocked("Request identity was already bound to different work")
                return False
            for name, bounds, cap in (("batch", self._totals(db), self.batch_cap),
                                      ("slot", self._totals(db, slot), self.slot_cap)):
                if bounds["breaches"]:
                    raise FundingBlocked("A provider exceeded its evidenced maximum; batch admission is suspended")
                if bounds["settled_tokens"] + bounds["held_tokens"] + self.maximum > cap:
                    raise FundingBlocked(f"Insufficient {name} balance for the full {self.maximum}-token request")
            db.execute("""INSERT INTO requests(request_id,slot,scope,payload_sha256,maximum,admitted_utc,state)
                          VALUES(?,?,?,?,?,?,'HELD')""", (request_id, slot, scope, payload_sha256, self.maximum, _now()))
        return True

    def unknown(self, request_id, reason):
        with self._db(write=True) as db:
            db.execute("UPDATE requests SET state='UNKNOWN', reason=? WHERE request_id=? AND state='HELD'",
                       (str(reason), request_id))

    def settle(self, request_id, receipt):
        """Record the provider's complete usage exactly once, including a genuine out-of-contract exposure."""
        if receipt.get("type") != "response.completed":
            raise ValueError("No final provider usage receipt")
        response = receipt["response"]
        if not isinstance(response.get("id"), str) or not response["id"]:
            raise ValueError("Missing provider response identity")
        usage = response["usage"]
        total = _integer(usage["total_tokens"], "total_tokens")
        incoming = _integer(usage["input_tokens"], "input_tokens")
        outgoing = _integer(usage["output_tokens"], "output_tokens")
        cached = _integer(usage.get("input_tokens_details", {}).get("cached_tokens", 0), "cached_tokens")
        if total != incoming + outgoing or cached > incoming:
            raise ValueError("Receipt components disagree with provider total")
        raw = _json(receipt)
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise ValueError("Unknown request")
            if row["receipt_json"] is not None:
                if row["receipt_json"] != raw:
                    raise ValueError("A different receipt was submitted for a settled request")
                return False
            if response["id"] != request_id:
                raise ValueError("Receipt identity differs from the admitted offline provider request")
            state = "SETTLED" if total <= row["maximum"] else "BREACH"
            db.execute("""UPDATE requests SET state=?, provider_receipt_id=?, receipt_json=?, actual=?,
                          settled_utc=?, reason=? WHERE request_id=?""",
                       (state, response["id"], raw, total, _now(),
                        "Actual bill exceeds the provider contract" if state == "BREACH" else None, request_id))
        if state == "BREACH":
            raise FundingBlocked("Actual provider usage exceeded its maximum; recorded full exposure and stopped admission")
        return True

    def report(self):
        def summary(row, limit):
            return {**row, "cap": limit, "available_tokens": limit-row["settled_tokens"]-row["held_tokens"],
                    "final_scripted_tokens": None if row["unresolved_requests"] else row["settled_tokens"]}
        with self._db() as db:
            requests = [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY rowid")]
            for row in requests:
                raw = row.pop("receipt_json")
                row["raw_provider_receipt"] = json.loads(raw) if raw is not None else None
                row["receipt_sha256"] = hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None
            return {"contract": self.contract, "backend": "SCRIPTED_LOOPBACK", "paid_provider_calls": 0,
                    "live_provider_tokens": None, "live_provider_usd": None, "requests": requests,
                    "batch": summary(self._totals(db), self.batch_cap),
                    "slots": {slot: summary(self._totals(db, slot), self.slot_cap) for slot in self.slots}}



def check_slot_receipts(ledger, slot, rows, *, require_reconciled=True):
    """Compare received HTTP calls with original usage receipts. A missing bill keeps its maximum held."""
    stored = {r["request_id"]: r for r in ledger.report()["requests"] if r["slot"] == slot}
    observed = {}
    for http in rows:
        ident = http.get("id")
        if http.get("refused"):
            if ident in stored:
                raise FundingBlocked("A refused request has a provider reservation")
            continue
        if ident not in stored or ident in observed:
            raise FundingBlocked("Provider request identity is missing or duplicated in the budget ledger")
        row = stored[ident]
        if (http.get("scope") != row["scope"] or http.get("payload_sha256") != row["payload_sha256"]
                or ("scripted_provider_tokens" in http and http["scripted_provider_tokens"] != row["actual"])):
            raise FundingBlocked("Provider request and its original budget receipt disagree")
        observed[ident] = http
    account = {}
    for ident, row in stored.items():
        scope = row["scope"]
        totals = account.setdefault(scope, {"requests": 0, "settled_tokens": 0, "held_tokens": 0,
                     "unresolved_requests": 0, "missing_http_witnesses": 0, "status": "SETTLED",
                     "fixture_input_tokens": 0, "fixture_output_tokens": 0, "fixture_cached_input_tokens": 0,
                     "actual_provider_input_tokens": None, "actual_provider_output_tokens": None,
                     "actual_provider_cached_input_tokens": None, "actual_usd": None})
        totals["requests"] += 1
        totals["missing_http_witnesses"] += int(ident not in observed)
        if row["actual"] is None:
            totals["held_tokens"] += row["maximum"]
            totals["unresolved_requests"] += 1
        else:
            usage = row["raw_provider_receipt"]["response"]["usage"]
            totals["settled_tokens"] += usage["total_tokens"]
            totals["fixture_input_tokens"] += usage["input_tokens"]
            totals["fixture_output_tokens"] += usage["output_tokens"]
            totals["fixture_cached_input_tokens"] += usage.get("input_tokens_details", {}).get("cached_tokens", 0)
        if row["state"] == "BREACH":
            raise FundingBlocked("Receipt exceeds contract maximum, remaining enrollment stopped")
    for totals in account.values():
        if totals["unresolved_requests"] or totals["missing_http_witnesses"]:
            totals["status"] = "UNRECONCILED"
            if require_reconciled:
                raise FundingBlocked("Missing final usage or request witness, retain exposure and pause enrollment")
            for key in ("fixture_total_tokens", "fixture_input_tokens", "fixture_output_tokens", "fixture_cached_input_tokens"):
                totals[key] = None
        else:
            totals["fixture_total_tokens"] = totals["settled_tokens"]
    return account

def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@contextmanager
def offline_responses(trace_path, ledger, slot, responses, action, *, scope_of):
    """The only Responses endpoint in this formal offline host; charge every POST, including tool follow-ups."""
    class FundedPeer(responses.Peer):
        def do_POST(self):
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            ident = "fixture-" + uuid.uuid4().hex
            try:
                raw_input = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw_input)
                scope = scope_of(body)
                payload_sha256 = hashlib.sha256(raw_input).hexdigest()
                if not ledger.reserve(slot, scope, ident, payload_sha256):
                    raise FundingBlocked("Duplicate physical request")
            except (ValueError, KeyError, TypeError) as error:
                self.server.record({"id": ident, "refused": True, "error": str(error), "slot": slot})
                self.send_error(403, "Request not admitted")
                return
            with self.server.lock:
                self.server.request_count += 1
            meta = {"id": ident, "at_utc": _now(), "scope": scope, "model": body.get("model"),
                    "reasoning_effort": (body.get("reasoning") or {}).get("effort"),
                    "peer": self.client_address[0], "payload_sha256": payload_sha256}
            try:
                item, detail = action(body, ident)
                if detail.get("scope", scope) != scope:
                    raise ValueError("Scripted provider and request scope differ")
                # This fixture owns both sides of its tariff: the same fixed contract sets its full bill.
                # A real model provider must enforce its own maximum before this adapter may admit it.
                completed = {"type": "response.completed", "response": {"id": ident, "usage": ledger.cost.usage()}}
                ledger.settle(ident, completed)
                meta.update(detail, scripted_provider_tokens=completed["response"]["usage"]["total_tokens"])
            except Exception as error:
                ledger.unknown(ident, type(error).__name__)
                self.server.record({**meta, "error": f"{type(error).__name__}: {error}"})
                self.send_error(400, "Offline provider did not return a usable completion")
                return
            self.server.record(meta)
            events = [{"type": "response.created", "response": {"id": ident}},
                      {"type": "response.output_item.done", "item": item}, completed]
            payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            # The provider's final receipt exists even if the client disconnects during delivery.
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with responses.FixtureServer(Path(trace_path)) as server:
        server.RequestHandlerClass = FundedPeer
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()
