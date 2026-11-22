"""Cross-package services used by the workflow, job and team owners.

The CLI and MCP doctor resolve the same inventory that runtime calls use.
Missing packages are reported by name. Required calls fail if their owner is
unavailable. Optional telemetry and brief compilation report their absence.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sqlite3
from typing import Any, Callable, Mapping

from .. import config
from ..context import Ctx
from ..errors import ArtifactNotFound, CapabilityNotApproved, GateNotMet, GxError, ImplementationGap
from ..persistence import store
from ..registry import Subject

#: seam name -> why the owner's function was not used, for this process.
ON_FALLBACK: dict[str, str] = {}

#: seam name -> ``module.attr`` of the owner's function, for ``gx doctor``.
OWNERS: dict[str, str] = {
    "agent_policy": "gx_core.hostobs.policy.agent_policy",
    "task_requirement": "gx_core.hostobs.policy.requirement_of_task",
    "bind_worker": "gx_core.hostobs.policy.bind_worker",
    "policy_verdict": "gx_core.hostobs.policy.verdict_for",
    "host_record": "gx_core.hostobs.detect.host_record",
    "host_detected": "gx_core.hostobs.detect.detected",
    "compile_brief": "gx_core.teams.briefs.compile_brief",
    "request_approval": "gx_core.governance.approvals.request",
    "approval_view": "gx_core.governance.approvals.get",
    "screen_objective": "gx_core.planning.objective.screen",
    "workflow_admission": "gx_core.governance.assets.require_active",
    "budget_profile": "gx_core.budgets.profile",
    "create_root_budget": "gx_core.budgets.create_root",
    "budget_state": "gx_core.budgets.state",
    "charge": "gx_core.budgets.spend",
    "release_chain": "gx_core.budgets.release_chain",
    "release_reservation": "gx_core.budgets.release",
    "memory_search": "gx_core.knowledge.memory.MemoryService",
    "run_scopes": "gx_core.knowledge.scopes.for_run",
    "cancel_run_jobs": "gx_core.jobs.service.JobService",
    "shipped_strategies": "gx_core.knowledge.strategy.shipped_report",
    "role_intent_pins": "gx_core.hostobs.roles.pins",
    "role_intents": "gx_core.hostobs.roles.shipped",
    "reconcile_jobs": "gx_core.jobs.service.JobService",
    "hold_run_jobs": "gx_core.jobs.service.JobService",
    "exec_need_cleared": "gx_core.jobs.needs.cleared",
    "stopped_holders": "gx_core.hostobs.policy.stopped_holders",
    "holder_stopped": "gx_core.hostobs.policy.holder_stopped",
    "record_verdict_for_lease": "gx_core.hostobs.policy.record_verdict_for_lease",
    "session_capability": "gx_core.hostobs.events.session_capability",
    "reviews_for": "gx_core.governance.reviews.reviews_for",
    "human_task_review": "gx_core.governance.reviews.human_task_review",
    "reserve_task_chain": "gx_core.budgets.reserve_chain",
    "advance_asset": "gx_core.governance.assets.advance",
    "get_asset": "gx_core.governance.assets.get",
    "asset_review_for": "gx_core.governance.reviews.request_review_for",
}


# --------------------------------------------------------------------------- resolution

def _package(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2])


_CANNOT_IMPORT_NAME = re.compile(r"cannot import name '([^']+)' from '([^']+)'")


def _is_owners_absence(exc: ImportError, module: str) -> bool:
    """Whether an import failed because a module of the owner's own package is not there.

    Two shapes: ``import a.b.c`` for a missing module is ``ModuleNotFoundError`` naming
    it; ``from . import c`` for a missing sibling is a plain ``ImportError`` naming the
    package. Anything else -- a module that exists and breaks, a name missing from a
    module that is there -- is the owner's defect and is not an absence.
    """
    package = _package(module)
    missing = exc.name or ""
    if isinstance(exc, ModuleNotFoundError):
        return bool(missing) and (
            missing == module or module.startswith(missing + ".") or missing.startswith(package + ".")
        )
    match = _CANNOT_IMPORT_NAME.match(str(exc))
    if match and match.group(2) == package:
        try:
            return importlib.util.find_spec(f"{package}.{match.group(1)}") is None
        except (ImportError, ValueError):
            return False
    return False


def _what_is_absent(exc: ImportError) -> str:
    match = _CANNOT_IMPORT_NAME.match(str(exc))
    if match and not isinstance(exc, ModuleNotFoundError):
        return f"{match.group(2)}.{match.group(1)}"
    return str(exc.name)


def _resolve(seam: str) -> Callable[..., Any] | None:
    """The owner's function, or None (with the reason recorded) when it is not installed."""
    module, _, attr = OWNERS[seam].rpartition(".")
    try:
        loaded = importlib.import_module(module)
    except ImportError as exc:
        if _is_owners_absence(exc, module):
            ON_FALLBACK[seam] = f"{_what_is_absent(exc)} is not installed"
            return None
        raise
    target = getattr(loaded, attr, None)
    if target is None:
        ON_FALLBACK[seam] = f"{module} defines no {attr}"
        return None
    ON_FALLBACK.pop(seam, None)
    return target


def _require(seam: str) -> Callable[..., Any]:
    target = _resolve(seam)
    if target is None:
        raise ImplementationGap(
            f"{OWNERS[seam]} is not installed ({ON_FALLBACK[seam]}); the core does not "
            "stand in for it",
            details={"seam": seam, "owner": OWNERS[seam], "completion_status": "DESIGN_ONLY"},
        )
    return target


def _call(seam: str, fn: Callable[..., Any], conn: sqlite3.Connection | None, *args: Any,
          **kwargs: Any) -> tuple[bool, Any]:
    """Call the owner. ``(False, None)`` when a module of the owner's package is absent.

    With a connection the call runs in its own savepoint, so an owner that wrote a row
    and then found one of its own modules missing leaves nothing behind for a stand-in
    to build on.
    """
    module = OWNERS[seam].rpartition(".")[0]
    try:
        if conn is not None:
            with store.transaction(conn):
                return True, fn(*args, **kwargs)
        return True, fn(*args, **kwargs)
    except ImportError as exc:
        if _is_owners_absence(exc, module):
            ON_FALLBACK[seam] = f"{_what_is_absent(exc)} is not installed (needed by {OWNERS[seam]})"
            return False, None
        raise


def _must(seam: str, conn: sqlite3.Connection | None, *args: Any, **kwargs: Any) -> Any:
    done, value = _call(seam, _require(seam), conn, *args, **kwargs)
    if not done:
        raise ImplementationGap(
            f"{OWNERS[seam]} cannot run: {ON_FALLBACK[seam]}",
            details={"seam": seam, "owner": OWNERS[seam], "completion_status": "DESIGN_ONLY"},
        )
    return value


# ------------------------------------------------------------------------ agent policy

def agent_policy() -> tuple[dict[str, Any], str]:
    """The agent policy in force and the sha256 of its bytes (owner: host)."""
    policy, sha256 = _must("agent_policy", None)
    return dict(policy), str(sha256)


def agent_policy_for_run(run: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Current policy in the family the run pinned, never the current caller's Host."""
    from ..hostobs import host_policy
    return host_policy.current_for_run(run) if host_policy.scope_of_run(run) else agent_policy()


def task_requirement(pinned_policy: Mapping[str, Any] | None, *, role: str,
                     task_class: str | None = None) -> dict[str, Any]:
    """What the policy a run pinned asks of whoever does a task of ``role`` (owner: host).

    Read from the policy the *run* pinned, not from the file as it is now: a policy
    edited after a run started must not change what that run's tasks require. A run that
    pinned no policy requires nothing, and its tasks say so with null columns.
    """
    if not pinned_policy:
        return {"task_class": task_class, "model": None, "allowed_efforts": [],
                "default_effort": None, "min_effort": None}
    return dict(_must("task_requirement", None, {"role": role, "task_class": task_class},
                      pinned_policy))


def host_record(declared: Mapping[str, Any] | None) -> dict[str, Any]:
    """The host a run is judged by, and how that was settled (owner: host).

    What the caller declared wins. With nothing declared it is what the launch environment
    shows -- Claude Code's own variables make the run ``claude_code``, which is judged
    strictly -- and with neither it is the most lenient kind, recorded as a default. A
    declaration more lenient than the environment shows is kept and flagged, never
    overridden. An unknown kind is the owner's ``SCHEMA_INVALID``.
    """
    return dict(_must("host_record", None, dict(declared or {})))


def launch_session_ref(host: str) -> str | None:
    """The host session this process was launched from, when the launch environment shows
    one and it is a session of ``host`` (owner: host). None otherwise: never a guess."""
    seen = _must("host_detected", None)
    if isinstance(seen, Mapping) and seen.get("kind") == host:
        ref = seen.get("session_ref")
        return ref if isinstance(ref, str) and ref else None
    return None


def launch_session() -> str | None:
    """The host session this process was launched from, whatever the host, when the launch
    environment shows one (owner: host). None otherwise, and None when the host stream is
    not installed: a record that names no session is kept, a guessed one never written."""
    try:
        seen = _must("host_detected", None)
    except ImplementationGap:
        return None
    ref = seen.get("session_ref") if isinstance(seen, Mapping) else None
    return ref if isinstance(ref, str) and ref else None


def bind_worker(ctx: Ctx, *, task_id: str, lease_id: str, host: str,
                host_session_ref: str | None, agent_ref: str | None, model: str | None,
                effort: str | None, delegation_mode: str | None, evidence_level: str,
                _codex_prepared: Any = None) -> dict[str, Any]:
    """Record what a worker says about itself and judge it (owner: host).

    The answer carries ``worker_observation_id``, ``observed``, ``policy_verdict`` and
    ``action``; ``refuse`` is the caller's ``MODEL_POLICY_VIOLATION``.
    """
    return dict(_must(
        "bind_worker", ctx.conn, ctx.conn, task_id=task_id, lease_id=lease_id, host=host,
        host_session_ref=host_session_ref, agent_ref=agent_ref, model=model, effort=effort,
        delegation_mode=delegation_mode, evidence_level=evidence_level, actor=ctx.actor,
        **({"_codex_prepared": _codex_prepared} if _codex_prepared is not None else {}),
    ))


def policy_verdict(conn: sqlite3.Connection, *, task_id: str, lease_id: str, _codex_prepared: Any = None) -> dict[str, Any]:
    """The verdict a submission under this lease is accepted or held on (owner: host)."""
    return dict(_must("policy_verdict", conn, conn, task_id=task_id, lease_id=lease_id,
                      **({"_codex_prepared": _codex_prepared} if _codex_prepared is not None else {})))


# ------------------------------------------------------------------------------ briefs

def compile_brief(ctx: Ctx, task: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The brief for a task's coming attempt (owner: host).

    Stand-in: no brief. The task says so (``brief_ref`` is null), the dispatch plan warns,
    and the task object ``gx_task_claim`` returns is the whole instruction. Nothing is
    written that could be mistaken for a brief.
    """
    owner = _resolve("compile_brief")
    if owner is None:
        return None, None
    done, value = _call("compile_brief", owner, ctx.conn, ctx, task)
    if not done:
        return None, None
    ref, sha256 = value
    return ref, sha256


# --------------------------------------------------------------------------- approvals

def request_approval(ctx: Ctx, *, action_kind: str, subject: Subject, run_id: str | None,
                     request: Mapping[str, Any]) -> dict[str, Any]:
    """Ask for an approval bound to one subject at one sha256 (owner: govern).

    The owner applies the user's standing policy at once; the answer's ``status`` is
    ``GRANTED`` or ``PENDING``.
    """
    return dict(_must("request_approval", ctx.conn, ctx, action_kind=action_kind,
                      subject=subject, run_id=run_id, request=dict(request)))


def approval_status(conn: sqlite3.Connection, approval_id: str) -> str | None:
    """The status of one approval as the owner reads it (an overdue one is EXPIRED)."""
    owner = _resolve("approval_view")
    if owner is not None:
        try:
            done, view = _call("approval_view", owner, conn, conn, approval_id)
        except ArtifactNotFound:
            return None
        if done:
            return view.get("status") if isinstance(view, Mapping) else None
    row = conn.execute("SELECT status FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    return row["status"] if row is not None else None


# --------------------------------------------------------------------------- objective

def screen_objective(objective: Any) -> dict[str, Any]:
    """Legality, privacy and biosafety applicability screen (owner: eval).

    Stand-in: ``UNSCREENED``. The objective stays DRAFT; nothing marks it screened,
    confirmed or clear on behalf of a module that did not look at it. What a run does
    with an unscreened objective is decided by its workflow, not here.
    """
    owner = _resolve("screen_objective")
    if owner is None:
        return {"result": "UNSCREENED", "reason": ON_FALLBACK["screen_objective"], "standin": True}
    done, value = _call("screen_objective", owner, None, objective)
    if not done:
        return {"result": "UNSCREENED", "reason": ON_FALLBACK["screen_objective"], "standin": True}
    return dict(value)


# ---------------------------------------------------------------------------- workflow

def shipped_pins(kind: str = "workflows") -> dict[str, str]:
    """``name -> sha256`` from ``config/shipped.json`` (lead-owned). Read on every call."""
    path = config.CONFIG_DIR / "shipped.json"
    try:
        table = json.loads(path.read_text(encoding="utf-8")).get(kind) or {}
    except (OSError, ValueError):
        return {}
    return {str(name): str(value) for name, value in table.items() if isinstance(value, str) and value}


def workflow_admission(conn: sqlite3.Connection, *, workflow_id: str, version: str,
                       sha256: str | None = None, sources_complete: bool = True) -> dict[str, Any]:
    """How a workflow version is admitted to start a run (``gates.json`` → bootstrap.SHIPPED).

    Start a workflow from either of the following.

    * ``ACTIVE``: the version is an ACTIVE asset (owner: govern, ``require_active``). One
      that is registered and has not reached ACTIVE is refused, whatever else holds.
    * ``SHIPPED``: the definition ships in this tree and its sha256 -- the definition and
      the source of the modules that implement its nodes -- equals its pin in
      ``config/shipped.json``. A pin that names another sha256 is a refusal naming both:
      the definition changed and nobody has looked at the difference.
    """
    name = f"{workflow_id}@{version}"
    owner = _resolve("workflow_admission")
    reason = None
    if owner is not None:
        try:
            done, asset = _call("workflow_admission", owner, conn, conn, workflow_id, version)
        except ArtifactNotFound:
            done, asset = True, None
            reason = f"{workflow_id}@{version} is not registered as an asset"
        except GateNotMet as exc:
            raise CapabilityNotApproved(
                f"workflow {workflow_id}@{version} is registered and is not ACTIVE: {exc.message}",
                details={"workflow_id": workflow_id, "version": version, **(exc.details or {})},
            ) from exc
        if done and asset is not None:
            return {"admission": "ACTIVE", "asset_sha256": asset.get("sha256"),
                    "asset_status": asset.get("status")}
        if not done:
            reason = ON_FALLBACK["workflow_admission"]
    else:
        reason = ON_FALLBACK["workflow_admission"]

    pinned = shipped_pins("workflows").get(name)
    if pinned is not None:
        if not sources_complete:
            raise CapabilityNotApproved(
                f"workflow {name} is pinned as shipped and the source of one of its node "
                "modules cannot be read, so its sha256 does not cover what would run",
                details={"workflow_id": workflow_id, "version": version, "pinned_sha256": pinned},
            )
        if sha256 != pinned:
            raise CapabilityNotApproved(
                f"workflow {name} has changed since it was pinned as shipped: pinned {pinned}, "
                f"installed {sha256}. config/shipped.json is moved by the lead after reading "
                "the difference",
                details={"workflow_id": workflow_id, "version": version, "pinned_sha256": pinned,
                         "installed_sha256": sha256},
            )
        ON_FALLBACK.pop("workflow_admission", None)
        # The admitted digest is the run's own ``workflow.sha256`` and equals the pin; the
        # answer names it once, so a reader cannot be left to wonder which of two it was.
        return {"admission": "SHIPPED"}

    raise CapabilityNotApproved(
        f"workflow {name} may not start a run: {reason}, and config/shipped.json has no pin for it",
        details={"workflow_id": workflow_id, "version": version, "reason": reason,
                 "installed_sha256": sha256},
    )


# ----------------------------------------------------------------------------- budgets

def budget_profile(name: str) -> dict[str, Any]:
    """The limit lines of a named budget profile (owner: eval). Unknown is ``SCHEMA_INVALID``."""
    return dict(_must("budget_profile", None, name))


def create_root_budget(conn: sqlite3.Connection, *, run_id: str | None, profile_name: str,
                       actor: str) -> str:
    return str(_must("create_root_budget", conn, conn, run_id=run_id,
                     profile_name=profile_name, actor=actor))


def budget_state(conn: sqlite3.Connection, budget_ref: str | None) -> dict[str, Any] | None:
    if not budget_ref:
        return None
    return dict(_must("budget_state", None, conn, budget_ref))


def charge_worker_task(conn: sqlite3.Connection, *, budget_ref: str | None, task_id: str,
                       actor: str) -> dict[str, Any] | None:
    """Charge one ``worker_tasks`` unit for a task that was just created (owner: eval).

    A run with no budget is charged nothing and refused nothing: it was created below the
    tool surface (a test, a service acting for itself) and has no lines to draw on. A run
    with a budget and no ``worker_tasks`` line is refused by the owner, which is the rule
    that an absent line is not an unlimited one.
    """
    if not budget_ref:
        return None
    return dict(_must("charge", conn, conn, budget_ref=budget_ref, subject_kind="task",
                      subject_id=task_id, amounts={"worker_tasks": 1}, actor=actor))


def release_chain(conn: sqlite3.Connection, *, budget_ref: str | None, head_id: str, actor: str,
                  reason: str) -> list[str]:
    """Give back every hold a chain still keeps for followers that will not be created
    (owner: eval). The references released; none for a run with no budget."""
    if not budget_ref:
        return []
    answers = _must("release_chain", conn, conn, budget_ref=budget_ref, head_id=head_id, actor=actor, reason=reason)
    return [str(item.get("reservation_ref")) for item in answers if isinstance(item, Mapping)]


def release_run_holds(conn: sqlite3.Connection, *, run_id: str, actor: str, reason: str) -> list[str]:
    """Give back everything a run still holds in its budgets: a run that has ended holds
    nothing (design r2.2 §2.5.7; owner of the release: eval). The references released."""
    released: list[str] = []
    # The newest budget first: a path's own allotment is emptied before the hold that funds
    # it is given back, which the owner refuses while anything inside is still held.
    refs = [row["budget_ref"] for row in conn.execute(
        "SELECT budget_ref FROM budgets WHERE run_id=? ORDER BY rowid DESC", (run_id,))]
    for budget_ref in refs:
        state = budget_state(conn, budget_ref) or {}
        for item in reversed(state.get("reservations") or []):
            if item.get("status") != "HELD" or not item.get("reservation_ref"):
                continue
            try:
                _must("release_reservation", conn, conn, item["reservation_ref"], actor=actor, reason=reason)
            except GxError:
                continue   # the owner says it cannot be given back yet; it stays on record as HELD
            released.append(str(item["reservation_ref"]))
    return released


# ------------------------------------------------------------------------------ memory

def run_scopes(conn: sqlite3.Connection, run_id: str) -> list[str] | None:
    """The namespaces a run may search (owner: knowledge). None if absent.

    Its own project's namespace and, for a real project, the shared library. Read from
    the run row by the module that defines namespaces, never built here from a project
    reference: v1's loop searched every namespace when it was handed no scope.
    """
    owner = _resolve("run_scopes")
    if owner is None:
        return None
    done, value = _call("run_scopes", lambda: owner(conn, run_id), None)
    return [str(item) for item in value] if done else None


def memory_search(conn: sqlite3.Connection, *, query: str, project_scope: Any,
                  limit: int | None = None, run_id: str | None = None,
                  principal: str | None = None) -> dict[str, Any] | None:
    """What the store already knows about a question (owner: knowledge). None if absent.

    ``project_scope`` is the namespaces to look in, as :func:`run_scopes` gives them. The
    search is recorded against the run with who asked, so the ranking a research brief
    was built from can be read back.
    """
    owner = _resolve("memory_search")
    if owner is None:
        return None
    done, value = _call(
        "memory_search",
        lambda: owner(conn).search(query, project_scope, None, limit, run_id=run_id, principal=principal),
        None,
    )
    return dict(value) if done and isinstance(value, Mapping) else None


def stopped_holders(conn: sqlite3.Connection, *, run_id: str) -> list[dict[str, Any]] | None:
    """The run's live leases whose worker the host's own record shows stopped (owner: host),
    ``[{lease_id, task_id, agent, cause, host_event_id}]``; None when the owner is not
    installed. It reads the host's records and writes nothing (gx_run_status ``in_doubt``)."""
    owner = _resolve("stopped_holders")
    if owner is None:
        return None
    done, value = _call("stopped_holders", owner, None, conn, run_id=run_id)
    return [dict(item) for item in value] if done else None


def holder_stopped(conn: sqlite3.Connection, *, lease_id: str) -> dict[str, Any] | None:
    """The host's own observation of this lease's holder; a report is never a stop."""
    owner = _resolve("holder_stopped")
    if owner is None:
        return None
    done, value = _call("holder_stopped", owner, None, conn, lease_id=lease_id)
    return dict(value) if done and isinstance(value, Mapping) else None


def record_verdict_for_lease(conn: sqlite3.Connection, *, task_id: str,
                             lease_id: str, _codex_prepared: Any = None) -> dict[str, Any] | None:
    """Read a policy verdict from the host's record, through its canonical reader."""
    owner = _resolve("record_verdict_for_lease")
    if owner is None:
        return None
    done, value = _call("record_verdict_for_lease", owner, None, conn, task_id=task_id, lease_id=lease_id,
                        **({"_codex_prepared": _codex_prepared} if _codex_prepared is not None else {}))
    return dict(value) if done and isinstance(value, Mapping) else None


def session_capability(conn: sqlite3.Connection, *, host: str, session_ref: str | None) -> dict[str, Any] | None:
    """What the host's SessionStart record of a session says it could do (owner: host):
    ``{recorded, can_coordinate, reason, event_id, start_source, roles, missing_roles, dispatch,
    workflows_dir_present}``; None when the owner is not installed. One read; writes nothing."""
    owner = _resolve("session_capability")
    if owner is None:
        return None
    done, value = _call("session_capability", owner, None, conn, host=host, session_ref=session_ref)
    return dict(value) if done and isinstance(value, Mapping) else None


def cancel_run_jobs(conn: sqlite3.Connection, *, run_id: str, reason: str,
                    actor: str) -> list[dict[str, Any]] | None:
    """Ask every job of a run that has not ended to end (owner: jobs). None if absent.

    One answer per job: a request is not a cancellation until the job's runner confirms
    it, and a job that had already finished is settled as what it was. The owner looks at
    each runner, so this is called after the run's own cancellation has committed and
    outside any transaction.
    """
    owner = _resolve("cancel_run_jobs")
    if owner is None:
        return None
    done, value = _call(
        "cancel_run_jobs", lambda: owner(conn, actor=actor).cancel_for_run(run_id, reason=reason), None,
    )
    return [dict(item) for item in value] if done else None


def hold_run_jobs(conn: sqlite3.Connection, *, run_id: str, reason: str,
                  actor: str) -> list[dict[str, Any]] | None:
    """End every job of a paused run whose tool has not started (owner: jobs). None if absent.

    A job admitted and not launched, or waiting for the device, would otherwise start
    while the run is paused. A job whose tool is running is left to finish. The owner looks
    at runners, so this is called after the pause has committed and outside any transaction.
    """
    owner = _resolve("hold_run_jobs")
    if owner is None:
        return None
    done, value = _call(
        "hold_run_jobs", lambda: owner(conn, actor=actor).cancel_unstarted_for_run(run_id, reason=reason), None)
    return [dict(item) for item in value] if done else None


def exec_need_cleared(conn: sqlite3.Connection, need: Mapping[str, Any]) -> dict[str, Any] | None:
    """Whether a need the exec stream holds the fact for has cleared (readiness,
    device_capacity, path_reachability; owner: jobs). ``{cleared, why, ...}``, or None when
    the owner is not installed. It may read the device or the filesystem, so it is called
    outside any transaction."""
    owner = _resolve("exec_need_cleared")
    if owner is None:
        return None
    done, value = _call("exec_need_cleared", owner, None, conn, dict(need))
    return dict(value) if done and isinstance(value, Mapping) else None


def reconcile_jobs(conn: sqlite3.Connection, *, run_id: str, actor: str) -> list[dict[str, Any]] | None:
    """Look again at every job of a run that has not ended, and settle those that have
    (owner: jobs). ``[{job_id, from, to}]`` for the jobs that moved; None if absent.

    This is what makes a job's ending known when nobody is waiting for it: each job it
    settles writes its ``job.terminal`` trigger in the transaction that settles it. The
    owner looks at runners and publishes outputs, so it is called outside any transaction
    and refuses, loudly, inside one. With no open job of the run it is one read.
    """
    owner = _resolve("reconcile_jobs")
    if owner is None:
        return None
    done, value = _call("reconcile_jobs", lambda: owner(conn, actor=actor).reconcile(run_id), None)
    return [dict(item) for item in value] if done else None


def review_levels(conn: sqlite3.Connection, *, kind: str, subject_id: str, sha256: str) -> list[str]:
    """The independence levels of the reviews on record for one subject at one sha256
    (owner: govern). Empty when there is none, and when the owner is not installed: a
    review nobody can show is not a review."""
    owner = _resolve("reviews_for")
    if owner is None:
        return []
    done, value = _call("reviews_for", owner, None, conn, Subject(kind=kind, id=subject_id, sha256=sha256))
    if not done:
        return []
    return [str(item.get("independence_level")) for item in value if isinstance(item, Mapping)]


def submission_review_state(conn: sqlite3.Connection,submission_id: str,sha256: str) -> dict[str,Any]:
    """An approving qualified review, with no outstanding recorded objection.

    Review rows are immutable. A later approval of the same rejected bytes does
    not erase an objection; a new accepted product has its own subject and hash.
    """
    rows=_must("reviews_for",None,conn,Subject(kind="submission",id=submission_id,sha256=sha256))
    qualified=[]
    for row in rows:
        level=row.get("independence_level")
        if level not in ("HUMAN","DIFFERENT_HOST"):
            continue
        task_id=row.get("task_id")
        task=conn.execute("SELECT role FROM tasks WHERE task_id=?",(task_id,)).fetchone() if task_id else None
        if (level=="HUMAN" and not task_id) or (task and task["role"]=="verifier"):
            qualified.append(row)
    objections=[row["review_id"] for row in rows if row.get("verdict")!="APPROVE"]
    approvals=[row["review_id"] for row in qualified if row.get("verdict")=="APPROVE"]
    return {"met":bool(approvals) and not objections,"approval_ids":approvals,"objection_ids":objections}


def human_task_review(ctx: Ctx,*,task_id: str,lease_id: str) -> bool:
    return bool(_must("human_task_review",None,ctx,{"task_id":task_id,"lease_id":lease_id}))


def shipped_strategies() -> dict[str, Any]:
    """Every shipped retrieval strategy with its sha256, its pin and whether they meet, as the
    owner reads them (owner: knowledge): the reading its own admission uses."""
    return dict(_must("shipped_strategies", None))


def shipped_role_intents() -> tuple[dict[str, str], dict[str, Any]]:
    """``(pins, shipped)``: the owner's reading of the role-intent pins, and each shipped
    intent with the sha256 computed from the policy file as it is now (owner: host). The
    pair the owner's own pin check compares."""
    return dict(_must("role_intent_pins", None)), dict(_must("role_intents", None))


def reserve_task_chain(conn: sqlite3.Connection, *, budget_ref: str, head_id: str,
                       followers: Mapping[str, Mapping[str, Any]], actor: str) -> dict[str, Any]:
    return dict(_must("reserve_task_chain", conn, conn, budget_ref=budget_ref, head_kind="expansion",
                      head_id=head_id, head_amounts={"worker_tasks":0}, followers=followers, actor=actor))


def release_reservation(conn: sqlite3.Connection,ref: str,*,actor: str,reason: str) -> dict[str,Any]:
    return dict(_must("release_reservation",conn,conn,ref,actor=actor,reason=reason))


def advance_asset(ctx: Ctx, asset_id: str, version: str, *, until: str = "ACTIVE") -> dict[str, Any]:
    return dict(_must("advance_asset", None, ctx, asset_id, version, until=until))


def asset_review_for(ctx: Ctx,asset_id: str,version: str,*,run_id: str,stage: str|None=None,
                     reservation_ref: str|None=None) -> dict[str,Any]:
    asset=_must("get_asset",None,ctx.conn,asset_id,version)
    return dict(_must("asset_review_for",None,ctx,asset,run_id=run_id,
                      stage=stage,reservation_ref=reservation_ref))


def report() -> dict[str, Any]:
    """Which seams resolve to their owner now, for ``gx doctor``."""
    resolved = {}
    for seam, owner in OWNERS.items():
        resolved[seam] = {"owner": owner, "installed": _resolve(seam) is not None}
    return {"seams": resolved, "on_fallback": dict(ON_FALLBACK)}


__all__ = [
    "ON_FALLBACK", "OWNERS", "agent_policy", "approval_status", "bind_worker",
    "cancel_run_jobs",
    "budget_profile", "budget_state", "charge_worker_task", "compile_brief",
    "create_root_budget", "exec_need_cleared", "hold_run_jobs", "host_record", "launch_session",
    "launch_session_ref", "memory_search", "session_capability", "stopped_holders",
    "policy_verdict", "reconcile_jobs", "release_chain", "release_run_holds", "report", "request_approval",
    "review_levels", "run_scopes",
    "screen_objective", "shipped_role_intents", "shipped_strategies", "task_requirement",
    "workflow_admission",
]
