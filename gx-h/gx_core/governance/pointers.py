"""The pointer a service actually reads.

A strategy — a retrieval configuration, for instance — is an asset with versions. Which
version a service uses is one row of ``strategy_pointers``, and the service reads it on
every call::

    current = pointers.active(conn, "memory_retrieval")     # None -> the service's built-in default
    current["version"], current["sha256"], current["config"]

Promotion of a STRATEGY asset to ACTIVE flips the pointer (effect ``flip_strategy_pointer``
of ``contracts/gates.json``); a rollback flips it back to a version that was active before.
Nothing else writes the table, so "promoted" always means "this is what the service now
does", and every flip is an event.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .. import contracts
from ..context import SCOPE_OPERATOR, Ctx
from ..domain import clock, machines
from ..errors import ArtifactNotFound, ContentHashMismatch, IllegalTransition, PermissionDenied, SchemaInvalid
from ..persistence import store
from . import assets, effects

STRATEGY = contracts.require_label("asset_kind", "STRATEGY")

_CONFIG_CACHE: dict[tuple[str, str], Any] = {}


def _config(conn: sqlite3.Connection, asset: Mapping[str, Any]) -> Any:
    """The ``config`` object of a strategy version. Content is immutable, so it is read once per sha256."""
    key = (asset["content_artifact_ref"], asset["sha256"])
    if key not in _CONFIG_CACHE:
        _CONFIG_CACHE[key] = assets.content(conn, asset).get("config")
    return _CONFIG_CACHE[key]


def _view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    asset = assets.get(conn, row["active_asset_id"], row["active_version"])
    if asset["sha256"] != row["active_sha256"]:
        raise ContentHashMismatch(
            f"the pointer for {row['strategy_kind']} names sha256 {str(row['active_sha256'])[:16]}… and the "
            f"registered version is {asset['sha256'][:16]}…",
            details={"strategy_kind": row["strategy_kind"], "pointer_sha256": row["active_sha256"],
                     "registered_sha256": asset["sha256"]},
        )
    return {
        "strategy_kind": row["strategy_kind"], "asset_id": row["active_asset_id"],
        "version": row["active_version"], "sha256": row["active_sha256"],
        "previous_version": row["previous_version"], "previous_sha256": row["previous_sha256"],
        "set_by": row["set_by"], "set_at": row["set_at"], "cycle_id": row["cycle_id"],
        "config": _config(conn, asset),
    }


def active(conn: sqlite3.Connection, strategy_kind: str) -> dict[str, Any] | None:
    """The strategy version a service of ``strategy_kind`` must use now, or None for its default."""
    row = conn.execute("SELECT * FROM strategy_pointers WHERE strategy_kind=?", (strategy_kind,)).fetchone()
    return _view(conn, row) if row is not None else None


def history(conn: sqlite3.Connection, strategy_kind: str) -> list[dict[str, Any]]:
    """Every flip of this pointer, oldest first, from the event log."""
    rows = conn.execute(
        "SELECT ts, kind, actor, reason, payload_json FROM events WHERE subject_kind='strategy_pointer'"
        " AND subject_id=? ORDER BY event_id",
        (strategy_kind,),
    )
    return [{"at": r["ts"], "event": r["kind"], "actor": r["actor"], "reason": r["reason"],
             **(store.loads(r, "payload_json", {}) or {})} for r in rows]


def _write(ctx: Ctx, *, strategy_kind: str, asset: Mapping[str, Any], cycle_id: str | None, event: str, reason: str) -> dict[str, Any]:
    conn = ctx.conn
    current = conn.execute("SELECT * FROM strategy_pointers WHERE strategy_kind=?", (strategy_kind,)).fetchone()
    previous_version = current["active_version"] if current is not None else None
    previous_sha256 = current["active_sha256"] if current is not None else None
    now = clock.now_iso()
    with store.transaction(conn) as tx:
        tx.execute(
            "INSERT INTO strategy_pointers(strategy_kind, active_asset_id, active_version, active_sha256,"
            " previous_version, previous_sha256, set_by, set_at, cycle_id) VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(strategy_kind) DO UPDATE SET active_asset_id=excluded.active_asset_id,"
            " active_version=excluded.active_version, active_sha256=excluded.active_sha256,"
            " previous_version=excluded.previous_version, previous_sha256=excluded.previous_sha256,"
            " set_by=excluded.set_by, set_at=excluded.set_at, cycle_id=excluded.cycle_id",
            (strategy_kind, asset["asset_id"], asset["version"], asset["sha256"], previous_version,
             previous_sha256, ctx.actor, now, cycle_id),
        )
        store.append_event(
            tx, kind=event, actor=ctx.actor, subject_kind="strategy_pointer", subject_id=strategy_kind,
            prev_state=previous_version, new_state=asset["version"], reason=reason,
            evidence_level="EXECUTION_OBSERVED",
            payload={"asset_id": asset["asset_id"], "version": asset["version"], "sha256": asset["sha256"],
                     "previous_version": previous_version, "previous_sha256": previous_sha256,
                     "cycle_id": cycle_id},
        )
    return active(conn, strategy_kind)


def _strategy(conn: sqlite3.Connection, strategy_kind: str, version: str) -> dict[str, Any]:
    asset = assets.get(conn, strategy_kind, version, kind=STRATEGY)
    if asset["manifest"].get("strategy_kind") != strategy_kind:
        raise SchemaInvalid(
            f"{asset['asset_id']}@{version} is a strategy of kind "
            f"{asset['manifest'].get('strategy_kind')!r}, not {strategy_kind!r}",
            details={"asset_id": asset["asset_id"], "version": version},
        )
    return asset


@effects.effect("flip_strategy_pointer", kinds=(STRATEGY,))
def _flip_on_active(ctx: Ctx, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Entering ACTIVE makes this version the one the service reads."""
    kind = asset["manifest"]["strategy_kind"]
    cycle = ctx.conn.execute(
        "SELECT cycle_id FROM rsi_cycles WHERE candidate_asset_id=? AND candidate_version=?"
        " AND candidate_sha256=? ORDER BY created_at DESC LIMIT 1",
        (asset["asset_id"], asset["version"], asset["sha256"]),
    ).fetchone()
    pointer = _write(
        ctx, strategy_kind=kind, asset=asset, cycle_id=cycle["cycle_id"] if cycle else None,
        event="strategy.pointer_flipped", reason=f"{asset['asset_id']}@{asset['version']} became ACTIVE",
    )
    return {"strategy_kind": kind, "active_version": pointer["version"],
            "previous_version": pointer["previous_version"], "cycle_id": pointer["cycle_id"]}


def install_baseline(ctx: Ctx, *, strategy_kind: str, version: str, sha256: str, reason: str) -> dict[str, Any]:
    """Name the status-quo strategy as the pointer's first value. Operator only, once per kind.

    A first strategy was not produced by an improvement cycle, so it cannot meet the
    ``rsi_cycle_verdict`` check that STRATEGY candidates need for VALIDATED. It is named
    here instead: a registered strategy version that has passed the REVIEWED gate (its bytes
    bound, its scripts scanned, an independent review on record), installed by the operator
    while the pointer is empty. Every later change goes through a cycle and the gate.
    """
    if ctx.principal.scope != SCOPE_OPERATOR:
        raise PermissionDenied("the baseline strategy is installed through the operator surface",
                               details={"scope": ctx.principal.scope})
    if active(ctx.conn, strategy_kind) is not None:
        raise IllegalTransition(
            f"the pointer for {strategy_kind} is already set; a strategy changes through an improvement "
            "cycle or a rollback, not by installing another baseline",
            details={"strategy_kind": strategy_kind},
        )
    asset = _strategy(ctx.conn, strategy_kind, version)
    if asset["sha256"] != sha256:
        raise ContentHashMismatch(
            f"{strategy_kind}@{version} is registered at {asset['sha256'][:16]}…, not {sha256[:16]}…",
            details={"registered_sha256": asset["sha256"], "expected_sha256": sha256},
        )
    reached = [assets.REVIEWED, assets.VALIDATED, assets.APPROVED, assets.ACTIVE]
    if asset["status"] not in reached:
        raise IllegalTransition(
            f"{strategy_kind}@{version} is {asset['status']}; a baseline has at least passed the "
            f"{assets.REVIEWED} gate",
            details={"status": asset["status"], "needs_one_of": reached},
        )
    return _write(ctx, strategy_kind=strategy_kind, asset=asset, cycle_id=None,
                  event="strategy.baseline_installed", reason=reason)


def clear(ctx: Ctx, *, strategy_kind: str, reason: str) -> dict[str, Any]:
    """Return the pointer to empty — the service's built-in default — when that is where it started. Operator only.

    An empty pointer means the service's default. A first promotion from that state has no
    earlier version to roll back to, only that default; this is how it is rolled back. It
    is refused when the pointer did not start empty (a baseline was installed): the default
    was then never what this pointer meant, and going "back" to it would be a change nobody
    reviewed. The row is removed; every flip, this one included, stays in the event log.
    """
    if ctx.principal.scope != SCOPE_OPERATOR:
        raise PermissionDenied("a strategy is rolled back through the operator surface",
                               details={"scope": ctx.principal.scope})
    if not (isinstance(reason, str) and reason.strip()):
        raise SchemaInvalid("a rollback states its reason")
    conn = ctx.conn
    current = active(conn, strategy_kind)
    if current is None:
        raise ArtifactNotFound(f"no pointer is set for {strategy_kind!r}", details={"strategy_kind": strategy_kind})
    flips = history(conn, strategy_kind)
    first = flips[0] if flips else {}
    if first.get("event") != "strategy.pointer_flipped" or first.get("previous_version") is not None:
        raise IllegalTransition(
            f"the pointer for {strategy_kind} did not start from the service's default; it is rolled back to a "
            "version that was active before, not cleared",
            details={"strategy_kind": strategy_kind, "first_event": first.get("event")},
        )
    with store.transaction(conn) as tx:
        tx.execute("DELETE FROM strategy_pointers WHERE strategy_kind=?", (strategy_kind,))
        store.append_event(
            tx, kind="strategy.pointer_rolled_back", actor=ctx.actor, subject_kind="strategy_pointer",
            subject_id=strategy_kind, prev_state=current["version"], new_state=None, reason=reason,
            evidence_level="EXECUTION_OBSERVED",
            payload={"asset_id": None, "version": None, "sha256": None, "previous_version": current["version"],
                     "previous_sha256": current["sha256"], "cycle_id": current["cycle_id"],
                     "to": "the service's built-in default"},
        )
    return {"strategy_kind": strategy_kind, "version": None, "previous_version": current["version"],
            "previous_sha256": current["sha256"], "cycle_id": current["cycle_id"]}


def rollback(ctx: Ctx, *, strategy_kind: str, to_version: str, reason: str) -> dict[str, Any]:
    """Flip the pointer back to a version that was active before. Operator only."""
    if ctx.principal.scope != SCOPE_OPERATOR:
        raise PermissionDenied("a strategy is rolled back through the operator surface",
                               details={"scope": ctx.principal.scope})
    if not (isinstance(reason, str) and reason.strip()):
        raise SchemaInvalid("a rollback states its reason")
    current = active(ctx.conn, strategy_kind)
    if current is None:
        raise ArtifactNotFound(f"no pointer is set for {strategy_kind!r}", details={"strategy_kind": strategy_kind})
    if to_version == current["version"]:
        raise IllegalTransition(f"{strategy_kind} already points at {to_version}",
                                details={"strategy_kind": strategy_kind, "active_version": to_version})
    was_active = {entry["version"] for entry in history(ctx.conn, strategy_kind)}
    if to_version not in was_active:
        raise IllegalTransition(
            f"{strategy_kind}@{to_version} was never the active version; a rollback returns to one that was",
            details={"strategy_kind": strategy_kind, "to_version": to_version,
                     "was_active": sorted(v for v in was_active if v)},
        )
    asset = _strategy(ctx.conn, strategy_kind, to_version)
    if machines.is_terminal(assets.MACHINE, asset["status"]):
        raise IllegalTransition(
            f"{strategy_kind}@{to_version} is {asset['status']} and can no longer be served",
            details={"status": asset["status"]},
        )
    return _write(ctx, strategy_kind=strategy_kind, asset=asset, cycle_id=current["cycle_id"],
                  event="strategy.pointer_rolled_back", reason=reason)
