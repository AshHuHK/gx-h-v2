"""The job service: submit, observe, settle. The tool belongs to a detached runner.

What v1 got wrong, and what holds here instead:

* **The submitter owned the tool.** A killed submitter left a tool running past its
  limit and a job nobody could explain. Now ``submit`` writes the job's intent, starts a
  runner (:mod:`gx_core.jobs.runner`) and returns; every later question — status, wait,
  reconcile, from any process — is answered from the runner's own files and from
  ``/proc``, found again by pid and start time.
* **The caller chose its own limits.** Leaving ``resources.gpu`` out skipped the GPU
  gate. Now nothing about resources comes from the request except ``tighten``, which can
  only lower a figure the manifest sets. GPU need, scope, memory and wall time are read
  from the capability's verified manifest.
* **Readiness was a sentence.** Now the capability must be ACTIVE (the asset row, for the
  registered sha256) and its environment HEALTHY from a probe on this host.
* **Accounting used declared figures.** Now a reservation is settled with what the runner
  measured. A job whose tool never started is charged nothing, whoever refused it (the
  service at its start deadline or on a refused approval, its own runner at launch on a
  hold, a switch, the preflight, the lease's queue, or a cancel that came first): its hold
  is returned. How often a path may wait is bounded elsewhere, per path.
* **The first reconcile deleted the evidence.** A successful Job cleans staging after
  publication and commit. An unsuccessful execution retains its actual raw files for
  diagnosis under the original Job, separately from accepted scientific outputs.
* **A job's ending was known only to whoever asked.** A job row moves when somebody looks,
  and the worker that submitted it may be gone. Now every ending — settled, never started,
  cancelled — goes through ``_end``, which writes one ``job.terminal`` outbox row in the
  transaction that makes the job terminal (:func:`terminal_trigger`); and ``reconcile(run_id)``
  settles the jobs of a run that nobody is waiting for. A reaction is driven by the row.

The order of ``submit`` is the order in which a refusal is cheapest and most useful:
holds; the switches the capability's job runs under; the run and path; the capability's
state and its environment; the input manifest and roles; the limits; verified input staging
and the adapter's own input check; the budget; the GPU preflight; the approval; and only then the intent is persisted
and the runner started. A hold and a switch are the operator's decisions about the whole
machine, so they answer before anything is said about the run, the path or the
capability's review state, and reach a caller who has nothing selected or reviewed. The budget is asked before the
preflight and held in the transaction that writes the job row, so a refusal before the row
exists leaves nothing behind, and neither does a submitter that dies between the two.

A job waiting for a person's approval is a ``CREATED`` row that the same idempotency key
resumes, until its start deadline (``START_DEADLINE_SECONDS`` after admission): the next
look after it ends the job not started and returns its hold, as it does at once when the
approval is DENIED or REVOKED. A refusal raised after the row exists names the job and says
what is left of it (``side_effect_status`` PARTIAL): the job kept CREATED with its hold, or
ended with its hold returned.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from .. import config, contracts, metrics
from ..capabilities import environments, probe, registry, storage
from ..capabilities import manifest as manifest_mod
from ..context import SCOPE_WORKER, Ctx, Principal
from ..domain import clock, ids, machines
from ..errors import (
    ContentHashMismatch,
    ApprovalRequired,
    BudgetExceeded,
    LeaseExpired,
    ArtifactHashMismatch,
    ArtifactNotFound,
    CapabilityNotApproved,
    GxError,
    DependencyUnmet,
    IdempotencyConflict,
    PhaseDenied,
    ResourceDenied,
    SchemaInvalid,
    ScopeDenied,
)
from ..persistence import store
from ..p0 import manifest as execution_manifest
from ..registry import Subject
from . import adapter as adapter_mod
from . import documents, executor, leases, resource_guard, retained
from . import limits as limits_mod
from . import runner as runner_mod
from .limits import limit_problems  # published here too: planning reads it as service.limit_problems

MACHINE = "job"
TERMINAL = frozenset(machines.terminal_states(MACHINE))
CREATED = machines.initial_state(MACHINE)
SUBMITTING, SUBMITTED, QUEUED, RUNNING = "SUBMITTING", "SUBMITTED", "QUEUED", "RUNNING"
SUCCEEDED, FAILED, CANCELLED = "SUCCEEDED", "FAILED", "CANCELLED"
CANCEL_REQUESTED, OUTCOME_UNKNOWN = "CANCEL_REQUESTED", "OUTCOME_UNKNOWN"
for _state in (SUBMITTING, SUBMITTED, QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED,
               CANCEL_REQUESTED, OUTCOME_UNKNOWN):
    if _state not in machines.all_states(MACHINE):  # pragma: no cover - the contract moved
        raise RuntimeError(f"job state {_state!r} left the contract")

#: Jobs that hold, or may still take, a resource.
IN_FLIGHT = (SUBMITTING, SUBMITTED, QUEUED, RUNNING, CANCEL_REQUESTED, OUTCOME_UNKNOWN)

SCHEDULER_KIND = "detached_runner"

#: Why a job exists. ``path`` is the only purpose a tool call can ask for.
PURPOSE_PATH = "path"                    # a step of a SELECTED or EXECUTING path
PURPOSE_PROBE = "probe"                  # the probe ladder, for a reviewed candidate
PURPOSE_REFERENCE = "reference_build"    # a backend-owned workflow building reference data
PURPOSE_EVALUATION = "evaluation"        # a backend-owned evaluation step
PURPOSE_WORKFLOW = "workflow_step"       # a step of a backend-owned workflow that has no path
PURPOSES = (PURPOSE_PATH, PURPOSE_PROBE, PURPOSE_REFERENCE, PURPOSE_EVALUATION, PURPOSE_WORKFLOW)

MAX_WAIT_SECONDS = 60
MAX_POLL_SECONDS = 1.0
#: How long a job may sit in SUBMITTING with no runner on record before reconcile says
#: nothing ran. Inside it the launcher may still be at work: a submission in flight is not
#: a dead one.
SUBMIT_GRACE_SECONDS = executor.LAUNCH_TIMEOUT_SECONDS + 30
#: Seconds a GPU job may wait for other jobs to release the lease, while the limits have no
#: line for it. What is in force is :func:`gpu_queue_wait_seconds`: the limits line
#: ``gpu.queue_wait_seconds`` when there is one (SCHEDULING_DESIGN r2.2 §7.1).
GPU_QUEUE_TIMEOUT_SECONDS = limits_mod.DEFAULT_GPU_QUEUE_WAIT_SECONDS
#: Seconds an admitted job may stay CREATED (waiting for its approval, or left between its row
#: and its runner by a submitter that died) before the next look ends it not started and
#: returns its hold (audit J10). The same bound as the lease's queue: a job that has not
#: started within it does not start. What is in force is :func:`start_deadline_seconds`.
START_DEADLINE_SECONDS = GPU_QUEUE_TIMEOUT_SECONDS
#: Seconds after a job's own time limit within which its runner has written its outcome and a
#: look has settled it. Part of the deadline a task lease is held to while its job is open.
SETTLE_GRACE_SECONDS = 120
#: Approvals that say no to the job they are bound to. EXPIRED is not among them: an expired
#: approval may be asked for again.
_REFUSING_APPROVALS = ("DENIED", "REVOKED")
#: How many of this store's GPU jobs may be in flight at once: one on the device, the rest
#: queued behind it in the order they were submitted. A longer queue is a refusal, because
#: each queued job holds part of its run's budget and a place it may not get to use.
GPU_QUEUE_MAX = 4
#: A log larger than this is published as its head and tail, and says so.
MAX_LOG_BYTES = 2 * 1024 * 1024
#: Bounds of one directory output (manifest kind ``dir``).
DIR_OUTPUT_MAX_FILES = runner_mod.MAX_LISTED_FILES
DIR_OUTPUT_MAX_BYTES = 256 * 1024 * 1024

#: LOCAL vocabulary, not from contracts/: what the backend itself saw go wrong with a job,
#: in ``outcome_json.failure.observed``. A scheduler decides on this, not on anybody's
#: description of the failure. ``failure_class`` beside it is the contract's taxonomy.
OBSERVED_FAILURES = (
    "timed_out",            # the runner ended the tool at its time limit
    "nonzero_exit",         # the tool ended by itself with a non-zero exit code
    "killed_by_signal",     # the tool was ended by a signal the runner did not send
    "missing_output",       # exit 0, and a required output was not written
    "output_rejected",      # exit 0, and the adapter or the backend refused what was written
    "gpu_memory_exceeded",  # historical runner stopped on device-wide growth
    "tool_gpu_memory_exceeded",  # an allocator peak reported by the Job exceeded the limit
    "not_started",          # refused at launch: a hold, a switch, the preflight, the lease
    "spawn_failed",         # the program could not be started at all
    "runner_lost",          # the runner is gone and left no outcome
    "runner_terminated",    # the runner itself was signalled
    "runner_error",         # the runner failed
    "readonly_tree_changed",  # the tool ran and something under a read-only root it was given changed
)

_FAILURE_CLASSES = contracts.failure_classes()
for _name in ("input_or_format", "adapter_or_protocol", "environment", "resource",
              "job_timeout_or_lost"):
    if _name not in _FAILURE_CLASSES:  # pragma: no cover - the contract moved
        raise RuntimeError(f"failure class {_name!r} left the contract")


def _backend_ctx(conn: sqlite3.Connection, actor: str) -> Ctx:
    return Ctx(conn=conn, principal=Principal(subject=actor, scope=SCOPE_WORKER, source="backend"),
               tool="jobs")


def intent_subject(job_id: str, intent: Mapping[str, Any]) -> Subject:
    """What an approval of a job is bound to: this job, asking for exactly this."""
    return Subject(kind="job", id=job_id, sha256=str(intent["request_sha256"]))


def latest_admission_approval(conn: sqlite3.Connection, job_id: str, intent: Mapping[str, Any]) -> dict[str, Any] | None:
    """The job's latest GPU approval, as recorded (approvals are the governance module's)."""
    if not intent.get("request_sha256"):
        return None
    subject = intent_subject(job_id, intent)
    latest = conn.execute(
        "SELECT approval_id, status FROM approvals WHERE action_kind='gpu_job' AND subject_kind=?"
        " AND subject_id=? AND subject_sha256=? ORDER BY rowid DESC LIMIT 1",
        (subject.kind, subject.id, subject.sha256)).fetchone()
    return dict(latest) if latest is not None else None


#: The topic of the trigger a job writes when it ends (SCHEDULING_DESIGN r2.2 §2.4.1).
TERMINAL_TOPIC = "job.terminal"


def terminal_trigger_key(job_id: str) -> str:
    """The idempotency key of a job's ``job.terminal`` outbox row: one row per job, ever."""
    return f"{TERMINAL_TOPIC}:{job_id}"


def terminal_trigger(row: sqlite3.Row, status: str, event_id: int | None) -> dict[str, Any]:
    """What a job's ``job.terminal`` outbox row says: which job ended, in which run, as what,
    and the event that records it. Identifiers only: what failed is on the job row
    (``outcome_json.failure``), where a reaction reads it, and the trigger is not a second
    copy of it. ``run_id`` is in the payload until the outbox has a column for it (r5)."""
    intent = store.loads(row, "submit_intent_json", {}) or {}
    return {"run_id": row["run_id"], "subject": row["job_id"], "status": status, "event_id": event_id,
            "path_id": row["path_id"], "task_id": row["task_id"], "purpose": intent.get("purpose")}


def gpu_queue_wait_seconds(limits: Mapping[str, Any] | None = None) -> int:
    """Seconds a GPU job may wait for the device's lease, as the limits say now."""
    return limits_mod.gpu_queue_wait_seconds(limits)


def start_deadline_seconds(limits: Mapping[str, Any] | None = None) -> int:
    """Seconds an admitted job may stay without a runner before it is ended not started: the
    same bound as the lease's queue, read from the same limits line."""
    return limits_mod.gpu_queue_wait_seconds(limits)


def job_deadline_seconds(resources: Mapping[str, Any], *, limits: Mapping[str, Any] | None = None) -> int:
    """Seconds from the start of a job's runner by which the job has ended and been settled,
    at the latest: for a GPU job the longest wait for the device's lease, then the job's own
    time limit, then the settle grace (SCHEDULING_DESIGN r2.2 §7.2.1: "the job's own reviewed
    wall time plus the queue wait for a GPU job"). ``resources`` is the job's own
    (``manifest.effective_resources``), so the time limit is the one reviewed with its
    capability and not the machine's ceiling, and a CPU job is not given a queue it never
    stands in (the check's EXEC-3). What a scheduler adds for a step of an execute_path row
    (§7.2.6); the deadline of a job that exists is :func:`.leases.job_deadline`.
    """
    queue = gpu_queue_wait_seconds(limits) if resources.get("gpu") is not None else 0
    return queue + int(resources.get("wall_seconds") or 0) + SETTLE_GRACE_SECONDS


class _KeyTaken(Exception):
    """Another submit wrote a row under the same idempotency key first."""


class _StepTaken(Exception):
    """The step already has a job that is open or SUCCEEDED (contract r5, job key rule B)."""

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.job_id = job_id


class _DryRun(Exception):
    """Rolls back a transaction that only asked a question."""


class JobService:
    """Submit, observe and settle jobs against one store."""

    def __init__(self, conn: sqlite3.Connection, *, actor: str = "backend", ctx: Ctx | None = None) -> None:
        self.conn = conn
        self.actor = ctx.actor if ctx is not None else actor
        self.ctx = ctx if ctx is not None else _backend_ctx(conn, actor)
        self.artifacts = storage.artifact_service(conn)

    # ------------------------------------------------------------------ small readers

    def _row(self, job_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no job {job_id!r}", details={"job_id": job_id})
        return row

    def _paths(self, job_id: str) -> executor.JobPaths:
        # Admission creates these directories. Status must see when the backend directory is missing.
        return executor.paths(storage.store_dir(self.conn) / "jobs", job_id)

    def _staging(self, row: sqlite3.Row) -> Path | None:
        return storage.store_dir(self.conn) / row["staging_ref"] if row["staging_ref"] else None

    def open_retained_output(self, job_id: str, relative_name: str):
        """Open an unverified raw file for local inspection using its original Job."""
        row = self._row(job_id)
        project = self.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
        if not self.ctx.principal.may_access_project(project["project_ref"]):
            raise ScopeDenied("this job is outside the caller's project")
        outcome = store.loads(row, "outcome_json", {}) or {}
        kept = outcome.get("kept") or {}
        recorded = kept.get("recovery_manifest_ref") and kept.get("staging_ref") == row["staging_ref"]
        # Older unknown-outcome cancellations kept the admitted absolute path before receipts existed.
        previous = (row["status"] == CANCELLED and "outcome_unknown" in outcome and row["staging_ref"]
                    and kept.get("staging") == str(self._staging(row)))
        if row["status"] not in (FAILED, CANCELLED) or not (recorded or previous):
            raise ArtifactNotFound("this job has no retained raw output record", details={"job_id": job_id})
        return retained.open_file(storage.store_dir(self.conn), row, relative_name)

    def view(self, row: sqlite3.Row, *, replayed: bool = False, observation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        outcome = store.loads(row, "outcome_json", {}) or {}
        outputs = list(outcome.get("outputs") or [])
        view = {
            "job_id": row["job_id"], "run_id": row["run_id"], "path_id": row["path_id"],
            "task_id": row["task_id"],
            "capability_version": f"{row['capability_id']}@{row['capability_version']}",
            "status": row["status"], "terminal": row["status"] in TERMINAL,
            "stale": bool((observation or {}).get("stale")),
            "scheduler_ref": row["scheduler_ref"],
            "observed_at": row["observed_at"] or row["created_at"],
            "exit_code": row["exit_code"],
            "measured": store.loads(row, "measured_json", {}) or {},
            "artifact_refs": [item["artifact_ref"] for item in outputs],
            "outputs": outputs,
            "failure": outcome.get("failure"),
            "preflight": store.loads(row, "preflight_json", {}) or None,
            "resources": store.loads(row, "resources_json", {}) or {},
            "created_at": row["created_at"], "submitted_at": row["submitted_at"],
            "finished_at": row["finished_at"], "replayed": replayed,
        }
        recovery_ref = (outcome.get("kept") or {}).get("recovery_manifest_ref")
        if recovery_ref:
            view["recovery_manifest_ref"] = recovery_ref
            view["artifact_refs"].append(recovery_ref)
        if (observation or {}).get("reason"):
            view["observation"] = observation["reason"]
        if (observation or {}).get("deferred"):
            view["reconciliation_deferred"] = True
        return view

    # ------------------------------------------------------------------------ submit

    def submit(
        self,
        *,
        run_id: str,
        capability_version: str,
        input_manifest_ref: str,
        idempotency_key: str,
        path_id: str | None = None,
        task_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        tighten: Mapping[str, Any] | None = None,
        purpose: str = PURPOSE_PATH,
        lease_ref: str | None = None,
        step_index: int | None = None,
        binding: Mapping[str, Any] | None = None,
        reserved_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a job and return at once. See the module docstring for the order of checks.

        Contract r5 (C11): a job of a task names the lease it is submitted under (``lease_ref``),
        and a job of a path names the step of the path it runs (``step_index``). The step is
        part of the request; the lease is not, so the same request under a later lease of the
        same task is the same job (it is adopted, SCHEDULING_DESIGN r2.2 §7.2.4)."""
        if self.conn.in_transaction:
            raise RuntimeError(
                "JobService.submit was called inside an open transaction. A job starts a real "
                "process; if the transaction rolled back, the process would run with no row. "
                "Submit outside the transaction and wait with wait()."
            )
        if purpose not in PURPOSES:
            raise SchemaInvalid(f"purpose {purpose!r} is not one of {list(PURPOSES)}",
                                details={"purpose": purpose})
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise SchemaInvalid("idempotency_key must be a non-empty string", details={})
        config.check_holds("jobs")

        parameters = dict(parameters or {})
        tighten = dict(tighten or {})
        request = {
            "run_id": run_id, "path_id": path_id, "task_id": task_id,
            "capability_version": capability_version, "input_manifest_ref": input_manifest_ref,
            "parameters": parameters, "tighten": tighten, "purpose": purpose,
        }
        if step_index is not None:
            request["step_index"] = step_index
        if binding:
            request["binding"] = dict(binding)
        request_sha = ids.sha256_of_obj(request)
        key = f"job:{run_id}:{idempotency_key}"

        existing = self.conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        if existing is not None:
            return self._existing(existing, request_sha, idempotency_key, run_id)
        if purpose == PURPOSE_PATH and isinstance(step_index, int) and not isinstance(step_index, bool):
            # Contract r5, job key rule B: a step whose job is open or SUCCEEDED is that job, whatever
            # key a new call brings (rule A, the caller's key as the replay key, is the lookup above).
            current = self._step_job(self.conn, run_id=run_id, path_id=path_id, step_index=step_index,
                                     manifest_sha256=self._manifest_sha(input_manifest_ref))
            if current is not None and current["status"] not in (FAILED, CANCELLED):
                return self._step_answer(current)
        from ..orchestration.service import prepare_workflow_job
        prepare_workflow_job(self.conn, run_id, request)
        try:
            admitted = self._admit({**request, "step_index": step_index, "lease_ref": lease_ref}, request_sha, key, reserved_job_id=reserved_job_id)
        except _StepTaken as taken:
            return self._step_answer(self._row(taken.job_id))
        except _KeyTaken:
            # Another submit with this key wrote its row while this one was being admitted:
            # this one is that one's replay, or a conflict, exactly as if it had come second.
            existing = self.conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            return self._existing(existing, request_sha, idempotency_key, run_id)
        return self._start(admitted)

    def _existing(self, existing: sqlite3.Row, request_sha: str, idempotency_key: str,
                  run_id: str) -> dict[str, Any]:
        """A submit whose key already names a job: that job, or IDEMPOTENCY_CONFLICT."""
        intent = store.loads(existing, "submit_intent_json", {}) or {}
        if intent.get("request_sha256") != request_sha:
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key!r} was used in run {run_id} for a different "
                "job request",
                details={"job_id": existing["job_id"], "idempotency_key": idempotency_key},
            )
        if existing["status"] == CREATED:
            self._reconcile_one(existing)               # it may be past its start deadline
            existing = self._row(existing["job_id"])
            if existing["status"] == CREATED:
                return self._start(existing)            # a job that was waiting for its approval
        self._reconcile_one(existing)
        return self._submit_answer(self._row(existing["job_id"]), replayed=True)

    def _submit_answer(self, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        return {
            "job_id": row["job_id"], "status": row["status"], "scheduler_ref": row["scheduler_ref"],
            "preflight": store.loads(row, "preflight_json", {}) or None, "replayed": replayed,
        }

    # -- admission: every check that needs no job row, then the row ---------------------

    def _admission_prerequisites(self, request: Mapping[str, Any], *, reprobe_when_stale: bool = True):
        """The original admission prefix, shared with input preparation."""
        conn = self.conn
        run_id, path_id, task_id = request["run_id"], request["path_id"], request["task_id"]
        purpose = request["purpose"]

        # The operator's switches answer first of all: before the run and the path are looked
        # at, and before anything is said about this version's review state, for a path's job
        # and for the ladder's own probe alike (lead's ruling, 2026-11-12T20:57Z). A switch is
        # a decision about the whole machine: a server of the earlier platform is launched
        # only under its switch and as its conditions say (one subprocess for one call, the
        # shipped client), and the GPU is used only under gpu_jobs and its scope, whatever
        # state the capability, the run or the path is in. Refused here there is no row and no
        # process, and the refusal reaches a caller who has no selected path and could take
        # nothing through a review. Which switches apply is read from the registered manifest
        # (its kind and its declared GPU need); a version nobody registered has none to ask.
        capability = registry.candidate(conn, request["capability_version"])
        manifest = capability["manifest"]
        switched_off = limits_mod.switch_findings(manifest_mod.effective_resources(manifest, None),
                                                  kind=manifest["kind"])
        if switched_off:
            raise switched_off[0]

        run = conn.execute("SELECT run_id, status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ArtifactNotFound(f"no run {run_id!r}", details={"run_id": run_id})
        execution_manifest.check_capability(conn, run_id, request["capability_version"], review_probe=purpose == PURPOSE_PROBE)
        executable = contracts.states()["machines"]["run"]["executable"]
        if purpose != PURPOSE_PROBE and run["status"] not in executable:
            raise PhaseDenied(
                f"run {run_id} is {run['status']}; a job is accepted only while the run is in "
                f"one of {executable}",
                details={"run_id": run_id, "status": run["status"], "executable": executable},
            )
        if purpose == PURPOSE_PATH:
            if not path_id:
                raise SchemaInvalid("a job of a path names its path_id", details={})
            path = conn.execute("SELECT run_id, status FROM paths WHERE path_id=?", (path_id,)).fetchone()
            if path is None or path["run_id"] != run_id:
                raise ArtifactNotFound(f"no path {path_id!r} in run {run_id!r}",
                                       details={"path_id": path_id, "run_id": run_id})
            if path["status"] not in ("SELECTED", "EXECUTING"):
                raise PhaseDenied(
                    f"path {path_id} is {path['status']}; jobs run for a SELECTED or EXECUTING path",
                    details={"path_id": path_id, "status": path["status"]},
                )
        elif path_id is not None:
            raise SchemaInvalid(f"a {purpose} job belongs to no path", details={"path_id": path_id})
        if task_id is not None:
            task = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["run_id"] != run_id:
                raise ArtifactNotFound(f"no task {task_id!r} in run {run_id!r}",
                                       details={"task_id": task_id, "run_id": run_id})
        lease_ref, step_index = self._lease_and_step(conn, request, purpose)

        if purpose == PURPOSE_PROBE:
            from ..capabilities import ladder  # the ladder is the only caller with this purpose

            ladder.require_reviewed(conn, capability)
        else:
            capability = registry.active_view(conn, capability, run_id=run_id)
        if purpose == PURPOSE_PATH:
            self._bind_to_plan(conn, request, capability, step_index)
        env = environments.require_ready(conn, capability["env_id"], actor=self.actor,
                                         reprobe_when_stale=reprobe_when_stale)
        env_view = manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                        binaries=dict(env["binary_paths"]), prefix=env["prefix"])
        return capability, manifest, env, env_view, lease_ref, step_index

    def preparation_prerequisites(self, request: Mapping[str, Any]) -> None:
        """Read-only/rolled-back owner checks before derived input publication.

        No environment refresh, input adapter, GPU probe or runner is started.
        The actual submission retains every original admission check afterward.
        """
        config.check_holds("jobs")
        if request["purpose"] == PURPOSE_PATH and type(request.get("step_index")) is int:
            self._bind_to_plan(self.conn, request, registry.candidate(self.conn, request["capability_version"]), request["step_index"])
            current = self._step_job(self.conn, run_id=request["run_id"], path_id=request["path_id"],
                                     step_index=request["step_index"], manifest_sha256=self._manifest_sha(request["input_manifest_ref"]))
            if current is not None and current["status"] not in (FAILED, CANCELLED):
                return  # Original step replay already owns its reservation.
        capability, manifest, _, _, _, _ = self._admission_prerequisites(request, reprobe_when_stale=False)
        resources = manifest_mod.effective_resources(manifest, request.get("tighten") or {})
        self._check_limits(resources)
        amounts = self._budget_amounts(resources)
        budgets = _budgets()
        budget_ref = budgets.budget_for(self.conn, run_id=request["run_id"],
                                       path_id=request["path_id"] if request["purpose"] == PURPOSE_PATH else None)
        from ..p0.team_admission import team_funding
        budget_ref = team_funding(self.conn, request["run_id"], request.get("task_id"), budget_ref, admit=True)
        from ..p0 import v62_budget
        budget_ref = v62_budget.job_funding(self.conn, request, budget_ref)
        self._refuse_prohibited_lines(self.conn, budget_ref=budget_ref, amounts=amounts)
        self._budget_would_hold(budgets, run_id=request["run_id"], budget_ref=budget_ref,
                                job_id=ids.new_id("job"), amounts=amounts)
        self._threads_or_refuse(self.conn, resources)

    def _admit(self, request: Mapping[str, Any], request_sha: str, key: str, *, reserved_job_id=None) -> sqlite3.Row:
        conn = self.conn
        run_id, path_id, task_id = request["run_id"], request["path_id"], request["task_id"]
        purpose = request["purpose"]

        capability, manifest, env, env_view, lease_ref, step_index = self._admission_prerequisites(request)

        input_doc = self.artifacts.read_input_manifest(request["input_manifest_ref"])
        if input_doc.get("run_id") not in (None, run_id):
            raise SchemaInvalid(
                f"input manifest {request['input_manifest_ref']} was made for run "
                f"{input_doc.get('run_id')}, not {run_id}",
                details={"manifest_run_id": input_doc.get("run_id"), "run_id": run_id},
            )
        listed = input_doc.get("entries", [])
        listed_roles = [entry["role"] for entry in listed]
        needed = manifest_mod.staged_roles(manifest, listed)
        if sorted(listed_roles) != needed:
            raise SchemaInvalid(
                f"{capability['capability_version']} takes input role(s) {needed}; the input "
                f"manifest supplies {sorted(listed_roles)}",
                details={"needed": needed, "supplied": listed_roles,
                         "failure_class": "input_or_format"},
            )

        resources = manifest_mod.effective_resources(manifest, request["tighten"])
        self._check_limits(resources)

        # From here on the job has a directory; a refusal removes it again.
        if reserved_job_id is not None:
            from ..p0.compute import validate_proposal
            validate_proposal(conn, reserved_job_id, request)
        job_id = reserved_job_id or ids.new_id("job")
        job = self._paths(job_id)
        job.create()
        out_dir = self.artifacts.staging_dir(f"jobs/{job_id}")
        try:
            staged_inputs: dict[str, str] = {}
            entries = {entry["role"]: entry for entry in listed}
            for role in needed:
                entry = entries[role]
                try:
                    data = self.artifacts.read_bytes(
                        str(entry.get("artifact_ref")), expected_sha256=entry.get("sha256") or ""
                    )
                except ArtifactHashMismatch as exc:
                    raise ArtifactHashMismatch(
                        exc.message, details={**exc.details, "role": role, "manifest_ref": request["input_manifest_ref"]}
                    ) from exc
                suffix = Path(entry.get("rel_path") or "").suffix[:16]
                target = job.in_dir / f"{role}{suffix}"
                target.write_bytes(data)
                staged_inputs[role] = str(target)

            plan = manifest_mod.launch_plan(
                capability["bundle"], env_view, inputs=staged_inputs, out_dir=str(out_dir),
                parameters=request["parameters"], home_dir=str(job.home_dir),
                tmp_dir=str(job.tmp_dir), cpu_threads=resources["cpu_threads"],
                gpu_device=None, gpu_memory_mib=None, resource_limits=resources,
            )
            parser = capability["bundle"].get("parser")
            if parser is not None:
                answer = adapter_mod.call(parser, "validate_inputs",
                                          [staged_inputs, dict(request["parameters"])],
                                          work_dir=job.work_dir)
                if not answer.ok and not answer.missing_function:
                    raise SchemaInvalid(
                        f"the adapter of {capability['capability_version']} could not check the "
                        f"inputs: {answer.error_type}: {answer.error}",
                        details={"failure_class": "adapter_or_protocol", **answer.describe()},
                    )
                problems = list(answer.result or []) if answer.ok else []
                if problems:
                    raise SchemaInvalid(
                        f"the inputs are not acceptable to {capability['capability_version']}: "
                        f"{problems[0]}",
                        details={"failure_class": "input_or_format", "input_error": True,
                                 "problems": [str(p)[:300] for p in problems[:20]]},
                    )

            amounts = self._budget_amounts(resources)
            budgets = _budgets()
            budget_ref = budgets.budget_for(conn, run_id=run_id,
                                            path_id=path_id if purpose == PURPOSE_PATH else None)
            from ..p0.team_admission import team_funding
            budget_ref = team_funding(conn, run_id, task_id, budget_ref, admit=True)
            from ..p0 import v62_budget
            budget_ref = v62_budget.job_funding(conn, request, budget_ref)
            # The budget is asked here, before the preflight, because its refusal is the cheaper
            # one. It is held only in the transaction that writes the row: a submitter that dies
            # between the two leaves neither a hold nor a row (audit J9).
            self._refuse_prohibited_lines(conn, budget_ref=budget_ref, amounts=amounts)
            self._budget_would_hold(budgets, run_id=run_id, budget_ref=budget_ref, job_id=job_id, amounts=amounts)

            preflight: dict[str, Any] = {}
            gpu = resources["gpu"]
            if gpu is not None:
                preflight = self._gpu_preflight(gpu)
                device = preflight["selected_device"]["index"]
                plan = manifest_mod.launch_plan(
                    capability["bundle"], env_view, inputs=staged_inputs, out_dir=str(out_dir),
                    parameters=request["parameters"], home_dir=str(job.home_dir),
                    tmp_dir=str(job.tmp_dir), cpu_threads=resources["cpu_threads"],
                    gpu_device=str(device), gpu_memory_mib=gpu["memory_mib"],
                )

            intent = {
                "request_sha256": request_sha, "request": dict(request),
                "capability_version": capability["capability_version"],
                "manifest_sha256": capability["manifest_sha256"], "env_id": capability["env_id"],
                "parameters": dict(request["parameters"]), "tighten": dict(request["tighten"]),
                "input_manifest_ref": request["input_manifest_ref"],
                "input_manifest_sha256": self._manifest_sha(request["input_manifest_ref"]),
                "input_roles": {role: entries[role]["artifact_ref"] for role in needed},
                "input_sha256": {role: entries[role]["sha256"] for role in needed},
                "argv": plan.argv, "cwd": plan.cwd, "env_names": sorted(plan.env),
                "executables": {
                    "python": os.path.realpath(env_view.python) if env_view.python else None,
                    "binaries": {name: os.path.realpath(path) for name, path in env_view.binaries.items()},
                    "program": os.path.realpath(plan.argv[0]),
                },
                "weights": [{"path": weight["path"], "sha256": weight["sha256"], "bytes": weight["bytes"],
                             "load_mode": weight["load_mode"]} for weight in manifest["weights"]],
                "purpose": purpose, "staged_inputs": staged_inputs,
                # The lease this job is submitted under: the task's live lease when the
                # submitter is the worker it was issued to, read here and never taken from
                # the request (SCHEDULING_DESIGN r2.2 §7.2.3). Not part of the request's
                # sha256: the same request under a later lease is the same job.
                "lease_ref": lease_ref, "step_index": step_index,
                "platform": adapter_mod.platform_digest(),
                "runner_sha256": ids.sha256_of_file(executor.RUNNER_PATH),
            }
            network = manifest_mod.network_terms(manifest)
            intent["network"] = network
            spec = self._spec(job_id, job, out_dir, plan, resources, preflight)
            spec["network"] = network
            spec["gx_context"] = {
                "schema": "gx.job_context/1", "harness_root": str(contracts.HARNESS_ROOT),
                "run_id": run_id, "task_id": task_id,
                "capability_version": capability["capability_version"],
                "manifest_sha256": capability["manifest_sha256"],
                "input_manifest_ref": request["input_manifest_ref"],
                "input_manifest_sha256": intent["input_manifest_sha256"],
                "input_sha256": intent["input_sha256"], "request_sha256": request_sha,
                "purpose": purpose, "lease_ref": lease_ref, "step_index": step_index,
                "effective_resources": dict(resources),
            }
            try:
                policy = resource_guard.policy_for(
                    capability["capability_version"], resources,
                    gpu_min_free_mib=int((preflight.get("limits_applied") or {}).get("min_free_mib", 4096)))
            except resource_guard.ObservationRefused as exc:
                raise ResourceDenied("job resource policy refused",
                                     details={"reason": str(exc)}) from exc
            self._attach_resource_guard(spec, intent, policy)
            executor.write_spec(job, spec)
            staging_ref = str(Path(out_dir).relative_to(storage.store_dir(conn)))

            from . import capacity, cpu_queue
            prepared = execution_manifest.prepare_runtime(intent, capability["bundle"], env, resources, preflight)
            v6_node = (request.get("binding") or {}).get("v6_node_id")
            if v6_node:
                from ..p0 import v6_jobs, v6_method_context, v62_evidence
                gate_proof = v62_evidence.prepare_dispatch(conn, request)
                method_dependencies = v6_method_context.dependencies(
                    conn, run_id, request["capability_version"], env["python_executable"])
            sample = capacity.sample_admission(conn)
            with store.transaction(conn, transaction_owner="JobService", operation="admit", run_id=run_id) as txn:
                if reserved_job_id is not None:
                    validate_proposal(txn, reserved_job_id, request)
                if txn.execute("SELECT 1 FROM jobs WHERE idempotency_key=?", (key,)).fetchone() is not None:
                    raise _KeyTaken(key)                  # writers are serialised: this is the race's loser
                attempt = 1
                if purpose == PURPOSE_PATH:
                    # Rule B, held where writers are serialised: an open or SUCCEEDED job of the
                    # step is the job; otherwise this is the step's next attempt.
                    current = self._step_job(txn, run_id=run_id, path_id=path_id, step_index=step_index,
                                             manifest_sha256=intent["input_manifest_sha256"])
                    if current is not None:
                        if current["status"] not in (FAILED, CANCELLED):
                            raise _StepTaken(current["job_id"])
                        attempt = int(current["job_attempt"] or 1) + 1
                # Input checks and staging happen before this write lock. The lease may
                # have expired or changed hands meanwhile; a new row and its reservation
                # must still be admitted under the caller's live lease, atomically.
                self._lease_and_step(txn, request, purpose)
                budget_ref = team_funding(txn, run_id, task_id, budget_ref, admit=True)
                budget_ref = v62_budget.job_funding(txn, request, budget_ref)
                from ..p0.meter_limits import check_soft
                check_soft(txn,run_id,task_id)
                self._threads_or_refuse(txn, resources, key=key, sample=sample)
                reservation = budgets.reserve(txn, budget_ref=budget_ref, subject_kind="job",
                                              subject_id=job_id, amounts=amounts, actor=self.actor)
                txn.execute(
                    "INSERT INTO jobs(job_id, run_id, path_id, task_id, capability_id,"
                    " capability_version, env_id, status, idempotency_key, submit_intent_json,"
                    " scheduler_kind, input_manifest_ref, resources_json, preflight_json,"
                    " side_effect_status, timeout_seconds, staging_ref, reservation_ref, created_at,"
                    " lease_ref, step_index, job_attempt)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, run_id, path_id, task_id, capability["capability_id"],
                     capability["version"], capability["env_id"], CREATED, key,
                     ids.canonical_json(intent), SCHEDULER_KIND, request["input_manifest_ref"],
                     ids.canonical_json(resources), ids.canonical_json(preflight), "NONE",
                     int(resources["wall_seconds"]), staging_ref, reservation["reservation_ref"],
                     clock.now_iso(), lease_ref, step_index, attempt),
                )
                if v6_node:
                    v62_evidence.fence_dispatch(txn, request, gate_proof)
                    v6_jobs.fence_admission(txn, request, request_sha, key, job_id, amounts,
                                            prepared["runtime"], method_dependencies)
                elif txn.execute("SELECT 1 FROM sqlite_master WHERE name='v6_goals'").fetchone() and txn.execute(
                        "SELECT 1 FROM v6_goals WHERE run_id=?", (run_id,)).fetchone():
                    raise ScopeDenied("Goal-only Jobs require an accepted node assignment")
                cpu_queue.admitted(txn, key)
                execution_manifest.admit(txn, job_id, launch=intent, bundle=capability["bundle"],
                                         inputs=listed, resources=resources, prepared=prepared)
                store.append_event(
                    txn, kind="job.created", actor=self.actor, run_id=run_id, subject_kind="job",
                    subject_id=job_id, new_state=CREATED, evidence_level="EXECUTION_OBSERVED",
                    payload={"capability_version": capability["capability_version"],
                             "manifest_sha256": capability["manifest_sha256"], "purpose": purpose,
                             "path_id": path_id, "resources": resources,
                             "reservation_ref": reservation["reservation_ref"]},
                )
        except BaseException:
            # The hold was made in the row's own transaction, so nothing of it outlives a refusal.
            executor.remove_tree(job.job_dir)
            executor.remove_tree(Path(out_dir))
            raise
        return self._row(job_id)

    @staticmethod
    def _threads_or_refuse(conn: sqlite3.Connection, resources: Mapping[str, Any], *, key=None, sample=None) -> None:
        """Audit J8 (the lead's ruling, 2026-11-12T21:57Z): a job is admitted only while the
        threads it needs are free, by the count ``capacity.fits`` reads (``capacity.cpu_room``)
        and in its words (``limits.cpu_threads_finding``), so that what fits says and what
        submit does are one predicate. Called inside the transaction that writes the job's
        row: writers are serialised, so a submit that comes second sees the first one's row
        and its threads. Refused here nothing is written: no row, no hold."""
        from . import capacity                              # imported late: it imports this module

        room = capacity.admission_room(conn, sample) if sample is not None else capacity.cpu_room(conn)
        from . import cpu_queue
        refusal = cpu_queue.refusal(conn, room, resources["cpu_threads"], key)
        if refusal is not None:
            raise refusal

    @staticmethod
    def _bind_to_plan(conn: sqlite3.Connection, request: Mapping[str, Any], capability: Mapping[str, Any],
                      step_index: int) -> None:
        """Contract r5, the planned-step binding: a job of a path runs the step of the path's plan
        that ``step_index`` names, as planning stored it when the path was accepted
        (``paths.capability_refs_json``: capability version, manifest sha256, parameters). A
        step the plan does not have, another capability, a capability registered at another
        manifest than the plan was accepted with, or other parameters, is refused SCHEMA_INVALID
        and leaves nothing: a worker cannot run under a path something the path was not
        accepted to run. The input manifest belongs to this specific Job request."""
        path_id = request["path_id"]
        row = conn.execute("SELECT capability_refs_json FROM paths WHERE path_id=?", (path_id,)).fetchone()
        plan = (store.loads(row, "capability_refs_json", []) or []) if row is not None else []
        if step_index >= len(plan):
            raise SchemaInvalid(
                f"path {path_id} has {len(plan)} planned step(s); step_index {step_index} names none",
                details={"path_id": path_id, "step_index": step_index, "planned_steps": len(plan)})
        step = plan[step_index]
        wrong: dict[str, Any] = {}
        if step.get("capability_version") != request["capability_version"]:
            wrong["capability_version"] = {"planned": step.get("capability_version"),
                                           "requested": request["capability_version"]}
        elif step.get("manifest_sha256") and step["manifest_sha256"] != capability["manifest_sha256"]:
            wrong["manifest_sha256"] = {"planned": step["manifest_sha256"], "registered": capability["manifest_sha256"]}
        if dict(step.get("parameters") or {}) != dict(request["parameters"] or {}):
            wrong["parameters"] = {"planned": dict(step.get("parameters") or {}),
                                   "requested": dict(request["parameters"] or {})}
        if wrong:
            raise SchemaInvalid(
                f"a job of path {path_id} runs step {step_index} of its plan; this request differs in "
                f"{', '.join(sorted(wrong))}",
                details={"path_id": path_id, "step_index": step_index, "differs": wrong})

    def _manifest_sha(self, input_manifest_ref: str) -> str | None:
        """The input manifest's own sha256: what the step's job key binds (contract r5)."""
        try:
            return self.artifacts.describe(input_manifest_ref).get("sha256")
        except GxError:
            return None

    @staticmethod
    def _step_job(conn: sqlite3.Connection, *, run_id: str, path_id: str | None, step_index: int | None,
                  manifest_sha256: str | None) -> sqlite3.Row | None:
        """The latest job of one step of a path on one input manifest, by attempt: the job key
        the backend derives (contract r5: run, path, step, input manifest, attempt)."""
        if path_id is None or step_index is None or manifest_sha256 is None:
            return None
        return conn.execute(
            "SELECT * FROM jobs WHERE run_id=? AND path_id=? AND step_index=?"
            " AND json_extract(submit_intent_json, '$.input_manifest_sha256')=?"
            " ORDER BY job_attempt DESC, rowid DESC LIMIT 1",
            (run_id, path_id, step_index, manifest_sha256)).fetchone()

    def _step_answer(self, row: sqlite3.Row) -> dict[str, Any]:
        """A new call for a step whose job is open or SUCCEEDED: that job, looked at again."""
        self._reconcile_one(row)
        answer = self._submit_answer(self._row(row["job_id"]), replayed=True)
        answer["step_job"] = True
        return answer

    def _lease_and_step(self, conn: sqlite3.Connection, request: Mapping[str, Any],
                        purpose: str) -> tuple[str | None, int | None]:
        """Contract r5 (C11): the lease a job of a task is submitted under, and the step of its
        path it runs, as the request names them and the store confirms them.

        ``lease_ref`` is required whenever ``task_id`` is given, and must be the live lease of
        that task held by the caller: anything else is LEASE_EXPIRED ("re-claim the task; do
        not submit results under the old lease"). A lease named without its task is a request
        that cannot be checked. ``step_index`` is required for a job of a path, a whole number
        from 0, and is not given for a job that belongs to no path; which planned step it
        names, and that the job is that step, is the planned-step binding. A job with no task
        records the lease of nobody (None)."""
        task_id, lease_ref, step_index = request.get("task_id"), request.get("lease_ref"), request.get("step_index")
        if task_id is not None:
            if not isinstance(lease_ref, str) or not lease_ref:
                raise SchemaInvalid("a job of a task names the lease it is submitted under (lease_ref)",
                                    details={"task_id": task_id})
            held = leases.held_lease(conn, task_id, self.ctx.principal.subject)
            if held is None or held["lease_id"] != lease_ref:
                raise LeaseExpired(
                    f"lease {lease_ref} is not the live lease of task {task_id} held by "
                    f"{self.ctx.principal.subject}; re-claim the task and submit under the lease it gives",
                    details={"task_id": task_id, "lease_ref": lease_ref,
                             "live_lease_ref": held["lease_id"] if held is not None else None})
        elif lease_ref is not None:
            raise SchemaInvalid("lease_ref names a task's lease; give the task_id it belongs to",
                                details={"lease_ref": lease_ref})
        if purpose == PURPOSE_PATH:
            if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
                raise SchemaInvalid("a job of a path names the step of the path it runs (step_index, from 0)",
                                    details={"path_id": request.get("path_id"), "step_index": step_index})
            # Which planned step it names, and that the job is that step, is the planned-step
            # binding (exec's next r5 item, with planning's ``planned_steps``); C11 takes the input.
        elif step_index is not None:
            raise SchemaInvalid(f"a {purpose} job belongs to no path and runs no step of one",
                                details={"step_index": step_index})
        return lease_ref, step_index

    @staticmethod
    def _refuse_prohibited_lines(conn: sqlite3.Connection, *, budget_ref: str, amounts: Mapping[str, Any]) -> None:
        """Contract r5, ruling 17 (states.json run.rules.budget): a line whose limit is 0 is a
        prohibition, not a budget to wait for. Admission refuses a job that needs one, naming
        the line, so that it never becomes a need of a pause and no budget_increase is asked
        for it; lifting it is a change of the profile by a person. The job's own budget and
        every budget above it are read: a path's allotment cannot hold what its run prohibits.
        Refused here nothing is written."""
        ref: str | None = budget_ref
        seen: set[str] = set()
        while ref is not None and ref not in seen:
            seen.add(ref)
            row = conn.execute("SELECT budget_ref, parent_ref, limits_json FROM budgets WHERE budget_ref=?",
                               (ref,)).fetchone()
            if row is None:
                return
            limits = store.loads(row, "limits_json", {}) or {}
            for line, needed in amounts.items():
                if needed and line in limits and limits[line] == 0:
                    raise BudgetExceeded(
                        f"budget {ref} prohibits {line} (its limit is 0) and this job needs {needed} of it: "
                        "a prohibition, not a budget to wait for; lifting it is a change of the profile by a person",
                        details={"rule": "prohibited_line", "prohibited": True, "line": line, "limit": 0,
                                 "needed": needed, "budget_ref": ref, "job_budget_ref": budget_ref})
            ref = row["parent_ref"]

    def _budget_would_hold(self, budgets: Any, *, run_id: str, budget_ref: str, job_id: str,
                           amounts: Mapping[str, Any]) -> None:
        """Ask the budget whether it would hold ``amounts`` now, holding nothing: the same
        ``reserve`` as admission makes, in a transaction that is rolled back. BUDGET_EXCEEDED
        is raised exactly as reserve() raises it."""
        try:
            with store.transaction(
                self.conn, transaction_owner="JobService", operation="probe_budget", run_id=run_id,
            ) as txn:
                budgets.reserve(txn, budget_ref=budget_ref, subject_kind="job", subject_id=job_id,
                                amounts=amounts, actor=self.actor)
                raise _DryRun
        except _DryRun:
            pass

    def _check_limits(self, resources: Mapping[str, Any]) -> None:
        """The figures a job may have at all, from ``config/limits.json`` read now: the first
        refusal of :func:`.limits.limit_findings`, the predicate planning, selection,
        applicability and ``capacity.fits`` read too."""
        found = limits_mod.limit_findings(resources)
        if found:
            raise found[0]

    @staticmethod
    def _budget_amounts(resources: Mapping[str, Any]) -> dict[str, int]:
        amounts = {"job_submissions": 1,
                   "cpu_seconds": int(resources["wall_seconds"]) * int(resources["cpu_threads"])}
        if resources["gpu"] is not None:
            amounts["gpu_jobs"] = 1
            amounts["gpu_minutes"] = math.ceil(int(resources["wall_seconds"]) / 60)
        return amounts

    def active_gpu_jobs(self, *, excluding: str | None = None) -> list[str]:
        """Our own GPU jobs in flight, after looking at each one again."""
        marks = ",".join("?" for _ in IN_FLIGHT)
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks})"
            " AND json_extract(resources_json, '$.gpu.memory_mib') IS NOT NULL", IN_FLIGHT,
        ).fetchall()
        active = []
        for row in rows:
            if row["job_id"] == excluding:
                continue
            self._reconcile_one(row)
            if self._row(row["job_id"])["status"] in IN_FLIGHT:
                active.append(row["job_id"])
        return active

    def _gpu_preflight(self, gpu: Mapping[str, Any], *, excluding: str | None = None) -> dict[str, Any]:
        """The device as it is now, and the jobs this one would queue behind.

        One job is on the GPU at a time: that is the lease's doing, in the runner, and it
        holds across stores. So another GPU job of ours in flight is not a refusal here;
        the new job is admitted and its runner waits its turn. What is refused is a queue
        that is already full, and a device that does not have the room now.
        """
        ahead = self.active_gpu_jobs(excluding=excluding)
        if len(ahead) >= GPU_QUEUE_MAX:
            raise ResourceDenied(
                f"{len(ahead)} GPU job(s) of this store are already running or queued; the queue "
                f"holds at most {GPU_QUEUE_MAX}",
                details={"rule": "gpu_queue_full", "in_flight": ahead, "queue_max": GPU_QUEUE_MAX},
            )
        # ... and a queue the job could not get through before its own wait ends: the jobs
        # ahead are counted at their own time limits (limits.gpu_queue_finding, which
        # capacity.fits asks too).
        too_long = limits_mod.gpu_queue_finding(self._gpu_jobs_ahead(ahead))
        if too_long is not None:
            raise too_long
        record = probe.gpu_preflight(requested_mib=int(gpu["memory_mib"]), active_gpu_jobs=0)
        probe.assert_preflight_fresh(record)
        record["queued_behind"] = ahead
        record["concurrency"] = ("one job on the device at a time, enforced by the lease the runner "
                                 "takes; this job waits behind those listed in queued_behind")
        return record

    def _gpu_jobs_ahead(self, job_ids: list[str]) -> list[dict[str, Any]]:
        """For each of this store's GPU jobs in flight: its own time limit, and when its tool
        started if its runner says it is running (a job still waiting has used none of it)."""
        listed = []
        for job_id in job_ids:
            row = self._row(job_id)
            status = executor.read_json(self._paths(job_id).job_dir / "status.json") or {}
            listed.append({"job_id": job_id, "wall_seconds": int(row["timeout_seconds"] or 0),
                           "tool_started_at": status.get("started_at") if status.get("state") == "running" else None})
        return listed

    @staticmethod
    def _attach_resource_guard(spec: dict[str, Any], intent: dict[str, Any], policy: dict[str, Any]) -> None:
        spec["resource_guard"] = policy
        spec["gx_context"]["resource_guard_sha256"] = resource_guard.canonical_sha(policy)
        intent["resource_guard"] = policy
        spec["env"].update(policy["required_env"])

    def _spec(self, job_id: str, job: executor.JobPaths, out_dir: Path, plan: manifest_mod.LaunchPlan,
              resources: Mapping[str, Any], preflight: Mapping[str, Any]) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "schema": runner_mod.SCHEMA, "job_id": job_id, "argv": plan.argv, "cwd": plan.cwd,
            "env": plan.env, "timeout_seconds": int(resources["wall_seconds"]),
            "stdout_path": str(job.stdout), "stderr_path": str(job.stderr), "out_dir": str(out_dir),
            "limits_path": str(config.CONFIG_DIR / "limits.json"), "hold_scope": "jobs",
            "heartbeat_seconds": executor.HEARTBEAT_SECONDS, "kill_grace_seconds": 3.0,
        }
        gpu = resources["gpu"]
        if gpu is not None:
            device = int(preflight["selected_device"]["index"])
            spec["gpu"] = {
                "device": device, "need_mib": int(gpu["memory_mib"]), "scope": gpu["scope"],
                "lock_path": executor.gpu_lock_path(device),
                "min_free_mib": int(preflight["limits_applied"]["min_free_mib"]),
                "queue_timeout_seconds": gpu_queue_wait_seconds(), "sample_seconds": 2.0,
                "nvidia_smi": shutil.which("nvidia-smi"),
            }
        return spec

    # -- start: the approval, then the intent is on record, then the runner -------------

    def _start(self, row: sqlite3.Row) -> dict[str, Any]:
        conn = self.conn
        job_id = row["job_id"]
        resources = store.loads(row, "resources_json", {}) or {}
        intent = store.loads(row, "submit_intent_json", {}) or {}
        job = self._paths(job_id)

        if resources.get("gpu") is not None:
            try:
                config.require_switch("gpu_jobs", scope=resources["gpu"]["scope"])
                preflight = self._gpu_preflight(resources["gpu"], excluding=job_id)
            except GxError as exc:
                # Admitted, and it cannot start now: it ends here, not started, with its hold
                # returned. Left CREATED with its hold, and unnamed in the refusal, it was J10.
                self._fail_unstarted(row, f"{exc.code}: {exc.message}", failure_class="resource",
                                     because="switch" if isinstance(exc, ApprovalRequired) else "gpu_preflight")
                raise self._named(exc, job_id) from None
            with store.transaction(
                conn, transaction_owner="JobService", operation="save_preflight", run_id=row["run_id"],
            ) as txn:
                txn.execute("UPDATE jobs SET preflight_json=? WHERE job_id=?",
                            (ids.canonical_json(preflight), job_id))
            refusing = self._refusing_approval(job_id, intent)
            if refusing is not None:
                # A person said no. Asking again would open an approval the policy cannot grant
                # and keep the hold for ever (audit J12): the job ends, and says why.
                said = f"its GPU approval {refusing['approval_id']} is {refusing['status']}"
                self._fail_unstarted(row, said, failure_class="resource", because="approval_refused")
                raise self._named(ApprovalRequired(
                    f"job {job_id} needs the GPU and {said}. The job is ended, not started, and its "
                    "hold returned; another try is a new job with a new idempotency key",
                    details={"approval_id": refusing["approval_id"], "status": refusing["status"]}), job_id)
            approval = _approvals().request(
                self.ctx, action_kind="gpu_job", subject=intent_subject(job_id, intent),
                run_id=row["run_id"],
                request={"job_id": job_id, "capability_version": intent["capability_version"],
                         "manifest_sha256": intent["manifest_sha256"], "gpu": resources["gpu"],
                         "wall_seconds": resources["wall_seconds"]},
            )
            if approval["status"] != "GRANTED":
                deadline = self._start_deadline(row)
                raise self._named(ApprovalRequired(
                    f"job {job_id} needs the GPU and its approval {approval['approval_id']} is "
                    f"{approval['status']}: {approval.get('pending_reason')}. The job is kept CREATED, "
                    f"with its hold, until {deadline}; submit again with the same idempotency key once "
                    "it is granted, or cancel it to return the hold now",
                    details={"approval_id": approval["approval_id"], "status": approval["status"],
                             "pending_reason": approval.get("pending_reason")}), job_id)

        if not job.spec.is_file():
            self._fail_unstarted(row, "the job directory was removed before the job could start")
            raise self._named(ArtifactNotFound(f"job {job_id} has no launch specification any more"),
                              job_id)
        try:
            from ..p0 import v62_evidence
            request = intent["request"]
            # A CREATED Job may be resumed after process death or waiting for resources.
            # Verify immutable bytes outside the writer before starting its first process.
            gate_proof = v62_evidence.prepare_dispatch(conn, request)
            with store.transaction(
                conn, transaction_owner="JobService", operation="begin_submission", run_id=row["run_id"],
            ) as txn:
                current = self._row(job_id)
                if current["status"] != CREATED:
                    # A concurrent replay acquired the same launch intent.
                    return self._submit_answer(current, replayed=True)
                if (intent["request"].get("binding") or {}).get("v6_node_id"):
                    from ..p0 import v6_jobs
                    v62_evidence.fence_dispatch(txn, request, gate_proof)
                    v6_jobs.fence_launch(txn, request, job_id)
                execution_manifest.check_launch(txn, job_id)
                store.transition(txn, machine=MACHINE, subject_id=job_id, to_state=SUBMITTING,
                                 actor=self.actor, bump=False, evidence_level="EXECUTION_OBSERVED",
                                 reason="intent persisted; starting the runner",
                                 extra_updates={"side_effect_status": "UNKNOWN",
                                                "observed_at": clock.now_iso()})
        except SchemaInvalid as exc:
            reason = exc.details.get("reason")
            if reason not in ("current_intent", "superseded_attempt"):
                raise
            self._fail_unstarted(row, exc.message, failure_class="input_or_format", because=reason)
            raise self._named(exc, job_id) from None
        try:
            identity = executor.launch(job.job_dir)
        except executor.LaunchReceiptUnknown as exc:
            self._mark_unknown(row, {"reason": str(exc), "observed_at": clock.now_iso()})
            self._reconcile_one(self._row(job_id))
            return self._submit_answer(self._row(job_id), replayed=False)
        except executor.RunnerLaunchFailed as exc:
            self._fail_unstarted(self._row(job_id), f"the runner could not be started: {exc}")
            raise self._named(ResourceDenied(f"job {job_id} could not be started: {exc}",
                                             details={"failure_class": "environment"}), job_id) from exc
        now = clock.now_iso()
        with store.transaction(
            conn, transaction_owner="JobService", operation="record_submission", run_id=row["run_id"],
        ) as txn:
            current = self._row(job_id)
            if current["status"] not in (SUBMITTING, OUTCOME_UNKNOWN):
                # A separate observer already found the detached runner or its outcome.
                return self._submit_answer(current, replayed=False)
            store.transition(
                txn, machine=MACHINE, subject_id=job_id, to_state=SUBMITTED, actor=self.actor,
                bump=False, evidence_level="EXECUTION_OBSERVED",
                reason=f"runner pid {identity['runner_pid']} started",
                extra_updates={
                    "runner_pid": identity["runner_pid"], "runner_started": identity["runner_started"],
                    "scheduler_ref": f"runner:{identity['runner_pid']}:{identity['runner_started']}",
                    "submitted_at": now, "observed_at": now, "side_effect_status": "APPLIED",
                },
            )
        return self._submit_answer(self._row(job_id), replayed=False)

    # -- the end of a job: its terminal state and its trigger, in one transaction -------

    def _end(self, txn: sqlite3.Connection, row: sqlite3.Row, to_state: str, *, reason: str | None,
             extra_updates: Mapping[str, Any], payload: Mapping[str, Any] | None = None) -> int | None:
        """The one way a job becomes SUCCEEDED, FAILED or CANCELLED: the transition and, in the
        same transaction, the ``job.terminal`` outbox row a reaction waits for (SCHEDULING_DESIGN
        r2.2 §2.4.1, §7.3). Every ending goes through here, so none can forget the trigger; and
        it is written inside ``txn``, so a job is never terminal without its trigger nor
        announced without being terminal. A look that is cut off leaves both to the next look.

        One row per job needs no lookup: a terminal state has no way out in the job machine,
        so a second ending of the same job is refused by the transition itself. Returns the
        id of the event that records the ending.
        """
        if to_state not in TERMINAL:  # pragma: no cover - a programming error, not a state of the world
            raise RuntimeError(f"{to_state!r} is not a terminal job state")
        moved = store.transition(
            txn, machine=MACHINE, subject_id=row["job_id"], to_state=to_state, actor=self.actor,
            bump=False, reason=reason, evidence_level="EXECUTION_OBSERVED",
            extra_updates=dict(extra_updates), payload=dict(payload) if payload is not None else None)
        execution_manifest.complete(txn, row["job_id"])
        store.enqueue_outbox(txn, topic=TERMINAL_TOPIC, idempotency_key=terminal_trigger_key(row["job_id"]),
                             payload=terminal_trigger(row, to_state, moved.event_id))
        from ..p0 import waits
        waits.terminal_job(txn, row)
        return moved.event_id

    def _fail_unstarted(self, row: sqlite3.Row, reason: str, *, failure_class: str = "environment",
                        because: str | None = None) -> None:
        """A job whose tool never ran: FAILED, its hold returned, an approval it was waiting
        for withdrawn, its directories removed. ``because`` names what stopped it, for a
        scheduler (``switch``, ``gpu_preflight``, ``approval_refused``, ``start_deadline``)."""
        with store.transaction(
            self.conn, transaction_owner="JobService", operation="fail_unstarted", run_id=row["run_id"],
        ) as txn:
            current = txn.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            if current["status"] != row["status"] or current["status"] not in (CREATED, SUBMITTING):
                return
            failure = {"failure_class": failure_class, "observed": "not_started", "not_started": True,
                       "summary": reason}
            if because is not None:
                failure["not_started_because"] = because
            outcome = {"result": "not_started", "reason": reason, "started": False, "failure": failure,
                       "outputs": [], "approvals": self._withdraw_approvals(txn, current, reason),
                       "charged": False, "charge": "nothing: the tool never started, so the hold is returned"}
            self._end(txn, current, FAILED, reason=reason[:500],
                      extra_updates={"outcome_json": outcome, "finished_at": clock.now_iso(),
                                     "observed_at": clock.now_iso(), "side_effect_status": "NONE"})
            self._release(txn, current, reason)
        retained.remove_staging(storage.store_dir(self.conn), row)
        executor.remove_tree(self._paths(row["job_id"]).in_dir)

    def _withdraw_approvals(self, conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> dict[str, Any]:
        """A job that ends before it starts takes its approvals with it: a PENDING one would
        go on counting as somebody waiting, a GRANTED one would be bound to nothing that can
        run. Its subject (``intent_subject``) is unique to the job."""
        intent = store.loads(row, "submit_intent_json", {}) or {}
        if not intent.get("request_sha256"):
            return {"withdrawn": None, "note": "the job has no recorded request to bind an approval to"}
        try:
            counts = _approvals().invalidate(
                conn, intent_subject(row["job_id"], intent), actor=self.actor,
                reason=f"job {row['job_id']} ended before it started: {reason}"[:500], decided_by="job_ended")
        except GxError as exc:
            return {"withdrawn": None, "error": exc.to_payload()}
        return {"withdrawn": counts}

    def _refusing_approval(self, job_id: str, intent: Mapping[str, Any]) -> dict[str, Any] | None:
        """The job's latest GPU approval, when it says no (DENIED or REVOKED)."""
        latest = latest_admission_approval(self.conn, job_id, intent)
        return latest if latest is not None and latest["status"] in _REFUSING_APPROVALS else None

    def _look_at_created(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """A CREATED job was admitted and has not started: it waits for its GPU approval, or
        its submitter died between writing the row and starting the runner. With its approval
        refused, or past its start deadline, it is ended not started, its hold returned and
        its approvals withdrawn (audit J10, J12). Otherwise nothing is decided. Inside a
        caller's transaction nothing is decided either (J2)."""
        if self.conn.in_transaction:
            return None
        job_id = row["job_id"]
        intent = store.loads(row, "submit_intent_json", {}) or {}
        latest = latest_admission_approval(self.conn, job_id, intent)
        now = clock.now_iso()
        if latest is not None and latest["status"] in _REFUSING_APPROVALS:
            reason = f"its GPU approval {latest['approval_id']} is {latest['status']}"
            self._fail_unstarted(row, reason, failure_class="resource", because="approval_refused")
            return {"observed_at": now, "state": None, "reason": f"ended, not started: {reason}"}
        deadline = start_deadline_seconds()
        if clock.age_seconds(row["created_at"]) <= deadline:
            return None
        if latest is not None:
            why = (f"its GPU approval {latest['approval_id']} was {latest['status']}"
                   + ("" if latest["status"] == "PENDING" else " and nobody submitted the job again with its key"))
        else:
            why = "it had no approval to wait for: its submitter stopped before starting the runner"
        reason = f"not started within {deadline} s of its admission at {row['created_at']}; {why}"
        self._fail_unstarted(row, reason, failure_class="resource" if latest is not None else "environment",
                             because="start_deadline")
        return {"observed_at": now, "state": None, "reason": f"ended, not started: {reason}"}

    @staticmethod
    def _start_deadline(row: sqlite3.Row) -> str:
        return (clock.parse(row["created_at"]) + timedelta(seconds=start_deadline_seconds())).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _named(self, exc: GxError, job_id: str) -> GxError:
        """A refusal raised after the job's row exists: it names the job, says what is left of
        it, and reports ``side_effect_status`` PARTIAL, because the request was not without
        effect (audit J13). The job is kept CREATED with its hold, or ended with it returned."""
        row = self.conn.execute("SELECT status, reservation_ref, created_at FROM jobs WHERE job_id=?",
                                (job_id,)).fetchone()
        exc.side_effect_status = "PARTIAL"
        details = {"job_id": job_id, "job_status": row["status"] if row is not None else None}
        if row is not None and row["status"] == CREATED:
            details.update(kept="the job is CREATED and holds its budget", reservation_ref=row["reservation_ref"],
                           start_deadline_at=self._start_deadline(row))
        else:
            details["kept"] = "nothing: the job ended before it started and its hold was returned"
        exc.details = {**details, **(exc.details or {})}
        return exc

    def _release(self, conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
        if row["reservation_ref"]:
            try:
                _budgets().release(conn, row["reservation_ref"], actor=self.actor, reason=reason[:300])
            except GxError:
                pass  # already settled or released; the ledger says which

    # ---------------------------------------------------------------- status and wait

    def status(self, job_id: str) -> dict[str, Any]:
        """The job as it is now. Looks at the runner before it answers."""
        observation = self._reconcile_one(self._row(job_id))
        return self.view(self._row(job_id), observation=observation)

    def wait(self, job_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Wait, at most sixty seconds, for the job to reach a terminal state."""
        limit = MAX_WAIT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        if limit < 0 or limit > MAX_WAIT_SECONDS:
            raise SchemaInvalid(
                f"timeout_seconds must be between 0 and {MAX_WAIT_SECONDS}; a longer wait is "
                "another call",
                details={"timeout_seconds": timeout_seconds, "max": MAX_WAIT_SECONDS},
            )
        started = time.monotonic()
        pause = 0.1
        while True:
            view = self.status(job_id)
            waited = time.monotonic() - started
            if view["terminal"] or waited >= limit:
                view["waited_seconds"] = round(waited, 3)
                return view
            time.sleep(min(pause, max(0.0, limit - waited)))
            pause = min(pause * 1.5, MAX_POLL_SECONDS)

    # ------------------------------------------------------------------------ cancel

    def cancel(self, job_id: str, *, reason: str, idempotency_key: str | None = None) -> dict[str, Any]:
        """Ask for a job to be ended. A request is not a cancellation: the runner confirms."""
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaInvalid("a cancel names its reason", details={})
        if self.conn.in_transaction:
            raise RuntimeError(
                "JobService.cancel was called inside an open transaction. It looks at the job's "
                "runner and may settle a job that has just finished; commit first, then cancel."
            )
        row = self._row(job_id)
        observation = self._reconcile_one(row)
        row = self._row(job_id)
        status = row["status"]
        if status in TERMINAL:
            return {"job_id": job_id, "status": status,
                    "note": f"the job had already ended as {status}; nothing was cancelled"}
        if status in (SUBMITTING, OUTCOME_UNKNOWN):
            return self._cancel_unsettled(row, observation, reason, idempotency_key)
        if status == CREATED:
            with store.transaction(
                self.conn, transaction_owner="JobService", operation="cancel_unstarted", run_id=row["run_id"],
            ) as txn:
                current = self._row(job_id)
                if current["status"] == CREATED:
                    approvals = self._withdraw_approvals(txn, row, f"cancelled: {reason}")
                    self._end(txn, row, CANCELLED, reason=reason[:500],
                              extra_updates={"finished_at": clock.now_iso(),
                                             "outcome_json": {"result": "cancelled", "started": False,
                                                              "reason": reason, "outputs": [],
                                                              "approvals": approvals, "charged": False,
                                                              "charge": "nothing: the job was cancelled before "
                                                                        "it started, so the hold is returned"}})
                    self._release(txn, row, f"cancelled before it started: {reason}")
            if current["status"] != CREATED:
                return self.cancel(job_id, reason=reason, idempotency_key=idempotency_key)
            retained.remove_staging(storage.store_dir(self.conn), row)
            executor.remove_tree(self._paths(job_id).job_dir)
            return {"job_id": job_id, "status": CANCELLED, "note": "the job had not started"}
        if status != CANCEL_REQUESTED:
            with store.transaction(
                self.conn, transaction_owner="JobService", operation="request_cancel", run_id=row["run_id"],
            ) as txn:
                store.transition(
                    txn, machine=MACHINE, subject_id=job_id, to_state=CANCEL_REQUESTED,
                    actor=self.actor, bump=False, reason=reason[:500],
                    evidence_level="EXECUTION_OBSERVED",
                    payload={"cancel_idempotency_key": idempotency_key},
                )
        wrote = executor.request_cancel(self._paths(job_id).job_dir, reason=reason, actor=self.actor)
        return {
            "job_id": job_id, "status": CANCEL_REQUESTED,
            "note": ("cancel requested; the runner ends the tool and the job becomes CANCELLED "
                     "when it confirms" if wrote else
                     "a cancel was already requested for this job; no second request was made"),
        }

    def _cancel_unsettled(self, row: sqlite3.Row, observation: Mapping[str, Any] | None, reason: str,
                          idempotency_key: str | None) -> dict[str, Any]:
        """Cancel a job that is SUBMITTING or OUTCOME_UNKNOWN: states the contract gives no
        CANCEL_REQUESTED edge.

        The request is written for the runner, if there is one: it ends the tool, or never
        starts it, and says so in its outcome, which settles the job as CANCELLED. The answer
        is CANCEL_REQUESTED, the request's state; the row keeps its own until the runner's
        answer is read. The one case decided here is a job of which nothing is known to run
        and whose end cannot be read (its runner is gone, or its directory is, so no runner
        could be told): cancelling is how whoever waits on it stops waiting, so it is
        CANCELLED with that recorded, charged one submission, and whatever is left of its
        job and staging directories is kept for a person to look at. A runner that is alive
        and cannot be told still ends its tool at the tool's own time limit.
        """
        job_id, status = row["job_id"], row["status"]
        wrote = executor.request_cancel(self._paths(job_id).job_dir, reason=reason, actor=self.actor)
        seen = dict(observation or {})
        if status == OUTCOME_UNKNOWN and (seen.get("runner_alive") is False or wrote is None):
            current = self._cancel_unreadable(row, seen, reason=reason, idempotency_key=idempotency_key,
                                              request_delivered=wrote is not None)
            if current != CANCELLED:
                return {"job_id": job_id, "status": current,
                        "note": f"the job moved to {current} while it was being cancelled"}
            how = ("nothing is running for this job" if seen.get("runner_alive") is False
                   else "its job directory is gone, so no runner could be told")
            return {"job_id": job_id, "status": CANCELLED,
                    "note": (f"{how} and how it ended could not be read; it is cancelled with that "
                             "recorded, charged one submission, and what is left of its job and staging "
                             "directories is kept")}
        with store.transaction(
            self.conn, transaction_owner="JobService", operation="cancel_unsettled", run_id=row["run_id"],
        ) as txn:
            store.append_event(
                txn, kind="job.cancel_requested", actor=self.actor, run_id=row["run_id"],
                subject_kind="job", subject_id=job_id, reason=reason[:500],
                evidence_level="EXECUTION_OBSERVED",
                payload={"status": status, "request_written": wrote,
                         "cancel_idempotency_key": idempotency_key})
        delivered = ("a cancel request was written for its runner" if wrote else
                     "a cancel was already requested for this job; no second request was made")
        why = ("the job is still being submitted" if status == SUBMITTING
               else f"how the job is doing cannot be read ({seen.get('reason') or 'no reason recorded'})")
        return {"job_id": job_id, "status": CANCEL_REQUESTED,
                "note": (f"{why}; {delivered}. The job stays {status} until its runner's answer is read: "
                         "it becomes CANCELLED then, or is settled as what it was if it had already ended, "
                         "or, if its runner goes away without answering, CANCELLED at the next look")}

    def _cancel_unreadable(self, row: sqlite3.Row, seen: Mapping[str, Any], *, reason: str,
                           idempotency_key: str | None, request_delivered: bool | None) -> str:
        """End an OUTCOME_UNKNOWN job CANCELLED because a cancel was asked of it and nothing
        that runs can answer: its runner is gone (or its directory is), and how it ended
        cannot be read. Charged one submission, since whether its tool started cannot be
        read; what is left of its job and staging directories is kept for a person. Used by
        the cancel itself and by a later look at a cancel nobody answered. Returns the
        status the job has afterwards: CANCELLED, or what it had moved to meanwhile."""
        job_id = row["job_id"]
        now = clock.now_iso()
        resources = store.loads(row, "resources_json", {}) or {}
        with store.transaction(
            self.conn, transaction_owner="JobService", operation="cancel_unreadable", run_id=row["run_id"],
        ) as txn:
            current = txn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()["status"]
            if current != OUTCOME_UNKNOWN:
                return current
            self._end(
                txn, row, CANCELLED, reason=reason[:500],
                extra_updates={
                    "finished_at": now, "observed_at": now,
                    "outcome_json": {"result": "cancelled", "started": None, "reason": reason,
                                     "outputs": [], "failure": None,
                                     "outcome_unknown": seen.get("reason"),
                                     "runner_alive": seen.get("runner_alive"),
                                     "request_delivered": request_delivered,
                                     "charged": True,
                                     "charge": "one submission: whether the tool started cannot be read",
                                     "kept": {"job_dir": str(self._paths(job_id).job_dir),
                                              "staging": str(self._staging(row)) if row["staging_ref"] else None}},
                },
                payload={"cancel_idempotency_key": idempotency_key, "outcome_unknown": seen.get("reason")},
            )
            if row["reservation_ref"]:
                try:
                    # Whether its tool ever started cannot be read, so this is not the case of
                    # a job known never to have started (which is charged nothing): one
                    # submission, and nothing measured.
                    _budgets().settle(txn, row["reservation_ref"], actor=self.actor,
                                      actual=self._actual({"started": None}, resources))
                except GxError as exc:
                    store.append_event(
                        txn, kind="job.budget_settle_failed", actor=self.actor, run_id=row["run_id"],
                        subject_kind="job", subject_id=job_id, evidence_level="EXECUTION_OBSERVED",
                        payload={"error": exc.to_payload()})
        return CANCELLED

    def cancel_for_run(self, run_id: str, *, reason: str) -> list[dict[str, Any]]:
        """Ask for every job of a run that has not ended to be ended. One answer per job.

        For whoever cancels a run. Call it after the run's own state has been committed and
        outside any transaction: each job is looked at and asked separately, so one runner
        that does not answer cannot hold anything up, and a job that had already finished is
        settled as what it was rather than relabelled. A job whose cancel raised is answered
        with ``status`` None and the ``error``, and the jobs after it are still asked.
        """
        marks = ",".join("?" for _ in IN_FLIGHT)
        rows = self.conn.execute(
            f"SELECT job_id FROM jobs WHERE run_id=? AND status IN (?,{marks}) ORDER BY created_at",
            (run_id, CREATED, *IN_FLIGHT)).fetchall()
        answers = []
        for row in rows:
            # One job whose cancel raises must not leave the jobs behind it running: the run's
            # own CANCELLED is already committed when this is called (audit J1 residual).
            try:
                answers.append(self.cancel(row["job_id"], reason=reason))
            except GxError as exc:
                answers.append({"job_id": row["job_id"], "status": None, "error": exc.to_payload()})
            except Exception as exc:  # noqa: BLE001 - reported per job, and the rest still asked
                answers.append({"job_id": row["job_id"], "status": None,
                                "error": {"code": type(exc).__name__, "message": str(exc)[:500]}})
        return answers

    def cancel_unstarted_for_run(self, run_id: str, *, reason: str) -> list[dict[str, Any]]:
        """Ask for every job of a run whose tool has not started to be ended: a CREATED job
        (admitted, waiting for its approval) and a job whose runner is waiting for the GPU
        lease. One answer per such job, as ``cancel_for_run`` gives them.

        For whoever pauses a run (audit, jobs skeptic: a GPU job queued before a pause still
        started on the device, because the runner's queue stops only for a cancel). A job
        whose tool is running is left to finish: a pause stops new work from starting, it
        does not throw away work in flight, and its ending is announced as any other. A job
        ended here never started, so nothing is charged for it. Call it after the run's own
        state has been committed and outside any transaction; inside one it is refused, as
        a cancel is.
        """
        if self.conn.in_transaction:
            raise RuntimeError(
                "JobService.cancel_unstarted_for_run was called inside an open transaction. It "
                "looks at each job's runner and ends jobs; commit the run's own change first.")
        marks = ",".join("?" for _ in IN_FLIGHT)
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE run_id=? AND status IN (?,{marks}) ORDER BY rowid",
            (run_id, CREATED, *IN_FLIGHT)).fetchall()
        answers = []
        for row in rows:
            if row["status"] != CREATED:
                seen = executor.observe(self._paths(row["job_id"]).job_dir, runner_pid=row["runner_pid"],
                                        runner_started=row["runner_started"])
                if seen["state"] != executor.QUEUED:
                    continue
            try:
                answers.append(self.cancel(row["job_id"], reason=reason))
            except GxError as exc:
                answers.append({"job_id": row["job_id"], "status": None, "error": exc.to_payload()})
            except Exception as exc:  # noqa: BLE001 - reported per job, and the rest still asked
                answers.append({"job_id": row["job_id"], "status": None,
                                "error": {"code": type(exc).__name__, "message": str(exc)[:500]}})
        return answers

    # --------------------------------------------------------------------- reconcile

    @staticmethod
    def next_poll_seconds(conn: sqlite3.Connection, run_id: str, checked_at: str) -> float | None:
        """Next look after a reconciliation, including starts and runners still handing off."""
        states = (CREATED, *IN_FLIGHT)
        rows = conn.execute("SELECT status,created_at,submitted_at,observed_at,resources_json,timeout_seconds "
                            f"FROM jobs WHERE run_id=? AND status IN ({','.join('?' * len(states))})",
                            (run_id, *states)).fetchall()
        if not rows:
            return None
        checked = clock.parse(checked_at)
        due = checked + timedelta(seconds=MAX_POLL_SECONDS)
        limits = config.limits()
        for row in rows:
            deadlines = [clock.parse(leases.job_deadline(row, limits=limits, at=checked_at)["deadline_at"])]
            if row["status"] == CREATED:
                deadlines.append(clock.parse(row["created_at"]) + timedelta(seconds=start_deadline_seconds(limits)))
            elif row["status"] == SUBMITTING:
                deadlines.append(clock.parse(row["observed_at"] or row["created_at"]) +
                                 timedelta(seconds=SUBMIT_GRACE_SECONDS))
            for when in deadlines:
                # An unavailable backend may leave a past-due Job open. Poll again at the normal bound.
                if checked < when < due:
                    due = when
        return max(0.0, (due - clock.now()).total_seconds())

    def reconcile(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Look again at every job that has not ended (of one run, or of the store), and settle
        the ones that have. Returns ``[{job_id, from, to}]`` for the jobs that moved.

        This is what makes a job's ending known when nobody is waiting for it: a job row moves
        only when somebody looks, and the worker that submitted a job may be gone. Whoever is
        about to decide what a run does next calls ``reconcile(run_id)`` first (the lazy path
        of SCHEDULING_DESIGN r2.2 §2.4.2), and every job it settles writes its ``job.terminal``
        outbox row in the transaction that settles it. A CREATED job is looked at too: past
        its start deadline, or with its approval refused, it is ended not started and its
        hold returned (audit J10, J12).

        Call it before taking a write lock, never inside a transaction: a settlement publishes
        outputs and removes the staging directory, and a rollback brings no file back (audit
        J2). Inside one it is refused, loudly, because a reconcile that silently decided
        nothing would leave the caller believing every finished job had been announced. With
        no open job of the run it is one SELECT and writes nothing.
        """
        if self.conn.in_transaction:
            raise RuntimeError(
                "JobService.reconcile was called inside an open transaction. Settling a job "
                "publishes its outputs and removes its staging directory; if the transaction "
                "rolled back, the row would be as it was and the files gone. Reconcile before "
                "the transaction begins."
            )
        marks = ",".join("?" for _ in IN_FLIGHT)
        query = f"SELECT * FROM jobs WHERE status IN (?,{marks})"
        params: list[Any] = [CREATED, *IN_FLIGHT]
        if run_id is not None:
            query += " AND run_id=?"
            params.append(run_id)
        started = time.monotonic_ns()
        rows = self.conn.execute(query + " ORDER BY created_at", params).fetchall()
        changed = []
        deferred = 0
        for row in rows:
            before = row["status"]
            observation = self._reconcile_one(row)
            if observation and observation.get("deferred"):
                deferred += 1
            after = self._row(row["job_id"])
            if after["status"] != before:
                changed.append({"job_id": row["job_id"], "from": before, "to": after["status"]})
        metrics.record("job_reconcile", started=started, run_id=run_id, open_jobs=len(rows),
                       changed_jobs=len(changed), deferred_jobs=deferred)
        return changed

    def _reconcile_one(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """Look at one job's runner and record what is seen. Never starts a runner.

        What is seen decides only when it can be read. Evidence that exists and cannot be
        read (an outcome that does not parse, a runner record that cannot be opened, a job
        directory that is gone after its runner ended) is OUTCOME_UNKNOWN with the reason:
        nothing is published, charged or removed, and a later look that can read it settles
        the job. "Not there" is different from "cannot be read": a runner records itself
        before it starts the tool and writes its outcome before it exits, so a missing
        record is itself an observation.
        A temporary backend failure supplies no new observation. The next poll retries it.
        """
        status = row["status"]
        if status in TERMINAL:
            return None
        if status == CREATED:
            return self._look_at_created(row)
        job_id = row["job_id"]
        try:
            job = self._paths(job_id)
            observation = executor.observe(job.job_dir, runner_pid=row["runner_pid"],
                                           runner_started=row["runner_started"])
        except OSError as exc:
            if not executor.temporarily_unavailable(exc):
                raise
            return executor.observation_error(exc)
        if observation.get("deferred"):
            # A failed status lookup provides no new Job fact. Keep its last observation and hold.
            return observation
        state = observation["state"]

        if self.conn.in_transaction:
            # Inside somebody else's transaction: look, and decide nothing. A settlement here
            # publishes outputs, charges the hold and removes the staging directory; if that
            # transaction then rolls back, the row is as it was and the files are gone.
            note = "looked at inside an open transaction; recorded and settled by the next look outside one"
            observation["reason"] = f"{observation['reason']}; {note}" if observation.get("reason") else note
            return observation
        if state == executor.UNKNOWN:
            if status == OUTCOME_UNKNOWN and observation.get("runner_alive") is False:
                asked = executor.read_json(job.job_dir / "cancel.request")
                if asked is not None:
                    # A cancel was asked while a runner could still answer it, and that runner
                    # is gone without answering (it had written its outcome and was exiting):
                    # nothing can answer the request now. The cancel's own rule decides.
                    self._cancel_unreadable(row, observation,
                                            reason=str(asked.get("reason") or "a cancel was requested"),
                                            idempotency_key=None, request_delivered=True)
                    return observation
            self._mark_unknown(row, observation)
            return observation
        if status == SUBMITTING and state == executor.NOT_STARTED:
            since = row["observed_at"] or row["created_at"]
            if clock.age_seconds(since) < SUBMIT_GRACE_SECONDS:
                observation["reason"] = "the submission is still in flight"
                return observation
            # The submitter died between persisting the intent and starting the runner.
            # Nothing ran: say so, rather than leaving a job nobody can explain.
            self._fail_unstarted(row, "submission was interrupted before the runner started; "
                                      "nothing ran")
            return observation
        if status == OUTCOME_UNKNOWN and state == executor.NOT_STARTED:
            # Evidence that could not be read earlier is not there now, and no runner can be
            # named. That is not "nothing ran": it stays unknown until a person looks.
            observation["reason"] = ("earlier evidence of this job could not be read and no runner "
                                     "record can be found now; nothing is decided")
            with store.transaction(
                self.conn, transaction_owner="JobService", operation="observe_unknown", run_id=row["run_id"],
            ) as txn:
                txn.execute("UPDATE jobs SET observed_at=? WHERE job_id=?",
                            (observation["observed_at"], job_id))
            return observation
        if state == executor.FINISHED:
            outcome = dict(observation["outcome"])
            guarded = outcome.get("resource_guard") or {}
            expected_guard = (store.loads(row, "submit_intent_json", {}) or {}).get("resource_guard")
            result = outcome.get("result")
            started = outcome.get("started")
            # The runner can receive a cancel or a termination signal before spawning the tool.
            # It reports started=False then, and there is no child group to clean up.
            unstarted = (result in runner_mod.NOT_STARTED_RESULTS and started is not True) or (
                result in (runner_mod.RESULT_CANCELLED, runner_mod.RESULT_TERMINATED) and started is False)
            if (expected_guard and result == runner_mod.RESULT_RUNNER_ERROR and started is True
                    and guarded.get("schema") == resource_guard.SCHEMA
                    and (guarded.get("cleanup") or {}).get("proven_gone") is not True):
                # A failed runner cannot update its original cleanup observation. The next
                # caller can finish the same recovery as for a lost runner after it exits.
                status_doc = executor.read_json(job.job_dir / "status.json") or {}
                if self._failed_runner_gone(row, outcome, status_doc):
                    orphans, cleanup = self._recover_runner_group(status_doc, job.job_dir, expected_guard)
                    outcome["orphans"] = {**orphans, "cleanup": cleanup}
                    guarded = outcome["resource_guard"] = {**guarded, "cleanup": cleanup}
            if expected_guard and not unstarted and (guarded.get("schema") != resource_guard.SCHEMA
                    or (guarded.get("cleanup") or {}).get("proven_gone") is not True):
                observation.update(state=executor.UNKNOWN,
                    reason="job child-group cleanup is unproven; retain budget, staging and owner intent")
                self._mark_unknown(row, observation)
                return observation
            if result == runner_mod.RESULT_RUNNER_ERROR and not expected_guard:
                # Older runners have no child-group cleanup record.
                outcome["orphans"] = executor.end_orphans(executor.read_json(job.job_dir / "status.json"),
                                                          job_dir=job.job_dir)
            self._settle(row, outcome)
            return observation
        if state in (executor.LOST, executor.NOT_STARTED):
            status_doc = observation.get("status") or {}
            expected_guard = (store.loads(row, "submit_intent_json", {}) or {}).get("resource_guard")
            orphans, cleanup = self._recover_runner_group(status_doc, job.job_dir, expected_guard)
            if cleanup is not None and not cleanup["proven_gone"]:
                observation.update(state=executor.UNKNOWN,
                    reason="lost job runner has no proven ended process group; retain owner budget")
                self._mark_unknown(row, observation)
                return observation
            self._settle(row, {
                "result": "runner_lost",
                "reason": observation.get("reason") or "the runner is gone and left no outcome",
                **({"resource_guard": {"schema": resource_guard.SCHEMA, "cleanup": cleanup}} if cleanup else {}),
                "orphans": orphans, "runner_pid": observation.get("runner_pid"),
                "runner_started": observation.get("runner_started"),
                **self._lost_runner_extent(status_doc, job.job_dir),
            })
            return observation

        target = {executor.QUEUED: QUEUED, executor.RUNNING: RUNNING}.get(state, SUBMITTED)
        updates: dict[str, Any] = {"observed_at": observation["observed_at"]}
        if observation.get("heartbeat_age_seconds") is not None:
            # When the runner last beat, not when we looked: a stopped runner's heartbeat ages
            # while every look is new (audit J11). observed_at minus heartbeat_at is the age.
            updates["heartbeat_at"] = clock.iso_after(-float(observation["heartbeat_age_seconds"]))
        if not row["runner_pid"] and observation.get("runner_pid"):
            # The submitter died after starting the runner and before writing that down: the
            # runner is found again from its own record, and the row says so from now on.
            updates.update(runner_pid=observation.get("runner_pid"),
                           runner_started=observation.get("runner_started"),
                           scheduler_ref=f"runner:{observation.get('runner_pid')}:"
                                         f"{observation.get('runner_started')}",
                           side_effect_status="APPLIED")
            if not row["submitted_at"]:
                updates["submitted_at"] = clock.now_iso()
        if status == OUTCOME_UNKNOWN:
            updates["side_effect_status"] = "APPLIED"      # a live runner was seen
        if status == SUBMITTING:
            target = SUBMITTED
        reason = f"observed {state.lower()}"
        if "runner_pid" in updates:
            reason = (f"runner found again from {observation.get('identity_from')} in its job "
                      f"directory; observed {state.lower()}")
        with store.transaction(
            self.conn, transaction_owner="JobService", operation="record_observation", run_id=row["run_id"],
        ) as txn:
            current = txn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()["status"]
            if current in TERMINAL:
                return observation
            if (current != target and current != CANCEL_REQUESTED
                    and machines.can_transition(MACHINE, current, target)):
                store.transition(txn, machine=MACHINE, subject_id=job_id, to_state=target,
                                 actor=self.actor, bump=False, evidence_level="EXECUTION_OBSERVED",
                                 reason=reason, extra_updates=updates)
            else:
                sets = ", ".join(f"{column}=?" for column in updates)
                txn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?", (*updates.values(), job_id))
        return observation

    @staticmethod
    def _failed_runner_gone(row: sqlite3.Row, outcome: Mapping[str, Any], status: Mapping[str, Any]) -> bool:
        """Can the service take over cleanup of the child recorded by this runner?"""
        pid, started = outcome.get("runner_pid"), outcome.get("runner_started")
        tool = outcome["resource_guard"].get("tool_pid") or outcome.get("tool_pid")
        if (type(pid) is not int or pid <= 0 or not str(started).isdigit()
                or outcome.get("job_id") != row["job_id"] or status.get("job_id") != row["job_id"]
                or (pid, started) != (status.get("runner_pid"), status.get("runner_started"))
                or (row["runner_pid"] and (row["runner_pid"], row["runner_started"]) != (pid, started))
                or type(tool) is not int or tool <= 0 or tool != status.get("tool_pid")
                or tool != status.get("tool_pgid")):
            return False
        try:
            process = resource_guard.parse_stat(Path(f"/proc/{pid}/stat").read_text())
        except (FileNotFoundError, ProcessLookupError):
            return True
        except (OSError, ValueError):
            return False
        return process["state"] == "Z" or process["start_ticks"] != str(started)

    @staticmethod
    def _recover_runner_group(status: dict[str, Any], job_dir: Path, policy: Mapping[str, Any] | None
                              ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """End a dead runner's remaining children and check its group's disposition."""
        if not policy:
            return executor.end_orphans(status, job_dir=job_dir), None
        pid, started = status.get("tool_pid"), status.get("tool_started")
        if type(pid) is not int or pid <= 0 or not str(started).isdigit():
            return {}, {"proven_gone": False}
        try:
            (job_dir / "heartbeat").stat()  # end_orphans uses this upper bound on descendant start times
            try:
                leader = resource_guard.parse_stat(Path(f"/proc/{pid}/stat").read_text())
            except (FileNotFoundError, ProcessLookupError):
                leader = None  # surviving children can retain the group after its leader disappears
            if leader is not None and (leader["start_ticks"] != str(started) or leader["pgid"] != pid):
                return {}, {"proven_gone": False}
            orphans = executor.end_orphans(status, job_dir=job_dir)
            cleanup = resource_guard.wait_group_gone(pid, policy)
        except (OSError, ValueError):
            return {}, {"proven_gone": False}
        return orphans, cleanup

    @staticmethod
    def _side_effect(outcome: Mapping[str, Any]) -> str:
        """Whether the tool ran: APPLIED, NONE, or UNKNOWN when nothing on record says."""
        if outcome.get("result") in runner_mod.NOT_STARTED_RESULTS or outcome.get("started") is False:
            return "NONE"
        if outcome.get("started") is True:
            return "APPLIED"
        return "UNKNOWN"

    @staticmethod
    def _lost_runner_extent(status_doc: Mapping[str, Any], job_dir: Path) -> dict[str, Any]:
        """How far a lost runner got, from its own last status and heartbeat (audit J5).

        If it had started the tool, the tool ran from its recorded start until at most the
        runner's last heartbeat: that span is the job's wall time, recorded as an estimate,
        and it is charged, rather than charging a job that ran as one that never started.
        """
        state = status_doc.get("state")
        if state == "running" or status_doc.get("tool_pid"):
            started: bool | None = True
        elif state in ("starting", "waiting_gpu", "guarding"):
            started = False
        else:
            started = None
        extent: dict[str, Any] = {"started": started}
        if started:
            try:
                begun = calendar.timegm(time.strptime(str(status_doc["started_at"])[:19], "%Y-%m-%dT%H:%M:%S"))
                last = (Path(job_dir) / "heartbeat").stat().st_mtime
                extent.update(wall_seconds=round(max(0.0, last - begun), 3), estimated=True,
                              started_at=status_doc.get("started_at"))
            except (KeyError, OSError, ValueError):
                extent.update(wall_seconds=None, estimated=True)
        return extent

    def _mark_unknown(self, row: sqlite3.Row, observation: Mapping[str, Any]) -> None:
        """Record that the job's evidence cannot be read. Decides nothing else: no outcome,
        no charge, no publication, nothing removed, no runner started or ended."""
        reason = str(observation.get("reason") or "the job's evidence cannot be read")
        with store.transaction(
            self.conn, transaction_owner="JobService", operation="mark_unknown", run_id=row["run_id"],
        ) as txn:
            current = txn.execute("SELECT status FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            if current["status"] in TERMINAL:
                return
            updates = {"observed_at": observation["observed_at"], "side_effect_status": "UNKNOWN"}
            if current["status"] != OUTCOME_UNKNOWN and machines.can_transition(
                    MACHINE, current["status"], OUTCOME_UNKNOWN):
                store.transition(
                    txn, machine=MACHINE, subject_id=row["job_id"], to_state=OUTCOME_UNKNOWN,
                    actor=self.actor, bump=False, evidence_level="EXECUTION_OBSERVED",
                    reason=reason[:500], extra_updates=updates,
                    payload={"observation": reason, "runner_alive": observation.get("runner_alive"),
                             "identity_from": observation.get("identity_from")},
                )
            else:
                txn.execute("UPDATE jobs SET observed_at=?, side_effect_status=? WHERE job_id=?",
                            (updates["observed_at"], updates["side_effect_status"], row["job_id"]))

    # ------------------------------------------------------------------------ settle

    def _settle(self, row: sqlite3.Row, outcome: Mapping[str, Any]) -> None:
        """Turn a terminal observation into a terminal job: publish, record, charge, clean."""
        job_id = row["job_id"]
        job = self._paths(job_id)
        staging = self._staging(row)
        intent = store.loads(row, "submit_intent_json", {}) or {}
        resources = store.loads(row, "resources_json", {}) or {}
        result = outcome.get("result")
        started = bool(outcome.get("started"))
        exit_code = outcome.get("exit_code")

        to_state, failure = self._classify(row, outcome)
        documents_out: dict[str, Any] = {}
        raw_outputs: list[dict[str, Any]] = []
        validated: dict[str, bool] = {}
        adapter_record: dict[str, Any] = {}
        if to_state == SUCCEEDED:
            try:
                bundle = registry.candidate(self.conn, intent["capability_version"])["bundle"]
                raw_outputs, documents_out, validated, adapter_record = self._collect(
                    row, intent, bundle, job, staging)
            except _OutputRefused as exc:
                to_state, failure = FAILED, {"failure_class": exc.failure_class, "observed": exc.observed,
                                             "summary": str(exc), "details": exc.details}
            except GxError as exc:
                to_state, failure = FAILED, {"failure_class": "adapter_or_protocol",
                                             "observed": "output_rejected",
                                             "summary": exc.message, "details": exc.details}
        elif result == runner_mod.RESULT_EXITED and exit_code not in (0, None):
            failure = dict(failure or {})
            failure.update(self._adapter_class(row, intent, job, staging, outcome))

        measured = self._measured(outcome, resources, documents_out)
        gpu_measurement = measured.get("gpu") or {}
        if to_state == SUCCEEDED and gpu_measurement.get("reported_peak_over_limit") is True:
            to_state, failure = FAILED, {
                "failure_class": "resource", "observed": "tool_gpu_memory_exceeded",
                "summary": (f"the tool reported {gpu_measurement['tool_peak_reserved_mib']} MiB reserved, "
                            f"above its {gpu_measurement['limit_mib']} MiB allowance")}
        namespace = self._namespace(row)
        recovery = None
        if to_state != SUCCEEDED and self._side_effect(outcome) != "NONE" and row["staging_ref"]:
            recovery = retained.inventory(storage.store_dir(self.conn), row, intent, outcome)
        with store.transaction(
            self.conn, transaction_owner="JobService", operation="settle", run_id=row["run_id"],
        ) as txn:
            current = txn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if current["status"] in TERMINAL:
                return  # another process settled it between our observation and this lock
            identity: dict[str, Any] = {}
            if not current["runner_pid"] and outcome.get("runner_pid"):
                # The submitter died after the runner started and before it wrote that down.
                identity = {"runner_pid": outcome.get("runner_pid"),
                            "runner_started": outcome.get("runner_started"),
                            "scheduler_ref": f"runner:{outcome.get('runner_pid')}:"
                                             f"{outcome.get('runner_started')}"}
                if not current["submitted_at"]:
                    identity["submitted_at"] = clock.now_iso()
            if current["status"] == SUBMITTING:
                store.transition(
                    txn, machine=MACHINE, subject_id=job_id, to_state=SUBMITTED, actor=self.actor,
                    bump=False, evidence_level="EXECUTION_OBSERVED",
                    reason="found again from the runner's own record", extra_updates=identity,
                )
                identity = {}
            if to_state == CANCELLED and current["status"] not in (CANCEL_REQUESTED, OUTCOME_UNKNOWN):
                # (OUTCOME_UNKNOWN may go to CANCELLED directly; it has no CANCEL_REQUESTED edge.)
                store.transition(txn, machine=MACHINE, subject_id=job_id, to_state=CANCEL_REQUESTED,
                                 actor=self.actor, bump=False, evidence_level="EXECUTION_OBSERVED",
                                 reason="the runner reports a cancel that this row had not recorded")
            if to_state == SUCCEEDED and current["status"] == QUEUED:
                # The row last saw the runner waiting for the GPU lease; since then the job got
                # the lease, ran and ended. The machine has no arrow from QUEUED to SUCCEEDED,
                # and without this step no look could ever settle such a job (it is the
                # ordinary case when looks are far apart). The run the outcome proves is
                # recorded first.
                store.transition(txn, machine=MACHINE, subject_id=job_id, to_state=RUNNING,
                                 actor=self.actor, bump=False, evidence_level="EXECUTION_OBSERVED",
                                 reason="the runner's outcome shows the tool ran after this row last saw it queued")
            published: list[dict[str, Any]] = []
            common = {"run_id": row["run_id"], "path_id": row["path_id"], "task_id": row["task_id"],
                      "job_id": job_id, "actor": self.actor, "origin": "JOB_OUTPUT"}
            if to_state == SUCCEEDED:
                runner_hashes = {f["path"]: f.get("sha256") for f in outcome.get("files", ()) if "path" in f}
                for item in raw_outputs:
                    artifact = self._publish_copy(namespace, item["rel_path"], item["artifact_kind"],
                                                  Path(item["path"]), common, item["name"])
                    published.append({"output_name": item["name"], "artifact_ref": artifact.artifact_ref,
                                      "kind": item["artifact_kind"], "sha256": artifact.sha256,
                                      "bytes": artifact.bytes, "validated": False,
                                      "runner_sha256": runner_hashes.get(item["name"])})
                for name, (kind, document) in documents_out.items():
                    artifact = self.artifacts.publish_bytes(
                        namespace=namespace, rel_path=f"{name}.json", kind=kind,
                        data=ids.canonical_json(document), media_type="application/json",
                        provenance={"output_name": name, "job_id": job_id,
                                    "capability_version": intent.get("capability_version"),
                                    "manifest_sha256": intent.get("manifest_sha256")},
                        **common,
                    )
                    published.append({"output_name": name, "artifact_ref": artifact.artifact_ref,
                                      "kind": kind, "sha256": artifact.sha256, "bytes": artifact.bytes,
                                      "validated": validated.get(name, False)})
            for name, path in (("stdout", job.stdout), ("stderr", job.stderr)):
                data = _bounded_log(path)
                if data is None:
                    continue
                artifact = self.artifacts.publish_bytes(
                    namespace=namespace, rel_path=f"logs/{name}.txt", kind=f"job_{name}", data=data,
                    media_type="text/plain", provenance={"output_name": name, "job_id": job_id},
                    **common,
                )
                published.append({"output_name": name, "artifact_ref": artifact.artifact_ref,
                                  "kind": f"job_{name}", "sha256": artifact.sha256,
                                  "bytes": artifact.bytes, "validated": False})

            record = {
                "result": result, "reason": outcome.get("reason"), "started": started,
                "exit_code": exit_code, "signal": outcome.get("signal"), "failure": failure,
                "outputs": published, "adapter": adapter_record,
                "runner": {key: outcome.get(key) for key in
                           ("runner_pid", "runner_started", "tool_pid", "tool_started",
                            "started_at", "finished_at", "timeout_seconds", "wall_seconds",
                            "group_leftover_ended", "orphans", "files_truncated") if key in outcome},
                "gpu": outcome.get("gpu"),
            }
            if recovery:
                recovery.update(terminal_status=to_state, runner_result=result, exit_code=exit_code)
                receipt = self.artifacts.publish_bytes(
                    namespace=namespace, rel_path="diagnostics/unverified-outputs.json",
                    kind="job_recovery_manifest", data=json.dumps(recovery, sort_keys=True), media_type="application/json",
                    provenance={"job_id": job_id, "validation": "UNVERIFIED"},
                    run_id=row["run_id"], path_id=row["path_id"], task_id=row["task_id"], job_id=job_id,
                    actor=self.actor, origin="BACKEND")
                record["kept"] = {"staging_ref": row["staging_ref"], "recovery_manifest_ref": receipt.artifact_ref}
                if failure:
                    failure["recovery_manifest_ref"] = receipt.artifact_ref
            if outcome.get("readonly_guard") is not None:
                record["readonly_guard"] = outcome["readonly_guard"]
            if outcome.get("network") is not None:
                record["network"] = outcome["network"]     # the route the tool was given, as checked
            never_started = self._side_effect(outcome) == "NONE"
            # ``charged``: whether the job's hold was settled against its budget (True) or
            # returned whole (False). The ledger says the same: the reservation is SETTLED or
            # RELEASED. ``charge`` is the sentence for a reader.
            record["charged"] = not never_started
            record["charge"] = ("nothing: the tool never started, so the hold is returned" if never_started
                                else "what the runner measured")
            now = clock.now_iso()
            self._end(
                txn, current, to_state,
                reason=(failure or {}).get("summary") or f"runner result: {result}",
                extra_updates={"exit_code": exit_code, "outcome_json": record,
                               "measured_json": measured, "finished_at": now, "observed_at": now,
                               "side_effect_status": self._side_effect(outcome), **identity},
                payload={"result": result, "failure_class": (failure or {}).get("failure_class"),
                         "outputs": [item["artifact_ref"] for item in published],
                         "measured": measured},
            )
            if row["reservation_ref"]:
                try:
                    if never_started:
                        # One rule for every job whose tool never started, whoever refused it
                        # (lead's ruling, 2026-11-12T21:24Z): nothing is charged. Charged a
                        # submission, a wait for the device used up the one submission a
                        # path's allotment holds per step; waits are bounded per path instead.
                        _budgets().release(txn, row["reservation_ref"], actor=self.actor,
                                           reason=f"job {job_id} never started ({result}): nothing is charged")
                    else:
                        _budgets().settle(txn, row["reservation_ref"], actor=self.actor,
                                          actual=self._actual(outcome, resources))
                except GxError as exc:  # the ledger refused; the job's own record still says what ran
                    store.append_event(
                        txn, kind="job.budget_settle_failed", actor=self.actor, run_id=row["run_id"],
                        subject_kind="job", subject_id=job_id, evidence_level="EXECUTION_OBSERVED",
                        payload={"error": exc.to_payload()},
                    )
        # Only now, with the terminal state committed and the outputs published by copy.
        if not recovery:
            retained.remove_staging(storage.store_dir(self.conn), row)
        for path in (job.in_dir, job.home_dir, job.tmp_dir, job.work_dir):
            executor.remove_tree(path)

    def _classify(self, row: sqlite3.Row, outcome: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        result = outcome.get("result")
        code = outcome.get("exit_code")
        reason = outcome.get("reason")
        if result == runner_mod.RESULT_EXITED and code == 0:
            return SUCCEEDED, None
        if result == runner_mod.RESULT_CANCELLED:
            return CANCELLED, None
        if result == runner_mod.RESULT_EXITED:
            how = f"exit code {code}" if code is not None else f"signal {outcome.get('signal')}"
            return FAILED, {"failure_class": "adapter_or_protocol",
                            "observed": "nonzero_exit" if code is not None else "killed_by_signal",
                            "summary": f"the tool ended with {how}"}
        if result == runner_mod.RESULT_TIMED_OUT:
            return FAILED, {"failure_class": "job_timeout_or_lost", "observed": "timed_out",
                            "timed_out": True,
                            "summary": f"timed out: the runner ended the tool at its "
                                       f"{outcome.get('timeout_seconds')} s limit"}
        if result == runner_mod.RESULT_GPU_MEMORY:
            return FAILED, {"failure_class": "resource", "observed": "gpu_memory_exceeded",
                            "summary": f"GPU memory limit: {reason}"}
        if result == runner_mod.RESULT_READONLY_CHANGED:
            guard = outcome.get("readonly_guard") or {}
            return FAILED, {
                "failure_class": "adapter_or_protocol", "observed": "readonly_tree_changed",
                "summary": f"the backend changed a tree it may only read: {reason}",
                "details": {"changed": list(guard.get("changed") or []),
                            "changed_count": guard.get("changed_count"),
                            "roots": [{key: root.get(key) for key in ("root", "before", "after", "changed_count",
                                                                       "counts_moved")}
                                      for root in guard.get("roots") or []],
                            "elsewhere": list(guard.get("elsewhere") or []),
                            "elsewhere_note": guard.get("elsewhere_note"),
                            "marker_at": guard.get("marker_at"),
                            "result_before_guard": outcome.get("result_before_guard"),
                            "exit_code": code},
            }
        if result in (runner_mod.RESULT_REFUSED, runner_mod.RESULT_PREFLIGHT_REFUSED,
                      runner_mod.RESULT_LEASE_TIMEOUT):
            because = "network" if outcome.get("refused_by") == "network" else result
            return FAILED, {"failure_class": "environment" if because == "network" else "resource",
                            "observed": "not_started", "not_started": True, "not_started_because": because,
                            "summary": f"not started ({because}): {reason}"}
        if result == runner_mod.RESULT_SPAWN_FAILED:
            return FAILED, {"failure_class": "environment", "observed": "spawn_failed",
                            "not_started": True, "summary": f"the tool could not be started: {reason}"}
        observed = result if result in OBSERVED_FAILURES else "runner_error"
        return FAILED, {"failure_class": "job_timeout_or_lost", "observed": observed,
                        "summary": f"{result}: {reason or outcome.get('error')}"}

    def _job_view_for_adapter(self, row: sqlite3.Row, intent: Mapping[str, Any], job: executor.JobPaths,
                              staging: Path | None, outputs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "job_id": row["job_id"], "capability_version": intent.get("capability_version"),
            "manifest_sha256": intent.get("manifest_sha256"), "out_dir": str(staging),
            "stdout": str(job.stdout), "stderr": str(job.stderr),
            "inputs": dict(intent.get("staged_inputs") or {}),
            "parameters": dict(intent.get("parameters") or {}), "outputs": outputs,
        }

    def _collect(self, row: sqlite3.Row, intent: Mapping[str, Any], bundle: Mapping[str, Any],
                 job: executor.JobPaths, staging: Path | None):
        """The outputs of a job that exited 0: raw files that exist, documents that validate."""
        manifest = bundle["manifest"]
        declared = [dict(output) for output in manifest["outputs"]]
        if staging is None or not staging.is_dir() or os.path.realpath(staging) != str(staging):
            raise _OutputRefused("the job's staging directory is gone; its outputs cannot be published",
                                 failure_class="job_timeout_or_lost", observed="missing_output")
        raw: list[dict[str, Any]] = []
        wanted_documents = [output for output in declared
                            if not manifest_mod.is_raw_output(output) and not manifest_mod.is_dir_output(output)]
        for output in declared:
            if manifest_mod.is_dir_output(output):
                raw.extend(self._directory_output(staging, output))
                continue
            if not manifest_mod.is_raw_output(output):
                continue
            path = staging / output["name"]
            if path.is_symlink() or not path.is_file():
                if output["required"]:
                    raise _OutputRefused(
                        f"required output file {output['name']!r} was not written; a job that "
                        "exits 0 without its output has not succeeded",
                        failure_class="adapter_or_protocol", observed="missing_output",
                        details={"output": output["name"]})
                continue
            kind = output["kind"].split(":", 1)[1] if ":" in output["kind"] else "job_output_file"
            raw.append({"name": output["name"], "path": str(path), "rel_path": f"files/{output['name']}",
                        "artifact_kind": kind})

        documents_out: dict[str, Any] = {}
        validated: dict[str, bool] = {}
        adapter_record: dict[str, Any] = {}
        if wanted_documents:
            parser = bundle.get("parser")
            if parser is None:
                raise _OutputRefused(
                    f"the manifest declares document output(s) "
                    f"{[output['name'] for output in wanted_documents]} and binds no adapter module "
                    "to produce them", failure_class="adapter_or_protocol")
            answer = adapter_mod.call(
                parser, "parse", [self._job_view_for_adapter(row, intent, job, staging, declared)],
                work_dir=job.work_dir,
                timeout=float(config.engineering_budget()["ordinary_test_timeout_seconds"]),
            )
            adapter_record = {"path": parser["path"], "sha256": parser["sha256"],
                              "parse": answer.describe(), "platform": intent.get("platform")}
            if not answer.ok:
                raise _OutputRefused(
                    f"the adapter rejected the tool's output: {answer.error_type}: {answer.error}",
                    failure_class="adapter_or_protocol", details=answer.describe())
            parsed = answer.result if isinstance(answer.result, dict) else {}
            for output in wanted_documents:
                name = output["name"]
                if name not in parsed:
                    if output["required"]:
                        raise _OutputRefused(
                            f"the adapter returned no document for required output {name!r}",
                            failure_class="adapter_or_protocol", details={"output": name})
                    continue
                if not isinstance(parsed[name], dict):
                    raise _OutputRefused(
                        f"output {name!r} must parse as a JSON object",
                        failure_class="adapter_or_protocol", details={"output": name})
                documents_out[name] = (output["kind"], documents.stamp(
                    parsed[name], job_id=row["job_id"],
                    capability_version=str(intent.get("capability_version")),
                    manifest_sha256=str(intent.get("manifest_sha256"))))
                validated[name] = False  # The core records parser output without claiming a domain validation.
        return raw, documents_out, validated, adapter_record

    @staticmethod
    def _directory_output(staging: Path, output: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every regular file under ``{out.dir}/<name>``, each to be published as
        ``<name>/<path inside it>``. A link is never followed (its target could be anything on
        this machine); more than the bounds is a refusal, not a silent cut."""
        base = staging / output["name"]
        kind = output["kind"].split(":", 1)[1] if ":" in output["kind"] else "job_output_file"
        if base.is_symlink() or not base.is_dir():
            if output["required"]:
                raise _OutputRefused(f"required output directory {output['name']!r} was not written",
                                     failure_class="adapter_or_protocol", observed="missing_output",
                                     details={"output": output["name"]})
            return []
        found: list[dict[str, Any]] = []
        links: list[str] = []
        total = 0
        for directory, subdirs, names in os.walk(base, followlinks=False):
            subdirs.sort()
            for name in sorted(names):
                path = Path(directory) / name
                rel = str(path.relative_to(base))
                if path.is_symlink() or not path.is_file():
                    links.append(rel)
                    continue
                total += path.stat().st_size
                found.append({"name": f"{output['name']}/{rel}", "path": str(path),
                              "rel_path": f"files/{output['name']}/{rel}", "artifact_kind": kind})
        if links:
            raise _OutputRefused(
                f"output directory {output['name']!r} holds {len(links)} entr(ies) that are not regular "
                f"files ({links[:5]}); a link is never published", failure_class="adapter_or_protocol",
                details={"output": output["name"], "not_regular": links[:50]})
        if len(found) > DIR_OUTPUT_MAX_FILES or total > DIR_OUTPUT_MAX_BYTES:
            raise _OutputRefused(
                f"output directory {output['name']!r} holds {len(found)} file(s), {total} bytes; at most "
                f"{DIR_OUTPUT_MAX_FILES} files and {DIR_OUTPUT_MAX_BYTES} bytes are published",
                failure_class="resource", details={"output": output["name"], "files": len(found), "bytes": total})
        return found

    def _adapter_class(self, row: sqlite3.Row, intent: Mapping[str, Any], job: executor.JobPaths,
                       staging: Path | None, outcome: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the adapter what a non-zero exit means, if it has an opinion."""
        try:
            bundle = registry.candidate(self.conn, intent["capability_version"])["bundle"]
            parser = bundle.get("parser")
            if parser is None:
                return {}
            view = self._job_view_for_adapter(row, intent, job, staging, [])
            view["exit_code"] = outcome.get("exit_code")
            answer = adapter_mod.call(parser, "classify_failure", [view], work_dir=job.work_dir)
        except (GxError, OSError, ValueError):
            return {}
        if answer.ok and isinstance(answer.result, str) and answer.result in _FAILURE_CLASSES:
            return {"failure_class": answer.result, "classified_by": "adapter"}
        return {}

    def _publish_copy(self, namespace: str, rel_path: str, kind: str, source: Path,
                      common: Mapping[str, Any], output_name: str):
        """Publish a copy of a staged file, so the staging directory stays whole until the
        terminal state has committed."""
        scratch = self.artifacts.staging_dir(namespace)
        try:
            copy = scratch / source.name
            shutil.copyfile(source, copy)
            return self.artifacts.publish(
                namespace=namespace, rel_path=rel_path, kind=kind, staged_path=copy,
                provenance={"output_name": output_name, "job_id": common["job_id"]}, **common,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _namespace(self, row: sqlite3.Row) -> str:
        if row["path_id"]:
            return f"{row['run_id']}/{row['path_id']}/jobs/{row['job_id']}"
        return f"{row['run_id']}/jobs/{row['job_id']}"

    @staticmethod
    def _measured(outcome: Mapping[str, Any], resources: Mapping[str, Any],
                  documents_out: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cpu = None
        if outcome.get("cpu_user_seconds") is not None:
            cpu = round(float(outcome["cpu_user_seconds"]) + float(outcome.get("cpu_system_seconds") or 0), 3)
        peak = round(outcome["max_rss_kib"] / 1024, 1) if outcome.get("max_rss_kib") is not None else None
        cap = resources.get("memory_mib")
        measured: dict[str, Any] = {
            "result": outcome.get("result"), "started": bool(outcome.get("started")),
            "wall_seconds": outcome.get("wall_seconds"), "cpu_seconds": cpu,
            "max_rss_mib": peak,
            "limit_wall_seconds": resources.get("wall_seconds"),
            # B5 (the lead's ruling, 2026-11-13T01:15Z): the measured peak memory is recorded and
            # checked against the job's own cap; no budget profile has a memory line, so it is
            # charged to none. The reading is the largest resident set among the tool's
            # processes (getrusage of the runner's children), not their sum.
            "limit_memory_mib": cap,
            "memory_within_limit": None if cap is None or peak is None else peak <= float(cap),
        }
        if cap is None:
            measured["memory_check"] = "no memory_mib is declared for this job: nothing to check against"
        elif peak is None:
            measured["memory_check"] = "no peak memory was measured: the cap could not be checked"
        if outcome.get("estimated"):
            measured["estimated"] = ("the runner was lost: wall_seconds is the span from the tool's start to the "
                                     "runner's last heartbeat, not a measurement")
        gpu = outcome.get("gpu")
        if gpu:
            # Existing adapters can report their own allocator peak in a parsed Job output.
            # Each such peak is a lower bound on total Job VRAM; device readings cannot fill a gap.
            reported_peaks = []
            for _, document in (documents_out or {}).values():
                report = document.get("gpu_self_reported")
                if isinstance(report, dict):
                    peak_mib = report.get("max_memory_reserved_mib")
                    numeric = type(peak_mib) is int or (type(peak_mib) is float and math.isfinite(peak_mib))
                    if numeric and peak_mib >= 0:
                        reported_peaks.append(peak_mib)
            tool_peak = max(reported_peaks, default=None)
            limit = (resources.get("gpu") or {}).get("memory_mib")
            measured["gpu"] = {
                "device": gpu.get("device"), "limit_mib": limit, "tool_peak_reserved_mib": tool_peak,
                "reported_peak_over_limit": tool_peak > limit if tool_peak is not None and limit is not None else None,
                "sample_problems": gpu.get("sample_problems"),
                "last_sample_problem": gpu.get("last_sample_problem"),
                "baseline_used_mib": gpu.get("baseline_used_mib"),
                "peak_used_mib": gpu.get("peak_used_mib"), "peak_delta_mib": gpu.get("peak_delta_mib"),
                "samples": gpu.get("samples"), "queue_seconds": gpu.get("queue_seconds"),
                "free_mib_at_launch": (gpu.get("preflight") or {}).get("memory_free_mib"),
                "lease_acquired_at": gpu.get("lease_acquired_at"),
                "launch_reading_at": (gpu.get("preflight") or {}).get("observed_at"),
                "after": {key: (gpu.get("after") or {}).get(key)
                          for key in ("observed_at", "memory_used_mib", "memory_free_mib", "ok")},
                "method": ("baseline, peak, delta and after describe the shared device; "
                           "tool_peak_reserved_mib is the largest allocator peak reported by this tool"),
            }
        return measured

    @staticmethod
    def _actual(outcome: Mapping[str, Any], resources: Mapping[str, Any]) -> dict[str, int]:
        """What a job that started, or may have, is charged: one submission and what was
        measured. A job known never to have started is not settled with this at all: its hold
        is released (see ``_settle``)."""
        started = bool(outcome.get("started"))
        cpu = float(outcome.get("cpu_user_seconds") or 0) + float(outcome.get("cpu_system_seconds") or 0)
        wall = float(outcome.get("wall_seconds") or 0)
        if started and outcome.get("estimated"):
            # A lost runner measured nothing: the span the tool could have run, on every
            # thread it was given, never more than the hold (audit J5; contract r5
            # job.rules.accounting). The span is cut at the job's time limit, so no line is
            # charged more than admission held, a GPU job's minutes included: it is read from a
            # start time recorded to the whole second and a file's modification time, so it can
            # come out past the limit although the runner beats only within it.
            limit = float(resources.get("wall_seconds") or 0)
            wall = limit if outcome.get("wall_seconds") is None else min(wall, limit)
            cpu = wall * int(resources.get("cpu_threads") or 1)
        actual = {"job_submissions": 1, "cpu_seconds": math.ceil(cpu) if started else 0}
        if resources.get("gpu") is not None:
            actual["gpu_jobs"] = 1 if started else 0
            actual["gpu_minutes"] = max(1, math.ceil(wall / 60)) if started else 0
        return actual


class _OutputRefused(Exception):
    def __init__(self, message: str, *, failure_class: str, observed: str = "output_rejected",
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.observed = observed
        self.details = dict(details or {})


def _bounded_log(path: Path) -> bytes | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    with open(path, "rb") as handle:
        if size <= MAX_LOG_BYTES:
            return handle.read()
        head = handle.read(MAX_LOG_BYTES // 2)
        handle.seek(size - MAX_LOG_BYTES // 2)
        tail = handle.read()
    note = f"\n\n[... {size - MAX_LOG_BYTES} bytes of {size} omitted by the job service ...]\n\n"
    return head + note.encode("utf-8") + tail


def _budgets():
    from .. import budgets

    return budgets


def _approvals():
    from ..governance import approvals

    return approvals


def jobs_for_path(conn: sqlite3.Connection, path_id: str) -> list[dict[str, Any]]:
    """Every job the backend recorded for a path, oldest first, as it is now."""
    service = JobService(conn)
    rows = conn.execute("SELECT * FROM jobs WHERE path_id=? ORDER BY created_at, job_id", (path_id,)).fetchall()
    out = []
    for row in rows:
        observation = service._reconcile_one(row)
        current = service._row(row["job_id"])
        view = service.view(current, observation=observation)
        intent = store.loads(current, "submit_intent_json", {}) or {}
        view.update(manifest_sha256=intent.get("manifest_sha256"),
                    parameters=intent.get("parameters") or {}, tighten=intent.get("tighten") or {},
                    input_manifest_ref=current["input_manifest_ref"], purpose=intent.get("purpose"))
        out.append(view)
    return out


__all__ = ["CANCELLED", "CANCEL_REQUESTED", "CREATED", "FAILED", "GPU_QUEUE_MAX", "IN_FLIGHT", "JobService",
           "MAX_WAIT_SECONDS", "OBSERVED_FAILURES", "OUTCOME_UNKNOWN", "PURPOSES", "PURPOSE_EVALUATION", "PURPOSE_PATH",
           "PURPOSE_PROBE", "PURPOSE_REFERENCE", "PURPOSE_WORKFLOW", "QUEUED", "RUNNING",
           "SUBMITTED", "SUBMITTING",
           "START_DEADLINE_SECONDS", "SUCCEEDED", "TERMINAL", "TERMINAL_TOPIC", "gpu_queue_wait_seconds",
           "intent_subject", "job_deadline_seconds", "jobs_for_path", "limit_problems",
           "start_deadline_seconds", "terminal_trigger", "terminal_trigger_key"]
