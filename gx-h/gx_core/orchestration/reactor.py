"""React to stored run facts when a task is claimed, submitted or resumed.

Job reconciliation, team settlement and trigger consumption run between tool
requests. Triggers identify a fact; its owner supplies the current state. A
successful reaction consumes its outbox row and records what happened. A
failure remains in the outbox for recovery. Queries do not drive mutations.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Mapping

from .. import config
from ..context import Ctx
from ..domain import ids, machines
from ..errors import (
    ApprovalRequired, ArtifactNotFound, ConcurrentRun, ContentHashMismatch, GxError, IllegalTransition,
    ModelPolicyViolation, VersionImmutable,
)
from ..persistence import store
from ..teams import recovery
from . import expansion, reactions, run_mode, seams

LOGGER = logging.getLogger("gx.reactor")

EVENT_REACTED = "schedule.reacted"
EVENT_FAILED = "schedule.reaction_failed"
EVENT_REFUSED = "schedule.reaction_refused"
#: Events the reactor writes about its own failures and refusals. They are not "the run
#: changed": counted as that, one failing reaction would be the reason to retry another.
OWN_FAILURE_EVENTS = (EVENT_FAILED, EVENT_REFUSED)

#: Failures with one code after which a reaction is parked (§2.4.8: "fails twice").
FAILURES_BEFORE_PARKED = 2

#: What a trigger of each topic is about, for the events that speak of it.
SUBJECT_KIND = {
    "submission.recorded": "submission", "submission.accepted": "submission", "job.terminal": "job", "approval.decided": "approval",
    "worker.stopped": "lease", "worker.verdict": "lease",
}

Reaction = Callable[[Ctx, str, sqlite3.Row, Mapping[str, Any]], Mapping[str, Any]]


# ------------------------------------------------------------------------- small reads

def _run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
    return row


def limits_sha256() -> str | None:
    path = config.CONFIG_DIR / "limits.json"
    return ids.sha256_of_file(path) if path.is_file() else None


def last_change(conn: sqlite3.Connection, run_id: str) -> int:
    """The run's last event that is not the reactor's own record of a failure or a refusal."""
    marks = ",".join("?" for _ in OWN_FAILURE_EVENTS)
    return int(conn.execute(
        f"SELECT COALESCE(MAX(event_id), 0) AS n FROM events WHERE run_id=? AND kind NOT IN ({marks})",
        (run_id, *OWN_FAILURE_EVENTS)).fetchone()["n"])


def _failure_of(row: sqlite3.Row) -> dict[str, Any] | None:
    """What the row's last failed reaction left on it, or None when none failed."""
    raw = row["last_error"]
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return {"code": "UNREADABLE", "count": FAILURES_BEFORE_PARKED, "parked": True, "message": str(raw)[:200]}
    return state if isinstance(state, dict) and not state.get("deferred") else None


def deferred_budget(row: sqlite3.Row) -> dict[str, Any] | None:
    """A pending resource bound is resumable bookkeeping, not a failed reaction."""
    try:
        value = json.loads(row["last_error"] or "null")
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("deferred") and value.get("budget_line") else None


def changed_since(conn: sqlite3.Connection, run_id: str, failure: Mapping[str, Any]) -> bool:
    """Whether the run has changed since a reaction failed (§2.4.8): the only reason to try again."""
    run = _run(conn, run_id)
    return (last_change(conn, run_id) > int(failure.get("after_event_id") or 0)
            or run["workflow_sha256"] != failure.get("workflow_sha256")
            or limits_sha256() != failure.get("limits_sha256"))


def _trigger(row: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"outbox_id": int(row["outbox_id"]), "topic": row["topic"], "subject": payload.get("subject"),
            "event_id": payload.get("event_id")}


def parked(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """The run's triggers whose reaction is parked: each a need of kind ``reaction`` (§2.8.2).

    A parked reaction is not tried again by itself. ``cleared`` says whether the run has
    changed since it failed, which is what lets a person's ``continue`` try it once more.
    """
    found = []
    for row in store.pending_triggers(conn, run_id):
        failure = _failure_of(row)
        if failure is None or not failure.get("parked"):
            continue
        payload = store.loads(row, "payload_json", {}) or {}
        found.append({**_trigger(row, payload), "code": failure.get("code"), "message": failure.get("message"),
                      "count": failure.get("count"), "failed_event_id": failure.get("event_id"),
                      "cleared": changed_since(conn, run_id, failure)})
    return found


# ------------------------------------------------------------------------------- pins

def _pins_refusal(conn: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any] | None:
    """Why nothing may be reacted to for this run: what is installed is not what it pinned."""
    if not run["workflow_id"]:
        return None   # a run created below the tool surface pinned no workflow to compare
    from . import service as runs   # the run service imports the task service; no cycle at call time

    try:
        workflow = runs.default_registry().get(run["workflow_id"], run["workflow_version"])
    except ArtifactNotFound:
        return {"code": "VERSION_IMMUTABLE", "what": "workflow_definition", "pinned_sha256": run["workflow_sha256"],
                "installed_sha256": None,
                "message": f"workflow {run['workflow_id']}@{run['workflow_version']} is pinned by run "
                           f"{run['run_id']} and is not installed"}
    try:
        runs.require_pins(run, workflow, conn=conn)
    except (VersionImmutable, ContentHashMismatch) as exc:
        return {"code": exc.code, "what": exc.details.get("what"), "pinned_sha256": exc.details.get("pinned_sha256"),
                "installed_sha256": exc.details.get("installed_sha256"), "message": exc.message}
    return None


def _record_refusal(ctx: Ctx, run_id: str, refusal: Mapping[str, Any], waiting: int) -> None:
    conn = ctx.conn
    seen = conn.execute(
        "SELECT 1 FROM events WHERE run_id=? AND kind=? AND json_extract(payload_json, '$.what')=?"
        " AND COALESCE(json_extract(payload_json, '$.pinned_sha256'), '')=?"
        " AND COALESCE(json_extract(payload_json, '$.installed_sha256'), '')=? LIMIT 1",
        (run_id, EVENT_REFUSED, refusal["what"], refusal["pinned_sha256"] or "", refusal["installed_sha256"] or ""),
    ).fetchone()
    if seen is not None:
        return
    with store.transaction(conn, transaction_owner="Reactor", operation="record_refusal", run_id=run_id) as tx:
        store.append_event(
            tx, kind=EVENT_REFUSED, actor=ctx.actor, run_id=run_id, subject_kind="run", subject_id=run_id,
            reason=str(refusal["message"])[:500], evidence_level="EXECUTION_OBSERVED",
            payload=recovery.derived({"code": refusal["code"], "what": refusal["what"],
                                      "pinned_sha256": refusal["pinned_sha256"],
                                      "installed_sha256": refusal["installed_sha256"],
                                      "triggers_waiting": waiting}),
        )


# -------------------------------------------------------------------------- reactions

#: Bundle kind of an accepted submission -> what the backend does about it. A kind that is
#: not here starts nothing: its acceptance is the whole of what happened.
ON_ACCEPTED: dict[str, Callable[[Ctx, str, sqlite3.Row, sqlite3.Row], Mapping[str, Any]]] = {
    "asset_review_bundle": reactions.review_accepted,
    "capability_manifest_bundle": reactions.asset_accepted,
}


def on_submission_recorded(ctx: Ctx, run_id: str, row: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
    """A durable hand-in may survive the worker and its response channel."""
    from ..teams.service import TeamService, backend_ctx

    subject = payload["subject"]
    match = ctx.conn.execute("SELECT 1 FROM submissions s JOIN tasks t ON t.task_id=s.task_id "
                             "WHERE s.submission_id=? AND t.run_id=?", (subject, run_id)).fetchone()
    if match is None:
        return {"reaction": None, "why": "no submission for this run"}
    validated = TeamService(ctx.conn, ctx=backend_ctx(ctx.conn)).validate_delivery(submission_id=subject)
    if validated.get("defer"):
        return validated
    return {"reaction": "validate_delivery", "submission_id": subject, "validation": validated}


def drive_science(ctx: Ctx, run_id: str) -> dict[str, Any] | None:
    """Advance a ready science checkpoint using GX, including after a lost notification."""
    from ..teams.service import backend_ctx

    conn = ctx.conn
    backend = backend_ctx(conn)
    run = _run(conn, run_id)
    if run["status"] not in {"ACTIVE", *machines.waiting_states("run")}:
        return None
    return run_mode.for_run(run).react(ctx, run, backend)


def _drive_workflow(ctx, run, backend):
    from . import service
    conn, run_id = ctx.conn, run["run_id"]
    if not run["workflow_id"]:
        return None
    hooks = service.hooks_for_run(conn, run_id)
    if not hooks.auto_advance:
        return None
    hooks.replan(conn, run_id)
    engine = service._engine(backend, run_id)
    # An unchanged parked run needs no write transaction. The selected wait
    # and its authority are checked again under the lock that consumes it.
    ready_refs = set()
    waits = engine.waits(run_id).values()
    cpu_sample = engine.cpu_resume_sample(waits)
    for wait in waits:
        ref = wait["interrupt_ref"]
        try:
            if engine.standing(run_id, interrupt_ref=ref, cpu_sample=cpu_sample).complete:
                ready_refs.add(ref)
        except IllegalTransition as exc:
            if exc.details.get("supplied_interrupt_ref") != ref:
                raise
    if not ready_refs and not engine.idle(run_id, retry_failed=False):
        return None
    workflow = service._pinned_workflow(conn, run)
    verified = service.require_pins(run, workflow, conn=conn)
    begun = None
    with store.transaction(conn, transaction_owner="Reactor", operation="drive_science", run_id=run_id) as tx:
        run = _run(tx, run_id)
        if run["status"] not in {"ACTIVE", *machines.waiting_states("run")}:
            return None
        if (workflow.workflow_id, workflow.version) != (run["workflow_id"], run["workflow_version"]):
            return None
        service._check_pins(run, verified, conn=tx)
        for wait in engine.waits(run_id, conn=tx).values():
            ref = wait["interrupt_ref"]
            if ref not in ready_refs or not engine.standing(run_id, conn=tx, interrupt_ref=ref, cpu_sample=cpu_sample).complete:
                continue
            try:
                service._require_no_held_submission(tx, run_id, pending=wait["interrupt"])
                service._require_workers_on_policy(tx, run_id, wait["interrupt"], round_index=wait.get("round"))
            except (ApprovalRequired, ModelPolicyViolation):
                continue
            try:
                begun = engine.begin_resume(tx, run_id=run_id, interrupt_ref=ref, actor=backend.actor, cpu_sample=cpu_sample)
            except ConcurrentRun:
                return None
            break
        if begun is None:
            if not engine.idle(run_id, retry_failed=False):
                return None
            service._require_no_held_submission(tx, run_id)
            begun = engine.begin_advance(tx, run_id=run_id, actor=backend.actor, retry_failed=False)
    return engine.advance_resumed(begun, report_branch_failures=True)


def on_submission_accepted(ctx: Ctx, run_id: str, row: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
    conn = ctx.conn
    submission = conn.execute("SELECT * FROM submissions WHERE submission_id=?", (payload.get("subject"),)).fetchone()
    task = None if submission is None else conn.execute(
        "SELECT * FROM tasks WHERE task_id=?", (submission["task_id"],)).fetchone()
    if submission is None or task is None or submission["result"] != "ACCEPTED" or task["run_id"] != run_id:
        return {"reaction": None, "why": "no accepted submission of this run is on record under the trigger's subject"}
    if task["stage"] and _run(conn,run_id)["status"]=="PAUSED":
        return {"defer":"the run is paused: this stored acceptance waits for its stage expansion"}
    reaction = ON_ACCEPTED.get(submission["bundle_kind"] or "")
    if reaction is None:
        return {"reaction": None, "why": f"an accepted {submission['bundle_kind'] or 'submission'} starts no reaction"}
    return dict(reaction(ctx, run_id, task, submission))


def on_job_terminal(ctx: Ctx, run_id: str, row: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
    """A job ended. Inside its lease the executor owns its jobs and reports their failure in
    its bundle, so nothing is routed while the job's task is open under a live lease (§2.3.6)."""
    conn = ctx.conn
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                       (payload.get("subject"),)).fetchone()
    if job is None or job["run_id"] != run_id or job["status"] not in machines.terminal_states("job"):
        return {"reaction": None, "why": "no ended job of this run is on record under the trigger's subject"}
    intent = store.loads(job, "submit_intent_json", {}) or {}
    if intent.get("purpose") == "probe":
        return {"reaction": None, "why": "the probe consumes this job's result"}
    if job["task_id"]:
        from ..p0 import waits
        waiter = waits.job_waiter(conn, run_id, job["task_id"], job["job_id"])
        if waiter and waiter["wait_job_id"] == job["job_id"]:
            return {"reaction": None, "job_status": job["status"], "why": "the parked attempt owns the terminal result"}
        task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (job["task_id"],)).fetchone()
        if task is not None and task["status"] in ("CLAIMED", "RUNNING") \
                and recovery.live_lease(conn, job["task_id"]) is not None:
            return {"reaction": None, "job_status": job["status"],
                    "why": f"task {job['task_id']} is open under a live lease: its executor owns the job and "
                           "reports it in its bundle"}
    outcome = store.loads(job, "outcome_json", {}) or {}
    failure = outcome.get("failure") or {}
    return {"reaction": "job_terminal", "job_id": job["job_id"], "job_status": job["status"],
            "task_id": job["task_id"], "failure_class": failure.get("failure_class")}


def on_approval_decided(ctx: Ctx,run_id: str,row: sqlite3.Row,payload: Mapping[str,Any]) -> dict[str,Any]:
    approval=ctx.conn.execute("SELECT * FROM approvals WHERE approval_id=? AND run_id=?",
                              (payload.get("subject"),run_id)).fetchone()
    if approval is None or approval["status"]=="PENDING":
        return {"reaction":None,"why":"no decided approval of this run bears out the trigger"}
    from ..teams.service import TeamService
    settled=TeamService(ctx.conn,ctx=ctx).settle(run_id=run_id)
    if approval["status"]=="GRANTED" and approval["subject_kind"]=="asset":
        for asset in ctx.conn.execute("SELECT asset_id,version FROM assets WHERE asset_id=? AND content_sha256=? AND kind IN ('TOOL_ADAPTER','ROLE_INTENT')",
                                     (approval["subject_id"],approval["subject_sha256"])).fetchall():
            seams.advance_asset(ctx,asset["asset_id"],asset["version"])
    return {"reaction":"settle_approval","approval_status":approval["status"],
            "released":settled.get("released") or []}


def on_worker_fact(ctx: Ctx,run_id: str,row: sqlite3.Row,payload: Mapping[str,Any]) -> dict[str,Any]:
    lease=ctx.conn.execute("SELECT l.*,t.run_id FROM leases l JOIN tasks t ON t.task_id=l.task_id"
                           " WHERE l.lease_id=? AND t.run_id=?",(payload.get("subject"),run_id)).fetchone()
    if lease is None:
        return {"reaction":None,"why":"no lease of this run bears out the trigger"}
    from ..teams.service import TeamService
    service=TeamService(ctx.conn,ctx=ctx)
    inspected=service.inspect_workers(run_id=run_id)
    service.settle(run_id=run_id)
    if row["topic"]=="worker.verdict":
        observation=ctx.conn.execute("SELECT * FROM worker_observations WHERE worker_observation_id=?"
            " AND task_id=? AND lease_id=?",(payload.get("worker_observation_id"),lease["task_id"],lease["lease_id"])).fetchone()
        if observation and observation["policy_verdict"] in ("MISMATCH","REFUSAL_OR_FALLBACK"):
            submission=ctx.conn.execute("SELECT * FROM submissions WHERE task_id=? AND lease_id=? AND result='ACCEPTED'"
                                        " ORDER BY rowid DESC LIMIT 1",(lease["task_id"],lease["lease_id"])).fetchone()
            if submission:
                from ..registry import Subject
                seams.request_approval(ctx,action_kind="model_switch",run_id=run_id,
                    subject=Subject(kind="submission",id=submission["submission_id"],sha256=submission["content_sha256"]),
                    request={"task_id":lease["task_id"],"worker_observation_id":observation["worker_observation_id"],
                             "policy_verdict":observation["policy_verdict"]})
    return {"reaction":"inspect_worker","settled":inspected}


#: Topic -> reaction. A topic that is not here has no reaction yet: its triggers wait,
#: unconsumed and unfailed, for the reaction that will read them.
REACTIONS: dict[str, Reaction] = {
    "submission.recorded": on_submission_recorded,
    "submission.accepted": on_submission_accepted,
    "job.terminal": on_job_terminal,
    "approval.decided": on_approval_decided,
    "worker.stopped": on_worker_fact,
    "worker.verdict": on_worker_fact,
}


# -------------------------------------------------------------------------- the loop

def _record_failure(ctx: Ctx, run_id: str, row: sqlite3.Row, payload: Mapping[str, Any],
                    exc: BaseException) -> dict[str, Any]:
    conn = ctx.conn
    code = exc.code if isinstance(exc, GxError) else type(exc).__name__
    message = str(exc.message if isinstance(exc, GxError) else exc)[:500]
    retryable = bool(exc.retryable) if isinstance(exc, GxError) else True
    before = _failure_of(row) or {}
    count = int(before.get("count") or 0) + 1 if before.get("code") == code else 1
    is_parked = count >= FAILURES_BEFORE_PARKED or not retryable
    trigger = _trigger(row, payload)
    try:
        with store.transaction(conn, transaction_owner="Reactor", operation="record_failure", run_id=run_id) as tx:
            state = {"code": code, "message": message[:300], "count": count, "parked": is_parked,
                     "retryable": retryable, "after_event_id": last_change(tx, run_id),
                     "workflow_sha256": _run(tx, run_id)["workflow_sha256"], "limits_sha256": limits_sha256()}
            state["event_id"] = store.append_event(
                tx, kind=EVENT_FAILED, actor=ctx.actor, run_id=run_id,
                subject_kind=SUBJECT_KIND.get(row["topic"], "outbox"), subject_id=str(payload.get("subject")),
                reason=f"{code}: {message}"[:500], evidence_level="EXECUTION_OBSERVED",
                payload=recovery.derived({"cause": trigger, "key": row["idempotency_key"], "code": code,
                                          "message": message, "count": count, "parked": is_parked,
                                          "retryable": retryable}),
            )
            store.fail_outbox(tx, int(row["outbox_id"]), json.dumps(state, sort_keys=True))
    except sqlite3.Error:  # pragma: no cover - the store itself is failing; the warning still says what happened
        LOGGER.exception("could not record the failed reaction to %s %s", row["topic"], payload.get("subject"))
    then = ("it is parked: it is not tried again by itself and is a need of the run"
            if is_parked else "it is tried again when the run next changes")
    ctx.warn(f"the reaction to {row['topic']} {payload.get('subject')} failed ({code}): {message}; {then}")
    return {**trigger, "outcome": "parked" if is_parked else "failed", "code": code, "count": count}


def _react_to(ctx: Ctx, run_id: str, row: sqlite3.Row, *, retry_parked: bool) -> dict[str, Any]:
    conn = ctx.conn
    payload = store.loads(row, "payload_json", {}) or {}
    trigger = _trigger(row, payload)
    failure = _failure_of(row)
    if failure is not None:
        if failure.get("parked") and not retry_parked:
            return {**trigger, "outcome": "parked", "code": failure.get("code"), "count": failure.get("count")}
        if not changed_since(conn, run_id, failure):
            return {**trigger, "outcome": "parked" if failure.get("parked") else "failed",
                    "code": failure.get("code"), "count": failure.get("count"),
                    "why": "nothing of the run has changed since it failed"}
    reaction = REACTIONS.get(row["topic"])
    if reaction is None:   # pragma: no cover - react() reads only the topics REACTIONS holds
        return {**trigger, "outcome": "deferred", "why": f"no reaction to {row['topic']} is installed yet"}
    try:
        outcome = dict(reaction(ctx, run_id, row, payload) or {})
    except Exception as exc:  # noqa: BLE001 - a failed reaction is a record and a warning, never the caller's error
        if conn.in_transaction:  # pragma: no cover - a reaction left a transaction open: give the lock back
            conn.execute("ROLLBACK")
        return _record_failure(ctx, run_id, row, payload, exc)
    if outcome.get("defer"):
        pending = ({"deferred": True, "budget_line": outcome["budget_line"],
                    "required": outcome.get("required", 1), "message": outcome["defer"]}
                   if outcome.get("budget_line") else None)
        encoded = ids.canonical_json(pending) if pending else None
        if row["last_error"] != encoded:
            with store.transaction(
                conn, transaction_owner="Reactor", operation="defer_outbox", run_id=run_id,
            ) as tx:
                tx.execute("UPDATE outbox SET last_error=? WHERE outbox_id=?", (encoded, row["outbox_id"]))
        return {**trigger, **outcome, "outcome": "deferred", "why": outcome["defer"]}
    with store.transaction(conn, transaction_owner="Reactor", operation="complete_outbox", run_id=run_id) as tx:
        if store.complete_outbox(tx, int(row["outbox_id"]), ctx.actor):
            store.append_event(
                tx, kind=EVENT_REACTED, actor=ctx.actor, run_id=run_id,
                subject_kind=SUBJECT_KIND.get(row["topic"], "outbox"), subject_id=str(payload.get("subject")),
                reason=str(outcome.get("reaction") or outcome.get("why") or "nothing to do")[:500],
                evidence_level="EXECUTION_OBSERVED",
                payload=recovery.derived({"cause": trigger, "reaction": outcome}),
            )
    return {**trigger, "outcome": "reacted", **outcome}


def react(ctx: Ctx, run_id: str, *, retry_parked: bool = False) -> list[dict[str, Any]]:
    """React to the run's unconsumed triggers, oldest first. Outside any transaction.

    Returns one entry per trigger looked at: ``reacted`` (consumed, with what was done),
    ``deferred`` (it waits, unfailed: for a review, a state, a reaction not built yet),
    ``failed`` or ``parked``, or one ``refused`` entry when the pins do not hold.
    """
    conn = ctx.conn
    if conn.in_transaction:
        raise RuntimeError("reactor.react was called inside an open transaction; a reaction reads and computes "
                           "outside the write lock (SCHEDULING_DESIGN §2.4.4)")
    # Only the topics a reaction is installed for: a row that waits for a reaction not built yet
    # stays unconsumed and unfailed, and is not read at all until its reaction exists.
    rows = store.pending_triggers(conn, run_id, topics=tuple(REACTIONS))
    if not rows:
        return []
    refusal = _pins_refusal(conn, _run(conn, run_id))
    if refusal is not None:
        _record_refusal(ctx, run_id, refusal, len(rows))
        ctx.warn(f"{len(rows)} trigger(s) of run {run_id} wait: {refusal['message']}")
        return [{"outcome": "refused", "triggers_waiting": len(rows), **refusal}]
    return [_react_to(ctx, run_id, row, retry_parked=retry_parked) for row in rows]


def lazy(ctx: Ctx, run_id: str, *, teams: Any | None = None, retry_parked: bool = False) -> dict[str, Any]:
    """The lazy path (§2.4.2): look at the run's jobs again, settle, react. Returns what each did.

    For the start of a claim, a submit and a resume, and the end of a submit. Inside a
    caller's transaction only the settlement is done -- looking at a job publishes files a
    rollback would not bring back, and a reaction computes outside the lock -- and the
    answer says so under ``skipped``.
    """
    conn = ctx.conn
    if teams is None:
        from ..teams.service import TeamService   # the task service calls this module; no cycle at call time

        teams = TeamService(conn, ctx=ctx)
    out: dict[str, Any] = {"reconciled": [], "settled": {}, "reactions": []}
    if conn.in_transaction:
        out["settled"] = teams.settle(run_id=run_id)
        out["skipped"] = "inside the caller's transaction: no job was looked at and no trigger reacted to"
        return out
    run=_run(conn,run_id)
    if run["workflow_id"]:
        from . import repin, service
        try:
            workflow=service.default_registry().get(run["workflow_id"],run["workflow_version"])
            repin.ensure(ctx,run,workflow)
        except (GxError,) as exc:
            # Hand-ins/settlement still stand; a mismatch keeps all expansions
            # behind react()'s pins-first refusal until a person grants this pair.
            ctx.warn(f"run pins await review: {exc.code}: {exc.message}")
    try:
        out["reconciled"] = seams.reconcile_jobs(conn, run_id=run_id, actor=ctx.actor) or []
    except Exception as exc:  # noqa: BLE001 - one job that cannot be looked at must not stop a claim or a hand-in
        LOGGER.exception("reconciling the jobs of run %s failed", run_id)
        ctx.warn(f"the jobs of run {run_id} could not be looked at again ({type(exc).__name__}: {str(exc)[:300]}); "
                 "a job that ended unseen is announced by the next call that looks")
    out["settled"] = teams.settle(run_id=run_id)
    out["reactions"] = react(ctx, run_id, retry_parked=retry_parked)
    if any(r.get("reaction") == "validate_delivery" and r["outcome"] == "reacted" for r in out["reactions"]):
        # Validation may have committed submission.accepted after the first snapshot.
        # Drain the consequence even when this was the final hand-in.
        out["reactions"].extend(react(ctx, run_id))
    derive_waiting(ctx,run_id)
    return out


def derive_waiting(ctx: Ctx,run_id: str) -> None:
    run=_run(ctx.conn,run_id)
    if run["status"] not in machines.waiting_states("run"):
        return
    checkpoint = store.latest_checkpoint(ctx.conn, run_id)
    state=store.loads(checkpoint,"state_json",{}) or {}
    pending = state.get("pending_interrupt") or {}
    if (len(state.get("node_waits", {})) > 1 or pending.get("kind") != "stage"
            or (pending.get("payload") or {}).get("wait_for") == "cpu.threads_free"):
        # Mixed waits already project their selected head. A CPU admission wait
        # can be held entirely by another Run, outside the local counts below.
        return
    approvals=ctx.conn.execute("SELECT 1 FROM approvals WHERE run_id=? AND status='PENDING'"
        " AND action_kind IN ('model_switch','worker_refusal') LIMIT 1",(run_id,)).fetchone()
    tasks=expansion.stage_tasks(ctx.conn,run_id,checkpoint["node"])
    opened=[task for task in tasks if task["status"] not in ("ACCEPTED","CANCELLED") and not recovery.has_ended(task)
            and not (task["status"]=="BLOCKED" and (store.loads(task,"hold_json",{}) or {}).get("approval_id"))]
    jobs=ctx.conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status IN ('CREATED','SUBMITTING','SUBMITTED','QUEUED','RUNNING','CANCEL_REQUESTED') LIMIT 1",(run_id,)).fetchone()
    target="WAITING_APPROVAL" if approvals else "WAITING_CLIENT" if opened else "WAITING_EXTERNAL_JOB" if jobs else "WAITING_APPROVAL"
    if target!=run["status"]:
        with store.transaction(
            ctx.conn, transaction_owner="Reactor", operation="derive_waiting", run_id=run_id,
        ) as tx:
            current=_run(tx,run_id)
            latest = store.latest_checkpoint(tx, run_id)
            if (latest is not None and latest["seq"] == checkpoint["seq"] and
                    current["status"] in machines.waiting_states("run") and current["status"]!=target):
                store.transition(tx,machine="run",subject_id=run_id,to_state=target,actor=ctx.actor,
                    reason="the stage's waiting state follows its stored tasks, jobs and approvals",
                    evidence_level="EXECUTION_OBSERVED",payload=recovery.derived({"stage":checkpoint["node"]}))


__all__ = [
    "EVENT_FAILED", "EVENT_REACTED", "EVENT_REFUSED",
    "FAILURES_BEFORE_PARKED", "ON_ACCEPTED", "REACTIONS", "changed_since", "last_change",
    "lazy", "limits_sha256", "on_job_terminal", "on_submission_accepted", "parked", "react",
]
