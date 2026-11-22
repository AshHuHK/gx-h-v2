"""Row creators for the core entities.

Kept in the kernel because every service needs them and because the initial state of a
row must come from ``contracts/states.json`` rather than from a literal typed twice.

Nothing here decides anything. A creator writes the row it is asked for and the event
that says so; whether the row may exist -- the workflow is ACTIVE, the role is one the
contract names, the bundle kind fits the task kind -- is the calling service's check.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from .. import contracts
from ..domain import clock, ids, machines
from ..errors import SchemaInvalid
from . import store


def create_run(
    conn: sqlite3.Connection,
    *,
    objective: str,
    project_ref: str,
    actor: str,
    budget_json: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    budget_ref: str | None = None,
    workflow_id: str | None = None,
    workflow_version: str | None = None,
    pins: Mapping[str, Any] | None = None,
    agent_policy_ref: str | None = None,
    agent_policy: Mapping[str, Any] | None = None,
    inputs: Mapping[str, Any] | None = None,
    host: Mapping[str, Any] | None = None,
) -> str:
    """Create a run in its initial state.

    ``pins`` carries what the run started under -- ``contract_version``,
    ``contract_sha256``, ``workflow_sha256``, ``agent_policy_sha256`` -- and resume
    compares each with what is installed. A run created without pins (a unit test of a
    single service) simply has none to compare.
    """
    run_id = ids.new_id("run")
    now = clock.now_iso()
    pins = dict(pins or {})
    conn.execute(
        "INSERT INTO runs(run_id, project_ref, objective, status, phase, revision, workflow_id,"
        " workflow_version, budget_ref, budget_json, constraints_json, agent_policy_ref,"
        " agent_policy_json, contract_version, contract_sha256, workflow_sha256,"
        " agent_policy_sha256, inputs_json, host_json, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, project_ref, objective, machines.initial_state("run"), "P0", 0,
            workflow_id, workflow_version, budget_ref, store._j(budget_json or {}),
            store._j(constraints or {}), agent_policy_ref, store._j(dict(agent_policy or {})),
            pins.get("contract_version"), pins.get("contract_sha256"),
            pins.get("workflow_sha256"), pins.get("agent_policy_sha256"),
            store._j(dict(inputs or {})), store._j(dict(host or {})), now, now,
        ),
    )
    store.append_event(
        conn, kind="run.created", actor=actor, run_id=run_id, subject_kind="run",
        subject_id=run_id, new_state=machines.initial_state("run"), new_revision=0,
        evidence_level="EXECUTION_OBSERVED",
        payload={"objective": objective, "project_ref": project_ref,
                 "workflow": {"id": workflow_id, "version": workflow_version}, "pins": pins},
    )
    return run_id


def create_path(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    spec: dict[str, Any],
    actor: str,
    parent_path_id: str | None = None,
    version: int = 1,
    budget_json: dict[str, Any] | None = None,
) -> str:
    for required in ("hypothesis", "method_family"):
        if not spec.get(required):
            raise SchemaInvalid(
                f"PathSpec is missing {required!r}",                 details={"missing": required},
            )
    path_id = ids.new_id("path")
    fingerprint = ids.path_fingerprint(spec)
    duplicate = conn.execute(
        "SELECT path_id, status FROM paths WHERE run_id=? AND fingerprint=?",
        (run_id, fingerprint),
    ).fetchone()
    if duplicate is not None and parent_path_id is None:
        raise SchemaInvalid(
            "a path with this fingerprint already exists in the run; re-running it without "
            "new evidence spends budget to learn nothing",
                        details={"existing_path_id": duplicate["path_id"], "fingerprint": fingerprint},
        )
    now = clock.now_iso()
    conn.execute(
        "INSERT INTO paths(path_id, run_id, version, parent_path_id, status, fingerprint,"
        " hypothesis, method_family, spec_json, eval_spec_ref, budget_json, artifact_namespace,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            path_id, run_id, version, parent_path_id, machines.initial_state("path"),
            fingerprint, spec["hypothesis"], spec["method_family"], store._j(spec),
            spec.get("eval_spec_ref"), store._j(budget_json or {}),
            f"{run_id}/{path_id}", now, now,
        ),
    )
    store.append_event(
        conn, kind="path.created", actor=actor, run_id=run_id, subject_kind="path",
        subject_id=path_id, new_state=machines.initial_state("path"),
        evidence_level="EXECUTION_OBSERVED",
        payload={"fingerprint": fingerprint, "parent_path_id": parent_path_id,
                 "method_family": spec["method_family"]},
    )
    return path_id


def create_task(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    role: str,
    actor: str,
    path_id: str | None = None,
    depends_on: Sequence[str] | None = None,
    required_artifacts: Sequence[str] | None = None,
    task_input: Mapping[str, Any] | None = None,
    budget_ref: str | None = None,
    task_class: str | None = None,
    bundle_kind: str | None = None,
    acceptance: Mapping[str, Any] | None = None,
    max_attempts: int = 3,
    parallel_group: str | None = None,
    subject: Mapping[str, Any] | None = None,
    independent_of_task_id: str | None = None,
    required_model: str | None = None,
    required_effort: Sequence[str] | None = None,
) -> str:
    """Create a task row in its initial state, with its dependency edges.

    ``subject`` is ``{"kind", "id", "sha256"}`` for a task about one thing at one
    content hash (a review, a scenario run). ``required_model`` and ``required_effort``
    are what the run's agent policy asks of whoever does the task; they are stamped here
    so that a later change of the policy file cannot change what an existing task was
    created to require.
    """
    task_id = ids.new_id("task")
    now = clock.now_iso()
    subject = dict(subject or {})
    conn.execute(
        "INSERT INTO tasks(task_id, run_id, path_id, kind, role, task_class, status, attempt,"
        " required_artifacts_json, bundle_kind, acceptance_json, input_json, max_attempts,"
        " parallel_group, subject_kind, subject_id, subject_sha256, independent_of_task_id,"
        " required_model, required_effort_json, budget_ref, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, run_id, path_id, kind, role, task_class, machines.initial_state("task"), 0,
            store._j(list(required_artifacts or [])), bundle_kind, store._j(dict(acceptance or {})),
            store._j(dict(task_input or {})), int(max_attempts), parallel_group,
            subject.get("kind"), subject.get("id"), subject.get("sha256"),
            independent_of_task_id, required_model, store._j(list(required_effort or [])),
            budget_ref, now, now,
        ),
    )
    for dep in depends_on or []:
        conn.execute(
            "INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES(?,?)", (task_id, dep)
        )
    store.append_event(
        conn, kind="task.created", actor=actor, run_id=run_id, subject_kind="task",
        subject_id=task_id, new_state=machines.initial_state("task"),
        evidence_level="EXECUTION_OBSERVED",
        payload={"kind": kind, "role": role, "bundle_kind": bundle_kind,
                 "depends_on": list(depends_on or []), "parallel_group": parallel_group,
                 "independent_of_task_id": independent_of_task_id},
    )
    return task_id


def dependencies_satisfied(conn: sqlite3.Connection, task_id: str) -> tuple[bool, list[str]]:
    """``(ready, blocking_task_ids)`` — a dependency counts only when ACCEPTED."""
    rows = conn.execute(
        "SELECT d.depends_on AS dep, t.status AS status FROM task_deps d"
        " LEFT JOIN tasks t ON t.task_id = d.depends_on WHERE d.task_id=?",
        (task_id,),
    ).fetchall()
    blocking = [r["dep"] for r in rows if r["status"] != "ACCEPTED"]
    return (not blocking), blocking


def record_worker_observation(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    host: str,
    state: str,
    evidence_level: str,
    lease_id: str | None = None,
    observed_worker_ref: str | None = None,
    delegation_mode: str | None = None,
    payload: dict[str, Any] | None = None,
    agent_ref: str | None = None,
    host_session_ref: str | None = None,
    observation_source: str = "client_report",
    reported_model: str | None = None,
    reported_effort: str | None = None,
    observed_model: str | None = None,
    observed_effort: str | None = None,
    model_evidence_level: str | None = None,
    policy_verdict: str | None = None,
    host_version: str | None = None,
) -> str:
    """Write what a worker reported about itself: a client report, and nothing more.

    ``reported_*`` is what the worker said; ``observed_*`` and ``observed_worker_ref`` are
    what the host's own records showed, and those rows are the host stream's to write --
    its observer (``gx_core.hostobs.observation.write_observation``), fed by
    ``gx_host_event`` and the transcript reader -- never this function. So a row written
    here has ``observation_source='client_report'``, no observed reference, no observed
    model or effort, no ``observed_*`` state and no ``EXECUTION_OBSERVED`` level: nothing
    that could be read as an observation. (v2's first cut let any caller write a
    host-sourced row through here, and governance had to carry a fallback for rows that
    claimed a host source without the reference a host record always has.)
    """
    contracts.require_label("evidence_level", evidence_level)
    contracts.require_label("observation_source", observation_source)
    if observation_source != "client_report" or observed_worker_ref is not None:
        raise SchemaInvalid(
            "this function writes what a worker reported about itself; a row from a host's "
            "record (an observation source other than client_report, or an observed worker "
            "reference) is written by the host stream's observer, through gx_host_event or "
            "gx_core.hostobs.observation.write_observation",
            details={"observation_source": observation_source,
                     "observed_worker_ref": observed_worker_ref,
                     "writer": "gx_core.hostobs.observation.write_observation"},
        )
    if delegation_mode is not None:
        contracts.require_label("delegation_mode", delegation_mode)
    if policy_verdict is not None:
        contracts.require_label("model_policy_verdict", policy_verdict)
    if state not in machines.all_states("worker_observation"):
        raise SchemaInvalid(
            f"{state!r} is not a worker observation state",             details={"allowed": machines.all_states("worker_observation")},
        )
    if observation_source == "client_report" and (
        state.startswith("observed_") or observed_model is not None or observed_effort is not None
        or evidence_level == "EXECUTION_OBSERVED"
    ):
        raise SchemaInvalid(
            "a client report cannot write an observed state or an observed model: an "
            "observation comes from a host adapter's record or from a process the backend "
            "started, with that source named",
            details={"state": state, "observation_source": observation_source},
        )
    observation_id = ids.new_id("worker")
    conn.execute(
        "INSERT INTO worker_observations(worker_observation_id, task_id, lease_id, host,"
        " host_session_ref, observed_worker_ref, agent_ref, state, evidence_level,"
        " observation_source, delegation_mode, reported_model, reported_effort, observed_model,"
        " observed_effort, model_evidence_level, host_version, policy_verdict, observed_at,"
        " payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id, task_id, lease_id, host, host_session_ref, observed_worker_ref,
            agent_ref, state, evidence_level, observation_source, delegation_mode,
            reported_model, reported_effort, observed_model, observed_effort,
            model_evidence_level, host_version, policy_verdict, clock.now_iso(),
            store._j(payload or {}),
        ),
    )
    return observation_id
