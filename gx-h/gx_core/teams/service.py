"""The task service: one task, one brief, one bundle, judged record by record.

This is the part of the protocol a worker talks to. A worker claims a task under a
lease, says what it is (``bind``), hands in one bundle (``submit``) and reads what the
backend made of each record. The rules are the task machine's
(``contracts/states.json`` → ``task.rules``) and each is a refusal a test observes:

* **ready** -- a task is READY only when every dependency is ACCEPTED.
* **claim** -- CLAIMED needs a matching ``expected_revision``, no live lease, the task's
  own role, an attempt left and, for a review task, a claimant who is not the author.
* **recovery** -- a refused submission and a lapsed lease send the task back to READY as
  the next attempt, settled inside the next call that looks (:mod:`.recovery`). v1 left
  both where they fell and its own error text said to claim again.
* **submit** -- reporting completion reaches SUBMITTED. ACCEPTED is the backend's: the
  bundle is read from bytes it holds, its shape is checked against the contract and its
  records are judged by the validator the owning service published.
* **held** -- a submission whose worker is outside the agent policy, or cannot be shown
  where the policy requires it, is kept and not judged: the task is BLOCKED, the run
  waits for the user's approval, and nothing continues on another model by default.
* **stale_owner** -- only the principal a live lease was issued to submits under it.

What a worker does under a lease never moves the run's revision. Sixteen workers claim
and submit in parallel; if each moved the revision each would invalidate the others'
``expected_revision``. The lease protects a task, the revision protects the plan.

``bind`` records a correlation, not an identity. What the worker says about itself is a
report; what the host's own record shows is an observation; the two are kept apart by
the host stream's ``hostobs`` module, which this service asks and does not imitate.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .. import config, contracts
from ..artifacts.service import ArtifactService
from ..bundles import framework
from ..context import SCOPE_OPERATOR, SCOPE_WORKER, SCOPES, Ctx, Principal
from ..domain import clock, ids, machines
from ..errors import (
    BY_CODE,
    ArtifactHashMismatch,
    ArtifactNotFound,
    ContentHashMismatch,
    DependencyUnmet,
    GxError,
    IdempotencyConflict,
    IllegalTransition,
    ImplementationGap,
    IndependenceRequired,
    LeaseExpired,
    LeaseHeld,
    ModelPolicyViolation,
    OutcomeUnknown,
    PhaseDenied,
    SchemaInvalid,
    ScopeDenied,
    StalePlan,
)
from ..orchestration import seams
from ..persistence import records, store
from ..registry import BundleVerdict, RecordVerdict, Subject
from . import recovery

TASK = "task"
RUN = "run"
BACKEND = "backend"

#: What a client may say about how it knows a worker started. Never EXECUTION_OBSERVED:
#: nothing the backend observed produced a string that arrived in a request.
BINDABLE_EVIDENCE_LEVELS = ("CLIENT_REPORTED", "PROPOSED", "UNVERIFIED")

#: Ceilings on what a task row carries. A task is an instruction, not a container: what
#: is larger goes into an artifact and is named by reference.
MAX_INPUT_BYTES = 8192
MAX_ACCEPTANCE_BYTES = 2048

#: Bytes of a response a list of tasks, or a verdict's records, may take.
ACTIONS_BUDGET = 13000
BLOCKED_BUDGET = 5000
VERDICT_BUDGET = 15000

#: How many ids of each kind of row a validator created are listed in a response.
CREATED_SHOWN = 20

VERDICT_KIND = "submission_verdict"
CHECK_KIND = "bundle_check_verdict"

#: The topic of the trigger an accepted submission writes (design r2.2 §2.4.1; the row's
#: shape is :func:`gx_core.persistence.store.enqueue_outbox`'s).
TRIGGER_ACCEPTED = "submission.accepted"

#: Runs in which no task is handed out.
_NO_DISPATCH = ("PAUSED",)

#: What gx_next_actions answers of the plan as the plan computed it (contract r5): the plan's own
#: facts, not cut and not recomputed here. ``next_actions``, ``blocked``, ``counts`` and
#: ``revision`` are this module's.
PLAN_FACTS = ("offered", "stage", "capacity", "as_of_event_id", "earliest_lease_expiry",
              "earliest_job_deadline", "jobs_due")

#: Events that move the run's stage (the node of its latest checkpoint): a claim made on a plan
#: older than one of them answers STALE_PLAN (contract r5 task.rules.claim). Every checkpoint that
#: changes the node commits with one of these or with a run.transition; a park is not a move away
#: from the tasks the plan offers, it is where they come from. Agreed with b-sched-2, 01:5xZ.
PLAN_MOVES = ("workflow.node_completed", "workflow.reentered")


def _limits() -> dict[str, Any]:
    return dict(config.limits().get("task_budget") or {})


def default_lease_seconds() -> int:
    return int(_limits().get("lease_seconds", 3600))


def default_max_attempts() -> int:
    return int(_limits().get("max_attempts", 3))



def backend_ctx(conn: sqlite3.Connection, *, tool: str = "") -> Ctx:
    """The context of something the backend does for itself, on nobody's request."""
    return Ctx(conn=conn, principal=Principal(subject=BACKEND, scope=SCOPE_WORKER, source=BACKEND),
               tool=tool)


def _encoded(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


# --------------------------------------------------------------------------- the views

def dependency_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [row["depends_on"] for row in conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id=? ORDER BY depends_on", (task_id,)
    )]


def changes_made_by(conn: sqlite3.Connection, fn: Callable[[], Any], *, name: str = "gx_dry_run") -> tuple[Any, int]:
    """Run ``fn`` in a savepoint that is always rolled back: ``(its value, row changes it made)``.

    The count is ``conn.total_changes`` across the call, taken after one empty savepoint.
    SQLite counts the flush of a full-text (FTS5) index, and the index flushes at the next
    savepoint boundary rather than at the statement that wrote: measured by rv-knowledge,
    a read entered after an FTS write in the same transaction moved the count by 4. The
    empty savepoint takes that flush, so what is counted is what ``fn`` wrote.
    """
    conn.execute(f"SAVEPOINT {name}_flush")
    conn.execute(f"RELEASE {name}_flush")
    before = conn.total_changes
    conn.execute(f"SAVEPOINT {name}")
    try:
        value = fn()
        return value, conn.total_changes - before
    finally:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")


def task_mapping(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """A task as validators, brief sections and the host stream receive it.

    Every column of ``tasks`` under its own name, plus the decoded forms a reader wants:
    ``input``, ``acceptance``, ``required_effort``, ``required_artifacts``, ``hold``,
    ``dependencies`` and ``previous_attempts`` (how each earlier attempt ended and which
    records were refused, from :func:`recovery.attempt_history`).
    """
    data: dict[str, Any] = {key: row[key] for key in row.keys()}
    data["input"] = store.loads(row, "input_json", {}) or {}
    data["acceptance"] = store.loads(row, "acceptance_json", {}) or {}
    data["required_effort"] = store.loads(row, "required_effort_json", []) or []
    data["required_artifacts"] = store.loads(row, "required_artifacts_json", []) or []
    data["hold"] = store.loads(row, "hold_json", None)
    data["dependencies"] = dependency_ids(conn, row["task_id"])
    data["previous_attempts"] = recovery.attempt_history(conn, row["task_id"])
    decision = conn.execute(
        "SELECT payload_json FROM events WHERE subject_kind='task' AND subject_id=?"
        " AND kind='task.worker_refusal_decided' ORDER BY event_id DESC LIMIT 1", (row["task_id"],),
    ).fetchone()
    data["worker_refusal_decision"] = store.loads(decision, "payload_json", None) if decision else None
    return data


def task_view(task: Mapping[str, Any]) -> dict[str, Any]:
    """A task as a client reads it: bounded, and without what only the backend uses."""
    subject = None
    if task.get("subject_kind"):
        subject = {"kind": task["subject_kind"], "id": task["subject_id"],
                   "sha256": task["subject_sha256"]}
    hold = task.get("hold")
    if isinstance(hold, Mapping):
        hold = {key: value for key, value in hold.items() if key != "refusals"}
    attempts = []
    for item in task.get("previous_attempts") or []:
        refused = list(item.get("refused") or [])
        attempts.append({
            "attempt": item.get("attempt"), "outcome": item.get("outcome"),
            "summary": item.get("summary"), "refused": refused[:5],
            "refused_total": item.get("refused_total", len(refused)),
            "records_ref": item.get("records_ref"),
        })
    return {
        "task_id": task["task_id"], "run_id": task["run_id"], "path_id": task.get("path_id"),
        "kind": task["kind"], "role": task["role"], "task_class": task.get("task_class"),
        "status": task["status"], "attempt": int(task["attempt"]),
        "max_attempts": int(task["max_attempts"]),
        "bundle_kind": task.get("bundle_kind"), "acceptance": task.get("acceptance") or {},
        "input": task.get("input") or {},
        "required_artifacts": task.get("required_artifacts") or [],
        "subject": subject, "independent_of": task.get("independent_of_task_id"),
        "parallel_group": task.get("parallel_group"),
        "model": task.get("required_model"), "efforts": task.get("required_effort") or [],
        "brief_ref": task.get("brief_ref"), "brief_sha256": task.get("brief_sha256"),
        "dependencies": task.get("dependencies") or [],
        "previous_attempts": attempts[-4:], "hold": hold,
        "budget_ref": task.get("budget_ref"), "owner": task.get("owner"),
        "lease_id": task.get("lease_id"),
    }


FRESH_AUTHORIZED = "FRESH_AUTHORIZED"
COMPLETED_REPLAY_VERIFIED = "COMPLETED_REPLAY_VERIFIED"


@dataclass(frozen=True)
class _LeaseEntry:
    disposition: str
    tool: str
    key: str
    request_sha256: str
    principal_subject: str
    request_id: str
    task_id: str
    run_id: str
    project_ref: str | None
    lease_id: str
    policy_family: str
    snapshot_json: str


class TeamService:
    """Tasks of one store. ``ctx`` is whoever is asking; without one the backend is."""

    def __init__(self, conn: sqlite3.Connection, *, ctx: Ctx | None = None,
                 hold_scope: str = "teams") -> None:
        self.conn = conn
        self.ctx = ctx if ctx is not None else backend_ctx(conn)
        self.hold_scope = hold_scope
        self.artifacts = ArtifactService(conn)

    # ----------------------------------------------------------------------- rows
    def _task_row(self, conn: sqlite3.Connection, task_id: str,
                  *, missing: type[GxError] = ArtifactNotFound) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise missing(f"no such task {task_id!r}", details={"task_id": task_id})
        return row

    def _run_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
        return row

    def task(self, task_id: str) -> dict[str, Any]:
        return task_view(task_mapping(self.conn, self._task_row(self.conn, task_id)))

    def _compact_stage_outputs(self, run_id: str, task_input: dict[str, Any]) -> bool:
        """Reference a large output list only when the same run's Job owns that exact list."""
        stage = task_input.get("stage_execution")
        if (not isinstance(stage, dict) or not isinstance(stage.get("job_id"), str)
                or not isinstance(stage.get("outputs"), list) or {"output_count", "read_outputs_with"} & stage.keys()):
            return False
        row = self.conn.execute("SELECT outcome_json FROM jobs WHERE job_id=? AND run_id=?",
                                (stage["job_id"], run_id)).fetchone()
        if row is None or (store.loads(row, "outcome_json", {}) or {}).get("outputs") != stage["outputs"]:
            return False
        compact = dict(stage)
        compact["output_count"] = len(compact.pop("outputs"))
        compact["read_outputs_with"] = ("Read gx_job_status(job_id). If the response is truncated, use "
                                        "gx_artifact_get with offset on the attached full-response artifact.")
        task_input["stage_execution"] = compact
        return True

    # ------------------------------------------------------------------ create_task
    def create_task(
        self, *, run_id: str, kind: str, role: str, bundle_kind: str | None,
        acceptance: Mapping[str, Any] | None, input: Mapping[str, Any] | None,  # noqa: A002
        subject: Subject | Mapping[str, Any] | None = None,
        independent_of_task_id: str | None = None, parallel_group: str | None = None,
        path_id: str | None = None, depends_on: Sequence[str] = (), stage: str | None = None,
        reservation_ref: str | None = None,
        max_attempts: int | None = None, required_artifacts: Sequence[str] = (),
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create one task and, when nothing blocks it, make it READY with its brief.

        The task's class, model and allowed efforts are taken from the agent policy the
        run pinned, by ``role``; nothing a caller passes can lower them. One
        ``worker_tasks`` unit is charged to the run's budget when it has one. Returns the
        task as :func:`task_view` shows it.
        """
        acceptance = dict(acceptance or {})
        task_input = dict(input or {})
        if not contracts.is_label("task_kind", kind):
            raise SchemaInvalid(f"{kind!r} is not a task kind",
                                details={"kind": kind, "known": contracts.label_values("task_kind")})
        if not contracts.is_label("worker_role", role):
            raise SchemaInvalid(
                f"{role!r} is not a role; the one role vocabulary is "
                f"{contracts.label_values('worker_role')}",
                details={"role": role, "known": contracts.label_values("worker_role")},
            )
        if bundle_kind is None:
            if (not stage or acceptance.get("validate_workflow_product") is not True
                    or not isinstance(required_artifacts, Sequence) or isinstance(required_artifacts, (str, bytes))
                    or not required_artifacts
                    or not all(isinstance(name, str) and name.strip() for name in required_artifacts)):
                raise SchemaInvalid("an artifact-only task needs a workflow stage, its product validator "
                                    "and the required artifact kinds")
        else:
            if bundle_kind not in contracts.bundle_kinds():
                raise SchemaInvalid(f"{bundle_kind!r} is not a bundle kind",
                                    details={"bundle_kind": bundle_kind, "known": contracts.bundle_kinds()})
            answers = contracts.bundle_spec(bundle_kind)["for_task_kinds"]
            if kind not in answers:
                raise SchemaInvalid(
                    f"a {kind} task is not answered by a {bundle_kind}; that bundle answers "
                    f"{', '.join(answers)}",
                    details={"kind": kind, "bundle_kind": bundle_kind, "for_task_kinds": list(answers)},
                )
        for name, value, ceiling in (("input", task_input, MAX_INPUT_BYTES),
                                     ("acceptance", acceptance, MAX_ACCEPTANCE_BYTES)):
            try:
                size = len(ids.canonical_json(value).encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise SchemaInvalid(f"task {name} is not JSON: {exc}", details={"field": name}) from exc
            if name == "input" and size > ceiling and self._compact_stage_outputs(run_id, task_input):
                size = len(ids.canonical_json(task_input).encode("utf-8"))
            if size > ceiling:
                raise SchemaInvalid(
                    f"task {name} is {size} bytes and the limit is {ceiling}; publish what is "
                    "large as an artifact and name it by reference",
                    details={"field": name, "bytes": size, "limit": ceiling},
                )
        if isinstance(subject, Subject):
            subject = {"kind": subject.kind, "id": subject.id, "sha256": subject.sha256}
        if subject is not None and not all(subject.get(key) for key in ("kind", "id", "sha256")):
            raise SchemaInvalid("a task's subject names a kind, an id and a sha256",
                                details={"subject": dict(subject)})
        attempts = default_max_attempts() if max_attempts is None else int(max_attempts)
        if attempts < 1:
            raise SchemaInvalid("max_attempts is at least 1", details={"max_attempts": max_attempts})
        if stage is not None and (not isinstance(stage, str) or not stage.strip() or len(stage) > 200):
            raise SchemaInvalid("a backend task stage is non-empty text of at most 200 characters")
        actor = actor or (self.ctx.actor if self.ctx.principal.subject != BACKEND else BACKEND)

        with store.transaction(self.conn) as conn:
            run = self._run_row(conn, run_id)
            if bundle_kind is None:
                workflow = self._product_workflow(conn, run)
                declared = (workflow.document or {}).get("nodes", ())
                if run["phase"] != stage or not any(
                    node.get("name") == stage and node.get("work") == "stage"
                    and node.get("artifact_task_kind") == kind for node in declared
                ):
                    raise SchemaInvalid("the active workflow stage does not declare this artifact-only task kind",
                                        details={"stage": stage, "kind": kind})
            if run["status"] in machines.terminal_states(RUN):
                raise PhaseDenied(
                    f"run {run_id} is {run['status']}; no task is added to a run that has ended",
                    details={"run_id": run_id, "status": run["status"]},
                )
            for dep in list(depends_on) + ([independent_of_task_id] if independent_of_task_id else []):
                other = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (dep,)).fetchone()
                if other is None:
                    raise SchemaInvalid(f"{dep!r} is not a task", details={"task_id": dep})
                if other["run_id"] != run_id:
                    raise ScopeDenied(f"task {dep} belongs to another run",
                                      details={"task_id": dep, "run_id": run_id})
            if path_id is not None:
                path = conn.execute("SELECT run_id FROM paths WHERE path_id=?", (path_id,)).fetchone()
                if path is None or path["run_id"] != run_id:
                    raise SchemaInvalid(f"{path_id!r} is not a path of run {run_id}",
                                        details={"path_id": path_id, "run_id": run_id})

            requirement = seams.task_requirement(store.loads(run, "agent_policy_json", {}), role=role)
            task_id = records.create_task(
                conn, run_id=run_id, kind=kind, role=role, actor=actor, path_id=path_id,
                depends_on=list(depends_on), required_artifacts=list(required_artifacts),
                task_input=task_input, budget_ref=run["budget_ref"],
                task_class=requirement.get("task_class"), bundle_kind=bundle_kind,
                acceptance=acceptance, max_attempts=attempts, parallel_group=parallel_group,
                subject=subject, independent_of_task_id=independent_of_task_id,
                required_model=requirement.get("model"),
                required_effort=list(requirement.get("allowed_efforts") or []),
            )
            if stage is not None:
                # The workflow supplies this below the request boundary. The public task
                # creation tools deliberately do not accept a caller-selected stage.
                conn.execute("UPDATE tasks SET stage=? WHERE task_id=?", (stage, task_id))
            if reservation_ref is None:
                seams.charge_worker_task(conn, budget_ref=run["budget_ref"], task_id=task_id, actor=actor)
            else:
                from .. import budgets
                held = conn.execute(
                    "SELECT b.run_id, r.amounts_json, r.status FROM budget_reservations r"
                    " JOIN budgets b ON b.budget_ref=r.budget_ref WHERE r.reservation_ref=?",
                    (reservation_ref,),
                ).fetchone()
                if held is None or held["run_id"] != run_id:
                    raise ScopeDenied("a task draws only a reservation of its own run",
                                      details={"run_id": run_id, "reservation_ref": reservation_ref})
                amounts = store.loads(held, "amounts_json", {}) or {}
                if amounts != {"worker_tasks": 1}:
                    raise SchemaInvalid("a task draws a hold of exactly one worker_tasks unit",
                                        details={"reservation_ref": reservation_ref})
                budgets.draw_reservation(conn, reservation_ref, subject_kind=TASK,
                                         subject_id=task_id, actor=actor)
            ready, _blocking = records.dependencies_satisfied(conn, task_id)
            if ready:
                self._enter_ready(conn, task_id, "created with no unmet dependency")
            return task_view(task_mapping(conn, self._task_row(conn, task_id)))

    def create_tasks(self, *, run_id: str, plan: Mapping[str, Any], actor: str | None = None) -> dict[str, Any]:
        """Create several tasks that depend on each other, in dependency order.

        ``plan = {"tasks": [{"key", "kind", "role", "bundle_kind", "acceptance", "input",
        "depends_on": [key or task_id], ...}]}``. A cyclic plan is refused here rather
        than producing a board on which nothing can ever become READY. One transaction:
        a plan is created whole or not at all.
        """
        specs = list(plan.get("tasks", []))
        if not specs:
            raise SchemaInvalid("a plan with no tasks creates nothing", details={"run_id": run_id})
        order = self._plan_order(specs, run_id=run_id)
        by_key = {spec["key"]: spec for spec in specs}
        created: dict[str, str] = {}
        with store.transaction(self.conn):
            for key in order:
                spec = by_key[key]
                view = self.create_task(
                    run_id=run_id, kind=spec["kind"], role=spec["role"],
                    bundle_kind=spec.get("bundle_kind"), acceptance=spec.get("acceptance"),
                    input=spec.get("input"), subject=spec.get("subject"),
                    independent_of_task_id=created.get(spec.get("independent_of"), spec.get("independent_of")),
                    parallel_group=spec.get("parallel_group"), path_id=spec.get("path_id"),
                    depends_on=[created.get(dep, dep) for dep in spec.get("depends_on", ())],
                    max_attempts=spec.get("max_attempts"),
                    required_artifacts=spec.get("required_artifacts", ()), actor=actor,
                )
                created[key] = view["task_id"]
        return {"tasks": created, "task_ids": [created[key] for key in order]}

    def _plan_order(self, specs: Sequence[Mapping[str, Any]], *, run_id: str) -> list[str]:
        keys: list[str] = []
        for spec in specs:
            for required in ("key", "kind", "role", "bundle_kind"):
                if not spec.get(required):
                    raise SchemaInvalid(f"task spec is missing {required!r}", details={"spec": dict(spec)})
            if spec["key"] in keys:
                raise SchemaInvalid(f"duplicate task key {spec['key']!r} in the plan",
                                    details={"key": spec["key"]})
            keys.append(spec["key"])
        known = set(keys)
        pending = {
            spec["key"]: {dep for dep in spec.get("depends_on", ()) if dep in known}
            | ({spec["independent_of"]} if spec.get("independent_of") in known else set())
            for spec in specs
        }
        order: list[str] = []
        while pending:
            ready = [key for key in keys if key in pending and not pending[key]]
            if not ready:
                raise SchemaInvalid(
                    "the plan has a dependency cycle among: " + ", ".join(sorted(pending)),
                    details={"cycle_candidates": sorted(pending), "run_id": run_id},
                )
            for key in ready:
                order.append(key)
                del pending[key]
                for deps in pending.values():
                    deps.discard(key)
        return order

    # ---------------------------------------------------------------- who wrote it
    @property
    def _writer(self) -> str:
        """The actor on everything this service writes: the principal the call was made for.

        Every record says which scope and subject wrote it, also where it was the backend
        and not that caller who decided what was written -- a task made READY, a bundle
        judged, a lapsed lease settled. Those say so in ``decided_by``
        (:func:`recovery.derived`). Used by the backend on nobody's request, it is the backend.
        """
        return BACKEND if self.ctx.principal.subject == BACKEND else self.ctx.actor

    # ----------------------------------------------------------------- READY, briefs
    def _enter_ready(self, conn: sqlite3.Connection, task_id: str, reason: str) -> None:
        """Make a task READY for its coming attempt and compile the brief for it."""
        store.transition(
            conn, machine=TASK, subject_id=task_id, to_state="READY", actor=self._writer,
            reason=reason, evidence_level="EXECUTION_OBSERVED", payload=recovery.derived(),
            extra_updates={"owner": None, "lease_id": None, "hold_json": None, "blocked_reason": None}, bump=False,
        )
        self._compile_brief(conn, task_id, raise_errors=False)

    def _compile_brief(self, conn: sqlite3.Connection, task_id: str, *, raise_errors: bool) -> tuple[str | None, str | None]:
        """Ask the host stream for the brief of the coming attempt and record it on the task.

        While settling, a compiler that raises does not take the dispatch plan down with
        it: the failure is recorded on the task, the brief stays null, and the claim that
        follows raises it where the worker who needs the brief can see it.
        """
        row = self._task_row(conn, task_id)
        try:
            ref, sha256 = seams.compile_brief(self.ctx.child(conn=conn), task_mapping(conn, row))
        except GxError as exc:
            if raise_errors:
                exc.details = {**exc.details, "ready_compile_task": task_id}
                raise
            store.append_event(
                conn, kind="task.brief_failed", actor=self._writer, run_id=row["run_id"],
                subject_kind=TASK, subject_id=task_id, reason=f"{exc.code}: {exc.message}"[:500],
                evidence_level="EXECUTION_OBSERVED",
                payload=recovery.derived({"code": exc.code, "attempt": int(row["attempt"]) + 1}),
            )
            self._dispatch_failed(conn, task_id, exc)
            ref = sha256 = None
        conn.execute(
            "UPDATE tasks SET brief_ref=?, brief_sha256=?, updated_at=? WHERE task_id=?",
            (ref, sha256, clock.now_iso(), task_id),
        )
        return ref, sha256

    # ------------------------------------------------------------------------ settle
    def settle(self, *, run_id: str) -> dict[str, Any]:
        """Settle what is already decided for a run. A no-op costs no write lock."""
        run = self._authorize_primary(self.conn, run_id)
        stopped = seams.stopped_holders(self.conn, run_id=run_id) or []
        if not stopped and not recovery.needs_settling(self.conn, run_id):
            # A previous version could settle both overlapping holds and leave
            # only the run parked. Repair the original transition on normal resume.
            if (run["status"] == "WAITING_APPROVAL" and self._run_before_task_hold(self.conn, run)
                    and not self.conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND status='BLOCKED'"
                                              " AND json_extract(hold_json,'$.approval_id') IS NOT NULL LIMIT 1",
                                              (run_id,)).fetchone()):
                with store.transaction(self.conn) as conn:
                    restored = self._after_holds(conn, run_id, [])
                if restored:
                    return {"run_restored": restored}
            return {}
        from ..hostobs import codex_worker_observation as codex_observation
        native = codex_observation.prepare_run(self.ctx, run_id)
        with store.transaction(self.conn) as conn:
            self._codex_observations = codex_observation.fence_run(conn, native)
            try:
                return self._settle(conn, run_id)
            finally:
                self._codex_observations = {}

    def inspect_workers(self, *, run_id: str) -> dict[str, Any]:
        """Canonical consumer of a host worker-stopped or worker-verdict trigger.

        Hooks propose a look; the host reader's present observations decide. No report
        supplied by the worker can release a lease or spend an attempt here.
        """
        self._authorize_primary(self.conn, run_id)
        from ..hostobs import codex_worker_observation as codex_observation
        native = codex_observation.prepare_run(self.ctx, run_id)
        with store.transaction(self.conn) as conn:
            self._authorize_primary(conn, run_id)
            self._codex_observations = codex_observation.fence_run(conn, native)
            try:
                return self._inspect_workers(conn, run_id, include_live=True)
            finally:
                self._codex_observations = {}

    def _native_passive_verdict(self, conn: sqlite3.Connection, task: sqlite3.Row,
                                lease: sqlite3.Row, *, due: bool) -> dict[str, Any] | None:
        """Read recorded Native facts; absence of a new callback is not a verdict.

        None leaves a live lease alone or lets ordinary lapse recovery consume a due
        attempt. It never certifies the old worker or supplies bind/submit evidence.
        """
        from ..hostobs import native_context as native, native_observation, policy
        from ..jobs import leases as job_leases

        proof, _ = native_observation.claim_receipt(conn, task["task_id"], lease["lease_id"])
        run = self._run_row(conn, task["run_id"])
        host = store.loads(run, "host_json", {})
        origin = proof.get("native_call")
        if (task["lease_id"] != lease["lease_id"] or task["task_id"] != lease["task_id"]
                or task["attempt"] != lease["attempt"] or task["owner"] != lease["owner"]
                or proof.get("source") != native.SOURCE or proof.get("run_id") != task["run_id"]
                or proof.get("task_id") != task["task_id"] or proof.get("attempt") != lease["attempt"]
                or proof.get("principal_subject") != lease["principal"] or not isinstance(origin, dict)
                or origin.get("tool_name") != "gx_task_claim" or origin.get("model_authority") != native.MODEL_AUTHORITY
                or origin.get("session_id") != host.get("session_ref")
                or origin.get("root_thread_id") != host.get("policy_selection", {}).get("native_agent_ref")
                or proof.get("observed_worker_ref") != f"codex:{origin.get('session_id')}:{origin.get('thread_id')}"):
            raise ScopeDenied("the recorded Native claim does not cover this task lease")
        if origin.get("evidence_sha256") != ids.sha256_of_obj({k: v for k, v in origin.items() if k != "evidence_sha256"}):
            raise ContentHashMismatch("the recorded Native claim callback hash changed")
        _, required, scope = native_observation.requirement(conn, task)
        if proof.get("scope") != scope:
            raise ContentHashMismatch("the Native claim policy or role pins changed")
        if not policy.is_model(origin.get("model"), required["model"]) or origin.get("effort") not in required["allowed_efforts"]:
            raise ModelPolicyViolation("the recorded Native claim is outside its task policy")
        for row in conn.execute("SELECT * FROM worker_observations WHERE task_id=? AND lease_id=? ORDER BY rowid",
                                (task["task_id"], lease["lease_id"])):
            verdict = row["policy_verdict"]
            if verdict and verdict != policy.CONFORMS:
                return {"policy_verdict": verdict, "worker_observation_id": row["worker_observation_id"],
                        "waits_for_user": True, "refusal": verdict == policy.REFUSAL_OR_FALLBACK,
                        "reasons": store.loads(row, "payload_json", {}).get("reasons", []), "deviation": None}
            if row["state"] == "failed" or row["turns_off_policy"]:
                raise ModelPolicyViolation("the Native lease has a recorded failed or off-policy observation")
        if due:
            if conn.execute("SELECT 1 FROM assets WHERE author_task_id=? LIMIT 1", (task["task_id"],)).fetchone():
                raise OutcomeUnknown("the expired Native task already authored a canonical asset")
            if conn.execute("SELECT 1 FROM plugin_invocations WHERE task_id=? AND status IN ('ADMITTED','OUTCOME_UNKNOWN') LIMIT 1",
                            (task["task_id"],)).fetchone():
                raise OutcomeUnknown("the expired Native task has an unresolved plugin invocation")
            # Job deadlines alone cannot settle UNKNOWN/overdue external effects.
            if any(job["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED") for job in job_leases.task_jobs(conn, task)):
                raise OutcomeUnknown("the expired Native task still has an unresolved Job")
            if conn.execute("SELECT 1 FROM submissions WHERE task_id=? AND (result='ACCEPTED' OR (attempt=? AND accepted_count>0)) LIMIT 1",
                            (task["task_id"], task["attempt"])).fetchone():
                raise OutcomeUnknown("the expired Native task already has accepted submission effects")
        return None

    def _inspect_workers(self, conn: sqlite3.Connection, run_id: str, *, include_live: bool = False) -> dict[str, Any]:
        stopped = {item["lease_id"]: item for item in (seams.stopped_holders(conn, run_id=run_id) or [])}
        due = {row["lease_id"] for row in recovery.lapsed_leases(conn, run_id)}
        out: dict[str, Any] = {"worker_holds": [], "host_stopped": []}
        rows = conn.execute(
            "SELECT l.* FROM leases l JOIN tasks t ON t.lease_id=l.lease_id"
            " WHERE t.run_id=? AND t.status IN ('CLAIMED','RUNNING') AND l.released_at IS NULL"
            " ORDER BY l.issued_at,l.lease_id", (run_id,),
        ).fetchall()
        for lease in rows:
            if not include_live and lease["lease_id"] not in due and lease["lease_id"] not in stopped:
                continue
            task = self._task_row(conn, lease["task_id"])
            prepared = getattr(self, "_codex_observations", {}).get(lease["lease_id"])
            from ..hostobs.native_context import is_native_run
            if prepared is None and is_native_run(conn, run_id):
                verdict = self._native_passive_verdict(conn, task, lease, due=lease["lease_id"] in due)
                if verdict is None:
                    continue
            else:
                verdict = seams.record_verdict_for_lease(conn, task_id=task["task_id"], lease_id=lease["lease_id"],
                    **({"_codex_prepared": prepared} if prepared is not None else {}))
            run = self._run_row(conn, run_id)
            policy_doc = store.loads(run, "agent_policy_json", {}) or seams.agent_policy()[0]
            hosts = {(store.loads(run, "host_json", {}) or {}).get("kind")}
            hosts.update(row["host"] for row in conn.execute(
                "SELECT DISTINCT host FROM worker_observations WHERE lease_id=?", (lease["lease_id"],)))
            modes = (policy_doc.get("observation") or {}).get("by_host") or {}
            unreadable_hold = verdict and any(verdict.get("policy_verdict") in (modes.get(host) or {}).get("holds", [])
                                              for host in hosts)
            if verdict and (verdict.get("refusal") or verdict.get("waits_for_user") or unreadable_hold):
                reason = recovery.HOLD_HOST_MODEL_REFUSAL if verdict.get("refusal") else recovery.HOLD_MODEL_POLICY
                out["worker_holds"].append(self._hold_worker(conn, task, lease, verdict, reason=reason))
                continue
            host_stop = stopped.get(lease["lease_id"])
            if host_stop is None:
                continue
            store.release_lease(conn, lease["lease_id"])
            why = f"the host recorded the holder stopped ({host_stop.get('cause')}); no attempt consumed"
            store.append_event(conn, kind="task.host_stopped", actor=self._writer, run_id=run_id,
                               subject_kind=TASK, subject_id=task["task_id"], reason=why,
                               evidence_level="EXECUTION_OBSERVED",
                               payload=recovery.derived({**host_stop, "attempt": task["attempt"], "attempt_consumed": False}))
            if task["status"] == "RUNNING":
                store.transition(conn, machine=TASK, subject_id=task["task_id"], to_state="RETRYABLE", actor=self._writer,
                                 run_id=run_id, reason=why, evidence_level="EXECUTION_OBSERVED", bump=False)
            recovery.requeue_or_exhaust(conn, task["task_id"], reason=why, enter_ready=self._enter_ready,
                                       actor=self._writer, end_chain=self._end_chain, consume=False)
            out["host_stopped"].append(task["task_id"])
        return {key: value for key, value in out.items() if value}

    @staticmethod
    def _run_before_task_hold(conn: sqlite3.Connection, run: sqlite3.Row) -> str | None:
        if run["status"] != "WAITING_APPROVAL":
            return run["status"] if machines.can_transition(RUN, run["status"], "WAITING_APPROVAL") else None
        # Overlapping task holds share the transition that opened this wait. A
        # workflow's own approval interrupt has a different owner and stays parked.
        event = conn.execute(
            "SELECT prev_state,payload_json FROM events WHERE subject_kind='run' AND subject_id=?"
            " AND kind='run.transition' AND new_state='WAITING_APPROVAL' ORDER BY event_id DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        payload = store.loads(event, "payload_json", {}) if event else {}
        if payload.get("decided_by") == BACKEND and payload.get("approval_id"):
            return event["prev_state"]
        return None

    def _hold_worker(self, conn: sqlite3.Connection, row: sqlite3.Row, lease: sqlite3.Row,
                     policy: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
        """A bind refusal or host-observed refusal is a decision, never a retry."""
        digest = row["brief_sha256"] or ids.sha256_of_obj({"task_id": row["task_id"], "attempt": row["attempt"]})
        request = {"task_id": row["task_id"], "lease_id": lease["lease_id"], "attempt": row["attempt"],
                   "brief_sha256": row["brief_sha256"], "policy_verdict": policy.get("policy_verdict"),
                   "worker_observation_id": policy.get("worker_observation_id"),
                   "reasons": list(policy.get("reasons") or []), "deviation": policy.get("deviation")}
        approval = seams.request_approval(self.ctx.child(conn=conn), action_kind="worker_refusal",
                                           subject=Subject(kind=TASK, id=row["task_id"], sha256=digest),
                                           run_id=row["run_id"], request=request)
        run = self._run_row(conn, row["run_id"])
        before = self._run_before_task_hold(conn, run)
        if run["status"] != "WAITING_APPROVAL" and machines.can_transition(RUN, run["status"], "WAITING_APPROVAL"):
            store.transition(conn, machine=RUN, subject_id=row["run_id"], to_state="WAITING_APPROVAL",
                             actor=self._writer, reason=f"task {row['task_id']} waits on worker_refusal",
                             evidence_level="EXECUTION_OBSERVED", payload=recovery.derived({"approval_id": approval["approval_id"]}))
        hold = {"reason": reason, "approval_id": approval["approval_id"], "attempt": row["attempt"],
                "brief_sha256": row["brief_sha256"], "subject_sha256": digest,
                "policy_verdict": policy.get("policy_verdict"), "run_status_before": before}
        store.release_lease(conn, lease["lease_id"])
        store.transition(conn, machine=TASK, subject_id=row["task_id"], to_state="BLOCKED", actor=self._writer,
                         run_id=row["run_id"], reason=f"worker held for approval {approval['approval_id']}",
                         evidence_level="EXECUTION_OBSERVED", bump=False,
                         extra_updates={"owner": None, "lease_id": None, "hold_json": hold},
                         payload=recovery.derived({"hold": hold}))
        return {"task_id": row["task_id"], "approval_id": approval["approval_id"], "hold": hold}

    def lazy(self, *, run_id: str) -> dict[str, Any]:
        """The lazy path of design r2.2 §2.4.2, for the start of a claim and of a submit and
        the end of a submit: the run's jobs are looked at again, what is decided is settled
        (:meth:`settle`), and the run's triggers are reacted to. With nothing to do it takes
        no write lock. A reaction that fails is a warning of the call, never its refusal."""
        from ..orchestration import reactor   # the reactor settles through this service; no cycle at call time

        # When the stage waits exclusively on child workflows, an idle parent
        # owns no work to reconcile. Its empty local queue is not an approval
        # request. Keep real local work on the original settlement path.
        waiting_on_children = self.conn.execute(
            "SELECT json_extract(c.state_json,'$.pending_interrupt.payload.wait_for')"
            " FROM runs r JOIN checkpoints c ON c.run_id=r.run_id"
            " WHERE r.run_id=? AND r.status='WAITING_CLIENT' AND r.phase IS NOT NULL"
            " ORDER BY c.seq DESC LIMIT 1", (run_id,),
        ).fetchone()
        if waiting_on_children and waiting_on_children[0] == "child_runs":
            local = self.conn.execute(
                "SELECT EXISTS(SELECT 1 FROM tasks WHERE run_id=:id AND status NOT IN ('ACCEPTED','CANCELLED'))"
                " OR EXISTS(SELECT 1 FROM jobs WHERE run_id=:id AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED'))"
                " OR EXISTS(SELECT 1 FROM approvals WHERE run_id=:id AND status='PENDING')"
                " OR EXISTS(SELECT 1 FROM outbox WHERE run_id=:id AND consumed_at IS NULL)",
                {"id": run_id},
            ).fetchone()[0]
            if not local:
                return {"reconciled": [], "settled": {}, "reactions": []}
        return reactor.lazy(self.ctx.child(conn=self.conn), run_id, teams=self)

    def _settle(self, conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        dispatch_ended = self._settle_failed_dispatches(conn, run_id)
        inspected = self._inspect_workers(conn, run_id)
        decided = self._settle_worker_holds(conn, run_id)
        out = recovery.settle(
            conn, run_id=run_id, enter_ready=self._enter_ready,
            release_held=self._release_held, approval_status=seams.approval_status,
            actor=self._writer, end_chain=self._end_chain,
        )
        settled_holds = list(out.get("released") or []) + list(out.get("refused_holds") or []) + decided
        if settled_holds:
            restored = self._after_holds(conn, run_id, [item.get("run_status_before") for item in settled_holds])
            if restored:
                out["run_restored"] = restored
        return {**inspected, **({"not_dispatchable": dispatch_ended} if dispatch_ended else {}),
                **({"worker_decisions": decided} if decided else {}),
                **{key: value for key, value in out.items() if value}}

    def _dispatch_failed(self, conn: sqlite3.Connection, task_id: str, exc: GxError) -> None:
        task = self._task_row(conn, task_id)
        store.append_event(conn, kind="task.dispatch_failed", actor=self._writer, run_id=task["run_id"],
                           subject_kind=TASK, subject_id=task_id, reason=f"{exc.code}: {exc.message}"[:500],
                           evidence_level="EXECUTION_OBSERVED", payload=recovery.derived({
                               "source": "ready_compile", "code": exc.code, "reason": exc.message[:300],
                               "attempt": task["attempt"]}))

    def _settle_failed_dispatches(self, conn: sqlite3.Connection, run_id: str) -> list[str]:
        ended = []
        for failed in recovery.failed_dispatches(conn, run_id):
            why = f"dispatch failed {failed['failures']} times since READY; the bound is {failed['max_attempts']}"
            store.transition(conn, machine=TASK, subject_id=failed["task_id"], to_state="BLOCKED", actor=self._writer,
                             run_id=run_id, reason=why, evidence_level="EXECUTION_OBSERVED", bump=False,
                             extra_updates={"blocked_reason": recovery.HOLD_NOT_DISPATCHABLE,
                                            "hold_json": {"reason": recovery.HOLD_NOT_DISPATCHABLE,
                                                          "dispatch_failures": failed["failures"]}})
            self._end_chain(conn, failed["task_id"], recovery.HOLD_NOT_DISPATCHABLE, why)
            ended.append(failed["task_id"])
        return ended

    def _settle_worker_holds(self, conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        """Apply only a person's hash- and attempt-bound worker-refusal decision."""
        decided = []
        for task in conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='BLOCKED' AND hold_json IS NOT NULL",
                                 (run_id,)).fetchall():
            hold = store.loads(task, "hold_json", {}) or {}
            if hold.get("reason") not in recovery.HOLDS_AWAITING_APPROVAL or hold.get("submission_id"):
                continue
            approval = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (hold.get("approval_id"),)).fetchone()
            if approval is None or approval["action_kind"] != "worker_refusal":
                continue
            state = seams.approval_status(conn, approval["approval_id"])
            if state not in ("GRANTED", "DENIED"):
                continue
            request = store.loads(approval, "request_json", {}) or {}
            if approval["subject_kind"] != TASK or approval["subject_id"] != task["task_id"] \
                    or approval["subject_sha256"] != hold.get("subject_sha256") \
                    or request.get("attempt") != task["attempt"] \
                    or request.get("brief_sha256") != task["brief_sha256"]:
                continue
            detail = store.loads(approval, "decision_json", {}) or {}
            choice = detail.get("choice")
            if (state, choice) not in (("GRANTED", "reword"), ("GRANTED", "authorize_once"), ("DENIED", "skip")):
                continue
            if choice == "authorize_once" and not detail.get("model"):
                continue
            payload = {"approval_id": approval["approval_id"], "attempt": task["attempt"],
                       "brief_sha256": task["brief_sha256"], **detail}
            store.append_event(conn, kind="task.worker_refusal_decided", actor=self._writer, run_id=run_id,
                               subject_kind=TASK, subject_id=task["task_id"], evidence_level="EXECUTION_OBSERVED",
                               reason=f"a person decided {choice}", payload=recovery.derived(payload))
            if choice == "skip":
                conn.execute("UPDATE tasks SET hold_json=NULL, blocked_reason=? WHERE task_id=?",
                             (recovery.HOLD_SKIPPED, task["task_id"]))
                self._end_chain(conn, task["task_id"], recovery.HOLD_SKIPPED, "a person decided to skip the refused task")
            else:
                if choice == "authorize_once":
                    conn.execute("UPDATE tasks SET required_model=? WHERE task_id=?", (detail["model"], task["task_id"]))
                self._enter_ready(conn, task["task_id"], f"a person decided {choice}; no attempt consumed")
            decided.append({"task_id": task["task_id"], "approval_id": approval["approval_id"],
                            "choice": choice, "run_status_before": hold.get("run_status_before")})
        return decided

    def _after_holds(self, conn: sqlite3.Connection, run_id: str, befores: Sequence[str | None]) -> str | None:
        """Put a run that waited only for held submissions back where it was, once none waits.

        The run went to WAITING_APPROVAL when a submission was held and recorded where it
        came from; a hold that arrived while it already waited for another kept that state.
        When no held submission of the run waits for a person any more -- whether its
        approval was granted, denied, revoked or expired, or its release failed -- the run
        is not waiting for an approval of that kind and goes back. A run that was waiting
        for an approval of its own (a node's interrupt) when the first hold came recorded
        nothing to go back to and stays as it is. Returns the state it was put in, if any.
        """
        if recovery.pending_holds(conn, run_id) or conn.execute(
            "SELECT 1 FROM approvals WHERE run_id=? AND status='PENDING'"
            " AND action_kind IN ('model_switch','worker_refusal') LIMIT 1", (run_id,),
        ).fetchone():
            return None
        run = self._run_row(conn, run_id)
        if run["status"] != "WAITING_APPROVAL":
            return None
        before = next((state for state in befores if state), None) or self._run_before_task_hold(conn, run)
        if not before:
            return None
        for target in (["ACTIVE"] if before == "ACTIVE" else ["ACTIVE", before]):
            current = self._run_row(conn, run_id)["status"]
            if current != target and machines.can_transition(RUN, current, target):
                store.transition(
                    conn, machine=RUN, subject_id=run_id, to_state=target, actor=self._writer,
                    reason="no held submission of the run waits for a person any more",
                    evidence_level="EXECUTION_OBSERVED",
                    payload=recovery.derived({"restoring": before}),
                )
        return self._run_row(conn, run_id)["status"]

    # ------------------------------------------------------------------ ending a chain
    def _end_chain(self, conn: sqlite3.Connection, task_id: str, blocked_reason: str, why: str) -> dict[str, Any]:
        """A task has ended its chain: do, in the caller's transaction, what follows (design r2.2 §2.7).

        1. The task stays BLOCKED with every refusal on its row; why it ended is its
           ``blocked_reason`` (a column at contract r5, the hold's reason until then). It
           counts as ended, not open: only a person's revival moves it.
        2. Every task that depends on it, however far behind, is CANCELLED: what it waited
           for will never be accepted.
        3. What the chain still held for followers that will now not be created is given back.
        4. A SELECTED or EXECUTING path the task belongs to goes BLOCKED, which frees its
           slot, with the ended chain on record.
        5. ``schedule.chain_ended`` records all of it, with the need a person can clear.
        """
        task = self._task_row(conn, task_id)
        run_id = task["run_id"]
        hold = store.loads(task, "hold_json", {}) or {}
        if store._has_column(conn, "tasks", "blocked_reason"):
            conn.execute("UPDATE tasks SET blocked_reason=?, updated_at=? WHERE task_id=?",
                         (blocked_reason, clock.now_iso(), task_id))

        cancelled: list[str] = []
        frontier = [task_id]
        while frontier:
            current = frontier.pop(0)
            for row in conn.execute(
                "SELECT t.task_id, t.status, t.lease_id FROM task_deps d JOIN tasks t ON t.task_id = d.task_id"
                " WHERE d.depends_on=? ORDER BY t.created_at, t.task_id", (current,),
            ).fetchall():
                if row["task_id"] in cancelled or row["status"] in machines.terminal_states(TASK):
                    continue
                if row["lease_id"]:
                    store.release_lease(conn, row["lease_id"])
                store.transition(
                    conn, machine=TASK, subject_id=row["task_id"], to_state="CANCELLED", actor=self._writer,
                    run_id=run_id, reason=f"its chain ended at task {task_id} ({blocked_reason}): what it waited "
                                          "for will never be accepted",
                    evidence_level="EXECUTION_OBSERVED", extra_updates={"owner": None, "lease_id": None},
                    payload=recovery.derived({"ended_by": task_id, "blocked_reason": blocked_reason,
                                              "status_at_cancel": row["status"]}),
                    bump=False,
                )
                cancelled.append(row["task_id"])
                frontier.append(row["task_id"])

        released: list[str] = []
        for head in [task_id, *cancelled]:
            released.extend(seams.release_chain(conn, budget_ref=task["budget_ref"], head_id=head,
                                                actor=self._writer,
                                                reason=f"the chain of task {task_id} ended ({blocked_reason})"))

        path_move = None
        if task["path_id"]:
            path = conn.execute("SELECT * FROM paths WHERE path_id=?", (task["path_id"],)).fetchone()
            if path is not None and path["status"] in ("SELECTED", "EXECUTING"):
                ended = {"task_id": task_id, "blocked_reason": blocked_reason, "class": hold.get("failure_class"),
                         "count": int(task["attempt"])}
                extra: dict[str, Any] = {}
                if store._has_column(conn, "paths", "ended_chains_json"):
                    extra["ended_chains_json"] = list(store.loads(path, "ended_chains_json", []) or []) + [ended]
                store.transition(
                    conn, machine="path", subject_id=path["path_id"], to_state="BLOCKED", actor=self._writer,
                    run_id=run_id, reason=f"its chain ended at task {task_id} ({blocked_reason}); the path is "
                                          "untested and its slot is free",
                    evidence_level="EXECUTION_OBSERVED", extra_updates=extra or None,
                    payload=recovery.derived({"ended_chain": ended}), bump=False,
                )
                path_move = {"path_id": path["path_id"], "from": path["status"], "to": "BLOCKED", "ended_chain": ended}

        # The need has one definition (scheduling's, per reason): the event, a pause and the plan's
        # revive name the same thing for the same task.
        from ..scheduling import needs as vocabulary   # a leaf of data and pure functions: no cycle
        need = (vocabulary.ended_task(task_id, blocked_reason, why[:400])
                if blocked_reason in vocabulary.REVIVABLE else None)
        event_id = store.append_event(
            conn, kind="schedule.chain_ended", actor=self._writer, run_id=run_id, subject_kind=TASK,
            subject_id=task_id, reason=why[:500], evidence_level="EXECUTION_OBSERVED",
            payload=recovery.derived({
                "task_id": task_id, "task_kind": task["kind"], "blocked_reason": blocked_reason,
                "attempts": int(task["attempt"]), "max_attempts": int(task["max_attempts"]),
                "refusals": len(hold.get("refusals") or []), "cancelled": cancelled, "released": released,
                "path": path_move, "need": need}),
        )
        return {"task_id": task_id, "blocked_reason": blocked_reason, "cancelled": cancelled, "released": released,
                "path": path_move, "need": need, "event_id": event_id}

    # ------------------------------------------------------------------ next_actions
    def next_actions(self, *, run_id: str, limit: int | None = None) -> dict[str, Any]:
        """The dispatch plan: what may be claimed now, and why the rest may not.

        A pure query (lead's ruling S2, 2026-11-12 20:00Z; contract: mutating false): it
        settles nothing and writes nothing. The plan is the scheduler's
        (``gx_core.scheduling``: ``snapshot.load`` then ``dispatch.plan``), in the r5 shape: its
        rows, blocked rows and plan facts (:data:`PLAN_FACTS`) as the plan answers them, cut to the
        response limits here, with ``counts`` saying what the cut left out and ``revision`` the run's.
        A lease past its end and not yet settled is neither in flight nor READY; its task is
        listed under ``blocked`` and settles at the next gx_task_claim, gx_task_submit or
        gx_run_resume. Every ``code`` is from ``contracts/errors.json``.
        """
        from ..scheduling import dispatch, snapshot   # the scheduler reads the store; no cycle at import

        self._run_row(self.conn, run_id)      # an unknown run is refused here, as before
        max_rows = int(contracts.response_limits()["max_rows"])
        cap = None if limit is None else max(0, min(int(limit), max_rows))
        snap = snapshot.load(self.conn, run_id, now=clock.now_iso())
        plan = dispatch.plan(snap, limit=cap)
        answer = plan.as_output(contract="r5")
        ready_count = int(plan.ready)
        blocked = list(answer["blocked"])
        actions: list[dict[str, Any]] = []
        used = 0
        for item in answer["next_actions"][:max_rows]:
            size = _encoded(item) + 1
            if used + size > ACTIONS_BUDGET:
                break
            actions.append(item)
            used += size
        dispatching = bool(plan.dispatching)
        shown_blocked: list[dict[str, Any]] = []
        used = 0
        for item in blocked:
            size = _encoded(item) + 1
            if used + size > BLOCKED_BUDGET or len(shown_blocked) >= max_rows:
                break
            shown_blocked.append(item)
            used += size

        warnings: list[str] = []
        if not dispatching and ready_count:
            warnings.append(
                f"run {run_id} is {snap.run.status}; {ready_count} READY task(s) are not "
                "handed out until it continues"
            )
        if dispatching and len(actions) < ready_count:
            # Fewer free slots than READY tasks, or a response cut to its size: the rest are
            # under blocked with the reason, and a later plan offers them.
            warnings.append(
                f"{ready_count - len(actions)} more READY task(s) are not offered in this plan "
                "(no free slot, or the response's size); claim some and ask again"
            )
        if len(shown_blocked) < len(blocked):
            warnings.append(f"{len(blocked) - len(shown_blocked)} more blocked task(s) are not listed")
        if any(item["brief_ref"] is None for item in actions) and "compile_brief" in seams.ON_FALLBACK:
            warnings.append(
                "no brief compiler is installed (" + seams.ON_FALLBACK["compile_brief"] + "); "
                "brief_ref is null and the task object gx_task_claim returns is the whole instruction"
            )
        return {
            "next_actions": actions, "blocked": shown_blocked,
            "revision": int(snap.run.revision),
            "counts": {"ready": ready_count, "listed": len(actions),
                       "blocked": len(blocked), "blocked_listed": len(shown_blocked)},
            **{key: answer[key] for key in PLAN_FACTS},
            "warnings": warnings,
        }

    def _blocked_row(self, row: sqlite3.Row) -> dict[str, Any]:
        status, task_id = row["status"], row["task_id"]
        if status == "PENDING":
            _ready, blocking = records.dependencies_satisfied(self.conn, task_id)
            states = {r["task_id"]: r["status"] for r in self.conn.execute(
                "SELECT task_id, status FROM tasks WHERE task_id IN ("
                + ",".join("?" for _ in blocking) + ")", list(blocking))} if blocking else {}
            return {"task_id": task_id, "code": "DEPENDENCY_UNMET",
                    "reason": "waiting for " + ", ".join(
                        f"{dep} ({states.get(dep, 'missing')})" for dep in blocking
                    ) + " to be ACCEPTED"}
        if status in ("CLAIMED", "RUNNING"):
            lease = recovery.live_lease(self.conn, task_id)
            holder = f"{lease['owner']} until {lease['expires_at']}" if lease is not None else "nobody"
            return {"task_id": task_id, "code": "LEASE_HELD",
                    "reason": f"{status} under a lease held by {holder}"}
        if status == "BLOCKED":
            hold = store.loads(row, "hold_json", {}) or {}
            reason = hold.get("reason")
            if reason == recovery.HOLD_MODEL_POLICY:
                reason = (f"submission {hold.get('submission_id')} is held "
                          f"({hold.get('policy_verdict')}); it is judged when approval "
                          f"{hold.get('approval_id')} is GRANTED")
                if hold.get("release_error"):
                    reason += f". The approval is granted and the release failed: {hold['release_error']}"
                return {"task_id": task_id, "code": "APPROVAL_REQUIRED", "reason": reason,
                        "approval_id": hold.get("approval_id")}
            if reason == recovery.HOLD_DEPENDENCY_CANCELLED:
                return {"task_id": task_id, "code": "DEPENDENCY_UNMET",
                        "reason": "a dependency was cancelled: " + ", ".join(hold.get("dependencies") or [])}
            return {"task_id": task_id, "code": "ILLEGAL_TRANSITION",
                    "reason": f"out of attempts ({hold.get('attempts')} of {hold.get('max_attempts')}): "
                              f"{hold.get('last') or 'every attempt was refused or lapsed'}"}
        return {"task_id": task_id, "code": "ILLEGAL_TRANSITION",
                "reason": f"task is {status}; it is claimed from READY"}

    # ------------------------------------------------------------------------- claim
    def acquire_task(self, *, task_id: str, operation_id: str | None = None) -> dict[str, Any]:
        """Redeem the settled assignment from the actual worker's first callback."""
        from ..hostobs import native_context
        call = native_context.call_for_ctx(self.ctx)
        if call.tool_name != "gx_task_acquire":
            raise ScopeDenied("acquire requires its own live native callback")
        task = self._task_row(self.conn, task_id)
        self._authorize_primary(self.conn, task["run_id"])
        if not native_context.is_native_run(self.conn, task["run_id"]):
            raise ScopeDenied("acquire requires a native task")
        assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
        if assignment is None or assignment["subject"] != self.ctx.principal.subject:
            raise ScopeDenied("the observed worker does not hold this assignment")
        if assignment["offered_attempt"] is None:
            raise StalePlan("this historical assignment needs a new handoff for native_v2")
        request = {"task_id": task_id, "worker": call.worker_ref,
                   "generation": assignment["assignment_generation"], "attempt": assignment["offered_attempt"]}
        operation_id = operation_id or ids.sha256_of_obj(request)
        key = store.tool_key(call.tool_name, operation_id)
        prior = self._acquire_replay(key, request)
        if prior is not None:
            return prior
        return self._claim(task_id=task_id, worker_role=task["role"],
                           expected_attempt=assignment["offered_attempt"],
                           acquire=(key, request, operation_id))

    def _acquire_replay(self, key, request):
        row = self.conn.execute("SELECT * FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["request_sha"] != ids.sha256_of_obj(request):
            raise IdempotencyConflict("acquire operation belongs to another task, assignment or worker")
        receipt = store.stored_response(self.conn, key)
        if not receipt:
            raise OutcomeUnknown("acquire has no completed receipt")
        eligible = self._current_acquisition(request["task_id"], receipt["lease_ref"], request["worker"])
        return {**receipt, "eligible": eligible, "replayed": True}

    def activity_lease(self, task_id):
        """Bind the inference input to the actual native worker holding this acquired task."""
        from ..hostobs import native_context
        call = native_context.call_for_ctx(self.ctx)
        task = self._task_row(self.conn, task_id)
        self._authorize_primary(self.conn, task["run_id"])
        if call.tool_name != "gx_model_activity" or not native_context.is_native_run(self.conn, task["run_id"]):
            raise ScopeDenied("model activity requires its native worker callback")
        if not task["lease_id"] or not self._current_acquisition(task_id, task["lease_id"], call.worker_ref):
            raise ScopeDenied("the observed worker does not hold an eligible acquisition for this task")
        return task["lease_id"]

    def _current_acquisition(self, task_id, lease_ref, worker_ref):
        from ..hostobs import native_observation
        snapshot = native_observation.snapshot(self.conn, task_id, lease_ref)
        proof, _ = native_observation.claim_receipt(self.conn, task_id, lease_ref)
        live = recovery.live_lease(self.conn, task_id)
        eligible = bool(proof["observed_worker_ref"] == worker_ref
                        and proof["principal_subject"] == self.ctx.principal.subject
                        and live and live["lease_id"] == lease_ref and snapshot["task"]["lease_id"] == lease_ref
                        and snapshot["task"]["status"] in ("CLAIMED", "RUNNING")
                        and snapshot["run"]["status"] not in (*_NO_DISPATCH, *machines.terminal_states(RUN))
                        and native_observation.lease_ownership_matches(proof, snapshot))
        if eligible:
            from ..hostobs import native_context, policy
            call = native_context.call_for_ctx(self.ctx)
            try:
                _, required, _ = native_observation.requirement(self.conn, snapshot["task"])
                native_observation._check_current(call, required)
            except (ModelPolicyViolation, ContentHashMismatch):
                eligible = False
            adverse = self.conn.execute("SELECT 1 FROM worker_observations WHERE lease_id=? "
                "AND policy_verdict IN (?,?) LIMIT 1", (lease_ref, policy.MISMATCH, policy.REFUSAL_OR_FALLBACK))
            eligible = eligible and adverse.fetchone() is None
        return eligible

    def claim(self, *, task_id: str, worker_role: str, expected_revision: int | None = None,
              lease_seconds: int | None = None, expected_attempt: int | None = None,
              as_of_event_id: int | None = None) -> dict[str, Any]:
        """Issue one admitted lease; a compiler refusal records a bounded dispatch failure."""
        try:
            return self._claim(task_id=task_id, worker_role=worker_role, expected_revision=expected_revision,
                               lease_seconds=lease_seconds, expected_attempt=expected_attempt, as_of_event_id=as_of_event_id)
        except GxError as exc:
            if exc.details.get("ready_compile_task") == task_id:
                with store.transaction(self.conn) as conn:
                    task = self._task_row(conn, task_id)
                    if task["status"] == "READY":
                        self._dispatch_failed(conn, task_id, exc)
                        self._settle_failed_dispatches(conn, task["run_id"])
                        exc.side_effect_status = "APPLIED"
            raise

    def _claim(self, *, task_id: str, worker_role: str, expected_revision: int | None = None,
              lease_seconds: int | None = None, expected_attempt: int | None = None,
              as_of_event_id: int | None = None, acquire=None) -> dict[str, Any]:
        """Lease a READY task to the caller (contract r5 ``task.rules.claim``).

        The precondition is the task's, not the run's: ``expected_attempt`` is the attempts the
        task had started when the plan was made, and ``as_of_event_id`` the plan's. A claim on a
        plan the task or the run has moved past (another attempt began, or a pause, a cancel or a
        stage move of the run is newer than the plan) answers STALE_PLAN and leaves the task as it
        was. The run revision is not compared by the tool (it moves on coordinator claims and node
        completions that change no task); ``expected_revision`` stays for a caller below the tool
        that asks for it.
        """
        from ..authz import require_scope
        tool = "gx_task_acquire" if acquire else "gx_task_claim"
        require_scope(tool, self.ctx.principal)
        config.check_holds(self.hold_scope)
        if not contracts.is_label("worker_role", worker_role):
            raise SchemaInvalid(
                f"{worker_role!r} is not a role; the one role vocabulary is "
                f"{contracts.label_values('worker_role')}",
                details={"worker_role": worker_role, "known": contracts.label_values("worker_role")},
            )
        seconds = default_lease_seconds() if lease_seconds is None else int(lease_seconds)
        if seconds < 1:
            raise SchemaInvalid("lease_seconds is a positive number of seconds",
                                details={"lease_seconds": lease_seconds})
        # The caller's view is checked against the run as it read it, before what is due is
        # settled (TASKS-07); the settlement is then a unit of its own, so it stands even when
        # the claim itself is refused (S2: the plan settles nothing, so a claim -- refused or
        # not -- is where a lapse or a decided hold is first written). Inside the claim's
        # transaction the run must still be where that settlement left it.
        run_id = self._task_row(self.conn, task_id, missing=ScopeDenied)["run_id"]
        self._authorize_primary(self.conn, run_id)
        store.check_revision(self.conn, run_id, expected_revision)
        self.lazy(run_id=run_id)
        settled_at = None if expected_revision is None else store.current_revision(self.conn, run_id)
        from ..hostobs.codex_dispatch import prepare_claim, fence_claim
        prepare_claim(self.ctx, task_id=task_id, run_id=run_id)
        if acquire or self.ctx.native_call is not None:
            from ..hostobs.native_context import call_for_ctx
            if call_for_ctx(self.ctx).tool_name != tool:
                raise ScopeDenied("the callback does not match this claim entry")
        with store.transaction(self.conn) as conn:
            if acquire:
                key, request, operation_id = acquire
                prior = self._acquire_replay(key, request)
                if prior is not None:
                    return prior
                store.begin_idempotent(conn, key, tool, request)
            fence_claim(self.ctx, task_id=task_id)
            if acquire:
                generation = conn.execute("SELECT assignment_generation FROM task_assignments WHERE task_id=?",
                                          (task_id,)).fetchone()
                if generation is None or generation[0] != request["generation"]:
                    raise StalePlan("the assignment changed during acquire")
            row = self._task_row(conn, task_id, missing=ScopeDenied)
            store.check_revision(conn, run_id, settled_at)
            self._require_current_plan(conn, row, expected_attempt=expected_attempt, as_of_event_id=as_of_event_id)
            run = self._run_row(conn, run_id)
            if run["status"] in machines.terminal_states(RUN) or run["status"] in _NO_DISPATCH:
                raise IllegalTransition(
                    f"run {run_id} is {run['status']}; no task of it is claimed",
                    details={"run_id": run_id, "run_status": run["status"], "task_id": task_id},
                )
            if row["role"] != worker_role:
                raise ScopeDenied(
                    f"task {task_id} is for role {row['role']!r}, claimed as {worker_role!r}",
                    details={"task_id": task_id, "task_role": row["role"], "worker_role": worker_role},
                )
            status = row["status"]
            if status == "PENDING":
                _ready, blocking = records.dependencies_satisfied(conn, task_id)
                raise DependencyUnmet(
                    f"task {task_id} depends on {', '.join(blocking)}, which are not ACCEPTED",
                    details={"task_id": task_id, "blocking": blocking},
                )
            if status in ("CLAIMED", "RUNNING"):
                # A job may hold the current lease open past its nominal expiry.
                # issue_lease only knows the nominal expiry and must never replace it.
                held = recovery.live_lease(conn, task_id)
                if held is not None:
                    raise LeaseHeld(
                        f"task {task_id} is leased by {held['owner']!r}",
                        details={"task_id": task_id, "owner": held["owner"],
                                 "expires_at": held["expires_at"], "lease_ref": held["lease_id"]},
                    )
            if status != "READY":
                blocked = self._blocked_row(row)
                raise IllegalTransition(
                    f"task {task_id} is {status} and is not claimed: {blocked['reason']}",
                    details={"task_id": task_id, "status": status, "hold": store.loads(row, "hold_json", None)},
                )
            satisfied, blocking = records.dependencies_satisfied(conn, task_id)
            if not satisfied:
                raise DependencyUnmet(
                    f"task {task_id} depends on {', '.join(blocking)}, which are not ACCEPTED",
                    details={"task_id": task_id, "blocking": blocking},
                )
            # Validate persisted team scope under the same task-claim write lock.
            # Legacy tasks with no assignment retain their original admission rules.
            from .manager import require_assignment
            require_assignment(self.ctx, row)
            attempt = int(row["attempt"]) + 1
            # max_attempts bounds the attempts consumed, not the attempts started (contract r5).
            if recovery.consumed(conn, row) >= int(row["max_attempts"]):
                raise IllegalTransition(
                    f"task {task_id} has used its {row['max_attempts']} attempts",
                    details={"task_id": task_id, "attempt": int(row["attempt"]),
                             "attempts_consumed": recovery.consumed(conn, row),
                             "max_attempts": int(row["max_attempts"])},
                )
            self._require_independent_claimant(conn, row)

            # Advisory plans can become stale between calls. Re-read and admit while
            # holding the very write transaction that issues the lease: two workers
            # cannot both consume its final slot, bypass a hold, or overtake an owed task.
            from ..scheduling import dispatch, snapshot
            snap = snapshot.load(conn, run_id, now=clock.now_iso())
            admission = dispatch.admit_claim(snap, task_id)
            if not admission.admitted:
                raise BY_CODE[admission.code](admission.reason, details=admission.as_dict())
            minimum = snap.by_id[task_id].lease_seconds
            if minimum is not None:
                seconds = max(seconds, minimum)

            lease = store.issue_lease(
                conn, task_id=task_id, owner=self.ctx.actor, worker_role=worker_role,
                seconds=seconds, attempt=attempt, principal=self.ctx.principal.subject,
            )
            store.transition(
                conn, machine=TASK, subject_id=task_id, to_state="CLAIMED", actor=self.ctx.actor,
                run_id=run_id, reason=f"claimed by {self.ctx.principal.subject} as attempt {attempt}",
                evidence_level="EXECUTION_OBSERVED",
                extra_updates={"owner": self.ctx.actor, "lease_id": lease["lease_id"], "attempt": attempt},
                payload={"lease_id": lease["lease_id"], "worker_role": worker_role, "attempt": attempt},
                bump=False,
            )
            # Consume only this request's Host-verified causal evidence. No Host I/O
            # occurs here and client-supplied correlation strings grant nothing.
            from ..hostobs.codex_dispatch import record_claim_binding
            record_claim_binding(self.ctx, lease_id=lease["lease_id"])
            if row["brief_ref"] is None:
                self._compile_brief(conn, task_id, raise_errors=True)
            fresh = task_mapping(conn, self._task_row(conn, task_id))
            if acquire:
                from ..hostobs.native_spawn import dispatch_for_worker
                call = self.ctx.native_call
                receipt = {"lease_ref": lease["lease_id"], "expires_at": lease["expires_at"],
                           "task_id": task_id, "run_id": run_id, "attempt": attempt,
                           "brief_ref": fresh["brief_ref"], "brief_sha256": fresh["brief_sha256"],
                           "assignment_generation": request["generation"], "operation_id": operation_id,
                           "dispatch_operation_id": dispatch_for_worker(
                               conn, project_ref=call._issuer.project_ref, session_id=call.session_id,
                               parent_thread_id=call.parent_thread_id, thread_id=call.thread_id),
                           "eligible": True, "replayed": False}
                store.complete_idempotent(conn, key, receipt)
                return receipt
            return {
                "lease_ref": lease["lease_id"], "expires_at": lease["expires_at"],
                "task": task_view(fresh), "attempt": attempt,
                "brief_ref": fresh["brief_ref"], "brief_sha256": fresh["brief_sha256"],
                "revision": store.current_revision(conn, run_id),
            }

    @staticmethod
    def _require_current_plan(conn: sqlite3.Connection, row: sqlite3.Row, *, expected_attempt: int | None,
                              as_of_event_id: int | None) -> None:
        """STALE_PLAN when the plan the claim was made on is older than the task or the run.

        Read after the claim's own settlement, under its write lock: what time decided is
        already written, so the comparison is with the task as it now is.
        """
        task_id, run_id = row["task_id"], row["run_id"]
        if expected_attempt is not None and int(expected_attempt) != int(row["attempt"]):
            raise StalePlan(
                f"task {task_id} has started {int(row['attempt'])} attempt(s); the plan this claim was made on "
                f"said {int(expected_attempt)}. Read the plan again (gx_next_actions)",
                details={"task_id": task_id, "expected_attempt": int(expected_attempt),
                         "attempt": int(row["attempt"]), "reason": "attempt_moved"},
            )
        if as_of_event_id is None:
            return
        moves = ["(kind='run.transition' AND new_state IN ('PAUSED','CANCELLED'))"]
        if PLAN_MOVES:
            moves.append(f"kind IN ({','.join('?' for _ in PLAN_MOVES)})")
        moved = conn.execute(
            f"SELECT event_id, kind, new_state FROM events WHERE run_id=? AND event_id>? AND ({' OR '.join(moves)})"
            " ORDER BY event_id LIMIT 1", (run_id, int(as_of_event_id), *PLAN_MOVES)).fetchone()
        if moved is not None:
            what = f"{moved['kind']} to {moved['new_state']}" if moved["new_state"] else moved["kind"]
            raise StalePlan(
                f"run {run_id} moved after the plan this claim was made on (event {moved['event_id']}: {what}, "
                f"newer than {int(as_of_event_id)}). Read the plan again (gx_next_actions)",
                details={"task_id": task_id, "as_of_event_id": int(as_of_event_id),
                         "event_id": int(moved["event_id"]), "kind": moved["kind"], "reason": "run_moved"},
            )

    def _require_independent_claimant(self, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        """A review task is not claimed by a principal that held the authoring task."""
        author_task = row["independent_of_task_id"]
        if not author_task:
            return
        claimant = self.ctx.principal.subject
        authors = {
            lease["principal"] or lease["owner"] for lease in conn.execute(
                "SELECT principal, owner FROM leases WHERE task_id=?", (author_task,)
            )
        }
        if claimant in authors or self.ctx.actor in authors:
            raise IndependenceRequired(
                f"task {row['task_id']} reviews the work of task {author_task}, which "
                f"{claimant!r} held; a review is claimed by a different worker, launched "
                "under its own subject (GX_SUBJECT, or `gx --as`)",
                details={"task_id": row["task_id"], "independent_of": author_task,
                         "claimant": claimant},
            )

    def _authorize_primary(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        """Stored primary authority; locators and Host reports grant no project access."""
        run = self._run_row(conn, run_id)
        if not self.ctx.principal.may_access_project(run["project_ref"]):
            raise ScopeDenied("the primary run is outside the caller's project scope",
                              details={"run_id": run_id})
        return run

    def _historical_holder(self, lease: Mapping[str, Any]) -> None:
        subject = self.ctx.principal.subject
        principal = lease["principal"]
        if principal is not None:
            same = principal == subject
        else:
            # The persisted legacy owner is backend provenance, never a current
            # request's actor field. A missing principal is not a wildcard.
            owner = lease["owner"]
            parts = owner.split(":", 2) if isinstance(owner, str) else []
            if len(parts) == 3 and parts[0] in ("cli", "mcp", "backend") and parts[1] in SCOPES:
                same = parts[2] == subject
            else:
                same = isinstance(owner, str) and owner == subject
        if not same:
            raise LeaseExpired("the requested lease has no historical holder proof for this principal",
                               details={"lease_ref": lease["lease_id"], "reason": "not_the_lease_holder"})

    def _replay_origin(self, conn: sqlite3.Connection, *, tool: str, response: Mapping[str, Any],
                       task: Mapping[str, Any], lease: Mapping[str, Any]) -> None:
        """A completed key is not its caller's identity; follow its actual result."""
        if tool == "gx_task_bind":
            identity = response.get("worker_observation_id")
            result = conn.execute("SELECT task_id,lease_id FROM worker_observations WHERE worker_observation_id=?",
                                  (identity,)).fetchone() if isinstance(identity, str) else None
            valid = (result is not None and result["task_id"] == task["task_id"]
                     and result["lease_id"] == lease["lease_id"]
                     and response.get("task_id") == task["task_id"] and response.get("run_id") == task["run_id"])
        else:
            identity = response.get("submission_id")
            result = conn.execute("SELECT task_id,lease_id,attempt FROM submissions WHERE submission_id=?",
                                  (identity,)).fetchone() if isinstance(identity, str) else None
            valid = (result is not None and result["task_id"] == task["task_id"]
                     and result["lease_id"] == lease["lease_id"]
                     and int(result["attempt"]) == int(lease["attempt"])
                     and response.get("run_id") == task["run_id"])
        if not valid:
            raise OutcomeUnknown("the completed key has no usable result bound to this historical task lease",
                                 details={"reason": "unproven_replay_origin", "tool": tool})

    def _admit_lease_entry(self, conn: sqlite3.Connection, *, tool: str, key: str,
                           request: Mapping[str, Any], task_id: str | None, lease_ref: str,
                           expect_replay: bool = False) -> _LeaseEntry:
        """One DB-only entry decision before lazy, readers, artifacts or key writes.

        The primary is fixed first. Secondary family never selects whether these
        checks apply. The same gate runs again under the canonical writer lock.
        """
        from ..authz import require_scope
        require_scope(tool, self.ctx.principal)
        lease = None
        if task_id is None:  # bind's primary locator is its lease
            lease = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_ref,)).fetchone()
            if lease is None:
                raise LeaseExpired("the requested primary lease does not exist", details={"lease_ref": lease_ref})
            task_id = lease["task_id"]
        task = self._task_row(conn, task_id)
        run = self._authorize_primary(conn, task["run_id"])
        digest = ids.sha256_of_obj(dict(request))
        prior = conn.execute("SELECT * FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
        response = None
        if prior is not None:
            if prior["scope"] != tool or prior["request_sha"] != digest:
                raise IdempotencyConflict("the key belongs to another tool or request body",
                                          details={"key": key, "scope": prior["scope"]})
            try:
                response = store.loads(prior, "response_json")
            except (ValueError, TypeError):
                response = None
            if (prior["completed_at"] is None or prior["side_effect_status"] == "UNKNOWN"
                    or not isinstance(response, dict)):
                raise OutcomeUnknown("the existing operation has no usable completed outcome",
                                     details={"idempotency_key": key, "side_effect_status": prior["side_effect_status"]})
        elif expect_replay:
            raise OutcomeUnknown("the verified replay key disappeared before its canonical gate",
                                 details={"idempotency_key": key, "reason": "replay_key_disappeared"})
        if lease is None:
            lease = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_ref,)).fetchone()
        if lease is None or lease["task_id"] != task["task_id"]:
            raise LeaseExpired("the requested secondary lease does not belong to the primary task",
                               details={"lease_ref": lease_ref, "task_id": task["task_id"]})
        self._historical_holder(lease)
        base = dict(tool=tool, key=key, request_sha256=digest, principal_subject=self.ctx.principal.subject,
                    request_id=self.ctx.request_id, task_id=task["task_id"], run_id=task["run_id"],
                    project_ref=run["project_ref"], lease_id=lease_ref)
        if prior is not None:
            self._replay_origin(conn, tool=tool, response=response, task=task, lease=lease)
            return _LeaseEntry(disposition=COMPLETED_REPLAY_VERIFIED, policy_family="historical",
                               snapshot_json="", **base)
        # All fresh associations and effective liveness are authorized before a
        # policy family chooses the later observation route, including legacy.
        self._lease_for(conn, lease_ref, task_id=task["task_id"])
        if (task["lease_id"] != lease_ref or task["status"] not in ("CLAIMED", "RUNNING")
                or int(task["attempt"]) != int(lease["attempt"])):
            raise LeaseExpired("the requested lease is not the primary task's current attempt",
                               details={"lease_ref": lease_ref, "task_id": task["task_id"],
                                        "reason": "not_the_tasks_lease"})
        from ..hostobs import observation, host_policy, codex_worker_observation
        observation.policy_of_run(conn, task["run_id"])
        family = "codex" if host_policy.scope_of_run(run) == "codex" else "legacy"
        from ..hostobs import native_context, native_observation
        if native_context.source_of_run(run) == native_context.SOURCE:
            if run["status"] == "PAUSED" or run["status"] in machines.terminal_states(RUN):
                raise StalePlan("the run no longer accepts worker writes", details={"status": run["status"]})
            snapshot = native_observation.snapshot(conn, task["task_id"], lease_ref)
        else:
            snapshot = codex_worker_observation.snapshot(conn, task["task_id"], lease_ref)
            snapshot["primary_run"] = dict(run)
        return _LeaseEntry(disposition=FRESH_AUTHORIZED, policy_family=family,
                           snapshot_json=ids.canonical_json(snapshot), **base)

    @staticmethod
    def _fence_lease_entry(expected: _LeaseEntry, current: _LeaseEntry) -> None:
        if (expected.disposition != FRESH_AUTHORIZED or current.disposition != FRESH_AUTHORIZED
                or expected != current):
            raise StalePlan("the authorized primary task/lease/assignment/policy/event snapshot moved during preparation")


    # -------------------------------------------------------------------------- bind
    def _lease_for(self, conn: sqlite3.Connection, lease_ref: str, *, task_id: str | None = None) -> sqlite3.Row:
        """The live lease, held by the principal who is asking."""
        try:
            lease = store.validate_lease(conn, lease_ref, task_id=task_id)
        except LeaseExpired:
            raw = conn.execute("SELECT task_id FROM leases WHERE lease_id=?", (lease_ref,)).fetchone()
            effective = recovery.live_lease(conn, raw["task_id"]) if raw is not None else None
            if effective is None or effective["lease_id"] != lease_ref \
                    or (task_id is not None and effective["task_id"] != task_id):
                raise
            lease = effective
        holder = lease["principal"]
        if holder is not None and holder != self.ctx.principal.subject:
            raise LeaseExpired(
                f"lease {lease_ref} was issued to {holder!r}, not to {self.ctx.principal.subject!r}; "
                "a lease is used by the worker that claimed it",
                details={"lease_ref": lease_ref, "reason": "not_the_lease_holder", "holder": holder},
            )
        return lease

    def bind(
        self, *, lease_ref: str, host: str, evidence_level: str, idempotency_key: str,
        agent_ref: str | None = None, host_session_ref: str | None = None,
        model: str | None = None, effort: str | None = None, delegation_mode: str | None = None,
    ) -> dict[str, Any]:
        """Record what the worker under a lease says about itself, and judge it.

        A worker outside the pinned model is refused here, before it does the work. The
        refusal keeps the observation: the user is told when, and which model, from rows
        that exist.
        """
        request = {"lease_ref": lease_ref, "host": host, "agent_ref": agent_ref,
                   "host_session_ref": host_session_ref, "model": model, "effort": effort,
                   "delegation_mode": delegation_mode, "evidence_level": evidence_level}
        key = store.tool_key("gx_task_bind", idempotency_key)
        refused: dict[str, Any] | None = None
        from ..hostobs import codex_worker_observation as codex_observation
        entry = self._admit_lease_entry(self.conn, tool="gx_task_bind", key=key, request=request,
                                        task_id=None, lease_ref=lease_ref)
        session_basis = "reported" if host_session_ref else None
        native = None
        if entry.disposition == FRESH_AUTHORIZED:
            from ..hostobs.native_context import is_native_run, call_for_ctx
            if is_native_run(self.conn, entry.run_id) and not host_session_ref:
                host_session_ref = call_for_ctx(self.ctx).session_id
                session_basis = "host_protocol"
            elif not host_session_ref:
                host_session_ref = seams.launch_session_ref(host)
                session_basis = "launch_environment" if host_session_ref else None
            if entry.policy_family == "codex":
                native = codex_observation.prepare(self.ctx, lease_id=lease_ref, operation="bind",
                                                    task_id=entry.task_id, _entry=entry)
        with store.transaction(self.conn) as conn:
            current = self._admit_lease_entry(conn, tool="gx_task_bind", key=key, request=request,
                task_id=entry.task_id, lease_ref=lease_ref,
                expect_replay=entry.disposition == COMPLETED_REPLAY_VERIFIED)
            record = store.begin_idempotent(conn, key, "gx_task_bind", request)
            if record.replay and record.response is not None:
                return {**record.response, "replayed": True}
            self._fence_lease_entry(entry, current)
            native = codex_observation.fence(conn, native)
            if not record.fresh:
                raise OutcomeUnknown(
                    f"bind key {idempotency_key!r} was claimed and never completed",
                    details={"idempotency_key": idempotency_key},
                )
            lease = self._lease_for(conn, lease_ref)
            task_id = lease["task_id"]
            row = self._task_row(conn, task_id)
            answer = seams.bind_worker(
                self.ctx.child(conn=conn), task_id=task_id, lease_id=lease_ref, host=host,
                host_session_ref=host_session_ref, agent_ref=agent_ref, model=model, effort=effort,
                delegation_mode=delegation_mode, evidence_level=evidence_level,
                **({"_codex_prepared": native} if native is not None else {}),
            )
            if answer.get("action") == "refuse":
                store.forget_idempotent(conn, key)
                store.append_event(
                    conn, kind="task.bind_refused", actor=self.ctx.actor, run_id=row["run_id"],
                    subject_kind=TASK, subject_id=task_id,
                    reason="; ".join(answer.get("reasons") or [])[:500] or answer.get("policy_verdict"),
                    evidence_level=evidence_level,
                    payload={"lease_id": lease_ref, "policy_verdict": answer.get("policy_verdict"),
                             "worker_observation_id": answer.get("worker_observation_id"),
                             "reported": answer.get("reported"), "required": answer.get("required")},
                )
                refused = answer
                self._hold_worker(conn, row, lease, answer, reason=recovery.HOLD_MODEL_POLICY)
            else:
                from ..jobs import leases as job_leases
                job_leases.renew(conn, lease, actor=self._writer)
                if row["status"] == "CLAIMED":
                    store.transition(
                        conn, machine=TASK, subject_id=task_id, to_state="RUNNING",
                        actor=self.ctx.actor, run_id=row["run_id"],
                        reason=f"{host} worker bound ({answer.get('policy_verdict')})",
                        evidence_level=evidence_level,
                        payload={"worker_observation_id": answer.get("worker_observation_id"),
                                 "policy_verdict": answer.get("policy_verdict")},
                        bump=False,
                    )
                response = {
                    "bound": True, "worker_observation_id": answer.get("worker_observation_id"),
                    "observed": answer.get("observed") or {},
                    "policy_verdict": answer.get("policy_verdict"), "task_id": task_id,
                    "run_id": row["run_id"], "required": answer.get("required"),
                    "reasons": answer.get("reasons") or [],
                    "host_session_ref": host_session_ref, "host_session_ref_basis": session_basis,
                    "note": "a correlation with its evidence level, not identity authentication",
                }
                store.complete_idempotent(conn, key, response)
                return response
        # The transaction above committed the observation of the worker that was turned away.
        raise ModelPolicyViolation(
            "the worker is outside the run's agent policy and is refused before it does the "
            "work: " + ("; ".join(refused.get("reasons") or []) or str(refused.get("policy_verdict"))),
            side_effect_status="APPLIED",
            details={"lease_ref": lease_ref, "policy_verdict": refused.get("policy_verdict"),
                     "required": refused.get("required"), "reported": refused.get("reported"),
                     "worker_observation_id": refused.get("worker_observation_id"),
                     "deviation": refused.get("deviation")},
        )

    # ------------------------------------------------------------------------ submit
    def finish_task(self, *, task_id: str, manifest_ref: str, summary: str | None = None,
                    operation_id: str | None = None) -> dict[str, Any]:
        """Record the original bytes, hand-in and validation trigger as one GX write."""
        from ..artifacts.delivery import load_manifest, register_manifest
        from ..hostobs import native_context, native_observation

        call = native_context.call_for_ctx(self.ctx)
        if call.tool_name != "gx_task_finish":
            raise ScopeDenied("finish requires its own native callback")
        task = self._task_row(self.conn, task_id)
        run_id = task["run_id"]
        run = self._authorize_primary(self.conn, run_id)
        if native_context.source_of_run(run) != native_context.SOURCE:
            raise ScopeDenied("finish requires a native task")
        # A replay can arrive after validation or after a later attempt started.
        # Find this exact committed delivery before selecting the current lease.
        previous = self.conn.execute(
            "SELECT json_extract(payload_json,'$.lease_ref') FROM events "
            "WHERE kind='submission.recorded' AND subject_id=? AND actor=? "
            "AND json_extract(payload_json,'$.manifest_ref')=? "
            "ORDER BY event_id DESC LIMIT 1", (task_id, self.ctx.actor, manifest_ref)).fetchone()
        lease_id = previous[0] if previous else task["lease_id"]
        if operation_id:
            saved = store.stored_response(self.conn, store.tool_key(call.tool_name, operation_id))
            if saved:
                source = self.conn.execute("SELECT lease_id FROM submissions WHERE submission_id=?",
                                           (saved["submission_id"],)).fetchone()
                if source:
                    lease_id = source[0]
        if lease_id is None:
            last = self.conn.execute("SELECT lease_id FROM leases WHERE task_id=? AND principal=? "
                "ORDER BY attempt DESC LIMIT 1", (task_id, self.ctx.principal.subject)).fetchone()
            lease_id = last[0] if last else None
        holder = self.conn.execute("SELECT * FROM leases WHERE lease_id=? AND principal=?",
            (lease_id, self.ctx.principal.subject)).fetchone()
        if holder is None:
            raise LeaseExpired("the caller has no lease for this delivery")
        lease_ref = holder["lease_id"]
        operation_id = operation_id or ids.sha256_of_obj(["finish", call.worker_ref, task_id, lease_ref])
        key = store.tool_key(call.tool_name, operation_id)
        request = dict(task_id=task_id, lease_ref=lease_ref, worker=call.worker_ref,
                       manifest_ref=manifest_ref, summary=summary)
        def admit(conn):
            return self._admit_lease_entry(conn, tool=call.tool_name, key=key,
                request=request, task_id=task_id, lease_ref=lease_ref)
        entry = admit(self.conn)
        if entry.disposition == COMPLETED_REPLAY_VERIFIED:
            return {**store.stored_response(self.conn, key), "replayed": True}
        prepared = native_observation.prepare(self.ctx, lease_id=lease_ref, operation="finish",
                                              task_id=task_id, _entry=entry)
        manifest = load_manifest(self.artifacts, manifest_ref, run_id=run_id, task_id=task_id,
                                 attempt=holder["attempt"])
        self._bundle_ref(task, manifest.entries)
        from ..research.model_activity import require_profile_product
        require_profile_product(self.conn, run, task, lease_ref, manifest.entries)
        with store.transaction(self.conn) as conn:
            current = admit(conn)
            if current.disposition == COMPLETED_REPLAY_VERIFIED:
                return {**store.stored_response(conn, key), "replayed": True}
            self._fence_lease_entry(entry, current)
            fenced = native_observation.fence(conn, prepared)
            store.begin_idempotent(conn, key, call.tool_name, request)
            self._require_independent_worker(conn, task, holder)
            policy = native_observation.verdict_for(conn, task_id=task_id, lease_id=lease_ref,
                                                    _codex_prepared=fenced)
            refs = register_manifest(self.artifacts, manifest, actor=self.ctx.actor)
            if task["status"] == "CLAIMED":
                store.transition(conn, machine=TASK, subject_id=task_id, to_state="RUNNING",
                                 actor=self._writer, reason="worker handed in its product", bump=False)
            store.transition(conn, machine=TASK, subject_id=task_id, to_state="SUBMITTED",
                             actor=self._writer, reason=(summary or "worker handed in its product")[:500],
                             payload={"artifact_refs": refs}, bump=False)
            store.transition(conn, machine=TASK, subject_id=task_id, to_state="VALIDATING",
                             actor=self._writer, reason="durable delivery awaits validation", bump=False)
            submission_id = ids.new_id("submission")
            base = dict(submission_id=submission_id, task_id=task_id, lease_id=lease_ref,
                        attempt=holder["attempt"], brief_sha256=task["brief_sha256"],
                        content_sha256=ids.sha256_of_obj(request), bundle_kind=task["bundle_kind"],
                        artifact_refs=refs, worker_observation_id=policy["worker_observation_id"],
                        policy_verdict=policy["policy_verdict"])
            receipt = dict(run_id=run_id, task_id=task_id, submission_id=submission_id, attempt=holder["attempt"],
                           manifest_ref=manifest_ref, artifact_refs=refs, operation_id=operation_id,
                           delivery_status="recorded", structure_status="manifest_verified",
                           scientific_status="pending_review", replayed=False)
            if policy["action"] != "accept":
                self._hold(conn, task, holder, base, policy)
                receipt["scientific_status"] = "held"
            else:
                self._insert_submission(conn, base, result="PENDING_VALIDATION", verdict=None,
                    validation={"structure_status": "manifest_verified", "manifest_ref": manifest_ref}, records_ref=None)
            event = store.append_event(conn, kind="submission.recorded", actor=self.ctx.actor, run_id=run_id,
                subject_kind=TASK, subject_id=task_id, evidence_level="EXECUTION_OBSERVED",
                payload={"submission_id": submission_id, "manifest_ref": manifest_ref, "lease_ref": lease_ref,
                         "summary": summary, "operation_id": operation_id, "native_call": call.as_evidence()})
            if policy["action"] == "accept":
                store.enqueue_outbox(conn, topic="submission.recorded",
                    idempotency_key=f"submission.recorded:{submission_id}",
                    payload={"run_id": run_id, "event_id": event, "subject": submission_id, "task_id": task_id})
            store.complete_idempotent(conn, key, receipt)
            return receipt

    def validate_delivery(self, *, submission_id: str) -> dict[str, Any]:
        """Apply a committed delivery to its own task/attempt, once, after worker exit."""
        from ..hostobs.native_observation import snapshot

        if self.conn.in_transaction:
            raise RuntimeError("prepare delivery validation outside the write transaction")
        def submission():
            row = self.conn.execute("SELECT * FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()
            if row is None:
                raise ArtifactNotFound("unknown submission", details={"submission_id": submission_id})
            return row

        sub = submission()
        if sub["result"] != "PENDING_VALIDATION":
            return {"submission_id": submission_id, "result": sub["result"]}
        task = self._task_row(self.conn, sub["task_id"])
        run = self._authorize_primary(self.conn, task["run_id"])
        if run["status"] == "PAUSED":
            return {"defer": "validation waits for project resume"}
        if (run["status"] in machines.terminal_states(RUN) or task["status"] != "VALIDATING"
                or task["attempt"] != sub["attempt"] or task["lease_id"] != sub["lease_id"]):
            with store.transaction(self.conn) as tx:
                current = submission()
                if current["result"] != "PENDING_VALIDATION":
                    return {"submission_id": submission_id, "result": current["result"]}
                now = self._task_row(tx, sub["task_id"])
                current_run = self._run_row(tx, now["run_id"])
                if (now["lease_id"] == sub["lease_id"] and now["attempt"] == sub["attempt"]
                        and now["status"] == "VALIDATING" and current_run["status"] not in machines.terminal_states(RUN)):
                    raise StalePlan("task eligibility moved during validation recovery")
                tx.execute("UPDATE submissions SET result='SUPERSEDED', "
                    "validation_json=json_set(validation_json,'$.superseded',1) WHERE submission_id=?",
                    (submission_id,))
                store.release_lease(tx, sub["lease_id"])
                return {"submission_id": submission_id, "result": "SUPERSEDED"}
        refs = store.loads(sub, "artifact_refs_json", [])
        before = snapshot(self.conn, sub["task_id"], sub["lease_id"])
        described = self._verified_artifacts(self.conn, task["run_id"], refs)
        bundle_ref = self._bundle_ref(task, described)
        inputs = self._prepare_judgment(self.conn, task, sub["lease_id"], bundle_ref, artifact_refs=refs)
        with store.transaction(self.conn) as tx:
            current = submission()
            if current["result"] != "PENDING_VALIDATION":
                return {"submission_id": submission_id, "result": current["result"]}
            if (dict(current) != dict(sub) or snapshot(tx, sub["task_id"], sub["lease_id"]) != before):
                raise StalePlan("the recorded task or submission changed during validation")
            verdict = self._apply_judgment(tx, task, inputs)
            return self._conclude(tx, task, sub["lease_id"], {**dict(sub), "artifact_refs": refs}, verdict,
                                  {"policy_verdict": sub["policy_verdict"]}, existing=True)

    def submit(self, *, task_id: str, lease_ref: str, artifact_refs: Sequence[str],
               idempotency_key: str, summary: str | None = None) -> dict[str, Any]:
        """Take a deliverable in and let the backend decide what it is worth.

        One transaction: a refusal of the *call* (a lease that lapsed, an artifact that
        is not there, no bundle among the references) changes nothing and the same key
        can be used again. A judged bundle ends the attempt, accepted or refused.
        """
        refs = [str(ref) for ref in (artifact_refs or ())]
        request = {"task_id": task_id, "lease_ref": lease_ref, "artifact_refs": refs,
                   "summary": summary}
        from ..hostobs import codex_worker_observation as codex_observation
        key = store.tool_key("gx_task_submit", idempotency_key)
        entry = self._admit_lease_entry(self.conn, tool="gx_task_submit", key=key, request=request,
                                        task_id=task_id, lease_ref=lease_ref)
        known = {"run_id": entry.run_id}
        if entry.disposition == FRESH_AUTHORIZED:
            self.lazy(run_id=entry.run_id)
            entry = self._admit_lease_entry(self.conn, tool="gx_task_submit", key=key, request=request,
                                            task_id=task_id, lease_ref=lease_ref)
        native = None
        if entry.disposition == FRESH_AUTHORIZED and entry.policy_family == "codex":
            native = codex_observation.prepare(self.ctx, lease_id=lease_ref, operation="submit",
                                                task_id=task_id, _entry=entry)
        prepared = None

        def work(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._task_row(conn, task_id)
            lease = self._lease_for(conn, lease_ref, task_id=task_id)
            if row["lease_id"] != lease_ref or row["status"] not in ("CLAIMED", "RUNNING"):
                raise LeaseExpired(
                    f"lease {lease_ref} does not cover task {task_id} any more: the task is "
                    f"{row['status']} under {row['lease_id'] or 'no lease'}",
                    details={"task_id": task_id, "lease_ref": lease_ref, "status": row["status"],
                             "reason": "not_the_tasks_lease"},
                )
            run_id = row["run_id"]
            run = self._run_row(conn, run_id)
            if run["status"] in machines.terminal_states(RUN):
                raise PhaseDenied(
                    f"run {run_id} is {run['status']}; a submission is not taken into a run that has ended",
                    details={"run_id": run_id, "run_status": run["status"], "task_id": task_id},
                )
            described = self._verified_artifacts(conn, run_id, refs)
            bundle_ref = self._bundle_ref(row, described)
            self._require_independent_worker(conn, row, lease)

            actor = self.ctx.actor
            if row["status"] == "CLAIMED":
                # The worker never bound; the backend saw no start, only this report.
                store.transition(conn, machine=TASK, subject_id=task_id, to_state="RUNNING",
                                 actor=actor, run_id=run_id, reason="submission implies the work ran",
                                 evidence_level="CLIENT_REPORTED", bump=False)
            store.transition(conn, machine=TASK, subject_id=task_id, to_state="SUBMITTED",
                             actor=actor, run_id=run_id, reason=(summary or "worker reports completion")[:500],
                             evidence_level="CLIENT_REPORTED", payload={"artifact_refs": refs}, bump=False)
            store.transition(conn, machine=TASK, subject_id=task_id, to_state="VALIDATING",
                             actor=self._writer, run_id=run_id, reason="backend validation of the deliverable",
                             evidence_level="EXECUTION_OBSERVED", payload=recovery.derived(), bump=False)

            policy = seams.policy_verdict(conn, task_id=task_id, lease_id=lease_ref,
                **({"_codex_prepared": prepared} if prepared is not None else {}))
            if (policy.get("action")!="accept" and policy.get("policy_verdict")=="UNOBSERVABLE"
                    and self.ctx.principal.scope==SCOPE_OPERATOR
                    and row["kind"] in ("review_asset","review_evaluation")
                    and seams.human_task_review(self.ctx.child(conn=conn),task_id=task_id,lease_id=lease_ref)):
                # The validator still decides the review's independence and content.
                # A person is not an observed model; retain UNOBSERVABLE evidence.
                policy={**policy,"action":"accept","reasons":[*(policy.get("reasons") or []),
                    "a confirmed operator delivered this exact leased review; no model observation is inferred"]}
            submission_id = ids.new_id("submission")
            content_sha256 = ids.sha256_of_obj({
                "task_id": task_id, "bundle_kind": row["bundle_kind"], "summary": summary,
                "artifacts": sorted([item["artifact_ref"], item["sha256"]] for item in described),
            })
            base = {
                "submission_id": submission_id, "task_id": task_id, "lease_id": lease_ref,
                "attempt": int(row["attempt"]), "brief_sha256": row["brief_sha256"],
                "content_sha256": content_sha256,
                "worker_observation_id": policy.get("worker_observation_id"),
                "policy_verdict": policy.get("policy_verdict"), "bundle_kind": row["bundle_kind"],
                "artifact_refs": refs,
            }
            if policy.get("action") != "accept":
                return self._hold(conn, row, lease, base, policy)

            verdict = self._judge(conn, row, lease_ref, bundle_ref, artifact_refs=refs)
            return self._conclude(conn, row, lease_ref, base, verdict, policy)

        try:
            # Classify again under the same writer lock before any live/native
            # fence. A concurrent exact completion is a canonical replay.
            with store.transaction(self.conn) as conn:
                current = self._admit_lease_entry(conn, tool="gx_task_submit", key=key, request=request,
                    task_id=task_id, lease_ref=lease_ref,
                    expect_replay=entry.disposition == COMPLETED_REPLAY_VERIFIED)
                if current.disposition == FRESH_AUTHORIZED:
                    self._fence_lease_entry(entry, current)
                    prepared = codex_observation.fence(conn, native)
                response, replayed = store.run_idempotent(conn, key, "gx_task_submit", request, work)
        except (PhaseDenied, IndependenceRequired) as exc:
            # Refused for a reason that cannot change within the lease (contract r5
            # refusal_records): the lease is given back with no attempt consumed, and the refusal
            # is recorded as a dispatch failure. The keyed work above left nothing.
            self._refuse_within_lease(task_id, lease_ref, exc)
            raise
        # What this submission decided is settled at once, after its commit, fresh or replayed:
        # a refused attempt goes back to READY as the next one, or is exhausted, a dependent
        # of an accepted task becomes READY, and the acceptance is reacted to (S2; design
        # r2.2 §2.4.2 and §2.4.4: the reaction runs after every submit). A failure here
        # leaves the submission as it is on record; the next call that settles finishes it.
        if known is not None:
            try:
                self.lazy(run_id=known["run_id"])
            except Exception as exc:
                # A recorded result survives maintenance failure. Unexpected error
                # text is not exposed; it may contain external source content.
                code = exc.code if isinstance(exc, GxError) else type(exc).__name__
                message = exc.message if isinstance(exc, GxError) else "the maintenance step did not finish"
                response = {**response, "settle_warning": (
                    f"the submission is on record; settling the run after it failed ({code}): "
                    f"{message}; the next gx_task_claim, gx_task_submit or gx_run_resume settles it")}
        if replayed:
            response = {**response, "replayed": True}
        if known is not None:
            try:
                response = {**response, "ready": self._ready_after(known["run_id"], task_id)}
            except Exception as exc:
                code = exc.code if isinstance(exc, GxError) else type(exc).__name__
                response = {**response, "settle_warning": (response.get("settle_warning", "") +
                    f"; the submission is on record; ready actions could not be recomputed ({code})").lstrip("; ")}
        return response

    def _refuse_within_lease(self, task_id: str, lease_ref: str, exc: GxError) -> None:
        """What a submission refused PHASE_DENIED or INDEPENDENCE_REQUIRED leaves (contract r5
        gx_task_submit refusal_records): the lease released with no attempt consumed, the task
        READY again for the next plan (CANCELLED when its run has ended: an ended run leaves
        nothing open), and ``task.dispatch_failed {source: submit_refused, reason}``. The refusal
        then answers side_effect_status APPLIED. A lease that no longer governs its task (another
        call moved it first) is left as it is, and the refusal answers as it would have."""
        with store.transaction(self.conn) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["lease_id"] != lease_ref or row["status"] not in ("CLAIMED", "RUNNING"):
                return
            run = self._run_row(conn, row["run_id"])
            store.release_lease(conn, lease_ref)
            why = (f"the submission under lease {lease_ref} was refused {exc.code} for a reason that cannot change "
                   "within the lease; no attempt is consumed")
            if run["status"] in machines.terminal_states(RUN):
                store.transition(conn, machine=TASK, subject_id=task_id, to_state="CANCELLED", actor=self._writer,
                                 run_id=row["run_id"], reason=f"{why}, and run {row['run_id']} has ended",
                                 evidence_level="EXECUTION_OBSERVED", extra_updates={"owner": None, "lease_id": None},
                                 payload=recovery.derived({"status_at_cancel": row["status"]}), bump=False)
                after = "CANCELLED"
            else:
                if row["status"] == "RUNNING":
                    store.transition(conn, machine=TASK, subject_id=task_id, to_state="RETRYABLE", actor=self._writer,
                                     run_id=row["run_id"], reason=why, evidence_level="EXECUTION_OBSERVED",
                                     extra_updates={"owner": None, "lease_id": None}, payload=recovery.derived(),
                                     bump=False)
                self._enter_ready(conn, task_id, why)
                after = "READY"
            store.append_event(
                conn, kind="task.dispatch_failed", actor=self._writer, run_id=row["run_id"], subject_kind=TASK,
                subject_id=task_id, reason=f"{exc.code}: {exc.message}"[:500], evidence_level="EXECUTION_OBSERVED",
                payload=recovery.derived({"source": "submit_refused", "reason": str(exc.message)[:300],
                                          "code": exc.code, "lease_id": lease_ref, "attempt": int(row["attempt"])}),
            )
        exc.side_effect_status = "APPLIED"
        exc.details = {**(exc.details or {}), "lease_released": lease_ref, "attempt_consumed": False,
                       "task_status": after}

    def _ready_after(self, run_id: str, task_id: str) -> list[dict[str, Any]]:
        """gx_task_submit's ``ready`` (contract r5): the plan computed after this submission's
        reaction, in gx_next_actions' rows -- overdue tasks first, then the tasks its acceptance
        released, then those released earlier, each in rank order, cut to the slots free now
        (``dispatch.plan(..., released_after=<the ACCEPTED transition's event>)``). Computed after
        every submit, fresh or replayed, and never stored with the answer. A submission that was
        not accepted released nothing; its ``ready`` is the plan in its ordinary order."""
        from ..scheduling import dispatch, snapshot   # the scheduler reads the store; no cycle at import

        accepted = self.conn.execute(
            "SELECT MAX(event_id) AS e FROM events WHERE kind='task.transition' AND subject_id=? AND new_state='ACCEPTED'",
            (task_id,)).fetchone()["e"]
        snap = snapshot.load(self.conn, run_id, now=clock.now_iso())
        plan = dispatch.plan(snap, released_after=None if accepted is None else int(accepted))
        rows: list[dict[str, Any]] = []
        used = 0
        for item in plan.as_output(contract="r5")["next_actions"][:int(contracts.response_limits()["max_rows"])]:
            size = _encoded(item) + 1
            if used + size > ACTIONS_BUDGET:
                break
            rows.append(item)
            used += size
        return rows

    def _verified_artifacts(self, conn: sqlite3.Connection, run_id: str, refs: Sequence[str]) -> list[dict[str, Any]]:
        """Each reference resolves to a published artifact of this run whose bytes re-hash."""
        described = []
        for ref in refs:
            item = self.artifacts.describe(ref)
            if item["run_id"] != run_id:
                raise ArtifactNotFound(
                    f"artifact {ref} is not an artifact of run {run_id}",
                    details={"artifact_ref": ref, "run_id": run_id},
                )
            check = self.artifacts.verify(ref)
            if not check["ok"]:
                if check["reason"] == "file_missing":
                    raise ArtifactNotFound(
                        f"artifact {ref} is registered but its file is gone",
                        details={"artifact_ref": ref, "reason": "file_missing"},
                    )
                raise ArtifactHashMismatch(
                    f"artifact {ref} no longer hashes to the sha256 it was published with",
                    details={"artifact_ref": ref, "recorded_sha256": check["recorded_sha256"],
                             "observed_sha256": check["observed_sha256"]},
                )
            described.append(item)
        return described

    @staticmethod
    def _bundle_ref(row: sqlite3.Row, described: Sequence[Mapping[str, Any]]) -> str | None:
        """The one artifact among those handed in that is the task's bundle."""
        kinds = [item["kind"] for item in described]
        missing = [kind for kind in (store.loads(row, "required_artifacts_json", []) or [])
                   if kind not in kinds]
        if missing:
            raise SchemaInvalid(
                "the task requires artifacts of kind " + ", ".join(missing) + ", and none was "
                "handed in. The name of a kind is not an artifact: publish the content with "
                "gx_artifact_put and name its reference",
                details={"missing_kinds": missing, "handed_in_kinds": kinds},
            )
        bundle_kind = row["bundle_kind"]
        if not bundle_kind:
            if not (store.loads(row, "required_artifacts_json", []) or []):
                # Nothing to judge it by: accepting whatever arrives would be an empty
                # "for all" passing, which is how v1 accepted work nobody had looked at.
                raise SchemaInvalid(
                    f"task {row['task_id']} names neither a bundle nor a required artifact, so "
                    "there is nothing a submission could be judged by",
                    details={"task_id": row["task_id"], "reason": "no_acceptance_rule"},
                )
            return None
        found = [item["artifact_ref"] for item in described if item["kind"] == bundle_kind]
        if len(found) != 1:
            raise SchemaInvalid(
                f"task {row['task_id']} is answered by one {bundle_kind}; {len(found)} artifact(s) "
                f"of that kind were handed in. Publish the bundle with gx_artifact_put "
                f"kind={bundle_kind} and name it in artifact_refs",
                details={"bundle_kind": bundle_kind, "handed_in_kinds": kinds, "found": found},
            )
        return found[0]

    def _require_independent_worker(self, conn: sqlite3.Connection, row: sqlite3.Row,
                                    lease: sqlite3.Row) -> None:
        """A review is not handed in by the worker the host observed doing the authoring."""
        author_task = row["independent_of_task_id"]
        if not author_task:
            return

        def workers(task_id: str, lease_id: str | None) -> set[str]:
            sql = ("SELECT agent_ref, observed_worker_ref FROM worker_observations WHERE task_id=?"
                   + (" AND lease_id=?" if lease_id else ""))
            found: set[str] = set()
            for obs in conn.execute(sql, (task_id, lease_id) if lease_id else (task_id,)):
                found.update(ref for ref in (obs["agent_ref"], obs["observed_worker_ref"]) if ref)
            return found

        shared = workers(row["task_id"], lease["lease_id"]) & workers(author_task, None)
        if shared:
            raise IndependenceRequired(
                f"task {row['task_id']} reviews task {author_task} and the same worker "
                f"({', '.join(sorted(shared))}) is on record for both",
                details={"task_id": row["task_id"], "independent_of": author_task,
                         "shared_workers": sorted(shared)},
            )

    @staticmethod
    def _product_workflow(conn: sqlite3.Connection, run: sqlite3.Row):
        from ..orchestration.service import default_registry
        name, version = run["workflow_id"], run["workflow_version"]
        if not name or not version:
            raise SchemaInvalid("a workflow product needs a versioned workflow")
        workflow = default_registry(conn, workflow_id=name, version=version).get(name, version)
        if workflow.digest() != run["workflow_sha256"] or workflow.task_product_validator is None:
            raise ContentHashMismatch("the run's pinned workflow product validator is unavailable")
        return workflow

    def _judge(self, conn: sqlite3.Connection, row: sqlite3.Row, lease_ref: str | None,
               bundle_ref: str | None, *, artifact_refs: Sequence[str] = ()) -> BundleVerdict:
        prepared = self._prepare_judgment(conn, row, lease_ref, bundle_ref, artifact_refs=artifact_refs)
        return self._apply_judgment(conn, row, prepared)

    def _prepare_judgment(self, conn, row, lease_ref, bundle_ref, *, artifact_refs):
        """Read the pinned originals and run any read-only workflow product validator."""
        bundle = self.artifacts.read_json(bundle_ref) if bundle_ref is not None else None
        task = task_mapping(conn, self._task_row(conn, row["task_id"]))
        # Internal verified submission context, never a caller-supplied task field.
        # Sidecar consumers can judge the exact product before task acceptance.
        task["submission_artifact_refs"] = list(artifact_refs)
        if lease_ref is not None:
            task["lease_id"] = lease_ref
        check_product = task["acceptance"].get("validate_workflow_product") is True
        if check_product:
            workflow = self._product_workflow(conn, self._run_row(conn, row["run_id"]))
            try:
                workflow.task_product_validator(conn, task, bundle)
            except SchemaInvalid as error:
                return task, bundle, BundleVerdict(row["bundle_kind"] or "", [RecordVerdict(index=-1, ok=False,
                    code=error.code, message=error.message)], accepted=False, summary="workflow product refused")
        if bundle_ref is None:
            summary = ("the workflow accepted the required artifacts" if check_product else
                       "every required artifact was handed in and re-hashed")
            return task, bundle, BundleVerdict(bundle_kind="", records=[], accepted=True, summary=summary)
        return task, bundle, None

    def _apply_judgment(self, conn, row, prepared):
        task, bundle, product_verdict = prepared
        if product_verdict is not None:
            return product_verdict
        # Registry validators commit typed records through their existing GX owner.
        verdict = framework.judge(self.ctx.child(conn=conn), task, row["bundle_kind"], bundle, commit=True)
        gap = next((r for r in verdict.records if r.index < 0 and r.code == "IMPLEMENTATION_GAP"), None)
        if gap is not None:
            # Not a judgement of the work: nothing is consumed, and the same submission
            # is accepted for judging once the validator exists.
            raise ImplementationGap(
                gap.message, details={"bundle_kind": row["bundle_kind"], "task_id": row["task_id"],
                                      "completion_status": "DESIGN_ONLY"},
            )
        return verdict

    def _publish_verdict(self, conn: sqlite3.Connection, row: sqlite3.Row, submission_id: str,
                         verdict: BundleVerdict) -> str:
        body = json.dumps(framework.full_view(verdict), ensure_ascii=False, sort_keys=True).encode("utf-8")
        published = ArtifactService(conn).publish_bytes(
            namespace=f"{row['run_id']}/verdicts", rel_path=f"{row['task_id']}/{submission_id}.json",
            kind=VERDICT_KIND, data=body, run_id=row["run_id"], task_id=row["task_id"],
            media_type="application/json",
            provenance={"submission_id": submission_id, "bundle_kind": verdict.bundle_kind},
            actor=self._writer, origin="BACKEND",
        )
        return published.artifact_ref

    def _conclude(self, conn: sqlite3.Connection, row: sqlite3.Row, lease_ref: str | None,
                  base: Mapping[str, Any], verdict: BundleVerdict, policy: Mapping[str, Any],
                  *, existing: bool = False) -> dict[str, Any]:
        """Record a judged submission and move its task to ACCEPTED or REJECTED."""
        task_id, run_id = row["task_id"], row["run_id"]
        result = "ACCEPTED" if verdict.accepted else "REJECTED"
        records_ref = self._publish_verdict(conn, row, base["submission_id"], verdict)
        view, complete = framework.bounded_view(verdict, max_bytes=VERDICT_BUDGET)
        # What the validator created can be as long as the bundle: the response carries the
        # count of each kind and the first few ids, the verdict artifact carries them all.
        validation = {
            **view, "complete": complete, "records_ref": records_ref,
            "created": {kind: list(made)[:CREATED_SHOWN] for kind, made in verdict.created.items()},
            "created_counts": {kind: len(made) for kind, made in verdict.created.items()},
            "policy_verdict": policy.get("policy_verdict"),
        }
        stored = {**validation, "records": [
            {key: item.get(key) for key in ("index", "ok", "code", "message")}
            for item in view["records"] if not item["ok"]
        ][:200]}
        if existing:
            conn.execute(
                "UPDATE submissions SET accepted_count=?, rejected_count=?, result=?,"
                " validation_json=?, records_artifact_ref=? WHERE submission_id=?",
                (verdict.accepted_count, verdict.rejected_count, result, store._j(stored),
                 records_ref, base["submission_id"]),
            )
        else:
            self._insert_submission(conn, base, result=result, verdict=verdict,
                                    validation=stored, records_ref=records_ref)
        moved = store.transition(
            conn, machine=TASK, subject_id=task_id, to_state=result, actor=self._writer, run_id=run_id,
            reason=(verdict.summary or f"{verdict.accepted_count} record(s) accepted, "
                    f"{verdict.rejected_count} refused")[:500],
            evidence_level="EXECUTION_OBSERVED",
            extra_updates={"owner": None, "lease_id": None, "hold_json": None},
            payload=recovery.derived({
                "submission_id": base["submission_id"], "accepted_count": verdict.accepted_count,
                "rejected_count": verdict.rejected_count, "records_ref": records_ref}),
            bump=False,
        )
        if lease_ref is not None:
            store.release_lease(conn, lease_ref)
        if result == "ACCEPTED":
            # The trigger of whatever follows from this acceptance (design r2.2 §2.4.1): one
            # outbox row, in the transaction that accepts, at a submit and at the release of
            # a hold alike. Identifiers only; the reaction reads the submission itself.
            submission = conn.execute("SELECT lease_id FROM submissions WHERE submission_id=?",
                                      (base["submission_id"],)).fetchone()
            store.enqueue_outbox(
                conn, topic=TRIGGER_ACCEPTED, idempotency_key=f"{TRIGGER_ACCEPTED}:{base['submission_id']}",
                payload={"run_id": run_id, "subject": base["submission_id"], "event_id": moved.event_id,
                         "task_id": task_id, "path_id": row["path_id"], "task_kind": row["kind"],
                         "bundle_kind": row["bundle_kind"],
                         "lease_id": submission["lease_id"] if submission is not None else lease_ref},
            )
            recovery.promote_pending(conn, run_id=run_id, enter_ready=self._enter_ready,
                                     actor=self._writer)
        return {
            "task_status": result, "submission_id": base["submission_id"],
            "validation": validation, "revision": store.current_revision(conn, run_id),
            "run_id": run_id, "records_ref": records_ref,
        }

    @staticmethod
    def _insert_submission(conn: sqlite3.Connection, base: Mapping[str, Any], *, result: str,
                           verdict: BundleVerdict | None, validation: Mapping[str, Any],
                           records_ref: str | None) -> None:
        conn.execute(
            "INSERT INTO submissions(submission_id, task_id, lease_id, attempt, brief_sha256,"
            " content_sha256, worker_observation_id, policy_verdict, bundle_kind, artifact_refs_json,"
            " accepted_count, rejected_count, result, validation_json, records_artifact_ref, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (base["submission_id"], base["task_id"], base["lease_id"], base["attempt"],
             base["brief_sha256"], base["content_sha256"], base["worker_observation_id"],
             base["policy_verdict"], base["bundle_kind"], store._j(list(base["artifact_refs"])),
             verdict.accepted_count if verdict else 0, verdict.rejected_count if verdict else 0,
             result, store._j(dict(validation)), records_ref, clock.now_iso()),
        )

    # -------------------------------------------------------------------------- held
    def _hold(self, conn: sqlite3.Connection, row: sqlite3.Row, lease: sqlite3.Row,
              base: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
        """Keep a submission unjudged until the user decides about the worker that made it."""
        task_id, run_id = row["task_id"], row["run_id"]
        subject = Subject(kind="submission", id=base["submission_id"], sha256=base["content_sha256"])
        approval = seams.request_approval(
            self.ctx.child(conn=conn), action_kind="model_switch", subject=subject, run_id=run_id,
            request={
                "task_id": task_id, "lease_id": lease["lease_id"], "attempt": int(row["attempt"]),
                "policy_verdict": policy.get("policy_verdict"), "reasons": policy.get("reasons"),
                "required": policy.get("required"), "reported": policy.get("reported"),
                "deviation": policy.get("deviation"), "brief_sha256": row["brief_sha256"],
                "worker_observation_id": policy.get("worker_observation_id"),
            },
        )
        run = self._run_row(conn, run_id)
        before = self._run_before_task_hold(conn, run)
        if machines.can_transition(RUN, run["status"], "WAITING_APPROVAL"):
            store.transition(
                conn, machine=RUN, subject_id=run_id, to_state="WAITING_APPROVAL", actor=self._writer,
                reason=f"submission {base['submission_id']} of task {task_id} is held: the "
                       f"worker is {policy.get('policy_verdict')} against the agent policy",
                evidence_level="EXECUTION_OBSERVED",
                payload=recovery.derived({
                    "approval_id": approval["approval_id"], "task_id": task_id,
                    "submission_id": base["submission_id"], "policy_verdict": policy.get("policy_verdict")}),
            )
        hold = {
            "reason": recovery.HOLD_MODEL_POLICY, "approval_id": approval["approval_id"],
            "submission_id": base["submission_id"], "content_sha256": base["content_sha256"],
            "policy_verdict": policy.get("policy_verdict"), "run_status_before": before,
        }
        validation = {
            "held": True, "policy_verdict": policy.get("policy_verdict"),
            "approval_id": approval["approval_id"], "approval_status": approval.get("status"),
            "reasons": list(policy.get("reasons") or []), "deviation": policy.get("deviation"),
            "required": policy.get("required"), "reported": policy.get("reported"),
            "records": [], "summary": "held unjudged: the worker is outside the agent policy "
                                      "or could not be shown where the policy requires it",
        }
        self._insert_submission(conn, base, result="HELD", verdict=None, validation=validation,
                                records_ref=None)
        store.transition(
            conn, machine=TASK, subject_id=task_id, to_state="BLOCKED", actor=self._writer, run_id=run_id,
            reason=f"held for approval {approval['approval_id']}: worker {policy.get('policy_verdict')}",
            evidence_level="EXECUTION_OBSERVED",
            extra_updates={"owner": None, "lease_id": None, "hold_json": hold},
            payload=recovery.derived({"hold": hold}), bump=False,
        )
        store.release_lease(conn, lease["lease_id"])
        return {
            "task_status": "BLOCKED", "submission_id": base["submission_id"],
            "validation": validation, "revision": store.current_revision(conn, run_id),
            "run_id": run_id, "records_ref": None,
        }

    def _release_held(self, conn: sqlite3.Connection, row: sqlite3.Row, hold: dict[str, Any]) -> str:
        """Judge a held submission now that its approval is GRANTED. Returns the task status.

        Never raises into the caller (a store error aside): this runs while a dispatch plan
        or a claim is being read, and one submission that cannot be released must not make
        every other task of the run unreadable. In order:

        1. When no other held submission of the run waits for a person, the run leaves
           WAITING_APPROVAL first (:meth:`_after_holds`), so the bundle is judged with the
           run where it was when the bundle was handed in -- a validator that needs the run
           plannable is not refused for a wait that is over.
        2. The bundle is judged with ``commit=True`` inside a savepoint.
        3. A release that cannot be carried out -- the grant is for other bytes, an
           artifact is gone, no validator, a validator that refuses for the run's phase or
           for budget, a validator that crashes -- is settled once, as a refused attempt:
           the held submission is recorded REJECTED with what stopped it, and the task goes
           back to READY as its next attempt, or BLOCKED with every refusal listed. It is
           not tried again on the next read.
        """
        task_id, run_id = row["task_id"], row["run_id"]
        submission = conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (hold.get("submission_id"),)
        ).fetchone()
        approval = conn.execute(
            "SELECT subject_sha256 FROM approvals WHERE approval_id=?", (hold.get("approval_id"),)
        ).fetchone()
        if submission is None or approval is None:
            return self._release_failed(conn, row, hold, submission, "ARTIFACT_NOT_FOUND",
                                        "the held submission or its approval is no longer on record")
        if approval["subject_sha256"] != submission["content_sha256"]:
            return self._release_failed(conn, row, hold, submission, "CONTENT_HASH_MISMATCH",
                                        "the approval is granted for another sha256 than this submission's")
        others = [item for item in recovery.pending_holds(conn, run_id) if item["task_id"] != task_id]
        if not others:
            self._after_holds(conn, run_id, [hold.get("run_status_before")])
        refs = store.loads(submission, "artifact_refs_json", []) or []
        try:
            with store.transaction(conn):
                described = self._verified_artifacts(conn, run_id, refs)
                bundle_ref = self._bundle_ref(row, described)
                store.transition(
                    conn, machine=TASK, subject_id=task_id, to_state="VALIDATING", actor=self._writer,
                    run_id=run_id, reason=f"approval {hold.get('approval_id')} is GRANTED for this submission",
                    evidence_level="EXECUTION_OBSERVED", extra_updates={"hold_json": None},
                    payload=recovery.derived({"approval_id": hold.get("approval_id")}), bump=False,
                )
                verdict = self._judge(conn, row, submission["lease_id"], bundle_ref, artifact_refs=refs)
                response = self._conclude(
                    conn, row, None, {"submission_id": submission["submission_id"]}, verdict,
                    {"policy_verdict": submission["policy_verdict"]}, existing=True,
                )
        except sqlite3.Error:
            raise
        except GxError as exc:
            return self._release_failed(conn, row, hold, submission, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - a crashing validator is a recorded outcome, not every reader's error
            return self._release_failed(conn, row, hold, submission, "IMPLEMENTATION_GAP",
                                        f"the release raised {type(exc).__name__}: {exc}")
        return response["task_status"]

    def _release_failed(self, conn: sqlite3.Connection, row: sqlite3.Row, hold: Mapping[str, Any],
                        submission: sqlite3.Row | None, code: str, message: str) -> str:
        """Settle a granted hold that could not be released: a refused attempt, recorded once."""
        task_id, run_id = row["task_id"], row["run_id"]
        why = f"approval {hold.get('approval_id')} was granted and the held submission could not be judged: " \
              f"{code}: {message}"[:600]
        if submission is not None:
            validation = store.loads(submission, "validation_json", {}) or {}
            validation.update(
                released=False, release_error={"code": code, "message": message[:400]},
                records=[{"index": -1, "ok": False, "code": code, "message": message[:400]}],
                summary=why,
            )
            conn.execute(
                "UPDATE submissions SET result='REJECTED', rejected_count=1, validation_json=? WHERE submission_id=?",
                (store._j(validation), submission["submission_id"]),
            )
        store.append_event(
            conn, kind="task.release_failed", actor=self._writer, run_id=run_id, subject_kind=TASK,
            subject_id=task_id, reason=why, evidence_level="EXECUTION_OBSERVED",
            payload=recovery.derived({"approval_id": hold.get("approval_id"),
                                      "submission_id": hold.get("submission_id"), "code": code}),
        )
        return recovery.requeue_or_exhaust(conn, task_id, reason=why, enter_ready=self._enter_ready,
                                           actor=self._writer, end_chain=self._end_chain)

    # ------------------------------------------------------------------ bundle_check
    def bundle_check(self, *, artifact_ref: str, bundle_kind: str, task_id: str | None = None,
                     lease_ref: str | None = None) -> dict[str, Any]:
        """Judge a bundle exactly as ``submit`` would, and change nothing.

        The validator runs inside a savepoint that is always rolled back, so "changes
        nothing" is the service's guarantee and not each validator's promise. A validator
        that wrote during a dry run is named in a warning; its writes are gone.
        """
        if bundle_kind not in contracts.bundle_kinds():
            raise SchemaInvalid(f"{bundle_kind!r} is not a bundle kind",
                                details={"bundle_kind": bundle_kind, "known": contracts.bundle_kinds()})
        try:
            described = self.artifacts.describe(artifact_ref)
            bundle = self.artifacts.read_json(artifact_ref)
        except ArtifactHashMismatch as exc:
            raise ArtifactNotFound(
                f"artifact {artifact_ref} cannot be read: its bytes no longer hash to the "
                "sha256 it was published with",
                details={"artifact_ref": artifact_ref, "reason": "hash_mismatch", **(exc.details or {})},
            ) from exc
        task: dict[str, Any] = {}
        if task_id is not None:
            row = self._task_row(self.conn, task_id, missing=ScopeDenied)
            if described["run_id"] != row["run_id"]:
                raise ScopeDenied(
                    f"artifact {artifact_ref} and task {task_id} belong to different runs",
                    details={"artifact_ref": artifact_ref, "task_id": task_id},
                )
            task = task_mapping(self.conn, row)
            if lease_ref is not None:
                lease = self.conn.execute("SELECT task_id FROM leases WHERE lease_id=?", (lease_ref,)).fetchone()
                if lease is None or lease["task_id"] != task_id:
                    raise SchemaInvalid(f"{lease_ref!r} is not a lease of task {task_id}",
                                        details={"lease_ref": lease_ref, "task_id": task_id})
                task["lease_id"] = lease_ref
        elif lease_ref is not None:
            raise SchemaInvalid("lease_ref is given together with task_id", details={"lease_ref": lease_ref})

        warnings: list[str] = []
        verdict, changed = changes_made_by(
            self.conn, lambda: framework.judge(self.ctx, task, bundle_kind, bundle, commit=False))
        if changed:
            warnings.append(
                f"the validator of {bundle_kind} ({registry_owner(bundle_kind)}) wrote {changed} "
                "row change(s) during a check; they were rolled back"
            )

        view, complete = framework.bounded_view(verdict, max_bytes=VERDICT_BUDGET)
        records_ref = None
        if not complete:
            body = json.dumps(framework.full_view(verdict), ensure_ascii=False, sort_keys=True).encode("utf-8")
            published = self.artifacts.publish_bytes(
                namespace=f"{described['run_id'] or 'shared'}/checks",
                rel_path=f"{artifact_ref}/{ids.sha256_of_obj(framework.full_view(verdict))[:24]}.json",
                kind=CHECK_KIND, data=body, run_id=described["run_id"], task_id=task_id,
                media_type="application/json", provenance={"checked": artifact_ref, "bundle_kind": bundle_kind},
                actor=self._writer, origin="BACKEND",
            )
            records_ref = published.artifact_ref
        return {
            "bundle_kind": bundle_kind, "would_accept": verdict.accepted, "records": view["records"],
            "records_ref": records_ref, "accepted_count": verdict.accepted_count,
            "rejected_count": verdict.rejected_count, "records_total": view["records_total"],
            "summary": verdict.summary, "run_id": described["run_id"], "warnings": warnings,
        }

    # ------------------------------------------------------------------------ cancel
    def cancel_open(self, conn: sqlite3.Connection, *, run_id: str, reason: str, actor: str) -> list[str]:
        """Cancel every task of a run that has not ended, releasing its lease.

        A worker that was still working finds its lease released when it submits, which
        is the answer it needs: the run it was working for has been cancelled.
        """
        cancelled = []
        for row in conn.execute(
            "SELECT task_id, status, lease_id FROM tasks WHERE run_id=? ORDER BY created_at, task_id",
            (run_id,),
        ).fetchall():
            if row["status"] in machines.terminal_states(TASK):
                continue
            if row["lease_id"]:
                store.release_lease(conn, row["lease_id"])
            store.transition(
                conn, machine=TASK, subject_id=row["task_id"], to_state="CANCELLED", actor=actor,
                run_id=run_id, reason=reason, evidence_level="EXECUTION_OBSERVED",
                extra_updates={"owner": None, "lease_id": None},
                payload={"status_at_cancel": row["status"]}, bump=False,
            )
            cancelled.append(row["task_id"])
        return cancelled


def registry_owner(bundle_kind: str) -> str:
    """Where the validator published for a kind is defined, for a message that names it."""
    from .. import registry

    validator = registry.validator_for(bundle_kind)
    if validator is None:
        return "no validator"
    return f"{getattr(validator, '__module__', '?')}.{getattr(validator, '__qualname__', '?')}"


__all__ = [
    "BINDABLE_EVIDENCE_LEVELS", "TeamService", "backend_ctx", "changes_made_by",
    "default_lease_seconds", "default_max_attempts", "dependency_ids", "task_mapping", "task_view",
]
