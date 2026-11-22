"""The facts a scheduling decision may use, and the one function that reads them.

Everything else in this package is a pure function of a :class:`Snapshot`: it reads no
store, no clock and no file, so a decision can be tested without a store and replayed
from the facts it was made on. :func:`load` is the only function that touches the store.
It writes nothing, reads no artifact and runs a fixed number of queries whatever the
size of the run.

Four things a reader should know about what is in here.

**Time is a fact, not a reading.** Whether a lease is live is a comparison with an
instant. ``load`` is given that instant by its caller (the one who just settled what
time had decided) and records it as ``Snapshot.as_of``; nothing here asks what time it
is. A CLAIMED or RUNNING task whose lease is past that instant and not yet settled is
neither in flight nor READY.

**Order is positions in the event log.** ``ready_seq`` is the id of the event at which a
task last became READY, ``created_seq`` the id of the event that created it, and
``overtaken`` counts the claims, since it last became READY, of tasks that became READY
after it. ``dispatch_failures`` counts the ``task.dispatch_failed`` events since then. A
path's place among the selected is the id of its latest SELECTED transition (design r2
§4.3). All are read from ``events``; none is a duration. ``as_of_event_id`` is the last
event of the run the snapshot includes, so two snapshots of one state carry the same number.

**Lineage is on record or it is reported missing.** A task's place along its chain is
the longest chain of predecessors over its dependencies, the task or tasks it must be
independent of, and the ``follows`` and ``independent_of`` rows of ``dependencies`` (design
r2.2 §2.3: lineage that does not gate READY; a job a follow-up names counts as that job's
task). A task that should name
the task it follows and does not is marked ``lineage_missing`` (see :func:`lineage_gaps`);
it is never ranked as a new chain silently.

**Every number is a line of ``config/limits.json``.** :meth:`Limits.from_config` names
the line each field comes from and refuses a file that lacks one. Nothing is defaulted.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from functools import cached_property
from typing import Any, Iterable, Mapping, Sequence

from .. import config, contracts
from ..budgets.lines import line as read_line
from ..errors import ArtifactNotFound

# ------------------------------------------------------------------------- vocabulary
#
# States are the contract's. A literal below is checked against ``contracts/states.json``
# when the module is imported, so a state that leaves the contract stops the import
# instead of quietly matching nothing.


def _state(machine: str, name: str) -> str:
    if name not in contracts.states()["machines"][machine]["states"]:  # pragma: no cover - contract drift
        raise RuntimeError(f"{machine} state {name!r} left the contract")
    return name


def _terminal(machine: str) -> frozenset[str]:
    return frozenset(contracts.states()["machines"][machine]["terminal"])


TASK_PENDING = _state("task", "PENDING")
TASK_READY = _state("task", "READY")
TASK_CLAIMED = _state("task", "CLAIMED")
TASK_RUNNING = _state("task", "RUNNING")
TASK_SUBMITTED = _state("task", "SUBMITTED")
TASK_VALIDATING = _state("task", "VALIDATING")
TASK_ACCEPTED = _state("task", "ACCEPTED")
TASK_REJECTED = _state("task", "REJECTED")
TASK_RETRYABLE = _state("task", "RETRYABLE")
TASK_BLOCKED = _state("task", "BLOCKED")
TASK_CANCELLED = _state("task", "CANCELLED")
#: States in which a worker may be holding the task.
TASK_HELD_STATES = (TASK_CLAIMED, TASK_RUNNING)
#: States the next call that looks moves on by itself (validation, recovery).
TASK_SETTLING_STATES = (TASK_SUBMITTED, TASK_VALIDATING, TASK_REJECTED, TASK_RETRYABLE)
TASK_TERMINAL = _terminal("task")

RUN_PAUSED = _state("run", "PAUSED")
RUN_TERMINAL = _terminal("run")
#: Runs in which no task is handed out: ended, or paused.
RUN_NO_DISPATCH = RUN_TERMINAL | {RUN_PAUSED}

PATH_SELECTED = _state("path", "SELECTED")
#: A path in one of these has not run yet and may be waiting for something to exist.
PATH_WAITING_STATES = tuple(_state("path", name) for name in ("DRAFT", "VALIDATED", "SELECTED", "BLOCKED"))

JOB_TERMINAL = _terminal("job")
APPROVAL_PENDING = _state("approval", "PENDING")

#: Why a BLOCKED task is blocked, in the contract's words (r5). A *held* task awaits an
#: approval and says why in ``hold_json.reason`` (``labels.hold_reason``); any other says
#: why in ``tasks.blocked_reason`` (``labels.blocked_reason``). With a reason that axis lists
#: as ``ended`` the task counts as ended, not open, and only a person revives it (design
#: r2.2 §2.7); with ``waiting_need`` it waits on a need its failure's route named. A word
#: that leaves the contract stops this import.
HOLD_REASONS = tuple(contracts.labels()["hold_reason"]["values"])
BLOCKED_REASONS = tuple(contracts.labels()["blocked_reason"]["values"])
ENDED_REASONS = tuple(contracts.labels()["blocked_reason"]["ended"])
BLOCKED_OUT_OF_ATTEMPTS = contracts.require_label("blocked_reason", "out_of_attempts")
BLOCKED_NOT_DISPATCHABLE = contracts.require_label("blocked_reason", "not_dispatchable")
BLOCKED_ROUTED = contracts.require_label("blocked_reason", "routed")
BLOCKED_DEPENDENCY_CANCELLED = contracts.require_label("blocked_reason", "dependency_cancelled")
WAITING_NEED = contracts.require_label("blocked_reason", "waiting_need")

#: Evaluation verdicts that record the absence of a trustworthy evaluator, not a result
#: (design r2.2a §5.10: PASS, FAIL and INCONCLUSIVE adjudicate; nothing else does).
NON_ADJUDICATING_VERDICTS = tuple(
    contracts.require_label("eval_verdict", name)
    for name in ("EVAL_PROPOSED", "NEEDS_DOMAIN_REVIEW", "EVALUATOR_INVALID"))
CANDIDATE_PURPOSE = contracts.require_label("eval_purpose", "CANDIDATE")

#: Task kinds that review another task's work and must name it (the task machine's
#: ``independence`` rule). One of these with no edge to any task has lost its lineage.
#: The task kind that executes a selected path; its claim asks for a lease its path's steps give.
EXECUTE_KIND = contracts.require_label("task_kind", "execute_path")

AUTHORED_REVIEW_KINDS = tuple(contracts.require_label("task_kind", kind) for kind in (
    "review_evidence", "review_evaluation", "review_asset"))

# Event kinds this module reads, and who writes each. They are not in the contract; a
# writer that renames one makes the fact it carries read as absent, which the tests of
# ``load`` on a real store are there to catch.
EVENT_TASK_CREATED = "task.created"              # persistence.records.create_task
EVENT_TASK_TRANSITION = "task.transition"        # persistence.store.transition
EVENT_DISPATCH_FAILED = "task.dispatch_failed"   # core (claim) and host (gates): {source, reason} (design r2 §4.8)
EVENT_PATH_TRANSITION = "path.transition"        # persistence.store.transition
EVENT_PATH_CREATED = "path.created"              # persistence.records.create_path
#: The evaluation reaction's record of one scored path (evaluation.reaction.evaluate_path):
#: {verdict, standing, failure_class, frozen_sha, eval_ids, reason, ...}. The latest per path
#: stands: a re-score under a successor specification is a newer record.
EVENT_PATH_EVALUATED = "path.evaluated"
EVENT_REENTERED = "workflow.reentered"           # orchestration.engine._rewind
EVENT_EXPANDED = "schedule.expanded"             # the reactor: {key, trigger: {kind, id}, created: [task ids]}
EVENT_ROUTED = "schedule.routed"                 # the reactor, payload = a routing decision's event form
EVENT_ROUND_CLOSED = "schedule.round_closed"     # the analyze stage, payload = a stop decision's event form
# The host's hand-out record (design r2.2 §8.4) is not read here as events: what an open
# hand-out is belongs to the host stream, and ``load`` asks its ``hostobs.dispatch.open_handouts``.

#: Kinds read whole; of the two transition kinds only the states scheduling orders by are read.
_LEDGER_KINDS = (EVENT_TASK_CREATED, EVENT_PATH_CREATED, EVENT_PATH_EVALUATED, EVENT_REENTERED, EVENT_EXPANDED,
                 EVENT_ROUTED, EVENT_ROUND_CLOSED, EVENT_DISPATCH_FAILED)

#: Where a judged path stands, in the evaluation stream's words (design r2.2a §6.4, ruling
#: R-e): it has an outcome, it was evaluated and could not be judged, or nothing was
#: adjudicated. Verdict to class is evaluation's; scheduling reads the word and derives none.
STANDING_OUTCOME, STANDING_INCONCLUSIVE, STANDING_UNTESTED = "outcome", "inconclusive", "untested"
STANDINGS = (STANDING_OUTCOME, STANDING_INCONCLUSIVE, STANDING_UNTESTED)

#: ``dependencies.relation`` of a row the reactor writes when it knows who waits for a
#: task: (subject = the waiter) waits on (depends_on = the task). The waiter rests on the
#: task, the same direction as every other row of that table.
RELATION_WAITS_ON = "waits_on"
#: ``dependencies.relation`` of lineage that does not gate READY (design r2 §2.3):
#: (subject = the follow-up task) follows (depends_on = the task, or the job, it follows).
RELATION_FOLLOWS = "follows"
#: ``dependencies.relation`` of a task that must be independent of another (design r2.2
#: §2.3.4): one row per task, beside ``tasks.independent_of_task_id``. Lineage, and no gate.
RELATION_INDEPENDENT_OF = "independent_of"

#: The scope a hold must name (or ``*``, or nothing) to stop tasks being handed out.
HOLD_SCOPE = "teams"


# ----------------------------------------------------------------------------- limits

def _line(limits: Mapping[str, Any], section: str, key: str) -> int:
    try:
        value = limits[section][key]
    except (KeyError, TypeError):
        raise KeyError(
            f"config/limits.json has no {section}.{key}; scheduling takes every number from "
            "that file and invents none") from None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"config/limits.json {section}.{key} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class Limits:
    """The numbers scheduling uses, each a line of ``config/limits.json``."""

    max_parallel_workers: int          # run_budget.max_parallel_workers
    max_overtakes: int                 # run_budget.max_overtakes
    stop_after_no_progress_rounds: int  # run_budget.stop_after_no_progress_rounds
    max_attempts: int                  # task_budget.max_attempts (a task row carries its own)
    initial_candidate_paths: int       # research_budget.initial_candidate_paths
    max_concurrent_active_paths: int   # research_budget.max_concurrent_active_paths
    max_literature_replanning_rounds: int  # research_budget.max_literature_replanning_rounds
    max_repair_attempts_per_fingerprint: int   # engineering_budget.max_repair_attempts_per_fingerprint
    max_global_repair_rounds_per_session: int  # engineering_budget.max_global_repair_rounds_per_session
    #: ids of the holds of ``limits.holds`` that stop tasks being handed out
    holds: tuple[str, ...] = ()
    #: run_budget.resource_waits_per_path (design r2 §6.2, R34), None while the file has no
    #: such line: only routing a ``resource`` failure needs it, and it refuses without it
    resource_waits_per_path: int | None = None

    @classmethod
    def from_config(cls, limits: Mapping[str, Any], *, hold_scope: str = HOLD_SCOPE) -> "Limits":
        """Build from the parsed ``config/limits.json``. A missing line is an error.

        ``max_overtakes`` is how many later arrivals may be claimed ahead of a READY task
        before it goes first (the lead's ruling 5). It has no default: a file without the
        line is refused, so the bound is never one nobody chose. ``resource_waits_per_path``
        is read when present; the one decision that needs it refuses without it.
        """
        holds = tuple(sorted(
            str(hold.get("id", "?")) for hold in (limits.get("holds") or [])
            if isinstance(hold, Mapping) and hold.get("scope") in (None, "*", hold_scope)))
        waits = None
        if "resource_waits_per_path" in (limits.get("run_budget") or {}):
            waits = _line(limits, "run_budget", "resource_waits_per_path")
        return cls(
            max_parallel_workers=_line(limits, "run_budget", "max_parallel_workers"),
            max_overtakes=_line(limits, "run_budget", "max_overtakes"),
            stop_after_no_progress_rounds=_line(limits, "run_budget", "stop_after_no_progress_rounds"),
            max_attempts=_line(limits, "task_budget", "max_attempts"),
            initial_candidate_paths=_line(limits, "research_budget", "initial_candidate_paths"),
            max_concurrent_active_paths=_line(limits, "research_budget", "max_concurrent_active_paths"),
            max_literature_replanning_rounds=_line(limits, "research_budget", "max_literature_replanning_rounds"),
            max_repair_attempts_per_fingerprint=_line(
                limits, "engineering_budget", "max_repair_attempts_per_fingerprint"),
            max_global_repair_rounds_per_session=_line(
                limits, "engineering_budget", "max_global_repair_rounds_per_session"),
            holds=holds, resource_waits_per_path=waits,
        )


# ------------------------------------------------------------------------------ facts

@dataclass(frozen=True)
class RunFacts:
    run_id: str
    status: str
    #: the engine node the run is at (its latest checkpoint), or None before it started
    stage: str | None = None
    revision: int = 0
    #: literature re-planning rounds used so far (the engine's ``round``)
    round_index: int = 0
    #: the event at which the current round began (0 for the first)
    round_started_event_id: int = 0
    #: task_class -> the effort the pinned agent policy assigns by default
    default_efforts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TaskFacts:
    """One task as a decision sees it. ``attempt`` is the number of attempts started."""

    task_id: str
    kind: str
    role: str
    status: str
    created_at: str = ""
    task_class: str | None = None
    #: attempts started: it numbers leases, briefs and submissions and is never reused
    attempt: int = 0
    #: attempts consumed, which is what ``max_attempts`` bounds (contract r5, design §4.8): a
    #: host-caused lapse or stop, a hold and a submission refused for a reason that cannot
    #: change within the lease consume none
    attempts_consumed: int = 0
    max_attempts: int = 1
    #: direct dependencies, and those of them that are not ACCEPTED with their status
    dependencies: tuple[str, ...] = ()
    unmet: tuple[tuple[str, str], ...] = ()
    #: tasks that have not ended and depend on it, transitively
    dependents: int = 0
    #: the longest chain of predecessors behind it (dependencies, and the task a review
    #: must be independent of)
    chain_depth: int = 0
    #: it should name the task it follows and names none (see :func:`lineage_gaps`)
    lineage_missing: bool = False
    #: what is on record as waiting for it: ``task:<id>``, ``path:<id>``, ``approval:<id>``, ...
    waiters: tuple[str, ...] = ()
    #: a live lease holds it (unreleased, its own, and not past ``Snapshot.as_of``)
    lease_live: bool = False
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    #: why it is held (``hold_json.reason``), if it is; an older writer also left here the
    #: words ``tasks.blocked_reason`` now carries
    hold: str | None = None
    #: why a BLOCKED task that is not held is blocked (``tasks.blocked_reason``)
    blocked_reason: str | None = None
    #: the approval a held task waits on (``hold_json.approval_id``), if its hold names one
    hold_approval_id: str | None = None
    path_id: str | None = None
    #: what the task is about (a review's subject), as the task row names it
    subject_kind: str | None = None
    subject_id: str | None = None
    subject_sha256: str | None = None
    #: the event that created it, and the event at which it last became READY (0: not on record)
    created_seq: int = 0
    ready_seq: int = 0
    #: claims since ``ready_seq`` of tasks that became READY after it (READY tasks only)
    overtaken: int = 0
    #: ``task.dispatch_failed`` events since ``ready_seq``: a compile failure, a gate's denial,
    #: a claim refused for the task's own reason (design r2 §4.8)
    dispatch_failures: int = 0
    # what a dispatch row passes on unchanged
    model: str | None = None
    efforts: tuple[str, ...] = ()
    brief_ref: str | None = None
    brief_sha256: str | None = None
    bundle_kind: str | None = None
    acceptance_json: str = "{}"
    independent_of: str | None = None
    parallel_group: str | None = None
    budget_ref: str | None = None
    #: the lease a claim of an execute_path task asks for: exec's ``lease_seconds_for`` of its
    #: path's steps (design r2.2a §7.2.6); None for every other task, where the default applies
    lease_seconds: int | None = None
    #: why an execute_path task's path gives no lease length (a step with no reviewed time
    #: limit): it is then not offered, so a null never reaches an execution's row
    lease_problem: str | None = None

    @property
    def in_flight(self) -> bool:
        """CLAIMED or RUNNING under a live lease. Nothing else is in flight."""
        return self.status in TASK_HELD_STATES and self.lease_live

    @property
    def why_blocked(self) -> str | None:
        """Why a BLOCKED task is blocked: its ``blocked_reason``, else the reason of its hold."""
        return (self.blocked_reason or self.hold) if self.status == TASK_BLOCKED else None

    @property
    def ended(self) -> bool:
        """BLOCKED for a reason the contract lists as ended: its chain ended; it is not open."""
        return self.why_blocked in ENDED_REASONS

    @property
    def out_of_attempts(self) -> bool:
        """``max_attempts`` bounds the attempts consumed."""
        return self.attempts_consumed >= self.max_attempts


@dataclass(frozen=True)
class PathFacts:
    path_id: str
    status: str
    method_family: str = ""
    fingerprint: str = ""
    parent_path_id: str | None = None
    eval_spec_sha256: str | None = None
    #: the round in which it was created
    round: int = 0
    #: the event of its LATEST SELECTED transition: selection order across waves (r2 §4.3)
    selection_seq: int | None = None
    #: the rank that wave gave it, and how many it considered
    selection_rank: int | None = None
    selection_of: int | None = None
    #: the failure class of its latest episode
    failure_class: str | None = None
    #: a candidate evaluation with an adjudicating verdict exists, and the round it was made in
    evaluated: bool = False
    evaluated_round: int | None = None
    # -- the evaluation reaction's latest record of the path (``path.evaluated``), if it has one
    #: its path-level verdict
    verdict: str | None = None
    #: where that leaves the path, one of :data:`STANDINGS` (None: the record does not say)
    standing: str | None = None
    #: the failure class the evaluation gave it (None: a PASS, or none determined)
    verdict_class: str | None = None
    #: the sha256 of the FROZEN specification it was scored under
    verdict_sha256: str | None = None
    #: the event that recorded it
    verdict_seq: int | None = None
    #: the candidate evaluations behind the verdict
    eval_ids: tuple[str, ...] = ()
    #: what the evaluator, or the coverage, said
    verdict_reason: str | None = None
    #: its jobs that ended without ever getting the device, as the exec stream counts a
    #: resource wait (design r2.2 §6.2: a not_started outcome counts per path), in id order
    resource_wait_jobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class BudgetLine:
    """One line of the run's budget, read the one way every reader reads a line (``budgets.line``).

    The lead's ruling (a), 2026-11-13T00:55Z, and ruling 17 of the r5 close: a zero line is
    exhausted and prohibited, never a pause need; a prohibited line that was spent on is broken,
    and that stops the run.
    """

    name: str
    limit: float
    spent: float = 0
    reserved: float = 0

    def __post_init__(self) -> None:
        self.reading  # a line nobody can read (a bool, NaN, an infinity) is refused when it is made, not when used

    @cached_property
    def reading(self) -> dict[str, Any]:
        """``{available, exhausted, prohibited, broken}`` as ``budgets.lines.line`` reads this line."""
        return read_line(self.limit, self.spent, self.reserved)

    @property
    def available(self) -> float:
        return self.reading["available"]

    @property
    def exhausted(self) -> bool:
        """Nothing left to reserve; a zero line is exhausted too."""
        return self.reading["exhausted"]

    @property
    def prohibited(self) -> bool:
        """The run may not use this resource at all: a budget increase is not how it is had."""
        return self.reading["prohibited"]

    @property
    def broken(self) -> bool:
        """Prohibited, and spent on all the same."""
        return self.reading["broken"]


@dataclass(frozen=True)
class ApprovalFacts:
    approval_id: str
    action_kind: str
    subject_kind: str | None = None
    subject_id: str | None = None
    subject_sha256: str | None = None


@dataclass(frozen=True)
class JobFacts:
    """A job that has not ended, as its row records it. A row moves only when somebody looks."""

    job_id: str
    status: str
    path_id: str | None = None
    task_id: str | None = None
    #: by when it will have ended and been settled at the latest, as the exec stream computes
    #: it from the row and the limits (None: it could not be computed from the row)
    deadline_at: str | None = None


@dataclass(frozen=True)
class RoutedFacts:
    """One failure that was routed, as its ``schedule.routed`` event recorded it."""

    event_id: int
    failure_class: str
    route: str
    task_id: str | None = None
    path_id: str | None = None
    fingerprint: str | None = None
    round: int = 0
    #: the job whose failure was routed, when it was a job's
    job_id: str | None = None


@dataclass(frozen=True)
class RoundFacts:
    """What one closed round added: the three counters of the no-progress rule, and why it may not count.

    A round whose searches all failed, or in which machinery stopped a path, is not evidence
    of no progress (design r2 §6.5, R37): a proxy outage is not a dead subject. Nor is a
    round whose record left a counter out, or gave something that is not a count
    (``incomplete``): what was not written down is not a zero.
    """

    index: int
    reviewed_added: int = 0
    gaps_covered: int = 0
    paths_evaluated: int = 0
    searches_all_failed: bool = False
    machinery_failed: bool = False
    incomplete: bool = False


_NESTED = {
    "run": RunFacts, "limits": Limits, "tasks": TaskFacts, "paths": PathFacts, "budget": BudgetLine,
    "approvals": ApprovalFacts, "jobs": JobFacts, "routed": RoutedFacts, "rounds": RoundFacts,
}


def _tuplify(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_tuplify(item) for item in value)
    return value


def _build(cls: type, data: Mapping[str, Any]) -> Any:
    known = {item.name for item in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{cls.__name__} has no field(s) {unknown}")
    return cls(**{key: _tuplify(value) for key, value in data.items()})


@dataclass(frozen=True)
class Snapshot:
    """The state of one run at one event, as far as scheduling may look."""

    run: RunFacts
    limits: Limits
    tasks: tuple[TaskFacts, ...] = ()
    paths: tuple[PathFacts, ...] = ()
    budget: tuple[BudgetLine, ...] = ()
    approvals: tuple[ApprovalFacts, ...] = ()
    jobs: tuple[JobFacts, ...] = ()
    routed: tuple[RoutedFacts, ...] = ()
    rounds: tuple[RoundFacts, ...] = ()
    #: tasks handed to a running dispatch and not yet claimed (the host's hand-out record,
    #: design r2 §4.7, §8.4): taken, not offered again
    offered: tuple[str, ...] = ()
    #: the last event of the run this state includes
    as_of_event_id: int = 0
    #: the instant leases were compared with, given by the caller of ``load``
    as_of: str = ""

    # -- views ------------------------------------------------------------------
    @cached_property
    def by_id(self) -> Mapping[str, TaskFacts]:
        return {task.task_id: task for task in self.tasks}

    @cached_property
    def path_by_id(self) -> Mapping[str, PathFacts]:
        return {path.path_id: path for path in self.paths}

    @property
    def ready(self) -> tuple[TaskFacts, ...]:
        return tuple(task for task in self.tasks if task.status == TASK_READY)

    @property
    def in_flight(self) -> tuple[TaskFacts, ...]:
        return tuple(task for task in self.tasks if task.in_flight)

    @property
    def dispatching(self) -> bool:
        return self.run.status not in RUN_NO_DISPATCH

    @property
    def lineage_missing(self) -> tuple[str, ...]:
        return tuple(sorted(task.task_id for task in self.tasks if task.lineage_missing))

    # -- recording and replay ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-able, in a canonical order: the same facts give the same bytes."""
        data = asdict(self)
        data["tasks"] = sorted(data["tasks"], key=lambda item: item["task_id"])
        data["paths"] = sorted(data["paths"], key=lambda item: item["path_id"])
        data["budget"] = sorted(data["budget"], key=lambda item: item["name"])
        data["approvals"] = sorted(data["approvals"], key=lambda item: item["approval_id"])
        data["jobs"] = sorted(data["jobs"], key=lambda item: item["job_id"])
        data["routed"] = sorted(data["routed"], key=lambda item: item["event_id"])
        data["rounds"] = sorted(data["rounds"], key=lambda item: item["index"])
        data["offered"] = sorted(data["offered"])
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Snapshot":
        """The inverse of :meth:`to_dict`, also after a trip through JSON."""
        built: dict[str, Any] = {}
        for key, value in data.items():
            nested = _NESTED.get(key)
            if nested is None:
                built[key] = _tuplify(value)
            elif isinstance(value, Mapping):
                built[key] = _build(nested, value)
            else:
                built[key] = tuple(_build(nested, item) for item in value)
        return cls(**built)

    def sha256(self) -> str:
        """Identifies the facts a decision was made on, whatever order they were listed in."""
        text = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- pure derivations

def instant(text: str) -> datetime:
    """An ISO-8601 instant as the store writes it (``...Z``), in UTC."""
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def lease_is_live(expires_at: str, as_of: str) -> bool:
    """A lease is live strictly before its deadline, as the task service judges it."""
    return instant(expires_at) > instant(as_of)


def deadline_passed(deadline_at: str, as_of: str) -> bool:
    """A deadline has passed at its own instant and after, as the job service judges a job overdue."""
    return instant(deadline_at) <= instant(as_of)


def count_overtaken(ready_seq: int, claimed_ready_seqs: Sequence[int]) -> int:
    """How many claims were of tasks that became READY after ``ready_seq``.

    ``claimed_ready_seqs`` is, for every claim of the run, the event at which the claimed
    task had become READY, in ascending order. A task that became READY later and was
    claimed while this one still waits has been taken ahead of it. An unknown
    ``ready_seq`` (0) counts nothing: an age that is not on record is not an age.
    """
    if ready_seq <= 0:
        return 0
    return len(claimed_ready_seqs) - bisect.bisect_right(claimed_ready_seqs, ready_seq)


def transitive_dependents(reverse: Mapping[str, Sequence[str]], status: Mapping[str, str],
                          task_id: str, ended: Iterable[str] = ()) -> int:
    """Tasks that have not ended and depend on ``task_id``, through any number of steps.

    ``ended``: tasks that are BLOCKED with their chain ended (design r2.2 §2.7): they count as
    ended, not open, though their state is not terminal.
    """
    over = set(ended)
    seen: set[str] = set()
    stack = list(reverse.get(task_id, ()))
    while stack:
        current = stack.pop()
        if current in seen or current == task_id:
            continue
        seen.add(current)
        stack.extend(reverse.get(current, ()))
    return sum(1 for item in seen if item in status and status[item] not in TASK_TERMINAL and item not in over)


def chain_depths(predecessors: Mapping[str, Sequence[str]]) -> dict[str, int]:
    """For each task, the length of the longest chain of predecessors behind it.

    Iterative, and safe on a graph that should not exist: a task on a cycle is given the
    depth reached before the cycle closes instead of recursing for ever.
    """
    depth: dict[str, int] = {}
    for start in predecessors:
        if start in depth:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        on_path: list[str] = []
        while stack:
            node, state = stack.pop()
            if state == 1:
                on_path.pop()
                depth[node] = max((depth.get(prev, 0) + 1 for prev in predecessors.get(node, ())
                                   if prev in predecessors), default=0)
                continue
            if node in depth or node in on_path:
                continue
            on_path.append(node)
            stack.append((node, 1))
            for prev in predecessors.get(node, ()):
                if prev in predecessors and prev not in depth and prev not in on_path:
                    stack.append((prev, 0))
    return depth


def ancestors(predecessors: Mapping[str, Sequence[str]], task_id: str) -> set[str]:
    """Every task behind ``task_id`` through any number of edges (cycles are cut)."""
    seen: set[str] = set()
    stack = list(predecessors.get(task_id, ()))
    while stack:
        current = stack.pop()
        if current in seen or current == task_id:
            continue
        seen.add(current)
        stack.extend(predecessors.get(current, ()))
    return seen


def lineage_gaps(kinds: Mapping[str, str], predecessors: Mapping[str, Sequence[str]],
                 expansions: Iterable[tuple[str, str, Sequence[str]]],
                 authored_here: Mapping[str, bool] | None = None) -> dict[str, str]:
    """Tasks that should name the task they follow and do not, each with why.

    Two sources, each a record somebody else wrote:

    * a review of another task's work (``AUTHORED_REVIEW_KINDS``) with no edge at all,
      when a task of this run wrote what it reviews: the task machine's independence rule
      says it names that task. ``authored_here`` says, per review, whether its subject's
      author is a task of this run; a review of what no task of this run wrote (an
      operator's registration, an import, another run's asset) has nothing to name and is a
      root. Without ``authored_here`` every edgeless review is reported;
    * an expansion the reactor recorded (``schedule.expanded``) whose trigger is a task:
      every task it created must have that task behind it, through its dependencies or
      the task it must be independent of, at any depth.

    ``expansions`` is ``(trigger_kind, trigger_id, created_task_ids)``. A root that no
    expansion recorded (a stage's first tasks) is not a gap: depth 0 is its true place.
    """
    gaps: dict[str, str] = {}
    for task_id, kind in kinds.items():
        if kind in AUTHORED_REVIEW_KINDS and not predecessors.get(task_id) and (
                authored_here is None or authored_here.get(task_id, False)):
            gaps[task_id] = f"a {kind} task names no task it reviews or follows"
    for trigger_kind, trigger_id, created in expansions:
        if trigger_kind != "task" or not trigger_id:
            continue
        for task_id in created:
            if task_id not in kinds or task_id in gaps:
                continue
            if trigger_id not in ancestors(predecessors, task_id):
                gaps[task_id] = (f"expanded from task {trigger_id} and names it in neither its "
                                 "dependencies nor independent_of, at any depth")
    return gaps


# ------------------------------------------------------------------------------- load

def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, tuple(params)).fetchall()


def _json(text: Any, default: Any) -> Any:
    """A JSON column, or ``default`` for NULL, empty or unreadable text."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _hold_reason(hold_json: Any) -> str | None:
    """The reason a task's ``hold_json`` gives, if it gives one."""
    hold = _json(hold_json, None)
    return str(hold["reason"]) if isinstance(hold, Mapping) and hold.get("reason") else None


def _hold_approval(hold_json: Any) -> str | None:
    """The approval a task's ``hold_json`` names, if it names one as a non-empty string."""
    hold = _json(hold_json, None)
    return _text(hold.get("approval_id")) if isinstance(hold, Mapping) else None


def _marks(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def _is_count(value: Any) -> bool:
    """A whole number that is not negative. True and False are not counts."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


_ROUND_COUNTERS = ("reviewed_added", "gaps_covered", "paths_evaluated")
_ROUND_FLAGS = ("searches_all_failed", "machinery_failed")


def _round(closed: Mapping[str, Any]) -> RoundFacts:
    """A closed round as its event recorded it. A counter it does not give as a count is not read as 0."""
    counted = {name: closed[name] for name in _ROUND_COUNTERS if _is_count(closed.get(name))}
    return RoundFacts(index=closed["index"], **counted, **{flag: bool(closed.get(flag)) for flag in _ROUND_FLAGS},
                      incomplete=bool(closed.get("incomplete")) or len(counted) < len(_ROUND_COUNTERS))


def _text(value: Any) -> str | None:
    """A non-empty string, or None: a number where a name belongs is not a name."""
    return value if isinstance(value, str) and value else None


def _eval_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The candidate evaluations behind a path's verdict: the record's flat list, else the ids of its rows."""
    ids = record.get("eval_ids")
    if not isinstance(ids, list):
        rows = record.get("rows")
        ids = [row.get("eval_id") for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    return tuple(item for item in ids if isinstance(item, str) and item)


def load(conn: sqlite3.Connection, run_id: str, *, now: str,
         limits: Mapping[str, Any] | None = None) -> Snapshot:
    """Read the facts of one run. Writes nothing; a fixed number of queries.

    ``now`` is the instant leases are compared with: pass the reading the caller settled
    at. ``limits`` is the parsed ``config/limits.json``; by default the file is read now,
    and a file that lacks a line scheduling needs is refused before any query. The reads
    happen inside one read transaction when the caller has none open, so the snapshot is
    one state of the store and not a blend of two; a transaction that only reads never
    takes the write lock. Inside the caller's transaction (a claim), the caller's view is
    the one read.
    """
    instant(now)  # refuse an unreadable instant before any query
    document = config.limits() if limits is None else limits
    bounds = Limits.from_config(document)
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        return _read(conn, run_id, now, bounds, document)
    finally:
        if owns:
            conn.execute("ROLLBACK")


def _read(conn: sqlite3.Connection, run_id: str, now: str, bounds: Limits, document: Mapping[str, Any]) -> Snapshot:
    run = _rows(conn, "SELECT run_id, status, revision, budget_ref, agent_policy_json FROM runs"
                      " WHERE run_id=?", (run_id,))
    if not run:
        raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
    run_row = run[0]
    as_of_event_id = int(_rows(conn, "SELECT COALESCE(MAX(event_id), 0) AS n FROM events WHERE run_id=?",
                               (run_id,))[0]["n"])
    stage_rows = _rows(conn, "SELECT node FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,))

    task_rows = _rows(
        conn,
        "SELECT task_id, path_id, kind, role, task_class, status, attempt, attempts_consumed, max_attempts,"
        " bundle_kind, acceptance_json, brief_ref, brief_sha256, parallel_group, subject_kind, subject_id,"
        " subject_sha256, independent_of_task_id, required_model, required_effort_json, hold_json,"
        " blocked_reason, budget_ref, lease_id, created_at FROM tasks WHERE run_id=? ORDER BY rowid", (run_id,))
    dep_rows = _rows(
        conn,
        "SELECT d.task_id AS task_id, d.depends_on AS depends_on FROM task_deps d"
        " JOIN tasks t ON t.task_id = d.task_id WHERE t.run_id=?", (run_id,))
    waits_rows = _rows(
        conn,
        "SELECT d.subject_kind AS subject_kind, d.subject_id AS subject_id, d.depends_on_id AS task_id"
        " FROM dependencies d JOIN tasks t ON t.task_id = d.depends_on_id"
        " WHERE t.run_id=? AND d.depends_on_kind='task' AND d.relation=?", (run_id, RELATION_WAITS_ON))
    # lineage that does not gate: a follow-up follows a task, or a job (which counts as the job's task),
    # and a task names each task it must be independent of
    follows_rows = _rows(
        conn,
        "SELECT d.subject_id AS task_id, CASE WHEN d.depends_on_kind='job' THEN j.task_id ELSE d.depends_on_id END"
        " AS follows FROM dependencies d JOIN tasks t ON t.task_id = d.subject_id"
        " LEFT JOIN jobs j ON d.depends_on_kind='job' AND j.job_id = d.depends_on_id"
        " WHERE t.run_id=? AND d.subject_kind='task' AND d.relation IN (?, ?)"
        " AND d.depends_on_kind IN ('task', 'job')",
        (run_id, RELATION_FOLLOWS, RELATION_INDEPENDENT_OF))
    # who wrote what each review reviews: an asset's, a specification's or an evidence record's author task
    author_rows = _rows(
        conn,
        "SELECT t.task_id AS task_id, a.author_task_id AS author FROM tasks t JOIN assets a"
        " ON a.asset_id = t.subject_id AND (t.subject_sha256 IS NULL OR a.content_sha256 = t.subject_sha256)"
        " WHERE t.run_id=? AND t.subject_kind='asset'"
        " UNION ALL SELECT t.task_id, e.author_task_id FROM tasks t JOIN eval_specs e ON e.eval_spec_id = t.subject_id"
        " WHERE t.run_id=? AND t.subject_kind='eval_spec'"
        " UNION ALL SELECT t.task_id, v.extracted_task_id FROM tasks t JOIN evidence v ON v.evidence_id = t.subject_id"
        " WHERE t.run_id=? AND t.subject_kind='evidence'", (run_id, run_id, run_id))
    lease_rows = _rows(
        conn,
        "SELECT l.lease_id AS lease_id, l.task_id AS task_id, l.owner AS owner, l.expires_at AS expires_at"
        " FROM leases l JOIN tasks t ON t.task_id = l.task_id"
        " WHERE t.run_id=? AND l.released_at IS NULL ORDER BY l.issued_at, l.lease_id", (run_id,))
    path_rows = _rows(
        conn,
        "SELECT path_id, status, method_family, fingerprint, parent_path_id, eval_spec_sha256,"
        " capability_refs_json, skill_refs_json FROM paths WHERE run_id=? ORDER BY rowid", (run_id,))
    # the registered rows of the capabilities the run's paths name: what a job of a step runs under
    # (one query whatever the run holds; SQLite reads ``IN ()`` as matching nothing)
    named = sorted({name.partition("@")[0] for row in path_rows for name in _step_names(row["capability_refs_json"])})
    capability_rows = _rows(
        conn,
        "SELECT capability_id, version, manifest_sha256, resource_requirements_json FROM capabilities"
        f" WHERE capability_id IN ({_marks(named)})", tuple(named))
    event_rows = _rows(
        conn,
        "SELECT event_id, ts, kind, subject_id, new_state, payload_json FROM events WHERE run_id=? AND ("
        "(kind=? AND new_state IN (?, ?)) OR (kind=? AND new_state=?)"
        f" OR kind IN ({_marks(_LEDGER_KINDS)})) ORDER BY event_id",
        (run_id, EVENT_TASK_TRANSITION, TASK_READY, TASK_CLAIMED, EVENT_PATH_TRANSITION, PATH_SELECTED,
         *_LEDGER_KINDS))
    approval_rows = _rows(
        conn,
        "SELECT approval_id, action_kind, subject_kind, subject_id, subject_sha256 FROM approvals"
        " WHERE run_id=? AND status=? ORDER BY requested_at, approval_id", (run_id, APPROVAL_PENDING))
    job_rows = _rows(
        conn,
        "SELECT job_id, status, path_id, task_id, resources_json, timeout_seconds, submitted_at, created_at"
        " FROM jobs WHERE run_id=?"
        f" AND status NOT IN ({_marks(JOB_TERMINAL)}) ORDER BY created_at, job_id",
        (run_id, *sorted(JOB_TERMINAL)))
    episode_rows = _rows(
        conn,
        "SELECT path_id, failure_class FROM episodes WHERE run_id=? AND path_id IS NOT NULL"
        " ORDER BY created_at, rowid", (run_id,))
    evaluation_rows = _rows(
        conn,
        "SELECT path_id, created_at FROM evaluations WHERE run_id=? AND path_id IS NOT NULL AND purpose=?"
        f" AND verdict NOT IN ({_marks(NON_ADJUDICATING_VERDICTS)}) ORDER BY created_at, rowid",
        (run_id, CANDIDATE_PURPOSE, *NON_ADJUDICATING_VERDICTS))
    budget = _budget_lines(conn, run_row["budget_ref"])
    waits = _resource_waits(conn, run_id)

    # -- the event log: rounds, selections, creation and READY order, who was claimed
    round_starts: list[tuple[int, str]] = []          # (event id, ts) of each re-entry
    path_created: dict[str, int] = {}
    selection: dict[str, tuple[int, int | None, int | None]] = {}
    created_seq: dict[str, int] = {}
    last_ready: dict[str, int] = {}
    failures_since_ready: dict[str, int] = {}
    claimed_ready_seqs: list[int] = []
    expansions: list[tuple[str, str, tuple[str, ...]]] = []
    routed: list[RoutedFacts] = []
    rounds: dict[int, RoundFacts] = {}
    judged: dict[str, tuple[int, Mapping[str, Any]]] = {}         # path_id -> its latest path.evaluated record
    for row in event_rows:
        kind, subject, event_id = row["kind"], row["subject_id"], int(row["event_id"])
        if kind == EVENT_TASK_TRANSITION:
            if row["new_state"] == TASK_READY:
                last_ready[subject] = event_id
                failures_since_ready[subject] = 0
            elif row["new_state"] == TASK_CLAIMED:
                claimed_ready_seqs.append(last_ready.get(subject, 0))
        elif kind == EVENT_DISPATCH_FAILED:
            failures_since_ready[subject] = failures_since_ready.get(subject, 0) + 1
        elif kind == EVENT_TASK_CREATED:
            created_seq.setdefault(subject, event_id)
        elif kind == EVENT_PATH_CREATED:
            path_created.setdefault(subject, event_id)
        elif kind == EVENT_PATH_EVALUATED:
            payload = _json(row["payload_json"], {})
            if isinstance(payload, Mapping) and isinstance(payload.get("verdict"), str):
                judged[subject] = (event_id, payload)  # the latest stands: a re-score replaces what it re-scored
        elif kind == EVENT_PATH_TRANSITION:
            if row["new_state"] == PATH_SELECTED:  # the latest selection wins (design r2 §4.3, R7)
                payload = _json(row["payload_json"], {})
                rank, of = payload.get("rank"), payload.get("considered")
                selection[subject] = (event_id, rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
                                      of if isinstance(of, int) and not isinstance(of, bool) else None)
        elif kind == EVENT_REENTERED:
            round_starts.append((event_id, row["ts"]))
        elif kind == EVENT_EXPANDED:
            payload = _json(row["payload_json"], {})
            trigger = payload.get("trigger") if isinstance(payload, Mapping) else None
            created = payload.get("created") if isinstance(payload, Mapping) else None
            if isinstance(trigger, Mapping) and isinstance(created, list):
                expansions.append((str(trigger.get("kind") or ""), str(trigger.get("id") or ""),
                                   tuple(str(item) for item in created)))
        elif kind == EVENT_ROUTED:
            payload = _json(row["payload_json"], {})
            if isinstance(payload, Mapping) and payload.get("failure_class") and payload.get("route"):
                routed.append(RoutedFacts(
                    event_id=event_id, failure_class=str(payload["failure_class"]), route=str(payload["route"]),
                    task_id=payload.get("task_id"), path_id=payload.get("path_id"),
                    fingerprint=payload.get("fingerprint"), round=int(payload.get("round") or 0),
                    job_id=_text(payload.get("job_id"))))
        elif kind == EVENT_ROUND_CLOSED:
            payload = _json(row["payload_json"], {})
            closed = payload.get("round") if isinstance(payload, Mapping) else None
            if isinstance(closed, Mapping) and _is_count(closed.get("index")):
                rounds[closed["index"]] = _round(closed)
    claimed_ready_seqs.sort()
    round_event_ids = [event_id for event_id, _ts in round_starts]
    round_instants = [instant(ts) for _event_id, ts in round_starts]

    # -- tasks
    status = {row["task_id"]: row["status"] for row in task_rows}
    reasons = {row["task_id"]: (row["blocked_reason"] or _hold_reason(row["hold_json"])) for row in task_rows
               if row["status"] == TASK_BLOCKED}
    over = {task_id for task_id, reason in reasons.items() if reason in ENDED_REASONS}   # ended, not open
    forward: dict[str, list[str]] = {row["task_id"]: [] for row in task_rows}
    reverse: dict[str, list[str]] = {}
    for row in dep_rows:
        forward.setdefault(row["task_id"], []).append(row["depends_on"])
        reverse.setdefault(row["depends_on"], []).append(row["task_id"])
    predecessors = {task_id: list(deps) for task_id, deps in forward.items()}
    for row in task_rows:
        author = row["independent_of_task_id"]
        if author and author in status and author not in predecessors[row["task_id"]]:
            predecessors[row["task_id"]].append(author)
    for row in follows_rows:
        followed = row["follows"]
        if followed and followed in status and followed != row["task_id"] \
                and followed not in predecessors[row["task_id"]]:
            predecessors[row["task_id"]].append(followed)
    depth = chain_depths(predecessors)
    authored_here: dict[str, bool] = {}
    for row in author_rows:
        authored_here[row["task_id"]] = authored_here.get(row["task_id"], False) or row["author"] in status
    missing = lineage_gaps({row["task_id"]: row["kind"] for row in task_rows}, predecessors, expansions,
                           authored_here)
    from ..jobs import leases as job_leases
    held_open = job_leases.held_open(conn, run_id, limits=document, at=now)
    live: dict[str, Mapping[str, Any]] = {}
    lease_of = {row["task_id"]: row["lease_id"] for row in task_rows}
    for row in lease_rows:
        task_id = row["task_id"]
        held = held_open.get(task_id, {})
        if lease_of.get(task_id) == row["lease_id"] and (
                lease_is_live(row["expires_at"], now) or held.get("lease_ref") == row["lease_id"]):
            effective = dict(row)
            if held.get("deadline_at"):
                effective["expires_at"] = max(row["expires_at"], held["deadline_at"], key=instant)
            live[task_id] = effective

    path_status = {row["path_id"]: row["status"] for row in path_rows}
    pending_approvals = {row["approval_id"] for row in approval_rows}
    waits_on: dict[str, list[str]] = {}
    for row in waits_rows:
        waiter_kind, waiter_id = row["subject_kind"], row["subject_id"]
        if waiter_kind == "task" and (status.get(waiter_id, TASK_PENDING) in TASK_TERMINAL or waiter_id in over):
            continue
        if waiter_kind == "path" and path_status.get(waiter_id, PATH_WAITING_STATES[0]) not in PATH_WAITING_STATES:
            continue
        if waiter_kind == "approval" and waiter_id not in pending_approvals:
            continue
        waits_on.setdefault(row["task_id"], []).append(f"{waiter_kind}:{waiter_id}")

    waiting_paths = [row for row in path_rows if row["status"] in PATH_WAITING_STATES]
    leases_of = _execution_leases(task_rows, path_rows, capability_rows, document)
    tasks = []
    for row in task_rows:
        task_id = row["task_id"]
        deps = sorted(forward.get(task_id, ()))
        ready_seq = last_ready.get(task_id, 0)
        lease = live.get(task_id)
        tasks.append(TaskFacts(
            task_id=task_id, kind=row["kind"], role=row["role"], status=row["status"],
            created_at=row["created_at"], task_class=row["task_class"], attempt=int(row["attempt"]),
            attempts_consumed=int(row["attempts_consumed"] or 0),
            max_attempts=int(row["max_attempts"]), dependencies=tuple(deps),
            unmet=tuple((dep, status.get(dep, "missing")) for dep in deps if status.get(dep) != TASK_ACCEPTED),
            dependents=transitive_dependents(reverse, status, task_id, over),
            chain_depth=depth.get(task_id, 0), lineage_missing=task_id in missing,
            waiters=_waiters(row, reverse, status, waiting_paths, approval_rows, waits_on.get(task_id, ()), over),
            lease_live=lease is not None, lease_owner=lease["owner"] if lease is not None else None,
            lease_expires_at=lease["expires_at"] if lease is not None else None,
            hold=_hold_reason(row["hold_json"]), blocked_reason=_text(row["blocked_reason"]),
            hold_approval_id=_hold_approval(row["hold_json"]),
            path_id=row["path_id"], subject_kind=row["subject_kind"], subject_id=row["subject_id"],
            subject_sha256=row["subject_sha256"], created_seq=created_seq.get(task_id, 0), ready_seq=ready_seq,
            overtaken=count_overtaken(ready_seq, claimed_ready_seqs) if row["status"] == TASK_READY else 0,
            dispatch_failures=failures_since_ready.get(task_id, 0),
            model=row["required_model"], efforts=tuple(_json(row["required_effort_json"], []) or ()),
            brief_ref=row["brief_ref"], brief_sha256=row["brief_sha256"], bundle_kind=row["bundle_kind"],
            acceptance_json=row["acceptance_json"] or "{}", independent_of=row["independent_of_task_id"],
            parallel_group=row["parallel_group"], budget_ref=row["budget_ref"],
            lease_seconds=leases_of.get(task_id, (None, None))[0],
            lease_problem=leases_of.get(task_id, (None, None))[1],
        ))

    # -- paths
    failure = {row["path_id"]: row["failure_class"] for row in episode_rows}   # the latest wins
    first_evaluated: dict[str, str] = {}
    for row in evaluation_rows:
        first_evaluated.setdefault(row["path_id"], row["created_at"])
    paths = []
    for row in path_rows:
        path_id = row["path_id"]
        chosen = selection.get(path_id)
        evaluated_at = first_evaluated.get(path_id)
        verdict_seq, record = judged.get(path_id, (None, {}))
        paths.append(PathFacts(
            path_id=path_id, status=row["status"], method_family=row["method_family"] or "",
            fingerprint=row["fingerprint"] or "", parent_path_id=row["parent_path_id"],
            eval_spec_sha256=row["eval_spec_sha256"],
            round=bisect.bisect_left(round_event_ids, path_created[path_id]) if path_id in path_created else 0,
            selection_seq=chosen[0] if chosen else None, selection_rank=chosen[1] if chosen else None,
            selection_of=chosen[2] if chosen else None, failure_class=failure.get(path_id),
            evaluated=evaluated_at is not None,
            evaluated_round=(bisect.bisect_right(round_instants, instant(evaluated_at))
                             if evaluated_at is not None else None),
            verdict=_text(record.get("verdict")),
            standing=record.get("standing") if record.get("standing") in STANDINGS else None,
            verdict_class=_text(record.get("failure_class")), verdict_sha256=_text(record.get("frozen_sha")),
            verdict_seq=verdict_seq, eval_ids=_eval_ids(record),
            verdict_reason=_text(record.get("reason")) or _text(record.get("why")),
            resource_wait_jobs=waits.get(path_id, ()),
        ))

    offered = _offered(conn, run_id, now, {row["task_id"]: (row["status"], int(row["attempt"])) for row in task_rows})

    policy = _json(run_row["agent_policy_json"], {}) or {}
    efforts = tuple(sorted(
        (str(name), str(spec["default_effort"])) for name, spec in (policy.get("task_classes") or {}).items()
        if isinstance(spec, Mapping) and spec.get("default_effort")))
    return Snapshot(
        run=RunFacts(
            run_id=run_id, status=run_row["status"], stage=stage_rows[0]["node"] if stage_rows else None,
            revision=int(run_row["revision"]), round_index=len(round_starts),
            round_started_event_id=round_event_ids[-1] if round_event_ids else 0, default_efforts=efforts),
        limits=bounds,
        tasks=tuple(tasks), paths=tuple(paths), budget=budget,
        approvals=tuple(ApprovalFacts(
            approval_id=row["approval_id"], action_kind=row["action_kind"], subject_kind=row["subject_kind"],
            subject_id=row["subject_id"], subject_sha256=row["subject_sha256"]) for row in approval_rows),
        jobs=tuple(JobFacts(job_id=row["job_id"], status=row["status"], path_id=row["path_id"],
                            task_id=row["task_id"], deadline_at=_job_deadline(row, document, now))
                   for row in job_rows),
        routed=tuple(routed), rounds=tuple(rounds[index] for index in sorted(rounds)), offered=tuple(offered),
        as_of_event_id=as_of_event_id, as_of=now,
    )


def _budget_lines(conn: sqlite3.Connection, budget_ref: str | None) -> tuple[BudgetLine, ...]:
    """The lines of the run's root budget, through the budget service's own reading."""
    if not budget_ref:
        return ()
    from .. import budgets  # the eval stream's; imported here so the pure modules never load it

    state = budgets.state(conn, budget_ref)
    lines = []
    for name, limit in sorted((state.get("limits") or {}).items()):
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            continue
        lines.append(BudgetLine(
            name=str(name), limit=limit, spent=(state.get("spent") or {}).get(name, 0) or 0,
            reserved=(state.get("reserved") or {}).get(name, 0) or 0))
    return tuple(lines)


def _offered(conn: sqlite3.Connection, run_id: str, now: str, attempt_of: Mapping[str, tuple[str, int]]) -> list[str]:
    """Tasks handed to a running dispatch and not claimed yet (design r2.2 §4.7, §8.4), in id order.

    Which hand-outs are open at ``now`` is the host stream's to say (``open_handouts``: not
    closed, before its expiry; a store read of at most two queries, with no clock when the
    instant is given). A row of one is offered while its task is still READY at the attempt
    the gate recorded, which is read off the snapshot's own task rows, so both halves are of
    the same moment. A row that does not name a task at an attempt is not a row.
    """
    from ..hostobs import dispatch as handouts  # the host stream's; imported here so the pure modules never load it

    offered = set()
    for item in handouts.open_handouts(conn, run_id=run_id, at=now):
        rows = item.get("rows")
        for row in rows if isinstance(rows, list) else ():
            if (isinstance(row, Mapping) and _is_count(row.get("attempt"))
                    and attempt_of.get(str(row.get("task_id"))) == (TASK_READY, row["attempt"])):
                offered.add(str(row["task_id"]))
    return sorted(offered)


def _resource_waits(conn: sqlite3.Connection, run_id: str) -> dict[str, tuple[str, ...]]:
    """Per path, the jobs that ended as a wait for the device that never got it, in id order.

    What such a wait is belongs to the exec stream (``jobs.outcomes.by_path``: one read of
    the ``jobs`` table as recorded, no runner, no file, no clock). A job of no path is no
    path's wait.
    """
    from ..jobs import outcomes  # the exec stream's; imported here so the pure modules never load it

    found = {}
    for path_id, summary in (outcomes.by_path(conn, run_id).get("paths") or {}).items():
        jobs = sorted(str(item["job_id"]) for item in summary.get("listed") or [] if item.get("resource_wait"))
        if jobs:
            found[path_id] = tuple(jobs)
    return found


def _step_names(capability_refs_json: Any) -> list[str]:
    """The ``capability_version`` (id@version) of each step a path's row records, where it records one."""
    steps = _json(capability_refs_json, None)
    if not isinstance(steps, list):
        return []
    return [step["capability_version"] for step in steps
            if isinstance(step, Mapping) and _text(step.get("capability_version"))]


def _execution_leases(task_rows: Sequence[sqlite3.Row], path_rows: Sequence[sqlite3.Row],
                      capability_rows: Sequence[sqlite3.Row],
                      document: Mapping[str, Any]) -> dict[str, tuple[int | None, str | None]]:
    """For each execute_path task, the lease a claim of it asks for, or why its path gives none.

    The length is exec's to define (``jobs.leases.lease_seconds_for``, design r2.2a §7.2.6):
    the task lease plus, per step of the path, the time its job may take. Planning records a
    step as ``{capability_version, manifest_sha256, parameters}``; its time limit is that of
    the registered manifest, which is what a job of the step runs under (the registry's row,
    ``capabilities.resource_requirements_json``), and only when the row is of the manifest the
    plan names (rv-models-2, 02:35Z: the row holds no resources). With the limits document
    given it reads nothing else. A step that names no registered version, or one registered
    with another manifest, and a path whose steps exec refuses (a step with no reviewed time
    limit) yield why instead, and the task is not offered.
    """
    from ..jobs import leases  # the exec stream's; imported here so the pure modules never load it

    steps_of = {row["path_id"]: row["capability_refs_json"] for row in path_rows}
    registered = {(row["capability_id"], row["version"]): row for row in capability_rows}
    found: dict[str, tuple[int | None, str | None]] = {}
    for row in task_rows:
        if row["kind"] != EXECUTE_KIND:
            continue
        path_id = row["path_id"]
        steps = _json(steps_of.get(path_id), None)
        if not isinstance(steps, list):
            found[row["task_id"]] = (None, f"its path {path_id} records no steps")
            continue
        planned, problem = [], None
        for index, step in enumerate(steps):
            name = _text(step.get("capability_version")) if isinstance(step, Mapping) else None
            capability_id, _at, version = (name or "").partition("@")
            entry = registered.get((capability_id, version))
            if entry is None:
                problem = f"step {index} of its path {path_id} names {name!r}, which is not a registered version"
                break
            if step.get("manifest_sha256") and entry["manifest_sha256"] != step["manifest_sha256"]:
                problem = (f"step {index} of its path {path_id} ({name}) is registered with another manifest "
                           "than its plan names")
                break
            planned.append({**step, "resources": _json(entry["resource_requirements_json"], {}) or {}})
        if problem is not None:
            found[row["task_id"]] = (None, problem)
            continue
        try:
            found[row["task_id"]] = (int(leases.lease_seconds_for(planned, limits=document)), None)
        except (TypeError, ValueError) as exc:
            found[row["task_id"]] = (None, f"its path {path_id} gives no lease length: {exc}")
    return found


def _job_deadline(row: sqlite3.Row, document: Mapping[str, Any], now: str) -> str | None:
    """An open job's deadline, through the exec stream's own function of the row (no query, no clock).

    Given the limits document and the instant, ``jobs.leases.job_deadline`` reads nothing
    else, so the answer is a fact of the row. A row it cannot compute a deadline from has
    none on record: the job is then waited for as recorded, and never breaks the read.
    """
    from ..jobs import leases  # the exec stream's; imported here so the pure modules never load it

    try:
        return _text(leases.job_deadline(row, limits=document, at=now).get("deadline_at"))
    except (KeyError, TypeError, ValueError):
        return None


def _waiters(task: sqlite3.Row, reverse: Mapping[str, Sequence[str]], status: Mapping[str, str],
             waiting_paths: Sequence[sqlite3.Row], approvals: Sequence[sqlite3.Row],
             recorded: Sequence[str], ended: Iterable[str] = ()) -> tuple[str, ...]:
    """What the store shows to be waiting for this task or for the thing it is about.

    Four sources, each a row somebody else wrote: a task that has not ended and depends
    on it; a path of the run that has not run yet and names the task's subject as its
    evaluation specification (by sha256), its capability or its skill (by id); a pending
    approval bound to the subject; and a ``dependencies`` row with relation ``waits_on``
    the reactor wrote when it knew the waiter (``recorded``, already filtered to waiters
    that still wait). A waiter nobody recorded is not found here. ``ended``: tasks BLOCKED
    with their chain ended, which wait for nothing.
    """
    over = set(ended)
    found = [f"task:{other}" for other in sorted(reverse.get(task["task_id"], ()))
             if other in status and status[other] not in TASK_TERMINAL and other not in over]
    subject = (task["subject_kind"], task["subject_id"], task["subject_sha256"])
    for path in waiting_paths:
        if _path_names(path, *subject):
            found.append(f"path:{path['path_id']}")
    for approval in approvals:
        if same_subject(subject, (approval["subject_kind"], approval["subject_id"], approval["subject_sha256"])):
            found.append(f"approval:{approval['approval_id']}")
    found.extend(recorded)
    return tuple(sorted(set(found)))  # sorted: a row read in another order names the same first waiter


def same_subject(one: Sequence[Any], other: Sequence[Any]) -> bool:
    """Two subjects ``(kind, id, sha256)`` name the same thing. Pure.

    The same kind, and an id or a sha256 that both carry and that is equal; where both carry
    the other of the two, it must not differ (another id is another thing, another sha256
    another version). An evaluation specification or an asset at a version is identified by
    its sha256, so a subject need not carry an id (the lead's ruling of 2026-11-12T20:36Z).
    """
    (kind, name, sha), (other_kind, other_name, other_sha) = one, other
    if not kind or kind != other_kind:
        return False
    if (name and other_name and name != other_name) or (sha and other_sha and sha != other_sha):
        return False
    return bool((name and other_name) or (sha and other_sha))


def _path_names(path: sqlite3.Row, subject_kind: str | None, subject_id: str | None,
                subject_sha: str | None) -> bool:
    """A path names the subject: its specification by sha256; an asset by id at any version, or, when the
    subject carries no id, by the sha256 of the manifest a step was planned against or of a skill it cites."""
    if subject_kind == "eval_spec":
        return bool(subject_sha) and path["eval_spec_sha256"] == subject_sha
    if subject_kind != "asset":
        return False
    for step in _json(path["capability_refs_json"], []) or []:
        if not isinstance(step, Mapping):
            continue
        if subject_id:
            if str(step.get("capability_version", "")).split("@", 1)[0] == subject_id:
                return True
        elif subject_sha and step.get("manifest_sha256") == subject_sha:
            return True
    for skill in _json(path["skill_refs_json"], []) or []:
        if not isinstance(skill, Mapping):
            continue
        if subject_id:
            if skill.get("asset_id") == subject_id:
                return True
        elif subject_sha and skill.get("sha256") == subject_sha:
            return True
    return False


__all__ = [
    "AUTHORED_REVIEW_KINDS", "ApprovalFacts", "BLOCKED_REASONS", "BudgetLine", "ENDED_REASONS", "EVENT_EXPANDED",
    "EVENT_PATH_EVALUATED",
    "EVENT_ROUND_CLOSED", "EVENT_ROUTED", "HOLD_REASONS", "JobFacts", "Limits", "PathFacts", "RELATION_FOLLOWS",
    "RELATION_INDEPENDENT_OF", "RELATION_WAITS_ON",
    "RoundFacts", "RoutedFacts", "RunFacts", "STANDINGS", "STANDING_INCONCLUSIVE", "STANDING_OUTCOME",
    "STANDING_UNTESTED", "Snapshot", "TaskFacts", "WAITING_NEED", "ancestors", "chain_depths", "count_overtaken",
    "deadline_passed", "instant",
    "lease_is_live", "lineage_gaps", "load", "same_subject", "transitive_dependents",
]
