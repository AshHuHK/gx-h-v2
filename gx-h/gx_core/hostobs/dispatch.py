"""The hand-out record: which rows of a dispatch plan a coordinator gave a running gx-dispatch.

Design r2 §8.4 (R26). The Workflow tool returns as soon as it has launched, so for the
minutes it takes the dispatched agents to claim their tasks, those tasks are still READY.
Without a record of the hand-out a second plan would offer them again, and a second launch
would start a second agent on each. So when the PreToolUse(Workflow) gate admits a launch of
``gx-dispatch``, the backend writes what it handed out, and the plan (``gx_next_actions``)
lists those rows under ``offered`` instead of offering them again.

Everything lives in the ``events`` table, so a plan stays a pure function of the store:

``dispatch.handed_out``
    written by the gate when it admits a launch. ``subject_id`` is the hand-out id
    ``<session8>:<nonce>``, which is also the prefix of every worker subject in the launch
    (``<session8>:<nonce>:<task_id>``). The payload names the session, the tool call, the
    rows (each with the task's ``attempt`` as the store held it at the gate, not as the
    coordinator typed it), the follow depth, and ``expires_at``.
``dispatch.launched``
    written on PostToolUse(Workflow): the host's workflow run id and task id.
``dispatch.closed``
    written when the dispatch is known to have stopped running: a Stop payload of the
    session no longer lists it, the launch failed, or the session ended. A StopFailure does
    not close it: the turn that failed was the coordinator's, and a background workflow of
    the session goes on running.

A hand-out is open at an instant when it has no ``closed`` event and its ``expires_at`` lies
after that instant. The TTL is the coordinator lease's length (``task_budget.
coordinator_lease_seconds``): a hand-out lives no longer than the lease of the coordinator
that made it would last without renewal. A row is *offered* while its hand-out is open and
its task is still READY at the recorded attempt; once claimed, the lease counts it in flight.

``task.dispatch_failed`` (§4.8) is written here too: when a gate denies a row for a reason of
its own task. A denial of a launch as a whole counts against no task (lead's ruling, 19:52Z).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable, Mapping

from .. import config
from ..domain import clock
from ..persistence import store

EVENT_HANDED_OUT = "dispatch.handed_out"
EVENT_LAUNCHED = "dispatch.launched"
EVENT_CLOSED = "dispatch.closed"
EVENT_DISPATCH_FAILED = "task.dispatch_failed"
SUBJECT_KIND = "dispatch"

CLOSED_NOT_RUNNING = "not_running_at_stop"
CLOSED_LAUNCH_FAILED = "launch_failed"
CLOSED_SESSION_ENDED = "session_ended"
CLOSE_REASONS = (CLOSED_NOT_RUNNING, CLOSED_LAUNCH_FAILED, CLOSED_SESSION_ENDED)

SOURCE_SPAWN_GATE = "spawn_gate"
SOURCE_WORKFLOW_GATE = "workflow_gate"

#: The host's answer when a Workflow was launched in the background.
LAUNCHED_STATUS = "async_launched"
#: A dispatch nonce: what the coordinator makes for each launch. Short, lower case.
NONCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
#: The part of a session id that names it in a subject.
SESSION_PREFIX_CHARS = 8
#: The follow depth gx-dispatch ships with, and the most a coordinator may ask for (§8.5).
DEFAULT_FOLLOW_DEPTH = 3
MAX_FOLLOW_DEPTH = 8

_DECIDED = {"decided_by": "backend"}
_TASK_READY = "READY"


def ttl_seconds() -> int:
    """How long a hand-out stays open without a closing event: the coordinator lease's length."""
    return int(config.limits()["task_budget"]["coordinator_lease_seconds"])


def session_prefix(session_ref: str) -> str:
    return str(session_ref)[:SESSION_PREFIX_CHARS]


def handout_id(session_ref: str, nonce: str) -> str:
    return f"{session_prefix(session_ref)}:{nonce}"


def subject_for(session_ref: str, nonce: str, task_id: str) -> str:
    """The subject a dispatched worker claims under: unique per hand-out, the same across its own retries."""
    return f"{handout_id(session_ref, nonce)}:{task_id}"


# ------------------------------------------------------------------------------ reading

def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"] or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _handouts(conn: sqlite3.Connection, where: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Hand-outs with their launch and close, oldest first. ``where`` filters the handed_out rows."""
    found: list[dict[str, Any]] = []
    for row in conn.execute(
        f"SELECT event_id, run_id, ts, payload_json FROM events WHERE kind=? AND {where} ORDER BY event_id",
        (EVENT_HANDED_OUT, *args),
    ):
        payload = _payload(row)
        found.append({**payload, "event_id": int(row["event_id"]), "run_id": row["run_id"],
                      "workflow_run_id": None, "workflow_task_id": None, "closed": None})
    if not found:
        return found
    by_id = {item["handout_id"]: item for item in found if item.get("handout_id")}
    marks = ",".join("?" * len(by_id))
    for row in conn.execute(
        f"SELECT kind, subject_id, payload_json FROM events WHERE kind IN (?,?) AND subject_kind=? "
        f"AND subject_id IN ({marks}) ORDER BY event_id",
        (EVENT_LAUNCHED, EVENT_CLOSED, SUBJECT_KIND, *by_id),
    ):
        item = by_id.get(row["subject_id"])
        if item is None:
            continue
        payload = _payload(row)
        if row["kind"] == EVENT_LAUNCHED:
            item["workflow_run_id"] = payload.get("workflow_run_id")
            item["workflow_task_id"] = payload.get("workflow_task_id")
        elif item["closed"] is None:
            item["closed"] = {"reason": payload.get("reason")}
    return found


def _is_open(item: Mapping[str, Any], at: str | None) -> bool:
    if item.get("closed") is not None:
        return False
    expires = item.get("expires_at")
    if not isinstance(expires, str):
        return False
    try:
        return clock.parse(expires) > (clock.parse(at) if at else clock.now())
    except (TypeError, ValueError):
        return False


def open_handouts(conn: sqlite3.Connection, *, run_id: str | None = None, session_ref: str | None = None,
                  at: str | None = None) -> list[dict[str, Any]]:
    """The hand-outs open at ``at`` (now by default), of one run or one session. A pure store read."""
    clauses, args = ["1=1"], []
    if run_id is not None:
        clauses.append("run_id=?")
        args.append(run_id)
    if session_ref is not None:
        clauses.append("json_extract(payload_json, '$.session_ref')=?")
        args.append(session_ref)
    return [item for item in _handouts(conn, " AND ".join(clauses), tuple(args)) if _is_open(item, at)]


def offered_rows(conn: sqlite3.Connection, run_id: str, *, at: str | None = None) -> list[dict[str, Any]]:
    """The rows of a run handed to a running dispatch and not claimed yet, as of ``at``.

    A row counts as offered while its hand-out is open and its task is still READY at the
    attempt the store held when the row was handed out. A pure store read, for the plan.
    """
    out: list[dict[str, Any]] = []
    for item in open_handouts(conn, run_id=run_id, at=at):
        rows = item.get("rows")
        for row in rows if isinstance(rows, list) else []:
            # One malformed event of a run must not fail whoever reads its plan or its stop gate:
            # a row that is not a mapping, or whose attempt is not a number, is not a row.
            if not isinstance(row, Mapping) or isinstance(row.get("attempt"), bool):
                continue
            try:
                attempt = int(row.get("attempt") or 0)
            except (TypeError, ValueError):
                continue
            task = conn.execute("SELECT status, attempt FROM tasks WHERE task_id=?", (row.get("task_id"),)).fetchone()
            if task is None or task["status"] != _TASK_READY or int(task["attempt"] or 0) != attempt:
                continue
            out.append({"task_id": row["task_id"], "attempt": attempt, "role": row.get("role"),
                        "handout_id": item["handout_id"], "session_ref": item.get("session_ref"),
                        "workflow_run_id": item.get("workflow_run_id"), "expires_at": item.get("expires_at")})
    return out


def nonce_used(conn: sqlite3.Connection, *, session_ref: str, nonce: str) -> bool:
    """Whether this session already handed out a launch under this nonce (open or not)."""
    return conn.execute(
        "SELECT 1 FROM events WHERE kind=? AND subject_kind=? AND subject_id=? LIMIT 1",
        (EVENT_HANDED_OUT, SUBJECT_KIND, handout_id(session_ref, nonce)),
    ).fetchone() is not None


def last_dispatch_failed(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether the task's last dispatch failed: a ``task.dispatch_failed`` after it last became READY."""
    failed = conn.execute(
        "SELECT MAX(event_id) AS last FROM events WHERE kind=? AND subject_kind='task' AND subject_id=?",
        (EVENT_DISPATCH_FAILED, task_id),
    ).fetchone()["last"]
    if failed is None:
        return False
    ready = conn.execute(
        "SELECT MAX(event_id) AS last FROM events WHERE kind='task.transition' AND subject_id=? AND new_state=?",
        (task_id, _TASK_READY),
    ).fetchone()["last"]
    return ready is None or int(failed) > int(ready)


# ------------------------------------------------------------------------------ writing

def record_handout(conn: sqlite3.Connection, *, run_id: str, host: str, session_ref: str, nonce: str,
                   tool_use_id: str | None, rows: Iterable[Mapping[str, Any]], follow_depth: int,
                   actor: str, host_event_id: str | None = None) -> dict[str, Any]:
    """Write ``dispatch.handed_out`` for an admitted launch; returns its payload."""
    ident = handout_id(session_ref, nonce)
    ttl = ttl_seconds()
    payload = {
        "handout_id": ident, "host": host, "session_ref": session_ref, "tool_use_id": tool_use_id,
        "nonce": nonce, "rows": [dict(row) for row in rows], "follow_depth": int(follow_depth),
        "issued_at": clock.now_iso(), "expires_at": clock.iso_after(ttl), "ttl_seconds": ttl,
        "host_event_id": host_event_id, **_DECIDED,
    }
    store.append_event(
        conn, kind=EVENT_HANDED_OUT, actor=actor, run_id=run_id, subject_kind=SUBJECT_KIND, subject_id=ident,
        reason=f"{len(payload['rows'])} row(s) handed to gx-dispatch by session {session_prefix(session_ref)}",
        evidence_level="EXECUTION_OBSERVED", payload=payload,
    )
    return payload


def _handout_of_call(conn: sqlite3.Connection, *, session_ref: str, tool_use_id: str) -> dict[str, Any] | None:
    found = _handouts(conn, "json_extract(payload_json, '$.session_ref')=? AND json_extract(payload_json, '$.tool_use_id')=?",
                      (session_ref, tool_use_id))
    return found[-1] if found else None


def record_launched(conn: sqlite3.Connection, *, session_ref: str, tool_use_id: str | None,
                    workflow_run_id: str | None, workflow_task_id: str | None, status: str | None,
                    actor: str) -> dict[str, Any] | None:
    """PostToolUse(Workflow): the run the host made of a hand-out; a launch that did not start closes it."""
    if not tool_use_id:
        return None
    item = _handout_of_call(conn, session_ref=session_ref, tool_use_id=tool_use_id)
    if item is None:
        return None
    if status == LAUNCHED_STATUS and (workflow_run_id or workflow_task_id):
        store.append_event(
            conn, kind=EVENT_LAUNCHED, actor=actor, run_id=item["run_id"], subject_kind=SUBJECT_KIND,
            subject_id=item["handout_id"], reason="the host launched the hand-out's gx-dispatch",
            evidence_level="EXECUTION_OBSERVED",
            payload={"handout_id": item["handout_id"], "workflow_run_id": workflow_run_id,
                     "workflow_task_id": workflow_task_id, "status": status, **_DECIDED},
        )
        return {"handout_id": item["handout_id"], "launched": True}
    close(conn, item, reason=CLOSED_LAUNCH_FAILED, actor=actor, detail=status)
    return {"handout_id": item["handout_id"], "launched": False}


def close(conn: sqlite3.Connection, item: Mapping[str, Any], *, reason: str, actor: str,
          detail: str | None = None) -> None:
    if reason not in CLOSE_REASONS:
        raise ValueError(f"{reason!r} is not a reason a hand-out closes; known: {CLOSE_REASONS}")
    payload = {"handout_id": item["handout_id"], "reason": reason, **_DECIDED}
    if detail:
        payload["detail"] = str(detail)[:128]
    store.append_event(
        conn, kind=EVENT_CLOSED, actor=actor, run_id=item.get("run_id"), subject_kind=SUBJECT_KIND,
        subject_id=item["handout_id"], reason=f"hand-out closed: {reason}", evidence_level="EXECUTION_OBSERVED",
        payload=payload,
    )


def no_longer_running(conn: sqlite3.Connection, *, session_ref: str, running_task_ids: list[str] | None,
                      dispatch_running: bool) -> list[dict[str, Any]]:
    """The session's open hand-outs a Stop payload shows are no longer running. Writes nothing.

    ``running_task_ids`` are the host task ids the payload lists as running workflows (None
    when the hook did not send them). A hand-out whose launch the host reported is running
    while its task id is listed; one whose launch was never reported, or any hand-out when no
    ids were sent, while a gx-dispatch of the session is named as running.
    """
    finished = []
    for item in open_handouts(conn, session_ref=session_ref):
        if running_task_ids is not None and item.get("workflow_task_id"):
            running = item["workflow_task_id"] in running_task_ids
        else:
            running = dispatch_running
        if not running:
            finished.append(item)
    return finished


def close_for_session(conn: sqlite3.Connection, *, session_ref: str, reason: str, actor: str) -> list[str]:
    """Close every open hand-out of the session (the session ended); returns their ids."""
    closed = []
    for item in open_handouts(conn, session_ref=session_ref):
        close(conn, item, reason=reason, actor=actor)
        closed.append(item["handout_id"])
    return closed


def record_dispatch_failed(conn: sqlite3.Connection, *, task_id: str, run_id: str | None, source: str,
                           reason: str, actor: str, host_event_id: str | None = None) -> None:
    """``task.dispatch_failed``: a gate denied this task's row for a reason of the task itself (§4.8)."""
    if source not in (SOURCE_SPAWN_GATE, SOURCE_WORKFLOW_GATE):
        raise ValueError(f"{source!r} is not a gate this module records for")
    store.append_event(
        conn, kind=EVENT_DISPATCH_FAILED, actor=actor, run_id=run_id, subject_kind="task", subject_id=task_id,
        reason=str(reason)[:500], evidence_level="EXECUTION_OBSERVED",
        payload={"source": source, "reason": str(reason)[:500], "host_event_id": host_event_id, **_DECIDED},
    )


__all__ = [
    "CLOSE_REASONS", "DEFAULT_FOLLOW_DEPTH", "EVENT_CLOSED", "EVENT_DISPATCH_FAILED", "EVENT_HANDED_OUT",
    "EVENT_LAUNCHED", "MAX_FOLLOW_DEPTH", "NONCE_RE", "SOURCE_SPAWN_GATE", "SOURCE_WORKFLOW_GATE", "close",
    "close_for_session", "handout_id", "last_dispatch_failed", "no_longer_running", "nonce_used", "offered_rows",
    "open_handouts",
    "record_dispatch_failed", "record_handout", "record_launched", "session_prefix", "subject_for", "ttl_seconds",
]
