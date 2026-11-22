"""Jobs and the task leases they run under (SCHEDULING_DESIGN r2.2 §7.2; review change R14).

A worker submits a job while it holds a task's lease, and the job belongs to a detached
runner: it can outlive the lease. The worker stops, its host session ends, a usage limit is
hit. Four things then went wrong. The lease lapsed while its GPU job was still in the
device's queue, and the next attempt submitted the step again. Nothing told the next
attempt what was already running, or done. A job nobody was responsible for kept its place
in the queue and its hold. And its ending was read as the business of whoever held the task
by then.

    job_lease(conn, job)                  the lease a job belongs to now: the one it was
                                          submitted under, or the one that last adopted it
    held_lease(conn, task_id, principal)  the task's live lease, if this principal holds it
    live_deadline(conn, task_id)          §7.2.1: until when the open jobs of the task's
                                          current lease may still end; the lease does not
                                          lapse before it
    held_open(conn, run_id)               the same for every task of a run, in three queries
                                          whatever its size: what the snapshot and core's
                                          settle both read, so that they cannot disagree
    lease_seconds_for(steps)              §7.2.6: the lease a claim of an execute_path row asks
                                          for, from the path's steps; pure
    earlier_jobs(conn, task_id)           §7.2.4: every job recorded for the task so far, for
                                          the brief of its next attempt (``tools.py`` renders it)
    adopt(conn, job, principal=…)         §7.2.4: a look at a job by the holder of its task's
                                          live lease takes it over (event ``job.adopted``)
    cancel_unadopted(conn, task_id, lease_ref, actor=…)
                                          §7.2.4: end the open jobs of the task that the new
                                          lease has no claim on and nobody can still take over

Every fact is the backend's own. Which lease a task is held under, by whom and until when is
read from ``tasks`` and ``leases``; which lease a job belongs to is what the job service
observed when it admitted the job, and the adoptions recorded since. Nothing is taken from a
request.

Until the contract has ``jobs.lease_ref`` (r5), the lease a job was submitted under is kept in
its submit intent (``lease_ref``, written by admission from its own reading of the task), and
an adoption is the latest ``job.adopted`` event of the job. :func:`job_lease` is the one
place that knows where to look.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Iterable, Mapping

from .. import config
from ..domain import clock
from ..persistence import store
from . import limits as limits_mod

ADOPTED_EVENT = "job.adopted"

#: The kind of task that executes a path: the one a path's job that names no task belongs to.
PATH_TASK_KIND = "execute_path"

#: Job states that are final. Spelt here, and checked against the contract by the service at
#: import, so that this module needs nothing of the service to be read.
_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


def _iso(moment: Any) -> str:
    return moment.astimezone(clock.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------------- reading leases

def _task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT task_id, run_id, path_id, kind, status, attempt, lease_id FROM tasks"
                        " WHERE task_id=?", (task_id,)).fetchone()


def _lease(conn: sqlite3.Connection, lease_ref: str | None) -> sqlite3.Row | None:
    if not lease_ref:
        return None
    return conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_ref,)).fetchone()


def held_lease(conn: sqlite3.Connection, task_id: str | None, principal: str | None) -> sqlite3.Row | None:
    """The lease ``task_id`` is held under now, when it is live (unexpired or held open
    by a job of that lease) and ``principal`` is the worker it was issued to. None otherwise: no such task, no
    lease, a lease that has ended, or somebody else's."""
    if not task_id or not principal:
        return None
    task = _task(conn, task_id)
    lease = _lease(conn, task["lease_id"]) if task is not None else None
    if lease is None or lease["released_at"] is not None or task["status"] not in ("CLAIMED", "RUNNING"):
        return None
    holder = lease["principal"] if lease["principal"] is not None else lease["owner"]
    if holder != principal:
        return None
    if clock.is_past(lease["expires_at"]):
        held = live_deadline(conn, task_id)
        if held["lease_ref"] != lease["lease_id"] or held["deadline_at"] is None:
            return None
    return lease


def renew(conn: sqlite3.Connection, lease: Mapping[str, Any], *, actor: str,
          limits: Mapping[str, Any] | None = None) -> str:
    """Renew a holder's accepted lease-bound call; never revive a lease that lapsed.

    Call after admission, inside its transaction. Effective liveness includes a job's
    hold, so polling a live job also leaves the worker its normal delivery interval.
    No new lease or attempt is created. Released, replaced and truly lapsed leases fail.
    """
    from ..errors import LeaseExpired
    current = _lease(conn, lease["lease_id"])
    task = _task(conn, lease["task_id"])
    if current is None or current["released_at"] is not None or task is None \
            or task["lease_id"] != current["lease_id"] or task["status"] not in ("CLAIMED", "RUNNING"):
        raise LeaseExpired("a released or replaced lease cannot be renewed")
    at = clock.now_iso()
    if clock.parse(current["expires_at"]) <= clock.parse(at):
        held = live_deadline(conn, current["task_id"], limits=limits, at=at)
        if not held["deadline_at"] or held["lease_ref"] != current["lease_id"]:
            raise LeaseExpired("a lapsed lease cannot be renewed", details={"lease_ref": current["lease_id"]})
    document = config.limits() if limits is None else limits
    seconds = document["task_budget"]["lease_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        raise ValueError("task_budget.lease_seconds must be a positive integer")
    expires = max(clock.parse(current["expires_at"]), clock.parse(at) + timedelta(seconds=seconds))
    expires_at = _iso(expires)
    if expires_at != current["expires_at"]:
        conn.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (expires_at, current["lease_id"]))
        store.append_event(conn, kind="task.lease_renewed", actor=actor, run_id=task["run_id"],
                           subject_kind="task", subject_id=task["task_id"],
                           evidence_level="EXECUTION_OBSERVED", reason="accepted call by the lease holder",
                           payload={"lease_id": current["lease_id"], "expires_at": expires_at,
                                    "previous_expires_at": current["expires_at"], "attempt": current["attempt"]})
    return expires_at


def lease_at_admission(conn: sqlite3.Connection, task_id: str | None, principal: str | None) -> str | None:
    """What admission records as the lease a job is submitted under: the id of the task's live
    lease when the submitter holds it, else None (a job with no task, a backend-owned job, a
    submitter that is not the holder)."""
    lease = held_lease(conn, task_id, principal)
    return lease["lease_id"] if lease is not None else None


def _lease_of(job: Mapping[str, Any], adopted: Mapping[str, Any] | None) -> dict[str, Any]:
    """The rule of :func:`job_lease`, on what has been read: the job's row and its latest
    ``job.adopted`` event (``{event_id, payload_json}``) or None."""
    if adopted is not None:
        payload = store.loads(adopted, "payload_json", {}) or {}
        return {"lease_ref": payload.get("lease_ref"), "basis": "adopted", "event_id": adopted["event_id"]}
    recorded = job["lease_ref"] if "lease_ref" in job.keys() else None        # the column, at r5
    if recorded is None:
        recorded = (store.loads(job, "submit_intent_json", {}) or {}).get("lease_ref")
    return {"lease_ref": recorded, "basis": "submitted" if recorded else None, "event_id": None}


def job_lease(conn: sqlite3.Connection, job: Mapping[str, Any]) -> dict[str, Any]:
    """The lease a job belongs to now: ``{lease_ref, basis, event_id}``. ``basis`` is
    ``adopted`` (the latest ``job.adopted`` event names the lease), ``submitted`` (the lease
    admission recorded) or None (no lease is on record for it)."""
    adopted = conn.execute(
        "SELECT event_id, payload_json FROM events WHERE subject_kind='job' AND subject_id=? AND kind=?"
        " ORDER BY event_id DESC LIMIT 1", (job["job_id"], ADOPTED_EVENT)).fetchone()
    return _lease_of(job, adopted)


def _executes_its_path(task: Mapping[str, Any]) -> bool:
    return bool(task["path_id"]) and task["kind"] == PATH_TASK_KIND


def task_jobs(conn: sqlite3.Connection, task: Mapping[str, Any]) -> list[sqlite3.Row]:
    """The jobs recorded for a task, in the order they were admitted: those that name it, and,
    for the task that executes a path, the jobs of that path that name no task (a path's job
    submitted without one belongs to the task that executes the path: the rule the execution
    bundle is judged by). ``task`` needs ``task_id``, ``path_id`` and ``kind``."""
    if _executes_its_path(task):
        return conn.execute(
            "SELECT * FROM jobs WHERE task_id=? OR (task_id IS NULL AND path_id=?) ORDER BY rowid",
            (task["task_id"], task["path_id"])).fetchall()
    return conn.execute("SELECT * FROM jobs WHERE task_id=? ORDER BY rowid", (task["task_id"],)).fetchall()


# ----------------------------------------------------------------------------- deadlines

def job_deadline(job: Mapping[str, Any], *, limits: Mapping[str, Any] | None = None,
                 at: str | None = None) -> dict[str, Any]:
    """By when a job that has not ended will have ended and been settled, at the latest.

    The job's own time limit, as reviewed in its capability's manifest and lowered by
    nothing but its ``tighten``; before that, for a GPU job, the longest it may wait for the
    device's lease (``gpu.queue_wait_seconds``); and after it the settle grace, in which the
    runner ends the tool's process group, writes its outcome and a look settles it. A job
    that has no runner yet (CREATED: waiting for its approval, or left by a submitter that
    stopped) is first given its start deadline, within which it starts or is ended.

    Counted from the row's own times: ``submitted_at`` once a runner exists, ``created_at``
    before. An upper bound, on purpose: read from the row alone, it does not know how long
    the job really queued.

    A function of the row and of what it is given, with no query: ``job`` needs the columns
    ``resources_json``, ``timeout_seconds``, ``submitted_at`` and ``created_at``; ``limits`` is
    a limits document (a snapshot's), the file as it is now when None; ``at`` is the instant
    ``overdue`` is judged at (ISO text), the clock's now when None. With both given the answer
    depends on nothing else, which is what a plan that is a pure function of its snapshot needs.
    """
    from . import service                                  # its constants; imported late, it imports this module

    resources = store.loads(job, "resources_json", {}) or {}
    wall = int(job["timeout_seconds"] or resources.get("wall_seconds") or 0)
    queue = limits_mod.gpu_queue_wait_seconds(limits) if resources.get("gpu") is not None else 0
    if job["submitted_at"]:
        counted_from, start, to_start = "submitted_at", job["submitted_at"], 0
    else:
        counted_from, start, to_start = "created_at", job["created_at"], service.start_deadline_seconds(limits)
    seconds = to_start + queue + wall + service.SETTLE_GRACE_SECONDS
    deadline = clock.parse(start) + timedelta(seconds=seconds)
    return {
        "deadline_at": _iso(deadline), "overdue": deadline <= (clock.parse(at) if at is not None else clock.now()),
        "basis": {"counted_from": counted_from, "at": start, "start_deadline_seconds": to_start,
                  "queue_wait_seconds": queue, "wall_seconds": wall,
                  "settle_grace_seconds": service.SETTLE_GRACE_SECONDS},
    }


def _hold(task_id: str, lease_ref: str | None, open_jobs: Iterable[tuple[Mapping[str, Any], str | None]], *,
          limits: Mapping[str, Any], at: str) -> dict[str, Any]:
    """The rule of §7.2.1 on what has been read, for :func:`live_deadline` (one task) and
    :func:`held_open` (a run): ``lease_ref`` is the task's current lease when it has not been
    released, else None; ``open_jobs`` are the task's jobs that have not ended, oldest first,
    each with the lease it belongs to (:func:`job_lease`)."""
    answer: dict[str, Any] = {"task_id": task_id, "lease_ref": lease_ref, "deadline_at": None,
                              "jobs": [], "overdue": [], "not_counted": []}
    latest = None
    for job, own in open_jobs:
        if lease_ref is None or (own is not None and own != lease_ref):
            answer["not_counted"].append({"job_id": job["job_id"], "status": job["status"], "lease_ref": own})
            continue
        found = job_deadline(job, limits=limits, at=at)
        answer["jobs"].append({"job_id": job["job_id"], "status": job["status"], "lease_ref": own, **found})
        if found["overdue"]:
            answer["overdue"].append(job["job_id"])
        elif latest is None or found["deadline_at"] > latest:
            latest = found["deadline_at"]                  # fixed-width UTC text sorts as time
    answer["deadline_at"] = latest
    return answer


def live_deadline(conn: sqlite3.Connection, task_id: str, *, limits: Mapping[str, Any] | None = None,
                  at: str | None = None) -> dict[str, Any]:
    """§7.2.1. Until when the lease a task is held under must not lapse, because a job
    submitted (or adopted) under it has not ended and is still inside its own deadline.

        {"task_id", "lease_ref",            # the lease the task is held under now; None: none, or it was released
         "deadline_at": iso | None,         # the latest deadline among those jobs; None: nothing holds the lease open
         "jobs": [{job_id, status, lease_ref, deadline_at, overdue, basis}],   # the open jobs of that lease
         "overdue": [job_id],               # of those, past their own deadline: they hold nothing open
         "not_counted": [{job_id, status, lease_ref}]}                         # open jobs that are not that lease's

    A read: it looks at no runner and settles nothing, so it may be asked inside a
    transaction. Ask it after ``JobService.reconcile(run_id)``, which is what turns a job
    that has ended into a terminal row; a job that is still open after that and past its
    deadline (a runner that is stuck, evidence that cannot be read) no longer holds its
    lease open, and is listed under ``overdue``.

    A job belongs to the lease it was submitted under or last adopted by. One with no lease
    on record (submitted without naming its task, before r5 makes the lease an input) counts
    for the task's current lease: it is this task's job, and nothing says it is not this
    attempt's. A lease that has been released is over and nothing holds it open: the open
    jobs of a task without a lease wait under ``not_counted`` for the next one to adopt them.

    ``limits`` is a limits document, the file as it is now when None; ``at`` the instant the
    deadlines are judged at, the clock's now when None. Both are read once, so every job is
    judged at one instant. :func:`held_open` is this rule for a whole run.
    """
    limits = config.limits() if limits is None else limits
    at = clock.now_iso() if at is None else at
    task = _task(conn, task_id)
    if task is None:
        return _hold(task_id, None, (), limits=limits, at=at)
    lease = _lease(conn, task["lease_id"])
    current = lease["lease_id"] if lease is not None and lease["released_at"] is None else None
    open_jobs = [(job, job_lease(conn, job)["lease_ref"]) for job in task_jobs(conn, task)
                 if job["status"] not in _TERMINAL]
    return _hold(task_id, current, open_jobs, limits=limits, at=at)


def held_open(conn: sqlite3.Connection, run_id: str, *, limits: Mapping[str, Any] | None = None,
              at: str | None = None) -> dict[str, dict[str, Any]]:
    """§7.2.1 for a run: the tasks whose current lease is held open at ``at`` by a job of it
    that has not ended and is inside its own deadline.

        {task_id: {"lease_ref", "deadline_at", "jobs": [job_id]}}

    ``deadline_at`` is the latest deadline among those jobs and ``jobs`` names them, oldest
    first. A task is absent when it has no lease, when its lease has been released, when it
    has no open job of that lease, or when every such job is past its own deadline. For each
    task in the answer it is :func:`live_deadline` of that task, at the same ``limits`` and
    ``at``: one rule (``_hold``), read here for the whole run.

    Three queries whatever the size of the run (its tasks with their unreleased leases; its
    open jobs; the adoptions of those jobs), no runner, no file. With ``limits`` and ``at``
    given it reads no clock and no configuration either, so it can be part of a snapshot that
    is read in one transaction, and of a plan that is a function of that snapshot. The
    scheduler's snapshot and core's settle both read it (SCHEDULING_DESIGN r2.2a §4.2): a
    task CLAIMED or RUNNING is in flight when its lease is unexpired at ``at`` or it is in
    this answer, and the later of the two instants is when it lapses.
    """
    limits = config.limits() if limits is None else limits
    at = clock.now_iso() if at is None else at
    tasks = conn.execute(
        "SELECT t.task_id, t.path_id, t.kind, t.lease_id FROM tasks t JOIN leases l ON l.lease_id=t.lease_id"
        " WHERE t.run_id=? AND l.released_at IS NULL ORDER BY t.rowid", (run_id,)).fetchall()
    marks = ",".join("?" * len(_TERMINAL))
    jobs = conn.execute(f"SELECT * FROM jobs WHERE run_id=? AND status NOT IN ({marks}) ORDER BY rowid",
                        (run_id, *_TERMINAL)).fetchall()
    adopted: dict[str, sqlite3.Row] = {}
    for event in conn.execute(
            "SELECT e.subject_id, e.event_id, e.payload_json FROM jobs j JOIN events e"
            " ON e.subject_kind='job' AND e.subject_id=j.job_id AND e.kind=?"
            f" WHERE j.run_id=? AND j.status NOT IN ({marks}) ORDER BY e.event_id",
            (ADOPTED_EVENT, run_id, *_TERMINAL)):
        adopted[event["subject_id"]] = event                # in event order: the latest stays
    path_tasks: dict[str, list[str]] = {}
    for task in tasks:
        if _executes_its_path(task):
            path_tasks.setdefault(task["path_id"], []).append(task["task_id"])
    of_task: dict[str, list[tuple[sqlite3.Row, str | None]]] = {}
    for job in jobs:                                         # in the order admitted, as task_jobs has them
        own = _lease_of(job, adopted.get(job["job_id"]))["lease_ref"]
        owners = [job["task_id"]] if job["task_id"] else path_tasks.get(job["path_id"], ())
        for task_id in owners:
            of_task.setdefault(task_id, []).append((job, own))
    held: dict[str, dict[str, Any]] = {}
    for task in tasks:
        found = _hold(task["task_id"], task["lease_id"], of_task.get(task["task_id"], ()), limits=limits, at=at)
        if found["deadline_at"] is not None:
            held[task["task_id"]] = {
                "lease_ref": found["lease_ref"], "deadline_at": found["deadline_at"],
                "jobs": [job["job_id"] for job in found["jobs"] if not job["overdue"]]}
    return held


def lease_seconds_for(steps: Iterable[Mapping[str, Any]], *, limits: Mapping[str, Any] | None = None) -> int:
    """§7.2.6. The lease a claim of an execute_path row asks for, in seconds: the lease every
    task is given for the worker's own work (``task_budget.lease_seconds``: reading the
    brief, submitting, reading what came back, assembling the bundle and handing it in), and
    on top of it, for each step of the path, the time by which a job of that step has ended
    and been settled at the latest (:func:`.service.job_deadline_seconds`: the wait for the
    device for a GPU step, the step's own reviewed time limit, the settle grace).

    ``steps`` are the path's steps as planning stored them (``paths.capability_refs_json``:
    ``[{capability_version, manifest_sha256, resources}]``), run one after another, one job
    each. A function of what it is given: no query and no clock, and no configuration when
    ``limits`` is given (the file as it is now when None). A step that records no time limit
    is an error (ValueError): a lease is not derived from a guess.

    A lease this long does not lean on :func:`held_open` in the ordinary case: every job of
    the path, each waiting as long as it may and running to its limit, ends inside it, with
    the task lease left over for the executor. What it does not cover is a step that is run
    a second time after its job failed; the lease is then held open by the job that runs
    (§7.2.1), which is what that rule is for.
    """
    from . import service                                  # imported late: it imports this module

    document = config.limits() if limits is None else limits
    own = (document.get("task_budget") or {}).get("lease_seconds")
    if isinstance(own, bool) or not isinstance(own, int) or own < 1:
        raise ValueError(f"task_budget.lease_seconds must be a positive whole number of seconds, got {own!r}")
    total = own
    for index, step in enumerate(steps):
        resources = step.get("resources") or {}
        wall = resources.get("wall_seconds")
        if isinstance(wall, bool) or not isinstance(wall, int) or wall < 1:
            raise ValueError(f"step {index} ({step.get('capability_version')!r}) records no time limit "
                             f"(resources.wall_seconds is {wall!r}); the lease of its path cannot be derived")
        total += service.job_deadline_seconds(resources, limits=document)
    return total


# ------------------------------------------------------------------------- earlier jobs

def earlier_jobs(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """§7.2.4. Every job recorded for a task so far, oldest first, as the row says: what the
    next attempt of the task is told before it does anything, so that it takes over what is
    running or done and submits nothing a second time. Rows as recorded; no runner is looked
    at and nothing is settled, because a brief is compiled inside the claim."""
    task = _task(conn, task_id)
    if task is None:
        return []
    listed = []
    for job in task_jobs(conn, task):
        intent = store.loads(job, "submit_intent_json", {}) or {}
        outcome = store.loads(job, "outcome_json", {}) or {}
        entry: dict[str, Any] = {
            "job_id": job["job_id"], "status": job["status"], "terminal": job["status"] in _TERMINAL,
            "capability_version": f"{job['capability_id']}@{job['capability_version']}",
            "parameters": intent.get("parameters") or {}, "input_manifest_ref": job["input_manifest_ref"],
            "lease": job_lease(conn, job), "created_at": job["created_at"],
            "submitted_at": job["submitted_at"], "finished_at": job["finished_at"],
            "observed_at": job["observed_at"] or job["created_at"],
            "failure": outcome.get("failure"),
            "outputs": [{"output_name": item.get("output_name"), "artifact_ref": item.get("artifact_ref"),
                         "kind": item.get("kind")} for item in outcome.get("outputs") or []],
        }
        if not entry["terminal"]:
            entry.update(job_deadline(job))
        listed.append(entry)
    return listed


# ----------------------------------------------------------------------------- adoption

def adopt(conn: sqlite3.Connection, job: Mapping[str, Any], *, principal: str | None, actor: str,
          via: str) -> dict[str, Any] | None:
    """§7.2.4. A look at a job by the worker that holds its task's live lease takes the job
    over, when the job belongs to another lease or to none: the event ``job.adopted``
    ``{task_id, lease_ref, from_lease_ref, attempt, principal, via, job_status}`` is written,
    and from then on the job is that lease's (for :func:`live_deadline`, for
    :func:`cancel_unadopted`, and for whoever asks whose job an ending is).

    Returns an adoption, or None when no adoption was needed: the caller holds no live lease of
    the job's task, the job is already that lease's, or the call is inside a transaction
    (where, like a settlement, nothing is decided). The job may have ended: its outputs are
    then what the new attempt takes over, and no step is run again for them. An accepted
    look by a live holder also renews its lease, including when it already owns the job.
    """
    if conn.in_transaction or not principal:
        return None
    task_id = job["task_id"]
    lease = held_lease(conn, task_id, principal) if task_id else _held_path_lease(conn, job, principal)
    if lease is None:
        return None
    with store.transaction(conn) as txn:
        # Recheck under the write lock; the holder or deadline may have moved since the
        # preliminary read. A status query never renews somebody else's lease.
        lease = held_lease(txn, lease["task_id"], principal)
        if lease is None:
            return None
        renew(txn, lease, actor=actor)
        current = job_lease(txn, job)
        if current["lease_ref"] == lease["lease_id"]:
            return None
        payload = {"task_id": lease["task_id"], "lease_ref": lease["lease_id"],
                   "from_lease_ref": current["lease_ref"], "attempt": int(lease["attempt"]),
                   "principal": principal, "via": via, "job_status": job["status"]}
        event_id = store.append_event(
            txn, kind=ADOPTED_EVENT, actor=actor, run_id=job["run_id"], subject_kind="job",
            subject_id=job["job_id"], evidence_level="EXECUTION_OBSERVED",
            reason=f"adopted under lease {lease['lease_id']} by its holder", payload=payload)
    return {"job_id": job["job_id"], "event_id": event_id, **payload}


def _held_path_lease(conn: sqlite3.Connection, job: Mapping[str, Any], principal: str) -> sqlite3.Row | None:
    """For a path's job that names no task: the live lease of a task that executes that path
    and which this principal holds, if there is exactly one."""
    if not job["path_id"]:
        return None
    held = [lease for lease in (held_lease(conn, row["task_id"], principal) for row in conn.execute(
        "SELECT task_id FROM tasks WHERE path_id=? AND kind=? AND lease_id IS NOT NULL ORDER BY rowid",
        (job["path_id"], PATH_TASK_KIND))) if lease is not None]
    return held[0] if len(held) == 1 else None


# ------------------------------------------------------------------- cancel what nobody took

def unadopted(conn: sqlite3.Connection, task_id: str, lease_ref: str) -> list[dict[str, Any]]:
    """The open jobs of a task that ``cancel_unadopted(task_id, lease_ref)`` would end, each
    with why. The one predicate of that function, kept apart so it can be read and asked
    without ending anything.

    At the bind of lease L a job is left alone when it is L's (submitted or adopted under
    it), and when it belongs to L's immediate predecessor, or to no recorded lease, and is
    still inside its own deadline: L has had no chance yet to adopt it, and its brief lists
    it. It is ended when it is past its own deadline, or when its lease is older than L's
    predecessor: an attempt has come and gone without taking it over.
    """
    task = _task(conn, task_id)
    new = _lease(conn, lease_ref)
    if task is None or new is None or new["task_id"] != task_id:
        return []
    attempts = {row["lease_id"]: int(row["attempt"]) for row in conn.execute(
        "SELECT lease_id, attempt FROM leases WHERE task_id=?", (task_id,))}
    earlier = [attempt for attempt in attempts.values() if attempt < int(new["attempt"])]
    predecessor = max(earlier) if earlier else None
    found = []
    for job in task_jobs(conn, task):
        if job["status"] in _TERMINAL:
            continue
        own = job_lease(conn, job)["lease_ref"]
        if own == lease_ref:
            continue
        deadline = job_deadline(job)
        why = None
        if deadline["overdue"]:
            why = f"it is past its own deadline ({deadline['deadline_at']}) and has not ended"
        elif own is not None and attempts.get(own, -1) < (predecessor if predecessor is not None else -1):
            why = (f"its lease {own} (attempt {attempts.get(own, '?')}) is older than the attempt before "
                   f"this one, which did not adopt it")
        if why is not None:
            found.append({"job_id": job["job_id"], "status": job["status"], "lease_ref": own, "why": why,
                          "deadline_at": deadline["deadline_at"]})
    return found


def cancel_unadopted(conn: sqlite3.Connection, task_id: str, lease_ref: str, *, actor: str) -> list[dict[str, Any]]:
    """§7.2.4. Called by core when lease ``lease_ref`` of ``task_id`` is bound: ask for every
    job :func:`unadopted` names to be ended. One answer per job: ``{job_id, lease_ref, why,
    status, note}``, or ``{…, status: None, error}`` for a cancel that raised, after which the
    rest are still asked.

    Call it after the bind has committed, outside any transaction: a cancel looks at the
    job's runner and may settle a job that has just finished. Inside one it is refused.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "jobs.cancel_unadopted was called inside an open transaction. It looks at each job's "
            "runner and may settle a job that has just finished; commit the bind first.")
    from .service import JobService

    service = JobService(conn, actor=actor)
    answers = []
    for item in unadopted(conn, task_id, lease_ref):
        base = {"job_id": item["job_id"], "lease_ref": item["lease_ref"], "why": item["why"]}
        try:
            done = service.cancel(item["job_id"], reason=f"not adopted when lease {lease_ref} of task "
                                                           f"{task_id} was bound: {item['why']}")
            answers.append({**base, "status": done["status"], "note": done.get("note")})
        except Exception as exc:  # noqa: BLE001 - reported per job, and the rest still asked
            code = getattr(exc, "code", type(exc).__name__)
            answers.append({**base, "status": None, "error": {"code": code, "message": str(exc)[:500]}})
    return answers


__all__ = ["ADOPTED_EVENT", "PATH_TASK_KIND", "adopt", "cancel_unadopted", "earlier_jobs", "held_lease",
           "held_open", "job_deadline", "job_lease", "lease_at_admission", "lease_seconds_for", "live_deadline",
           "renew", "task_jobs", "unadopted"]
