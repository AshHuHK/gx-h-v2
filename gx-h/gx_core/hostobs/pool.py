"""One durable native Host pool shared by all runs of one authoritative store.

The ledger records intentions before native calls. Every nonterminal row occupies a
slot, across runs and session generations, until a reader confirms termination. No
lease, handout TTL, task result or turn completion releases native capacity.

This service neither creates schema nor owns tools/authorization. Lead-owned DDL and
tool handlers supply those boundaries. Host calls and Host record reads are outside
SQLite transactions; every subsequent write fences both pool and slot generation.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .. import config, contracts
from ..context import Ctx, Principal, SCOPE_HOST_ADAPTER
from ..domain import clock, ids, machines
from ..errors import (
    ArtifactNotFound, ContentHashMismatch, IdempotencyConflict, IllegalTransition,
    OutcomeUnknown, PermissionDenied, ResourceDenied, SchemaInvalid, ScopeDenied, StaleRevision,
)
from ..persistence import store
from ..registry import ToolResult, tool
from . import codex

RESERVED = "RESERVED"
CREATING = "CREATING"
UNKNOWN = "UNKNOWN"
ACTIVE = "ACTIVE"
CLOSING = "CLOSING"
TERMINATED = "TERMINATED"
SLOT_STATES = (RESERVED, CREATING, UNKNOWN, ACTIVE, CLOSING, TERMINATED)
MAX_PAGE = 200
MAX_SNAPSHOT_BYTES = 16000
MAX_SUBTREE = 10000
HANDOFF_FIELDS = ("source_run_id", "destination_run_id", "source_coordinator_lease_ref",
                  "destination_coordinator_lease_ref", "expected_source_revision", "expected_destination_revision")
UNCLAIMED_TRANSFER = "unclaimed_owned_transfer"
NATIVE_MIGRATIONS_PATH = contracts.CONFIG_DIR / "native_contract_migrations.json"
Observer = Callable[..., codex.NativeObservation]


@dataclass(frozen=True)
class _FencedBinding:
    """Internal proof fenced before this transaction appends its own events."""
    lease_id: str
    native_agent_ref: str
    binding_sha256: str


MAX_INVENTORY_AGE_SECONDS = 30


@dataclass(frozen=True)
class NativeInventory:
    """Backend-read native roster; never accepted as a client request body."""

    session_ref: str
    call_id: str
    snapshot_at: str
    observed_at: str
    members: tuple[dict[str, Any], ...]
    evidence: dict[str, Any]
    evidence_sha256: str
    source: str = "host_transcript"


def read_native_inventory(*, session_ref: str, call_id: str,
                          config_root: str | Path | None = None) -> NativeInventory:
    """Read a complete root-owned native tool result, retaining no answer text.

    A persistent thread's completed status is an inactive execution lane, never
    native termination. This observation does not release any GX-owned slot.
    The supported Codex tool schema's quota semantics require independent review;
    caller integers, JSON rosters and hashes are not read as evidence.
    """
    record = codex.read_worker(session_ref, config_root=config_root)
    packet = record.meta.get("current_turn")
    if (not record.readable or not record.meta.get("current_turn_readable")
            or not isinstance(packet, dict) or packet.get("status") != "active"
            or record.meta.get("native_depth") != 0 or record.meta.get("native_parent_ref") is not None):
        raise ResourceDenied("native inventory requires the verified current depth-zero Host root")
    _text(call_id, "inventory_call_id", 128)
    prefix_digest = hashlib.sha256()
    total = 0
    call = None
    observed = None
    first_boundary = packet.get("source_boundary_line")
    with Path(record.path).open("rb") as stream:
        for line in range(1, codex.MAX_CONTROL_ROWS * 32 + 1):
            remaining = record.size - total
            if remaining <= 0:
                break
            raw = stream.readline(min(codex.MAX_ROW_BYTES + 1, remaining))
            if not raw or len(raw) > codex.MAX_ROW_BYTES:
                raise ContentHashMismatch("the native inventory source prefix is incomplete or too large")
            total += len(raw)
            prefix_digest.update(raw)
            row = json.loads(raw)
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict) or row.get("type") != "response_item":
                continue
            if payload.get("type") == "function_call" and payload.get("call_id") == call_id:
                if (call is not None or payload.get("namespace") != "collaboration"
                        or payload.get("name") != "list_agents" or not isinstance(first_boundary, int)
                        or line <= first_boundary or line <= packet["context_line"]
                        or codex._object(payload.get("internal_chat_message_metadata_passthrough")).get("turn_id") != packet["turn_id"]
                        or json.loads(payload.get("arguments", "null")) != {}):
                    raise ContentHashMismatch("the native roster locator is not a current unfiltered root list_agents call")
                call = {"call_line": line, "call_at": row.get("timestamp")}
            elif payload.get("type") == "function_call_output" and payload.get("call_id") == call_id:
                if call is None or observed is not None:
                    raise ContentHashMismatch("the native roster call/output pairing is ambiguous")
                output = payload.get("output")
                data = json.loads(output) if isinstance(output, str) else output
                if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
                    raise ContentHashMismatch("the native roster output has an unsupported field schema")
                observed = {"output_line": line, "snapshot_at": row.get("timestamp"),
                            "source_prefix_sha256": prefix_digest.hexdigest(), "source_prefix_bytes": total,
                            "agents": data["agents"]}
        if total != record.size or prefix_digest.hexdigest() != record.sha256:
            raise ContentHashMismatch("the verified native inventory source prefix changed while reading")
    if observed is None or call is None:
        raise ResourceDenied("no matching current root-owned native inventory observation exists")
    snapshot_at = observed["snapshot_at"]
    if not isinstance(snapshot_at, str):
        raise ContentHashMismatch("the native inventory observation has no timestamp")
    age = (clock.now() - clock.parse(snapshot_at)).total_seconds()
    if age < 0 or age > MAX_INVENTORY_AGE_SECONDS:
        raise ResourceDenied("the native inventory observation is stale; observe the Host again")
    agents = observed.pop("agents")
    if not 1 <= len(agents) <= 256:
        raise ResourceDenied("the native inventory is empty or exceeds its bounded reader limit")
    home = (Path(config_root) if config_root is not None else Path.home() / ".codex").resolve()
    conn = sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    members: list[dict[str, Any]] = []
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        root_row = conn.execute("SELECT cwd FROM threads WHERE id=?", (session_ref,)).fetchone()
        if root_row is None or not isinstance(root_row["cwd"], str):
            raise ResourceDenied("the native inventory has no independently attributed project")
        project = root_row["cwd"]
        used_paths: set[str] = set()
        used_ids: set[str] = set()
        for agent in agents:
            if not isinstance(agent, dict):
                raise ContentHashMismatch("a native inventory member has an unsupported field schema")
            path = agent.get("agent_name")
            if not isinstance(path, str) or path in used_paths or not (path == "/root" or path.startswith("/root/")):
                raise ContentHashMismatch("native inventory canonical identities are missing or duplicated")
            used_paths.add(path)
            if path == "/root":
                candidates = conn.execute("SELECT id,source,cli_version,agent_path FROM threads WHERE id=? AND cwd=?",
                                          (session_ref, project)).fetchall()
            else:
                candidates = conn.execute("SELECT id,source,cli_version,agent_path FROM threads WHERE agent_path=? AND cwd=?",
                                          (path, project)).fetchall()
            matches = []
            for candidate in candidates:
                if candidate["cli_version"] not in codex.VERIFIED_VERSIONS:
                    continue
                identity = codex._identity({"id": candidate["id"], "source": codex._source(candidate["source"]),
                                            "cli_version": candidate["cli_version"], "agent_path": candidate["agent_path"]})
                if identity is None:
                    continue
                cursor, lineage = candidate["id"], set()
                while cursor != session_ref:
                    if cursor in lineage or len(lineage) >= 64:
                        break
                    lineage.add(cursor)
                    parent = conn.execute("SELECT source FROM threads WHERE id=? AND cwd=?", (cursor, project)).fetchone()
                    source = codex._source(parent["source"]) if parent else None
                    cursor = codex._object(codex._object(codex._object(source).get("subagent")).get("thread_spawn")).get("parent_thread_id")
                    if not codex.valid_thread_ref(cursor):
                        break
                if cursor == session_ref:
                    matches.append(identity)
            if len(matches) != 1 or matches[0]["native_agent_ref"] in used_ids:
                raise ContentHashMismatch("the native inventory cannot uniquely map a canonical identity inside this root/project")
            identity = matches[0]
            used_ids.add(identity["native_agent_ref"])
            raw_status = agent.get("agent_status")
            status = ("ACTIVE" if raw_status == "running" else "INACTIVE" if isinstance(raw_status, dict)
                      and set(raw_status) == {"completed"} else "UNKNOWN")
            members.append({"native_agent_ref": identity["native_agent_ref"], "agent_path": path,
                            "native_parent_ref": identity["native_parent_ref"], "native_depth": identity["native_depth"],
                            "runtime_status": status})
        if session_ref not in used_ids:
            raise ContentHashMismatch("a complete native inventory must include its root")
        conn.execute("COMMIT")
    finally:
        conn.close()
    evidence = {**call, **observed, "call_id": call_id, "namespace": "collaboration", "tool": "list_agents",
                "source_native_ref": session_ref, "host_version": record.host_version,
                "turn_id": packet["turn_id"], "context_line": packet["context_line"],
                "scope": "complete_root_tree", "schema": "codex-0.169.0-collaboration-list_agents-v1",
                "members": members}
    return NativeInventory(session_ref=session_ref, call_id=call_id, snapshot_at=snapshot_at,
        observed_at=clock.now_iso(), members=tuple(members), evidence=evidence,
        evidence_sha256=ids.sha256_of_obj(evidence))


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SchemaInvalid(f"{name} must be an integer of at least {minimum}")
    return value


def _text(value: Any, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SchemaInvalid(f"{name} must be a nonempty string of at most {maximum} characters")
    return value


def _metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchemaInvalid("metadata must be an object")
    try:
        raw = ids.canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise SchemaInvalid("metadata must be JSON serializable") from exc
    if len(raw.encode("utf-8")) > 16384:
        raise SchemaInvalid("metadata exceeds 16384 bytes")
    return value


def _row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for column in ("metadata_json", "termination_evidence_json", "native_creator_evidence_json"):
        if column in result:
            raw = result.pop(column)
            result[column.removesuffix("_json")] = json.loads(raw) if raw else None
    if "slot_id" in result:
        result["origin_run_id"] = result["run_id"]
        result["current_run_id"] = result.get("current_run_id") or result["run_id"]
    return result


def _current_run(slot: Mapping[str, Any]) -> str:
    """The backend column owns current membership; caller metadata never does."""
    return str(slot.get("current_run_id") or slot["run_id"])


def _bounded_product(product: Mapping[str, Any]) -> dict[str, Any]:
    """Keep identity fields and replace large opaque products by bounded summaries."""
    result = dict(product)
    for key in ("metadata", "native_creator_evidence", "termination_evidence"):
        value = result.get(key)
        if value is None:
            continue
        raw = ids.canonical_json(value)
        if len(raw.encode("utf-8")) > 512:
            result[key] = {"sha256": ids.sha256_of_obj(value), "bytes": len(raw.encode("utf-8")),
                           "content_omitted": True}
    return result


def backend_limits() -> tuple[int, int]:
    """Fresh hard ceilings. A missing line is denied rather than treated as unlimited."""
    limits = config.limits().get("host_pool")
    if not isinstance(limits, dict):
        raise ResourceDenied("the backend has no authoritative Host pool limits")
    return (_integer(limits.get("max_slots"), "host_pool.max_slots"),
            _integer(limits.get("max_agent_depth"), "host_pool.max_agent_depth"))


def capacity(pool: Mapping[str, Any], *, ceiling: int) -> int:
    """Total native capacity; unknown effective capacity grants zero."""
    effective = pool.get("effective_capacity")
    if effective is None:
        return 0
    return min(_integer(ceiling, "ceiling"),
               _integer(pool.get("configured_capacity"), "configured_capacity"),
               _integer(effective, "effective_capacity"))


def application_capacity(owned_occupied: int) -> dict[str, int]:
    """The backend's child limit is independent of native total capacity.

    Registered pool fields and caller metadata cannot enlarge this application
    quota. All nonterminal GX-owned slots share it across runs and generations;
    external root/other agents affect native availability separately.
    """
    limits = config.limits().get("host_pool")
    if not isinstance(limits, dict) or "target_concurrent_children" not in limits:
        raise ResourceDenied("the backend has no authoritative application Host child limit")
    limit = _integer(limits["target_concurrent_children"], "host_pool.target_concurrent_children")
    occupied = _integer(owned_occupied, "application occupied")
    return {"limit": limit, "occupied": occupied, "available": max(0, limit - occupied)}


def _unclaimed_migration_route(source_sha: str, target_sha: str) -> dict[str, Any]:
    """One exact backend-approved contract exception; no request selects its path."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate migration key")
            result[key] = value
        return result

    try:
        with NATIVE_MIGRATIONS_PATH.open("rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError("oversized migration configuration")
        document = json.loads(raw, object_pairs_hook=unique_object)
        if (not isinstance(document, dict) or set(document) != {"schema_version", "routes"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or not isinstance(document["routes"], list) or len(document["routes"]) > 32):
            raise ValueError("invalid migration configuration")
        known = set()
        for route in document["routes"]:
            if (not isinstance(route, dict) or set(route) != {
                    "purpose", "source_contract_sha", "target_contract_sha", "db_schema_sha"}
                    or route["purpose"] != UNCLAIMED_TRANSFER
                    or any(not isinstance(route[key], str) or re.fullmatch(r"[0-9a-f]{64}", route[key]) is None
                           for key in ("source_contract_sha", "target_contract_sha", "db_schema_sha"))
                    or route["source_contract_sha"] == route["target_contract_sha"]):
                raise ValueError("invalid exact migration route")
            identity = (route["source_contract_sha"], route["target_contract_sha"], route["purpose"])
            if identity in known:
                raise ValueError("duplicate migration route")
            known.add(identity)
        digest = contracts.contract_digest()
        matches = [route for route in document["routes"]
                   if route["source_contract_sha"] == source_sha
                   and route["target_contract_sha"] == target_sha == digest["__combined__"]
                   and route["db_schema_sha"] == digest["db_schema.sql"]]
        if len(matches) != 1:
            raise ValueError("no exact unchanged-DDL migration route")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ContentHashMismatch("no valid root-owned contract route authorizes this unclaimed transfer") from exc
    return {**matches[0], "configuration_sha256": hashlib.sha256(raw).hexdigest()}


def assert_external_call_allowed(conn: sqlite3.Connection) -> None:
    """A caller's outer transaction must not silently keep the write lock held."""
    if conn.in_transaction:
        raise IllegalTransition("Host calls and Host reads require a committed SQLite connection")


@contextmanager
def _snapshot_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        if owned and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if owned:
            conn.execute("COMMIT")


class HostPoolService:
    def __init__(self, conn: sqlite3.Connection, *, observer: Observer | None = None,
                 principal: Principal | None = None, ctx: Ctx | None = None,
                 inventory_observer: Callable[..., NativeInventory] | None = None) -> None:
        self.conn = conn
        self.observer = observer or codex.observe_native
        self.principal = ctx.principal if ctx is not None else principal
        self.ctx = ctx
        self.inventory_observer = inventory_observer or read_native_inventory

    @staticmethod
    def _validate_inventory(inventory: NativeInventory, session_ref: str) -> None:
        if (not isinstance(inventory, NativeInventory) or inventory.session_ref != session_ref
                or inventory.source != "host_transcript" or inventory.evidence.get("scope") != "complete_root_tree"
                or inventory.evidence_sha256 != ids.sha256_of_obj(inventory.evidence)
                or inventory.evidence.get("members") != list(inventory.members)
                or inventory.evidence.get("call_id") != inventory.call_id):
            raise ContentHashMismatch("no authenticated complete native inventory product was read")
        age = (clock.now() - clock.parse(inventory.snapshot_at)).total_seconds()
        if age < 0 or age > MAX_INVENTORY_AGE_SECONDS:
            raise ResourceDenied("the native inventory is stale; observe the Host again")
        keys = [member["native_agent_ref"] for member in inventory.members]
        if len(keys) != len(set(keys)) or session_ref not in keys:
            raise ContentHashMismatch("the native inventory has duplicate or missing root identities")

    def _inventory(self, session_ref: str, call_id: str) -> NativeInventory:
        assert_external_call_allowed(self.conn)
        try:
            value = self.inventory_observer(session_ref=session_ref, call_id=call_id)
            self._validate_inventory(value, session_ref)
            return value
        except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError, KeyError) as exc:
            raise ResourceDenied("the native inventory could not be independently read") from exc

    def _inventory_external_count(self, pool_id: str, inventory: NativeInventory) -> int:
        owned = {row[0] for row in self.conn.execute(
            "SELECT native_agent_ref FROM host_pool_slots WHERE pool_id=? AND state<>? AND native_agent_ref IS NOT NULL",
            (pool_id, TERMINATED))}
        statuses = {row["native_agent_ref"]: "INACTIVE" if row["runtime_status"] == "INACTIVE" else "UNKNOWN"
                    for row in self.conn.execute("SELECT native_agent_ref,runtime_status FROM host_pool_external_inventory "
                        "WHERE pool_id=? AND ownership='EXTERNAL'", (pool_id,))}
        statuses.update({member["native_agent_ref"]: member["runtime_status"] for member in inventory.members})
        return sum(status != "INACTIVE" and native_ref not in owned for native_ref, status in statuses.items())

    def _inventory_receipt(self, pool: Mapping[str, Any], inventory: NativeInventory) -> str:
        self._validate_inventory(inventory, str(pool["session_ref"]))
        receipt_id = ids.sha256_of_obj({"pool_id": pool["pool_id"], "call_id": inventory.call_id,
                                       "evidence_sha256": inventory.evidence_sha256})
        existing = self.conn.execute(
            "SELECT receipt_id,pool_generation FROM host_pool_inventory_receipts WHERE pool_id=? AND native_call_id=?",
            (pool["pool_id"], inventory.call_id)).fetchone()
        if existing is not None:
            if existing["pool_generation"] != pool["session_generation"]:
                raise StaleRevision("this native inventory receipt belongs to an earlier pool generation; observe a new call")
            if existing["receipt_id"] != receipt_id:
                raise ContentHashMismatch("the same native inventory call produced different identity evidence")
            return receipt_id
        self.conn.execute(
            "INSERT INTO host_pool_inventory_receipts(receipt_id,pool_id,pool_generation,source_native_ref,"
            "native_call_id,snapshot_at,source_evidence_json,roster_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (receipt_id, pool["pool_id"], pool["session_generation"], pool["session_ref"], inventory.call_id,
             inventory.snapshot_at, ids.canonical_json(inventory.evidence), ids.canonical_json(list(inventory.members)), clock.now_iso()))
        return receipt_id

    def _register_inventory(self, pool: Mapping[str, Any], inventory: NativeInventory) -> None:
        receipt = self._inventory_receipt(pool, inventory)
        observed = {member["native_agent_ref"] for member in inventory.members}
        # Absence is not a Host-completed observation. Carry previously chargeable
        # identities forward as UNKNOWN; no owned slot state changes here.
        for old in self.conn.execute("SELECT native_agent_ref FROM host_pool_external_inventory WHERE pool_id=?",
                                     (pool["pool_id"],)).fetchall():
            if old["native_agent_ref"] not in observed:
                self.conn.execute("UPDATE host_pool_external_inventory SET runtime_status=CASE WHEN runtime_status='INACTIVE' "
                    "THEN 'INACTIVE' ELSE 'UNKNOWN' END,receipt_id=?,"
                    "generation=generation+1,observed_at=? WHERE pool_id=? AND native_agent_ref=?",
                    (receipt, inventory.snapshot_at, pool["pool_id"], old["native_agent_ref"]))
        for member in inventory.members:
            owned = self.conn.execute("SELECT slot_id FROM host_pool_slots WHERE pool_id=? AND native_agent_ref=? AND state<>? LIMIT 1",
                (pool["pool_id"], member["native_agent_ref"], TERMINATED)).fetchone()
            self.conn.execute(
                "INSERT INTO host_pool_external_inventory(pool_id,native_agent_ref,native_parent_ref,native_depth,agent_path,"
                "runtime_status,ownership,slot_id,receipt_id,generation,observed_at) VALUES(?,?,?,?,?,?,?,?,?,1,?) "
                "ON CONFLICT(pool_id,native_agent_ref) DO UPDATE SET native_parent_ref=excluded.native_parent_ref,"
                "native_depth=excluded.native_depth,agent_path=excluded.agent_path,runtime_status=excluded.runtime_status,"
                "ownership=excluded.ownership,slot_id=excluded.slot_id,receipt_id=excluded.receipt_id,"
                "generation=host_pool_external_inventory.generation+1,observed_at=excluded.observed_at",
                (pool["pool_id"], member["native_agent_ref"], member["native_parent_ref"], member["native_depth"],
                 member["agent_path"], member["runtime_status"], "OWNED" if owned else "EXTERNAL",
                 owned["slot_id"] if owned else None, receipt, inventory.snapshot_at))

    def _require_inventory_covered(self, pool: Mapping[str, Any], inventory: NativeInventory) -> None:
        self._validate_inventory(inventory, str(pool["session_ref"]))
        owned = {row[0] for row in self.conn.execute(
            "SELECT native_agent_ref FROM host_pool_slots WHERE pool_id=? AND state<>? AND native_agent_ref IS NOT NULL",
            (pool["pool_id"], TERMINATED))}
        entries = {row["native_agent_ref"]: row for row in self.conn.execute(
            "SELECT * FROM host_pool_external_inventory WHERE pool_id=?", (pool["pool_id"],))}
        for entry in entries.values():
            if entry["ownership"] != "EXTERNAL" or entry["runtime_status"] == "INACTIVE":
                continue
            receipt = self.conn.execute("SELECT pool_id,pool_generation FROM host_pool_inventory_receipts WHERE receipt_id=?",
                                        (entry["receipt_id"],)).fetchone()
            if receipt is None or receipt["pool_id"] != pool["pool_id"] or receipt["pool_generation"] != pool["session_generation"]:
                raise StaleRevision("a chargeable external identity has no current-generation native inventory receipt")
        for member in inventory.members:
            if member["runtime_status"] == "INACTIVE" or member["native_agent_ref"] in owned:
                continue
            entry = entries.get(member["native_agent_ref"])
            if entry is None or entry["ownership"] != "EXTERNAL" or entry["runtime_status"] == "INACTIVE":
                raise ResourceDenied("a current native identity has no charged inventory entry; explicitly refresh registration")
        charged = sum(row["ownership"] == "EXTERNAL" and row["runtime_status"] != "INACTIVE" for row in entries.values())
        if charged != pool["external_occupied"]:
            raise ContentHashMismatch("external native occupancy is not correlated to its registered identities")

    def _pool_has_inventory(self, pool_id: str) -> bool:
        # Protected backend rows, not a caller-shaped metadata flag, own the mode.
        return (self.conn.execute("SELECT 1 FROM host_pool_inventory_receipts WHERE pool_id=? LIMIT 1", (pool_id,)).fetchone() is not None
                or self.conn.execute("SELECT 1 FROM host_pool_external_inventory WHERE pool_id=? LIMIT 1", (pool_id,)).fetchone() is not None)

    def _requires_inventory(self, run_id: str, pool: Mapping[str, Any]) -> bool:
        from .host_policy import scope_of_run
        run = self._require_run(run_id)
        scoped = scope_of_run(run) == "codex"
        if scoped:
            host = store.loads(run, "host_json", {})
            if host.get("kind") != pool["host"] or host.get("session_ref") != pool["session_ref"]:
                raise ScopeDenied("the backend-pinned run belongs to a different native Host root/session")
        return self._pool_has_inventory(str(pool["pool_id"])) or scoped

    def _adoption_task(self, pool: Mapping[str, Any], run_id: str, task_id: str,
                       seen: codex.NativeObservation | None, lease_id: str | None,
                       *, native_agent_ref: str | None = None, owned_transfer: bool = False) -> dict[str, Any]:
        from . import host_policy, policy
        from ..orchestration import seams
        run = self._require_run(run_id)
        if run["status"] == "PAUSED" or machines.is_terminal("run", run["status"]):
            raise IllegalTransition("a paused or terminal run cannot receive a native ownership transfer")
        if host_policy.scope_of_run(run) != "codex" or pool["host"] != "codex":
            raise ResourceDenied("existing-native adoption requires a backend-pinned Codex policy run")
        self._requires_inventory(run_id, pool)
        task = self.conn.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone()
        assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ArtifactNotFound("the adoption task does not belong to this run")
        worker = f"codex:{pool['session_ref']}:{seen.native_agent_ref if seen is not None else native_agent_ref}"
        if (assignment is None or assignment["status"] != "ASSIGNED" or assignment["pending_json"] is not None
                or not assignment["subject"] or assignment["observed_worker_ref"] not in (None, worker)):
            raise IllegalTransition("existing-native adoption requires a settled task/principal assignment")
        if lease_id is None:
            if task["status"] != "READY" or task["lease_id"] is not None:
                raise IllegalTransition("pre-dispatch adoption requires a real READY and unclaimed public task")
        else:
            lease = self.conn.execute("SELECT * FROM leases WHERE lease_id=? AND task_id=?", (lease_id, task_id)).fetchone()
            if (lease is None or task["lease_id"] != lease_id or task["attempt"] != lease["attempt"]
                    or lease["released_at"] is not None or clock.is_past(lease["expires_at"])
                    or lease["principal"] != assignment["subject"]):
                raise IllegalTransition("recovery adoption requires the task's current assigned-principal lease")
        document, policy_sha = seams.agent_policy_for_run(run)
        required = policy.requirement_of_task(dict(task), document)
        proof = {"task_sha256": ids.sha256_of_obj(dict(task)), "assignment_sha256": ids.sha256_of_obj(dict(assignment)),
                 "principal_subject": assignment["subject"], "assignment_generation": assignment["assignment_generation"],
                 "required": required, "policy_sha256": policy_sha, "native_packet_sha256": None}
        if seen is None:
            return proof
        packet = seen.current_turn
        if (not seen.readable or not seen.current_turn_readable or not isinstance(packet, dict)
                or packet.get("source") != "host_applied_turn_context"
                or packet.get("status") not in (("active",) if lease_id is None and not owned_transfer
                                               else ("active", "completed", "aborted"))
                or packet.get("model_authority") != "codex_host_applied_metadata"
                or packet.get("upstream_tier_independently_verified") is not False
                or not policy.names_model(packet.get("model"), required["model"], document)
                or packet.get("effort") not in required["allowed_efforts"]):
            raise ResourceDenied("the existing native identity's actual current model/effort does not satisfy the intended task policy")
        return {**proof, "native_packet_sha256": packet["evidence_sha256"]}

    def _strict_binding_current(self, binding: Mapping[str, Any] | None, *, lease_id: str,
                                native_agent_ref: str) -> bool:
        if (not isinstance(binding, Mapping) or binding.get("policy_verdict") != "CONFORMS"
                or binding.get("model_authority") != "codex_host_applied_metadata"
                or binding.get("upstream_tier_independently_verified") is not False):
            return False
        from .codex_worker_observation import fence_binding_observation
        return fence_binding_observation(self.conn, binding, lease_id=lease_id, native_agent_ref=native_agent_ref)

    def adopt_existing(self, pool_id: str, *, run_id: str, task_id: str, native_agent_ref: str,
                       inventory_call_id: str, expected_pool_generation: int, expected_pool_revision: int,
                       actor: str, reason: str, lease_id: str | None = None,
                       idempotency_key: str | None = None) -> dict[str, Any]:
        """Charge an existing external identity before any new task dispatch.

        No native call is made and no creation/termination/claim is invented. An
        optional already-current lease is recovery, not a first-dispatch bypass.
        """
        assert_external_call_allowed(self.conn)
        backend_limits()
        config.check_holds()
        self._require_run(run_id)
        _text(reason, "reason", 2048)
        request = {"pool_id": pool_id, "run_id": run_id, "task_id": task_id,
            "native_agent_ref": native_agent_ref, "inventory_call_id": inventory_call_id,
            "expected_pool_generation": expected_pool_generation, "expected_pool_revision": expected_pool_revision,
            "reason": reason, "lease_id": lease_id}
        key = store.tool_key("gx_host_pool_adopt_existing", _text(idempotency_key, "idempotency_key")) if idempotency_key is not None else None
        replay = self._replay("gx_host_pool_adopt_existing", key, request)
        if replay is not None:
            return {**replay, "replayed": True}
        pool = self._pool(pool_id)
        self._requires_inventory(run_id, pool)
        if not codex.valid_thread_ref(native_agent_ref):
            raise SchemaInvalid("adoption requires a valid native identity locator")
        self._adoption_task(pool, run_id, task_id, None, lease_id, native_agent_ref=native_agent_ref)
        inventory = self._inventory(pool["session_ref"], inventory_call_id)
        seen = self.observer(host=pool["host"], session_ref=pool["session_ref"], native_agent_ref=native_agent_ref)
        if (not isinstance(seen, codex.NativeObservation) or seen.host != pool["host"] or seen.session_ref != pool["session_ref"]
                or seen.native_agent_ref != native_agent_ref or seen.source != "host_transcript"
                or len(seen.evidence_sha256) != 64 or seen.terminal_confirmed or not seen.readable
                or type(seen.native_depth) is not int or not 1 <= seen.native_depth <= min(backend_limits()[1], pool["max_depth"])):
            raise ResourceDenied("no non-root existing native identity/depth was independently observed")
        member = next((entry for entry in inventory.members if entry["native_agent_ref"] == native_agent_ref), None)
        if (member is None or (lease_id is None and member["runtime_status"] != "ACTIVE") or member["native_parent_ref"] != seen.native_parent_ref
                or member["native_depth"] != seen.native_depth):
            raise ResourceDenied("the existing identity is not an observed active member of this native root")
        task_proof = self._adoption_task(pool, run_id, task_id, seen, lease_id)
        binding = None
        if lease_id is not None:
            from .codex_dispatch import verified_lease_binding
            binding = verified_lease_binding(self.conn, lease_id=lease_id, native_agent_ref=native_agent_ref)
            if binding is None:
                raise ContentHashMismatch("recovery adoption requires fresh backend-verified native lease causality")
        def work(conn):
            return self._adopt_existing_commit(request, seen, inventory, task_proof, binding, actor)
        if key is not None:
            result, _ = store.run_idempotent(self.conn, key, "gx_host_pool_adopt_existing", request, work)
            return result
        return work(self.conn)

    def _adopt_existing_commit(self, request: Mapping[str, Any], seen: codex.NativeObservation,
                               inventory: NativeInventory, task_proof: Mapping[str, Any],
                               binding: Mapping[str, Any] | None, actor: str) -> dict[str, Any]:
        with store.transaction(self.conn):
            config.check_holds()
            pool = self._pool(request["pool_id"])
            if pool["session_generation"] != request["expected_pool_generation"] or pool["revision"] != request["expected_pool_revision"]:
                raise StaleRevision("the Host pool changed before ownership transfer")
            self._require_inventory_covered(pool, inventory)
            if task_proof != self._adoption_task(pool, request["run_id"], request["task_id"], seen, request["lease_id"]):
                raise StaleRevision("the intended task, assignment or pinned policy changed before ownership transfer")
            entry = self.conn.execute("SELECT * FROM host_pool_external_inventory WHERE pool_id=? AND native_agent_ref=?",
                (pool["pool_id"], seen.native_agent_ref)).fetchone()
            if entry is None or entry["ownership"] != "EXTERNAL" or entry["runtime_status"] == "INACTIVE":
                raise ResourceDenied("the exact native identity has no independently registered chargeable external place")
            if self.conn.execute("SELECT 1 FROM host_pool_slots WHERE pool_id=? AND native_agent_ref=? AND state<>?",
                                 (pool["pool_id"], seen.native_agent_ref, TERMINATED)).fetchone():
                raise IdempotencyConflict("this native identity already has a nonterminal GX-owned place")
            application = application_capacity(self._occupied(pool["pool_id"]))
            if application["available"] == 0:
                raise ResourceDenied("the shared application child limit is full before task dispatch")
            if pool["external_occupied"] < 1:
                raise ContentHashMismatch("the external identity ledger cannot transfer an uncharged place")
            if request["lease_id"] is not None:
                lease = self.conn.execute("SELECT * FROM leases WHERE lease_id=?", (request["lease_id"],)).fetchone()
                prospective = {"run_id": request["run_id"], "native_agent_ref": seen.native_agent_ref}
                if (not self._claim_receipt_current(binding, prospective, pool, lease)
                        or not self._strict_binding_current(binding, lease_id=request["lease_id"], native_agent_ref=seen.native_agent_ref)):
                    raise ContentHashMismatch("the recovery binding's full lease policy/causality proof changed")
            before = self._occupied(pool["pool_id"]) + pool["external_occupied"]
            receipt = self._inventory_receipt(pool, inventory)
            slot_id, now = ids.new_id("host_pool_slot"), clock.now_iso()
            transfer = {"kind": "adopt_existing", "native_created_by_gx": False, "task_bound": request["lease_id"] is not None,
                "run_id": request["run_id"], "task_id": request["task_id"], "principal_subject": task_proof["principal_subject"],
                "assignment_generation": task_proof["assignment_generation"], "inventory_receipt_id": receipt,
                "native_evidence_sha256": seen.evidence_sha256, "native_packet_sha256": task_proof["native_packet_sha256"],
                "previous_external_occupied": pool["external_occupied"], "external_occupied": pool["external_occupied"] - 1,
                "occupied_before": before, "occupied_after": before, "capacity_released": False, "reason": request["reason"]}
            transfer["evidence_sha256"] = ids.sha256_of_obj(transfer)
            values = {"slot_id": slot_id, "pool_id": pool["pool_id"], "run_id": request["run_id"],
                "request_key": "adopt_existing:" + slot_id, "request_sha256": ids.sha256_of_obj(dict(request)),
                "pool_generation": pool["session_generation"], "generation": 1, "state": ACTIVE,
                "logical_depth": 1, "task_id": request["task_id"], "lease_id": request["lease_id"],
                "source": "host_transcript", "metadata_json": ids.canonical_json({"adoption": transfer}),
                "created_at": now, "updated_at": now, "native_agent_ref": seen.native_agent_ref,
                "native_parent_ref": seen.native_parent_ref, "native_depth": seen.native_depth,
                "native_creator_ref": seen.native_parent_ref, "native_creator_depth": seen.native_depth - 1,
                "native_creator_evidence_json": ids.canonical_json({"kind": "preexisting_native_observation",
                    "evidence": seen.to_dict(), "creation_intent_claimed": False}),
                "observed_source": "host_transcript", "observed_at": seen.observed_at}
            if "current_run_id" in {row["name"] for row in self.conn.execute("PRAGMA table_info(host_pool_slots)")}:
                values["current_run_id"] = request["run_id"]
            self.conn.execute("INSERT INTO host_pool_slots(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
            self.conn.execute("UPDATE host_pool_external_inventory SET ownership='OWNED',slot_id=?,receipt_id=?,"
                "generation=generation+1,observed_at=? WHERE pool_id=? AND native_agent_ref=? AND ownership='EXTERNAL'",
                (slot_id, receipt, inventory.snapshot_at, pool["pool_id"], seen.native_agent_ref))
            self.conn.execute("UPDATE host_pools SET external_occupied=external_occupied-1,revision=revision+1,updated_at=? WHERE pool_id=?",
                              (now, pool["pool_id"]))
            after = self._occupied(pool["pool_id"]) + self._pool(pool["pool_id"])["external_occupied"]
            if after != before:
                raise ContentHashMismatch("native ownership transfer failed total occupancy conservation")
            store.append_event(self.conn, kind="host_pool.adopted_existing", actor=actor, run_id=request["run_id"],
                subject_kind="host_pool_slot", subject_id=slot_id, new_state=ACTIVE,
                evidence_level="EXECUTION_OBSERVED", payload=transfer)
            return {"slot": self._slot(slot_id), "transfer_receipt": transfer, "replayed": False}


    def _scope_run(self, run_id: str) -> sqlite3.Row:
        run = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ArtifactNotFound(f"no run {run_id!r}")
        if self.principal is not None and not self.principal.may_access_project(run["project_ref"]):
            raise ScopeDenied("the Host reservation belongs to a project outside this caller's scope")
        return run

    def _require_run(self, run_id: str) -> sqlite3.Row:
        run = self._scope_run(run_id)
        from ..orchestration import service as runs
        if run["workflow_id"]:
            runs.require_pins(run, runs.default_registry().get(run["workflow_id"], run["workflow_version"]))
        else:
            if run["contract_sha256"] and run["contract_sha256"] != contracts.contract_digest()["__combined__"]:
                raise ContentHashMismatch("the run's contract pin differs from the installed contract")
            if run["agent_policy_sha256"] and run["agent_policy_sha256"] != runs.seams.agent_policy_for_run(run)[1]:
                raise ContentHashMismatch("the run's agent policy pin differs from the installed policy")
        return run

    def _pool(self, pool_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM host_pools WHERE pool_id=?", (pool_id,)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no Host pool {pool_id!r}")
        return _row(row)

    def _slot(self, slot_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM host_pool_slots WHERE slot_id=?", (slot_id,)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no Host pool slot {slot_id!r}")
        return _row(row)

    @staticmethod
    def _fence(pool: Mapping[str, Any], slot: Mapping[str, Any], expected_generation: int,
               *, allow_old_pool: bool = False, expected_pool_generation: int | None = None) -> None:
        _integer(expected_generation, "expected_generation", 1)
        if slot["generation"] != expected_generation:
            raise StaleRevision("the Host slot generation changed", details={
                "slot_id": slot["slot_id"], "expected": expected_generation, "actual": slot["generation"]})
        if expected_pool_generation is not None and pool["session_generation"] != expected_pool_generation:
            raise StaleRevision("the Host pool session generation changed")
        if not allow_old_pool and slot["pool_generation"] != pool["session_generation"]:
            raise StaleRevision("the slot belongs to an earlier Host session generation; adopt and reconcile it")

    def _occupied(self, pool_id: str) -> int:
        # Unknown future states also occupy capacity. Never filter to the current generation.
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM host_pool_slots WHERE pool_id=? AND state<>?", (pool_id, TERMINATED),
        ).fetchone()[0])

    def _write(self, slot: Mapping[str, Any], actor: str, *, state: str | None = None,
               fields: Mapping[str, Any] | None = None, reason: str | None = None,
               kind: str = "host_pool.transition") -> dict[str, Any]:
        state = state or str(slot["state"])
        contracts.require_label("host_pool_slot_state", state)
        changes = {**dict(fields or {}), "state": state,
                   "generation": int(slot["generation"]) + 1, "updated_at": clock.now_iso()}
        allowed = {"pool_generation", "current_run_id", "native_agent_ref", "native_parent_ref", "native_depth",
                   "logical_parent_slot_id", "logical_depth", "task_id", "lease_id", "source",
                   "observed_source", "observed_at", "metadata_json", "termination_evidence_json",
                   "native_creator_depth", "native_creator_evidence_json",
                   "state", "generation", "updated_at"}
        if not set(changes) <= allowed:
            raise SchemaInvalid("invalid internal Host slot update")
        self.conn.execute(
            "UPDATE host_pool_slots SET " + ",".join(f"{key}=?" for key in changes) + " WHERE slot_id=?",
            (*changes.values(), slot["slot_id"]),
        )
        self.conn.execute("UPDATE host_pools SET revision=revision+1,updated_at=? WHERE pool_id=?",
                          (changes["updated_at"], slot["pool_id"]))
        store.append_event(self.conn, kind=kind, actor=actor, run_id=_current_run(slot),
                           subject_kind="host_pool_slot", subject_id=slot["slot_id"],
                           prev_state=slot["state"], new_state=state, reason=reason,
                           evidence_level="EXECUTION_OBSERVED", payload={
                               "pool_id": slot["pool_id"], "pool_generation": changes.get(
                                   "pool_generation", slot["pool_generation"]),
                               "previous_generation": slot["generation"], "generation": changes["generation"]})
        return self._slot(str(slot["slot_id"]))

    def register(
        self, pool_id: str, *, host: str, session_ref: str, configured_capacity: int,
        external_occupied: int, max_depth: int, source: str, observed_source: str, actor: str,
        effective_capacity: int | None = None, expected_generation: int | None = None,
        expected_revision: int | None = None, metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None, inventory_call_id: str | None = None,
    ) -> dict[str, Any]:
        assert_external_call_allowed(self.conn)
        backend_limits()
        config.check_holds()
        if effective_capacity is not None and observed_source == "client_report":
            raise ResourceDenied("a client report cannot establish loaded effective native capacity")
        old = self.conn.execute("SELECT host,session_ref FROM host_pools WHERE pool_id=?", (pool_id,)).fetchone()
        if old is not None and (old["host"] != host or old["session_ref"] != session_ref):
            raise IllegalTransition("a pool's Host and native session identity cannot change; register a new pool")
        request = {"pool_id": pool_id, "host": host, "session_ref": session_ref,
                   "configured_capacity": configured_capacity, "external_occupied": external_occupied,
                   "max_depth": max_depth, "source": source, "observed_source": observed_source,
                   "effective_capacity": effective_capacity, "expected_generation": expected_generation,
                   "expected_revision": expected_revision, "metadata": _metadata(metadata),
                   "inventory_call_id": inventory_call_id}
        key = store.tool_key("gx_host_pool_register", _text(idempotency_key, "idempotency_key")) if idempotency_key is not None else None
        replay = self._replay("gx_host_pool_register", key, request)
        if replay is not None:
            return replay["pool"]
        seen = self.observer(host=host, session_ref=session_ref, native_agent_ref=session_ref)
        if (not isinstance(seen, codex.NativeObservation) or not seen.readable or seen.host != host
                or seen.session_ref != session_ref or seen.native_agent_ref != session_ref
                or seen.native_depth != 0 or seen.native_parent_ref is not None
                or seen.source not in ("host_transcript", "backend_process")
                or len(seen.evidence_sha256) != 64 or seen.terminal_confirmed):
            raise ResourceDenied("a Host pool must identify an independently observed native depth-zero root")
        inventory = self._inventory(session_ref, inventory_call_id) if inventory_call_id is not None else None
        work = lambda conn: {"pool": self._register_commit(pool_id, host=host, session_ref=session_ref,
            configured_capacity=configured_capacity, external_occupied=external_occupied, max_depth=max_depth,
            source=source, observed_source=observed_source, actor=actor, effective_capacity=effective_capacity,
            expected_generation=expected_generation, expected_revision=expected_revision, metadata=metadata,
            inventory=inventory)}
        if key is not None:
            response, _ = store.run_idempotent(self.conn, key, "gx_host_pool_register", request, work)
            return response["pool"]
        return work(self.conn)["pool"]


    def _register_commit(
        self, pool_id: str, *, host: str, session_ref: str, configured_capacity: int,
        external_occupied: int, max_depth: int, source: str, observed_source: str, actor: str,
        effective_capacity: int | None = None, expected_generation: int | None = None,
        expected_revision: int | None = None, metadata: dict[str, Any] | None = None,
        inventory: NativeInventory | None = None,
    ) -> dict[str, Any]:
        backend_limits()  # The authoritative limits are read before every pool operation.
        config.check_holds()
        _text(pool_id, "pool_id")
        _text(session_ref, "session_ref")
        _text(source, "source")
        contracts.require_label("host_kind", host)
        contracts.require_label("host_pool_state_source", observed_source)
        if effective_capacity is not None:
            _integer(effective_capacity, "effective_capacity")
            if observed_source == "client_report":
                raise ResourceDenied("a client report cannot establish loaded effective native capacity")
        configured_capacity = _integer(configured_capacity, "configured_capacity")
        external_occupied = _integer(external_occupied, "external_occupied")
        max_depth = _integer(max_depth, "max_depth")
        metadata = _metadata(metadata)
        if "native_inventory" in metadata:
            raise SchemaInvalid("native_inventory metadata is reserved for backend observations")
        if inventory is not None:
            self._validate_inventory(inventory, session_ref)
            if observed_source != "host_transcript":
                raise ResourceDenied("native inventory requires Host-transcript provenance")
            metadata = {**metadata, "native_inventory": {"call_id": inventory.call_id,
                "evidence_sha256": inventory.evidence_sha256, "snapshot_at": inventory.snapshot_at}}
        values = {"host": host, "session_ref": session_ref, "configured_capacity": configured_capacity,
                  "effective_capacity": effective_capacity, "external_occupied": external_occupied,
                  "max_depth": max_depth, "source": source, "observed_source": observed_source,
                  "metadata_json": ids.canonical_json(metadata)}
        with store.transaction(self.conn):
            config.check_holds()
            if inventory is not None:
                self._validate_inventory(inventory, session_ref)
                observed_external = self._inventory_external_count(pool_id, inventory)
                if external_occupied != observed_external:
                    raise ContentHashMismatch("the external occupancy assertion differs from the conservative native inventory",
                                              details={"observed_external_occupied": observed_external})
            raw = self.conn.execute("SELECT * FROM host_pools WHERE pool_id=?", (pool_id,)).fetchone()
            now = clock.now_iso()
            if raw is None:
                if expected_generation not in (None, 1) or expected_revision not in (None, 0):
                    raise StaleRevision("a new Host pool begins at session generation 1 and revision 0")
                try:
                    self.conn.execute(
                        "INSERT INTO host_pools(pool_id," + ",".join(values)
                        + ",session_generation,revision,observed_at,created_at,updated_at) VALUES("
                        + ",".join("?" for _ in range(len(values) + 6)) + ")",
                        (pool_id, *values.values(), 1, 0, now, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IdempotencyConflict("this native host/session already has a different pool id") from exc
                kind, generation = "host_pool.registered", 1
            else:
                have = dict(raw)
                if self._pool_has_inventory(pool_id) and inventory is None:
                    raise ResourceDenied("an inventory-observed pool cannot downgrade to anonymous external integers")
                if expected_generation is not None and have["session_generation"] != expected_generation:
                    raise StaleRevision("the Host pool session generation changed")
                if expected_revision is not None and have["revision"] != expected_revision:
                    raise StaleRevision("the Host pool revision changed")
                if all(have[key] == value for key, value in values.items()):
                    return self._pool(pool_id)
                if expected_generation is None:
                    raise StaleRevision("changing a Host pool needs its current session generation")
                if have["host"] != host or have["session_ref"] != session_ref:
                    raise IllegalTransition("a pool's Host and native session identity cannot change; register a new pool")
                generation = int(have["session_generation"]) + 1
                try:
                    self.conn.execute(
                        "UPDATE host_pools SET " + ",".join(f"{key}=?" for key in values)
                        + ",session_generation=?,revision=revision+1,observed_at=?,updated_at=? WHERE pool_id=?",
                        (*values.values(), generation, now, now, pool_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IdempotencyConflict("this native host/session already has a different pool id") from exc
                kind = "host_pool.reconfigured"
            if inventory is not None:
                self._register_inventory(self._pool(pool_id), inventory)
            store.append_event(self.conn, kind=kind, actor=actor, subject_kind="host_pool",
                               subject_id=pool_id, evidence_level="EXECUTION_OBSERVED", payload={
                                   "session_generation": generation, "source": source,
                                   "observed_source": observed_source})
            return self._pool(pool_id)


    @staticmethod
    def _validate_creator(pool: Mapping[str, Any], seen: codex.NativeObservation,
                          native_ref: str, depth_ceiling: int) -> None:
        if (not isinstance(seen, codex.NativeObservation) or not seen.readable
                or seen.host != pool["host"] or seen.session_ref != pool["session_ref"]
                or seen.native_agent_ref != native_ref or seen.native_depth is None
                or seen.source not in ("host_transcript", "backend_process")
                or len(seen.evidence_sha256) != 64 or seen.terminal_confirmed):
            raise ResourceDenied("the native creator's actual identity/depth is not independently observable")
        _integer(seen.native_depth, "native creator depth")
        if seen.native_depth + 1 > min(depth_ceiling, int(pool["max_depth"])):
            raise ResourceDenied("the actual native creator is already at the maximum agent depth")

    def _replay(self, tool: str, key: str | None, request: dict[str, Any]) -> dict[str, Any] | None:
        if key is None:
            return None
        prior = self.conn.execute("SELECT * FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
        if prior is None:
            return None
        if prior["request_sha"] != ids.sha256_of_obj(request):
            raise IdempotencyConflict(f"the {tool} idempotency key already has a different request")
        if prior["completed_at"] and prior["side_effect_status"] != "UNKNOWN":
            return json.loads(prior["response_json"])
        raise OutcomeUnknown("the earlier native transition has no confirmed result; reconcile it explicitly")

    def reserve(
        self, pool_id: str, *, run_id: str, request_key: str, expected_generation: int,
        actor: str, logical_parent_slot_id: str | None = None, task_id: str | None = None,
        metadata: dict[str, Any] | None = None, native_parent_ref: str | None = None,
        idempotency_key: str | None = None, inventory_call_id: str | None = None,
    ) -> dict[str, Any]:
        assert_external_call_allowed(self.conn)
        config.check_holds()
        _, depth_ceiling = backend_limits()
        self._require_run(run_id)
        if "native_inventory_admission" in _metadata(metadata):
            raise SchemaInvalid("native_inventory_admission is reserved for backend receipts")
        request = {"pool_id": pool_id, "run_id": run_id, "request_key": request_key,
                   "expected_generation": expected_generation, "logical_parent_slot_id": logical_parent_slot_id,
                   "task_id": task_id, "metadata": _metadata(metadata), "native_parent_ref": native_parent_ref,
                   "inventory_call_id": inventory_call_id}
        key = store.tool_key("gx_host_pool_reserve", _text(idempotency_key, "idempotency_key")) if idempotency_key is not None else None
        replay = self._replay("gx_host_pool_reserve", key, request)
        if replay is not None:
            return replay
        pool = self._pool(pool_id)
        required_inventory = self._requires_inventory(run_id, pool)
        if required_inventory and inventory_call_id is None:
            raise ResourceDenied("this Host pool requires a fresh native inventory before admission")
        if task_id is not None and self.conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone() is None:
            raise ArtifactNotFound("the reserved task does not belong to this run")
        inventory = self._inventory(pool["session_ref"], inventory_call_id) if inventory_call_id is not None else None
        # Logical ancestry is independent of who actually invokes the native spawn.
        # An omitted creator explicitly selects the observed native session root.
        creator_ref = native_parent_ref or pool["session_ref"]
        _text(creator_ref, "native_parent_ref")
        seen = self.observer(host=pool["host"], session_ref=pool["session_ref"], native_agent_ref=creator_ref)
        self._validate_creator(pool, seen, creator_ref, depth_ceiling)
        work = lambda conn: self._reserve_commit(pool_id, run_id=run_id, request_key=request_key,
            expected_generation=expected_generation, actor=actor, logical_parent_slot_id=logical_parent_slot_id,
            task_id=task_id, metadata=metadata, native_parent_ref=native_parent_ref, creator=seen,
            inventory=inventory)
        if key is not None:
            response, _ = store.run_idempotent(self.conn, key, "gx_host_pool_reserve", request, work)
            return response
        return work(self.conn)


    def _reserve_commit(
        self, pool_id: str, *, run_id: str, request_key: str, expected_generation: int,
        actor: str, logical_parent_slot_id: str | None = None, task_id: str | None = None,
        metadata: dict[str, Any] | None = None, native_parent_ref: str | None = None,
        creator: codex.NativeObservation, inventory: NativeInventory | None = None,
    ) -> dict[str, Any]:
        ceiling, depth_ceiling = backend_limits()
        _text(request_key, "request_key")
        _integer(expected_generation, "expected_generation", 1)
        metadata = _metadata(metadata)
        if "native_inventory_admission" in metadata:
            raise SchemaInvalid("native_inventory_admission is reserved for backend receipts")
        request_sha = ids.sha256_of_obj({"pool_id": pool_id, "run_id": run_id,
            "logical_parent_slot_id": logical_parent_slot_id, "task_id": task_id, "metadata": metadata,
            "native_parent_ref": native_parent_ref})
        with store.transaction(self.conn):
            pool = self._pool(pool_id)
            self._require_run(run_id)
            old = self.conn.execute("SELECT * FROM host_pool_slots WHERE pool_id=? AND request_key=?",
                                    (pool_id, request_key)).fetchone()
            if old is not None:
                if old["request_sha256"] != request_sha:
                    raise IdempotencyConflict("the pool request key already identifies a different reservation")
                return {"slot": _row(old), "replayed": True}
            if pool["session_generation"] != expected_generation:
                raise StaleRevision("the Host pool session generation changed")
            config.check_holds()
            if self._requires_inventory(run_id, pool):
                if inventory is None:
                    raise ResourceDenied("this Host pool requires a fresh native inventory before admission")
                self._require_inventory_covered(pool, inventory)
                receipt_id = self._inventory_receipt(pool, inventory)
                metadata = {**metadata, "native_inventory_admission": {"call_id": inventory.call_id,
                    "snapshot_at": inventory.snapshot_at, "evidence_sha256": inventory.evidence_sha256,
                    "receipt_id": receipt_id}}
            self._validate_creator(pool, creator, native_parent_ref or pool["session_ref"], depth_ceiling)
            if task_id is not None and self.conn.execute(
                "SELECT 1 FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id),
            ).fetchone() is None:
                raise ArtifactNotFound("the reserved task does not belong to this run")
            depth = 1
            if logical_parent_slot_id is not None:
                parent = self._slot(logical_parent_slot_id)
                if parent["pool_id"] != pool_id or _current_run(parent) != run_id:
                    raise IllegalTransition("a logical parent must belong to the same pool and run")
                if parent["state"] in (CLOSING, TERMINATED):
                    raise IllegalTransition("a closing or terminated parent cannot admit a new child")
                if parent["pool_generation"] != pool["session_generation"]:
                    raise StaleRevision("the parent belongs to an earlier Host session generation")
                depth = int(parent["logical_depth"]) + 1
            if depth > min(depth_ceiling, int(pool["max_depth"])):
                raise ResourceDenied("the requested logical child would exceed the backend/Host depth ceiling")
            owned_occupied = self._occupied(pool_id)
            occupied = owned_occupied + int(pool["external_occupied"])
            total = capacity(pool, ceiling=ceiling)
            application = application_capacity(owned_occupied)
            native_available = max(0, total - occupied)
            if min(native_available, application["available"]) == 0:
                reason = ("the shared application Host child limit has no free slot"
                          if application["available"] == 0 else "the shared native Host pool has no free slot")
                raise ResourceDenied(reason, details={"pool_id": pool_id, "occupied": occupied,
                    "capacity": total, "native_available": native_available, "application_capacity": application})
            slot_id, now = ids.new_id("host_pool_slot"), clock.now_iso()
            contracts.require_label("host_pool_slot_state", RESERVED)
            self.conn.execute(
                "INSERT INTO host_pool_slots(slot_id,pool_id,run_id,request_key,request_sha256,"
                "pool_generation,generation,state,logical_parent_slot_id,logical_depth,task_id,source,"
                "metadata_json,created_at,updated_at,native_creator_ref,native_creator_depth,"
                "native_creator_evidence_json) VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
                (slot_id, pool_id, run_id, request_key, request_sha, expected_generation, RESERVED,
                 logical_parent_slot_id, depth, task_id, "backend_process", ids.canonical_json(metadata), now, now,
                 creator.native_agent_ref, creator.native_depth, ids.canonical_json(creator.to_dict())),
            )
            self.conn.execute("UPDATE host_pools SET revision=revision+1,updated_at=? WHERE pool_id=?", (now, pool_id))
            store.append_event(self.conn, kind="host_pool.reserved", actor=actor, run_id=run_id,
                               subject_kind="host_pool_slot", subject_id=slot_id,
                               new_state=RESERVED, evidence_level="EXECUTION_OBSERVED", payload={
                                   "pool_id": pool_id, "pool_generation": expected_generation,
                                   "generation": 1, "logical_depth": depth,
                                   "native_inventory_admission": metadata.get("native_inventory_admission")})
            return {"slot": self._slot(slot_id), "replayed": False}


    def _unclaimed_transfer_checks(self, slot: Mapping[str, Any], pool: Mapping[str, Any],
                                   destination_task_id: str, handoff: Mapping[str, Any],
                                   seen: codex.NativeObservation | None = None) -> dict[str, Any]:
        """DB authorization before Host reads, repeated under the commit lock.

        The sole historical-pin exception belongs to the terminal source contract.
        It neither re-pins that run nor grants a terminal coordinator a renewal.
        """
        from ..orchestration import service as runs
        from ..teams.manager import _context, assignment_intent
        from . import host_policy, observation
        from .codex_dispatch import CLAIM_EVENT

        config.check_holds()
        if (self.ctx is None or self.ctx.conn is not self.conn
                or self.ctx.principal.scope != SCOPE_HOST_ADAPTER):
            raise ScopeDenied("unclaimed transfer requires the actual host_adapter context")
        if set(handoff) != set(HANDOFF_FIELDS):
            raise SchemaInvalid("unclaimed transfer requires both run IDs, live coordinator leases and revisions")
        source_id, destination_id = _current_run(slot), handoff["destination_run_id"]
        if handoff["source_run_id"] != source_id or source_id == destination_id:
            raise SchemaInvalid("unclaimed transfer must explicitly name the current and distinct destination run")
        if (slot["state"] != ACTIVE or slot["lease_id"] is not None or not slot["task_id"]
                or not codex.valid_thread_ref(slot["native_agent_ref"]) or pool["host"] != "codex"):
            raise IllegalTransition("only an already-owned active and never-bound native task slot can transfer")
        source, destination = self._scope_run(source_id), self._require_run(destination_id)
        if source["status"] != "CANCELLED":
            raise IllegalTransition("the exact source run and its unclaimed task must already be publicly cancelled")
        if source["project_ref"] != destination["project_ref"]:
            raise ScopeDenied("unclaimed native transfer cannot cross project scope")
        route = _unclaimed_migration_route(source["contract_sha256"], destination["contract_sha256"])
        workflow_fields = ("workflow_id", "workflow_version", "workflow_sha256", "agent_policy_sha256")
        if any(not source[key] or source[key] != destination[key] for key in workflow_fields):
            raise ContentHashMismatch("unclaimed transfer requires identical complete workflow and policy pins")
        snapshots = []
        for prefix, run in (("source", source), ("destination", destination)):
            if run["contract_version"] != contracts.contract_version():
                raise ContentHashMismatch("the transfer contract major versions do not match")
            if host_policy.scope_of_run(run) != "codex":
                raise ScopeDenied("unclaimed transfer requires two backend-pinned Codex policy runs")
            pinned, pinned_sha = observation.policy_of_run(self.conn, run["run_id"])
            current, current_sha = host_policy.current_for_run(run)
            if pinned_sha != current_sha or pinned != current:
                raise ContentHashMismatch("the transfer policy snapshot is not the current installed scoped policy")
            snapshots.append(pinned)
            # Validate every ordinary pin; only the exact route permits this one
            # historical contract digest. No stored source field is changed.
            ordinary = {**dict(run), "contract_sha256": destination["contract_sha256"]}
            workflow = runs.default_registry().get(run["workflow_id"], run["workflow_version"])
            runs.require_pins(ordinary, workflow)
            host = store.loads(run, "host_json", {})
            selected = host.get("policy_selection", {})
            if (host.get("kind") != pool["host"] or host.get("session_ref") != pool["session_ref"]
                    or selected.get("native_agent_ref") != pool["session_ref"]):
                raise ScopeDenied("both backend run proofs must identify the pool's actual native Root/session")
            _integer(handoff["expected_" + prefix + "_revision"], "expected_" + prefix + "_revision")
            store.check_revision(self.conn, run["run_id"], handoff["expected_" + prefix + "_revision"])
            coordinator = runs.require_coordinator(self.ctx, self.conn, run["run_id"],
                _text(handoff[prefix + "_coordinator_lease_ref"], prefix + "_coordinator_lease_ref"), renew=False)
            if (coordinator["owner"] != self.ctx.principal.subject or coordinator["host"] != pool["host"]
                    or coordinator["session_ref"] != pool["session_ref"]):
                raise ScopeDenied("both existing coordinator leases must identify this owner and native Root")
        if snapshots[0] != snapshots[1]:
            raise ContentHashMismatch("the two transfer policy snapshots differ")
        source_task = self.conn.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=?",
                                        (slot["task_id"], source_id)).fetchone()
        if (source_task is None or source_task["status"] != "CANCELLED" or source_task["attempt"] != 0
                or source_task["attempts_consumed"] != 0 or source_task["lease_id"] is not None
                or source_task["owner"] is not None):
            raise IllegalTransition("the source task must be cancelled at attempt zero without a lease or owner")
        cancellations = self.conn.execute("SELECT event_id,kind,subject_id,payload_json FROM events WHERE run_id=? "
            "AND evidence_level='EXECUTION_OBSERVED' AND new_state='CANCELLED' "
            "AND ((kind='run.transition' AND subject_kind='run' AND subject_id=?) "
            "OR (kind='task.transition' AND subject_kind='task' AND subject_id=?)) ORDER BY event_id",
            (source_id, source_id, slot["task_id"])).fetchall()
        run_cancel = [row for row in cancellations if row["kind"] == "run.transition"]
        task_cancel = [row for row in cancellations if row["kind"] == "task.transition"]
        try:
            cancelled_payload = json.loads(run_cancel[0]["payload_json"]) if len(run_cancel) == 1 else {}
            if (len(task_cancel) != 1 or cancelled_payload.get("action") != "cancel"
                    or not isinstance(cancelled_payload.get("cancelled_tasks"), list)
                    or slot["task_id"] not in cancelled_payload.get("cancelled_tasks", [])):
                raise ValueError("missing public cancellation receipts")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContentHashMismatch("the source run/task have no matching protected public cancellation receipts") from exc
        for table in ("leases", "jobs", "submissions", "worker_observations"):
            if self.conn.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (slot["task_id"],)).fetchone():
                raise IllegalTransition("the source task has a lease, job, submission or worker-observation history")
        if self.conn.execute("SELECT 1 FROM events WHERE subject_kind='task' AND subject_id=? "
                "AND (kind=? OR new_state IN ('CLAIMED','SUBMITTED','ACCEPTED')) LIMIT 1",
                (slot["task_id"], CLAIM_EVENT)).fetchone():
            raise IllegalTransition("the source task has a prior claim or submission receipt")
        assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (slot["task_id"],)).fetchone()
        expected_worker = f"codex:{pool['session_ref']}:{slot['native_agent_ref']}"
        if (assignment is None or assignment["status"] != "ASSIGNED" or assignment["pending_json"] is not None
                or not assignment["subject"] or assignment["observed_worker_ref"] not in (None, expected_worker)):
            raise IllegalTransition("the source task's exact principal assignment must be settled")
        source_team = self.conn.execute("SELECT * FROM teams WHERE team_id=?", (assignment["team_id"],)).fetchone()
        if source_team is None or source_team["run_id"] != source_id:
            raise ScopeDenied("the source assignment team belongs to another run")
        assignment_intent(self.conn, assignment)
        adopted = self.conn.execute("SELECT event_id,payload_json FROM events WHERE kind='host_pool.adopted_existing' "
            "AND evidence_level='EXECUTION_OBSERVED' AND subject_kind='host_pool_slot' "
            "AND subject_id=? AND run_id=? ORDER BY event_id",
            (slot["slot_id"], source_id)).fetchall()
        try:
            adoption = json.loads(adopted[0]["payload_json"]) if len(adopted) == 1 else None
            if (not isinstance(adoption, dict) or adoption != slot["metadata"].get("adoption")
                    or adoption.get("kind") != "adopt_existing" or adoption.get("task_bound") is not False
                    or adoption.get("native_created_by_gx") is not False or adoption.get("run_id") != source_id
                    or slot["run_id"] != source_id or adoption.get("task_id") != slot["task_id"]
                    or adoption.get("principal_subject") != assignment["subject"]
                    or adoption.get("evidence_sha256") != ids.sha256_of_obj({key: value for key, value in adoption.items()
                                                                          if key != "evidence_sha256"})):
                raise ValueError("no exact protected pre-dispatch adoption")
        except (ValueError, TypeError) as exc:
            raise ContentHashMismatch("the original unclaimed adoption and protected event do not match") from exc
        creator = slot.get("native_creator_evidence")
        original = creator.get("evidence") if isinstance(creator, dict) else None
        if (not isinstance(original, dict) or creator.get("kind") != "preexisting_native_observation"
                or creator.get("creation_intent_claimed") is not False or original.get("source") != "host_transcript"
                or original.get("host") != pool["host"] or original.get("session_ref") != pool["session_ref"]
                or original.get("native_agent_ref") != slot["native_agent_ref"]
                or original.get("native_parent_ref") != slot["native_parent_ref"]
                or original.get("native_depth") != slot["native_depth"]
                or original.get("evidence_sha256") != adoption.get("native_evidence_sha256")):
            raise ContentHashMismatch("the protected original adoption does not identify this exact native slot ancestry")
        ownership = self.conn.execute("SELECT * FROM host_pool_external_inventory "
            "WHERE pool_id=? AND native_agent_ref=? AND slot_id=? AND ownership='OWNED'",
            (pool["pool_id"], slot["native_agent_ref"], slot["slot_id"])).fetchone()
        if ownership is None:
            raise ContentHashMismatch("the exact native inventory identity is not already owned by this slot")
        target = self._adoption_task(pool, destination_id, destination_task_id, seen, None,
                                     native_agent_ref=slot["native_agent_ref"], owned_transfer=True)
        if target["principal_subject"] != assignment["subject"]:
            raise IllegalTransition("unclaimed transfer must retain the exact backend worker principal")
        destination_task = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (destination_task_id,)).fetchone()
        if destination_task["attempt"] != 0 or destination_task["owner"] is not None:
            raise IllegalTransition("the destination task must be a fresh unclaimed READY task")
        for table in ("leases", "jobs", "submissions", "worker_observations"):
            if self.conn.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (destination_task_id,)).fetchone():
                raise IllegalTransition("the destination task already has execution history")
        if self.conn.execute("SELECT 1 FROM events WHERE subject_kind='task' AND subject_id=? "
                "AND (kind=? OR new_state IN ('CLAIMED','SUBMITTED','ACCEPTED')) LIMIT 1",
                (destination_task_id, CLAIM_EVENT)).fetchone():
            raise IllegalTransition("the destination task has a prior claim or submission receipt")
        destination_assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (destination_task_id,)).fetchone()
        destination_teams, team_id, visited = [], destination_assignment["team_id"], set()
        while team_id is not None:
            team = self.conn.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
            if team is None or team["run_id"] != destination_id:
                raise ScopeDenied("the destination assignment team belongs to another run")
            if team_id in visited or team["status"] != "ACTIVE":
                raise IllegalTransition("the destination team and every ancestor must be active without a cycle")
            visited.add(team_id)
            destination_teams.append(dict(team))
            team_id = team["parent_team_id"]
        member = self.conn.execute("SELECT * FROM team_members WHERE team_id=? AND subject=?",
            (destination_assignment["team_id"], assignment["subject"])).fetchone()
        if (member is None or member["status"] != "ACTIVE"
                or member["observed_worker_ref"] not in (None, expected_worker)):
            raise IllegalTransition("the destination principal must have the same active team/native membership")
        assignment_intent(self.conn, destination_assignment)
        if destination_assignment["context_ref"]:
            _context(self.conn, destination_id, destination_assignment["context_ref"])
        children = self.conn.execute("SELECT slot_id FROM host_pool_slots WHERE logical_parent_slot_id=? "
            "AND state<>? LIMIT 1", (slot["slot_id"], TERMINATED)).fetchone()
        if children:
            raise IllegalTransition("detach live logical children before transferring their parent to another run")
        if seen is not None and (not isinstance(seen, codex.NativeObservation) or not seen.readable
                or seen.terminal_confirmed or seen.source != "host_transcript" or seen.host != pool["host"]
                or seen.session_ref != pool["session_ref"] or seen.native_agent_ref != slot["native_agent_ref"]
                or seen.native_parent_ref != slot["native_parent_ref"] or seen.native_depth != slot["native_depth"]
                or type(seen.native_depth) is not int or not 1 <= seen.native_depth <= min(pool["max_depth"], backend_limits()[1])
                or not isinstance(seen.evidence_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", seen.evidence_sha256) is None):
            raise ResourceDenied("the same nonterminal owned native identity and ancestry must be freshly observed")
        return {"route": route, "target": target, "adoption_event_id": adopted[0]["event_id"],
            "principal_subject": assignment["subject"], "source_run_id": source_id, "destination_run_id": destination_id,
            "source_revision": int(source["revision"]), "destination_revision": int(destination["revision"]),
            "database_sha256": ids.sha256_of_obj({"slot": dict(slot), "pool": dict(pool), "source": dict(source),
                "destination": dict(destination), "source_task": dict(source_task), "destination_task": dict(destination_task),
                "source_assignment": dict(assignment), "destination_assignment": dict(destination_assignment),
                "source_team": dict(source_team), "destination_teams": destination_teams, "destination_member": dict(member),
                "cancellation_receipts": [dict(row) for row in cancellations], "ownership": dict(ownership)})}

    def _unclaimed_transfer_commit(self, before: Mapping[str, Any], pool_before: Mapping[str, Any],
                                   destination_task_id: str, handoff: Mapping[str, Any],
                                   seen: codex.NativeObservation, proof: Mapping[str, Any], actor: str) -> dict[str, Any]:
        with store.transaction(self.conn):
            slot, pool = self._slot(before["slot_id"]), self._pool(pool_before["pool_id"])
            self._fence(pool, slot, before["generation"], expected_pool_generation=pool_before["session_generation"])
            if proof != self._unclaimed_transfer_checks(slot, pool, destination_task_id, handoff, seen):
                raise StaleRevision("unclaimed transfer authorization or intended task changed before commit")
            old_total = self._occupied(pool["pool_id"]) + pool["external_occupied"]
            parent = self._slot(slot["logical_parent_slot_id"]) if slot["logical_parent_slot_id"] else None
            detached = parent is not None and _current_run(parent) != handoff["destination_run_id"]
            fields = {"current_run_id": handoff["destination_run_id"], "task_id": destination_task_id}
            if detached:
                fields.update(logical_parent_slot_id=None, logical_depth=1)
            result = self._write(slot, actor, fields=fields, kind="host_pool.unclaimed_transferred",
                reason="exact authorized contract migration of a cancelled never-claimed owned task; capacity retained")
            if old_total != self._occupied(pool["pool_id"]) + self._pool(pool["pool_id"])["external_occupied"]:
                raise ContentHashMismatch("unclaimed native transfer changed occupied capacity")
            payload = {"action": UNCLAIMED_TRANSFER, "origin_run_id": slot["run_id"],
                "previous_binding": {"run_id": _current_run(slot), "task_id": slot["task_id"], "lease_id": None},
                "binding": {"run_id": handoff["destination_run_id"], "task_id": destination_task_id, "lease_id": None},
                "principal_subject": proof["principal_subject"], "slot_id": slot["slot_id"],
                "native_agent_ref": slot["native_agent_ref"], "native_parent_ref": slot["native_parent_ref"],
                "native_depth": slot["native_depth"], "previous_generation": slot["generation"],
                "generation": result["generation"], "pool_generation": slot["pool_generation"],
                "task_bound": False, "native_created_by_gx": False, "capacity_released": False,
                "occupied_before": old_total, "occupied_after": old_total, "detach_to_root": detached,
                "authorization": dict(handoff), "migration_route": proof["route"],
                "source_adoption_event_id": proof["adoption_event_id"],
                "native_evidence_sha256": seen.evidence_sha256,
                "native_packet_sha256": proof["target"]["native_packet_sha256"],
                "destination_task_sha256": proof["target"]["task_sha256"],
                "destination_assignment_sha256": proof["target"]["assignment_sha256"],
                "assignment_generation": proof["target"]["assignment_generation"],
                "destination_policy_sha256": proof["target"]["policy_sha256"],
                "destination_contract_sha256": proof["route"]["target_contract_sha"]}
            payload["evidence_sha256"] = ids.sha256_of_obj(payload)
            revisions = {}
            for prefix in ("source", "destination"):
                run_id = handoff[prefix + "_run_id"]
                previous_revision, revision = store.bump_revision(self.conn, run_id, handoff["expected_" + prefix + "_revision"])
                revisions[run_id] = revision
                store.append_event(self.conn, kind="host_pool.unclaimed_run_handoff", actor=actor, run_id=run_id,
                    subject_kind="host_pool_slot", subject_id=slot["slot_id"], prev_revision=previous_revision,
                    new_revision=revision, evidence_level="EXECUTION_OBSERVED", payload=payload)
            result["handoff"] = {"action": UNCLAIMED_TRANSFER, "source_run_id": _current_run(slot),
                "destination_run_id": handoff["destination_run_id"], "detach_to_root": detached,
                "revisions": revisions, "evidence_sha256": payload["evidence_sha256"], "task_bound": False}
            return result

    def _handoff_checks(self, slot: Mapping[str, Any], pool: Mapping[str, Any],
                        lease: sqlite3.Row, request: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only preflight and commit-time authorization for an explicit run move."""
        from ..orchestration import service as runs
        from ..teams.manager import _context, assignment_intent

        if set(request) != set(HANDOFF_FIELDS):
            raise SchemaInvalid("cross-run next_task requires both run IDs, coordinator leases and revisions")
        source_id, destination_id = _current_run(slot), lease["run_id"]
        if request["source_run_id"] != source_id or request["destination_run_id"] != destination_id:
            raise SchemaInvalid("handoff run assertions do not match the current source binding and new task lease")
        if source_id == destination_id:
            raise SchemaInvalid("handoff authorization is only supplied for a cross-run next_task")
        if self.ctx is None or self.ctx.conn is not self.conn or self.ctx.principal.scope != SCOPE_HOST_ADAPTER:
            raise ScopeDenied("cross-run native reuse requires the actual host_adapter context")
        source, destination = self._require_run(source_id), self._require_run(destination_id)
        if source["project_ref"] != destination["project_ref"]:
            raise ScopeDenied("native task reuse cannot cross project scope")
        for run in (source, destination):
            if run["status"] == "PAUSED" or machines.is_terminal("run", run["status"]):
                raise IllegalTransition("a paused or ended source/destination run cannot authorize a new handoff")
            host = store.loads(run, "host_json", {}) or {}
            if host.get("kind") != pool["host"] or host.get("session_ref") != pool["session_ref"]:
                raise ScopeDenied("both handoff runs must use the pool's actual Host root/session")
        for prefix, run in (("source", source), ("destination", destination)):
            _integer(request["expected_" + prefix + "_revision"], "expected_" + prefix + "_revision")
            store.check_revision(self.conn, run["run_id"], request["expected_" + prefix + "_revision"])
            lease_ref = _text(request[prefix + "_coordinator_lease_ref"], prefix + "_coordinator_lease_ref")
            coordinator = runs.require_coordinator(self.ctx, self.conn, run["run_id"], lease_ref, renew=False)
            if (coordinator["owner"] != self.ctx.principal.subject
                    or coordinator["host"] != pool["host"] or coordinator["session_ref"] != pool["session_ref"]):
                raise ScopeDenied("both real coordinator leases must identify this caller and the same native root")
        old = self.conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (slot["task_id"],)).fetchone()
        if old is None or old["run_id"] != source_id:
            raise ContentHashMismatch("the previous task does not belong to the slot's effective source run")
        assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (lease["task_id"],)).fetchone()
        if assignment is not None:
            assignment_intent(self.conn, assignment)
            if assignment["context_ref"]:
                _context(self.conn, destination_id, assignment["context_ref"])
        children = self.conn.execute(
            "SELECT slot_id FROM host_pool_slots WHERE logical_parent_slot_id=? AND state<>? LIMIT 21",
            (slot["slot_id"], TERMINATED)).fetchall()
        if children:
            raise IllegalTransition("detach the live logical children before handing their parent to another run",
                                    details={"logical_children": [row["slot_id"] for row in children[:20]],
                                             "truncated": len(children) > 20})
        return {"source_run_id": source_id, "destination_run_id": destination_id,
                "source_revision": int(source["revision"]), "destination_revision": int(destination["revision"])}

    def _fence_native_policy(self, slot: Mapping[str, Any], pool: Mapping[str, Any],
                             lease_id: str, binding: Mapping[str, Any] | None, *,
                             required: bool = False, fenced: _FencedBinding | None = None) -> _FencedBinding | None:
        """Fence new Codex evidence once, before settle creates this operation's events."""
        from . import codex_worker_observation
        lease = self.conn.execute("SELECT l.*,t.run_id FROM leases l JOIN tasks t ON t.task_id=l.task_id"
                                  " WHERE l.lease_id=?", (lease_id,)).fetchone()
        required = required or (pool["host"] == "codex" and lease is not None
                               and codex_worker_observation.scoped_run(self.conn, lease["run_id"]))
        if not required:
            return None
        if (pool["host"] != "codex" or not isinstance(binding, Mapping)
                or binding.get("policy_verdict") != "CONFORMS"
                or binding.get("model_authority") != "codex_host_applied_metadata"):
            raise ContentHashMismatch("native reuse needs this lease's current causal and model observation")
        expected = _FencedBinding(lease_id, slot["native_agent_ref"], ids.sha256_of_obj(binding))
        if fenced is not None:
            if type(fenced) is not _FencedBinding or fenced != expected or not self.conn.in_transaction:
                raise ContentHashMismatch("the native binding was not fenced for this consuming operation")
            return fenced
        if not codex_worker_observation.fence_binding_observation(
                self.conn, binding, lease_id=lease_id, native_agent_ref=slot["native_agent_ref"]):
            raise ContentHashMismatch("the new lease's native policy observation changed before handoff")
        return expected

    def _simple(self, slot_id: str, *, expected_generation: int, actor: str, action: str,
                expected_pool_generation: int | None = None, lease_id: str | None = None,
                verified_binding: Mapping[str, Any] | None = None,
                handoff: Mapping[str, Any] | None = None,
                fenced_binding: _FencedBinding | None = None) -> dict[str, Any]:
        backend_limits()
        with store.transaction(self.conn):
            slot = self._slot(slot_id)
            self._require_run(_current_run(slot))
            pool = self._pool(slot["pool_id"])
            self._fence(pool, slot, expected_generation, allow_old_pool=action == "adopt",
                        expected_pool_generation=expected_pool_generation)
            state = slot["state"]
            if action == "adopt":
                if state == TERMINATED:
                    raise IllegalTransition("a terminated slot needs no adoption")
                return self._write(slot, actor, fields={"pool_generation": pool["session_generation"]},
                                   reason="adopted without changing occupancy or claiming native termination")
            if action == "creation_unknown":
                if state not in (CREATING, UNKNOWN):
                    raise IllegalTransition("only a pending native creation may become unknown")
                return self._write(slot, actor, state=UNKNOWN, reason="native create outcome unknown; capacity retained")
            if action == "request_close":
                if state == TERMINATED:
                    return slot
                if state == RESERVED:
                    proof = {"source": "backend_process", "kind": "native_call_never_started",
                             "slot_id": slot_id, "generation": slot["generation"], "observed_at": clock.now_iso()}
                    return self._write(slot, actor, state=TERMINATED, fields={
                        "termination_evidence_json": ids.canonical_json(proof),
                        "observed_source": "backend_process", "observed_at": proof["observed_at"]},
                        reason="backend proved this untouched reservation never reached Host")
                if state not in (CREATING, UNKNOWN, ACTIVE, CLOSING):
                    raise IllegalTransition("the native slot cannot be closed from its current state")
                return self._write(slot, actor, state=CLOSING, reason="native close requested; capacity retained")
            if action in ("bind", "next_task"):
                if state != ACTIVE or not slot["native_agent_ref"]:
                    raise IllegalTransition("a task lease can bind only to an observed active native worker")
                _text(lease_id, "lease_id")
                lease = self.conn.execute(
                    "SELECT l.*,t.run_id,t.lease_id AS current_lease,t.attempt AS current_attempt "
                    "FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?",
                    (lease_id,),
                ).fetchone()
                if (lease is None or lease["released_at"] is not None
                        or clock.parse(lease["expires_at"]) <= clock.now()
                        or lease["current_lease"] != lease_id or lease["current_attempt"] != lease["attempt"]
                        or (action == "bind" and slot["task_id"] is not None and lease["task_id"] != slot["task_id"])):
                    raise IllegalTransition("the task lease is not current for this native reservation")
                cross_run = lease["run_id"] != _current_run(slot)
                if cross_run and action != "next_task":
                    raise IllegalTransition("a native bind cannot silently move a slot to another run")
                if not cross_run and handoff:
                    raise SchemaInvalid("handoff fields are supplied only for a cross-run next_task")
                authorization = self._handoff_checks(slot, pool, lease, handoff or {}) if cross_run else None
                if action == "bind" and slot["lease_id"] is not None and slot["lease_id"] != lease_id:
                    raise IllegalTransition("a native slot cannot silently replace its bound task lease")
                if action == "next_task":
                    self._require_previous_binding_settled(slot, lease, pool)
                planned = (slot["metadata"].get("adoption") or {}).get("principal_subject")
                if planned is not None and lease["principal"] != planned:
                    raise IllegalTransition("the native adoption intended principal differs from the actual task lease")
                tie = self.conn.execute(
                    "SELECT 1 FROM worker_observations WHERE task_id=? AND lease_id=? AND host=? "
                    "AND host_session_ref=? AND agent_ref=? AND evidence_level='EXECUTION_OBSERVED' "
                    "AND observation_source IN ('host_transcript','host_hook','backend_process') LIMIT 1",
                    (lease["task_id"], lease_id, pool["host"], pool["session_ref"], slot["native_agent_ref"]),
                ).fetchone()
                native_policy = self._fence_native_policy(slot, pool, lease_id, verified_binding,
                                                         required=cross_run, fenced=fenced_binding)
                receipt_slot = {**slot, "current_run_id": lease["run_id"]}
                if ((native_policy is not None or tie is None)
                        and not self._claim_receipt_current(verified_binding, receipt_slot, pool, lease)):
                    raise ContentHashMismatch("no Host-observed worker record ties this native identity to the task lease")
                previous = {"run_id": _current_run(slot), "task_id": slot["task_id"], "lease_id": slot["lease_id"]}
                fields = {"lease_id": lease_id, "task_id": lease["task_id"]}
                detached = False
                if cross_run:
                    fields["current_run_id"] = lease["run_id"]
                    parent = self._slot(slot["logical_parent_slot_id"]) if slot["logical_parent_slot_id"] else None
                    if parent is not None and _current_run(parent) != lease["run_id"]:
                        fields.update(logical_parent_slot_id=None, logical_depth=1)
                        detached = True
                if tie is None or cross_run:
                    metadata = {**(slot["metadata"] or {}), "codex_claim_binding": {
                        "claim_event_id": verified_binding["claim_event_id"],
                        "claim_call_id": verified_binding["claim_call_id"], "source": "host_transcript"}}
                    fields["metadata_json"] = ids.canonical_json(metadata)
                if cross_run:
                    from ..orchestration.service import require_coordinator
                    for prefix in ("source", "destination"):
                        require_coordinator(self.ctx, self.conn, authorization[prefix + "_run_id"],
                                            handoff[prefix + "_coordinator_lease_ref"])
                result = self._write(slot, actor, fields=fields,
                                     kind="host_pool.bound", reason="native identity tied to the task's observed lease")
                payload = {"action": action, "native_agent_ref": slot["native_agent_ref"],
                           "previous_binding": previous,
                           "binding": {"run_id": lease["run_id"], "task_id": lease["task_id"], "lease_id": lease_id},
                           "generation": result["generation"], "capacity_released": False}
                if cross_run:
                    revisions = {}
                    for prefix in ("source", "destination"):
                        run_id = authorization[prefix + "_run_id"]
                        revisions[run_id] = store.bump_revision(self.conn, run_id, authorization[prefix + "_revision"])
                    payload.update(origin_run_id=slot["run_id"], detach_to_root=detached,
                        native_parent_ref=slot["native_parent_ref"], native_depth=slot["native_depth"],
                        authorization={key: value for key, value in handoff.items()},
                        model_observation_sha256=verified_binding["model_observation"]["observation_sha256"])
                    for run_id, (previous_revision, revision) in revisions.items():
                        store.append_event(self.conn, kind="host_pool.run_handoff", actor=actor, run_id=run_id,
                            subject_kind="host_pool_slot", subject_id=slot["slot_id"],
                            prev_revision=previous_revision, new_revision=revision,
                            evidence_level="EXECUTION_OBSERVED", payload=payload)
                    result["handoff"] = {**authorization, "detach_to_root": detached,
                                         "revisions": {run_id: pair[1] for run_id, pair in revisions.items()}}
                store.append_event(self.conn, kind="host_pool.bound", actor=actor, run_id=_current_run(result),
                    subject_kind="host_pool_slot", subject_id=slot["slot_id"], evidence_level="EXECUTION_OBSERVED",
                    payload=payload)
                return result
            raise SchemaInvalid(f"unknown Host pool transition action {action!r}")


    def _claim_receipt_current(self, binding: Mapping[str, Any] | None, slot: Mapping[str, Any],
                               pool: Mapping[str, Any], lease: sqlite3.Row) -> bool:
        """Only a backend atomic claim receipt can replace a transcript observation.

        The expensive Host verification already happened outside this transaction.
        This is the commit-time check of its lease, receipt, pin and assignment CAS.
        """
        if not isinstance(binding, Mapping) or pool["host"] != "codex":
            return False
        from .codex_dispatch import CLAIM_EVENT
        event = self.conn.execute("SELECT * FROM events WHERE event_id=?", (binding.get("claim_event_id"),)).fetchone()
        if (event is None or event["kind"] != CLAIM_EVENT or event["run_id"] != _current_run(slot)
                or event["subject_id"] != lease["task_id"] or event["evidence_level"] != "EXECUTION_OBSERVED"):
            return False
        payload = json.loads(event["payload_json"])
        digest = payload.pop("evidence_sha256", None)
        if digest != ids.sha256_of_obj(payload):
            return False
        checks = {"lease_id": lease["lease_id"], "task_id": lease["task_id"], "run_id": _current_run(slot),
                  "principal_subject": lease["principal"], "native_agent_ref": slot["native_agent_ref"],
                  "session_ref": pool["session_ref"], "contract_sha256": contracts.contract_digest()["__combined__"]}
        if any(binding.get(key) != value or payload.get(key) != value for key, value in checks.items()):
            return False
        if (binding.get("source") != "host_transcript" or payload.get("source") != "host_transcript"
                or payload.get("attempt") != lease["attempt"]
                or payload.get("claim_call_id") != binding.get("claim_call_id")
                or payload.get("dispatch_call_id") != binding.get("dispatch_call_id")):
            return False
        assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (lease["task_id"],)).fetchone()
        if assignment is None:
            return binding.get("assignment_generation") is None and payload.get("assignment_generation") is None
        expected = f"codex:{pool['session_ref']}:{slot['native_agent_ref']}"
        return (assignment["assignment_generation"] == binding.get("assignment_generation")
                == payload.get("assignment_generation") and assignment["status"] == "ASSIGNED"
                and assignment["pending_json"] is None and assignment["subject"] == lease["principal"]
                and assignment["observed_worker_ref"] in (None, expected))

    def _require_previous_binding_settled(self, slot: Mapping[str, Any], new_lease: sqlite3.Row,
                                         pool: Mapping[str, Any]) -> None:
        config.check_holds()
        if not slot["lease_id"] or not slot["task_id"] or slot["lease_id"] == new_lease["lease_id"]:
            raise IllegalTransition("next_task requires a distinct, previously bound and settled task lease")
        old = self.conn.execute(
            "SELECT l.*,t.status AS task_status FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?",
            (slot["lease_id"],),
        ).fetchone()
        if old is None or old["released_at"] is None or not machines.is_terminal("task", old["task_status"]):
            raise IllegalTransition("the previous task and its lease must settle before the native worker is reused")
        if (old["principal"] or old["owner"]) != (new_lease["principal"] or new_lease["owner"]):
            raise IllegalTransition("reusing a native worker cannot silently change its backend principal")
        for job in self.conn.execute("SELECT status,side_effect_status FROM jobs WHERE task_id=? OR lease_ref=?",
                                     (slot["task_id"], slot["lease_id"])):
            if not machines.is_terminal("job", job["status"]) or job["side_effect_status"] == "UNKNOWN":
                raise IllegalTransition("a previous task's active or unknown job still owns its resources")
        expected_worker = f"{pool['host']}:{pool['session_ref']}:{slot['native_agent_ref']}"
        for task_id, principal in ((slot["task_id"], old["principal"] or old["owner"]),
                                    (new_lease["task_id"], new_lease["principal"] or new_lease["owner"])):
            assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
            if assignment is not None and (
                assignment["status"] != "ASSIGNED" or assignment["subject"] != principal
                or assignment["observed_worker_ref"] not in (None, expected_worker)
                or assignment["pending_json"] is not None
            ):
                raise IllegalTransition("the task assignment's identity or handoff has not settled for native reuse")

    def _next_task(self, before: Mapping[str, Any], pool_before: Mapping[str, Any],
                   seen: codex.NativeObservation, lease_id: str | None, actor: str,
                   verified_binding: Mapping[str, Any] | None = None,
                   handoff: Mapping[str, Any] | None = None) -> dict[str, Any]:
        config.check_holds()
        if not seen.readable or seen.terminal_confirmed or seen.native_agent_ref != before["native_agent_ref"]:
            raise ResourceDenied("reusing a native worker requires a fresh observation of the same active identity")
        fenced_binding = self._fence_native_policy(before, pool_before, lease_id, verified_binding,
                                                   required=bool(handoff))
        settled = self._settle(before, pool_before, seen, actor)
        return self._simple(settled["slot_id"], expected_generation=settled["generation"],
                            actor=actor, action="next_task", lease_id=lease_id,
                            expected_pool_generation=int(pool_before["session_generation"]), verified_binding=verified_binding,
                            handoff=handoff, fenced_binding=fenced_binding)

    def _require_fresh_admission(self, slot: Mapping[str, Any]) -> None:
        pool = self._pool(str(slot["pool_id"]))
        run_id = str(slot.get("current_run_id") or slot["run_id"])
        required = self._requires_inventory(run_id, pool)
        metadata = slot.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ResourceDenied("the native admission metadata is not readable")
        admission = metadata.get("native_inventory_admission")
        if admission is None:
            if required:
                raise ResourceDenied("this existing intent has no authenticated native admission; close it and reserve freshly")
            return  # Actual legacy family AND backend has never enabled inventory.
        try:
            if (not isinstance(admission, Mapping) or set(admission) != {"call_id", "snapshot_at", "evidence_sha256", "receipt_id"}
                    or any(not isinstance(admission.get(key), str) or not admission[key] for key in admission)
                    or len(admission["call_id"]) > 128
                    or any(len(admission[key]) != 64 or any(c not in "0123456789abcdef" for c in admission[key])
                           for key in ("evidence_sha256", "receipt_id"))):
                raise ValueError("no complete admission packet")
            age = clock.age_seconds(admission["snapshot_at"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ResourceDenied("the native admission timestamp or packet is not readable") from exc
        if age < 0 or age > MAX_INVENTORY_AGE_SECONDS:
            raise ResourceDenied("native inventory expired before creation; close the untouched intent and reserve freshly")
        receipt = self.conn.execute("SELECT * FROM host_pool_inventory_receipts WHERE receipt_id=?", (admission["receipt_id"],)).fetchone()
        if (receipt is None or receipt["pool_id"] != pool["pool_id"]
                or receipt["pool_generation"] != pool["session_generation"]
                or slot["pool_generation"] != pool["session_generation"]
                or receipt["source_native_ref"] != pool["session_ref"]
                or receipt["native_call_id"] != admission["call_id"] or receipt["snapshot_at"] != admission["snapshot_at"]):
            raise ResourceDenied("the native admission has no matching current backend inventory receipt")
        try:
            evidence = json.loads(receipt["source_evidence_json"])
            if not isinstance(evidence, dict):
                raise ValueError("no structured source evidence")
        except (TypeError, ValueError) as exc:
            raise ResourceDenied("the native inventory receipt is not readable") from exc
        expected_receipt = ids.sha256_of_obj({"pool_id": pool["pool_id"], "call_id": admission["call_id"],
                                             "evidence_sha256": ids.sha256_of_obj(evidence)})
        if ids.sha256_of_obj(evidence) != admission["evidence_sha256"] or expected_receipt != admission["receipt_id"]:
            raise ResourceDenied("the native admission receipt no longer matches its source evidence")
        events = self.conn.execute("SELECT run_id,payload_json FROM events WHERE kind='host_pool.reserved' "
            "AND subject_kind='host_pool_slot' AND subject_id=? AND evidence_level='EXECUTION_OBSERVED'", (slot["slot_id"],)).fetchall()
        if len(events) != 1:
            raise ResourceDenied("the native admission has no unambiguous backend reservation event")
        try:
            reserved = json.loads(events[0]["payload_json"])
            if not isinstance(reserved, dict):
                raise ValueError("no structured backend reservation event")
        except (TypeError, ValueError) as exc:
            raise ResourceDenied("the native reservation event is not readable") from exc
        if (events[0]["run_id"] != slot["run_id"] or reserved.get("pool_id") != pool["pool_id"]
                or reserved.get("pool_generation") != pool["session_generation"]
                or reserved.get("native_inventory_admission") != dict(admission)):
            raise ResourceDenied("legacy or caller-shaped metadata is not authenticated admission for this intent")

    def _creator_observation(self, slot_id: str, expected_generation: int,
                             expected_pool_generation: int | None) -> tuple[dict[str, Any], dict[str, Any], codex.NativeObservation]:
        assert_external_call_allowed(self.conn)
        config.check_holds()
        _, depth_ceiling = backend_limits()
        slot = self._slot(slot_id)
        self._require_run(_current_run(slot))
        pool = self._pool(slot["pool_id"])
        self._fence(pool, slot, expected_generation, expected_pool_generation=expected_pool_generation)
        if slot["state"] != RESERVED:
            raise IllegalTransition("only an untouched reservation may begin one native creation")
        self._require_fresh_admission(slot)
        creator_ref = slot["native_creator_ref"]
        if not creator_ref:
            raise ResourceDenied("this historical reservation has no observed native creator")
        seen = self.observer(host=pool["host"], session_ref=pool["session_ref"], native_agent_ref=creator_ref)
        self._validate_creator(pool, seen, creator_ref, depth_ceiling)
        return slot, pool, seen


    def _begin_create(self, before: Mapping[str, Any], pool_before: Mapping[str, Any],
                      seen: codex.NativeObservation, actor: str) -> dict[str, Any]:
        slot, pool = self._slot(str(before["slot_id"])), self._pool(str(before["pool_id"]))
        self._require_run(_current_run(slot))
        config.check_holds()
        _, depth_ceiling = backend_limits()
        self._fence(pool, slot, int(before["generation"]),
                    expected_pool_generation=int(pool_before["session_generation"]))
        if slot["state"] != RESERVED:
            raise IllegalTransition("only an untouched reservation may begin one native creation")
        self._validate_creator(pool, seen, str(slot["native_creator_ref"]), depth_ceiling)
        # Host I/O and waiting for this transaction can consume the remaining TTL.
        # Recheck under the writer immediately before persisting any create intent.
        self._require_fresh_admission(slot)
        return self._write(slot, actor, state=CREATING, fields={
            "native_creator_depth": seen.native_depth,
            "native_creator_evidence_json": ids.canonical_json(seen.to_dict())},
            reason="native creation intent committed after observing the actual creator's depth")

    def _observe(self, slot_id: str, expected_generation: int,
                 native_agent_ref: str | None, expected_pool_generation: int | None) -> tuple[dict[str, Any], dict[str, Any], codex.NativeObservation]:
        assert_external_call_allowed(self.conn)
        backend_limits()
        slot, pool = self._slot(slot_id), None
        self._require_run(_current_run(slot))
        pool = self._pool(slot["pool_id"])
        self._fence(pool, slot, expected_generation, expected_pool_generation=expected_pool_generation)
        if slot["state"] in (RESERVED, TERMINATED):
            raise IllegalTransition("only a started nonterminal native request can be reconciled")
        native = native_agent_ref or slot["native_agent_ref"]
        _text(native, "native_agent_ref")
        if slot["native_agent_ref"] is not None and native != slot["native_agent_ref"]:
            raise ContentHashMismatch("a native reservation cannot change the worker it identifies")
        seen = self.observer(host=pool["host"], session_ref=pool["session_ref"], native_agent_ref=native)
        if not isinstance(seen, codex.NativeObservation):
            raise SchemaInvalid("the internal native observer returned an unsupported evidence product")
        return slot, pool, seen

    def _settle(self, before: Mapping[str, Any], pool_before: Mapping[str, Any],
                seen: codex.NativeObservation, actor: str) -> dict[str, Any]:
        slot, pool = self._slot(str(before["slot_id"])), self._pool(str(before["pool_id"]))
        self._require_run(_current_run(slot))
        self._fence(pool, slot, int(before["generation"]),
                    expected_pool_generation=int(pool_before["session_generation"]))
        if (seen.host != pool["host"] or seen.session_ref != pool["session_ref"]
                or (slot["native_agent_ref"] is not None and seen.native_agent_ref != slot["native_agent_ref"])):
            raise ContentHashMismatch("the native observation does not identify the reserved Host worker")
        metadata = dict(slot["metadata"] or {})
        metadata["native_observation"] = seen.to_dict()
        fields: dict[str, Any] = {"metadata_json": ids.canonical_json(metadata)}
        state = slot["state"]
        if not seen.readable:
            if state == CREATING:
                state = UNKNOWN
            return self._write(slot, actor, state=state, fields=fields,
                               reason="native evidence unavailable; capacity retained")
        if seen.source not in ("host_transcript", "backend_process") or len(seen.evidence_sha256) != 64:
            raise ContentHashMismatch("the native observation has no independently read evidence provenance")
        _text(seen.native_agent_ref, "native_agent_ref")
        if seen.native_parent_ref != slot["native_creator_ref"]:
            raise ContentHashMismatch("the observed native parent differs from the reserved actual creator")
        # Native depth stays unknown unless Host supplied it, never copied from logical depth.
        if seen.native_depth is not None:
            _integer(seen.native_depth, "native_depth")
        fields.update({"native_agent_ref": seen.native_agent_ref, "native_parent_ref": seen.native_parent_ref,
                       "native_depth": seen.native_depth, "observed_source": seen.source,
                       "observed_at": seen.observed_at})
        duplicate = self.conn.execute(
            "SELECT slot_id FROM host_pool_slots WHERE pool_id=? AND native_agent_ref=? "
            "AND slot_id<>? AND state<>? LIMIT 1",
            (slot["pool_id"], seen.native_agent_ref, slot["slot_id"], TERMINATED),
        ).fetchone()
        if duplicate is not None:
            raise IdempotencyConflict("this native worker already occupies another nonterminal slot, including an earlier generation")
        if seen.terminal_confirmed:
            if not seen.terminal_reason:
                raise ContentHashMismatch("a native termination observation needs its verified reason")
            state = TERMINATED
            fields["termination_evidence_json"] = ids.canonical_json(seen.to_dict())
        else:
            _, ceiling = backend_limits()
            if seen.native_depth is not None and seen.native_depth > min(ceiling, int(pool["max_depth"])):
                state = CLOSING  # preserve actual observation and charge the unexpected native worker
                metadata["native_depth_violation"] = True
                fields["metadata_json"] = ids.canonical_json(metadata)
            elif state != CLOSING:
                state = ACTIVE
        try:
            return self._write(slot, actor, state=state, fields=fields,
                               reason="native reader evidence settled with pool and slot generation fencing")
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflict("this native worker is already charged to another slot in the same pool generation") from exc

    def transition(
        self, slot_id: str, *, action: str, expected_generation: int, actor: str,
        native_agent_ref: str | None = None, lease_id: str | None = None,
        expected_pool_generation: int | None = None, idempotency_key: str | None = None,
        source_run_id: str | None = None, destination_run_id: str | None = None,
        source_coordinator_lease_ref: str | None = None, destination_coordinator_lease_ref: str | None = None,
        expected_source_revision: int | None = None, expected_destination_revision: int | None = None,
        destination_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Tool-ready transitions; observation is outside the idempotent commit unit.

        Repeating an observation is safe because it only reads Host state. A completed
        key is checked before that read, and checked again under the commit lock.
        """
        request = {"slot_id": slot_id, "action": action, "expected_generation": expected_generation,
                   "native_agent_ref": native_agent_ref, "lease_id": lease_id,
                   "expected_pool_generation": expected_pool_generation}
        handoff = {key: value for key, value in {
            "source_run_id": source_run_id, "destination_run_id": destination_run_id,
            "source_coordinator_lease_ref": source_coordinator_lease_ref,
            "destination_coordinator_lease_ref": destination_coordinator_lease_ref,
            "expected_source_revision": expected_source_revision,
            "expected_destination_revision": expected_destination_revision}.items() if value is not None}
        request.update(handoff)
        if destination_task_id is not None:
            request["destination_task_id"] = destination_task_id
        if handoff:
            request["handoff_principal"] = self.principal.subject if self.principal is not None else None
        key = store.tool_key("gx_host_pool_transition", _text(idempotency_key, "idempotency_key")) if idempotency_key is not None else None
        # Verify scope before returning a cached identity-bearing result.
        self._scope_run(_current_run(self._slot(slot_id)))
        if action == UNCLAIMED_TRANSFER:
            for name in ("source_run_id", "destination_run_id"):
                self._scope_run(_text(handoff.get(name), name))
        replay = self._replay("gx_host_pool_transition", key, request)
        if replay is not None:
            return replay["slot"]
        if destination_task_id is not None and action != UNCLAIMED_TRANSFER:
            raise SchemaInvalid("destination_task_id is supplied only for unclaimed_owned_transfer")
        if action == UNCLAIMED_TRANSFER:
            assert_external_call_allowed(self.conn)
            backend_limits()
            _integer(expected_pool_generation, "expected_pool_generation", 1)
            _text(destination_task_id, "destination_task_id")
            if key is None or lease_id is not None:
                raise SchemaInvalid("unclaimed transfer requires a durable key and accepts no task lease")
            before = self._slot(slot_id)
            current_pool = self._pool(before["pool_id"])
            self._fence(current_pool, before, expected_generation, expected_pool_generation=expected_pool_generation)
            if native_agent_ref is not None and native_agent_ref != before["native_agent_ref"]:
                raise SchemaInvalid("the native identity assertion differs from the owned slot")
            self._unclaimed_transfer_checks(before, current_pool, destination_task_id, handoff)
            seen = self.observer(host=current_pool["host"], session_ref=current_pool["session_ref"],
                                 native_agent_ref=before["native_agent_ref"])
            if (not isinstance(seen, codex.NativeObservation) or not isinstance(seen.current_turn, dict)
                    or not isinstance(seen.current_turn.get("evidence_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", seen.current_turn["evidence_sha256"]) is None):
                raise ResourceDenied("the owned native identity has no readable applied-metadata evidence packet")
            proof = self._unclaimed_transfer_checks(before, current_pool, destination_task_id, handoff, seen)
            work = lambda conn: {"slot": self._unclaimed_transfer_commit(before, current_pool,
                destination_task_id, handoff, seen, proof, actor)}
            response, _ = store.run_idempotent(self.conn, key, "gx_host_pool_transition", request, work)
            return response["slot"]
        if handoff and action != "next_task":
            raise SchemaInvalid("run handoff fields are allowed only on next_task")
        verified_binding = None
        if action in ("bind", "next_task"):
            assert_external_call_allowed(self.conn)
            current = self._slot(slot_id)
            current_pool = self._pool(current["pool_id"])
            self._require_run(_current_run(current))
            self._fence(current_pool, current, expected_generation,
                        expected_pool_generation=expected_pool_generation)
            next_lease = self.conn.execute(
                "SELECT l.*,t.run_id,t.lease_id AS current_lease,t.attempt AS current_attempt"
                " FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?", (lease_id,)).fetchone()
            if action == "next_task" and next_lease is not None and next_lease["run_id"] != _current_run(current):
                _integer(expected_pool_generation, "expected_pool_generation", 1)
                if key is None:
                    raise SchemaInvalid("cross-run next_task requires a durable idempotency key")
                if (next_lease["released_at"] is not None or clock.parse(next_lease["expires_at"]) <= clock.now()
                        or next_lease["current_lease"] != lease_id
                        or next_lease["current_attempt"] != next_lease["attempt"]):
                    raise IllegalTransition("the destination task lease is not current and live")
                self._handoff_checks(current, current_pool, next_lease, handoff)
                self._require_previous_binding_settled(current, next_lease, current_pool)
            elif next_lease is not None and next_lease["run_id"] != _current_run(current):
                raise IllegalTransition("bind cannot read or adopt a lease in another run; use an authorized next_task")
            elif handoff:
                raise SchemaInvalid("handoff fields must name an actual new task lease in another run")
            if current_pool["host"] == "codex" and current["native_agent_ref"] and lease_id:
                from .codex_dispatch import verified_lease_binding
                verified_binding = verified_lease_binding(self.conn, lease_id=lease_id,
                                                          native_agent_ref=current["native_agent_ref"])
        if action == "begin_create":
            before, pool, seen = self._creator_observation(slot_id, expected_generation, expected_pool_generation)
            work = lambda conn: {"slot": self._begin_create(before, pool, seen, actor)}
        elif action in ("reconcile", "next_task"):
            if action == "next_task":
                config.check_holds()
            before, pool, seen = self._observe(slot_id, expected_generation, native_agent_ref, expected_pool_generation)
            if action == "next_task":
                work = lambda conn: {"slot": self._next_task(before, pool, seen, lease_id, actor, verified_binding, handoff)}
            else:
                work = lambda conn: {"slot": self._settle(before, pool, seen, actor)}
        else:
            work = lambda conn: {"slot": self._simple(slot_id, expected_generation=expected_generation,
                actor=actor, action=action, expected_pool_generation=expected_pool_generation, lease_id=lease_id,
                verified_binding=verified_binding)}
        if key is not None:
            response, _ = store.run_idempotent(self.conn, key, "gx_host_pool_transition", request, work)
            return response["slot"]
        with store.transaction(self.conn):
            return work(self.conn)["slot"]

    def create(
        self, slot_id: str, *, expected_generation: int, actor: str,
        native_create: Callable[[Mapping[str, Any]], str],
    ) -> dict[str, Any]:
        """Internal adapter driver: commit intent, call Host once, then read evidence.

        A retry sees CREATING/UNKNOWN and is refused before invoking Host again.
        Exceptions, even a response missing its id, preserve unknown occupancy.
        """
        assert_external_call_allowed(self.conn)
        slot = self.transition(slot_id, action="begin_create", expected_generation=expected_generation, actor=actor)
        try:
            assert_external_call_allowed(self.conn)
            native_ref = native_create(slot)
            _text(native_ref, "native_agent_ref")
        except BaseException as exc:
            try:
                self.transition(slot_id, action="creation_unknown", expected_generation=slot["generation"], actor=actor)
            except StaleRevision:
                pass  # A takeover still retains the persisted CREATING slot.
            if not isinstance(exc, Exception):
                raise
            raise OutcomeUnknown("the native create response was lost or unusable; reconcile before any new create",
                                 details={"slot_id": slot_id, "exception_type": type(exc).__name__}) from exc
        return self.transition(slot_id, action="reconcile", expected_generation=slot["generation"],
                               native_agent_ref=native_ref, actor=actor)

    def reparent(self, slot_id: str, *, logical_parent_slot_id: str | None,
                 expected_generation: int, actor: str) -> dict[str, Any]:
        _, depth_ceiling = backend_limits()
        with store.transaction(self.conn):
            slot = self._slot(slot_id)
            self._require_run(_current_run(slot))
            pool = self._pool(slot["pool_id"])
            self._fence(pool, slot, expected_generation)
            if slot["state"] == TERMINATED:
                raise IllegalTransition("historical terminated slots are not reparented")
            subtree = list(self.conn.execute(
                "WITH RECURSIVE sub(slot_id) AS (SELECT ? UNION SELECT s.slot_id FROM host_pool_slots s "
                "JOIN sub ON s.logical_parent_slot_id=sub.slot_id WHERE COALESCE(s.current_run_id,s.run_id)=?) "
                "SELECT s.* FROM host_pool_slots s JOIN sub ON s.slot_id=sub.slot_id LIMIT ?",
                (slot_id, _current_run(slot), MAX_SUBTREE + 1),
            ))
            if len(subtree) > MAX_SUBTREE:
                raise ResourceDenied("the logical subtree exceeds the bounded reparent operation")
            if logical_parent_slot_id in {item["slot_id"] for item in subtree}:
                raise IllegalTransition("reparent would introduce a logical parent cycle")
            new_depth = 1
            if logical_parent_slot_id is not None:
                parent = self._slot(logical_parent_slot_id)
                if parent["pool_id"] != slot["pool_id"] or _current_run(parent) != _current_run(slot):
                    raise IllegalTransition("a logical parent must belong to the same pool and run")
                if parent["state"] in (CLOSING, TERMINATED) or parent["pool_generation"] != pool["session_generation"]:
                    raise IllegalTransition("the target logical parent is not current and open")
                new_depth = int(parent["logical_depth"]) + 1
            delta = new_depth - int(slot["logical_depth"])
            if any(int(item["logical_depth"]) + delta > min(depth_ceiling, int(pool["max_depth"])) for item in subtree):
                raise ResourceDenied("reparent would place part of the logical subtree beyond the depth ceiling")
            for raw in subtree:
                item = _row(raw)
                fields = {"logical_depth": int(item["logical_depth"]) + delta}
                if item["slot_id"] == slot_id:
                    fields["logical_parent_slot_id"] = logical_parent_slot_id
                self._write(item, actor, fields=fields, kind="host_pool.reparented",
                            reason="logical subtree reparented; native ancestry is unchanged")
            return self._slot(slot_id)

    def snapshot(self, pool_id: str, *, after_slot_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        ceiling, _ = backend_limits()
        _integer(limit, "limit", 1)
        if limit > MAX_PAGE:
            raise SchemaInvalid(f"limit must be at most {MAX_PAGE}")
        with _snapshot_transaction(self.conn):
            pool = self._pool(pool_id)
            counts = {row["state"]: int(row["n"]) for row in self.conn.execute(
                "SELECT state,COUNT(*) n FROM host_pool_slots WHERE pool_id=? GROUP BY state", (pool_id,))}
            owned_occupied = sum(n for state, n in counts.items() if state != TERMINATED)
            occupied = owned_occupied + int(pool["external_occupied"])
            total = capacity(pool, ceiling=ceiling)
            application = application_capacity(owned_occupied)
            native_available = max(0, total - occupied)
            visible_clause, visible_args = "", ()
            detailed_pool = self.principal is None or self.principal.scope == SCOPE_HOST_ADAPTER
            if self.principal is not None and not self.principal.allows_all_projects:
                projects = self.principal.project_refs
                visible_clause = " AND r.project_ref IN (" + ",".join("?" for _ in projects) + ")"
                visible_args = tuple(projects)
            rows = list(self.conn.execute(
                "SELECT s.* FROM host_pool_slots s JOIN runs r ON r.run_id=COALESCE(s.current_run_id,s.run_id) "
                "WHERE s.pool_id=? AND s.slot_id>?" + visible_clause + " ORDER BY s.slot_id LIMIT ?",
                (pool_id, after_slot_id or "", *visible_args, limit + 1)))
            page = rows[:limit]
            remaining = int(self.conn.execute(
                "SELECT COUNT(*) FROM host_pool_slots s JOIN runs r ON r.run_id=COALESCE(s.current_run_id,s.run_id) "
                "WHERE s.pool_id=? AND s.slot_id>?" + visible_clause,
                (pool_id, after_slot_id or "", *visible_args),
            ).fetchone()[0])
            if not detailed_pool:
                # Global identity/provenance may mention another project; workers only
                # receive global capacity facts and their own project's detailed slots.
                pool = {key: pool[key] for key in (
                    "pool_id", "host", "session_generation", "configured_capacity", "effective_capacity",
                    "external_occupied", "max_depth", "revision", "observed_source", "observed_at")}
            result = {"pool": {**_bounded_product(pool), "admission_capacity": total,
                               "native_available": native_available, "application_capacity": application}, "slots": [],
                      "counts": counts, "occupied": occupied, "available": min(native_available, application["available"]),
                      "truncated": remaining, "next_slot_id": None}
            for raw in page:
                projected = _bounded_product(_row(raw))
                trial = {**result, "slots": [*result["slots"], projected],
                         "truncated": remaining - len(result["slots"]) - 1,
                         "next_slot_id": raw["slot_id"]}
                if len(ids.canonical_json(trial).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                    break
                result = trial
            if result["truncated"] == 0:
                result["next_slot_id"] = None
            return result


def observe_backend_process(
    process: subprocess.Popen[Any], *, session_ref: str, native_agent_ref: str,
) -> codex.NativeObservation:
    """Evidence from a process the backend actually owns, never a reported PID/bool.

    An adapter can use this for a backend-owned native process. It cannot prove that
    an unrelated Codex thread ended and is deliberately bound to other_verified.
    """
    if not isinstance(process, subprocess.Popen):
        raise SchemaInvalid("backend process observation requires the backend's own Popen object")
    returncode = process.poll()
    proof = {"pid": process.pid, "returncode": returncode, "session_ref": session_ref,
             "native_agent_ref": native_agent_ref}
    return codex.NativeObservation(
        host="other_verified", session_ref=session_ref, native_agent_ref=native_agent_ref,
        observed_at=clock.now_iso(), readable=True, source="backend_process",
        evidence_sha256=ids.sha256_of_obj(proof), terminal_confirmed=returncode is not None,
        terminal_reason="backend-owned process reaped" if returncode is not None else None,
    )


def _adapter(ctx: Ctx) -> HostPoolService:
    if ctx.principal.scope != SCOPE_HOST_ADAPTER:
        raise PermissionDenied("Host pool mutations require the host_adapter scope")
    return HostPoolService(ctx.conn, ctx=ctx)


def _key(args: Mapping[str, Any]) -> str:
    return _text(args.get("idempotency_key"), "idempotency_key")


@tool("gx_host_pool_register")
def host_pool_register(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    service = _adapter(ctx)
    request = {key: value for key, value in args.items() if key != "idempotency_key"}
    values = dict(request)
    if "expected_pool_revision" in values:
        values["expected_revision"] = values.pop("expected_pool_revision")
    response = service.register(actor=ctx.actor, idempotency_key=_key(args), **values)
    return ToolResult(data={"pool": response})


@tool("gx_host_pool_reserve")
def host_pool_reserve(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    service = _adapter(ctx)
    values = {key: value for key, value in args.items() if key != "idempotency_key"}
    response = service.reserve(actor=ctx.actor, idempotency_key=_key(args), **values)
    return ToolResult(data=response, run_id=args["run_id"])


@tool("gx_host_pool_adopt_existing")
def host_pool_adopt_existing(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    service = _adapter(ctx)
    values = {key: value for key, value in args.items() if key != "idempotency_key"}
    response = service.adopt_existing(actor=ctx.actor, idempotency_key=_key(args), **values)
    return ToolResult(data=response, run_id=args["run_id"])


@tool("gx_host_pool_transition")
def host_pool_transition(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    service = _adapter(ctx)
    values = {key: value for key, value in args.items() if key != "idempotency_key"}
    slot = service.transition(actor=ctx.actor, idempotency_key=_key(args), **values)
    return ToolResult(data={"slot": slot}, run_id=_current_run(slot))


@tool("gx_host_pool_reparent")
def host_pool_reparent(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    service = _adapter(ctx)
    request = {key: value for key, value in args.items() if key != "idempotency_key"}
    values = {"logical_parent_slot_id": None, **request}
    response, _ = store.run_idempotent(ctx.conn, store.tool_key("gx_host_pool_reparent", _key(args)),
        "gx_host_pool_reparent", request, lambda conn: {"slot": service.reparent(actor=ctx.actor, **values)})
    return ToolResult(data=response, run_id=_current_run(response["slot"]))


@tool("gx_host_pool_snapshot")
def host_pool_snapshot(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    return ToolResult(data=HostPoolService(ctx.conn, ctx=ctx).snapshot(**args))
