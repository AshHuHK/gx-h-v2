"""Read-only runtime board, derived from one SQLite snapshot.

No board database or second task state machine exists. The legacy ``teams`` field is
a task display grouping. ``organization`` projects explicit teams separately; native
Host identities and scientific control records retain their own authorities.
The transport authorizes the run before calling this internal service. Authorization
and this query may share an outer read transaction; this service never closes it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict
from typing import Any, Callable, Mapping, Sequence

from .. import config
from ..budgets.lines import line as budget_line
from ..context import Ctx, Principal, SCOPE_READONLY
from ..domain import clock, ids
from ..errors import ArtifactNotFound, SchemaInvalid, ScopeDenied
from ..jobs import leases as job_leases
from ..persistence import store
from ..scheduling import dispatch, needs as vocabulary, snapshot

MAX_EVENT_ROWS = 1000
MAX_BOARD_ROWS = 100
MAX_BOARD_BYTES = 18000
_PAGE_BYTES = 1400
_LEGACY_ARRAYS = ("teams", "tasks", "dependencies", "workers", "jobs", "approvals", "budget", "budgets", "needs", "paths")
_BOARD_CURSORS = frozenset(("teams", "memberships", "assignments", "messages", "pools", "slots",
                            *("legacy_" + name for name in _LEGACY_ARRAYS)))
FEEDBACK_EVENTS = ("schedule.loop_decision", "schedule.round_closed")


def _object(raw: str | None) -> tuple[dict[str, Any], bool]:
    """Decode a stored metadata object without echoing malformed or private bytes."""
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError, RecursionError):
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def _text(value: Any, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _reaction_sections(conn: sqlite3.Connection, run_id: str, after: int,
                       limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read the outbox, using the reactor's failure rule and clearing predicate only.

    Error messages, raw payloads and idempotency bodies are deliberately absent. Legacy
    run ids are read only from valid JSON; a stored run_id column always wins.
    """
    from ..orchestration import reactor

    rows = conn.execute(
        "SELECT * FROM outbox WHERE COALESCE(run_id,CASE WHEN json_valid(payload_json)"
        " THEN json_extract(payload_json,'$.run_id') END)=? ORDER BY outbox_id", (run_id,),
    ).fetchall()
    known = set(store.trigger_topics())
    items, pending, parked, needs = [], [], [], []
    for row in rows:
        payload, readable = _object(row["payload_json"])
        failure = None
        error = None
        if row["last_error"]:
            # _failure_of is the canonical reactor's pure reader. For invalid JSON it
            # returns UNREADABLE+parked; no raw error message is copied into this view.
            try:
                failure = reactor._failure_of(row)
            except (TypeError, RecursionError):
                failure = {"code": "UNREADABLE", "count": reactor.FAILURES_BEFORE_PARKED, "parked": True}
            error = {"code": _text((failure or {}).get("code"), 100),
                     "count": _number((failure or {}).get("count")),
                     "parked": bool((failure or {}).get("parked")),
                     "retryable": (failure or {}).get("retryable") if isinstance((failure or {}).get("retryable"), bool) else None,
                     "failed_event_id": _number((failure or {}).get("event_id")),
                     "after_event_id": _number((failure or {}).get("after_event_id")),
                     "decodable": _object(row["last_error"])[1]}
        is_pending = row["consumed_at"] is None
        is_parked = is_pending and row["topic"] in known and bool(failure and failure.get("parked"))
        item = {"outbox_id": int(row["outbox_id"]), "topic": _text(row["topic"]),
                "subject": _text(payload.get("subject")), "event_id": _number(payload.get("event_id")),
                "created_at": row["created_at"], "attempts": int(row["attempts"] or 0),
                "consumed_at": row["consumed_at"], "consumer": _text(row["consumer"]),
                "status": "consumed" if not is_pending else "parked" if is_parked else "failed" if error else "pending",
                "error": error, "payload_readable": readable}
        items.append(item)
        if is_pending:
            pending.append(item)
        if is_parked:
            try:
                cleared = reactor.changed_since(conn, run_id, failure)
            except (ValueError, TypeError, OverflowError):
                cleared = None  # malformed stored metadata is visible, never a claimed clearance
            parked.append({**item, "cleared": cleared})
            needs.append(vocabulary.need(
                "reaction", f"the reaction to outbox {row['outbox_id']} succeeding when tried again",
                clear_by=vocabulary.MACHINE, subject={"kind": "outbox", "id": int(row["outbox_id"])},
                why=f"{item['topic']} failed {error['count']} time(s), code {error['code']}",
            ))
    page = [item for item in items if item["outbox_id"] > after]
    has_more, page = len(page) > limit, page[:limit]
    return {"items": page, "recent": items[-limit:], "recent_truncated": len(items) > limit,
            "pending": pending[:limit], "parked": parked[:limit],
            "pending_truncated": len(pending) > limit, "parked_truncated": len(parked) > limit,
            "counts": {"total": len(items), "pending": len(pending), "parked": len(parked),
                       "consumed": len(items) - len(pending)},
            "cursor": {"after_outbox_id": after, "next_outbox_id": page[-1]["outbox_id"] if page else after,
                       "as_of_outbox_id": items[-1]["outbox_id"] if items else 0, "has_more": has_more}}, needs[:limit]


def _feedback_sections(conn: sqlite3.Connection, run_id: str, after: int,
                       limit: int, as_of: int) -> dict[str, Any]:
    """Project only decision metadata and progress counters, never observer products."""
    rows = conn.execute(
        "SELECT event_id,ts,kind,subject_kind,subject_id,evidence_level,payload_json FROM events"
        " WHERE run_id=? AND kind IN (?,?) AND event_id<=? ORDER BY event_id",
        (run_id, *FEEDBACK_EVENTS, as_of),
    ).fetchall()
    items = []
    for row in rows:
        payload, readable = _object(row["payload_json"])
        item = _selected(row, ("event_id", "ts", "kind", "subject_kind", "subject_id", "evidence_level"))
        item.update({"payload_readable": readable, "loop_key": _text(payload.get("loop_key"), 300),
                     "decision": _text(payload.get("decision"), 40), "reason": _text(payload.get("reason"), 600),
                     "rule": _text(payload.get("rule"), 100), "stage": _text(payload.get("stage") or payload.get("node")),
                     "iteration": _number(payload.get("iteration")), "cap": _number(payload.get("cap")),
                     "attempt": _number(payload.get("attempt")), "task_id": _text(payload.get("task_id")),
                     "submission_id": _text(payload.get("submission_id")),
                     "bound_spent": payload.get("bound_spent") if isinstance(payload.get("bound_spent"), bool) else None,
                     "quiescent": payload.get("quiescent") if isinstance(payload.get("quiescent"), bool) else None,
                     "needs_count": len(payload["needs"]) if isinstance(payload.get("needs"), list) else 0,
                     "decided_by": _text(payload.get("decided_by"), 100)})
        closed = payload.get("round")
        if isinstance(closed, dict):
            item["round"] = {name: _number(closed.get(name)) for name in
                             ("index", "reviewed_added", "gaps_covered", "paths_evaluated")}
            item["round"].update({name: closed.get(name) if isinstance(closed.get(name), bool) else None for name in
                                  ("searches_all_failed", "machinery_failed", "incomplete")})
        else:
            item["round"] = _number(closed)
        items.append(item)
    page = [item for item in items if item["event_id"] > after]
    has_more, page = len(page) > limit, page[:limit]
    return {"items": page, "recent": items[-limit:], "recent_truncated": len(items) > limit,
            "counts": {"total": len(items), "loop_decisions": sum(item["kind"] == FEEDBACK_EVENTS[0] for item in items),
                       "round_closed": sum(item["kind"] == FEEDBACK_EVENTS[1] for item in items)},
            "cursor": {"after_event_id": after, "next_event_id": page[-1]["event_id"] if page else after,
                       "as_of_event_id": as_of, "has_more": has_more}}


def _selected(row: Mapping[str, Any] | sqlite3.Row, columns: Sequence[str]) -> dict[str, Any]:
    return {name: row[name] for name in columns}


def _groups(tasks: Sequence[sqlite3.Row], edges: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Explicit groups retain cross-team dependencies instead of silently merging."""
    parents = {row["task_id"]: row["task_id"] for row in tasks if not row["parallel_group"]}

    def root(task_id: str) -> str:
        while parents[task_id] != task_id:
            parents[task_id] = parents[parents[task_id]]
            task_id = parents[task_id]
        return task_id

    for edge in edges:
        left, right = edge["task_id"], edge["depends_on"]
        if left in parents and right in parents:
            first, second = sorted((root(left), root(right)))
            parents[second] = first
    found: dict[str, dict[str, Any]] = {}
    for row in tasks:
        group = row["parallel_group"]
        team_id = f"parallel_group:{group}" if group else f"dependency_component:{root(row['task_id'])}"
        team = found.setdefault(team_id, {
            "team_id": team_id, "basis": "parallel_group" if group else "dependency_component",
            "parallel_group": group, "task_ids": [], "counts": {}, "worker_subjects": [],
        })
        team["task_ids"].append(row["task_id"])
    return found


def _board_cursors(value: Mapping[str, Any] | None) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _BOARD_CURSORS:
        raise SchemaInvalid("after_board contains only declared board section cursors")
    for name, cursor in value.items():
        if type(cursor) is not int or not 0 <= cursor <= (1 << 63) - 1:
            raise SchemaInvalid(f"after_board.{name} is a non-negative SQLite integer")
    return dict(value)


def _preview(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Metadata only. Large bodies are reached through their canonical references."""
    return {field: row[field] for field in fields}


def _bounded_page(conn: sqlite3.Connection, sql: str, parameters: Sequence[Any], *,
                  after: int, limit: int, identity: str,
                  project: Callable[[sqlite3.Row], dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int, dict[str, Any]]:
    """Stable row cursors advance only over returned identities, including thin rows.

    SQL and identifiers here are code constants. Counting never materializes bodies;
    at most ``limit`` records are decoded, and each family has its own byte allowance.
    A large first record still returns its reference so pagination always progresses.
    """
    count, as_of = conn.execute("SELECT COUNT(*),COALESCE(MAX(board_rowid),0) FROM (" + sql + ")",
                               parameters).fetchone()
    remaining = int(conn.execute("SELECT COUNT(*) FROM (" + sql + ") WHERE board_rowid>?",
                                 [*parameters, after]).fetchone()[0])
    values, used = [], 2
    for row in conn.execute("SELECT * FROM (" + sql + ") WHERE board_rowid>? ORDER BY board_rowid LIMIT ?",
                            [*parameters, after, limit]):
        value = {**project(row), "board_rowid": int(row["board_rowid"])}
        size = len(ids.canonical_json(value).encode("utf-8")) + 1
        if size > _PAGE_BYTES:
            reference = {name: value[name] for name in ("ref", "team_id") if name in value and name != identity}
            value = {identity: row[identity], **reference, "board_rowid": int(row["board_rowid"]),
                     "details_truncated": True}
            size = len(ids.canonical_json(value).encode("utf-8")) + 1
        if used + size > _PAGE_BYTES:
            break
        values.append(value)
        used += size
    truncated = remaining - len(values)
    return values, int(count), truncated, {
        "after": after, "next": values[-1]["board_rowid"] if values else after,
        "as_of": int(as_of), "has_more": truncated > 0,
    }


def _family(out: dict[str, Any], name: str, page: tuple[Any, ...]) -> None:
    out[name], out["counts"][name], out["truncated"][name], out["cursors"][name] = page


def _organization(ctx: Ctx, run_id: str, cursors: Mapping[str, int], limit: int) -> dict[str, Any]:
    from .manager import TeamManager

    # Keep the canonical scope/revision/snapshot reader in the chain. Its single
    # message cursor cannot page teams/members/assignments independently; the board
    # adds those bounded projections without duplicating mutation authorization.
    canonical = TeamManager(ctx).snapshot(run_id=run_id, after_event_id=cursors.get("messages", 0), limit=1)
    out: dict[str, Any] = {"run_id": run_id, "revision": canonical["revision"],
                           "as_of_event_id": canonical["as_of_event_id"],
                           "counts": {}, "truncated": {}, "cursors": {}}
    conn = ctx.conn

    def team(row: sqlite3.Row) -> dict[str, Any]:
        return _preview(row, ("team_id", "parent_team_id", "status", "intent_version", "intent_sha256",
                              "leader_subject", "leader_generation", "leader_expires_at", "context_ref",
                              "budget_ref", "revision", "created_at", "updated_at"))

    def message(row: sqlite3.Row) -> dict[str, Any]:
        value = _preview(row, ("message_id", "sender_team_id", "recipient_team_id", "sender_subject", "kind",
                               "task_id", "hypothesis_ref", "context_ref", "causal_event_id", "correlation_key",
                               "payload_sha256", "created_at"))
        # A message is a persistent causal reference, never an inline instruction.
        value["acknowledged_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM team_message_acks WHERE message_id=?", (row["message_id"],)).fetchone()[0])
        return value

    queries = {
        "teams": ("SELECT t.rowid AS board_rowid,t.* FROM teams t WHERE t.run_id=?", "team_id", team),
        "memberships": ("SELECT m.rowid AS board_rowid,m.* FROM team_members m JOIN teams t ON t.team_id=m.team_id"
                        " WHERE t.run_id=?", "subject", lambda r: _preview(r, (
                            "team_id", "subject", "observed_worker_ref", "status", "joined_at", "updated_at"))),
        "assignments": ("SELECT a.rowid AS board_rowid,a.* FROM task_assignments a JOIN teams t ON t.team_id=a.team_id"
                        " WHERE t.run_id=?", "task_id", lambda r: {
                            **_preview(r, ("task_id", "team_id", "subject", "observed_worker_ref", "context_ref",
                                           "assignment_generation", "status", "assigned_by", "created_at", "updated_at")),
                            "handoff_pending": r["pending_json"] not in (None, "null")}),
        "messages": ("SELECT (SELECT MIN(e.event_id) FROM events e WHERE e.run_id=m.run_id"
                     " AND e.kind='team.message_sent' AND e.subject_kind='team_message'"
                     " AND e.subject_id=m.message_id) AS board_rowid,m.* FROM team_messages m WHERE m.run_id=?",
                     "message_id", message),
    }
    for name, (sql, identity, project) in queries.items():
        _family(out, name, _bounded_page(conn, sql, [run_id], after=cursors.get(name, 0), limit=limit,
                                        identity=identity, project=project))
    return out


def _host_pool(conn: sqlite3.Connection, run_id: str, ctx: Ctx | None,
               cursors: Mapping[str, int], limit: int, limits: Mapping[str, Any]) -> dict[str, Any]:
    from ..hostobs.pool import TERMINATED, capacity

    # No unscoped SELECT of native identities. A legacy internal caller has only
    # this run's view; an actual backend principal can see its allowed projects.
    if ctx is None:
        scope, parameters = "(s.run_id=? OR COALESCE(s.current_run_id,s.run_id)=?)", [run_id, run_id]
    elif ctx.principal.allows_all_projects:
        scope, parameters = "1=1", []
    else:
        projects = tuple(ctx.principal.project_refs)
        scope = "r.project_ref IN (" + ",".join("?" for _ in projects) + ")" if projects else "0=1"
        parameters = list(projects)
    scoped = (" FROM host_pool_slots s JOIN runs r ON r.run_id=COALESCE(s.current_run_id,s.run_id)"
              " WHERE " + scope)
    out: dict[str, Any] = {"counts": {}, "truncated": {}, "cursors": {},
                           "occupancy_basis": "all non-TERMINATED slots plus external_occupied; no lease expiry release"}

    def pool(row: sqlite3.Row) -> dict[str, Any]:
        value = _preview(row, ("pool_id", "host", "session_generation", "configured_capacity", "effective_capacity",
                               "external_occupied", "max_depth", "revision", "observed_source", "observed_at"))
        counts = {r["state"]: int(r["n"]) for r in conn.execute(
            "SELECT state,COUNT(*) n FROM host_pool_slots WHERE pool_id=? GROUP BY state", (row["pool_id"],))}
        occupied = sum(n for state, n in counts.items() if state != TERMINATED) + int(row["external_occupied"])
        ceiling = limits.get("host_pool", {}).get("max_slots", 0)
        total = capacity(dict(row), ceiling=ceiling)
        value.update(counts=counts, occupied=occupied, admission_capacity=total, available=max(0, total - occupied))
        return value

    pool_sql = ("SELECT p.rowid AS board_rowid,p.* FROM host_pools p WHERE p.pool_id IN (SELECT s.pool_id" + scoped + ")")
    parent_scope = scope.replace("s.", "a.").replace("r.", "ar.")
    slot_sql = ("SELECT s.rowid AS board_rowid,s.*,CASE WHEN s.native_parent_ref IS NULL OR EXISTS"
                " (SELECT 1 FROM host_pool_slots a JOIN runs ar ON ar.run_id=COALESCE(a.current_run_id,a.run_id)"
                " WHERE a.pool_id=s.pool_id AND a.native_agent_ref=s.native_parent_ref AND " + parent_scope + ")"
                " THEN s.native_parent_ref END AS visible_native_parent_ref,CASE WHEN s.logical_parent_slot_id IS NULL OR EXISTS"
                " (SELECT 1 FROM host_pool_slots a JOIN runs ar ON ar.run_id=COALESCE(a.current_run_id,a.run_id)"
                " WHERE a.slot_id=s.logical_parent_slot_id AND " + parent_scope + ")"
                " THEN s.logical_parent_slot_id END AS visible_logical_parent_slot_id" + scoped)

    def slot(row: sqlite3.Row) -> dict[str, Any]:
        value = _preview(row, ("slot_id", "pool_id", "run_id", "pool_generation", "generation", "state",
                               "native_agent_ref", "native_depth", "logical_depth", "task_id", "lease_id",
                               "observed_source", "observed_at"))
        value.update(native_parent_ref=row["visible_native_parent_ref"],
                     origin_run_id=row["run_id"], current_run_id=row["current_run_id"] or row["run_id"],
                     origin_member=row["run_id"] == run_id,
                     current_member=(row["current_run_id"] or row["run_id"]) == run_id,
                     logical_parent_slot_id=row["visible_logical_parent_slot_id"],
                     parent_identity_redacted=(row["native_parent_ref"] != row["visible_native_parent_ref"]
                                               or row["logical_parent_slot_id"] != row["visible_logical_parent_slot_id"]))
        return value
    _family(out, "pools", _bounded_page(conn, pool_sql, parameters, after=cursors.get("pools", 0), limit=limit,
                                       identity="pool_id", project=pool))
    _family(out, "slots", _bounded_page(conn, slot_sql, [*parameters, *parameters, *parameters],
                                       after=cursors.get("slots", 0), limit=limit, identity="slot_id", project=slot))
    # Aggregate occupancy is shared-pool truth. Only scalar counts cross project
    # scope; neither hidden run IDs nor native identities are materialized here.
    out["occupied"] = int(conn.execute(
        "SELECT COUNT(*) FROM host_pool_slots WHERE state<>? AND pool_id IN (SELECT s.pool_id" + scoped + ")",
        [TERMINATED, *parameters]).fetchone()[0]) + int(conn.execute(
            "SELECT COALESCE(SUM(external_occupied),0) FROM host_pools WHERE pool_id IN (SELECT s.pool_id" + scoped + ")",
            parameters).fetchone()[0])
    return out


def _transport_record(value: Any) -> Any:
    """Thin an oversized record explicitly, retaining references and omission counts."""
    if len(ids.canonical_json(value).encode("utf-8")) <= _PAGE_BYTES:
        return value

    task_control = ("task_id", "run_id", "status", "attempt", "brief_ref", "brief_sha256", "lease")
    if isinstance(value, dict) and "task_id" in value:
        # A task's live lease is late in the complete snapshot's field order.
        # Keep its existing control metadata ahead of the generic field cutoff.
        value = {**{key: value[key] for key in task_control if key in value}, **value}

    def thin(item: Any, depth: int = 0) -> Any:
        if isinstance(item, str):
            return item[:160]
        if isinstance(item, list):
            return {"omitted_rows": len(item)}
        if isinstance(item, dict):
            if depth >= 3:
                return {"omitted_fields": len(item)}
            result = {key: val if key.endswith(("_id", "_ref")) and isinstance(val, str) else thin(val, depth + 1)
                      for key, val in list(item.items())[:30]}
            result["details_truncated"] = True
            if len(item) > 30:
                result["omitted_fields"] = len(item) - 30
            return result
        return item

    result = thin(value)
    if len(ids.canonical_json(result).encode("utf-8")) > _PAGE_BYTES:
        # Even a single record with many large fields cannot block its cursor.
        if isinstance(value, dict):
            controls = task_control if "task_id" in value else ("status", "revision")
            fields = [name for name in (*controls, *value)
                      if name in value and (name.endswith(("_id", "_ref")) or name in controls)]
            result = {"details_truncated": True, "omitted_fields": len(value)}
            for name in dict.fromkeys(fields):
                item = value[name]
                if name == "lease" and isinstance(item, dict):
                    item = {key: thin(item[key]) for key in
                        ("lease_id", "principal", "owner", "attempt", "expires_at", "effective_expires_at", "released_at", "live")
                        if key in item}
                candidate = {**result, name: item}
                if len(ids.canonical_json(candidate).encode("utf-8")) <= _PAGE_BYTES - 32:
                    result = candidate
                    result["omitted_fields"] -= 1
        else:
            result = {"details_truncated": True}
    return result


def bounded_transport(board: dict[str, Any], *, after_board: Mapping[str, Any] | None = None,
                      board_limit: int = 50) -> dict[str, Any]:
    """Bound the *whole* public tool data before the envelope's artifact spill path.

    CLI retains its complete legacy snapshot. The public tool pages legacy arrays
    by explicit offsets, and names every omitted record/count. Its nested metadata
    may be a thin view; canonical task/brief/status tools retain full detail.
    """
    cursors = _board_cursors(after_board)
    out = dict(board)
    bounds: dict[str, Any] = {"byte_limit": MAX_BOARD_BYTES, "row_limit": board_limit,
                              "counts": {}, "truncated": {}, "cursors": {}, "metadata_truncated": []}
    out["read_bounds"] = bounds
    for name in _LEGACY_ARRAYS:
        values = board[name]
        after = cursors.get("legacy_" + name, 0)
        out[name] = [_transport_record(value) for value in values[after:after + board_limit]]
        bounds["counts"][name] = len(values)
        bounds["truncated"][name] = max(0, len(values) - after - len(out[name]))
        bounds["cursors"][name] = {"after": after, "next": after + len(out[name]), "as_of": len(values),
                                    "has_more": bounds["truncated"][name] > 0}
    for name in ("run", "capacity", "dispatch", "progress", "coordinator"):
        bounded = _transport_record(board[name])
        if bounded is not board[name]:
            bounds["metadata_truncated"].append(name)
        out[name] = bounded
    ledger_ids = {"reactions": ("outbox_id", "after_outbox_id", "next_outbox_id"),
                  "feedback": ("event_id", "after_event_id", "next_event_id")}
    for name, (identity, after_key, next_key) in ledger_ids.items():
        original = board[name]
        section = {**original, "cursor": dict(original["cursor"]), "omitted_rows": {}}
        for key, rows in original.items():
            if not isinstance(rows, list):
                continue
            values, used = [], 2
            for row in rows[:board_limit]:
                value = _transport_record(row)
                size = len(ids.canonical_json(value).encode("utf-8")) + 1
                if used + size > _PAGE_BYTES:
                    break
                values.append(value)
                used += size
            section[key] = values
            section["omitted_rows"][key] = len(rows) - len(values)
            if key != "items":
                section[key + "_truncated"] = bool(original.get(key + "_truncated")) or len(values) < len(rows)
        section["cursor"].update({next_key: section["items"][-1][identity] if section["items"]
                                  else section["cursor"][after_key],
                                  "has_more": original["cursor"]["has_more"] or section["omitted_rows"]["items"] > 0})
        out[name] = section
    # Events have their existing event-ID cursor, including after byte trimming.
    out["events"] = [_transport_record(value) for value in board["events"][:board_limit]]
    out["event_cursor"] = dict(board["event_cursor"])
    out["event_cursor"]["has_more"] |= len(out["events"]) < len(board["events"])
    out["event_cursor"]["next_event_id"] = (out["events"][-1]["event_id"] if out["events"]
                                               else out["event_cursor"]["after_event_id"])
    while len(ids.canonical_json(out).encode("utf-8")) > MAX_BOARD_BYTES:
        choices = [(len(ids.canonical_json(out[name]).encode("utf-8")), "legacy", name)
                   for name in _LEGACY_ARRAYS if out[name]]
        choices.extend((len(ids.canonical_json(out[section][name]).encode("utf-8")), section, name)
                       for section in ("organization", "host_pool")
                       for name in out[section]["cursors"] if out[section][name])
        choices.extend((len(ids.canonical_json(out[section][name]).encode("utf-8")), section, name)
                       for section in ledger_ids for name in out[section]["omitted_rows"] if out[section][name])
        if out["events"]:
            choices.append((len(ids.canonical_json(out["events"]).encode("utf-8")), "events", "events"))
        if not choices:
            raise SchemaInvalid("board reference metadata exceeds its public response allowance")
        _, section, name = max(choices, key=lambda item: item[0])
        if section == "legacy":
            out[name].pop()
            bounds["truncated"][name] += 1
            bounds["cursors"][name].update(next=bounds["cursors"][name]["after"] + len(out[name]), has_more=True)
        elif section == "events":
            out["events"].pop()
            out["event_cursor"].update(next_event_id=out["events"][-1]["event_id"] if out["events"]
                                        else out["event_cursor"]["after_event_id"], has_more=True)
        elif section in ledger_ids:
            value = out[section]
            value[name].pop()
            value["omitted_rows"][name] += 1
            if name == "items":
                identity, after_key, next_key = ledger_ids[section]
                value["cursor"].update({next_key: value[name][-1][identity] if value[name] else value["cursor"][after_key],
                                         "has_more": True})
            else:
                value[name + "_truncated"] = True
        else:
            out[section][name].pop()
            out[section]["truncated"][name] += 1
            out[section]["cursors"][name].update(
                next=out[section][name][-1]["board_rowid"] if out[section][name]
                else out[section]["cursors"][name]["after"], has_more=True)
    return out


class SharedBoardService:
    """Read an authorized run without changing task, lease or event ledgers."""

    def __init__(self, conn: sqlite3.Connection, *, ctx: Ctx | None = None) -> None:
        if ctx is not None and ctx.conn is not conn:
            raise SchemaInvalid("board context and snapshot use the same store connection")
        self.conn, self.ctx = conn, ctx

    def snapshot(self, *, run_id: str, after_event_id: int = 0, event_limit: int = 100,
                 after_reaction_id: int = 0, after_feedback_event_id: int = 0,
                 board_limit: int = 50, after_board: Mapping[str, Any] | None = None,
                 now: str | None = None) -> dict[str, Any]:
        """A complete board and a bounded page of this run's committed event views.

        as_of_event_id describes the complete board. next_event_id advances only to
        an event returned, so pagination cannot skip an unread event. All state is
        re-derived on each call, including after restart. No settlement occurs here.
        """
        for name, value in (("after_event_id", after_event_id), ("event_limit", event_limit),
                            ("after_reaction_id", after_reaction_id), ("after_feedback_event_id", after_feedback_event_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > (1 << 63) - 1:
                raise SchemaInvalid(f"{name} is a non-negative SQLite integer")
        if event_limit < 1 or event_limit > MAX_EVENT_ROWS:
            raise SchemaInvalid(f"event_limit is between 1 and {MAX_EVENT_ROWS}")
        if type(board_limit) is not int or not 1 <= board_limit <= MAX_BOARD_ROWS:
            raise SchemaInvalid(f"board_limit is between 1 and {MAX_BOARD_ROWS}")
        cursors = _board_cursors(after_board)
        at = clock.now_iso() if now is None else now
        snapshot.instant(at)
        owns = not self.conn.in_transaction
        if owns:
            self.conn.execute("BEGIN")
        try:
            return self._read(run_id, after_event_id, event_limit, at, after_reaction_id, after_feedback_event_id,
                              board_limit, cursors)
        finally:
            if owns:
                self.conn.execute("ROLLBACK")

    def _read(self, run_id: str, after: int, limit: int, at: str,
              after_reaction: int, after_feedback: int, board_limit: int, cursors: Mapping[str, int]) -> dict[str, Any]:
        conn = self.conn
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
        if self.ctx is not None and not self.ctx.principal.may_access_project(run["project_ref"]):
            raise ScopeDenied("the board run is outside this principal's project scope")
        limits = config.limits()
        snap = snapshot.load(conn, run_id, now=at, limits=limits)
        plan = dispatch.plan(snap)
        planned = plan.as_output(contract="r5")
        task_rows = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        dependencies = [dict(row) for row in conn.execute(
            "SELECT d.task_id, d.depends_on, p.status, p.status='ACCEPTED' AS satisfied FROM task_deps d"
            " JOIN tasks t ON t.task_id=d.task_id LEFT JOIN tasks p ON p.task_id=d.depends_on"
            " WHERE t.run_id=? ORDER BY d.task_id, d.depends_on", (run_id,),
        )]
        for edge in dependencies:
            edge["satisfied"] = bool(edge["satisfied"])
        teams = _groups(task_rows, dependencies)
        team_of = {task_id: team["team_id"] for team in teams.values() for task_id in team["task_ids"]}
        for edge in dependencies:
            edge["team_id"] = team_of[edge["task_id"]]
            edge["depends_on_team_id"] = team_of.get(edge["depends_on"])
        leases = {row["lease_id"]: row for row in conn.execute(
            "SELECT l.* FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE t.run_id=?"
            " ORDER BY l.issued_at, l.lease_id", (run_id,),
        )}
        held_open = job_leases.held_open(conn, run_id, limits=limits, at=at)
        workers, workers_of = [], defaultdict(list)
        for row in conn.execute(
            "SELECT o.* FROM worker_observations o JOIN tasks t ON t.task_id=o.task_id"
            " WHERE t.run_id=? ORDER BY o.observed_at, o.rowid", (run_id,),
        ):
            worker = _selected(row, (
                "worker_observation_id", "task_id", "lease_id", "host", "host_session_ref",
                "observed_worker_ref", "agent_ref", "state", "evidence_level", "observation_source",
                "delegation_mode", "model_evidence_level", "turns_observed", "turns_off_policy",
                "transcript_sha256", "host_version", "policy_verdict", "observed_at",
            ))
            lease = leases.get(row["lease_id"])
            worker.update(
                team_id=team_of[row["task_id"]], principal=lease["principal"] if lease else None,
                reported={"model": row["reported_model"], "effort": row["reported_effort"]},
                observed={"model": row["observed_model"], "effort": row["observed_effort"]},
            )
            workers.append(worker)
            workers_of[row["task_id"]].append(worker)
        jobs, jobs_of = [], defaultdict(list)
        for row in conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY created_at, job_id", (run_id,)):
            deadline = job_leases.job_deadline(row, limits=limits, at=at)
            terminal = row["status"] in snapshot.JOB_TERMINAL
            job = _selected(row, (
                "job_id", "task_id", "path_id", "lease_ref", "step_index", "job_attempt", "status",
                "capability_id", "capability_version", "created_at", "submitted_at", "finished_at",
                "side_effect_status", "exit_code",
            ))
            job.update(deadline_at=deadline["deadline_at"], deadline_basis=deadline["basis"],
                       overdue=not terminal and deadline["overdue"], terminal=terminal,
                       lease=job_leases.job_lease(conn, row), team_id=team_of.get(row["task_id"]))
            jobs.append(job)
            if row["task_id"]:
                jobs_of[row["task_id"]].append(job["job_id"])
            elif row["path_id"]:
                for task in task_rows:
                    if task["path_id"] == row["path_id"] and task["kind"] == snapshot.EXECUTE_KIND:
                        jobs_of[task["task_id"]].append(job["job_id"])
        approvals = [_selected(row, (
            "approval_id", "action_kind", "subject_kind", "subject_id", "subject_sha256", "status",
            "requested_at", "decided_at", "expires_at",
        )) for row in conn.execute(
            "SELECT * FROM approvals WHERE run_id=? ORDER BY requested_at, approval_id", (run_id,),
        )]
        blocking = {item["task_id"]: item for item in planned["blocked"]}
        dead_needs = {item.task_id: item.need for item in plan.progress.needs if item.need}
        paused = store.loads(run, "paused_json", {}) or {}
        all_needs = [dict(item) for item in paused.get("needs", []) if isinstance(item, Mapping)]
        tasks = []
        for row in task_rows:
            facts = snap.by_id[row["task_id"]]
            task_id = facts.task_id
            current = leases.get(row["lease_id"])
            hold = store.loads(row, "hold_json", {}) or {}
            task_needs = [dict(dead_needs[task_id])] if task_id in dead_needs else []
            for approval_id in dispatch.blocking_approvals(facts, snap):
                task_needs.append(vocabulary.need(
                    "approval", "the approval must be granted", clear_by=vocabulary.PERSON,
                    subject={"kind": "approval", "id": approval_id},
                    why=blocking.get(task_id, {}).get("reason", "a pending approval holds this task"),
                ))
            if isinstance(hold.get("need"), Mapping):
                task_needs.append(dict(hold["need"]))
            task = _selected(row, (
                "task_id", "run_id", "path_id", "kind", "role", "task_class", "status", "attempt",
                "attempts_consumed", "max_attempts", "parallel_group", "stage", "bundle_kind",
                "brief_ref", "brief_sha256", "budget_ref", "blocked_reason", "owner", "created_at", "updated_at",
            ))
            task.update(
                team_id=team_of[task_id], dependencies=list(facts.dependencies), independent_of=facts.independent_of,
                subject={"kind": facts.subject_kind, "id": facts.subject_id, "sha256": facts.subject_sha256}
                        if facts.subject_kind else None,
                hold={key: value for key, value in hold.items() if key != "refusals"} or None,
                required={"model": facts.model, "efforts": list(facts.efforts)},
                blocking=blocking.get(task_id), needs=task_needs, in_flight=facts.in_flight,
                offered=task_id in snap.offered,
                lease=None if current is None else {
                    **_selected(current, ("lease_id", "principal", "owner", "attempt", "issued_at", "expires_at", "released_at")),
                    "effective_expires_at": facts.lease_expires_at, "live": facts.lease_live,
                    "held_open_until": held_open.get(task_id, {}).get("deadline_at"),
                    "held_open_jobs": held_open.get(task_id, {}).get("jobs", []),
                },
                workers=[worker["worker_observation_id"] for worker in workers_of[task_id]],
                jobs=jobs_of[task_id], lease_seconds=facts.lease_seconds,
                ready_seq=facts.ready_seq, overtaken=facts.overtaken, dispatch_failures=facts.dispatch_failures,
            )
            tasks.append(task)
            all_needs.extend({**need, "task_id": task_id} for need in task_needs)
        for team in teams.values():
            members = [task for task in tasks if task["team_id"] == team["team_id"]]
            team["counts"] = dict(sorted(Counter(task["status"] for task in members).items()))
            team["worker_subjects"] = sorted({lease["principal"] for lease in leases.values()
                                               if lease["task_id"] in team["task_ids"] and lease["principal"]})
        budget = [asdict(line) | line.reading for line in snap.budget]
        budgets = []
        for row in conn.execute("SELECT * FROM budgets WHERE run_id=? ORDER BY rowid", (run_id,)):
            limits_of = store.loads(row, "limits_json", {}) or {}
            spent, reserved = store.loads(row, "spent_json", {}) or {}, store.loads(row, "reserved_json", {}) or {}
            lines = [{"name": name, "limit": ceiling, "spent": spent.get(name, 0), "reserved": reserved.get(name, 0),
                      **budget_line(ceiling, spent.get(name, 0), reserved.get(name, 0))}
                     for name, ceiling in sorted(limits_of.items())]
            budgets.append({"budget_ref": row["budget_ref"], "parent_ref": row["parent_ref"], "lines": lines})
        events = [dict(row) for row in conn.execute(
            "SELECT event_id, ts, kind, actor, subject_kind, subject_id, prev_state, new_state, reason, evidence_level"
            " FROM events WHERE run_id=? AND event_id>? AND event_id<=? ORDER BY event_id LIMIT ?",
            (run_id, after, snap.as_of_event_id, limit + 1),
        )]
        has_more, events = len(events) > limit, events[:limit]
        coordinator = conn.execute(
            "SELECT * FROM coordinator_leases WHERE run_id=? AND released_at IS NULL ORDER BY issued_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        run_view = _selected(run, (
            "run_id", "project_ref", "objective", "status", "phase", "revision", "workflow_id",
            "workflow_version", "budget_ref", "created_at", "updated_at",
        ))
        run_view.update(stage=snap.run.stage, round_index=snap.run.round_index, paused=paused or None)
        reactions, reaction_needs = _reaction_sections(conn, run_id, after_reaction, limit)
        feedback = _feedback_sections(conn, run_id, after_feedback, limit, snap.as_of_event_id)
        for need in reaction_needs:
            if not any(item.get("kind") == need["kind"] and item.get("subject") == need["subject"] for item in all_needs):
                all_needs.append(need)
        ctx = self.ctx or Ctx(conn, Principal(subject="board-internal", scope=SCOPE_READONLY,
                                              project_refs=(run["project_ref"],)))
        additions = {
            "organization": _organization(ctx, run_id, cursors, board_limit),
            "host_pool": _host_pool(conn, run_id, self.ctx, cursors, board_limit, limits),
        }
        # Family budgets include every returned row. This final ceiling includes
        # cursor/count/context metadata as well, and keeps pagination truthful if
        # an unusually long identifier or metadata value consumes the allowance.
        while len(ids.canonical_json(additions).encode("utf-8")) > MAX_BOARD_BYTES:
            choices = [(len(ids.canonical_json(section[name]).encode("utf-8")), section, name)
                       for section in additions.values() for name in section["cursors"] if section[name]]
            if not choices:
                raise SchemaInvalid("board reference metadata exceeds its response byte allowance")
            _, section, name = max(choices, key=lambda item: item[0])
            section[name].pop()
            section["truncated"][name] += 1
            section["cursors"][name].update(
                next=section[name][-1]["board_rowid"] if section[name] else section["cursors"][name]["after"],
                has_more=True)
        return {
            "schema_version": 1, "run": run_view, "as_of": at, "as_of_event_id": snap.as_of_event_id,
            "teams": list(teams.values()), "tasks": tasks, "dependencies": dependencies,
            "workers": workers, "jobs": jobs, "approvals": approvals, "budget": budget, "budgets": budgets,
            "capacity": plan.capacity.as_dict(), "dispatch": planned, "needs": all_needs,
            "progress": plan.progress.as_dict(), "paths": [asdict(path) for path in snap.paths],
            "coordinator": dict(coordinator) if coordinator else None,
            "reactions": reactions, "feedback": feedback,
            **additions,
            "events": events, "event_cursor": {
                "after_event_id": after, "next_event_id": events[-1]["event_id"] if events else after,
                "as_of_event_id": snap.as_of_event_id, "has_more": has_more,
            },
        }


__all__ = ["MAX_EVENT_ROWS", "MAX_BOARD_ROWS", "MAX_BOARD_BYTES", "SharedBoardService", "bounded_transport"]
