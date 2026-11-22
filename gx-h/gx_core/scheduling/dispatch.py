"""The dispatch plan: what may be claimed now, in what order, how many at once.

:func:`plan` is a pure function of a :class:`~.snapshot.Snapshot`. It answers four
questions and returns the facts behind each answer.

**How many.** ``slots = max(0, min(max_parallel_workers, caller's limit) - in_flight)``.
A task is in flight when it is CLAIMED or RUNNING under a live lease, and at no other
time. :func:`admit_claim` applies the same count when a task is claimed, so a second
coordinator or a worker that did not wait for a plan cannot exceed it.

**In what order.** READY tasks are sorted by :data:`ORDER`, a tuple of named components.
The order is data: moving, removing or adding a component changes the schedule and
touches no function. Each row says which component its reason speaks of and which one
put it ahead of the next row.

**What waits.** Every READY task is a row, or is withheld with a code of the contract and
a reason, or was handed to a running dispatch and not claimed yet (``offered``: taken,
holding a slot). A task that is not READY is never a row. A READY task an approval is
pending for (about the task itself or its path) is withheld with APPROVAL_REQUIRED, and
every READY task is while a model decision of the run is open (design r2 §2.6.1); an
approval bound to the thing the task is *about* waits for the task, and makes it go first
instead (a waiter).

**Can anything still happen.** :class:`Progress` says whether the run is moving, waiting
for a person, a job or an approval, idle, or at a dead end: open tasks none of which can
ever be accepted and nothing else to wait for. A dead end lists what each of those tasks
needs, so the run can be paused with them instead of waiting for ever.

Starvation. A lexicographic order lets work of a higher class arrive for ever in front
of a task that is waiting. The run's budget bounds that only by the size of the whole
run, so age enters the order as *bounded overtaking*: once tasks that became READY
after a task have been claimed ahead of it ``limits.max_overtakes`` times, it is
overdue, goes before everything that is not, and overdue tasks go oldest first.
:func:`admit_claim` keeps the free slots for overdue tasks ranked ahead of the claimant,
so the bound holds for claims and not only for plans: order is advice to a host until a
task is overdue, and binding after. Slots are kept only for an overdue task that has not
failed to be dispatched since it last became READY (design r2 §4.4, R20): one whose brief
does not compile, or that a gate refuses, would otherwise hold slots nobody can use.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .. import contracts
from .needs import REVIVABLE, ended_task, ended_what
from .snapshot import (
    BLOCKED_DEPENDENCY_CANCELLED,
    BLOCKED_NOT_DISPATCHABLE,
    BLOCKED_OUT_OF_ATTEMPTS,
    HOLD_REASONS,
    TASK_BLOCKED,
    TASK_HELD_STATES,
    TASK_PENDING,
    TASK_READY,
    TASK_SETTLING_STATES,
    TASK_TERMINAL,
    WAITING_NEED,
    Snapshot,
    TaskFacts,
    deadline_passed,
    instant,
)


def _code(name: str) -> str:
    contracts.error_spec(name)  # KeyError when the code leaves the contract
    return name


#: READY, and no slot is free (or the free slots are owed to tasks that waited longer).
CODE_FULL = _code("CONCURRENT_RUN")
#: ``config/limits.json`` declares a hold that stops tasks being handed out.
CODE_HOLD = _code("HOLD_ACTIVE")
#: The run is paused or has ended; the task service answers a claim the same way.
CODE_NOT_DISPATCHING = _code("ILLEGAL_TRANSITION")
CODE_DEPENDENCY = _code("DEPENDENCY_UNMET")
CODE_APPROVAL = _code("APPROVAL_REQUIRED")
CODE_LEASED = _code("LEASE_HELD")
CODE_NOT_CLAIMABLE = _code("ILLEGAL_TRANSITION")
CODE_UNKNOWN_TASK = _code("ARTIFACT_NOT_FOUND")
#: READY, and the run is not in a state the task's kind needs: its work would be refused there.
CODE_RUN_STATE = _code("PHASE_DENIED")

#: The token a waiting row's reason starts with when only settlement moves it on (a lapsed
#: lease, a PENDING task whose dependencies are all ACCEPTED). The acceptance walk and core
#: look for it; settlement runs at the start of the calls named in :data:`SETTLED_BY`.
SETTLEMENT_DUE = "settlement_due"
SETTLED_BY = "gx_task_claim, gx_task_submit or gx_run_resume"

#: Task kinds that are a review or a scenario answer: what a gate, a grounding or a
#: freeze waits for.
REVIEW_KINDS = tuple(contracts.require_label("task_kind", kind) for kind in (
    "review_evidence", "review_evaluation", "review_asset", "answer_scenarios"))
EXECUTE_KIND = contracts.require_label("task_kind", "execute_path")

#: Fields of a dispatch row that contract r5 added to r4's. ``expected_attempt`` is the claim's
#: precondition scoped to the task (design r2 §4.5); ``lease_seconds`` the lease a claim of
#: the row asks for, None where the task default applies (§7.2.6).
R5_ROW_FIELDS = ("rank", "reason", "ready_seq", "expected_attempt", "lease_seconds")

#: Approval kinds that, PENDING for the run, admit no new claim at all (design r2.2 §2.6.1:
#: the user's rule is to stop and ask while a model decision of the run is open; a refusal
#: about any task of the run is one).
RUN_HOLDING_ACTION_KINDS = tuple(contracts.require_label("approval_action_kind", kind)
                                 for kind in ("model_switch", "worker_refusal"))

#: What a task kind needs of its run before a task of it is handed out (contract r5,
#: ``labels.task_kind.needs_run``: plannable, executable or none), and the states of the run
#: machine that give each (design r2.2 §2.6.3).
NEEDS_RUN = dict(contracts.labels()["task_kind"]["needs_run"])
NO_RUN_NEED = "none"

HIGH, LOW = "high", "low"
#: Sorts after every real selection: a task whose path no wave has selected, or that
#: executes no path.
NOT_SELECTED = sys.maxsize


# ------------------------------------------------------------------- the order, as data

@dataclass(frozen=True)
class Component:
    """One component of the order: a named fact of a task and which end goes first."""

    name: str
    #: HIGH: the larger value goes first (a number). LOW: the smaller goes first.
    first: str
    fact: Callable[[TaskFacts, Snapshot], Any]
    #: the value that says nothing in particular about a task
    neutral: Any
    #: the reason a person reads when this is the first component that says something
    says: Callable[[TaskFacts, Snapshot], str]


def overdue(task: TaskFacts, snap: Snapshot) -> bool:
    """Later arrivals have been claimed ahead of it ``max_overtakes`` times or more."""
    return task.overtaken >= snap.limits.max_overtakes


def owed_a_slot(task: TaskFacts, snap: Snapshot) -> bool:
    """Overdue, and not failed to dispatch since it last became READY (design r2 §4.4, R20).

    A task whose brief does not compile, or that a gate refuses, would hold a slot nobody
    can use; it still goes first, but nothing is kept back for it.
    """
    return overdue(task, snap) and task.dispatch_failures == 0


def _overdue(task: TaskFacts, snap: Snapshot) -> int:
    return 1 if overdue(task, snap) else 0


def _waiting_since(task: TaskFacts, snap: Snapshot) -> int:
    return task.ready_seq if overdue(task, snap) else 0


def _is_review(task: TaskFacts, snap: Snapshot) -> int:
    return 1 if task.kind in REVIEW_KINDS else 0


def _waited_review(task: TaskFacts, snap: Snapshot) -> int:
    return 1 if task.kind in REVIEW_KINDS and task.waiters else 0


def _retry(task: TaskFacts, snap: Snapshot) -> int:
    return 1 if task.attempt >= 1 else 0


def run_states_for(kind: str) -> tuple[str, tuple[str, ...]] | None:
    """What a task kind needs of its run, with the run states that give it; None when it needs nothing."""
    need = NEEDS_RUN.get(kind, NO_RUN_NEED)
    if need == NO_RUN_NEED:
        return None
    return need, tuple(contracts.states()["machines"]["run"][need])


def _selection(task: TaskFacts, snap: Snapshot) -> int:
    if task.kind != EXECUTE_KIND or task.path_id is None:
        return NOT_SELECTED
    path = snap.path_by_id.get(task.path_id)
    if path is None or path.selection_seq is None:
        return NOT_SELECTED
    return path.selection_seq


def _say_selection(task: TaskFacts, snap: Snapshot) -> str:
    path = snap.path_by_id[task.path_id]
    if path.selection_rank is None:
        return f"its path {path.path_id} was selected before the others waiting to run (event {path.selection_seq})"
    of = f" of {path.selection_of}" if path.selection_of else ""
    return (f"its path {path.path_id} was selected at rank {path.selection_rank}{of} of its wave "
            f"(event {path.selection_seq}); earlier waves go first")


def _say_waiters(task: TaskFacts, snap: Snapshot) -> str:
    more = f" and {len(task.waiters) - 1} more" if len(task.waiters) > 1 else ""
    return f"a review that {task.waiters[0]}{more} is waiting for"


#: The order of the dispatch plan (design section 4 as amended by rulings 2, 4 and 5),
#: first component first. The two at the head are the guard against starvation; the
#: last makes the order total.
ORDER: tuple[Component, ...] = (
    Component("overdue", HIGH, _overdue, 0,
              lambda t, s: f"overdue: {t.overtaken} later arrivals were claimed ahead of it "
                           f"(the bound is {s.limits.max_overtakes}); the longest wait goes first"),
    Component("waiting_since", LOW, _waiting_since, 0, lambda t, s: "the longest wait goes first"),
    Component("waited_review", HIGH, _waited_review, 0, _say_waiters),
    Component("review", HIGH, _is_review, 0, lambda t, s: "a review or a scenario answer"),
    Component("retry", HIGH, _retry, 0,
              lambda t, s: f"a retry: attempt {t.attempt + 1}, {t.attempts_consumed} of {t.max_attempts} attempts "
                           "consumed; its chain is stalled"),
    Component("chain_depth", HIGH, lambda t, s: t.chain_depth, 0,
              lambda t, s: f"{t.chain_depth} step(s) into a chain that is already started"),
    Component("dependents", HIGH, lambda t, s: t.dependents, 0,
              lambda t, s: f"{t.dependents} task(s) depend on it"),
    Component("selection", LOW, _selection, NOT_SELECTED, _say_selection),
    Component("created", LOW, lambda t, s: t.created_seq, None,
              lambda t, s: "created before the others that are equal on everything else"),
    Component("task_id", LOW, lambda t, s: t.task_id, None, lambda t, s: "ties broken by task id"),
)


def key_of(task: TaskFacts, snapshot: Snapshot, order: Sequence[Component] = ORDER) -> tuple[tuple[str, Any], ...]:
    """The components of a task's key, in order, each with the task's own value."""
    return tuple((component.name, component.fact(task, snapshot)) for component in order)


def _sort_key(key: tuple[tuple[str, Any], ...], order: Sequence[Component]) -> tuple[Any, ...]:
    out = []
    for component, (_name, value) in zip(order, key):
        if component.first == HIGH:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"component {component.name!r} is ordered high-first and its value "
                                f"{value!r} is not a number")
            out.append(-value)
        elif component.first == LOW:
            out.append(value)
        else:
            raise ValueError(f"component {component.name!r} is ordered {component.first!r}; "
                             f"expected {HIGH!r} or {LOW!r}")
    return tuple(out)


def rank(tasks: Sequence[TaskFacts], snapshot: Snapshot,
         order: Sequence[Component] = ORDER) -> list[tuple[TaskFacts, tuple[tuple[str, Any], ...]]]:
    """``tasks`` best first, each with its key. The input order does not matter."""
    keyed = [(task, key_of(task, snapshot, order)) for task in tasks]
    keyed.sort(key=lambda item: _sort_key(item[1], order))
    return keyed


def separates(first: tuple[tuple[str, Any], ...], second: tuple[tuple[str, Any], ...]) -> str | None:
    """The first component on which two keys differ, or None when they are the same key."""
    for (name, left), (_name, right) in zip(first, second):
        if left != right:
            return name
    return None


def _because(task: TaskFacts, snapshot: Snapshot, order: Sequence[Component]) -> tuple[str, str]:
    for component in order:
        if component.fact(task, snapshot) != component.neutral:
            return component.name, component.says(task, snapshot)
    return "", "nothing distinguishes it"


# ---------------------------------------------------------------------------- results

@dataclass(frozen=True)
class Capacity:
    max_parallel: int
    #: what the caller said it can run at once, when it said
    limit: int | None
    in_flight: int
    #: rows that may be handed out now
    slots: int
    #: READY tasks handed to a running dispatch and not claimed: each holds a slot
    offered: int = 0

    def as_output(self) -> dict[str, Any]:
        """The ``capacity`` object of ``gx_next_actions`` (design section 4)."""
        return {"slots": self.slots, "in_flight": self.in_flight, "max_parallel": self.max_parallel}

    def as_dict(self) -> dict[str, Any]:
        return {**self.as_output(), "limit": self.limit, "offered": self.offered}


@dataclass(frozen=True)
class PlanRow:
    """One task that may be claimed now, with what placed it."""

    task_id: str
    rank: int
    kind: str
    role: str
    task_class: str | None
    model: str | None
    effort: str | None
    #: the attempt a claim would open
    attempt: int
    #: the claim's precondition (design r2 §4.5): the task's attempts started when the plan was
    #: made; a claim is refused, retryable, when the task has moved since
    expected_attempt: int
    brief_ref: str | None
    brief_sha256: str | None
    bundle_kind: str | None
    acceptance_json: str
    dependencies: tuple[str, ...]
    independent_of: str | None
    parallel_group: str
    budget_ref: str | None
    path_id: str | None
    #: the event at which it became READY: what one acceptance released goes ahead in ``ready``
    ready_seq: int
    key: tuple[tuple[str, Any], ...]
    #: the component the reason speaks of
    because: str
    #: the first component on which it differs from the task ranked next (None: nothing follows)
    ahead_by: str | None
    reason: str
    lineage_missing: bool
    #: dispatches of it that failed since it last became READY (a brief that does not compile, a gate's denial)
    dispatch_failures: int = 0
    #: the lease a claim of the row asks for; None: the task default applies
    lease_seconds: int | None = None

    def as_output(self) -> dict[str, Any]:
        """The row of ``gx_next_actions``: the contract's fields plus those of :data:`R5_ROW_FIELDS`."""
        return {
            "task_id": self.task_id, "kind": self.kind, "role": self.role, "task_class": self.task_class,
            "model": self.model, "effort": self.effort, "attempt": self.attempt,
            "brief_ref": self.brief_ref, "brief_sha256": self.brief_sha256, "bundle_kind": self.bundle_kind,
            "acceptance": json.loads(self.acceptance_json or "{}"), "dependencies": list(self.dependencies),
            "independent_of": self.independent_of, "parallel_group": self.parallel_group,
            "budget_ref": self.budget_ref, "rank": self.rank, "reason": self.reason, "ready_seq": self.ready_seq,
            "expected_attempt": self.expected_attempt, "lease_seconds": self.lease_seconds,
        }

    def facts(self) -> dict[str, Any]:
        """What placed the row: its key, the component its reason speaks of, and its age."""
        key = {name: (None if name == "selection" and value == NOT_SELECTED else value)
               for name, value in self.key}
        return {"task_id": self.task_id, "rank": self.rank, "key": key, "because": self.because,
                "ahead_by": self.ahead_by, "ready_seq": self.ready_seq, "path_id": self.path_id,
                "lineage_missing": self.lineage_missing, "dispatch_failures": self.dispatch_failures}


@dataclass(frozen=True)
class Withheld:
    """A task that is not handed out now, and what it is waiting for."""

    task_id: str
    code: str
    reason: str
    status: str = TASK_READY
    #: its place among the tasks that could run (None when it could not run at all)
    rank: int | None = None
    #: free slots that must open before it gets one (0 when capacity is not what it waits for)
    slots_needed: int = 0
    #: the approval a held task waits on (contract r5: the row carries it only then)
    approval_id: str | None = None

    def as_output(self) -> dict[str, Any]:
        """The ``blocked`` row of ``gx_next_actions``; ``approval_id`` only on a held task's row that names one."""
        row = {"task_id": self.task_id, "code": self.code, "reason": self.reason}
        if self.approval_id is not None:
            row["approval_id"] = self.approval_id
        return row

    def as_dict(self) -> dict[str, Any]:
        return {**self.as_output(), "status": self.status,
                "waiting_for": {"rank": self.rank, "slots": self.slots_needed}}


#: The states of a run's open work, in the order :func:`progress` decides them.
STOPPED, MOVING, WAITING, DEAD_END, IDLE = "STOPPED", "MOVING", "WAITING", "DEAD_END", "IDLE"


@dataclass(frozen=True)
class Need:
    """An open task that cannot be accepted without a person or a repair: why, and what would revive it."""

    task_id: str
    kind: str
    status: str
    reason: str
    revive: str
    #: what a person clears to revive it (``needs.ended_task``); None when nothing revives it
    #: (see :func:`revival`)
    need: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "kind": self.kind, "status": self.status, "reason": self.reason,
                "revive": self.revive, "need": dict(self.need) if self.need is not None else None}


def revive(task: TaskFacts, dead: dict[str, str]) -> str:
    """What would let a task that cannot move go on: the decision a person or a repair has to make.

    A BLOCKED task, and a READY one out of attempts, says what the need of an ended task says
    (``needs.ended_what``: one table for the plan, the pause and core's ``schedule.chain_ended``,
    with a line for every reason ``labels.blocked_reason`` lists as ended). No tool cancels one
    task, so the other way out is cancelling the run.
    """
    if task.status == TASK_BLOCKED:
        return ended_what(task.why_blocked)
    if task.status == TASK_READY and not task.unmet:
        if task.dispatch_failures >= task.max_attempts:
            return ended_what(BLOCKED_NOT_DISPATCHABLE)
        if task.lease_problem and not task.out_of_attempts:   # the order of _own_refusal's reasons
            return "a fix of its path's steps (a reviewed time limit for each), or cancelling the run"
        return ended_what(BLOCKED_OUT_OF_ATTEMPTS)
    blockers = [dep for dep, _state in task.unmet if dep in dead]
    if blockers:
        return f"reviving {blockers[0]}, or cancelling the run"
    return "cancelling the run: what it waits for can never be accepted"


def revival(task: TaskFacts, why: str) -> dict[str, Any] | None:
    """The need a person clears to revive a task that cannot move; None when nothing can. Pure.

    A task BLOCKED for a reason a person's revival covers (``needs.REVIVABLE``), or READY and
    out of attempts, is revived by a person (design r2.2 §2.7): its need is
    ``needs.ended_task``, the one core's ``schedule.chain_ended`` carries. Any other has none:
    §2.8.2 pauses only on what someone can clear, and quiescence with no clearable need stops
    (§2.8.3). So a task BLOCKED because a dependency was cancelled, because a person skipped
    it, for a reason nobody recorded, or for one the table does not know, has none; nor has a
    READY task whose path's steps give no lease, nor one that waits on a dead task: it waits
    on that task's need, and settle cancels it.
    """
    if task.status == TASK_BLOCKED:
        return ended_task(task.task_id, task.why_blocked, why) if task.why_blocked in REVIVABLE else None
    if task.status == TASK_READY and not task.unmet and task.out_of_attempts:
        return ended_task(task.task_id, BLOCKED_OUT_OF_ATTEMPTS, why)
    if task.status == TASK_READY and not task.unmet and task.dispatch_failures >= task.max_attempts:
        return ended_task(task.task_id, BLOCKED_NOT_DISPATCHABLE, why)
    return None


@dataclass(frozen=True)
class Progress:
    """Whether anything can still happen in the run without a person, and what."""

    state: str
    reason: str
    #: tasks under a live lease
    in_flight: tuple[str, ...] = ()
    #: READY tasks a claim would accept but for capacity
    claimable: tuple[str, ...] = ()
    #: tasks the next call that looks moves on by itself (a lapsed lease, a refusal, a
    #: submission being validated, a PENDING task whose dependencies are all ACCEPTED)
    settling: tuple[str, ...] = ()
    #: PENDING tasks whose dependencies can all still be accepted
    pending: tuple[str, ...] = ()
    #: READY tasks withheld for a pending approval, and BLOCKED tasks held for one
    awaiting_approval: tuple[str, ...] = ()
    #: BLOCKED tasks that wait on a need their failure's route named (``waiting_need``)
    waiting_need: tuple[str, ...] = ()
    #: open jobs still inside their deadline at the snapshot instant (or with none on record): waited for
    jobs: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    #: open tasks that can never be accepted
    needs: tuple[Need, ...] = ()
    #: open jobs past their deadline at the snapshot instant: the row has not moved because
    #: nobody looked, and the next call that settles the run reconciles them
    jobs_due: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason, "in_flight": list(self.in_flight),
                "claimable": list(self.claimable), "settling": list(self.settling), "pending": list(self.pending),
                "awaiting_approval": list(self.awaiting_approval), "waiting_need": list(self.waiting_need),
                "jobs": list(self.jobs), "jobs_due": list(self.jobs_due), "approvals": list(self.approvals),
                "needs": [need.as_dict() for need in self.needs]}


@dataclass(frozen=True)
class Plan:
    run_id: str
    stage: str | None
    #: False when the run is paused or ended, or a hold is active: nothing is handed out
    dispatching: bool
    capacity: Capacity
    rows: tuple[PlanRow, ...]
    #: READY tasks not offered, each with a code and a reason
    withheld: tuple[Withheld, ...]
    #: open tasks that are not READY, each with a code and a reason
    waiting: tuple[Withheld, ...]
    #: READY tasks handed to a running dispatch and not claimed yet: taken, each holding a slot
    offered: tuple[str, ...]
    #: the acceptance whose releases go first after the overdue (the ``ready`` of a submit)
    released_after: int | None
    #: READY tasks in the snapshot: ``len(rows) + len(withheld) + len(offered)``
    ready: int
    progress: Progress
    #: tasks whose lineage should be on record and is not (a reactor defect)
    lineage_missing: tuple[str, ...]
    as_of_event_id: int
    as_of: str
    #: the earliest instant at which a lease in flight lapses, and at which an open job
    #: still inside its deadline passes it (None: there is none). Until then settlement has
    #: nothing new to do for them; a coordinator with nothing to launch waits until the earlier
    earliest_lease_expiry: str | None = None
    earliest_job_deadline: str | None = None

    def as_output(self, *, contract: str = "r5") -> dict[str, Any]:
        """The answer of ``gx_next_actions``, less ``revision``, which is the run's and core's.

        ``contract="r5"``: the contract's fields and those queued for r5 (rows' ``rank``,
        ``reason``, ``ready_seq``, ``expected_attempt``, ``lease_seconds``; a held task's
        blocked row's ``approval_id``; ``stage``, ``capacity``, ``as_of_event_id``,
        ``earliest_lease_expiry``, ``earliest_job_deadline``, ``jobs_due``, ``offered``).
        ``contract="r4"``: the frozen r4 fields only, so nothing undeclared is answered before
        the freeze. Everything else a decision used is in :meth:`facts`.
        """
        if contract not in ("r4", "r5"):
            raise ValueError(f"contract is 'r4' or 'r5', got {contract!r}")
        rows = [row.as_output() for row in self.rows]
        if contract == "r4":
            rows = [{key: value for key, value in row.items() if key not in R5_ROW_FIELDS} for row in rows]
            return {"next_actions": rows, "blocked": [
                {key: value for key, value in item.as_output().items() if key != "approval_id"}
                for item in self.withheld + self.waiting]}
        return {
            "next_actions": rows, "blocked": [item.as_output() for item in self.withheld + self.waiting],
            "offered": list(self.offered), "stage": self.stage, "capacity": self.capacity.as_output(),
            "as_of_event_id": self.as_of_event_id, "earliest_lease_expiry": self.earliest_lease_expiry,
            "earliest_job_deadline": self.earliest_job_deadline, "jobs_due": list(self.progress.jobs_due),
        }

    def facts(self) -> dict[str, Any]:
        """Everything the plan was decided on and found, for an event or a reader."""
        return {
            "run_id": self.run_id, "stage": self.stage, "dispatching": self.dispatching,
            "capacity": self.capacity.as_dict(), "rows": [row.facts() for row in self.rows],
            "withheld": [item.as_dict() for item in self.withheld],
            "waiting": [item.as_dict() for item in self.waiting], "offered": list(self.offered),
            "released_after": self.released_after, "ready": self.ready, "progress": self.progress.as_dict(),
            "lineage_missing": list(self.lineage_missing), "as_of_event_id": self.as_of_event_id,
            "as_of": self.as_of, "earliest_lease_expiry": self.earliest_lease_expiry,
            "earliest_job_deadline": self.earliest_job_deadline,
        }


@dataclass(frozen=True)
class Admission:
    """Whether one task may be claimed now, as far as capacity, age and approvals decide."""

    task_id: str
    admitted: bool
    #: the contract's code for the refusal, None when admitted
    code: str | None
    reason: str
    capacity: Capacity
    #: the first fact: the run hands out work and a slot is free
    capacity_ok: bool
    #: the second fact: overdue tasks ranked ahead of this one, which the free slots are kept for
    owed_to: tuple[str, ...] = ()
    rank: int | None = None
    #: the rules this answer was decided under
    rules: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "admitted": self.admitted, "code": self.code, "reason": self.reason,
                "capacity": self.capacity.as_dict(), "capacity_ok": self.capacity_ok,
                "owed_to": list(self.owed_to), "rank": self.rank, "rules": list(self.rules)}


# -------------------------------------------------------------------------- the rules

def _ceiling(snapshot: Snapshot, limit: int | None) -> tuple[int, int | None]:
    parallel = snapshot.limits.max_parallel_workers
    if limit is None:
        return parallel, None
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit is a whole number of workers, got {limit!r}")
    asked = max(0, limit)
    return min(parallel, asked), asked


def _run_refusal(snapshot: Snapshot, refusal: Any = None) -> tuple[str, str] | None:
    """Why nothing at all is handed out, when that is so.

    The store's own reasons first: the run is paused or ended, or a hold stands. Then
    ``refusal``, a reason the caller found that is no fact of the store: ``(code, reason)``
    with a code of the contract. Core passes it when the run's pins no longer hold (design
    r2.2 §2.4.5: a pin is checked against files, so the snapshot cannot hold it).
    """
    if not snapshot.dispatching:
        return CODE_NOT_DISPATCHING, (f"run {snapshot.run.run_id} is {snapshot.run.status}; "
                                      "nothing is handed out until it continues")
    if snapshot.limits.holds:
        return CODE_HOLD, ("config/limits.json declares an active hold ("
                           + ", ".join(snapshot.limits.holds) + "); nothing is handed out while it stands")
    if refusal is None:
        return None
    if isinstance(refusal, str) or len(refusal) != 2:
        raise TypeError(f"refusal is a pair (code, reason), got {refusal!r}")
    code, reason = refusal
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a refusal says why")
    return _code(code), reason


def run_holding_approvals(snapshot: Snapshot) -> tuple[str, ...]:
    """Pending approvals of the run that admit no new claim at all (design r2 §2.6.1).

    While a model decision of the run is open (a ``model_switch`` approval, whatever its
    subject: the run, a held submission, a refused brief), the user's rule is to stop and
    ask. Tasks already claimed may still hand in.
    """
    return tuple(sorted(approval.approval_id for approval in snapshot.approvals
                        if approval.action_kind in RUN_HOLDING_ACTION_KINDS))


def blocking_approvals(task: TaskFacts, snapshot: Snapshot) -> tuple[str, ...]:
    """Pending approvals this task waits for: about the task itself or its path, or holding the whole run.

    Every approval in a snapshot is PENDING (``load`` reads no other). One bound to what
    the task is *about* (its subject) is the other way round: it waits for the task, the
    way an asset activation waits for its review, so it is a waiter and never a reason to
    hold the task back, unless it holds the whole run.
    """
    found = set(run_holding_approvals(snapshot))
    for approval in snapshot.approvals:
        about = (approval.subject_kind, approval.subject_id)
        if task.subject_kind and about == (task.subject_kind, task.subject_id):
            continue
        if about == ("task", task.task_id) or (task.path_id is not None and about == ("path", task.path_id)):
            found.add(approval.approval_id)
    return tuple(sorted(found))


def _out_of_attempts(task: TaskFacts) -> str:
    """Why a task has no attempt left: what ``max_attempts`` bounds, and how many leases it has had."""
    return f"out of attempts ({task.attempts_consumed} of {task.max_attempts} consumed; {task.attempt} started)"


def _own_refusal(task: TaskFacts, snapshot: Snapshot, *, approvals: bool = True) -> tuple[str, str] | None:
    """Why a READY task could not be claimed whatever the capacity. A claim would be refused too."""
    if task.unmet:
        return CODE_DEPENDENCY, "waiting for " + ", ".join(
            f"{dep} ({state})" for dep, state in task.unmet) + " to be ACCEPTED"
    if task.out_of_attempts:
        return CODE_NOT_CLAIMABLE, _out_of_attempts(task)
    if task.dispatch_failures >= task.max_attempts:
        return CODE_NOT_CLAIMABLE, (f"dispatch failed {task.dispatch_failures} times since READY; "
                                    f"the bound is {task.max_attempts}; settlement ends this chain")
    if task.lease_problem:
        return CODE_NOT_CLAIMABLE, task.lease_problem
    wanted = run_states_for(task.kind)
    if wanted is not None and snapshot.run.status not in wanted[1]:
        return CODE_RUN_STATE, (f"the run is {snapshot.run.status}; a {task.kind} task needs a run that is "
                                f"{wanted[0]} ({', '.join(wanted[1])})")
    if approvals:
        pending = blocking_approvals(task, snapshot)
        if pending:
            by_id = {approval.approval_id: approval for approval in snapshot.approvals}
            holding = set(run_holding_approvals(snapshot)) & set(pending)
            return CODE_APPROVAL, "waiting for " + ", ".join(
                f"approval {item} ({by_id[item].action_kind} on {by_id[item].subject_kind} "
                f"{by_id[item].subject_id})" for item in pending) + " to be decided" + (
                "; no new claim while a model decision of the run is open" if holding else "")
    return None


def _effort(task: TaskFacts, snapshot: Snapshot) -> str | None:
    default = dict(snapshot.run.default_efforts).get(task.task_class or "")
    if default in task.efforts or not task.efforts:
        return default
    return task.efforts[0]


def _candidates(snapshot: Snapshot, order: Sequence[Component], *, approvals: bool = True):
    """READY tasks that could be claimed, best first, and those that could not with why."""
    eligible, refused = [], []
    for task in snapshot.ready:
        own = _own_refusal(task, snapshot, approvals=approvals)
        if own is None:
            eligible.append(task)
        else:
            refused.append((task, own))
    refused.sort(key=lambda item: item[0].task_id)
    return rank(eligible, snapshot, order), refused


def _refusal_codes(snapshot: Snapshot) -> dict[str, str]:
    """READY tasks a claim would refuse whatever the capacity, by the code it would refuse with."""
    codes = {}
    for task in snapshot.ready:
        own = _own_refusal(task, snapshot)
        if own is not None:
            codes[task.task_id] = own[0]
    return codes


def _not_ready(task: TaskFacts, dead: dict[str, str]) -> Withheld:
    """An open task that is not READY, with the code a claim of it would be refused with.

    The two states settlement moves on by itself say :data:`SETTLEMENT_DUE` first: the plan is
    a query and settles nothing (the lead's ruling S2), so they wait for the next call that
    settles the run.
    """
    if task.status == TASK_PENDING:
        if not task.unmet:
            return Withheld(task.task_id, CODE_NOT_CLAIMABLE,
                            f"{SETTLEMENT_DUE}: every dependency is ACCEPTED; it becomes READY when the run is next "
                            f"settled ({SETTLED_BY})", status=task.status)
        if task.task_id in dead:
            why = f"can never become READY: {dead[task.task_id]}"
        else:
            why = "waiting for " + ", ".join(f"{dep} ({state})" for dep, state in task.unmet) + " to be ACCEPTED"
        return Withheld(task.task_id, CODE_DEPENDENCY, why, status=task.status)
    if task.in_flight:
        return Withheld(task.task_id, CODE_LEASED,
                        f"{task.status} under a lease held by {task.lease_owner} until {task.lease_expires_at}",
                        status=task.status)
    if task.status in TASK_HELD_STATES:
        return Withheld(task.task_id, CODE_NOT_CLAIMABLE,
                        f"{SETTLEMENT_DUE}: {task.status} and its lease has lapsed; it is settled at the next "
                        f"{SETTLED_BY}", status=task.status)
    if task.status == TASK_BLOCKED:
        why = task.why_blocked
        if why in HOLD_REASONS:
            named = f"approval {task.hold_approval_id}" if task.hold_approval_id else "the approval about it"
            return Withheld(task.task_id, CODE_APPROVAL, f"held ({why}) until {named} is decided", status=task.status,
                            approval_id=task.hold_approval_id)
        if why == WAITING_NEED:
            return Withheld(task.task_id, CODE_NOT_CLAIMABLE,
                            "BLOCKED: it waits on a need its failure's route named, and returns to READY when that "
                            "need is cleared", status=task.status)
        return Withheld(task.task_id, CODE_DEPENDENCY if why == BLOCKED_DEPENDENCY_CANCELLED else CODE_NOT_CLAIMABLE,
                        f"BLOCKED: {dead.get(task.task_id) or why or 'no reason on record'}",
                        status=task.status)
    return Withheld(task.task_id, CODE_NOT_CLAIMABLE, f"task is {task.status}; it is claimed from READY",
                    status=task.status)


def dead_tasks(snapshot: Snapshot, refused: dict[str, str] | None = None) -> dict[str, str]:
    """Open tasks that can never be ACCEPTED, each with why. Pure.

    A task is alive when something can still move it: it is READY and claimable or
    withheld only for an approval or for the run's state, it is in a worker's hands or being
    settled, it is held for an approval, or it waits on a need its failure's route named; a
    PENDING task is alive when every dependency it waits for is. Everything open that is not
    alive is dead: a task out of attempts, one BLOCKED with its chain ended (contract r5,
    ``labels.blocked_reason`` ended) or for a reason nobody recorded, and whatever waits on
    those or on each other in a cycle.
    """
    if refused is None:
        refused = _refusal_codes(snapshot)
    alive: set[str] = set()
    deferred: list[TaskFacts] = []
    dead: dict[str, str] = {}
    for task in snapshot.tasks:
        if task.status in TASK_TERMINAL:
            continue
        if task.status == TASK_READY:
            code = refused.get(task.task_id)
            if code is None or code in (CODE_APPROVAL, CODE_RUN_STATE):
                alive.add(task.task_id)
            elif code == CODE_DEPENDENCY:
                deferred.append(task)
            else:
                own = _own_refusal(task, snapshot, approvals=False)
                dead[task.task_id] = own[1] if own is not None else _out_of_attempts(task)
        elif task.status in TASK_HELD_STATES or task.status in TASK_SETTLING_STATES:
            alive.add(task.task_id)
        elif task.status == TASK_BLOCKED:
            why = task.why_blocked
            if why in HOLD_REASONS or why == WAITING_NEED:
                alive.add(task.task_id)
            else:
                dead[task.task_id] = (_out_of_attempts(task) if why == BLOCKED_OUT_OF_ATTEMPTS
                                      else f"blocked: {why or 'no reason on record'}")
        else:  # PENDING
            deferred.append(task)
    changed = True
    while changed:
        changed = False
        for task in deferred:
            if task.task_id in alive:
                continue
            if all(dep in alive for dep, _state in task.unmet):
                alive.add(task.task_id)
                changed = True
    status = {task.task_id: task.status for task in snapshot.tasks}
    for task in deferred:
        if task.task_id in alive:
            continue
        culprit = next(dep for dep, _state in task.unmet if dep not in alive)
        dead[task.task_id] = f"it depends on {culprit} ({status.get(culprit, 'missing')}), which can never be ACCEPTED"
    return dead


def progress(snapshot: Snapshot, refused: dict[str, str] | None = None, *, refusal: Any = None) -> Progress:
    """Whether the run's open work can still move, wait, or is at a dead end. Pure.

    ``refusal``: a run-level refusal the caller found (see :func:`_run_refusal`).
    """
    if refused is None:
        refused = _refusal_codes(snapshot)
    dead = dead_tasks(snapshot, refused)
    in_flight, claimable, settling, pending, awaiting, on_need = [], [], [], [], [], []
    for task in snapshot.tasks:
        if task.status in TASK_TERMINAL or task.task_id in dead:
            continue
        if task.in_flight:
            in_flight.append(task.task_id)
        elif task.status == TASK_READY:
            code = refused.get(task.task_id)
            (awaiting if code == CODE_APPROVAL else pending if code in (CODE_DEPENDENCY, CODE_RUN_STATE)
             else claimable).append(task.task_id)
        elif task.status in TASK_HELD_STATES or task.status in TASK_SETTLING_STATES:
            settling.append(task.task_id)
        elif task.status == TASK_BLOCKED:
            (on_need if task.why_blocked == WAITING_NEED else awaiting).append(task.task_id)
        elif not task.unmet:
            settling.append(task.task_id)
        else:
            pending.append(task.task_id)
    by_id = snapshot.by_id
    needs = tuple(Need(task_id, by_id[task_id].kind, by_id[task_id].status, why, revive(by_id[task_id], dead),
                       revival(by_id[task_id], why))
                  for task_id, why in sorted(dead.items()))
    due = {job.job_id for job in snapshot.jobs
           if job.deadline_at is not None and deadline_passed(job.deadline_at, snapshot.as_of)}
    jobs = tuple(sorted(job.job_id for job in snapshot.jobs if job.job_id not in due))
    approvals = tuple(sorted(approval.approval_id for approval in snapshot.approvals))
    parts = dict(in_flight=tuple(sorted(in_flight)), claimable=tuple(sorted(claimable)),
                 settling=tuple(sorted(settling)), pending=tuple(sorted(pending)),
                 awaiting_approval=tuple(sorted(awaiting)), jobs=jobs, approvals=approvals, needs=needs,
                 jobs_due=tuple(sorted(due)), waiting_need=tuple(sorted(on_need)))
    stopped = _run_refusal(snapshot, refusal)
    if stopped is not None:
        return Progress(STOPPED, stopped[1], **parts)
    if snapshot.limits.max_parallel_workers == 0 and claimable:
        return Progress(STOPPED, "run_budget.max_parallel_workers is 0: nothing can be claimed", **parts)
    if in_flight or claimable or settling or due:
        return Progress(MOVING, f"{len(in_flight)} in flight, {len(claimable)} claimable, "
                                f"{len(settling)} being settled"
                        + (f", {len(due)} job(s) past their deadline, settled at the next {SETTLED_BY}"
                           if due else ""), **parts)
    if awaiting or jobs or approvals or pending or on_need:
        return Progress(WAITING, f"waiting for {len(approvals)} approval(s), {len(jobs)} job(s); "
                                 f"{len(awaiting)} task(s) held for an approval, {len(pending)} pending"
                        + (f", {len(on_need)} waiting on a need" if on_need else ""), **parts)
    if needs:
        return Progress(DEAD_END, f"{len(needs)} open task(s) can never be accepted and nothing else can move: "
                                  + "; ".join(f"{need.task_id}: {need.reason}" for need in needs[:5]), **parts)
    return Progress(IDLE, "no open task, no job and no approval", **parts)


def plan(snapshot: Snapshot, *, limit: int | None = None, released_after: int | None = None,
         order: Sequence[Component] = ORDER, refusal: Any = None) -> Plan:
    """What may be claimed now, ranked, and what has to wait and why.

    ``limit`` is how many workers the caller can run at once; it can lower the run's
    ceiling and never raise it. Tasks handed to a running dispatch and not claimed yet
    (``snapshot.offered``) are taken: they are not offered again and each holds a slot.

    ``refusal`` is ``(code, reason)`` for something the caller found that stops every
    hand-out and that the store does not hold (the run's pins, design r2.2 §2.4.5): every
    READY row is then listed with that code and reason, and nothing is offered.

    ``released_after`` is the event of an acceptance, for the ``ready`` answer of
    ``gx_task_submit`` (design r2 §4.7, R27): overdue tasks go first, then the tasks that
    became READY after the event, then those released earlier, each part in rank order,
    cut to the free slots. It reorders and never filters. The same snapshot gives the same
    plan, row for row, whatever order its tasks are listed in.
    """
    if released_after is not None and (isinstance(released_after, bool) or not isinstance(released_after, int)):
        raise TypeError(f"released_after is an event id, got {released_after!r}")
    ceiling, asked = _ceiling(snapshot, limit)
    in_flight = len(snapshot.in_flight)
    stopped = _run_refusal(snapshot, refusal)
    ranked, refused = _candidates(snapshot, order)
    handed_out = set(snapshot.offered)
    offered = [task.task_id for task, _key in ranked if task.task_id in handed_out]
    free = 0 if stopped is not None else max(0, ceiling - in_flight - len(offered))
    capacity = Capacity(max_parallel=snapshot.limits.max_parallel_workers, limit=asked,
                        in_flight=in_flight, slots=free, offered=len(offered))
    group = f"plan:{snapshot.run.run_id}:{snapshot.as_of_event_id}"

    positions = {task.task_id: index + 1 for index, (task, _key) in enumerate(ranked)}
    candidates = [(task, key) for task, key in ranked if task.task_id not in handed_out]
    if released_after is not None:
        def tier(task: TaskFacts) -> int:
            if overdue(task, snapshot):
                return 0
            return 1 if task.ready_seq > released_after else 2
        candidates.sort(key=lambda item: (tier(item[0]), positions[item[0].task_id]))

    chosen: list[tuple[int, TaskFacts, tuple]] = []
    withheld: list[Withheld] = []
    for index, (task, key) in enumerate(candidates):
        position = positions[task.task_id]
        if stopped is not None:
            withheld.append(Withheld(task.task_id, stopped[0], stopped[1], rank=position))
        elif index < free:
            chosen.append((position, task, key))
        else:
            bound = f", the caller's limit is {asked}" if asked is not None else ""
            taken = f", {len(offered)} handed out and not claimed" if offered else ""
            needed = index + 1 - free
            withheld.append(Withheld(
                task.task_id, CODE_FULL,
                f"ready at rank {position} of {len(ranked)}; {in_flight} in flight of {ceiling}{bound}{taken}; "
                f"{needed} slot(s) must free before it gets one", rank=position, slots_needed=needed))

    rows = []
    for position, task, key in chosen:
        because, said = _because(task, snapshot, order)
        following = ranked[position][1] if position < len(ranked) else None
        reason = f"rank {position} of {len(ranked)}: {said}"
        if released_after is not None and not overdue(task, snapshot):
            reason += ("; released by this acceptance" if task.ready_seq > released_after
                       else "; released before this acceptance")
        if task.lineage_missing:
            reason += "; its lineage is not on record, so it is ranked as a new chain (a reactor defect)"
        if task.dispatch_failures:
            reason += (f"; {task.dispatch_failures} dispatch(es) of it failed since it became READY, so no slot is "
                       "kept for it")
        rows.append(PlanRow(
            task_id=task.task_id, rank=position, kind=task.kind, role=task.role, task_class=task.task_class,
            model=task.model, effort=_effort(task, snapshot), attempt=task.attempt + 1,
            expected_attempt=task.attempt,
            brief_ref=task.brief_ref, brief_sha256=task.brief_sha256, bundle_kind=task.bundle_kind,
            acceptance_json=task.acceptance_json, dependencies=task.dependencies,
            independent_of=task.independent_of, parallel_group=group, budget_ref=task.budget_ref,
            path_id=task.path_id, ready_seq=task.ready_seq, key=key, because=because,
            ahead_by=separates(key, following) if following is not None else None,
            reason=reason, lineage_missing=task.lineage_missing, dispatch_failures=task.dispatch_failures,
            lease_seconds=task.lease_seconds,
        ))
    for task, (code, why) in refused:
        withheld.append(Withheld(task.task_id, code, why))
    refused_codes = {task.task_id: code for task, (code, _why) in refused}
    state = progress(snapshot, refused_codes, refusal=refusal)
    dead = {need.task_id: need.reason for need in state.needs}
    waiting = tuple(_not_ready(task, dead) for task in sorted(snapshot.tasks, key=lambda item: item.task_id)
                    if task.status not in TASK_TERMINAL and task.status != TASK_READY)
    return Plan(
        run_id=snapshot.run.run_id, stage=snapshot.run.stage, dispatching=stopped is None,
        capacity=capacity, rows=tuple(rows), withheld=tuple(withheld), waiting=waiting, offered=tuple(offered),
        released_after=released_after, ready=len(snapshot.ready), progress=state,
        lineage_missing=snapshot.lineage_missing, as_of_event_id=snapshot.as_of_event_id, as_of=snapshot.as_of,
        earliest_lease_expiry=_earliest(task.lease_expires_at for task in snapshot.in_flight),
        earliest_job_deadline=_earliest(job.deadline_at for job in snapshot.jobs if job.job_id in state.jobs),
    )


def _earliest(instants: Any) -> str | None:
    """The earliest of some instants as written, None when there is none (one not on record is skipped)."""
    given = [text for text in instants if text]
    return min(given, key=instant) if given else None


#: The rules a claim is admitted by, in the order they are applied. ``capacity`` is the
#: plan's count. ``overdue_first`` keeps free slots for overdue tasks ranked ahead of the
#: claimant; without it the starvation bound holds for plans and not for claims.
#: ``approval`` refuses a task the plan withholds for a pending approval about it or its path.
RULE_CAPACITY = "capacity"
RULE_OVERDUE_FIRST = "overdue_first"
RULE_APPROVAL = "approval"
ADMISSION: tuple[str, ...] = (RULE_CAPACITY, RULE_OVERDUE_FIRST, RULE_APPROVAL)


def admit_claim(snapshot: Snapshot, task_id: str, *, order: Sequence[Component] = ORDER,
                rules: Sequence[str] = ADMISSION, refusal: Any = None) -> Admission:
    """Whether ``task_id`` may be claimed now. The caller's own checks still apply.

    Both facts are returned whatever the answer: whether a slot is free, and which
    overdue tasks ranked ahead of this one the free slots are kept for (only those with no
    dispatch failure since they last became READY, design r2 §4.4). A slot held by a task
    handed to a running dispatch is not free to anybody else. The answer is about
    capacity, age and approvals only: the role, the task's precondition and the
    independence of the claimant are the task service's. Call it inside the transaction
    that issues the lease, on a snapshot loaded in that transaction, so two claims cannot
    both be given the last slot. ``refusal`` is a run-level refusal the caller found, as
    for :func:`plan`: no claim is admitted while it stands.
    """
    unknown = sorted(set(rules) - set(ADMISSION))
    if unknown:
        raise ValueError(f"unknown admission rule(s) {unknown}")
    applied = tuple(rule for rule in ADMISSION if rule in rules)
    parallel = snapshot.limits.max_parallel_workers
    in_flight = len(snapshot.in_flight)
    stopped = _run_refusal(snapshot, refusal)
    ranked, _refused = _candidates(snapshot, order, approvals=RULE_APPROVAL in applied)
    # a task handed to a dispatch holds a slot only while it could be claimed; its own claim takes it
    handed_out = set(snapshot.offered) & {item.task_id for item, _key in ranked}
    others_offered = len(handed_out - {task_id})
    free = 0 if stopped is not None else max(0, parallel - in_flight - others_offered)
    capacity = Capacity(max_parallel=parallel, limit=None, in_flight=in_flight, slots=free, offered=others_offered)
    capacity_ok = free > 0

    def answer(admitted: bool, code: str | None, why: str, **more: Any) -> Admission:
        return Admission(task_id=task_id, admitted=admitted, code=code, reason=why, capacity=capacity,
                         capacity_ok=capacity_ok, rules=applied, **more)

    task = snapshot.by_id.get(task_id)
    if task is None:
        return answer(False, CODE_UNKNOWN_TASK, f"no task {task_id!r} in run {snapshot.run.run_id}")
    if stopped is not None:
        return answer(False, *stopped)
    if task.status == TASK_PENDING:
        return answer(False, CODE_DEPENDENCY, f"task {task_id} is {task.status}: " + (
            "waiting for " + ", ".join(f"{dep} ({state})" for dep, state in task.unmet) + " to be ACCEPTED"
            if task.unmet else "its dependencies are ACCEPTED and it is not settled to READY yet"))
    if task.in_flight:
        return answer(False, CODE_LEASED, f"task {task_id} is {task.status} under a lease held by "
                                          f"{task.lease_owner} until {task.lease_expires_at}")
    if task.status != TASK_READY:
        held = " (its lease has lapsed and is not settled yet)" if task.status in TASK_HELD_STATES else ""
        return answer(False, CODE_NOT_CLAIMABLE,
                      f"task {task_id} is {task.status}{held}; a task is claimed from READY")
    own = _own_refusal(task, snapshot, approvals=RULE_APPROVAL in applied)
    if own is not None:
        return answer(False, *own)

    ids = [item.task_id for item, _key in ranked]
    position = ids.index(task_id) + 1
    # a handed-out task already holds its slot: it is not owed a second one
    owed = tuple(item.task_id for item, _key in ranked[:position - 1]
                 if owed_a_slot(item, snapshot) and item.task_id not in handed_out)
    if RULE_CAPACITY in applied and not capacity_ok:
        taken = f" and {others_offered} handed out to a dispatch" if others_offered else ""
        return answer(False, CODE_FULL, f"the run is full: {in_flight} in flight{taken} of {parallel}; "
                                        "return NOT_CLAIMED: the next plan lists it",
                      rank=position, owed_to=owed)
    if RULE_OVERDUE_FIRST in applied and owed and len(owed) >= free:
        return answer(
            False, CODE_FULL,
            f"{free} slot(s) are free and are kept for {len(owed)} task(s) that later arrivals were claimed "
            f"ahead of {snapshot.limits.max_overtakes} times or more: " + ", ".join(owed[:5])
            + (" ..." if len(owed) > 5 else ""), rank=position, owed_to=owed)
    return answer(True, None, f"{free} of {parallel} slot(s) free; ranked {position} of {len(ranked)} ready"
                  + (f"; {len(owed)} overdue task(s) ranked ahead" if owed else ""),
                  rank=position, owed_to=owed)


__all__ = [
    "ADMISSION", "Admission", "CODE_APPROVAL", "CODE_FULL", "Capacity", "Component", "DEAD_END",
    "EXECUTE_KIND", "HIGH", "IDLE", "LOW", "MOVING", "NOT_SELECTED", "Need", "ORDER", "Plan", "PlanRow",
    "Progress", "R5_ROW_FIELDS", "REVIEW_KINDS", "RULE_APPROVAL", "RULE_CAPACITY", "RULE_OVERDUE_FIRST",
    "RUN_HOLDING_ACTION_KINDS", "SETTLED_BY", "SETTLEMENT_DUE", "STOPPED", "WAITING", "Withheld", "admit_claim", "blocking_approvals",
    "dead_tasks", "key_of", "overdue", "owed_a_slot", "plan", "progress", "rank", "revival", "revive",
    "run_holding_approvals", "separates",
]
