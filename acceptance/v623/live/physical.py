"""Durable admission at the physical Responses boundary shared by every role in a trial."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from acceptance.v622.public.funding import BatchFunding, FundingBlocked, _integer, _json, _now


@dataclass(frozen=True)
class Quote:
    request_id: str
    payload_sha256: str
    maximum_tokens: int
    contract_id: str
    quote_id: str


@dataclass(frozen=True)
class MeteredReply:
    request_id: str
    quote_id: str
    wire: bytes


def _terminal(wire):
    """Extract the unique terminal provider event from the original Responses SSE."""
    completed = []
    data = []
    for line in [*wire.decode("utf-8").splitlines(), ""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload != "[DONE]":
                event = json.loads(payload)
                if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                    completed.append(event)
    if len(completed) != 1:
        raise FundingBlocked("No unique final provider receipt; reservation retained")
    return completed[0]


def terminal_metadata(wire):
    terminal = _terminal(wire)
    response = terminal["response"]
    status = terminal["type"].split(".", 1)[1]
    if response.get("status", status) != status:
        raise FundingBlocked("Provider terminal status conflicts with the original event")
    return {"id": response.get("id"), "model": response.get("model"), "status": status}


def _usage(wire):
    response = _terminal(wire)["response"]
    if not isinstance(response.get("id"), str) or not response["id"]:
        raise FundingBlocked("Provider receipt lacks a response ID")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise FundingBlocked("Terminal response lacks final provider usage; reservation retained")
    incoming = _integer(usage["input_tokens"], "input_tokens")
    outgoing = _integer(usage["output_tokens"], "output_tokens")
    total = _integer(usage["total_tokens"], "total_tokens")
    # A missing component is unknown. Zero, when explicitly reported, is a valid reading.
    cached = usage.get("input_tokens_details", {}).get("cached_tokens")
    reasoning = usage.get("output_tokens_details", {}).get("reasoning_tokens")
    if (incoming + outgoing != total or (cached is not None and _integer(cached, "cached_tokens") > incoming)
            or (reasoning is not None and _integer(reasoning, "reasoning_tokens") > outgoing)):
        raise FundingBlocked("Inconsistent final provider usage; reservation retained")
    return response["id"], usage


class FundedTransport:
    """One verified deployment owns quote, irreversible send and free lookup by original request ID.

    Production callers may only obtain their owner from the installed deployment binding.
    The owner enforces its all-inclusive quote on the same authenticated request and must never
    retry a billable send internally. Lookup must be free and confined to the original request.
    """
    # Preserve the existing two-level accounting and SQLite locking semantics.
    _db = BatchFunding._db
    _totals = staticmethod(BatchFunding._totals)

    def __init__(self, path, *, series, slots, slot_cap=1_000_000, batch_cap=4_000_000, owner, authorization=None):
        self.path = Path(path)
        self.owner = owner
        self.slots = tuple(s["slot"] if isinstance(s, dict) else s for s in slots)
        self.slot_cap = _integer(slot_cap, "slot cap")
        self.batch_cap = _integer(batch_cap, "batch cap")
        if (not series or not self.slots or any(not isinstance(s, str) or not s for s in self.slots)
                or len(set(self.slots)) != len(self.slots) or not owner.deployment_id or not owner.contract_id
                or owner.source not in {"SCRIPTED_LOOPBACK", "PRODUCTION"}):
            raise FundingBlocked("Missing enrolled series, deployment, cost contract or slots")
        self.contract = {"series": series, "slots": list(self.slots), "tokens_per_slot": self.slot_cap,
                         "tokens_whole_batch": self.batch_cap, "deployment_id": owner.deployment_id,
                         "contract_id": owner.contract_id, "source": owner.source, "schema": "gx.physical-budget/2"}
        self.expiry = None
        self.reconciliation_only = False
        if owner.source == "PRODUCTION":
            from acceptance.v623.live.deployment import VerifiedApproval, VerifiedOwner
            if not isinstance(owner, VerifiedOwner):
                raise FundingBlocked("Unverified production transport")
            expected = {"status": "VERIFIED", "issuer_verified": True, "covers_series": True,
                        "series": series, "tokens_per_slot": self.slot_cap, "tokens_whole_batch": self.batch_cap,
                        "deployment_id": owner.deployment_id, "contract_id": owner.contract_id,
                        "account_ref": owner.proof["account_ref"], "cost_mode": "HARD_TOKEN_CAP"}
            if not isinstance(authorization, dict) or any(authorization.get(k) != v for k,v in expected.items()):
                raise FundingBlocked("No matching current operator approval for the installed priced-send owner")
            if not isinstance(authorization, VerifiedApproval) or authorization != authorization.checked:
                # An exported receipt can identify an existing ledger for a free lookup. Copying or
                # editing the JSON cannot (re)authorize sending, even with a genuine installed owner.
                self.reconciliation_only = True
                if not self.path.is_file():
                    raise FundingBlocked("A new ledger requires a freshly verified operator grant")
            self.expiry = datetime.fromisoformat(authorization["expires_utc"].replace("Z", "+00:00"))
            if self.expiry.tzinfo is None:
                raise FundingBlocked("Operator grant expiry lacks a time zone")
            self.contract.update(candidate=authorization["candidate"], protocol_sha256=authorization["protocol_sha256"],
                                 account_ref=authorization["account_ref"])
        with self._db(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS enrollment(id INTEGER PRIMARY KEY CHECK(id=1), contract TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO enrollment VALUES(1,?)", (_json(self.contract),))
            if db.execute("SELECT contract FROM enrollment WHERE id=1").fetchone()[0] != _json(self.contract):
                raise FundingBlocked("Ledger already enrolled for different work, caps or deployment")
            db.execute("""CREATE TABLE IF NOT EXISTS requests(
                request_id TEXT PRIMARY KEY, slot TEXT NOT NULL, scope TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                quote_id TEXT NOT NULL, maximum INTEGER NOT NULL, admitted_utc TEXT NOT NULL, sent_intent_utc TEXT,
                state TEXT NOT NULL CHECK(state IN ('HELD','UNKNOWN','SETTLED','BREACH')),
                provider_receipt_id TEXT UNIQUE, usage_json TEXT, actual INTEGER, wire BLOB, settled_utc TEXT)""")

    def _get(self, ident):
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE request_id=?", (ident,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _match(row, slot, scope, digest):
        if (row["slot"], row["scope"], row["payload_sha256"]) != (slot, scope, digest):
            raise FundingBlocked("Original request ID refers to different work or body")

    def reserve(self, *, slot, scope, request_id, body):
        if (slot not in self.slots or not isinstance(scope, str) or not scope or
                not isinstance(request_id, str) or not request_id or not isinstance(body, bytes)):
            raise FundingBlocked("Unregistered slot, scope, physical request ID or payload")
        digest = hashlib.sha256(body).hexdigest()
        prior = self._get(request_id)
        if prior:
            self._match(prior, slot, scope, digest)
            return
        # Nonbillable binding to the same provider, outside the SQLite write transaction.
        quote = self.owner.quote(request_id, body)
        if (not isinstance(quote, Quote) or quote.request_id != request_id or quote.payload_sha256 != digest
                or quote.contract_id != self.owner.contract_id or not quote.quote_id or
                not _integer(quote.maximum_tokens, "all-inclusive provider bound")):
            raise FundingBlocked("HARD_CAP_UNAVAILABLE for this provider request")
        with self._db(write=True) as db:
            prior = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                self._match(prior, slot, scope, digest)
                return
            self._require_active()
            if db.execute("SELECT 1 FROM requests WHERE slot=? AND scope=? AND actual IS NULL",
                          (slot, scope)).fetchone():
                raise FundingBlocked("Original request in this scope needs reconciliation before a new ID")
            for name, limits, cap in (("batch", self._totals(db), self.batch_cap),
                                      ("slot", self._totals(db, slot), self.slot_cap)):
                if limits["breaches"] or limits["settled_tokens"] + limits["held_tokens"] + quote.maximum_tokens > cap:
                    raise FundingBlocked(f"{name} cannot admit this request under its bound")
            db.execute("""INSERT INTO requests(request_id,slot,scope,payload_sha256,quote_id,maximum,admitted_utc,state)
                          VALUES(?,?,?,?,?,?,?,'HELD')""", (request_id, slot, scope, digest,
                                                            quote.quote_id, quote.maximum_tokens, _now()))

    def _require_active(self):
        if self.reconciliation_only:
            raise FundingBlocked("Exported approval permits only free reconciliation of original requests")
        if self.expiry is not None and datetime.now(timezone.utc) >= self.expiry:
            raise FundingBlocked("Current operator approval expired; only free reconciliation remains")

    def request(self, *, slot, scope, request_id, body):
        self.reserve(slot=slot, scope=scope, request_id=request_id, body=body)
        with self._db(write=True) as db:
            if self._totals(db)["breaches"]:
                raise FundingBlocked("A provider exceeded its quote; new sends suspended")
            if db.execute("SELECT state FROM requests WHERE request_id=?", (request_id,)).fetchone()[0] == "HELD":
                self._require_active()
            send = db.execute("""UPDATE requests SET state='UNKNOWN', sent_intent_utc=?
                                 WHERE request_id=? AND state='HELD'""", (_now(), request_id)).rowcount
        if not send:
            wire = self.reconcile(request_id)
            if wire is None:
                raise FundingBlocked("Original physical request pending; no second send")
            return wire
        row = self._get(request_id)
        quote = Quote(request_id, row["payload_sha256"], row["maximum"], self.owner.contract_id, row["quote_id"])
        # Only the winner of the durable HELD -> UNKNOWN transition can reach this irreversible call.
        reply = self.owner.send(request_id, body, quote)
        return self._settle(row, reply)

    def _settle(self, row, reply):
        if (not isinstance(reply, MeteredReply) or reply.request_id != row["request_id"]
                or reply.quote_id != row["quote_id"]):
            raise FundingBlocked("Provider receipt does not bind the original request and quote")
        response_id, usage = _usage(reply.wire)
        total = usage["total_tokens"]
        breach = total > row["maximum"]
        with self._db(write=True) as db:
            already = db.execute("SELECT * FROM requests WHERE request_id=?", (row["request_id"],)).fetchone()
            if already["actual"] is not None:
                if (already["provider_receipt_id"] != response_id or already["usage_json"] != _json(usage)
                        or terminal_metadata(already["wire"]) != terminal_metadata(reply.wire)):
                    raise FundingBlocked("Conflicting late provider receipt")
                if already["state"] == "BREACH":
                    raise FundingBlocked("Confirmed provider usage exceeded its quote")
                return already["wire"]
            if already["state"] != "UNKNOWN":
                raise FundingBlocked("No committed send intent")
            if db.execute("SELECT 1 FROM requests WHERE provider_receipt_id=?", (response_id,)).fetchone():
                raise FundingBlocked("Provider receipt is already bound to another physical request")
            db.execute("""UPDATE requests SET state=?,provider_receipt_id=?,usage_json=?,actual=?,wire=?,settled_utc=?
                          WHERE request_id=?""", ("BREACH" if breach else "SETTLED", response_id, _json(usage),
                                                  total, reply.wire, _now(), row["request_id"]))
        if breach:
            raise FundingBlocked("Actual usage exceeded its quote; full cost recorded and further sends suspended")
        return reply.wire

    def reconcile(self, request_id):
        row = self._get(request_id)
        if not row:
            raise FundingBlocked("Unknown physical request ID")
        if row["state"] == "BREACH":
            raise FundingBlocked("Confirmed provider usage exceeded its quote")
        if row["state"] == "SETTLED":
            return row["wire"]
        if row["state"] == "HELD":
            return None
        # A missing lookup is not proof the provider never executed the request.
        reply = self.owner.lookup(request_id)
        return self._settle(row, reply) if reply is not None else None

    def report(self):
        with self._db() as db:
            rows = [dict(row) for row in db.execute("""SELECT request_id,slot,scope,payload_sha256,
                    quote_id,maximum,admitted_utc,sent_intent_utc,state,provider_receipt_id,usage_json,
                    actual,settled_utc,wire FROM requests ORDER BY rowid""")]
            for row in rows:
                wire = row.pop("wire")
                row["upstream"] = terminal_metadata(wire) if wire is not None else None
                value = row.pop("usage_json")
                row["usage"] = json.loads(value) if value is not None else None
            batch = self._totals(db)
            return {"contract": self.contract, "requests": rows, "batch": batch,
                    "slots": {slot: self._totals(db, slot) for slot in self.slots},
                    "actual_provider_tokens": (batch["settled_tokens"] if self.owner.source == "PRODUCTION" and
                                               not batch["unresolved_requests"] else None),
                    "actual_provider_usd": None}
