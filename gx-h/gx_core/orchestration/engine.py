"""The executable workflow engine: real state, real branching, real interrupts.

What this module is for
-----------------------
The Python backend genuinely executes the workflow. It is not "a flow chart the model
is asked to guess its way through", and it is equally not a hidden caller of some
chat-internal ``spawn_agent``. When the graph reaches work that needs intelligence, the
engine **returns an interrupt** describing the pending tasks and the artifacts they must
produce, and the tool call ends. The Main Agent does that work with its host's own
delegation, the products come back through ``gx_task_submit``, and ``resume``
re-validates the authoritative state before advancing. See the design reference §14.1.

Three consequences are load-bearing, and each one is tested:

* **Nothing blocks.** ``start`` and ``resume`` return as soon as external work is
  needed. No engine code waits for a Main Agent to think.
* **A resumed node re-executes from its beginning.** That is the interrupt semantics
  of every checkpointed graph engine, LangGraph included, and it is why every side
  effect a node performs before interrupting must go through :meth:`NodeContext.once`.
  ``tests/unit/test_engine.py`` proves non-duplication with a counter.
* **Preconditions are re-checked by the backend, never asserted by the caller.** A
  resume is refused when its interrupt is not the pending one, when the revision is
  stale, when a required task is not ACCEPTED, or when an approval is still PENDING.
  The contract forbids an arbitrary ``goto`` or a caller-supplied ``approved=true``,
  so :meth:`Engine.resume` has no such parameter and refuses any extra keyword.

Why a hand-written engine and not LangGraph
-------------------------------------------
The design reference makes LangGraph conditional ("如果採用 LangGraph ...") and the
autonomous prompt forbids rewriting the project around a framework. Three concrete
reasons to keep this engine:

1. The control plane is stdlib-only Python 3.11 and this round permits no installs, so
   a LangGraph dependency could not be installed, pinned, licence-checked or reviewed.
   A dependency we cannot verify is not a shortcut, it is an unverified claim.
2. The authority table (§2) puts run/path/task state in the GX services over one store.
   A framework carrying its own checkpoint store would be a second place that holds "the
   real state" and can be rewritten independently — precisely the failure §2 forbids.
3. What a graph framework would give us here — topological order with cycle detection,
   durable checkpoints, interrupt/resume, version pinning — is a few hundred lines over
   a store that already provides transactions, revisions, leases and checkpoints.

So the engine is small and auditable, and the checkpointer is kept behind
:class:`Checkpointer`. A LangGraph-backed checkpointer (or any other) can be
substituted without touching a single node function; the engine never talks to
``store.save_checkpoint`` directly.
"""

from __future__ import annotations

import functools
import inspect
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import config, contracts, metrics
from ..context import Ctx
from ..domain import clock, ids, machines
from ..errors import (
    ApprovalRequired,
    ArtifactNotFound,
    BudgetExceeded,
    ConcurrentRun,
    ContentHashMismatch,
    DependencyUnmet,
    EvaluatorInvalid,
    GxError,
    IllegalTransition,
    OutcomeUnknown,
    SchemaInvalid,
    StaleRevision,
)
from ..persistence import store
from ..teams import recovery
from . import branches, seams

RUN = "run"

#: Events of a run that are no change a node could act on (design r2.2 §2.5.4, no spinning):
#: the coordinator renewing, taking over or releasing its lease, and the reactor's record of
#: a reaction that failed or was refused. Counted as changes, a coordinator that renews its
#: lease before each resume would run a waiting node again every time it asked.
NOT_A_CHANGE = ("run.coordinator", "run.coordinator_lease", "schedule.reaction_failed",
                "schedule.reaction_refused", "workflow.node_waiting", "workflow.stage_entered",
                "run.resume_recovered")
DRIVE_STATES = ("ACTIVE", *sorted(machines.waiting_states(RUN)))

# --------------------------------------------------------------------- interrupt kinds

#: Interrupt kind -> the run state the engine parks in while that work happens outside.
#: The values are the run machine's declared waiting states; :func:`_check_kind_map`
#: fails loudly at import if the contract grows a waiting state this table does not
#: cover, so the mapping cannot drift away from ``contracts/states.json`` unnoticed.
INTERRUPT_KIND_WAITING_STATE: dict[str, str] = {
    "client_work": "WAITING_CLIENT",
    "external_job": "WAITING_EXTERNAL_JOB",
    "approval": "WAITING_APPROVAL",
    "stage": "WAITING_CLIENT",
}


def _check_kind_map() -> None:
    declared = machines.waiting_states(RUN)
    mapped = set(INTERRUPT_KIND_WAITING_STATE.values())
    if mapped != declared:
        raise RuntimeError(
            "interrupt kinds no longer cover the run machine's waiting states: "
            f"mapped={sorted(mapped)} declared={sorted(declared)}"
        )


_check_kind_map()


def interrupt_kinds() -> list[str]:
    return sorted(INTERRUPT_KIND_WAITING_STATE)


#: The approval state that clears a WAITING_APPROVAL interrupt: the one word the approval
#: machine has for an approval that holds (``contracts/states.json`` → approval.rules.
#: vocabulary). v1 waited here for ``APPROVED`` while the operator surface wrote
#: ``GRANTED``, so an approved run stayed waiting. Granting is an operator action or the
#: user's standing policy, never an engine action.
APPROVAL_GRANTED = "GRANTED"
if APPROVAL_GRANTED not in machines.all_states("approval"):  # pragma: no cover - contract drift
    raise RuntimeError("the approval machine no longer has the state the engine waits for")

# -------------------------------------------------------------------------- interrupt

@dataclass(frozen=True)
class InterruptRequest:
    """Work the backend cannot do itself, described well enough to be delegated.

    ``tasks``/``job_refs``/``approval_ref`` are not decoration: :meth:`Engine.resume`
    re-reads exactly these from the store to decide whether the interrupt may clear. An
    interrupt with nothing to wait for would clear vacuously, so it is refused here —
    an empty ∀-check passing silently is how a gate stops gating.
    """

    kind: str
    reason: str
    tasks: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    approval_ref: str | None = None
    job_refs: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(self.tasks or ()))
        object.__setattr__(self, "job_refs", tuple(self.job_refs or ()))
        object.__setattr__(self, "required_artifacts", tuple(self.required_artifacts or ()))
        object.__setattr__(self, "payload", dict(self.payload or {}))
        if self.kind not in INTERRUPT_KIND_WAITING_STATE:
            raise SchemaInvalid(
                f"unknown interrupt kind {self.kind!r}; known: {interrupt_kinds()}",
                details={"kind": self.kind, "known": interrupt_kinds()},
            )
        if not self.reason:
            raise SchemaInvalid("an interrupt must say why it is waiting", details={"kind": self.kind})
        if self.kind == "client_work" and not self.tasks:
            raise SchemaInvalid(
                "a client_work interrupt with no tasks would clear without anything being "
                "done; create the tasks first",
                details={"kind": self.kind},
            )
        if self.kind == "external_job" and not self.job_refs:
            raise SchemaInvalid(
                "an external_job interrupt must name the jobs it waits for",
                details={"kind": self.kind},
            )
        if self.kind == "approval" and not self.approval_ref:
            raise SchemaInvalid(
                "an approval interrupt must name the approval it waits for",
                details={"kind": self.kind},
            )
        if self.kind == "stage":
            waiting = self.payload.get("waiting_state")
            if not self.payload.get("stage") or waiting not in machines.waiting_states(RUN):
                raise SchemaInvalid("a stage interrupt names its stage and a derived waiting state",
                                    details={"payload": dict(self.payload)})

    @property
    def waiting_state(self) -> str:
        if self.kind == "stage":
            return str(self.payload["waiting_state"])
        return INTERRUPT_KIND_WAITING_STATE[self.kind]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "tasks": list(self.tasks),
            "required_artifacts": list(self.required_artifacts),
            "approval_ref": self.approval_ref,
            "job_refs": list(self.job_refs),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InterruptRequest":
        return cls(
            kind=data["kind"],
            reason=data.get("reason", ""),
            tasks=tuple(data.get("tasks", ())),
            required_artifacts=tuple(data.get("required_artifacts", ())),
            approval_ref=data.get("approval_ref"),
            job_refs=tuple(data.get("job_refs", ())),
            payload=data.get("payload", {}),
        )


@dataclass(frozen=True)
class PauseRequest:
    """A node cannot go on until something is cleared, and says what (design r2.2 §2.8.2).

    ``needs`` are in scheduling's closed vocabulary, each ``{kind, subject?, what, why,
    clear_by}``. They are not decoration: ``gx_run_control continue`` evaluates each kind's
    clearing predicate and refuses while one is uncleared. A pause that names nothing to
    clear would be a stop nobody could lift, so it is refused here: such a run stops.
    """

    reason: str
    needs: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        from ..scheduling import needs as vocabulary   # data and two pure functions; no cycle at call time

        object.__setattr__(self, "needs", tuple(dict(item) for item in (self.needs or ())))
        if not self.reason:
            raise SchemaInvalid("a pause must say why the run cannot go on")
        if not self.needs:
            raise SchemaInvalid(
                "a pause names what a person or the machine can clear; a run that nothing can "
                "clear is stopped, with its reason",
                details={"reason": self.reason},
            )
        problems = [f"need {index}: {problem}" for index, item in enumerate(self.needs)
                    for problem in vocabulary.problems(item, recorded=True)]
        if problems:
            raise SchemaInvalid("a pause's needs are of the closed vocabulary: " + "; ".join(problems[:6]),
                                details={"problems": problems})


@dataclass(frozen=True)
class StopRequest:
    """A node ends the run, with its reason (design r2.2 §2.8.3). ``rule`` names the rule
    that decided, when one did (``no_tested_path``, ``no_progress``, ...)."""

    reason: str
    rule: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise SchemaInvalid("a stop must say why the run ends")


@dataclass(frozen=True)
class NodeResult:
    """What a node returns: outputs, an interrupt, a request to go round again, a pause or a stop.

    ``reenter`` is how the discovery loop is a loop. The graph stays a DAG — cycles are
    still refused at definition time, so the order remains auditable — and iteration is
    an explicit, bounded, logged **rewind** of the completed set, decided by the backend
    from authoritative state. The three properties that keep it from being an arbitrary
    ``goto``: only a node can request it (never a caller), the target must lie *before*
    the requesting node in topological order, and the number of rounds is capped by
    ``research_budget.max_literature_replanning_rounds``, re-read at the time.
    """

    outputs: Mapping[str, Any] | None = None
    interrupt: InterruptRequest | None = None
    reenter: str | None = None
    reason: str = ""
    #: The run cannot go on until a need is cleared: it is PAUSED at this node and
    #: ``continue`` runs the node again once every need reads clear (§2.8.2, §2.8.4).
    pause: PauseRequest | None = None
    #: The run ends here, STOPPED with the reason: its open tasks are cancelled and what it
    #: still held is given back. Never COMPLETED, whatever evidence the run holds (§2.8.4).
    stop: StopRequest | None = None

    def __post_init__(self) -> None:
        if self.pause is not None and self.stop is not None:
            raise SchemaInvalid("a node pauses the run or stops it, not both")
        if (self.pause is not None or self.stop is not None) and (
                self.outputs is not None or self.interrupt is not None or self.reenter is not None):
            raise SchemaInvalid(
                "a node that pauses or stops the run returns nothing else: it has no outputs, "
                "waits for no outside work and rewinds nothing",
            )
        if self.outputs is not None and self.interrupt is not None:
            raise SchemaInvalid(
                "a node either produced outputs or needs external work, not both; the outputs "
                "of an interrupting node would be recomputed on resume anyway",
            )
        if self.interrupt is not None and self.reenter is not None:
            raise SchemaInvalid(
                "a node cannot both wait for outside work and rewind the graph; finish the "
                "round first, then decide whether to go round again",
            )
        if self.reenter is not None and not self.reason:
            raise SchemaInvalid(
                "a rewind must state its trigger: the design reference requires the "
                "conditions for returning to the literature to be explicit (§16)",
            )


def pause(needs: Iterable[Mapping[str, Any]], *, reason: str) -> NodeResult:
    """The result of a node that cannot go on until ``needs`` are cleared."""
    return NodeResult(pause=PauseRequest(reason=reason, needs=tuple(needs)))


def stop(reason: str, *, rule: str | None = None) -> NodeResult:
    """The result of a node that ends the run."""
    return NodeResult(stop=StopRequest(reason=reason, rule=rule))


@dataclass(frozen=True)
class Standing:
    """Where a run's pending interrupt stands (design r2.2 §2.5.1, §2.5.2, §2.5.4).

    One function computes it (:meth:`Engine.standing`) and a resume, a status and a
    dispatch projection all read it, so what a status says a resume would do is what a
    resume does.
    """

    #: every item is what the node waits for: tasks ACCEPTED, the approval GRANTED, jobs ended
    complete: bool
    #: every item has reached a terminal outcome, good or not: there is something to hand back
    handed_back: bool
    #: not complete, and nothing of the run is open, pending or waited for
    quiescent: bool
    #: something of the run, or config/limits.json, changed since the node last ran
    changed: bool
    #: the refusal a resume answers while it is not resumable; None when it is
    waiting: GxError | None = None
    #: item -> its state, for the items that are not what the node waits for
    outstanding: Mapping[str, Any] = field(default_factory=dict)
    #: Needs reported by the stage rule in the same observation as its completion decision.
    stage_needs: Sequence[Mapping[str, Any]] | None = None

    @property
    def resumable(self) -> bool:
        return self.waiting is None


# ------------------------------------------------------------------------------ nodes

@dataclass(frozen=True)
class Node:
    """One unit of the graph.

    ``idempotent=False`` declares that re-running this node would duplicate something.
    Such a node may not interrupt, because resuming re-executes it from the beginning;
    the engine refuses at runtime rather than quietly doubling the side effect.

    ``work`` records whether the node is mechanical (validation, fingerprint dedup,
    budget check, state transitions — executed in Python), a gate (mechanical, and it
    may wait for one thing only: an approval) or needs intelligence (it interrupts and
    the host delegates). Declaring it makes the distinction enforceable: a node declared
    ``mechanical`` that returns an interrupt is refused, and so is a ``gate`` that asks
    for anything but an approval, so the label in a workflow definition cannot quietly
    become fiction. ``None`` means undeclared.
    """

    WORK_KINDS = ("mechanical", "gate", "intelligent", "stage")

    name: str
    fn: Callable[["NodeContext"], NodeResult]
    deps: tuple[str, ...] = ()
    idempotent: bool = True
    work: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "deps", tuple(self.deps or ()))
        if self.work is not None and self.work not in self.WORK_KINDS:
            raise SchemaInvalid(
                f"node {self.name!r} declares work={self.work!r}; expected one of "
                f"{list(self.WORK_KINDS)}",
                details={"node": self.name, "work": self.work},
            )
        if not self.name or not isinstance(self.name, str):
            raise SchemaInvalid("a node needs a name")
        if not callable(self.fn):
            raise SchemaInvalid(f"node {self.name!r} has no callable", details={"node": self.name})

    def fn_ref(self) -> str:
        fn = self.fn
        module = getattr(fn, "__module__", type(fn).__module__)
        qualname = getattr(fn, "__qualname__", type(fn).__qualname__)
        return f"{module}.{qualname}"


def module_sources(functions: Iterable[Callable[..., Any]]) -> dict[str, str | None]:
    """``module name -> sha256 of its source file`` for the modules a set of functions live in.

    Keyed by module name, never by path, so a tree copied to another root has the same
    answer. ``None`` for a module whose source cannot be read (defined in an interactive
    session, built from a string): such a function can run, and nothing can say what it
    is, so a workflow that has one is never admitted as shipped.
    """
    out: dict[str, str | None] = {}
    for fn in functions:
        while isinstance(fn, functools.partial):
            fn = fn.func
        name = getattr(fn, "__module__", None) or type(fn).__module__
        if name in out:
            continue
        module = sys.modules.get(name)
        path = None
        if module is not None:
            try:
                path = inspect.getsourcefile(module) or getattr(module, "__file__", None)
            except TypeError:
                path = getattr(module, "__file__", None)
        out[name] = ids.sha256_of_file(path) if path and os.path.isfile(path) else None
    return dict(sorted(out.items()))


CompletionEvidence = Callable[[sqlite3.Connection, sqlite3.Row], tuple[bool, list[str]]]
ObjectiveCoverage = Callable[[sqlite3.Connection, str], Mapping[str, Any]]


class Workflow:
    """A versioned graph. Topologically ordered at construction; cycles are refused."""

    def __init__(
        self,
        workflow_id: str,
        version: str,
        nodes: Sequence[Node],
        *,
        entry_transitions: Sequence[str] = ("PLANNING", "ACTIVE"),
        description: str = "",
        document: Mapping[str, Any] | None = None,
        stage_rules: Mapping[str, Callable[..., Mapping[str, Any]]] | None = None,
        completion_evidence: CompletionEvidence | None = None,
        objective_coverage: ObjectiveCoverage | None = None,
        task_product_validator: Callable[..., None] | None = None,
    ) -> None:
        if not workflow_id or not version:
            raise SchemaInvalid("a workflow needs an id and a version")
        self.workflow_id = workflow_id
        self.version = str(version)
        from .hooks import DEFAULT_HOOKS
        self.hooks = DEFAULT_HOOKS
        self.hook_bindings = None
        self.description = description
        #: The definition as it was written (the shipped JSON), when there is one.
        self.document: dict[str, Any] | None = dict(document) if document is not None else None
        self.nodes: dict[str, Node] = {}
        for node in nodes:
            if node.name in self.nodes:
                raise SchemaInvalid(
                    f"duplicate node {node.name!r} in {workflow_id}@{version}",
                    details={"node": node.name},
                )
            self.nodes[node.name] = node
        self.entry_transitions = tuple(entry_transitions)
        self._validate_entry_transitions()
        self.order: tuple[str, ...] = tuple(self._topological_order())
        self.stage_rules = dict(stage_rules or {})
        for name, rule in self.stage_rules.items():
            if name not in self.nodes or self.nodes[name].work != "stage" or not callable(rule):
                raise SchemaInvalid(f"{name!r} is not a declared stage with a callable completion rule")
        missing = [node.name for node in self.nodes.values()
                   if node.work == "stage" and node.name not in self.stage_rules]
        if missing:
            raise SchemaInvalid("a stage needs a read-only completion rule", details={"stages": missing})
        #: Read once, when the workflow is built: this is the code this process loaded and
        #: will run. A file edited afterwards changes what a *new* process pins and
        #: compares, not what this one does.
        self.sources: dict[str, str | None] = module_sources(node.fn for node in self.nodes.values())
        self.sources.update(module_sources(self.stage_rules.values()))
        self.objective_coverage = objective_coverage
        if objective_coverage is not None:
            if not callable(objective_coverage) or not getattr(objective_coverage, "__qualname__", None):
                raise SchemaInvalid("objective coverage needs a named read-only function")
            self.sources.update(module_sources([objective_coverage]))
        self.task_product_validator = task_product_validator
        if task_product_validator is not None:
            self.sources.update(module_sources([task_product_validator]))
        # The read-only rule returns (complete, missing reasons) for this run.
        # Its binding is part of the workflow's pin only when the workflow declares one.
        self.completion_evidence = completion_evidence
        if completion_evidence is not None:
            if not callable(completion_evidence) or not getattr(completion_evidence, "__qualname__", None):
                raise SchemaInvalid("a workflow completion rule needs a named callable")
            self.sources.update(module_sources([completion_evidence]))

    # -- construction checks -------------------------------------------------------
    def _validate_entry_transitions(self) -> None:
        state = machines.initial_state(RUN)
        for target in self.entry_transitions:
            machines.check_transition(RUN, state, target, subject_id=self.workflow_id)
            state = target
        if state in machines.terminal_states(RUN):
            raise SchemaInvalid(
                f"workflow {self.workflow_id} would enter the terminal state {state!r}",
                details={"entry_transitions": list(self.entry_transitions)},
            )

    def _topological_order(self) -> list[str]:
        unknown = {
            (name, dep)
            for name, node in self.nodes.items()
            for dep in node.deps
            if dep not in self.nodes
        }
        if unknown:
            raise SchemaInvalid(
                f"{self.workflow_id}@{self.version} depends on undeclared nodes: "
                + ", ".join(f"{n}->{d}" for n, d in sorted(unknown)),
                details={"unknown": sorted(unknown)},
            )
        remaining = {name: set(node.deps) for name, node in self.nodes.items()}
        order: list[str] = []
        # Declaration order among ready nodes, so the execution order is deterministic.
        declared = list(self.nodes)
        while remaining:
            ready = [name for name in declared if name in remaining and not remaining[name]]
            if not ready:
                raise SchemaInvalid(
                    f"{self.workflow_id}@{self.version} has a dependency cycle among: "
                    + ", ".join(sorted(remaining)),
                    details={"cycle_candidates": sorted(remaining),
                             "edges": {n: sorted(d) for n, d in remaining.items()}},
                )
            for name in ready:
                order.append(name)
                del remaining[name]
                for deps in remaining.values():
                    deps.discard(name)
        return order

    # -- identity ------------------------------------------------------------------
    def definition(self) -> dict[str, Any]:
        definition = {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "entry_transitions": list(self.entry_transitions),
            "nodes": [
                {
                    "name": name,
                    "deps": list(self.nodes[name].deps),
                    "idempotent": self.nodes[name].idempotent,
                    "work": self.nodes[name].work,
                    "fn": self.nodes[name].fn_ref(),
                }
                for name in self.order
            ],
        }
        if self.stage_rules:
            definition["stage_rules"]={name:f"{fn.__module__}.{fn.__qualname__}"
                                       for name,fn in sorted(self.stage_rules.items())}
        if self.hook_bindings is not None:
            definition["workflow_hooks"] = self.hook_bindings
        if self.task_product_validator is not None:
            fn = self.task_product_validator
            definition["task_product_validator"] = f"{fn.__module__}.{fn.__qualname__}"
        if self.objective_coverage is not None:
            rule = self.objective_coverage
            definition["objective_coverage"] = f"{rule.__module__}.{rule.__qualname__}"
        if self.completion_evidence is not None:
            rule = self.completion_evidence
            definition["completion_evidence"] = f"{rule.__module__}.{rule.__qualname__}"
        return definition

    def digest(self) -> str:
        """The workflow's sha256: its definition and the source of the modules that implement it.

        v1 hashed the graph and the *names* of the node functions, so a node whose body
        changed kept its digest: neither a run's pin nor a shipped pin could notice. The
        sources are part of it now (``contracts/states.json`` → run.rules.pins).
        """
        return ids.sha256_of_obj({"definition": self.definition(), "document": self.document,
                                  "sources": self.sources})

    def sources_complete(self) -> bool:
        return all(sha256 is not None for sha256 in self.sources.values())

    def node(self, name: str) -> Node:
        if name not in self.nodes:
            raise SchemaInvalid(
                f"{self.workflow_id}@{self.version} has no node {name!r}",
                details={"node": name, "known": list(self.order)},
            )
        return self.nodes[name]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Workflow {self.workflow_id}@{self.version} nodes={len(self.nodes)}>"


# ------------------------------------------------------------------------ checkpointer

@dataclass(frozen=True)
class CheckpointView:
    checkpoint_id: str
    run_id: str
    workflow_id: str
    workflow_version: str
    node: str
    seq: int
    state: dict[str, Any]
    interrupt_ref: str | None
    created_at: str


class Checkpointer:
    """Durable position of a run. Swap this to change where checkpoints live.

    Deliberately narrow: ``save`` and ``latest``. The engine never reaches past it,
    which is what makes a LangGraph-backed (or Redis-backed, or file-backed)
    implementation a drop-in rather than a rewrite.
    """

    def save(
        self, conn: sqlite3.Connection, *, run_id: str, workflow_id: str, workflow_version: str,
        node: str, state: Mapping[str, Any], interrupt_ref: str | None = None,
    ) -> str:
        raise NotImplementedError

    def latest(self, conn: sqlite3.Connection, run_id: str) -> CheckpointView | None:
        raise NotImplementedError


class StoreCheckpointer(Checkpointer):
    """The default: the authoritative SQLite store, inside the engine's transaction."""

    def save(
        self, conn: sqlite3.Connection, *, run_id: str, workflow_id: str, workflow_version: str,
        node: str, state: Mapping[str, Any], interrupt_ref: str | None = None,
    ) -> str:
        return store.save_checkpoint(
            conn, run_id=run_id, workflow_id=workflow_id, workflow_version=workflow_version,
            node=node, state=dict(state), interrupt_ref=interrupt_ref,
        )

    def latest(self, conn: sqlite3.Connection, run_id: str) -> CheckpointView | None:
        row = store.latest_checkpoint(conn, run_id)
        if row is None:
            return None
        return CheckpointView(
            checkpoint_id=row["checkpoint_id"],
            run_id=row["run_id"],
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            node=row["node"],
            seq=int(row["seq"]),
            state=store.loads(row, "state_json", {}) or {},
            interrupt_ref=row["interrupt_ref"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------- node context

START_NODE = "__start__"
END_NODE = "__end__"

#: How long the one process advancing a run may go without saving a checkpoint before
#: another may take over. A run has one advancer (SCHEDULING_DESIGN r2 §2.5.3): the lease
#: is written into the checkpoint in the transaction that starts, resumes or continues the
#: run, re-checked before every node, and dropped when the run parks, pauses or ends. A
#: process that dies mid-advance leaves a lease that lapses, after which ``continue`` drives
#: the run from its last checkpoint.
ADVANCE_LEASE_SECONDS = 600


#: The machine this process runs on. The store is for one host at a time (design §4), so a
#: lease taken here names a process this machine can look up.
_HOST = os.uname().nodename


def _new_advance(actor: str) -> dict[str, Any]:
    return {"id": ids.new_id("lease"), "by": actor, "until": clock.iso_after(ADVANCE_LEASE_SECONDS),
            "pid": os.getpid(), "host": _HOST}


def _advance_live(state: Mapping[str, Any]) -> bool:
    """Whether somebody holds the run's advance lease now.

    A lease lapses at its time, or at once when the process that took it, on this machine,
    is gone: a killed advancer is not waited out for ten minutes.
    """
    lease = state.get("advance") or {}
    if not lease.get("id") or clock.is_past(str(lease.get("until") or "")):
        return False
    pid = lease.get("pid")
    if lease.get("host") == _HOST and isinstance(pid, int) and not isinstance(pid, bool):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    return True


class NodeContext:
    """What a node function is handed. Thin on purpose, so nodes stay injectable."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        run_id: str,
        workflow_id: str,
        workflow_version: str,
        node: str,
        actor: str,
        inputs: Mapping[str, Any],
        outputs: Mapping[str, Any],
        state: dict[str, Any],
        submissions: Sequence[str],
        resumed: bool,
        revision: int,
        round_index: int = 0,
        rounds_left: int = 0,
        budget: Mapping[str, Any] | None,
        teams: Any | None = None,
        services: Mapping[str, Any] | None = None,
        gx: Ctx | None = None,
    ) -> None:
        self.conn = conn
        #: Who asked for the call that is advancing the run (run start or resume). What a
        #: node creates on the way -- tasks, approvals -- is created for that caller.
        self.gx = gx
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.workflow_version = workflow_version
        self.node = node
        self.actor = actor
        self.inputs = dict(inputs)
        self.outputs = dict(outputs)
        self.state = state
        self.submissions = tuple(submissions)
        self.resumed = resumed
        self.revision = revision
        #: 0 on the first pass; 1 after one rewind. Scopes ``once`` keys, so a second
        #: round creates new tasks instead of replaying the first round's.
        self.round = round_index
        self.rounds_left = rounds_left
        self.budget = dict(budget or {})
        self.teams = teams
        self.services = dict(services or {})

    # -- idempotent side effects ---------------------------------------------------
    def once(self, key: str, fn: Callable[[], Any]) -> Any:
        """Perform a side effect at most once for this (run, node, key).

        A node that interrupted is re-executed from its beginning on resume. Anything
        it did before interrupting — creating tasks, submitting a job, writing a file —
        must be wrapped here, or the resume duplicates it. The guard is the kernel's
        idempotency table, so it survives a process restart, not just a retry loop.

        The key is scoped by ``round``. Without that, a second pass after a rewind would
        replay the first pass's stored value: ``collect_evidence`` would "create" the
        same tasks, find them already ACCEPTED, and sail past the interrupt without
        collecting anything — a loop that looks like it ran and did nothing.
        """
        request = {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "node": self.node,
            "round": self.round,
            "key": key,
        }
        idem_key = f"node:{self.run_id}:{self.node}:r{self.round}:{key}"
        record = store.begin_idempotent(self.conn, idem_key, f"workflow_node:{self.node}", request)
        if record.replay:
            return (record.response or {}).get("value")
        if not record.fresh:
            raise OutcomeUnknown(
                f"side effect {key!r} of node {self.node!r} was claimed but never completed; "
                "reconcile before retrying",
                details={"run_id": self.run_id, "node": self.node, "key": key,
                         "side_effect_status": record.side_effect_status},
            )
        value = fn()
        store.complete_idempotent(self.conn, idem_key, {"value": value})
        return value

    # -- authoritative reads -------------------------------------------------------
    def task_states(self, task_ids: Iterable[str]) -> dict[str, str | None]:
        wanted = list(task_ids)
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        rows = self.conn.execute(
            f"SELECT task_id, status FROM tasks WHERE task_id IN ({placeholders})", wanted
        ).fetchall()
        found = {row["task_id"]: row["status"] for row in rows}
        return {task_id: found.get(task_id) for task_id in wanted}

    def all_accepted(self, task_ids: Iterable[str]) -> bool:
        """True only when every named task is ACCEPTED. An empty set is never 'done'."""
        states = self.task_states(task_ids)
        if not states:
            return False
        return all(state == "ACCEPTED" for state in states.values())

    def approval_state(self, approval_ref: str) -> str | None:
        return seams.approval_status(self.conn, approval_ref)

    def paths(self, *, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return list(self.conn.execute(
                "SELECT * FROM paths WHERE run_id=? ORDER BY created_at, path_id", (self.run_id,)
            ))
        return list(self.conn.execute(
            "SELECT * FROM paths WHERE run_id=? AND status=? ORDER BY created_at, path_id",
            (self.run_id, status),
        ))

    def log(self, kind: str, payload: Mapping[str, Any] | None = None, *,
            reason: str | None = None, evidence_level: str = "EXECUTION_OBSERVED") -> int:
        return store.append_event(
            self.conn, kind=kind, actor=self.actor, run_id=self.run_id,
            subject_kind="workflow", subject_id=f"{self.workflow_id}@{self.workflow_version}",
            reason=reason, evidence_level=evidence_level,
            payload={"node": self.node, **(payload or {})},
        )


# --------------------------------------------------------------------------- engine

class Engine:
    """Executes a :class:`Workflow` against the authoritative store."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: Any,
        *,
        teams: Any | None = None,
        checkpointer: Checkpointer | None = None,
        services: Mapping[str, Any] | None = None,
        hold_scope: str = "orchestration",
        ctx: Ctx | None = None,
    ) -> None:
        self.conn = conn
        self.registry = registry
        self.ctx = ctx
        self.teams = teams
        self.checkpointer = checkpointer or StoreCheckpointer()
        self.services = dict(services or {})
        self.hold_scope = hold_scope

    # ------------------------------------------------------------------- public API
    def start(
        self, *, run_id: str, workflow_id: str, workflow_version: str,
        inputs: Mapping[str, Any] | None = None, actor: str = "engine",
    ) -> dict[str, Any]:
        config.check_holds(self.hold_scope)
        workflow = self.registry.get(workflow_id, workflow_version)
        state: dict[str, Any] = {
            "workflow_id": workflow.workflow_id,
            "workflow_version": workflow.version,
            "workflow_digest": workflow.digest(),
            "inputs": dict(inputs or {}),
            "completed": [],
            "round": 0,
            "outputs": {},
            "scratch": {},
            "node_waits": {},
            "pending_interrupt": None,
            "interrupt_ref": None,
        }
        with store.transaction(self.conn, transaction_owner="Engine", operation="start", run_id=run_id) as conn:
            # Looked at under the write lock: two calls that both found no checkpoint before
            # taking it would otherwise both start the run, and a run that has gone on to
            # ACTIVE or WAITING_APPROVAL may legally move to PLANNING, so the second start
            # would not even be refused by the machine -- it would run the workflow again.
            existing = self.checkpointer.latest(conn, run_id)
            if existing is not None:
                raise SchemaInvalid(
                    f"run {run_id} already started on {existing.workflow_id}@"
                    f"{existing.workflow_version}; use resume, not start",
                    details={"run_id": run_id, "workflow_id": existing.workflow_id,
                             "workflow_version": existing.workflow_version},
                )
            for target in workflow.entry_transitions:
                store.transition(
                    conn, machine=RUN, subject_id=run_id, to_state=target, actor=actor,
                    reason=f"workflow {workflow.workflow_id}@{workflow.version} start",
                    evidence_level="EXECUTION_OBSERVED",
                    payload={"workflow_id": workflow.workflow_id,
                             "workflow_version": workflow.version},
                )
            state["advance"] = _new_advance(actor)
            self.checkpointer.save(
                conn, run_id=run_id, workflow_id=workflow.workflow_id,
                workflow_version=workflow.version, node=START_NODE, state=state,
            )
        return self._advance(run_id=run_id, workflow=workflow, state=state, actor=actor)

    def resume(
        self, *, run_id: str, interrupt_ref: str, submission_refs: Sequence[Any] = (),
        expected_revision: int | None = None, actor: str = "engine", **unsupported: Any,
    ) -> dict[str, Any]:
        """Advance past a pending interrupt, after the backend re-checks every premise.

        There is deliberately no ``goto`` and no ``approved`` parameter: the contract
        says a caller-supplied authorisation is not an authorisation. Any extra keyword
        is refused by name rather than silently ignored.

        The checks and the move to ACTIVE are one transaction (:meth:`begin_resume`); the
        nodes then run after it has committed. A caller that has more to check under the
        same lock (the run service: the coordinator lease, holds) calls
        :meth:`begin_resume` inside its own transaction and :meth:`advance_resumed` after.
        """
        if unsupported:
            raise SchemaInvalid(
                "gx_run_resume does not accept " + ", ".join(sorted(unsupported))
                + "; the backend re-validates preconditions and will not take a caller's "
                  "word for an approval or jump to an arbitrary node",
                details={"rejected_arguments": sorted(unsupported)},
            )
        with store.transaction(self.conn, transaction_owner="Engine", operation="resume", run_id=run_id) as conn:
            begun = self.begin_resume(conn, run_id=run_id, interrupt_ref=interrupt_ref,
                                      submission_refs=submission_refs,
                                      expected_revision=expected_revision, actor=actor)
        return self.advance_resumed(begun)

    def begin_resume(
        self, conn: sqlite3.Connection, *, run_id: str, interrupt_ref: str,
        submission_refs: Sequence[Any] = (), expected_revision: int | None = None, actor: str = "engine",
    ) -> dict[str, Any]:
        """Everything a resume decides, inside the caller's write transaction (R1, §2.5.3).

        The interrupt's clearance, the caller's ``expected_revision``, the move to ACTIVE
        and a checkpoint with the pending interrupt cleared and this caller's advance lease
        are committed together or not at all. A node that then raises, or a process that
        dies before the next node commits, leaves an ACTIVE run with nothing pending: an
        idle run that ``gx_run_control continue`` drives, never one that no call can move.
        Two resumes of one interrupt are serialised by the write lock: the second finds the
        interrupt gone and is refused.

        A run that older code left ACTIVE with its interrupt still pending (it committed
        ACTIVE and died before the node ran) is accepted here, as what it is.
        """
        if not conn.in_transaction:
            raise SchemaInvalid("begin_resume runs inside the caller's transaction", details={"run_id": run_id})
        refs = self._clean_submission_refs(submission_refs)
        config.check_holds(self.hold_scope)
        checkpoint = self.checkpointer.latest(conn, run_id)
        if checkpoint is None:
            raise SchemaInvalid(
                f"run {run_id} has no checkpoint; it was never started",
                details={"run_id": run_id},
            )
        store.check_revision(conn, run_id, expected_revision)
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        status = row["status"]
        state = dict(checkpoint.state)
        state["node_waits"] = branches.waits(checkpoint)
        half_resumed = status == "ACTIVE" and bool(state["node_waits"])
        if status == "ACTIVE" and _advance_live(state):
            # Contract r5: a second advance while one is in progress answers CONCURRENT_RUN.
            raise ConcurrentRun(
                f"run {run_id} is being advanced by another call; a resume waits until that call ends",
                details={"run_id": run_id, "status": status, "node": checkpoint.node},
            )
        if status not in machines.waiting_states(RUN) and not half_resumed:
            raise IllegalTransition(
                f"run {run_id} is {status}, not waiting for anything; there is nothing to resume",
                details={"run_id": run_id, "status": status,
                         "waiting_states": sorted(machines.waiting_states(RUN))},
            )
        wait = self._wait_by_ref(run_id, state["node_waits"], interrupt_ref)
        name = wait["node"]
        # What the node waits for has come, or has come back as it is: every terminal outcome
        # is handed to the node (design r2.2 §2.5.1), and a run in which nothing is open,
        # pending or waited for is resumed into a decision (§2.5.2). Otherwise it still waits.
        standing = self.standing(run_id, conn=conn, interrupt_ref=interrupt_ref)
        if standing.waiting is not None:
            # Contract r5: refused with the needs still open (each a need of the closed
            # vocabulary; work in flight is waited for and is no need).
            needs = self.waiting_needs(run_id, conn=conn, interrupt_ref=interrupt_ref, stage_needs=standing.stage_needs)
            standing.waiting.details.setdefault("needs", needs)
            raise standing.waiting

        # The run pinned its workflow version at start; a newer version must not change
        # an execution that is already under way (contracts/states.json asset.versioning).
        workflow = self.registry.get(checkpoint.workflow_id, checkpoint.workflow_version)

        if status != "ACTIVE":
            store.transition(
                conn, machine=RUN, subject_id=run_id, to_state="ACTIVE", actor=actor,
                reason=f"resume {interrupt_ref}", evidence_level="EXECUTION_OBSERVED",
                extra_updates={"interrupt_ref": None},
                payload={"interrupt_ref": interrupt_ref, "submission_refs": list(refs),
                         "node": name,
                         "standing": {"complete": standing.complete, "handed_back": standing.handed_back,
                                      "quiescent": standing.quiescent},
                         "outstanding": dict(standing.outstanding)},
            )
        else:
            conn.execute("UPDATE runs SET interrupt_ref=NULL WHERE run_id=?", (run_id,))
            store.append_event(
                conn, kind="run.resume_recovered", actor=actor, run_id=run_id, subject_kind=RUN,
                subject_id=run_id, reason=f"resume {interrupt_ref} of a run left ACTIVE with it pending",
                evidence_level="EXECUTION_OBSERVED",
                payload={"interrupt_ref": interrupt_ref, "node": name},
            )
        del state["node_waits"][name]
        state["pending_interrupt"] = None
        state["interrupt_ref"] = None
        # A crash between clearance and execution still delivers submissions only to
        # this node, including an empty but authorized approval resume.
        reentered = "reenter_round" in wait
        state.setdefault("resumptions", {})[name] = {
            "submissions": [] if reentered else list(refs), "round": wait.get("reenter_round", wait["round"]),
            "reentered": reentered,
        }
        state["advance"] = _new_advance(actor)
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=name, state=state,
        )
        return {"run_id": run_id, "workflow": workflow, "state": state, "actor": actor,
                "submissions": refs}

    def advance_resumed(self, begun: Mapping[str, Any], *, report_branch_failures: bool = False) -> dict[str, Any]:
        """Drive a committed advance. The reactor reports an isolated failure and keeps serving other runs."""
        return self._advance(
            run_id=begun["run_id"], workflow=begun["workflow"], state=dict(begun["state"]),
            actor=begun["actor"], resumed=True, report_branch_failures=report_branch_failures,
        )

    def _release_advance(self, run_id: str, workflow: "Workflow", advance_id: str | None) -> None:
        """Give back this call's advance lease, if the run still carries it."""
        if not advance_id:
            return
        with store.transaction(
            self.conn, transaction_owner="Engine", operation="release_advance", run_id=run_id,
        ) as conn:
            latest = self.checkpointer.latest(conn, run_id)
            if latest is None or (latest.state.get("advance") or {}).get("id") != advance_id:
                return
            kept = dict(latest.state)
            kept.pop("advance", None)
            self.checkpointer.save(
                conn, run_id=run_id, workflow_id=workflow.workflow_id,
                workflow_version=workflow.version, node=latest.node, state=kept,
                interrupt_ref=latest.interrupt_ref,
            )

    def waits(self, run_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, dict]:
        """All current branch waits with their stable references; reads old checkpoints too."""
        return branches.waits(self.checkpointer.latest(conn or self.conn, run_id))

    @staticmethod
    def _wait_by_ref(run_id, waits, ref):
        for wait in waits.values():
            if ref and wait["interrupt_ref"] == ref:
                return wait
        raise IllegalTransition(f"run {run_id} has no pending interrupt {ref!r}",
                                details={"run_id": run_id, "supplied_interrupt_ref": ref,
                                         "pending_interrupt_refs": [w["interrupt_ref"] for w in waits.values()]})

    def pending_for(self, run_id, interrupt_ref, *, conn=None):
        return self._wait_by_ref(run_id, self.waits(run_id, conn=conn), interrupt_ref)["interrupt"]

    def idle(self, run_id: str, *, retry_failed: bool = True) -> bool:
        """An unowned advance can execute, recover its last step, or park its recorded waits."""
        row = self._run_row(run_id)
        checkpoint = self.checkpointer.latest(self.conn, run_id)
        if checkpoint is None or _advance_live(checkpoint.state):
            return False
        active = row["status"] == "ACTIVE"
        if not active and row["status"] not in machines.waiting_states(RUN):
            return False
        state = checkpoint.state
        waits = branches.waits(checkpoint)
        if not waits and not state.get("failed_nodes"):
            return active
        if retry_failed and state.get("failed_nodes"):
            return True
        if active and waits and not state.get("pending_interrupt"):
            return True  # killed between recording a branch wait and parking the Run
        if not self.registry.has(checkpoint.workflow_id, checkpoint.workflow_version):
            return False
        workflow = self.registry.get(checkpoint.workflow_id, checkpoint.workflow_version)
        return "READY" in branches.node_states(workflow, {**state, "node_waits": waits}).values()

    def advance(self, *, run_id: str, actor: str = "engine", retry_failed: bool = True) -> dict[str, Any]:
        """Drive an idle run on from its last checkpoint. Completed nodes are not redone.

        The idle check and the taking of the advance lease are one transaction, so two
        callers that both found the run idle cannot both drive it.
        """
        with store.transaction(
            self.conn, transaction_owner="Engine", operation="begin_advance", run_id=run_id,
        ) as conn:
            begun = self.begin_advance(conn, run_id=run_id, actor=actor, retry_failed=retry_failed)
        return self.advance_resumed(begun)

    def begin_advance(self, conn: sqlite3.Connection, *, run_id: str, actor: str = "engine",
                      retry_failed: bool = True) -> dict[str, Any]:
        """Claim an idle advance inside the same transaction as the caller's policy checks."""
        if not conn.in_transaction:
            raise SchemaInvalid("begin_advance requires a write transaction", details={"run_id": run_id})
        config.check_holds(self.hold_scope)
        checkpoint = self.checkpointer.latest(conn, run_id)
        if checkpoint is None:
            raise SchemaInvalid(f"run {run_id} has no checkpoint; it was never started",
                                details={"run_id": run_id})
        if not self.idle(run_id, retry_failed=retry_failed):
            row = self._run_row(run_id)
            raise IllegalTransition(
                f"run {run_id} is {row['status']}"
                + (" and waiting on an interrupt; it continues with gx_run_resume"
                   if checkpoint.state.get("pending_interrupt")
                   else " and another call is advancing it" if _advance_live(checkpoint.state)
                   else "; there is nothing to drive on"),
                details={"run_id": run_id, "status": row["status"],
                         "interrupt_ref": checkpoint.state.get("interrupt_ref")},
            )
        workflow = self.registry.get(checkpoint.workflow_id, checkpoint.workflow_version)
        state = dict(checkpoint.state)
        state["node_waits"] = branches.waits(checkpoint)
        if self._run_row(run_id)["status"] != "ACTIVE":
            store.transition(conn, machine=RUN, subject_id=run_id, to_state="ACTIVE", actor=actor,
                             reason="continue an independent workflow branch",
                             extra_updates={"interrupt_ref": None}, evidence_level="EXECUTION_OBSERVED")
        elif state.get("interrupt_ref"):
            conn.execute("UPDATE runs SET interrupt_ref=NULL WHERE run_id=?", (run_id,))
        state.update(pending_interrupt=None, interrupt_ref=None)
        if retry_failed:
            state.pop("failed_nodes", None)
        state["advance"] = _new_advance(actor)
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=checkpoint.node, state=state,
        )
        return {"run_id": run_id, "workflow": workflow, "state": state, "actor": actor, "submissions": ()}

    def state(self, run_id: str) -> dict[str, Any]:
        row = self._run_row(run_id)
        checkpoint = self.checkpointer.latest(self.conn, run_id)
        pending = None
        if checkpoint is not None and checkpoint.state.get("pending_interrupt"):
            pending = dict(checkpoint.state["pending_interrupt"])
        waits = branches.waits(checkpoint)
        positions = {}
        if checkpoint and self.registry.has(checkpoint.workflow_id, checkpoint.workflow_version):
            workflow = self.registry.get(checkpoint.workflow_id, checkpoint.workflow_version)
            positions = branches.node_states(workflow, {**checkpoint.state, "node_waits": waits})
            if row["status"] == "ACTIVE" and _advance_live(checkpoint.state):
                ready = [n for n, status in positions.items() if status == "READY"]
                selected = next((n for n in ready if n in checkpoint.state.get("resumptions", {})),
                                next(iter(ready), None))
                if selected:
                    positions[selected] = "RUNNING"
        return {
            "run_id": run_id,
            "status": row["status"],
            "revision": int(row["revision"]),
            "workflow_id": checkpoint.workflow_id if checkpoint else None,
            "workflow_version": checkpoint.workflow_version if checkpoint else None,
            "node": checkpoint.node if checkpoint else None,
            "checkpoint_seq": checkpoint.seq if checkpoint else 0,
            "interrupt_ref": row["interrupt_ref"],
            "pending_interrupt": pending,
            "node_waits": waits,
            "node_states": positions,
            "failed_nodes": dict(checkpoint.state.get("failed_nodes", {})) if checkpoint else {},
            "completed_nodes": list(checkpoint.state.get("completed", [])) if checkpoint else [],
        }

    # ------------------------------------------------------------------- internals
    @staticmethod
    def _clean_submission_refs(submission_refs: Sequence[Any]) -> tuple[str, ...]:
        refs: list[str] = []
        for item in submission_refs or ():
            if not isinstance(item, str):
                raise SchemaInvalid(
                    "submission_refs holds artifact/task references, not objects; a flag "
                    "smuggled in here would still not authorise anything",
                    details={"offending_type": type(item).__name__},
                )
            refs.append(item)
        return tuple(refs)

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no such run {run_id!r}",
                          details={"run_id": run_id},
            )
        return row

    def _outstanding(self, conn: sqlite3.Connection, run_id: str, pending: InterruptRequest,
                     *, strict: bool) -> tuple[GxError | None, dict[str, Any]]:
        """Re-derive from the store what of an interrupt has not come: ``(refusal, item -> state)``.

        ``strict``: an item counts only as what the node waits for -- a task ACCEPTED, the
        approval GRANTED. Not strict: an item counts once it reached any terminal outcome
        -- a task ACCEPTED, CANCELLED or ended (§2.7.1), an approval decided either way --
        because a terminal outcome is handed back to the node, which decides what it means
        (§2.5.1). A job counts when it has ended, in both readings: how it ended is the
        node's to read. The refusal is the one a resume answers; None when nothing is outstanding.
        """
        outstanding: dict[str, Any] = {}
        refusal: GxError | None = None
        if pending.tasks:
            rows = conn.execute(
                "SELECT task_id, status, hold_json FROM tasks WHERE task_id IN ("
                + ",".join("?" for _ in pending.tasks) + ")",
                list(pending.tasks),
            ).fetchall()
            by_id = {row["task_id"]: row for row in rows}
            for task_id in pending.tasks:
                row = by_id.get(task_id)
                state = row["status"] if row is not None else None
                if state == "ACCEPTED":
                    continue
                if not strict and row is not None and (state == "CANCELLED" or recovery.has_ended(row)):
                    continue
                outstanding[task_id] = state
            if outstanding:
                refusal = DependencyUnmet(
                    f"run {run_id} cannot resume: " + ", ".join(
                        f"{tid} is {st or 'missing'}" for tid, st in sorted(outstanding.items())
                    ) + "; an agent reporting completion only reaches SUBMITTED",
                    details={"run_id": run_id, "outstanding": dict(outstanding)},
                )
        if pending.job_refs:
            rows = conn.execute(
                "SELECT job_id, status FROM jobs WHERE job_id IN ("
                + ",".join("?" for _ in pending.job_refs) + ")",
                list(pending.job_refs),
            ).fetchall()
            states = {row["job_id"]: row["status"] for row in rows}
            terminal = machines.terminal_states("job")
            running = {job_id: states.get(job_id) for job_id in pending.job_refs
                       if states.get(job_id) not in terminal}
            if running:
                outstanding.update(running)
                refusal = refusal or DependencyUnmet(
                    f"run {run_id} cannot resume: external jobs are not finished: "
                    + ", ".join(f"{jid} is {st or 'missing'}" for jid, st in sorted(running.items())),
                    details={"run_id": run_id, "outstanding": running},
                )
        if pending.approval_ref:
            status = seams.approval_status(conn, pending.approval_ref)
            if status != APPROVAL_GRANTED and (strict or status in (None, "PENDING")):
                outstanding[pending.approval_ref] = status
                refusal = refusal or ApprovalRequired(
                    f"approval {pending.approval_ref} is {status}; a caller-supplied "
                    "approved flag is ignored and the run stays WAITING_APPROVAL",
                    details={"run_id": run_id, "approval_ref": pending.approval_ref,
                             "approval_status": status},
                )
        return refusal, outstanding

    def _require_interrupt_cleared(self, run_id: str, pending: InterruptRequest) -> None:
        """Raise unless every item of the interrupt is what the node waits for."""
        refusal, _outstanding = self._outstanding(self.conn, run_id, pending, strict=True)
        if refusal is not None:
            raise refusal

    def interrupt_outstanding(self, run_id: str) -> GxError | None:
        """Why the run's pending interrupt is not complete, or None when it is. Reads only.

        The strict check (:meth:`_require_interrupt_cleared`) for a caller that wants the
        answer and not the refusal. A run that waits on no interrupt has nothing to clear,
        and that is said as a refusal too. Whether a resume would be accepted is
        :meth:`standing`, which also knows quiescence and what was handed back.
        """
        checkpoint = self.checkpointer.latest(self.conn, run_id)
        pending = checkpoint.state.get("pending_interrupt") if checkpoint is not None else None
        if not pending:
            return IllegalTransition(f"run {run_id} waits on no interrupt", details={"run_id": run_id})
        return self._outstanding(self.conn, run_id, InterruptRequest.from_dict(pending), strict=True)[0]

    # ------------------------------------------------------------- quiescence, no spin
    def _check(self, conn: sqlite3.Connection, run_id: str) -> Any:
        """Scheduling's check of the run as it stands: is anything open, pending or waited for,
        and which tasks cannot move (``stoprules.check``; pure, on a snapshot read here)."""
        from ..scheduling import snapshot, stoprules   # the scheduler reads the store; no cycle at call time

        return stoprules.check(snapshot.load(conn, run_id, now=clock.now_iso()))

    @staticmethod
    def _last_change(conn: sqlite3.Connection, run_id: str) -> int:
        """The run's last event that is a change a node could act on. Renewing or handing over
        the coordinator lease is none, and neither is the reactor's record of its own failure."""
        marks = ",".join("?" for _ in NOT_A_CHANGE)
        # Selecting another branch and returning to a parked projection changes
        # no input of this controller. Otherwise two incomplete stages wake each
        # other forever on their own park / resume traffic.
        states = ",".join("?" for _ in DRIVE_STATES)
        return int(conn.execute(
            f"SELECT COALESCE(MAX(event_id), 0) AS n FROM events WHERE run_id=? AND kind NOT IN ({marks}) "
            f"AND NOT(kind='run.transition' AND prev_state IN ({states}) AND new_state IN ({states}))",
            (run_id, *NOT_A_CHANGE, *DRIVE_STATES, *DRIVE_STATES)).fetchone()["n"])

    @staticmethod
    def _limits_sha256() -> str | None:
        path = config.CONFIG_DIR / "limits.json"
        return ids.sha256_of_file(path) if path.is_file() else None

    def _controller_ran(self, conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        """The pair the no-spin rule compares (§2.5.4), as it is now: stored when a node parks."""
        return {"event_id": self._last_change(conn, run_id), "limits_sha256": self._limits_sha256()}

    def standing(self, run_id: str, *, conn: sqlite3.Connection | None = None,
                 interrupt_ref: str | None = None) -> Standing:
        """Where the run's pending interrupt stands, and whether a resume would be accepted. Reads only.

        * **complete**: every item is what the node waits for. Resumable.
        * **quiescent** (§2.5.2): not complete, and nothing of the run is open, pending or
          waited for. Resumable: the node is run again and decides -- or, when it only asks
          to wait again, the engine decides for it (:meth:`_dead_end`).
        * **handed back** (§2.5.1): every item reached a terminal outcome, not all of them
          good, while other work of the run is still open. Resumable once, when something
          has changed since the node last ran (§2.5.4, no spinning): the node sees the
          outcome and may wait again, and is not run a second time on the same facts.
        * otherwise it still waits, and ``waiting`` is the refusal a resume answers.
        """
        conn = conn or self.conn
        checkpoint = self.checkpointer.latest(conn, run_id)
        state = checkpoint.state if checkpoint is not None else {}
        ref = interrupt_ref or state.get("interrupt_ref")
        if not ref:
            return Standing(False, False, False, False, waiting=IllegalTransition(
                f"run {run_id} waits on no interrupt", details={"run_id": run_id}))
        wait = self._wait_by_ref(run_id, branches.waits(checkpoint), ref)
        interrupt = InterruptRequest.from_dict(wait["interrupt"])
        mark = wait["controller_ran"]
        if "reenter_round" in wait and (interrupt.tasks or interrupt.job_refs or interrupt.approval_ref):
            # The old node's result was invalidated by an explicit rewind. Before a
            # new-round once key can run, the original commissioned work must settle.
            refusal, outstanding = self._outstanding(conn, run_id, interrupt, strict=True)
            return Standing(refusal is None, refusal is None, False, refusal is None,
                            waiting=refusal, outstanding=outstanding)
        if interrupt.kind == "stage":
            workflow = self.registry.get(checkpoint.workflow_id, checkpoint.workflow_version)
            name = str(interrupt.payload["stage"])
            rule = workflow.stage_rules.get(name)
            if rule is None:
                raise SchemaInvalid(f"stage {name!r} has no installed completion rule")
            view = dict(rule(conn, run_id, int(wait["round"])))
            complete = bool(view.get("complete"))
            if interrupt.payload.get("wait_for") == "child_runs":
                # The children progress in separate runs. A parent event does
                # not settle them; the installed rule reads their actual state.
                waiting = None if complete else DependencyUnmet(
                    interrupt.reason, details={"run_id": run_id, "reason": "child_runs_pending",
                                               "needs": view.get("needs") or []})
                return Standing(complete, complete, False, complete, waiting=waiting,
                                stage_needs=view.get("needs") or ())
            quiescent = bool(view.get("quiescent")) and not complete
            changed = (self._last_change(conn, run_id) > int(mark.get("event_id") or 0)
                       or self._limits_sha256() != mark.get("limits_sha256"))
            refusal = None if complete or quiescent or changed else DependencyUnmet(
                f"stage {name} cannot resume: nothing has changed since its controller last ran",
                details={"run_id": run_id, "reason": "nothing_changed", "needs": view.get("needs") or []})
            return Standing(complete, changed, quiescent, changed, waiting=refusal,
                            stage_needs=view.get("needs") or ())
        refusal, outstanding = self._outstanding(conn, run_id, interrupt, strict=True)
        if refusal is None:
            return Standing(True, True, False, True)
        still_open, _open_items = self._outstanding(conn, run_id, interrupt, strict=False)
        handed_back = still_open is None
        quiescent = not state.get("failed_nodes") and bool(self._check(conn, run_id).quiescent)
        changed = (self._last_change(conn, run_id) > int(mark.get("event_id") or 0)
                   or self._limits_sha256() != mark.get("limits_sha256"))
        waiting: GxError | None = None
        if quiescent or (handed_back and changed):
            waiting = None
        elif handed_back:
            waiting = DependencyUnmet(
                f"run {run_id} cannot resume: nothing has changed since its node {wait['node']!r} last "
                "ran and saw " + ", ".join(f"{item} {st or 'missing'}" for item, st in sorted(outstanding.items()))
                + ", and other work of the run is still open",
                details={"run_id": run_id, "outstanding": dict(outstanding), "reason": "nothing_changed"},
            )
        else:
            waiting = still_open or refusal
        return Standing(False, handed_back, quiescent, changed, waiting=waiting, outstanding=outstanding)

    def _budget_snapshot(self, run_id: str) -> dict[str, Any] | None:
        row = self._run_row(run_id)
        budget_ref = row["budget_ref"]
        if not budget_ref:
            return None
        return store.budget_state(self.conn, budget_ref)

    @staticmethod
    def _budget_exhausted(budget: Mapping[str, Any] | None) -> list[str]:
        """Budget lines with nothing left to spend.

        A ceiling of zero is a **prohibition**, not an exhausted budget:
        ``research_budget`` declares ``paid_external_calls: 0`` and
        ``automatic_new_weight_downloads: 0``, and ``store.spend_budget`` already
        refuses any spend against them. Reading those as "exhausted" paused every real
        run before its first node — which is how this rule was found. A zero line is
        only reported here when something has nevertheless been spent against it, which
        means the prohibition was already broken and the run should stop loudly.
        """
        if not budget:
            return []
        limits = budget.get("limits") or {}
        spent = budget.get("spent") or {}
        exhausted = []
        for key, ceiling in limits.items():
            if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)):
                continue
            used = spent.get(key, 0)
            if ceiling <= 0:
                if used > 0:
                    exhausted.append(key)
            elif used >= ceiling:
                exhausted.append(key)
        return sorted(exhausted)

    def _advance(
        self, *, run_id: str, workflow: Workflow, state: dict[str, Any], actor: str,
        resumed: bool = False, report_branch_failures: bool = False,
    ) -> dict[str, Any]:
        advance_id = (state.get("advance") or {}).get("id")
        try:
            return self._advance_nodes(run_id=run_id, workflow=workflow, state=state, actor=actor,
                                       resumed=resumed, advance_id=advance_id,
                                       report_branch_failures=report_branch_failures)
        except BaseException:
            try:
                self._release_advance(run_id, workflow, advance_id)
            except sqlite3.Error:  # pragma: no cover - the store is failing; the raise says why
                pass
            raise

    def _advance_nodes(
        self, *, run_id: str, workflow: Workflow, state: dict[str, Any], actor: str,
        resumed: bool, advance_id: str | None, report_branch_failures: bool,
    ) -> dict[str, Any]:
        """The nodes of one advance. ``advance_id`` is this call's lease: before every node it
        must still be the run's, so a pause, a cancel or another advancer that took over a
        lapsed lease stops this one."""
        from ..p0.waits import is_p0_run
        operation_budgets = is_p0_run(self._run_row(run_id))
        resumed_once = resumed and not state.get("resumptions") and not state.get("node_waits")
        last_failure = None

        # The next node is recomputed every iteration rather than walked once, because a
        # rewind puts earlier nodes back on the list. A hard step bound keeps a
        # pathological node from spinning even if the round budget were misread.
        max_steps = len(workflow.order) * (self._round_budget() + 2) + 10
        steps = 0
        while True:
            started = time.monotonic_ns() if metrics.enabled() else None
            positions = branches.node_states(workflow, state)
            ready = [name for name, status in positions.items() if status == "READY"]
            name = next((n for n in ready if n in state.get("resumptions", {})), next(iter(ready), None))
            if started is not None:
                metrics.record("workflow_ready_scan", started=started, run_id=run_id, selected_node=name,
                               ready_count=len(ready), parked_count=len(state.get("node_waits", {})),
                               failed_count=len(state.get("failed_nodes", {})))
            if name is None:
                if state.get("node_waits"):
                    with store.transaction(
                        self.conn, transaction_owner="Engine", operation="park_waits", run_id=run_id,
                    ) as conn:
                        stopped = self._advance_stopped(conn, run_id, advance_id)
                        if stopped:
                            return self._response(run_id, warnings=[stopped], conn=conn)
                        response = self._park_waits(conn, run_id, workflow, state, actor)
                    return self._settled_response(run_id, actor, response, operation_budgets)
                if failures := state.get("failed_nodes"):
                    if last_failure is not None and not report_branch_failures:
                        raise last_failure
                    self._release_advance(run_id, workflow, advance_id)
                    warnings = [f"{name} failed ({failure['code']}): {failure['message']}"
                                for name, failure in failures.items()]
                    warnings.append("Use gx_run_control continue to retry failed nodes: " + ", ".join(failures))
                    return self._response(run_id, warnings=warnings)
                return self._finish(run_id=run_id, workflow=workflow, state=state, actor=actor)
            completed = list(state.get("completed", ()))
            steps += 1
            if steps > max_steps:
                raise SchemaInvalid(
                    f"run {run_id} took more than {max_steps} node steps on "
                    f"{workflow.workflow_id}@{workflow.version}; refusing to spin",
                    details={"run_id": run_id, "completed": completed},
                )
            node = workflow.node(name)

            # Every iteration re-reads the authority: a limit, a hold, a budget or a
            # revision captured one node ago is not a reading of the current state.
            config.check_holds(self.hold_scope)
            revision = store.current_revision(self.conn, run_id)
            budget = self._budget_snapshot(run_id)
            # P0's native-turn and Job owners reserve exactly what each operation
            # spends. A full Job line still permits observing its result and review.
            exhausted = [] if operation_budgets else self._budget_exhausted(budget)
            if exhausted:
                return self._pause_for_budget(
                    run_id=run_id, workflow=workflow, state=state, actor=actor,
                    node=name, keys=exhausted, budget=budget,
                )

            handoff = state.get("resumptions", {}).pop(name, None)
            ctx = NodeContext(
                conn=self.conn, run_id=run_id, workflow_id=workflow.workflow_id,
                workflow_version=workflow.version, node=name, actor=actor,
                inputs=state.get("inputs", {}), outputs=state.get("outputs", {}),
                state=state.setdefault("scratch", {}), submissions=handoff["submissions"] if handoff else (),
                resumed=(not handoff.get("reentered", False)) if handoff else resumed_once, revision=revision, budget=budget,
                round_index=int(handoff["round"] if handoff else state.get("round", 0)),
                rounds_left=max(0, self._round_budget() - int(state.get("round", 0))),
                teams=self.teams, services=self.services, gx=self.ctx,
            )

            try:
                if node.work == "stage":
                    # Controllers gather and validate outside the write lock. Each
                    # expansion commits on its own; the advance lease fences this
                    # controller's eventual checkpoint from pause/cancel/takeover.
                    with store.transaction(
                        self.conn, transaction_owner="Engine", operation="enter_node", run_id=run_id,
                    ) as conn:
                        stopped = self._advance_stopped(conn, run_id, advance_id)
                        if stopped is None and self._run_row(run_id)["phase"] != name:
                            conn.execute("UPDATE runs SET phase=? WHERE run_id=?", (name, run_id))
                            store.append_event(conn, kind="workflow.stage_entered", actor=actor,
                                               run_id=run_id, subject_kind="run", subject_id=run_id,
                                               evidence_level="EXECUTION_OBSERVED",
                                               payload={"stage": name, "round": ctx.round})
                    if stopped is None:
                        result = node.fn(ctx)
                        with store.transaction(
                            self.conn, transaction_owner="Engine", operation="checkpoint_node", run_id=run_id,
                        ) as conn:
                            stopped = self._advance_stopped(conn, run_id, advance_id)
                            if stopped is None:
                                outcome = self._run_node(conn, node, ctx, run_id=run_id, workflow=workflow,
                                                         state=state, actor=actor, name=name,
                                                         completed=completed, result=result, computed=True,
                                                         other_ready=len(ready) > 1)
                            else:
                                outcome = ("stopped", self._response(run_id, node=name,
                                                                    warnings=[stopped], conn=conn))
                    else:
                        outcome = ("stopped", self._response(run_id, node=name, warnings=[stopped]))
                else:
                    with store.transaction(
                        self.conn, transaction_owner="Engine", operation="execute_node", run_id=run_id,
                    ) as conn:
                        stopped = self._advance_stopped(conn, run_id, advance_id)
                        if stopped is not None:
                            outcome = ("stopped", self._response(run_id, node=name, warnings=[stopped], conn=conn))
                        else:
                            outcome = self._run_node(conn, node, ctx, run_id=run_id, workflow=workflow,
                                                     state=state, actor=actor, name=name, completed=completed,
                                                     other_ready=len(ready) > 1)
            except (sqlite3.Error, OSError):
                raise
            except Exception as exc:
                if operation_budgets and isinstance(exc, BudgetExceeded):
                    waiting = self._wait_for_admission(run_id, workflow, state, actor, name, exc, advance_id,
                                                       other_ready=len(ready) > 1)
                    if waiting is not None:
                        if waiting == "branch_wait":
                            resumed_once = False
                            continue
                        return waiting
                # GX authority, budget, ownership and uncertain external outcomes keep
                # their original refusal. A workflow node's own failure isolates its descendants.
                branch_failure = not isinstance(exc, GxError) or isinstance(exc, (SchemaInvalid, EvaluatorInvalid))
                self._node_failed(run_id=run_id, workflow=workflow, state=state, actor=actor, node=name,
                                  exc=exc, advance_id=advance_id, isolate=branch_failure)
                if not branch_failure:
                    raise
                last_failure = exc
                resumed_once = False
                continue
            kind, payload = outcome
            if kind == "stopped":
                return payload
            if kind in ("parked", "paused", "ended"):
                return self._settled_response(run_id, actor, payload, operation_budgets)
            # Submissions belong to the interrupt that was just cleared, not to the nodes
            # that run after it; a rewind put earlier nodes back on the list. The same holds
            # for "this node is run again after a resume": it is said to the first node only.
            resumed_once = False

    def _settled_response(self, run_id, actor, response, operation_budgets):
        """Notify the Job owner of whole-run decisions only after the checkpoint commits."""
        reason = (response.get("warnings") or [response["status"]])[0]
        if response["status"] == "STOPPED":
            self._end_jobs(run_id, actor, reason=reason)
        elif response["status"] == "PAUSED" and not operation_budgets:
            self._hold_jobs(run_id, actor, reason=reason)
        return response

    def _wait_for_admission(self, run_id, workflow, state, actor, node, refusal, advance_id, *, other_ready):
        """A spending owner refused this operation. Let commissioned work settle before pausing."""
        from ..scheduling import needs as vocabulary
        needs = []
        for line in refusal.details.get("shortfalls") or [refusal.details]:
            key = line.get("key") or line.get("line")
            amount = line.get("requested", line.get("needed"))
            if not isinstance(key, str) or type(amount) not in (int, float) or amount <= 0:
                return None
            subject = {"kind":"budget_line", "id":key, "required":amount, "admission":True}
            if ref := line.get("budget_ref") or refusal.details.get("budget_ref"):
                subject["budget_ref"] = ref
            needs.append(vocabulary.need("budget_line", f"{amount} {key} for the requested operation",
                                         clear_by=vocabulary.PERSON, why=refusal.message, subject=subject))
        with store.transaction(
            self.conn, transaction_owner="Engine", operation="wait_for_admission", run_id=run_id,
        ) as conn:
            stopped = self._advance_stopped(conn, run_id, advance_id)
            if stopped:
                return self._response(run_id, node=node, warnings=[stopped], conn=conn)
            state.clear()
            state.update(self.checkpointer.latest(conn, run_id).state)
            outstanding = self._objective_work(conn, run_id, refusal.message, runnable_only=True)
            if outstanding:
                answer = self._park(conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                                    node=node, interrupt=outstanding, other_ready=other_ready)
                return answer if answer is not None else "branch_wait"
            return self._pause(conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                               node=node, reason=refusal.message, needs=needs)

    def _advance_stopped(self, conn: sqlite3.Connection, run_id: str, advance_id: str | None) -> str | None:
        """Why this advance may not run its next node, or None. Read under the write lock."""
        status = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()["status"]
        if status != "ACTIVE":
            return (f"run {run_id} became {status} while it was being advanced; no further node ran"
                    + (" and it continues from where it stood" if status == "PAUSED" else ""))
        latest = self.checkpointer.latest(conn, run_id)
        holder = ((latest.state if latest is not None else {}).get("advance") or {}).get("id")
        if advance_id is not None and holder != advance_id:
            return f"run {run_id} is being advanced by another call; this one stopped before its next node"
        return None

    def _node_failed(self, *, run_id: str, workflow: "Workflow", state: dict[str, Any], actor: str,
                     node: str, exc: BaseException, advance_id: str | None, isolate: bool) -> None:
        """Record a rolled-back attempt under its original advance fence."""
        code = getattr(exc, "code", None) or type(exc).__name__
        message = str(getattr(exc, "message", None) or exc)[:500]
        with store.transaction(
            self.conn, transaction_owner="Engine", operation="record_node_failure", run_id=run_id,
        ) as conn:
            if self._advance_stopped(conn, run_id, advance_id):
                raise exc
            failure = {"node": node, "code": code, "message": message, "since": clock.now_iso(),
                       "continue_with": "gx_run_control action=continue"}
            store.append_event(conn, kind="workflow.node_failed", actor=actor, run_id=run_id, subject_kind="workflow",
                               subject_id=f"{workflow.workflow_id}@{workflow.version}", reason=f"{code}: {message}",
                               evidence_level="EXECUTION_OBSERVED", payload=failure)
            if isolate:
                latest = self.checkpointer.latest(conn, run_id)
                # A failed node can have mutated Python scratch or outputs before its
                # transaction rolled back. Continue only from the committed snapshot.
                state.clear()
                state.update(latest.state)
                state.setdefault("failed_nodes", {})[node] = failure
                self.checkpointer.save(conn, run_id=run_id, workflow_id=workflow.workflow_id,
                                       workflow_version=workflow.version, node=node, state=state)

    def _run_node(self, conn: sqlite3.Connection, node: "Node", ctx: "NodeContext", *, run_id: str,
                  workflow: "Workflow", state: dict[str, Any], actor: str, name: str,
                  completed: list[str], result: "NodeResult | None" = None,
                  computed: bool = False, other_ready: bool = False) -> tuple[str, Any]:
        """One node, inside its transaction: ``("parked", response)``, ``("rewound", None)``
        or ``("done", None)``. Never an exception for an ordinary end: raising inside the
        transaction would roll back the park it describes."""
        result = result if computed else node.fn(ctx)
        if not isinstance(result, NodeResult):
            raise SchemaInvalid(
                f"node {name!r} returned {type(result).__name__}, not a NodeResult",
                details={"node": name},
            )
        if result.stop is not None:
            return "ended", self._stop(conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                                       node=name, reason=result.stop.reason, rule=result.stop.rule)
        if result.pause is not None:
            return "paused", self._pause(conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                                         node=name, reason=result.pause.reason, needs=result.pause.needs)
        if node.work == "stage" and result.interrupt is None and result.reenter is None:
            view = workflow.stage_rules[name](conn, run_id, ctx.round)
            if not view.get("complete"):
                raise StaleRevision("the stage completion rule does not hold at commit",
                                    details={"run_id": run_id, "stage": name, "needs": view.get("needs") or []})
        if result.interrupt is not None:
            if not node.idempotent:
                raise SchemaInvalid(
                    f"node {name!r} is declared non-idempotent and must not interrupt: "
                    "a resume re-executes it from the beginning",
                    details={"node": name},
                )
            if node.work == "mechanical":
                raise SchemaInvalid(
                    f"node {name!r} is declared mechanical but asked for outside "
                    "work; declare it intelligent or keep the work in Python",
                    details={"node": name, "interrupt_kind": result.interrupt.kind},
                )
            if node.work == "gate" and result.interrupt.kind != "approval":
                raise SchemaInvalid(
                    f"node {name!r} is declared a gate and asked for "
                    f"{result.interrupt.kind}; a gate waits for an approval and "
                    "for nothing else",
                    details={"node": name, "interrupt_kind": result.interrupt.kind},
                )
            if node.work == "stage" and result.interrupt.kind != "stage":
                raise SchemaInvalid("a stage controller parks under one stage interrupt",
                                    details={"node": name, "kind": result.interrupt.kind})
            answer = self._park(
                conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                node=name, interrupt=result.interrupt, other_ready=other_ready, round_index=ctx.round,
            )
            return ("parked", answer) if answer is not None else ("branch_wait", None)
        if result.reenter is not None:
            rewound = self._rewind(
                conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                node=name, target=result.reenter, reason=result.reason,
            )
            if rewound:
                return "rewound", None
            # The round budget is spent: record the request, keep the node's outputs, and
            # let the graph finish so the run stops explicitly.
        outputs = dict(result.outputs or {})
        if node.work == "stage":
            conn.execute("UPDATE runs SET phase=NULL WHERE run_id=? AND phase=?", (run_id, name))
        state.setdefault("outputs", {})[name] = outputs
        completed.append(name)
        state["completed"] = completed
        prev_rev, new_rev = store.bump_revision(conn, run_id)
        store.append_event(
            conn, kind="workflow.node_completed", actor=actor, run_id=run_id,
            subject_kind="workflow", subject_id=f"{workflow.workflow_id}@{workflow.version}",
            prev_revision=prev_rev, new_revision=new_rev,
            evidence_level="EXECUTION_OBSERVED",
            payload={"node": name, "output_keys": sorted(outputs)},
        )
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=name, state=state,
        )
        return "done", None

    @staticmethod
    def _round_budget() -> int:
        """Re-read every time: a ceiling read at start-up cannot be tightened mid-run."""
        return int(config.research_budget()["max_literature_replanning_rounds"])

    def _rewind(
        self, conn: sqlite3.Connection, *, run_id: str, workflow: Workflow,
        state: dict[str, Any], actor: str, node: str, target: str, reason: str,
    ) -> bool:
        """Put the graph back to ``target`` for another round. ``False`` when refused.

        Refuses a target that is not a declared node, and a target that is not *before*
        the requesting node — a forward jump is not a loop, it is the arbitrary ``goto``
        the contract rules out. Exhausting the round budget is not an error: the request
        is recorded and the graph finishes, so the run stops explicitly rather than
        circling (design reference §18).
        """
        order = list(workflow.order)
        if target not in order:
            raise SchemaInvalid(
                f"node {node!r} asked to re-enter at {target!r}, which is not a node of "
                f"{workflow.workflow_id}@{workflow.version}",
                details={"node": node, "target": target, "nodes": order},
            )
        if order.index(target) >= order.index(node):
            raise SchemaInvalid(
                f"node {node!r} asked to re-enter at {target!r}, which does not precede it; "
                "a rewind goes back to an earlier node, it does not jump forward",
                details={"node": node, "target": target,
                         "target_position": order.index(target),
                         "node_position": order.index(node)},
            )
        budget = self._round_budget()
        round_index = int(state.get("round", 0))
        if round_index >= budget:
            store.append_event(
                conn, kind="workflow.reentry_refused", actor=actor, run_id=run_id,
                subject_kind="workflow", subject_id=f"{workflow.workflow_id}@{workflow.version}",
                reason=f"re-planning budget spent after {round_index} round(s): {reason}",
                evidence_level="EXECUTION_OBSERVED",
                payload={"node": node, "target": target, "rounds_used": round_index,
                         "max_rounds": budget, "trigger": reason},
            )
            return False

        affected = {target, node}
        for name in order:
            if affected.intersection(workflow.node(name).deps):
                affected.add(name)
        keep = [n for n in state.get("completed", ()) if n not in affected]
        dropped = [n for n in state.get("completed", []) if n not in keep]
        state["completed"] = keep
        state["round"] = round_index + 1
        outputs = state.setdefault("outputs", {})
        for name in dropped:
            outputs.pop(name, None)
        waits = state.get("node_waits", {})
        # Existing explicit replan owners reconcile Jobs and retire their original
        # task/interrupt before invoking this method without an advance lease.
        if not state.get("advance") and not state.get("pending_interrupt"):
            waits.pop(node, None)
        for name in affected:
            if name in waits:
                waits[name]["reenter_round"] = round_index + 1
            state.get("failed_nodes", {}).pop(name, None)
            state.get("resumptions", {}).pop(name, None)
        state.update(pending_interrupt=None, interrupt_ref=None)
        prev_rev, new_rev = store.bump_revision(conn, run_id)
        store.append_event(
            conn, kind="workflow.reentered", actor=actor, run_id=run_id,
            subject_kind="workflow", subject_id=f"{workflow.workflow_id}@{workflow.version}",
            prev_revision=prev_rev, new_revision=new_rev, reason=reason,
            evidence_level="EXECUTION_OBSERVED",
            payload={"node": node, "target": target, "round": round_index + 1,
                     "max_rounds": budget, "replayed_nodes": dropped, "trigger": reason},
        )
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=target, state=state,
        )
        return True

    def _park(
        self, conn: sqlite3.Connection, *, run_id: str, workflow: Workflow,
        state: dict[str, Any], actor: str, node: str, interrupt: InterruptRequest,
        other_ready: bool = False, round_index: int | None = None,
    ) -> dict[str, Any] | None:
        """Commit this wait with its node effects. Retain the advance while a sibling is ready."""
        interrupt_ref = ids.new_id("interrupt")
        pending = interrupt.as_dict()
        wait = branches.wait_record(node, pending, interrupt_ref, clock.now_iso(),
                                    state.get("round", 0) if round_index is None else round_index, {})
        state.setdefault("node_waits", {})[node] = wait
        store.append_event(conn, kind="workflow.node_waiting", actor=actor, run_id=run_id,
                           subject_kind="workflow", subject_id=f"{workflow.workflow_id}@{workflow.version}",
                           reason=interrupt.reason, evidence_level="EXECUTION_OBSERVED",
                           payload={"node": node, "interrupt_ref": interrupt_ref, **pending})
        wait["controller_ran"] = self._controller_ran(conn, run_id)
        if not other_ready:
            return self._park_waits(conn, run_id, workflow, state, actor, just_parked=node)
        self.checkpointer.save(conn, run_id=run_id, workflow_id=workflow.workflow_id,
                               workflow_version=workflow.version, node=node, state=state)
        return None

    def _park_waits(self, conn, run_id, workflow, state, actor, *, just_parked=None):
        """Maintain the historic single-wait view when no independent node can execute."""
        waits = state["node_waits"]
        node = next((name for name in workflow.order if name in waits), next(iter(waits)))
        wait = waits[node]
        interrupt_ref, pending = wait["interrupt_ref"], wait["interrupt"]
        interrupt = InterruptRequest.from_dict(pending)
        # An unrun sibling could commission the work a waiting node needs. Apply
        # the existing dead-end rule only when the static graph has no ready node,
        # with every wait outstanding and no failed branch available for recovery.
        dead = None if state.get("failed_nodes") else self._dead_end(conn, run_id, node, interrupt)
        if dead and all(self._outstanding(conn, run_id, InterruptRequest.from_dict(other["interrupt"]),
                                          strict=True)[0] is not None
                        for name, other in waits.items() if name != node):
            del waits[node]
            if dead["needs"]:
                return self._pause(conn, run_id=run_id, workflow=workflow, state=state, actor=actor, node=node,
                                   reason=dead["reason"], needs=dead["needs"], decided=dead["decision"])
            return self._stop(conn, run_id=run_id, workflow=workflow, state=state, actor=actor, node=node,
                              reason=dead["reason"], rule="quiescent", decided=dead["decision"])
        state.update(pending_interrupt=pending, interrupt_ref=interrupt_ref)
        state.pop("advance", None)
        if interrupt.kind == "stage":
            conn.execute("UPDATE runs SET phase=? WHERE run_id=?", (node, run_id))
        store.transition(
            conn, machine=RUN, subject_id=run_id, to_state=interrupt.waiting_state, actor=actor,
            reason=interrupt.reason, evidence_level="EXECUTION_OBSERVED",
            extra_updates={"interrupt_ref": interrupt_ref},
            payload={"node": node, "interrupt_ref": interrupt_ref, **interrupt.as_dict()},
        )
        if just_parked is not None:
            waits[just_parked]["controller_ran"] = self._controller_ran(conn, run_id)
        state["controller_ran"] = wait["controller_ran"]
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=node, state=state,
            interrupt_ref=interrupt_ref,
        )
        return self._response(
            run_id, node=node, interrupt=interrupt, interrupt_ref=interrupt_ref, conn=conn,
        )

    def _dead_end(self, conn: sqlite3.Connection, run_id: str, node: str,
                  interrupt: InterruptRequest) -> dict[str, Any] | None:
        """What to do instead of parking on an interrupt that nothing open can clear, or None.

        A node that asks to wait is parked -- unless the run is quiescent (§2.5.2): the
        interrupt is not complete, and no task is open, no job is pending and no approval
        is pending, so no call any client can make would ever clear it. Then the answer is
        a pause naming what a person or the machine can clear, or, with nothing to name, a
        stop with the reason.

        What can be cleared has one definition for each source. Each task that ended its chain
        (§2.7) is the need scheduling's check names for it (``check().ended[*].need``): the
        stop rules and the pause say the same thing. Each parked reaction (§2.4.8) is core's
        own. A need that is missing or outside the closed vocabulary is refused loudly
        (SchemaInvalid: the node fails and the run stays ACTIVE), never paused on.
        """
        refusal, outstanding = self._outstanding(conn, run_id, interrupt, strict=True)
        if refusal is None:
            return None            # it is complete already: parking it costs one resume, not a stall
        decision = self._check(conn, run_id)
        if not decision.quiescent:
            return None
        needs = self.named_needs(conn, run_id, decision=decision, node=node)
        waited = ", ".join(f"{item} is {state or 'missing'}" for item, state in sorted(outstanding.items()))
        reason = (f"node {node!r} waits for what nothing open can provide ({waited}): no task is open, no job "
                  f"is pending and no approval is pending. It asked: {interrupt.reason}")
        return {"reason": reason[:900], "needs": needs, "decision": decision}

    def named_needs(self, conn: sqlite3.Connection, run_id: str, *, decision: Any | None = None,
                    node: str | None = None) -> list[dict[str, Any]]:
        """What of the run a person or the machine can clear, as needs of the closed vocabulary.

        One definition for each source: each task that ended its chain (§2.7) is the need
        scheduling's check names for it (``check().ended[*].need``); each parked reaction
        (§2.4.8) is core's own. A dead end pauses on exactly these, and gx_run_status's
        ``stage.needs`` and a refused resume or continue name them. A need that is missing or
        outside the vocabulary is refused loudly (SchemaInvalid), never paused on or reported.
        """
        from ..scheduling import needs as vocabulary
        from . import reactor                    # the reactor imports the task service; no cycle at call time

        decision = decision if decision is not None else self._check(conn, run_id)
        needs = [dict(item.get("need") or {}) for item in decision.ended]
        needs += [vocabulary.need(
            "reaction", f"the reaction to {item['topic']} {item['subject']} succeeding when tried again",
            clear_by=vocabulary.MACHINE, why=f"it failed {item['count']} time(s): {item['code']}: {item['message']}",
            subject={"kind": "outbox", "id": item["outbox_id"]}) for item in reactor.parked(conn, run_id)]
        problems = [f"need {index}: {problem}" for index, need in enumerate(needs)
                    for problem in vocabulary.problems(need, recorded=True)]
        if problems:
            raise SchemaInvalid(
                f"run {run_id}" + (f" at node {node!r}" if node else "") + " has a need that is missing or "
                "outside the closed vocabulary; the run is not paused on it: " + "; ".join(problems[:6]),
                details={"run_id": run_id, "node": node, "problems": problems,
                         "ended": [item.get("task_id") for item in decision.ended]},
            )
        return needs

    def waiting_needs(self, run_id: str, *, conn: sqlite3.Connection | None = None,
                      interrupt_ref: str | None = None,
                      stage_needs: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        """The needs of the run as it now waits (contract r5 ``stage.needs``, and a refused
        resume's details): :meth:`named_needs`, and the approval its pending interrupt waits
        for while that approval is still PENDING. Work in flight is no need: it is waited for."""
        from ..scheduling import needs as vocabulary

        conn = conn or self.conn
        needs = self.named_needs(conn, run_id)
        checkpoint = self.checkpointer.latest(conn, run_id)
        ref = interrupt_ref or (checkpoint.state.get("interrupt_ref") if checkpoint else None)
        wait = self._wait_by_ref(run_id, branches.waits(checkpoint), ref) if ref else {}
        pending = wait.get("interrupt") or {}
        if pending.get("kind") == "stage":
            if stage_needs is None:
                workflow = self.registry.get(checkpoint.workflow_id, checkpoint.workflow_version)
                rule = workflow.stage_rules[str(pending["payload"]["stage"])]
                stage_needs = rule(conn, run_id, int(wait["round"])).get("needs") or ()
            existing = {ids.canonical_json(item) for item in needs}
            needs.extend(dict(item) for item in stage_needs if ids.canonical_json(item) not in existing)
        approval_ref = pending.get("approval_ref")
        if approval_ref and seams.approval_status(conn, approval_ref) in (None, "PENDING"):
            row = conn.execute("SELECT action_kind FROM approvals WHERE approval_id=?", (approval_ref,)).fetchone()
            needs.append(vocabulary.need(
                "approval", f"a person's decision on approval {approval_ref}", clear_by=vocabulary.PERSON,
                why=str(pending.get("reason") or "the run's node waits for it"),
                subject={"kind": "approval", "action_kind": row["action_kind"] if row is not None else None,
                         "id": approval_ref}))
        return needs

    def _checked(self, conn: sqlite3.Connection, run_id: str, workflow: "Workflow", actor: str, node: str,
                 decision: Any, applied: str) -> None:
        """Record scheduling's check and what the engine made of it (``schedule.checked``)."""
        store.append_event(
            conn, kind=decision.event_kind, actor=actor, run_id=run_id, subject_kind="workflow",
            subject_id=f"{workflow.workflow_id}@{workflow.version}", reason=str(decision.reason)[:500],
            evidence_level="EXECUTION_OBSERVED",
            payload={**decision.as_event(), "node": node, "applied": applied, "decided_by": "backend"},
        )

    def _pause(
        self, conn: sqlite3.Connection, *, run_id: str, workflow: Workflow, state: dict[str, Any], actor: str,
        node: str, reason: str, needs: Sequence[Mapping[str, Any]], decided: Any | None = None,
    ) -> dict[str, Any]:
        """PAUSED at ``node`` until every need reads clear (design r2.2 §2.8.2, §2.8.4).

        The pause point is the node itself: ``gx_run_control continue`` checks the needs,
        puts the run back to ACTIVE and runs the node again from its beginning.
        """
        if decided is not None:
            self._checked(conn, run_id, workflow, actor, node, decided, "PAUSE")
        state.pop("advance", None)
        state["pending_interrupt"] = None
        state["interrupt_ref"] = None
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=node, state=state,
        )
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM checkpoints WHERE run_id=?",
                           (run_id,)).fetchone()["s"]
        paused = {
            "reason": reason, "paused_by": "backend", "at": clock.now_iso(),
            "needs": [dict(item) for item in needs],
            "continue_from": {"status": "ACTIVE", "node": node, "interrupt_ref": None, "checkpoint_seq": int(seq)},
        }
        store.transition(
            conn, machine=RUN, subject_id=run_id, to_state="PAUSED", actor=actor, reason=reason[:500],
            evidence_level="EXECUTION_OBSERVED", extra_updates={"paused_json": paused, "interrupt_ref": None},
            payload={"node": node, "needs": paused["needs"], "decided_by": "backend"},
        )
        return self._response(
            run_id, node=node, conn=conn,
            warnings=[f"the run is PAUSED, not finished: {reason}"]
            + [f"it needs {item['what']} ({item['kind']}, cleared by a {item['clear_by']})" for item in paused["needs"]],
        )

    def _close(self, conn: sqlite3.Connection, run_id: str, actor: str, reason: str) -> dict[str, Any]:
        """A run that ends leaves nothing open: its tasks that have not ended are cancelled and
        what it still holds in its budgets is given back (design r2.2 §2.5.7; audit RS-11)."""
        cancelled: list[str] = []
        if self.teams is not None:
            cancelled = list(self.teams.cancel_open(conn, run_id=run_id, reason=reason[:300], actor=actor))
        released = seams.release_run_holds(conn, run_id=run_id, actor=actor, reason=reason[:300])
        return {"cancelled_tasks": cancelled, "released_holds": released}

    def _hold_jobs(self, run_id: str, actor: str, *, reason: str) -> None:
        """End the jobs of a paused run whose tool has not started. After the commit, outside
        any transaction; a job that is running is left to finish and announces its own end."""
        try:
            seams.hold_run_jobs(self.conn, run_id=run_id, reason=reason[:300], actor=actor)
        except Exception:  # noqa: BLE001 - the pause is on record; a job that could not be asked starts or is reconciled
            pass

    def _end_jobs(self, run_id: str, actor: str, *, reason: str) -> None:
        """Ask the jobs of a run that has ended to end. After the commit, outside any transaction."""
        try:
            seams.cancel_run_jobs(self.conn, run_id=run_id, reason=reason[:300], actor=actor)
        except Exception:  # noqa: BLE001 - the run has ended; a job that could not be asked is found by reconcile
            pass

    def _stop(
        self, conn: sqlite3.Connection, *, run_id: str, workflow: Workflow, state: dict[str, Any], actor: str,
        node: str, reason: str, rule: str | None = None, decided: Any | None = None,
    ) -> dict[str, Any]:
        """STOPPED with its reason (design r2.2 §2.8.3, §2.8.4). Never through the completion
        test: a node that stops the run has said it is not complete."""
        # A node can commit a reviewed stop in this same transaction. Its decision
        # already released the holds and recorded the transition; the graph still
        # owns the checkpoint in both cases.
        if self._run_row(run_id)["status"] != "STOPPED":
            if decided is not None:
                self._checked(conn, run_id, workflow, actor, node, decided, "STOP")
            closed = self._close(conn, run_id, actor, f"run stopped: {reason}")
            store.transition(
                conn, machine=RUN, subject_id=run_id, to_state="STOPPED", actor=actor, reason=reason[:500],
                evidence_level="EXECUTION_OBSERVED", extra_updates={"interrupt_ref": None},
                payload={"node": node, "rule": rule, "decided_by": "backend", **closed,
                         "nodes": list(state.get("completed", []))},
            )
        state.pop("advance", None)
        state["pending_interrupt"] = None
        state["interrupt_ref"] = None
        state["node_waits"] = {}
        state.pop("resumptions", None)
        self.checkpointer.save(
            conn, run_id=run_id, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, node=node, state=state,
        )
        return self._response(run_id, node=node, conn=conn, warnings=[f"the run is STOPPED: {reason}"])

    def _pause_for_budget(
        self, *, run_id: str, workflow: Workflow, state: dict[str, Any], actor: str,
        node: str, keys: Sequence[str], budget: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        with store.transaction(
            self.conn, transaction_owner="Engine", operation="pause_for_budget", run_id=run_id,
        ) as conn:
            stopped = self._advance_stopped(conn, run_id, (state.get("advance") or {}).get("id"))
            if stopped is not None:
                return self._response(run_id, node=node, warnings=[stopped], conn=conn)
            # The checkpoint first, so the pause names the checkpoint it continues from and
            # not the one before it (audit ENG-14).
            state.pop("advance", None)
            self.checkpointer.save(
                conn, run_id=run_id, workflow_id=workflow.workflow_id,
                workflow_version=workflow.version, node=node, state=state,
            )
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM checkpoints WHERE run_id=?",
                               (run_id,)).fetchone()["s"]
            # A pause says why, what it needs and where the run continues from; v1 entered
            # PAUSED with none of the three and had no code that left it.
            paused = {
                "reason": f"budget exhausted: {', '.join(keys)}", "exhausted": list(keys),
                "paused_by": "backend", "at": clock.now_iso(),
                "needs": [{"kind": "budget", "line": key,
                           "how": "raise the line under a budget_increase approval"} for key in keys],
                "continue_from": {"status": "ACTIVE", "node": node, "interrupt_ref": None,
                                  "checkpoint_seq": int(seq)},
            }
            store.transition(
                conn, machine=RUN, subject_id=run_id, to_state="PAUSED", actor=actor,
                reason=f"budget exhausted: {', '.join(keys)}",
                evidence_level="EXECUTION_OBSERVED", extra_updates={"paused_json": paused},
                payload={"node": node, "exhausted": list(keys), "budget": dict(budget or {})},
            )
        return self._response(
            run_id, node=node,
            warnings=[f"budget exhausted on {key}; the run is PAUSED, not finished" for key in keys],
        )

    @staticmethod
    def _objective_work(conn: sqlite3.Connection, run_id: str, reason: str, *,
                        runnable_only: bool = False) -> InterruptRequest | None:
        """Work already commissioned by the run; its existing owners settle it."""
        task_terminal = machines.terminal_states("task")
        job_terminal = machines.terminal_states("job")
        tasks = tuple(row["task_id"] for row in conn.execute(
            "SELECT task_id,status FROM tasks WHERE run_id=? ORDER BY rowid", (run_id,))
            if row["status"] not in task_terminal and
            (not runnable_only or row["status"] in ("READY", "CLAIMED", "RUNNING")))
        jobs = tuple(row["job_id"] for row in conn.execute(
            "SELECT job_id,status FROM jobs WHERE run_id=? ORDER BY rowid", (run_id,))
            if row["status"] not in job_terminal)
        if not tasks and not jobs:
            return None
        return InterruptRequest(kind="client_work" if tasks else "external_job", tasks=tasks, job_refs=jobs,
                                reason=reason or "Settle outstanding research work before closing this objective.")

    @staticmethod
    def _open_child_work(conn: sqlite3.Connection, run: sqlite3.Row, coverage: Mapping[str, Any]) -> bool:
        """The project identifies its children. Their original runs own their progress."""
        waiting = bool(coverage.get("child_requests"))
        for ref in coverage.get("pending_child_run_ids", ()):
            child = conn.execute("SELECT status FROM runs WHERE run_id=? AND project_ref=? AND run_id<>?",
                                 (ref, run["project_ref"], run["run_id"])).fetchone()
            if child is None:
                raise SchemaInvalid("objective coverage references a missing or foreign child run")
            if child["status"] not in machines.terminal_states(RUN):
                waiting = True
        return waiting

    def _finish(
        self, *, run_id: str, workflow: Workflow, state: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        with store.transaction(self.conn, transaction_owner="Engine", operation="finish", run_id=run_id) as conn:
            run = self._run_row(run_id)
            stopped = self._advance_stopped(conn, run_id, (state.get("advance") or {}).get("id"))
            if stopped is not None:
                return self._response(run_id, warnings=[stopped], conn=conn)
            missing = []
            node = state["completed"][-1] if state["completed"] else START_NODE
            coverage = {}
            action = ""
            if workflow.objective_coverage is not None:
                if workflow.digest() != run["workflow_sha256"]:
                    raise ContentHashMismatch("the installed objective coverage differs from this run's workflow pin")
                coverage = workflow.objective_coverage(conn, run_id)
                if not isinstance(coverage, Mapping) or type(coverage.get("allows_close")) is not bool:
                    raise SchemaInvalid("objective coverage must report a boolean allows_close")
                action = str(coverage.get("next_action") or "")
                if not coverage["allows_close"]:
                    missing = [str(question) for question in coverage.get("open_questions", [])]
                    if not missing:
                        missing = ["the workflow has not established its original objective"]
            interrupt = self._objective_work(conn, run_id, action if missing else "")
            if interrupt:
                dead = self._dead_end(conn, run_id, node, interrupt)
                if dead is None:
                    return self._park(conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                                      node=node, interrupt=interrupt)
                if dead["needs"]:
                    return self._pause(conn, run_id=run_id, workflow=workflow, state=state, actor=actor,
                                       node=node, reason=dead["reason"], needs=dead["needs"], decided=dead["decision"])
                missing.append(dead["reason"])
            if coverage and self._open_child_work(conn, run, coverage):
                self._release_advance(run_id, workflow, (state.get("advance") or {}).get("id"))
                warning = action or "Settle the registered child work and continue the parent."
                return self._response(run_id, node=node, warnings=missing + [warning], conn=conn)
            if missing:
                complete = False
            elif workflow.completion_evidence is None:
                complete, missing = False, ["the workflow has no completion evidence rule"]
            else:
                complete, missing = workflow.completion_evidence(conn, run)
                reason = "every node executed and the workflow's completion evidence was verified"
            complete = complete and not missing
            target = "COMPLETED" if complete else "STOPPED"
            if not complete:
                reason = "graph exhausted without completion evidence"
                if missing:
                    reason += ": " + "; ".join(missing)
            state.pop("advance", None)
            closed = self._close(conn, run_id, actor, f"run ended {target}: {reason}")
            store.transition(
                conn, machine=RUN, subject_id=run_id, to_state=target, actor=actor,
                reason=reason, evidence_level="EXECUTION_OBSERVED",
                payload={"missing_completion_evidence": missing,
                         "nodes": list(state.get("completed", [])), **closed},
            )
            self.checkpointer.save(
                conn, run_id=run_id, workflow_id=workflow.workflow_id,
                workflow_version=workflow.version, node=END_NODE, state=state,
            )
        self._end_jobs(run_id, actor, reason=f"run ended {target}: {reason}")
        warnings = [] if complete else [reason]
        return self._response(run_id, node=END_NODE, warnings=warnings)

    def _next_actions(self, run_id: str, interrupt: InterruptRequest | None) -> list[dict[str, Any]]:
        if interrupt is None:
            return []
        if interrupt.kind == "approval":
            return [{
                "kind": "await_approval",
                "approval_ref": interrupt.approval_ref,
                "reason": interrupt.reason,
                "note": "surface this to the user; the backend will not self-approve",
            }]
        if interrupt.kind == "external_job":
            return [{"kind": "await_external_job", "job_id": job_id, "reason": interrupt.reason}
                    for job_id in interrupt.job_refs]
        if self.teams is not None:
            return list(self.teams.next_actions(run_id=run_id)["next_actions"])
        return [{"kind": "task", "task_id": task_id, "reason": interrupt.reason}
                for task_id in interrupt.tasks]

    def _response(
        self, run_id: str, *, node: str | None = None, interrupt: InterruptRequest | None = None,
        interrupt_ref: str | None = None, warnings: Sequence[str] = (),
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        source = conn or self.conn
        row = source.execute("SELECT status, revision FROM runs WHERE run_id=?", (run_id,)).fetchone()
        seq_row = source.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM checkpoints WHERE run_id=?", (run_id,)
        ).fetchone()
        return {
            "run_id": run_id,
            "status": row["status"],
            "revision": int(row["revision"]),
            "node": node,
            "checkpoint_seq": int(seq_row["s"]),
            "interrupt_ref": interrupt_ref,
            "interrupt": interrupt.as_dict() if interrupt is not None else None,
            "next_actions": self._next_actions(run_id, interrupt),
            "warnings": list(warnings),
        }
