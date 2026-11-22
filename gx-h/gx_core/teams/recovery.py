"""What time and refusals have already decided, settled when somebody looks.

``contracts/states.json`` (task → ``recovery``): *rejection and expiry are settled inside
the next call that looks, with no daemon*. v1 left a REJECTED task and a task whose
lease had lapsed where they fell: the machine had the arrows back to READY and nothing
ever took them, so one refused submission ended a run.

Four things are settled here, each a task transition the machine declares:

1. **A lapsed lease.** The lease is released and its task goes back to READY as the next
   attempt -- ``CLAIMED → READY``, or ``RUNNING → RETRYABLE → READY`` so the attempt
   stays on record -- or, with no attempt left, to BLOCKED.
2. **A rejected submission.** ``REJECTED → READY`` as the next attempt, or
   ``REJECTED → RETRYABLE → BLOCKED`` with every refusal listed.
3. **A held submission whose approval was decided.** GRANTED for that submission's
   sha256: ``BLOCKED → VALIDATING`` and the bundle is judged now. DENIED, EXPIRED or
   REVOKED: back to READY as a new attempt.
4. **A dependency that finished.** ``PENDING → READY`` when every dependency is ACCEPTED,
   ``PENDING → CANCELLED`` when one of them ended or was cancelled: what it waited for
   will never be accepted, and a task that can never start is not left open.

A task with no attempt left **ends its chain**: it stays BLOCKED with every refusal listed, which is what a person revives; the
task service cancels what depended on it, gives back what the chain still held, and
records ``schedule.chain_ended`` -- all in the transaction that found it out of attempts.

None of these moves the run's revision: they are consequences, not decisions, and a
query that changed the revision by being asked would invalidate the plan it returns.

The module holds the rules and no service: what entering READY involves (compiling the
brief for the attempt) and how a held submission is judged are the task service's, passed
in as two functions.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Callable

from .. import contracts
from ..domain import clock
from ..persistence import records, store

TASK = "task"
BACKEND = "backend"

#: ``hold_json.reason`` values. A task is BLOCKED for exactly one of them.
HOLD_OUT_OF_ATTEMPTS = "out_of_attempts"
HOLD_DEPENDENCY_CANCELLED = "dependency_cancelled"
HOLD_MODEL_POLICY = "model_policy"
#: It failed to be dispatched ``max_attempts`` times (design r2.2 §4.8).
HOLD_NOT_DISPATCHABLE = "not_dispatchable"
#: A failure was routed and its route's bound is spent (design r2.2 §6.2, END_CHAIN).
HOLD_ROUTED = "routed"
#: A worker refused, or was refused, before it handed anything in (design r2.2 §8.7).
HOLD_HOST_MODEL_REFUSAL = "host_model_refusal"

#: The chain ended because a person decided to skip the task (a worker_refusal DENIED, ruling 21).
HOLD_SKIPPED = "skipped"

#: Holds a person's decision on an approval lifts: the task is waited on, not ended. The values of
#: the r5 axis ``hold_reason``.
HOLDS_AWAITING_APPROVAL = tuple(contracts.label_values("hold_reason"))
#: Holds under which a task has ended its chain (§2.7.1; ``tasks.blocked_reason`` at
#: contract r5). It stays BLOCKED and is not open: nothing but a person's revival moves it.
#: The same four as the r5 axis ``blocked_reason.ended``. A task is no longer BLOCKED for
#: ``dependency_cancelled`` (what depends on an ended task is CANCELLED, §2.7.2), but a store
#: written before that can hold one, and it will never be ACCEPTED either. One definition:
#: the scheduler's snapshot imports both sets from here.
ENDED_HOLDS = tuple(contracts.labels()["blocked_reason"]["ended"])

#: Approval states that end a hold without releasing the submission.
_APPROVAL_REFUSED = ("DENIED", "EXPIRED", "REVOKED")

#: How many refusals of one attempt are carried on a task row and in a brief's input.
MAX_REFUSALS_PER_ATTEMPT = 12

EnterReady = Callable[[sqlite3.Connection, str, str], None]
ReleaseHeld = Callable[[sqlite3.Connection, sqlite3.Row, dict[str, Any]], str]
ApprovalStatus = Callable[[sqlite3.Connection, str], "str | None"]
#: ``(conn, task_id, blocked_reason, why)``: what the task service does when a task ended its chain.
EndChain = Callable[[sqlite3.Connection, str, str, str], Any]


# ------------------------------------------------------------------------------ reading

def _task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def staged_task_product(conn: sqlite3.Connection, task_id: str, lease_id: str) -> str | None:
    """Latest requested product that the named lease holder published during its attempt."""
    row = conn.execute(
        "SELECT a.artifact_ref FROM tasks t JOIN leases l ON l.task_id=t.task_id"
        " JOIN artifacts a ON a.task_id=t.task_id AND a.run_id=t.run_id AND a.put_by=l.owner"
        " WHERE t.task_id=? AND l.lease_id=? AND a.origin='HOST_PUT' AND a.published=1 AND a.bytes>0"
        " AND a.created_at>=l.issued_at AND a.created_at<l.expires_at"
        " AND (l.released_at IS NULL OR a.created_at<=l.released_at)"
        " AND (a.kind=t.bundle_kind OR a.kind IN (SELECT value FROM json_each(t.required_artifacts_json))"
        " OR (t.kind='review_asset' AND a.kind='independent_review_evidence'))"
        # At same-millisecond handoffs, use the order of publication and claim.
        " AND ((a.created_at>l.issued_at AND (l.released_at IS NULL OR a.created_at<l.released_at))"
        " OR l.lease_id=(SELECT json_extract(e.payload_json,'$.lease_id') FROM events e"
        " JOIN events p ON p.subject_kind='artifact' AND p.subject_id=a.artifact_ref"
        " AND p.kind='artifact.published' AND p.event_id>e.event_id"
        " WHERE e.subject_kind='task' AND e.subject_id=t.task_id AND e.kind='task.transition'"
        " AND e.new_state='CLAIMED' ORDER BY e.event_id DESC LIMIT 1))"
        " ORDER BY a.rowid DESC LIMIT 1", (task_id, lease_id)).fetchone()
    return row["artifact_ref"] if row else None


def _interrupted_product(conn, task, lease):
    """Attribute a staged native product to the Host wait or interruption that left it unfinished."""
    from ..hostobs import native_context
    from ..hostobs.session_receipts import native_sessions

    run = conn.execute("SELECT host_json FROM runs WHERE run_id=?", (task["run_id"],)).fetchone()
    if native_context.source_of_run(run) != native_context.SOURCE:
        return None
    product = staged_task_product(conn, task["task_id"], lease["lease_id"])
    if product is None:
        return None
    published = conn.execute("SELECT created_at FROM artifacts WHERE artifact_ref=?", (product,)).fetchone()[0]
    at = clock.parse(published)
    root = store.loads(run, "host_json").get("session_ref")
    try:
        sessions = native_sessions(store.data_root(conn), at=at)
        if not sessions:
            return None
        closure = sessions[0]
        if (not root or closure["root_thread_id"] != root or closure["session_id"] != root
                or closure["cleanup_failure"] is not None or closure["unresolved_previous_effects"] != []
                or closure["recovery_only"] is not False):
            return None
        if closure["closed_at"]:
            end = clock.parse(closure["closed_at"])
        else:  # Older native results only recorded the starting instant and elapsed duration.
            end = clock.parse(closure["started_at"]) + timedelta(seconds=closure["elapsed_seconds"])
        if not at <= end <= clock.parse(lease["expires_at"]):
            return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None  # No usable Host observation. Settle under the ordinary lapse rule.
    continuation = closure["project_continuation"] or {}
    waited = (closure["status"] == "COMPLETED" and closure["root_turn_status"] == "completed"
              and not closure["failure_type"] and closure["pending_requests"] == []
              and isinstance(continuation, dict) and isinstance(continuation.get("run_ids"), list)
              and continuation.get("reason") in ("WAITING_WORKER", "WAITING_JOB")
              and task["run_id"] in continuation["run_ids"])
    interrupted = (closure["status"] == "OUTCOME_UNKNOWN"
                   and closure["failure_type"] in ("AppServerTimeout", "AppServerDisconnected"))
    if waited or interrupted:
        return {"artifact_ref": product, "launch_id": closure["launch_id"]}
    return None


def counts_consumed(conn: sqlite3.Connection) -> bool:
    """Whether the store counts consumed attempts apart (``tasks.attempts_consumed``, contract r5)."""
    return store._has_column(conn, "tasks", "attempts_consumed")


def consumed(conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> int:
    """The attempts of a task that count against ``max_attempts`` (contract r5 task.rules.recovery).

    ``tasks.attempt`` numbers leases, briefs and submissions and is never reused;
    ``tasks.attempts_consumed`` is what ``max_attempts`` bounds: an attempt is consumed when it
    ends in a REJECTED submission or lapses without evidence of a Host interruption;
    a hold and a submission refused for a reason that cannot change within the lease
    consume none. On a store written under r4, which has no such
    column, every attempt started counts, as it did then.
    """
    if counts_consumed(conn):
        return int(row["attempts_consumed"] or 0)
    return int(row["attempt"])


def lapsed_leases(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Unreleased leases past their expiry and not held open by a job of that lease."""
    from ..jobs import leases as job_leases
    at = clock.now_iso()
    held = job_leases.held_open(conn, run_id, at=at)
    rows = conn.execute(
        "SELECT l.* FROM leases l JOIN tasks t ON t.task_id = l.task_id"
        " WHERE t.run_id=? AND l.released_at IS NULL ORDER BY l.issued_at, l.lease_id",
        (run_id,),
    ).fetchall()
    return [row for row in rows if clock.parse(row["expires_at"]) <= clock.parse(at)
            and held.get(row["task_id"], {}).get("lease_ref") != row["lease_id"]]


def live_lease(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    from ..jobs import leases as job_leases
    row = conn.execute(
        "SELECT l.* FROM leases l JOIN tasks t ON t.lease_id=l.lease_id"
        " WHERE t.task_id=? AND l.released_at IS NULL", (task_id,),
    ).fetchone()
    if row is None or not clock.is_past(row["expires_at"]):
        return row
    held = job_leases.live_deadline(conn, task_id)
    return row if held["lease_ref"] == row["lease_id"] and held["deadline_at"] else None


def attempt_history(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Every finished attempt of a task and how it ended, oldest first.

    Built from what the backend recorded: one row of ``submissions`` per attempt that
    handed something in, and one ``task.attempt_lapsed`` event per attempt whose lease
    ran out. This is what "with every refusal listed" and "the brief recompiled to carry
    what was refused and why" are made from.
    """
    history: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM submissions WHERE task_id=? ORDER BY attempt, created_at, submission_id",
        (task_id,),
    ):
        validation = store.loads(row, "validation_json", {}) or {}
        refused = [
            {"index": item.get("index"), "code": item.get("code"), "message": item.get("message")}
            for item in validation.get("records", []) if not item.get("ok")
        ]
        history.append({
            "attempt": int(row["attempt"]),
            "outcome": row["result"],
            "submission_id": row["submission_id"],
            "at": row["created_at"],
            "accepted_count": int(row["accepted_count"]),
            "rejected_count": int(row["rejected_count"]),
            "summary": validation.get("summary"),
            "refused": refused[:MAX_REFUSALS_PER_ATTEMPT],
            "refused_total": int(row["rejected_count"]),
            "records_ref": row["records_artifact_ref"],
            "policy_verdict": row["policy_verdict"],
        })
    for row in conn.execute(
        "SELECT ts, reason, payload_json FROM events WHERE subject_kind='task' AND subject_id=?"
        " AND kind='task.attempt_lapsed' ORDER BY event_id",
        (task_id,),
    ):
        payload = store.loads(row, "payload_json", {}) or {}
        history.append({
            "attempt": int(payload.get("attempt") or 0),
            "outcome": "LAPSED",
            "at": row["ts"],
            "summary": row["reason"],
            "lease_id": payload.get("lease_id"),
            "status_at_lapse": payload.get("status_at_lapse"),
        })
    history.sort(key=lambda item: (item["attempt"], item["at"]))
    return history


def pending_holds(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """The run's held tasks still waiting for a person: ``{task_id, approval_id, hold}``.

    A hold whose approval has been decided is not pending: the next settle releases or
    requeues it.
    """
    found = []
    for row in conn.execute(
        "SELECT task_id, hold_json FROM tasks WHERE run_id=? AND status='BLOCKED' AND hold_json IS NOT NULL"
        " ORDER BY created_at, task_id", (run_id,),
    ):
        hold = store.loads(row, "hold_json", {}) or {}
        if hold.get("reason") not in HOLDS_AWAITING_APPROVAL or not hold.get("approval_id"):
            continue
        approval = conn.execute("SELECT status FROM approvals WHERE approval_id=?",
                                (hold["approval_id"],)).fetchone()
        if approval is not None and approval["status"] == "PENDING":
            found.append({"task_id": row["task_id"], "approval_id": hold["approval_id"], "hold": hold})
    return found


def failed_dispatches(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """READY tasks whose dispatch failures exhausted their bound since entering READY."""
    rows = conn.execute(
        "SELECT t.task_id,t.max_attempts,COUNT(e.event_id) AS failures FROM tasks t JOIN events e"
        " ON e.subject_kind='task' AND e.subject_id=t.task_id AND e.kind='task.dispatch_failed'"
        " AND e.event_id>COALESCE((SELECT MAX(r.event_id) FROM events r WHERE r.subject_kind='task'"
        " AND r.subject_id=t.task_id AND r.kind='task.transition' AND r.new_state='READY'),0)"
        " WHERE t.run_id=? AND t.status='READY' GROUP BY t.task_id,t.max_attempts"
        " HAVING COUNT(e.event_id)>=t.max_attempts ORDER BY t.task_id", (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def needs_settling(conn: sqlite3.Connection, run_id: str) -> bool:
    """Whether :func:`settle` would change anything. Reads only, takes no write lock.

    A dispatch plan is asked for far more often than there is anything to settle, and a
    plan that took the store's write lock every time would queue sixteen workers behind
    each other for nothing.
    """
    if failed_dispatches(conn, run_id):
        return True
    if conn.execute(
        "SELECT 1 FROM tasks WHERE run_id=? AND status IN ('REJECTED','RETRYABLE') LIMIT 1",
        (run_id,),
    ).fetchone():
        return True
    if lapsed_leases(conn, run_id):
        return True
    for row in conn.execute(
        "SELECT task_id FROM tasks WHERE run_id=? AND status='PENDING'", (run_id,)
    ):
        ready, blocking = records.dependencies_satisfied(conn, row["task_id"])
        if ready or _cancelled(conn, blocking):
            return True
    for row in conn.execute(
        "SELECT hold_json FROM tasks WHERE run_id=? AND status='BLOCKED' AND hold_json IS NOT NULL",
        (run_id,),
    ):
        hold = store.loads(row, "hold_json", {}) or {}
        approval_id = hold.get("approval_id")
        if hold.get("reason") not in HOLDS_AWAITING_APPROVAL or not approval_id:
            continue
        approval = conn.execute(
            "SELECT status, expires_at FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if approval is None:
            continue
        if approval["status"] != "PENDING":
            if not hold.get("submission_id") and approval["status"] not in ("GRANTED", "DENIED"):
                continue
            return True
        if approval["expires_at"] and clock.is_past(approval["expires_at"]):
            return True
    return False


def has_ended(row: sqlite3.Row | None) -> bool:
    """Whether a task ended its chain: BLOCKED for a reason only a person's revival lifts."""
    if row is None or row["status"] != "BLOCKED":
        return False
    reason = row["blocked_reason"] if "blocked_reason" in row.keys() else None
    return (reason or (store.loads(row, "hold_json", {}) or {}).get("reason")) in ENDED_HOLDS


def _cancelled(conn: sqlite3.Connection, task_ids: list[str]) -> list[str]:
    """Those of ``task_ids`` that will never be ACCEPTED: cancelled, or ended (§2.7.1)."""
    if not task_ids:
        return []
    rows = conn.execute(
        "SELECT task_id, status, hold_json FROM tasks WHERE status IN ('CANCELLED','BLOCKED') AND task_id IN ("
        + ",".join("?" for _ in task_ids) + ")",
        list(task_ids),
    ).fetchall()
    return sorted(row["task_id"] for row in rows if row["status"] == "CANCELLED" or has_ended(row))


# ---------------------------------------------------------------------------- settling

def derived(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """The payload of a record the backend decided, in whoever's call it was written.

    ``actor`` on a record is the principal whose call it was written in: every record says
    which scope and subject wrote it. Where what was written is the backend's own
    conclusion -- a lease that lapsed, a bundle that was judged, a dependency that was
    met -- and not something that caller did, the record says so here.
    """
    return {**(payload or {}), "decided_by": BACKEND}


def _move(conn: sqlite3.Connection, task_id: str, to_state: str, *, reason: str,
          extra: dict[str, Any] | None = None, payload: dict[str, Any] | None = None,
          actor: str = BACKEND) -> None:
    store.transition(
        conn, machine=TASK, subject_id=task_id, to_state=to_state, actor=actor,
        reason=reason, evidence_level="EXECUTION_OBSERVED", extra_updates=extra,
        payload=derived(payload), bump=False,
    )


def requeue_or_exhaust(conn: sqlite3.Connection, task_id: str, *, reason: str,
                       enter_ready: EnterReady, actor: str = BACKEND,
                       end_chain: EndChain | None = None, consume: bool = True) -> str:
    """Send a task that failed an attempt back to READY, or BLOCK it with nothing left.

    Returns the state it ended in. The caller has already brought the task to a state the
    machine lets leave for READY (CLAIMED, RETRYABLE, REJECTED or a held BLOCKED). A task
    left with no attempt has ended its chain: ``end_chain`` is called for it, in this
    transaction, once it is BLOCKED with every refusal on its row.

    ``consume`` says whether the ending counts against ``max_attempts``. The task
    service reads the host's record before ordinary lapse settlement: a host-caused
    stop passes false here, a refusal becomes a hold, and a silent clean lapse
    consumes an attempt. A REJECTED submission consumes one; a hold does not.
    """
    task = _task(conn, task_id)
    if consume and counts_consumed(conn):
        conn.execute("UPDATE tasks SET attempts_consumed=COALESCE(attempts_consumed, 0)+1 WHERE task_id=?",
                     (task_id,))
        task = _task(conn, task_id)
    attempt, ceiling = consumed(conn, task), int(task["max_attempts"])
    if attempt < ceiling:
        enter_ready(conn, task_id, f"{reason}; {attempt} of {ceiling} attempts consumed, the next is open")
        return "READY"
    history = attempt_history(conn, task_id)
    hold = {
        "reason": HOLD_OUT_OF_ATTEMPTS,
        "attempts": attempt,
        "max_attempts": ceiling,
        "last": reason,
        "refusals": history,
    }
    if task["status"] == "BLOCKED":
        # A held submission whose approval was refused on the task's last attempt: it is
        # already BLOCKED, and what changes is why.
        conn.execute(
            "UPDATE tasks SET hold_json=?, owner=NULL, lease_id=NULL, updated_at=? WHERE task_id=?",
            (store._j(hold), clock.now_iso(), task_id),
        )
        store.append_event(
            conn, kind="task.hold_changed", actor=actor, run_id=task["run_id"], subject_kind=TASK,
            subject_id=task_id, prev_state="BLOCKED", new_state="BLOCKED",
            reason=f"out of attempts ({attempt} of {ceiling}): {reason}",
            evidence_level="EXECUTION_OBSERVED",
            payload=derived({"hold": {key: hold[key] for key in ("reason", "attempts", "max_attempts", "last")}}),
        )
        if end_chain is not None:
            end_chain(conn, task_id, HOLD_OUT_OF_ATTEMPTS, f"out of attempts ({attempt} of {ceiling}): {reason}")
        return "BLOCKED"
    if task["status"] == "REJECTED":
        # The machine has no arrow from REJECTED to BLOCKED; the attempt that was refused
        # is first recorded as one that may not be retried.
        _move(conn, task_id, "RETRYABLE", reason=reason, actor=actor)
    _move(
        conn, task_id, "BLOCKED",
        reason=f"out of attempts ({attempt} of {ceiling}): {reason}",
        extra={"owner": None, "lease_id": None, "hold_json": hold},
        payload={"hold": {key: hold[key] for key in ("reason", "attempts", "max_attempts", "last")},
                 "attempts_on_record": len(history)},
        actor=actor,
    )
    if end_chain is not None:
        end_chain(conn, task_id, HOLD_OUT_OF_ATTEMPTS, f"out of attempts ({attempt} of {ceiling}): {reason}")
    return "BLOCKED"


def settle(
    conn: sqlite3.Connection, *, run_id: str, enter_ready: EnterReady,
    release_held: ReleaseHeld, approval_status: ApprovalStatus, actor: str = BACKEND,
    end_chain: EndChain | None = None,
) -> dict[str, Any]:
    """Apply everything that is already decided for one run. Caller holds the transaction.

    ``actor`` is the principal in whose call this is happening; what is settled is the
    backend's conclusion and every record written here says both. ``end_chain`` is what
    the task service does for a task found out of attempts (§2.7).
    """
    out: dict[str, Any] = {
        "lapsed": [], "requeued": [], "exhausted": [], "released": [], "refused_holds": [],
        "promoted": [], "dependency_cancelled": [],
    }

    def landed(task_id: str, state: str) -> None:
        out["requeued" if state == "READY" else "exhausted"].append(task_id)

    # 1. leases that ran out
    for lease in lapsed_leases(conn, run_id):
        store.release_lease(conn, lease["lease_id"])
        task = _task(conn, lease["task_id"])
        if task is None or task["lease_id"] != lease["lease_id"] or task["status"] not in ("CLAIMED", "RUNNING"):
            continue  # the lease no longer governs its task; releasing it was all there was to do
        task_id = task["task_id"]
        reason = f"lease {lease['lease_id']} of {lease['owner']} expired at {lease['expires_at']}"
        checkpoint = _interrupted_product(conn, task, lease)
        if checkpoint:
            reason += f"; saved product {checkpoint['artifact_ref']} before native Host handoff or interruption"
        store.append_event(
            conn, kind="task.attempt_lapsed", actor=actor, run_id=run_id, subject_kind=TASK,
            subject_id=task_id, reason=reason, evidence_level="EXECUTION_OBSERVED",
            payload=derived({"attempt": int(task["attempt"]), "lease_id": lease["lease_id"],
                             "owner": lease["owner"], "principal": lease["principal"],
                             "expires_at": lease["expires_at"], "status_at_lapse": task["status"],
                             "attempt_consumed": checkpoint is None,
                             **({"checkpoint": checkpoint} if checkpoint else {})}),
        )
        out["lapsed"].append(task_id)
        if task["status"] == "RUNNING":
            _move(conn, task_id, "RETRYABLE", reason=reason,
                  extra={"owner": None, "lease_id": None}, actor=actor)
        landed(task_id, requeue_or_exhaust(conn, task_id, reason=reason, enter_ready=enter_ready, actor=actor,
                                           end_chain=end_chain, consume=checkpoint is None))

    # 2. held submissions whose approval was decided. Before the refusals: a release that
    #    ends REJECTED is then requeued by step 3 of this same settle, so one settle is a
    #    fixpoint and two reads of one state give one plan.
    for row in conn.execute(
        "SELECT * FROM tasks WHERE run_id=? AND status='BLOCKED' AND hold_json IS NOT NULL"
        " ORDER BY created_at, task_id", (run_id,),
    ).fetchall():
        hold = store.loads(row, "hold_json", {}) or {}
        approval_id = hold.get("approval_id")
        if hold.get("reason") != HOLD_MODEL_POLICY or not approval_id or not hold.get("submission_id"):
            continue
        status = approval_status(conn, approval_id)
        if status == "GRANTED":
            out["released"].append({"task_id": row["task_id"], "approval_id": approval_id,
                                    "task_status": release_held(conn, row, hold),
                                    "run_status_before": hold.get("run_status_before")})
        elif status in _APPROVAL_REFUSED:
            reason = f"approval {approval_id} for the held submission is {status}"
            out["refused_holds"].append({"task_id": row["task_id"], "approval_id": approval_id,
                                         "approval_status": status,
                                         "run_status_before": hold.get("run_status_before")})
            # A hold consumes no attempt (contract r5 task.rules.held).
            landed(row["task_id"], requeue_or_exhaust(conn, row["task_id"], reason=reason, enter_ready=enter_ready,
                                                      actor=actor, end_chain=end_chain, consume=False))

    # 3. refused submissions, and anything a validator left RETRYABLE
    for row in conn.execute(
        "SELECT task_id, status FROM tasks WHERE run_id=? AND status IN ('REJECTED','RETRYABLE')"
        " ORDER BY created_at, task_id", (run_id,),
    ).fetchall():
        reason = "the last submission was refused" if row["status"] == "REJECTED" else "the last attempt may be retried"
        landed(row["task_id"], requeue_or_exhaust(conn, row["task_id"], reason=reason, enter_ready=enter_ready,
                                                  actor=actor, end_chain=end_chain))

    # 4. dependencies that finished, or never will
    promote_pending(conn, run_id=run_id, enter_ready=enter_ready, out=out, actor=actor)
    return out


def promote_pending(conn: sqlite3.Connection, *, run_id: str, enter_ready: EnterReady,
                    out: dict[str, Any] | None = None, actor: str = BACKEND) -> dict[str, Any]:
    """``PENDING → READY`` where every dependency is ACCEPTED; CANCELLED where one will never be.

    A dependency that was cancelled, or that ended its chain (§2.7.1), will never be
    ACCEPTED, so what waits for it is cancelled and not left open (design r2.2 §2.7.2). The
    pass is repeated until nothing moves: cancelling one task settles the tasks behind it.
    """
    out = out if out is not None else {"promoted": [], "dependency_cancelled": []}
    out.setdefault("promoted", [])
    out.setdefault("dependency_cancelled", [])
    moved = True
    while moved:
        moved = False
        for row in conn.execute(
            "SELECT task_id FROM tasks WHERE run_id=? AND status='PENDING' ORDER BY created_at, task_id",
            (run_id,),
        ).fetchall():
            task_id = row["task_id"]
            ready, blocking = records.dependencies_satisfied(conn, task_id)
            if ready:
                enter_ready(conn, task_id, "every dependency is ACCEPTED")
                out["promoted"].append(task_id)
                continue
            dead = _cancelled(conn, blocking)
            if dead:
                _move(
                    conn, task_id, "CANCELLED",
                    reason="a dependency ended or was cancelled and will never be ACCEPTED: " + ", ".join(dead),
                    extra={"hold_json": {"reason": HOLD_DEPENDENCY_CANCELLED, "dependencies": dead}},
                    payload={"dead_dependencies": dead}, actor=actor,
                )
                out["dependency_cancelled"].append(task_id)
                moved = True
    return out


__all__ = [
    "ENDED_HOLDS", "HOLDS_AWAITING_APPROVAL", "HOLD_DEPENDENCY_CANCELLED", "HOLD_HOST_MODEL_REFUSAL",
    "HOLD_MODEL_POLICY", "HOLD_NOT_DISPATCHABLE", "HOLD_OUT_OF_ATTEMPTS", "HOLD_ROUTED",
    "MAX_REFUSALS_PER_ATTEMPT", "attempt_history", "has_ended", "lapsed_leases", "live_lease",
    "failed_dispatches", "needs_settling", "promote_pending", "requeue_or_exhaust", "settle", "staged_task_product",
]
