"""Runs: start, status, resume, the coordinator lease and run control.

Six things a run needs that v1 did not give it, each a rule of
``contracts/states.json`` (machine ``run``):

* **Inputs are named at the start, by hash.** ``gx_run_start`` takes ``inputs`` and
  ``input_files``; each file is re-hashed against the sha256 given for it. v1 took them
  through an undocumented key of ``constraints`` and a run started without it skipped
  evidence collection.
* **Pins.** The run records the sha256 of the contract, of its workflow definition and
  of the agent policy it started under. Resume compares all three with what is installed
  and refuses on a difference, naming both digests. v1 recorded a digest and never
  compared it.
* **One coordinator.** Whoever starts a run holds its coordinator lease. Run-level
  writes (resume, control) are accepted only from the holder of the live lease and renew
  it; a second host takes over with ``gx_coordinator_claim`` after expiry or release. In
  v1 only tests ever acquired the lease.
* **Control.** A run is paused, continued and cancelled through ``gx_run_control``. A
  pause records its cause and where the run continues from. v1 could enter PAUSED and
  had no code that left it.
* **The objective is a record with a screen**, created with the run and gated by the
  workflow's first node.
* **A waiting run says what it is waiting for** (``gx_run_status`` → ``waiting_for``).
"""

from __future__ import annotations

import fnmatch
import importlib
import json
import logging
import secrets
import sqlite3
import string
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import config, contracts
from ..artifacts.service import ArtifactService
from ..context import SCOPE_OPERATOR, Ctx
from ..domain import clock, ids, machines
from ..errors import (
    ApprovalRequired,
    ArtifactNotFound,
    CapabilityNotApproved,
    ClientCapabilityMissing,
    ConcurrentRun,
    ContentHashMismatch,
    DependencyUnmet,
    GxError,
    HoldActive,
    IdempotencyConflict,
    IllegalTransition,
    LeaseExpired,
    ModelPolicyViolation,
    OutcomeUnknown,
    PermissionDenied,
    PolicyScreenBlocked,
    SchemaInvalid,
    VersionImmutable,
)
from ..persistence import records, store
from ..teams import recovery
from ..teams.service import TeamService
from . import packages, seams
from .hooks import DEFAULT_HOOKS
from .engine import Engine, InterruptRequest, Workflow
from .registry import WorkflowRegistry

LOGGER = logging.getLogger("gx.orchestration")

RUN = "run"
INPUT_KIND = "run_input"
#: An input file up to this size is copied into the store, so a worker can fetch it by
#: reference. A larger one stays where it is and is named by path and sha256.
MAX_COPIED_INPUT_BYTES = 8 * 1024 * 1024
MAX_INPUT_FILES = 64
MAX_OBJECTIVE_TEXT = 4000

CONTROL_ACTIONS = ("pause", "continue", "cancel", "budget_apply", "workflow_repin")
LEASE_ACTIONS = ("claim", "renew", "release")

_registry: WorkflowRegistry | None = None
#: Package -> why it could not be registered, for the last registry built.
_load_errors: dict[str, str] = {}
_registry_injected = False
# The initial installed package inventory, separate from later asset cache entries
# and from approval pins. Installed code with no pin must still reach admission.
_installed_shipped_refs: frozenset[tuple[str, str]] = frozenset()


# ----------------------------------------------------------------- workflow registry

def default_workflow_ref() -> tuple[str, str]:
    """The workflow a run starts on when it names none: the plugin's own default."""
    return tuple(packages.installation()["default"].rsplit("@", 1))


def default_registry(conn: sqlite3.Connection | None = None, *, workflow_id: str | None = None,
                     version: str | None = None) -> WorkflowRegistry:
    """The process-wide registry, with every shipped workflow version registered once.

    A package that cannot be loaded is not installed and says why (``gx doctor``, and the
    refusal of a run asked for on it); it does not take the other packages down with it.
    Being installed admits nothing: a run starts on a shipped workflow only by its pin.

    With a store and an exact requested version, restore that ACTIVE native asset
    into a request-local registry. Never scan all assets or populate the global
    registry from one store: a same-named asset of another store grants no authority.
    This path only reads existing bytes/state and binds fixed approved node code.
    """
    global _registry, _load_errors, _installed_shipped_refs
    if _registry is None:
        registry = WorkflowRegistry()
        errors: dict[str, str] = {}
        for entry in packages.installation()["packages"]:
            name = entry["module"]
            try:
                staged = registry.staged_copy(entry.get("compat_hooks", {}))
                importlib.import_module(name).register_all(staged)
                for reference, provider in entry.get("compat_hooks", {}).items():
                    wid, ver = reference.rsplit("@", 1)
                    staged.bind_compat(wid, ver, packages.component(provider),
                        workflow_pin=seams.shipped_pins("workflows").get(reference),
                        hook_pin=seams.shipped_pins("workflow_compat_hooks").get(reference))
                registry = staged
            except Exception as exc:  # an unavailable package cannot hide the remaining installations
                errors[name] = f"{type(exc).__name__}: {exc}"[:600]
        _registry, _load_errors = registry, errors
        _installed_shipped_refs = frozenset(
            (registered_id, registered_version) for registered_id in registry.ids()
            for registered_version in registry.versions(registered_id))
    if conn is None or workflow_id is None or version is None:
        return _registry
    version = str(version)
    if _registry_injected and _registry.has(workflow_id, version):
        # Preserve the pre-existing trusted wiring/test injection seam. Requests
        # cannot select it; normal process caches always take the store path below.
        return _registry
    row = conn.execute("SELECT kind,status,content_sha256 FROM assets WHERE asset_id=? AND version=?",
                       (workflow_id, version)).fetchone()
    if (workflow_id, version) not in _installed_shipped_refs:
        raise ArtifactNotFound("The exact workflow package is not installed",
                               details={"workflow_id": workflow_id, "version": version})
    if row is not None:
        flow = _registry.get(workflow_id, version)
        if (row["kind"], row["status"], row["content_sha256"]) != ("EXECUTABLE_WORKFLOW", "ACTIVE", flow.digest()):
            raise CapabilityNotApproved("The installed package differs from the exact ACTIVE workflow asset",
                details={"workflow_id": workflow_id, "version": version, "asset_status": row["status"],
                         "asset_sha256": row["content_sha256"], "installed_sha256": flow.digest()})
    return _registry


def load_errors() -> dict[str, str]:
    """Why each shipped workflow package that is not installed could not be loaded."""
    default_registry()
    return dict(_load_errors)


def set_registry(registry: WorkflowRegistry | None) -> None:
    """Replace the process-wide registry (wiring and tests)."""
    global _registry, _load_errors, _registry_injected, _installed_shipped_refs
    _registry, _load_errors = registry, {}
    _registry_injected = registry is not None
    _installed_shipped_refs = frozenset()


def _engine(ctx: Ctx, run_id: str | None = None) -> Engine:
    run = _run(ctx.conn, run_id) if run_id is not None else None
    if run is not None and not run["workflow_id"]:
        from ..p0 import v6_goal
        if v6_goal.is_goal(run):
            # Goal instances resolve from the Run's immutable graph rows. A third-party
            # static recipe should not be loaded merely to drive or inspect that Run.
            return Engine(ctx.conn, WorkflowRegistry(), teams=TeamService(ctx.conn, ctx=ctx), ctx=ctx)
    registry = default_registry()
    if run is not None:
        if run["workflow_id"]:
            try:
                registry = default_registry(ctx.conn, workflow_id=run["workflow_id"], version=run["workflow_version"])
            except GxError as exc:
                # Metadata/status/cancel remain readable when this one workflow
                # cannot be restored. No cached body may then execute or certify
                # stage readiness; writes validate with _pinned_workflow first.
                registry = WorkflowRegistry()
                warning = f"workflow {run['workflow_id']}@{run['workflow_version']} is unavailable: {exc.code}: {exc.message}"
                if warning not in ctx.warnings:
                    ctx.warn(warning)
    return Engine(ctx.conn, registry, teams=TeamService(ctx.conn, ctx=ctx), ctx=ctx)


def _pinned_workflow(conn: sqlite3.Connection, run: sqlite3.Row) -> Workflow:
    """The exact binding needed by a write, or a refused version (never a re-pin)."""
    try:
        return default_registry(conn, workflow_id=run["workflow_id"], version=run["workflow_version"]).get(
            run["workflow_id"], run["workflow_version"])
    except GxError as exc:
        raise VersionImmutable(
            "the run's exact workflow binding is unavailable; restore that approved version before continuing",
            details={"workflow_id": run["workflow_id"], "version": run["workflow_version"],
                     "cause": {"code": exc.code, "message": exc.message}},
        ) from exc


def hooks_for_run(conn, run_id):
    """Resolve the exact saved binding before invoking any workflow behavior."""
    run = _run(conn, run_id)
    if not run["workflow_id"]:
        from ..p0 import v6_goal
        if v6_goal.is_goal(run):
            from dataclasses import replace
            return replace(DEFAULT_HOOKS, register_supplement=v6_goal.supplement)
        return DEFAULT_HOOKS
    flow = _pinned_workflow(conn, run)
    if run["workflow_sha256"] and flow.digest() != run["workflow_sha256"]:
        raise VersionImmutable("The installed workflow behavior differs from this run's original workflow pin")
    if '"plugin_binding"' in (run["inputs_json"] or ""):
        from ..plugins import runtime
        if (saved := runtime.recorded(run)) is not None:
            return runtime.for_saved(flow, saved).hooks
    return flow.hooks


def prepare_workflow_job(conn, run_id, proposal):
    """Validate an exact fresh Job request. Existing idempotent Jobs retain their original receipt."""
    prepare = hooks_for_run(conn, run_id).prepare_job
    if prepare is DEFAULT_HOOKS.prepare_job:
        return
    from copy import deepcopy
    original = deepcopy(proposal)
    if prepare(conn, run_id, original) is not None or original != proposal:
        raise SchemaInvalid("prepare_job validates the exact request; a new proposal needs its own Job request")


def shipped_report() -> dict[str, Any]:
    """Every workflow, strategy and role intent this tree installs or pins: its sha256, its pin,
    and whether they meet.

    ``gx doctor`` prints it. A definition changed without its pin being moved shows here
    as a mismatch, and is refused at run start. Strategies and role intents are read through
    their owners' functions (knowledge, host), the readings their own admission uses.
    """
    pins = seams.shipped_pins("workflows")
    installed = {f"{item['workflow_id']}@{item['version']}": item for item in default_registry().describe()}
    report: dict[str, Any] = {}
    for name in sorted(set(pins) | set(installed)):
        workflow = default_registry().get(*name.split("@", 1)) if name in installed else None
        sha256 = workflow.digest() if workflow is not None else None
        report[name] = {
            "installed": workflow is not None, "sha256": sha256, "pinned_sha256": pins.get(name),
            "matches": sha256 is not None and sha256 == pins.get(name),
            "sources": workflow.sources if workflow is not None else None,
            "admitted_as": ("SHIPPED" if sha256 is not None and sha256 == pins.get(name) else None),
        }
    return {"workflows": report, "strategies": _strategy_report(), "role_intents": _role_intent_report(),
            "packages_not_loaded": load_errors()}


def _not_computed(pinned: str, exc: BaseException) -> dict[str, Any]:
    """A pin whose owner could not compute the installed digest: nothing about it is guessed."""
    return {"installed": None, "sha256": None, "pinned_sha256": pinned, "matches": None, "admitted_as": None,
            "error": f"{type(exc).__name__}: {exc}"[:300]}


def _strategy_report() -> dict[str, Any]:
    """Shipped retrieval strategies, as the knowledge stream's ``strategy.shipped_report()`` reads them
    (the reading ``strategy.require_admitted`` admits by), plus any pin of a strategy not shipped."""
    try:
        owned = seams.shipped_strategies()
    except Exception as exc:  # noqa: BLE001 - a doctor reports what it could not compute
        return {name: _not_computed(pin, exc) for name, pin in sorted(seams.shipped_pins("strategies").items())}
    report = {name: {"installed": True, **entry} for name, entry in owned.items()}
    for name, pin in seams.shipped_pins("strategies").items():
        report.setdefault(name, {"installed": False, "sha256": None, "pinned_sha256": pin,
                                 "matches": False, "admitted_as": None})
    return dict(sorted(report.items()))


def _role_intent_report() -> dict[str, Any]:
    """Shipped role intents: each sha256 as the host stream's ``roles.shipped()`` computes it from the
    policy file as it is now, against host's reader of the pins (the pair ``roles.pin_problems`` compares)."""
    try:
        pins, shipped = seams.shipped_role_intents()
        computed = {role_id: entry["sha256"] for role_id, entry in shipped.items()}
    except Exception as exc:  # noqa: BLE001 - a doctor reports what it could not compute
        return {name: _not_computed(pin, exc) for name, pin in sorted(seams.shipped_pins("role_intents").items())}
    report: dict[str, Any] = {}
    for name in sorted(set(pins) | set(computed)):
        sha256, pinned = computed.get(name), pins.get(name)
        matches = sha256 is not None and sha256 == pinned
        report[name] = {"installed": name in computed, "sha256": sha256, "pinned_sha256": pinned,
                        "matches": matches, "admitted_as": "SHIPPED" if matches else None}
    return report


def _run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
    return row


# ------------------------------------------------------------------------------- pins

def installed_pins(workflow: Workflow | None, *, run: Mapping[str, Any] | None = None,
                   policy_sha256: str | None = None) -> dict[str, Any]:
    """Current pins in this run's immutable policy family, or an already selected new snapshot."""
    policy_sha = policy_sha256
    if policy_sha is None:
        _policy, policy_sha = seams.agent_policy_for_run(run) if run is not None else seams.agent_policy()
    return {
        "contract_version": contracts.contract_version(),
        "contract_sha256": contracts.contract_digest()["__combined__"],
        "workflow_sha256": workflow.digest() if workflow is not None else None,
        "agent_policy_sha256": policy_sha,
    }


def run_pins(run: sqlite3.Row) -> dict[str, Any]:
    return {key: run[key] for key in ("contract_version", "contract_sha256", "workflow_sha256",
                                      "agent_policy_sha256")}


def pins_view(run: sqlite3.Row) -> dict[str, Any]:
    """The pins as an answer shows them: the three digests and the host the run is judged by.

    ``host`` is how the host was settled at start (``kind``, ``kind_basis``) and whether a
    more lenient host than the launch environment showed was declared (``understated``):
    what the run's workers are held to is part of what the run is pinned to.
    """
    host = store.loads(run, "host_json", None) or {}
    return {**run_pins(run),
            "host": {"kind": host.get("kind"), "kind_basis": host.get("kind_basis"),
                     "understated": bool(host.get("understated"))}}


def require_pins(run: sqlite3.Row, workflow: Workflow) -> dict[str, Any]:
    """Refuse unless what is installed now is what the run started under.

    A workflow definition that changed under an unchanged version is
    ``VERSION_IMMUTABLE``; a contract or an agent policy that changed is
    ``CONTENT_HASH_MISMATCH``. Either names both digests. A pin the run never recorded
    (a run created below the tool surface) is not compared.
    """
    now = installed_pins(workflow, run=run)
    _check_pins(run, now)
    return now


def _check_pins(run: sqlite3.Row, now: Mapping[str, Any]) -> None:
    pinned = run_pins(run)
    if pinned["workflow_sha256"] and pinned["workflow_sha256"] != now["workflow_sha256"]:
        raise VersionImmutable(
            f"workflow {run['workflow_id']}@{run['workflow_version']} has changed since run "
            f"{run['run_id']} pinned it: pinned {pinned['workflow_sha256']}, installed "
            f"{now['workflow_sha256']}. A changed definition is a new version",
            details={"run_id": run["run_id"], "what": "workflow_definition",
                     "pinned_sha256": pinned["workflow_sha256"],
                     "installed_sha256": now["workflow_sha256"]},
        )
    for what, key in (("contract", "contract_sha256"), ("agent_policy", "agent_policy_sha256")):
        if pinned[key] and pinned[key] != now[key]:
            raise ContentHashMismatch(
                f"the {what.replace('_', ' ')} has changed since run {run['run_id']} pinned it: "
                f"pinned {pinned[key]}, installed {now[key]}",
                details={"run_id": run["run_id"], "what": what, "pinned_sha256": pinned[key],
                         "installed_sha256": now[key]},
            )


# ---------------------------------------------------------------------- gx_run_start

def _objective_text(objective: Any) -> str:
    if isinstance(objective, str):
        text = objective.strip()
    elif isinstance(objective, Mapping):
        text = ""
        for key in ("question", "statement", "objective", "title"):
            value = objective.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            text = ids.canonical_json(dict(objective))
    else:
        text = ""
    if not text:
        raise SchemaInvalid("a run needs an objective: a question, or an object that states one",
                            details={"objective_type": type(objective).__name__})
    return text[:MAX_OBJECTIVE_TEXT]


def _forbidden(path: Path) -> str | None:
    patterns = [p.split("/")[-1] for p in (config.paths().get("forbidden_paths") or {}).get("patterns", [])]
    for candidate in {str(path), str(path.resolve())}:
        for part in Path(candidate).parts:
            for pattern in patterns:
                if fnmatch.fnmatch(part, pattern):
                    return pattern
    return None


def _check_input_files(entries: Sequence[Any]) -> list[dict[str, Any]]:
    """Each named input file exists, may be read and hashes to the sha256 given for it."""
    if len(entries) > MAX_INPUT_FILES:
        raise SchemaInvalid(f"at most {MAX_INPUT_FILES} input files are named at run start",
                            details={"given": len(entries)})
    checked: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str) \
                or not isinstance(entry.get("sha256"), str):
            raise SchemaInvalid("each input file is {path, sha256}", details={"index": index})
        path = Path(entry["path"])
        if not path.is_absolute():
            raise SchemaInvalid(f"input file path {entry['path']!r} is not absolute",
                                details={"index": index, "path": entry["path"]})
        pattern = _forbidden(path)
        if pattern is not None:
            raise PermissionDenied(
                f"input file {entry['path']!r} matches the forbidden pattern {pattern!r} of "
                "config/paths.json and is not opened",
                details={"index": index, "pattern": pattern},
            )
        if not path.is_file():
            raise ArtifactNotFound(f"input file {entry['path']!r} does not exist",
                                   details={"index": index, "path": entry["path"]})
        observed = ids.sha256_of_file(path)
        if observed != entry["sha256"].lower():
            raise ContentHashMismatch(
                f"input file {entry['path']!r} hashes to {observed}, not the {entry['sha256']} "
                "given for it",
                details={"index": index, "path": entry["path"], "given_sha256": entry["sha256"],
                         "observed_sha256": observed},
            )
        checked.append({"path": str(path), "sha256": observed, "bytes": path.stat().st_size,
                        "role": entry.get("role")})
    return checked


def _workflow_ref(spec: Any) -> tuple[str, str]:
    default_id, default_version = default_workflow_ref()
    if spec is None:
        return default_id, default_version
    if not isinstance(spec, Mapping):
        raise SchemaInvalid("workflow is an object {id, version}", details={})
    workflow_id = spec.get("id") or spec.get("workflow_id") or default_id
    version = spec.get("version") or spec.get("workflow_version")
    if version is None:
        if workflow_id != default_id:
            raise SchemaInvalid(f"workflow {workflow_id!r} is named without a version",
                                details={"workflow": dict(spec)})
        version = default_version
    return str(workflow_id), str(version)


def _host_record(ctx: Ctx, host: Any) -> dict[str, Any]:
    """The host this run's workers are judged by, as the host stream settles it.

    The kind decides how strictly a worker's model must be shown, so it is not left to
    whoever starts the run to leave out: with nothing declared, a run started from inside
    Claude Code is a ``claude_code`` run. What else the caller said about its host is
    kept beside the record as said, and is consulted by nothing.
    """
    if host is not None and not isinstance(host, Mapping):
        raise SchemaInvalid("host is an object", details={})
    declared = dict(host or {})
    if ctx.native_call is not None:
        # Actual identity is settled by the controlled native callback and the
        # policy selector below. Do not invoke legacy environment/transcript
        # detection before that selector can see the native source.
        return {"declared": declared, "session_ref": declared.get("session_ref"),
                "recorded_from": ctx.actor}
    record = seams.host_record(declared)
    also = {key: value for key, value in declared.items()
            if isinstance(key, str) and key not in (record.get("declared") or {})
            and len(ids.canonical_json(value)) <= 2000}
    if also:
        record["also_said"] = also
    record["recorded_from"] = ctx.actor
    return record


def _require_capable_session(conn: sqlite3.Connection, host: Mapping[str, Any]) -> None:
    """Contract r5 gx_run_start: under host claude_code a run is refused CLIENT_CAPABILITY_MISSING
    when the host recorded no SessionStart for the session, or one that shows the gx roles or
    gx-dispatch absent; the answer is to start a new session, and there is no fallback to untyped
    agents for gx roles. What the session began with is the host stream's record
    (``hostobs.events.session_capability``); a run declared ``other_verified`` has no hook and is
    not asked. With no session named there is nothing to read, and that is refused too."""
    if host.get("kind") != "claude_code":
        return
    session_ref = host.get("session_ref")
    capability = seams.session_capability(conn, host="claude_code", session_ref=session_ref) if session_ref else None
    if capability is not None and capability.get("can_coordinate"):
        return
    if not session_ref:
        reason = "the run names no session of the host, so no SessionStart record can be read"
    elif capability is None:
        reason = ("the host stream's reader of SessionStart records is not installed ("
                  + seams.ON_FALLBACK.get("session_capability", "no owner") + ")")
    else:
        reason = str(capability.get("reason") or "the session's start shows the gx roles or gx-dispatch absent")
    raise ClientCapabilityMissing(
        f"a run under host claude_code is coordinated from a session that began with the gx roles and "
        f"gx-dispatch installed: {reason}. Start a new session after installing the adapter",
        details={"host": "claude_code", "session_ref": session_ref,
                 **({key: capability.get(key) for key in ("recorded", "event_id", "start_source", "missing_roles",
                                                          "dispatch", "workflows_dir_present")}
                    if capability is not None else {})},
    )


def run_start(ctx: Ctx, args: Mapping[str, Any], *, prepare_p0=None, workspace_key=None, plugin_binding=None) -> dict[str, Any]:
    """Create a run with its pins, objective, budget and coordinator lease, then run the
    workflow to its first interrupt. Returns as soon as outside work is needed."""
    config.check_holds("orchestration")
    conn = ctx.conn
    objective = args["objective"]
    text = _objective_text(objective)
    project_ref = args["project_ref"]
    profile = seams.budget_profile(args["budget_profile"])
    goal_mode = prepare_p0 is not None and args.get("inputs", {}).get("mode") == "goal"
    workflow_id = version = workflow = None
    admission = {}
    if not goal_mode:
        workflow_id, version = _workflow_ref(args.get("workflow"))
        try:
            workflow = default_registry(conn, workflow_id=workflow_id, version=version).get(workflow_id, version)
        except ArtifactNotFound as exc:
            failed = load_errors()
            raise SchemaInvalid(
                f"no workflow {workflow_id}@{version} is installed"
                + (f"; the package workflows/{workflow_id} could not be loaded: {failed[workflow_id]}"
                   if workflow_id in failed else ""),
                details={"workflow_id": workflow_id, "version": version, **(exc.details or {}),
                         **({"load_error": failed[workflow_id]} if workflow_id in failed else {})},
            ) from exc
        admission = seams.workflow_admission(conn, workflow_id=workflow_id, version=version,
                                             sha256=workflow.digest(),
                                             sources_complete=workflow.sources_complete())
    host = _host_record(ctx, args.get("host"))
    from ..hostobs import host_policy
    if prepare_p0 is not None:
        # A partial start may have committed its run before writing its first
        # checkpoint. Resume that deployment even when its profile file is gone.
        prior = conn.execute(
            "SELECT r.* FROM idempotency i JOIN runs r ON r.run_id=json_extract(i.response_json,'$.run_id') "
            "WHERE i.scope='gx_run_start' AND i.idempotency_key=? AND i.side_effect_status='APPLIED' "
            "AND json_extract(r.inputs_json,'$.inputs.p0')=1",
            (store.tool_key("gx_run_start", args["idempotency_key"]),)).fetchone()
        if prior:
            policy, policy_sha = host_policy.current_for_run(prior)
            host = store.loads(prior, "host_json")
        else:
            policy, policy_sha, host = host_policy.p0_host()
    else:
        policy, policy_sha, host = host_policy.for_new_run(ctx, host)
    _require_capable_session(conn, host)
    files = _check_input_files(args.get("input_files") or [])
    inputs = dict(args.get("inputs") or {})
    hooks = plugin_binding.hooks if plugin_binding else workflow.hooks if workflow else DEFAULT_HOOKS
    inputs = hooks.run_inputs(inputs)
    constraints = dict(args.get("constraints") or {})
    if inputs.get("p0") is True and prepare_p0 is None:
        raise PermissionDenied("Use the P0 operator entry to authorize a new research project")
    if workspace_key is not None and (prepare_p0 is None or inputs.get("p0") is not True):
        raise SchemaInvalid("A workspace run key requires the P0 operator entry")
    pins = installed_pins(workflow, policy_sha256=policy_sha)
    screen = seams.screen_objective(objective)
    if screen.get("result") == "BLOCKED":
        raise PolicyScreenBlocked(
            "the objective is outside the permitted boundary and no run is started for it: "
            + str(screen.get("reason") or "the screen blocked it"),
            details={"screen": {key: screen[key] for key in screen if key != "objective"}},
        )

    request = {"objective": objective, "project_ref": project_ref,
               "budget_profile": args["budget_profile"], "workflow": [workflow_id, version],
               "inputs": inputs, "input_files": [[f["path"], f["sha256"]] for f in files],
               "host": args.get("host"), "constraints": constraints}
    if workspace_key is not None:
        request["workspace_key"] = workspace_key
    if plugin_binding:
        request["plugin_binding"] = plugin_binding.receipt
    if ctx.native_call is not None:
        # A model may omit host/session from its arguments. Bind start replay to
        # the backend-settled native root, while allowing new calls/connections
        # from that same actual root to reconcile their original request.
        selected = host["policy_selection"]
        request["native_host"] = {key: selected[key] for key in
                                  ("source", "session_ref", "native_agent_ref")}

    def create(tx: sqlite3.Connection) -> dict[str, Any]:
        p0 = inputs.get("p0") is True
        budget_ref = None if p0 else seams.create_root_budget(
            tx, run_id=None, profile_name=args["budget_profile"], actor=ctx.actor)
        run_id = records.create_run(
            tx, objective=text, project_ref=project_ref, actor=ctx.actor,
            budget_json=profile.get("limits") or {}, constraints=constraints, budget_ref=budget_ref,
            workflow_id=workflow_id, workflow_version=version, pins=pins,
            agent_policy_ref=f"{('config/role_policy.codex.json' if policy.get('policy_host') == 'codex' else 'config/role_policy.json')}@{policy_sha}", agent_policy=policy,
            inputs={"inputs": inputs, "files": files}, host=host, resource_profile=resource_profile,
        )
        if p0:
            from ..p0 import team_admission, workspace
            budget_ref = workspace.attach_run(
                tx, run_id, project_ref=project_ref, run_key=workspace_key, profile_name=args["budget_profile"],
                spec=inputs.get("p0_spec", {}), actor=ctx.actor)
            tx.execute("UPDATE runs SET budget_ref=?, budget_json=(SELECT limits_json FROM budgets "
                       "WHERE budget_ref=?) WHERE run_id=?", (budget_ref, budget_ref, run_id))
            team_admission.configure(tx, run_id, inputs.get("p0_spec", {}))
        else:
            tx.execute("UPDATE budgets SET run_id=? WHERE budget_ref=?", (run_id, budget_ref))

        arts = ArtifactService(tx)
        stored_files = []
        for item in files:
            ref = None
            if item["bytes"] <= MAX_COPIED_INPUT_BYTES:
                raw = Path(item["path"]).read_bytes()
                media_type = "chemical/x-mmcif" if Path(item["path"]).suffix.lower() in (".cif", ".mmcif") else None
                if Path(item["path"]).suffix.lower() == ".py" and b"\x00" not in raw:
                    try:
                        raw.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                    else:
                        media_type = "text/x-python"
                published = arts.publish_bytes(
                    namespace=f"{run_id}/inputs", rel_path=f"{item['sha256'][:24]}/{Path(item['path']).name}",
                    kind=INPUT_KIND, data=raw, run_id=run_id, media_type=media_type,
                    provenance={"path": item["path"], "role": item.get("role")},
                    actor=ctx.actor, origin="INGEST",
                )
                if published.sha256 != item["sha256"]:
                    raise ContentHashMismatch(
                        f"input file {item['path']!r} changed while it was being taken in",
                        details={"path": item["path"], "given_sha256": item["sha256"],
                                 "observed_sha256": published.sha256},
                    )
                ref = published.artifact_ref
            stored_files.append({**item, "artifact_ref": ref})
        run_inputs = {"inputs": inputs, "files": stored_files}
        if plugin_binding:
            run_inputs["plugin_binding"] = plugin_binding.receipt
        tx.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (store._j(run_inputs), run_id))

        objective_id = ids.new_id("objective")
        spec = {"objective": objective, "text": text}
        now = clock.now_iso()
        tx.execute(
            "INSERT INTO objectives(objective_id, run_id, status, spec_json, screen_json,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (objective_id, run_id, machines.initial_state("objective"), store._j(spec),
             store._j(dict(screen)), now, now),
        )
        tx.execute("UPDATE runs SET objective_id=? WHERE run_id=?", (objective_id, run_id))
        store.append_event(
            tx, kind="objective.created", actor=ctx.actor, run_id=run_id, subject_kind="objective",
            subject_id=objective_id, new_state=machines.initial_state("objective"),
            evidence_level="EXECUTION_OBSERVED",
            payload={"screen_result": screen.get("result"), "screen_standin": bool(screen.get("standin")),
                     "spec_sha256": ids.sha256_of_obj(spec)},
        )
        store.append_event(
            tx, kind="run.pinned", actor=ctx.actor, run_id=run_id, subject_kind=RUN, subject_id=run_id,
            evidence_level="EXECUTION_OBSERVED",
            payload={"pins": pins, "workflow": {"id": workflow_id, "version": version,
                                                "admission": admission},
                     "input_files": len(stored_files)},
        )
        lease = store.acquire_coordinator_lease(
            tx, run_id=run_id, host=str(host.get("kind") or "unknown"), owner=ctx.principal.subject,
            seconds=_coordinator_seconds(None), session_ref=host.get("session_ref"), actor=ctx.actor,
        )
        if goal_mode:
            # Goal, its immutable inputs, the first decision and all their authorizations
            # are either visible together to another GX process or rolled back together.
            prepare_p0(run_id)
        return {"run_id": run_id, "budget_ref": budget_ref, "objective_id": objective_id,
                "coordinator_lease_ref": lease["lease_id"], "admission": admission}

    key = store.tool_key("gx_run_start", args["idempotency_key"])
    from ..jobs.run_resources import observe
    previous = conn.execute("SELECT 1 FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
    resource_profile = None if previous else observe()
    created, replayed = store.run_idempotent(conn, key, "gx_run_start", request, create)
    run_id = created["run_id"]
    # Creation has committed (or this exact request replayed that committed
    # record). A later node failure must not erase its durable recovery refs.
    ctx.authz["run_id"] = run_id
    ctx.authz["run_creation"] = {key: created[key] for key in
        ("run_id", "budget_ref", "objective_id", "coordinator_lease_ref")}
    ctx.authz["run_creation"].update(idempotency_key=args["idempotency_key"],
                                     side_effect_status="APPLIED")
    said_again = ["this idempotency key already started a run; the answer it was given is returned, "
                  "and gx_run_status says where the run is now"] if replayed else []

    # One key, one answer: the first complete answer stored under the key is what every
    # call with it gets, however many processes sent it and whichever of them finished
    # first. A call that finds no answer yet starts the workflow if nobody has (the first
    # call may have died between creating the run and starting it), or waits a bounded
    # time for the call that is starting it, and settles one itself only after that. (A key
    # that already has its answer is found by that wait at once.)
    engine = None if goal_mode else _engine(ctx, run_id)
    warnings: list[str] = []
    wait = False
    if inputs.get("p0") is True and not goal_mode:
        # The first native human turn authorizes the initial Intent before any node can submit a Job.
        prepare_p0(run_id)
    # The first Goal checkpoint belongs to a graph the Lead actually proposed.
    if not goal_mode and engine.state(run_id)["checkpoint_seq"] == 0:
        try:
            started = engine.start(
                run_id=run_id, workflow_id=workflow_id, workflow_version=version,
                inputs={**inputs, "objective": text, "objective_id": created["objective_id"]},
                actor=ctx.actor,
            )
            warnings.extend(started.get("warnings") or [])
        except (SchemaInvalid, IllegalTransition):
            if engine.state(run_id)["checkpoint_seq"] == 0:
                raise
            wait = True                      # another call with this key started it first
    elif not goal_mode:
        wait = True
    if wait:
        answer = _await_answer(conn, key, seconds=START_ANSWER_WAIT_SECONDS)
        if answer is not None:
            return {**answer, "warnings": list(answer.get("warnings") or []) + said_again}
        warnings.append("the call that started this run had not answered after "
                        f"{START_ANSWER_WAIT_SECONDS:g}s; the answer was settled from the run as it stood")
    host_said = store.loads(_run(conn, run_id), "host_json", None) or {}
    if host_said.get("understated"):
        warnings.append(
            f"the host was declared as {host_said.get('kind')}, which is judged more leniently than "
            f"the {(host_said.get('detected') or {}).get('kind')} this backend was launched from; the "
            "run is recorded as understated and its workers are held to the declared kind"
        )
    run = _run(conn, run_id)
    objective_row = conn.execute("SELECT status FROM objectives WHERE objective_id=?",
                                 (created["objective_id"],)).fetchone()
    # The run's inbox and outbox exist from the start, beside the store (``store.data_root``), so a
    # host can place a file there before its first put; the answer names both (contract r5).
    directories = run_directories(conn, run_id)
    answer = {
        "run_id": run_id, "revision": int(run["revision"]), "status": run["status"],
        "budget_ref": created["budget_ref"], "objective_status": objective_row["status"],
        "workflow": {"id": workflow_id, "version": version,
                     "sha256": run["workflow_sha256"], **created["admission"]},
        "agent_policy_ref": run["agent_policy_ref"],
        "coordinator_lease_ref": created["coordinator_lease_ref"],
        "pins": pins_view(run), "interrupt_ref": run["interrupt_ref"],
        "objective_id": created["objective_id"], "directories": directories, "warnings": warnings,
    }
    answer = store.settle_answer(conn, key, answer)
    return {**answer, "warnings": list(answer.get("warnings") or []) + said_again}


#: How long a run start that finds its key's run being started by another call waits for
#: that call's answer before settling one from the run as it stands.
START_ANSWER_WAIT_SECONDS = 60.0


def _await_answer(conn: sqlite3.Connection, key: str, *, seconds: float) -> dict[str, Any] | None:
    """The answer stored under a run start's key, waited for up to ``seconds``. Reads only."""
    deadline = time.monotonic() + seconds
    pause = 0.02
    while True:
        stored = store.stored_response(conn, key) or {}
        if stored.get("answer") is not None:
            return dict(stored["answer"])
        if time.monotonic() >= deadline:
            return None
        time.sleep(pause)
        pause = min(pause * 2, 0.25)


def proves_reconciled_start(conn: sqlite3.Connection, *, project_ref: str, run_id: str,
                           native_call: Mapping[str, Any]) -> bool:
    """Read the original creation and the settlement of its capacity-deferred Job step.

    The native receipt owner checks the original PARTIAL/RESOURCE_DENIED callback and
    a later successful call with the same arguments and run. The run's creation pins
    that original native evidence, including its argument hash. A replay can answer
    while the node is still idle, so also check the actual continuation and its Jobs.
    """
    from ..hostobs import native_context

    owners = conn.execute(
        "SELECT r.host_json,r.budget_ref,r.objective_id,i.idempotency_key,i.response_json FROM runs r "
        "JOIN budgets b ON b.budget_ref=r.budget_ref AND b.run_id=r.run_id AND b.parent_ref IS NULL "
        "JOIN objectives o ON o.objective_id=r.objective_id AND o.run_id=r.run_id "
        "JOIN idempotency i ON json_extract(i.response_json,'$.run_id')=r.run_id "
        "WHERE r.run_id=? AND r.project_ref=? AND i.scope='gx_run_start' "
        "AND i.side_effect_status='APPLIED' AND i.completed_at IS NOT NULL", (run_id, project_ref)).fetchmany(2)
    if len(owners) != 1:
        return False
    owner = owners[0]
    host = store.loads(owner, "host_json", {})
    selected = host.get("policy_selection") if isinstance(host, Mapping) else None
    prefix = store.tool_key("gx_run_start", "")
    if (not owner["idempotency_key"].startswith(prefix) or not owner["idempotency_key"][len(prefix):]
            or not isinstance(selected, Mapping) or not isinstance(native_call, Mapping)
            or selected.get("source") != native_context.SOURCE or native_call.get("tool_name") != "gx_run_start"
            or not selected.get("identity_sha256") or selected["identity_sha256"] != native_call.get("evidence_sha256")
            or not selected.get("invocation_call_id") or selected["invocation_call_id"] != native_call.get("call_id")
            or not host.get("session_ref") or host["session_ref"] != native_call.get("session_id")):
        return False
    creation = store.loads(owner, "response_json", {})
    answer = creation.get("answer") if isinstance(creation, Mapping) else None
    if not isinstance(answer, Mapping):
        return False
    lease = creation.get("coordinator_lease_ref")
    expected = {"run_id": run_id, "budget_ref": owner["budget_ref"], "objective_id": owner["objective_id"],
                "coordinator_lease_ref": lease}
    if (not lease or any(creation.get(key) != value or answer.get(key) != value for key, value in expected.items())
            or answer.get("status") != "ACTIVE" or type(answer.get("revision")) is not int):
        return False
    if not conn.execute("SELECT 1 FROM events WHERE run_id=? AND kind='run.coordinator_lease' "
                        "AND json_extract(payload_json,'$.lease_id')=? AND json_extract(payload_json,'$.session_ref')=?",
                        (run_id, lease, host["session_ref"])).fetchone():
        return False

    failure = conn.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='workflow.node_failed' "
                           "ORDER BY event_id LIMIT 1", (run_id,)).fetchone()
    failed = store.loads(failure, "payload_json", {}) if failure else {}
    if not isinstance(failed, Mapping) or failed.get("code") != "RESOURCE_DENIED" or not failed.get("node"):
        return False
    # The original attempt must have failed before submitting any Job. Its specific
    # node subsequently continued from the replayed revision and finished its wait.
    waits = conn.execute(
        "SELECT w.event_id,w.payload_json FROM events w WHERE w.run_id=? AND w.kind='run.transition' "
        "AND w.new_state='WAITING_EXTERNAL_JOB' AND w.prev_revision=? AND json_extract(w.payload_json,'$.node')=? "
        "AND NOT EXISTS (SELECT 1 FROM events j WHERE j.run_id=w.run_id AND j.event_id<? AND "
        "(j.kind='job.created' OR (j.kind='budget.reserved' AND json_extract(j.payload_json,'$.for[0]')='job'))) "
        "AND EXISTS (SELECT 1 FROM events c WHERE c.run_id=w.run_id AND c.kind='run.continued_from_idle' "
        "AND c.event_id>? AND c.event_id<w.event_id) "
        "AND EXISTS (SELECT 1 FROM events d WHERE d.run_id=w.run_id AND d.kind='workflow.node_completed' "
        "AND d.event_id>w.event_id AND json_extract(d.payload_json,'$.node')=?)",
        (run_id, answer["revision"], failed["node"], failure["event_id"], failure["event_id"], failed["node"])).fetchmany(2)
    if len(waits) != 1:
        return False
    job_refs = store.loads(waits[0], "payload_json", {}).get("job_refs")
    if not isinstance(job_refs, list) or not job_refs or not all(isinstance(ref, str) and ref for ref in job_refs):
        return False
    terminal = machines.terminal_states("job")
    settled = {row[0] for row in conn.execute(
        "SELECT j.job_id,j.status FROM jobs j JOIN budget_reservations b ON b.reservation_ref=j.reservation_ref "
        "AND b.subject_kind='job' AND b.subject_id=j.job_id AND b.budget_ref=? "
        "WHERE j.run_id=? AND j.side_effect_status='APPLIED' AND j.finished_at IS NOT NULL "
        "AND b.status='SETTLED' AND b.settled_at IS NOT NULL AND EXISTS (SELECT 1 FROM events j0 "
        "WHERE j0.run_id=j.run_id AND j0.kind='job.created' AND j0.subject_id=j.job_id "
        "AND json_extract(j0.payload_json,'$.reservation_ref')=j.reservation_ref AND j0.event_id>? AND j0.event_id<?)",
        (owner["budget_ref"], run_id, failure["event_id"], waits[0]["event_id"])) if row[1] in terminal}
    return set(job_refs).issubset(settled)


# --------------------------------------------------------------------- gx_run_status

def _waiting_for(conn: sqlite3.Connection, run: sqlite3.Row, engine_state: Mapping[str, Any],
                 *, idle: bool = False) -> dict[str, Any] | None:
    """What a run that is not moving is waiting for, and how it continues."""
    status = run["status"]
    pending = engine_state.get("pending_interrupt")
    if status == "ACTIVE" and not run["workflow_id"]:
        from ..p0 import v6_goal
        if v6_goal.is_goal(run):
            return v6_goal.waiting_for(conn, run["run_id"])
    if status == "ACTIVE" and idle:
        # Nothing pending and nobody advancing it: after a pause is lifted, a node that
        # raised, or a process that died between two nodes. Only `continue` moves it.
        failed = conn.execute(
            "SELECT reason, payload_json, ts FROM events WHERE run_id=? AND kind='workflow.node_failed'"
            " AND event_id > COALESCE((SELECT MAX(event_id) FROM events WHERE run_id=?"
            " AND kind IN ('workflow.node_completed', 'run.transition')), 0)"
            " ORDER BY event_id DESC LIMIT 1", (run["run_id"], run["run_id"]),
        ).fetchone()
        out = {"kind": "idle", "node": engine_state.get("node"), "interrupt_ref": None,
               "reason": "nothing is pending and nothing is advancing the run",
               "continue_with": "gx_run_control action=continue"}
        if failed is not None:
            out["reason"] = f"the next node raised at {failed['ts']}: {failed['reason']}"
        return out
    if status == "PAUSED":
        paused = store.loads(run, "paused_json", {}) or {}
        return {"kind": "paused", "reason": paused.get("reason"), "needs": paused.get("needs") or [],
                "continue_with": "gx_run_control action=continue"}
    if status not in machines.waiting_states(RUN):
        return None
    out: dict[str, Any] = {"kind": None, "reason": None, "interrupt_ref": run["interrupt_ref"],
                           "node": engine_state.get("node"), "continue_with": "gx_run_resume"}
    if pending:
        out.update(kind=pending.get("kind"), reason=pending.get("reason"))
        if pending.get("tasks"):
            rows = conn.execute(
                "SELECT task_id, status FROM tasks WHERE task_id IN ("
                + ",".join("?" for _ in pending["tasks"]) + ")", list(pending["tasks"]),
            ).fetchall()
            status_of = {row["task_id"]: row["status"] for row in rows}
            out["tasks"] = [{"task_id": task_id, "status": status_of.get(task_id)}
                            for task_id in pending["tasks"]]
        if pending.get("approval_ref"):
            out["approval"] = {"approval_id": pending["approval_ref"],
                               "status": seams.approval_status(conn, pending["approval_ref"])}
        if pending.get("job_refs"):
            out["jobs"] = list(pending["job_refs"])
    held = [
        hold for hold in (
            store.loads(row, "hold_json", {}) or {} for row in conn.execute(
                "SELECT hold_json FROM tasks WHERE run_id=? AND status='BLOCKED' AND hold_json IS NOT NULL",
                (run["run_id"],),
            )
        ) if hold.get("approval_id")
    ]
    if held:
        out["held_submissions"] = [
            {"approval_id": hold["approval_id"], "submission_id": hold.get("submission_id"),
             "policy_verdict": hold.get("policy_verdict")} for hold in held
        ]
        if status == "WAITING_APPROVAL" and (not pending or pending.get("kind") != "approval"):
            out.update(kind="approval", reason="a submission is held until the user decides about "
                                               "the worker that made it",
                       continue_with="gx_approval_grant, then gx_run_resume (which settles the decision)")
    return out


def _budget_view(conn: sqlite3.Connection, budget_ref: str | None) -> dict[str, Any]:
    """The run's budget as a status shows it: the lines, not one row per task charged."""
    state = seams.budget_state(conn, budget_ref)
    if state is None:
        return {}
    reservations = state.pop("reservations", [])
    state["reservations_total"] = len(reservations)
    state["reservations_held"] = sum(1 for item in reservations if item.get("status") == "HELD")
    return state


def _admission_of(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """How the run's workflow was admitted when it started (``run.pinned``): SHIPPED by its
    pin, ACTIVE as an asset, or the build's stand-in. Read from the record, never recomputed."""
    row = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='run.pinned'"
                       " ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
    if row is None:
        return None
    admission = (store.loads(row, "payload_json", {}) or {}).get("workflow", {}).get("admission")
    return dict(admission) if isinstance(admission, Mapping) else None


def run_status(ctx: Ctx, run_id: str) -> dict[str, Any]:
    """Where a run is, what it is waiting for and from where it would continue.

    A status reads and never writes: it is asked for by callers that may only read, and
    what time has decided -- a lapsed lease, a refused submission, a hold that was
    lifted -- is settled by the next gx_task_claim, gx_task_submit or gx_run_resume (the
    plan settles nothing either: ruling S2). When what would be settled now differs from
    the task states shown here, a warning says so.
    """
    conn = ctx.conn
    if conn.in_transaction:
        return _run_status(ctx, run_id)
    conn.execute("BEGIN")
    try:
        return _run_status(ctx, run_id)
    finally:
        conn.rollback()


def _run_status(ctx: Ctx, run_id: str) -> dict[str, Any]:
    conn = ctx.conn
    run = _run(conn, run_id)
    engine = _engine(ctx, run_id)
    engine_state = engine.state(run_id)
    unsettled = recovery.needs_settling(conn, run_id)

    task_terminal = sorted(machines.terminal_states("task"))
    job_terminal = sorted(machines.terminal_states("job"))
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE run_id=? GROUP BY status", (run_id,),
    ))
    pending_tasks = [dict(row) for row in conn.execute(
        "SELECT task_id, kind, role, status, attempt, parallel_group FROM tasks WHERE run_id=?"
        f" AND status NOT IN ({','.join('?' for _ in task_terminal)}) ORDER BY created_at, task_id LIMIT 60",
        (run_id, *task_terminal),
    )]
    jobs = [dict(row) for row in conn.execute(
        "SELECT job_id, status, path_id, observed_at FROM jobs WHERE run_id=?"
        f" AND status NOT IN ({','.join('?' for _ in job_terminal)}) ORDER BY created_at LIMIT 60",
        (run_id, *job_terminal),
    )]
    approvals = [
        {"approval_id": row["approval_id"], "action_kind": row["action_kind"],
         "subject": {"kind": row["subject_kind"], "id": row["subject_id"]},
         "requested_at": row["requested_at"]}
        for row in conn.execute(
            "SELECT approval_id, action_kind, subject_kind, subject_id, requested_at FROM approvals"
            " WHERE run_id=? AND status='PENDING' ORDER BY requested_at LIMIT 40", (run_id,),
        )
    ]
    gaps = [
        {"gap_id": row["gap_id"], "gap_kind": row["gap_kind"], "question": str(row["question"])[:200]}
        for row in conn.execute(
            "SELECT gap_id, gap_kind, question FROM gaps WHERE run_id=? AND status='OPEN' ORDER BY created_at LIMIT 40",
            (run_id,),
        )
    ]
    lease = store.coordinator_lease(conn, run_id)
    coordinator = None
    if lease is not None:
        coordinator = {"owner": lease["owner"], "host": lease["host"], "expires_at": lease["expires_at"],
                       "released_at": lease["released_at"], "live": store.coordinator_lease_live(lease)}
    objective = conn.execute("SELECT * FROM objectives WHERE objective_id=?", (run["objective_id"],)).fetchone() \
        if run["objective_id"] else None
    objective_view: dict[str, Any] = {"text": str(run["objective"])[:600]}
    if objective is not None:
        screen = store.loads(objective, "screen_json", {}) or {}
        objective_view.update(objective_id=objective["objective_id"], status=objective["status"],
                              screen_result=screen.get("result"), approval_id=objective["approval_id"])
    # A project may describe its scientific obligations using the same checkpoint
    # owner that rules on closure. Ordinary workflows keep their original status.
    if engine.registry.has(run["workflow_id"], str(run["workflow_version"])):
        workflow = engine.registry.get(run["workflow_id"], str(run["workflow_version"]))
        if workflow.objective_coverage is not None:
            if workflow.digest() == run["workflow_sha256"]:
                try:
                    objective_view["coverage"] = dict(workflow.objective_coverage(conn, run_id))
                except GxError as exc:
                    objective_view["coverage"] = {"allows_close": False, "unavailable": exc.message}
            else:
                objective_view["coverage"] = {"allows_close": False, "unavailable": "installed workflow differs from run pin"}
    in_doubt, doubt_warnings = _in_doubt(conn, run_id)
    result = {
        "status": run["status"], "revision": int(run["revision"]), "phase": run["phase"],
        "interrupt_ref": run["interrupt_ref"], "pending_tasks": pending_tasks,
        "external_jobs": jobs, "pending_approvals": approvals, "open_gaps": gaps,
        "budget": _budget_view(conn, run["budget_ref"]),
        "coordinator_lease": coordinator, "objective": objective_view,
        "workflow": {"id": run["workflow_id"], "version": run["workflow_version"],
                     "sha256": run["workflow_sha256"], "admission": _admission_of(conn, run_id),
                     "node": engine_state["node"],
                     "checkpoint_seq": engine_state["checkpoint_seq"],
                     "completed_nodes": engine_state["completed_nodes"]},
        "agent_policy_ref": run["agent_policy_ref"], "pins": pins_view(run),
        "paused": store.loads(run, "paused_json", None),
        "host": store.loads(run, "host_json", None) or None,
        "task_counts": by_status,
        "waiting_for": _waiting_for(conn, run, engine_state, idle=engine.idle(run_id)),
        "project_ref": run["project_ref"],
        "stage": (_stage_view(engine, run, engine_state, include_needs=True)
                  if engine_state["node"] is not None and run["status"] not in machines.terminal_states(RUN)
                  else None),
        "in_doubt": in_doubt,
        "directories": run_directories(conn, run_id),
        "warnings": (["a lease has lapsed, a submission was refused or a hold was decided since the "
                      "tasks shown here last moved; the next gx_task_claim, gx_task_submit or "
                      "gx_run_resume settles it, and gx_next_actions lists such tasks under blocked "
                      "until then"] if unsettled else []) + doubt_warnings,
    }
    waits = engine.waits(run_id)
    if waits:
        result["workflow"]["node_waits"] = {
            node: {key: wait[key] for key in ("status", "owner", "reason", "interrupt_ref", "references", "since", "next_retry_at")
                   if key in wait} for node, wait in waits.items()
        }
    if engine_state.get("failed_nodes"):
        result["workflow"]["failed_nodes"] = engine_state["failed_nodes"]
    # Frozen inputs are handed off by metadata only. Never expose their original
    # paths, arbitrary input objects or file contents through this read surface.
    inputs = store.loads(run, "inputs_json", {})
    files = inputs.get("files", []) if isinstance(inputs, Mapping) else []
    files = files if isinstance(files, list) else []
    projected = {"input_files": [], "input_files_truncated": bool(files), "input_files_omitted": len(files)}
    # Keep the addition bounded independently of the transport's spill-to-artifact
    # path, which writes and therefore must not be triggered by this status read.
    budget = min(8192, int(contracts.response_limits()["max_response_bytes"])
                 - len(ids.canonical_json(result).encode("utf-8")) - 2048)
    if len(ids.canonical_json(projected).encode("utf-8")) > budget:
        return result
    role_chars = string.ascii_letters + string.digits + "_.-"
    for item in files[:MAX_INPUT_FILES]:
        if not isinstance(item, Mapping):
            continue
        role, ref = item.get("role"), item.get("artifact_ref")
        if role is not None and (not isinstance(role, str) or not 1 <= len(role) <= 64
                                 or any(char not in role_chars for char in role)):
            continue
        if isinstance(ref, str) and len(ref) <= 128 and ids.kind_of(ref) == "artifact":
            artifact = conn.execute("SELECT sha256, bytes FROM artifacts WHERE artifact_ref=?"
                                    " AND run_id=? AND kind=? AND published=1", (ref, run_id, INPUT_KIND)).fetchone()
            if artifact is None:
                continue
            sha256, size = artifact["sha256"], artifact["bytes"]
        elif ref is None:
            # Large files were intentionally not copied at start. Preserve that
            # absence; a read cannot mint a reference or open their raw paths.
            sha256, size = item.get("sha256"), item.get("bytes")
            if type(size) is not int or not MAX_COPIED_INPUT_BYTES < size < 2 ** 63:
                continue
        else:
            continue
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256)
                or type(size) is not int or size < 0):
            continue
        row = {"role": role, "artifact_ref": ref, "sha256": sha256, "bytes": size}
        candidate = {"input_files": [*projected["input_files"], row],
                     "input_files_truncated": len(projected["input_files"]) + 1 < len(files),
                     "input_files_omitted": len(files) - len(projected["input_files"]) - 1}
        if len(ids.canonical_json(candidate).encode("utf-8")) > budget:
            break
        projected = candidate
    result["workflow"].update(projected)
    return result


# ------------------------------------------------------------------ gx_next_actions

def next_actions(ctx: Ctx, run_id: str, limit: int | None = None) -> dict[str, Any]:
    return TeamService(ctx.conn, ctx=ctx).next_actions(run_id=run_id, limit=limit)


# --------------------------------------------------------- the dispatch projection

#: How many steps a task that became ready is followed inside one dispatch (design r2.2 §8.5:
#: shipped 3, at most 8). A host's dispatch step reads it from the projection.
DISPATCH_FOLLOW_DEPTH = 3
_NONCE_ALPHABET = string.ascii_lowercase + string.digits


def _in_doubt(conn: sqlite3.Connection, run_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """gx_run_status's ``in_doubt`` (contract r5): the leases whose worker the host recorded as
    stopped, with no submission, each ``{task_id, lease_ref, why}``; and a warning when the host's
    record could not be read, so an empty list never stands for something nobody looked at."""
    try:
        stopped = seams.stopped_holders(conn, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - a status still answers; it says what it could not read
        return [], [f"the host's record of stopped workers could not be read ({type(exc).__name__}: "
                    f"{str(exc)[:200]}); in_doubt lists nothing it looked at"]
    if stopped is None:
        return [], ["the host's record of stopped workers is not installed ("
                    + seams.ON_FALLBACK.get("stopped_holders", "no owner") + "); in_doubt lists nothing it looked at"]
    return [{"task_id": item["task_id"], "lease_ref": item["lease_id"],
             "why": (f"the host recorded its agent {item.get('agent')} as stopped ({item.get('cause')}, host event "
                     f"{item.get('host_event_id')}); the lease is live and has no submission")}
            for item in stopped], []


def run_directories(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    """The run's inbox and outbox (contract r5 ``directories``), created if they are not there yet:
    a client that speaks only the protocol finds them here rather than deriving them from the store."""
    service = ArtifactService(conn)
    return {"inbox": str(service.inbox_dir(run_id, create=True)),
            "outbox": str(service.outbox_dir(run_id, create=True))}


def stage_view(ctx: Ctx, run_id: str, *, quiescent: bool | None = None) -> dict[str, Any]:
    """The open stage as a dispatch projection shows it: ``{name, complete, quiescent, resumable}``.

    Design r2.2 §2.5.4 and §10. Until a workflow has stages, the stage is the engine node
    the run stands at, and the three words are read from the function a resume uses
    (:meth:`Engine.standing`). ``complete``: every item of its pending interrupt is what
    the node waits for. ``quiescent``: it is not complete and nothing of the run is open,
    pending or waited for; a resume is then accepted and ends in a decision. ``resumable``:
    gx_run_resume would be accepted now as far as the run's own state decides -- complete,
    quiescent, or a terminal outcome to hand back that the node has not seen -- with no
    submission held and every accepted worker on the policy. Who asks, under which lease
    and at which revision, is the call's own matter. ``quiescent`` as a parameter is
    accepted for callers that computed it and is no longer needed.
    """
    run = _run(ctx.conn, run_id)
    engine = _engine(ctx, run_id)
    return _stage_view(engine, run, engine.state(run_id))


def _stage_view(engine: Engine, run: sqlite3.Row, state: Mapping[str, Any], *,
                include_needs: bool = False) -> dict[str, Any]:
    run_id = run["run_id"]
    pending = state.get("pending_interrupt") or {}
    available = engine.registry.has(run["workflow_id"], str(run["workflow_version"]))
    view = {"name": state.get("node"), "complete": False, "quiescent": False, "resumable": False}
    stage_needs = None
    if available and pending and run["status"] in machines.waiting_states(RUN):
        standing = engine.standing(run_id)
        stage_needs = standing.stage_needs
        view.update(complete=standing.complete, quiescent=standing.quiescent, resumable=standing.resumable)
        if standing.resumable:
            try:
                _require_no_held_submission(engine.conn, run_id)
                _require_workers_on_policy(engine.conn, run_id, pending)
            except (ApprovalRequired, ModelPolicyViolation):
                view["resumable"] = False
    if include_needs:
        view["needs"] = engine.waiting_needs(run_id, stage_needs=stage_needs) if available else []
    return view


NATIVE_DISPATCH_DATA_BYTES = 16 * 1024


def _bound_native_dispatch(data: dict[str, Any], *, unassigned: int, oversized: int) -> None:
    """Bound the CLI-only native projection without publishing a read-time artifact.

    Whole tail rows are withheld; a backend TaskContract is never shortened. ``gx`` is
    filled by the CLI after this call, so account for that exact path here too.
    """
    args = data["args"]
    totals = {key: len(value) for key, value in (("tasks", args["tasks"]),
                                               ("jobs_due", data["jobs_due"]),
                                               ("offered", data["offered"]))}
    meta = data["native_dispatch"] = {
        "task_contract_source": "backend_codex_dispatch", "data_byte_limit": NATIVE_DISPATCH_DATA_BYTES,
        "candidate_task_count": totals["tasks"] + oversized, "unassigned_task_count": unassigned,
        "contract_size_withheld_task_count": oversized,
        "jobs_due_count": totals["jobs_due"], "offered_count": totals["offered"],
    }
    while True:
        meta.update(returned_task_count=len(args["tasks"]),
                    complete_contract_count=sum("native_task_contract" in row for row in args["tasks"]),
                    omitted_task_count=totals["tasks"] + oversized - len(args["tasks"]),
                    omitted_jobs_due_count=totals["jobs_due"] - len(data["jobs_due"]),
                    omitted_offered_count=totals["offered"] - len(data["offered"]))
        meta["truncated"] = any(meta[key] for key in ("omitted_task_count", "omitted_jobs_due_count",
                                                    "omitted_offered_count"))
        wire = {**data, "args": {**args, "gx": str(contracts.HARNESS_ROOT / "bin" / "gx")}}
        if len(json.dumps(wire, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) <= NATIVE_DISPATCH_DATA_BYTES:
            return
        for values in (data["jobs_due"], data["offered"], args["tasks"]):
            if values:
                values.pop()
                break
        else:
            raise SchemaInvalid("the native dispatch metadata exceeds the bounded CLI response")


def dispatch_projection(ctx: Ctx, run_id: str, limit: int | None = None) -> dict[str, Any]:
    """The dispatch plan as a host's dispatch step takes it: ``gx next --for-dispatch`` (design r2.2 §8.5).

    ``args`` is what a coordinator hands its dispatch workflow unchanged. Its ``tasks`` are
    the rows that may be started now -- READY, not handed out, not withheld, cut to the
    free slots, in plan order -- as ids and settings only: no brief and no acceptance
    text, which a worker gets from its claim. On a backend-scoped Codex run, a settled
    assignment additionally carries its complete backend ``native_task_contract``;
    unassigned or pending-transfer tasks carry no marker. This is a CLI projection,
    not a field of the MCP ``gx_next_actions`` response. ``session8`` is the launch environment's
    host session (null when it shows none) and ``nonce`` a name made for this document,
    so a document launched twice is refused the second time by the host's gate. ``gx`` is
    the client's own path and is filled in by the client.

    Beside it: when the earliest live lease runs out and the earliest open job passes
    its deadline, the jobs already past theirs, the capacity, what is handed out and not
    claimed, how many open tasks are not offered, and the stage. A read: nothing is
    settled and nothing is written to the store.
    """
    from ..scheduling import dispatch, snapshot
    from ..teams.service import default_lease_seconds

    conn = ctx.conn
    run = _run(conn, run_id)
    max_rows = int(contracts.response_limits()["max_rows"])
    cap = None if limit is None else max(0, min(int(limit), max_rows))
    snap = snapshot.load(conn, run_id, now=clock.now_iso())
    plan = dispatch.plan(snap, limit=cap)
    lease_seconds = default_lease_seconds()
    rows = [{
        "task_id": row.task_id, "role": row.role, "kind": row.kind, "bundle_kind": row.bundle_kind,
        "model": row.model, "effort": row.effort, "attempt": row.attempt,
        "expected_attempt": row.expected_attempt, "lease_seconds": row.lease_seconds or lease_seconds,
        "independent_of": row.independent_of, "parallel_group": row.parallel_group,
    } for row in plan.rows]
    # Validate stored snapshot/pin semantics before deciding compatibility. Corrupt
    # scoped state must not look like an unpinned below-tool legacy fixture.
    from ..hostobs import observation
    policy_snapshot, pinned_policy_sha = observation.policy_of_run(conn, run_id)
    try:
        host_record = json.loads(run["host_json"] or "{}")
    except (ValueError, TypeError):
        host_record = {}
    selected = host_record.get("policy_selection") if isinstance(host_record, dict) else None
    retained_native_proof = isinstance(selected, dict) and selected.get("source") == "codex_native_invocation"
    native_scoped = False
    unassigned = oversized = 0
    if isinstance(policy_snapshot, dict) and any(key in policy_snapshot for key in ("policy_host", "policy_scope")):
        from ..hostobs import codex_dispatch, host_policy
        native_scoped = host_policy.scope_of_run(run) == "codex"
        if native_scoped:
            installed_policy, installed_sha = host_policy.current_for_run(run)
            if pinned_policy_sha != installed_sha or ids.canonical_json(policy_snapshot) != ids.canonical_json(installed_policy):
                raise ContentHashMismatch("the native dispatch policy changed; no automatic re-pin is allowed")
            for index, row in enumerate(rows):
                assignment = conn.execute(
                    "SELECT a.subject FROM task_assignments a JOIN teams tm ON tm.team_id=a.team_id"
                    " WHERE a.task_id=? AND tm.run_id=? AND a.status='ASSIGNED' AND a.pending_json IS NULL",
                    (row["task_id"], run_id)).fetchone()
                if assignment is None:
                    unassigned += 1
                    continue
                marker = codex_dispatch.make_task_contract(conn, run_id=run_id, task_id=row["task_id"],
                                                           principal_subject=assignment["subject"])
                if len(marker.encode("utf-8")) > 8192:
                    # Do not offer an invalid/partial marker, or later rows ahead of it.
                    oversized = len(rows) - index
                    del rows[index:]
                    break
                row["native_task_contract"] = marker
    elif retained_native_proof:
        raise ContentHashMismatch("the native dispatch Host proof has no matching pinned scoped policy")
    session = seams.launch_session()
    args: dict[str, Any] = {
        "run_id": run_id, "gx": None, "session8": session[:8] if session else None,
        "nonce": f"d{plan.as_of_event_id}-" + "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(6)),
        "revision": int(snap.run.revision), "as_of_event_id": int(plan.as_of_event_id), "tasks": rows,
        "max_tasks": int(plan.capacity.slots), "follow_depth": DISPATCH_FOLLOW_DEPTH,
        "inbox": str(ArtifactService(conn).inbox_dir(run_id, create=True)),
    }
    result = {
        "args": args,
        # When settlement next has something to do, as the plan computes it: the first live
        # lease to run out, and the first open job to pass its deadline. A coordinator with
        # nothing to launch waits until the earlier of the two. ``jobs_due`` are jobs already
        # past theirs: the next call that settles (a claim, a submit, a resume) looks at them.
        "earliest_lease_expiry": plan.earliest_lease_expiry,
        "earliest_job_deadline": plan.earliest_job_deadline,
        "jobs_due": list(plan.progress.jobs_due),
        "capacity": {"slots": int(plan.capacity.slots), "in_flight": int(plan.capacity.in_flight),
                     "offered": int(plan.capacity.offered)},
        "offered": list(plan.offered),
        "blocked": len(plan.withheld) + len(plan.waiting),
        "stage": stage_view(ctx, run_id),
    }
    if native_scoped:
        _bound_native_dispatch(result, unassigned=unassigned, oversized=oversized)
    return result


# ------------------------------------------------------------- the coordinator lease

def _coordinator_seconds(requested: int | None) -> int:
    default = int((config.limits().get("task_budget") or {}).get("coordinator_lease_seconds", 1800))
    if requested is None:
        return default
    seconds = int(requested)
    if seconds < 1:
        raise SchemaInvalid("lease_seconds is a positive number of seconds",
                            details={"lease_seconds": requested})
    return seconds


def require_coordinator(ctx: Ctx, conn: sqlite3.Connection, run_id: str, lease_ref: str | None,
                        *, operator_may: bool = False, renew: bool = True) -> sqlite3.Row | None:
    """The live coordinator lease, held by the caller -- or, where allowed, the operator.

    A lease is used by the principal it was issued to. A lease reference that turns up in
    another principal's hands is refused like one that has lapsed: it does not show who
    is coordinating.
    """
    if operator_may and ctx.principal.scope == SCOPE_OPERATOR and not lease_ref:
        return None
    row = store.require_coordinator_lease(conn, run_id=run_id, lease_ref=lease_ref,renew=renew)
    if row["owner"] != ctx.principal.subject and ctx.principal.scope != SCOPE_OPERATOR:
        raise LeaseExpired(
            f"coordinator lease {lease_ref} of run {run_id} was issued to {row['owner']!r}, "
            f"not to {ctx.principal.subject!r}",
            details={"run_id": run_id, "reason": "not_the_lease_holder", "holder": row["owner"]},
        )
    return row


def coordinator_claim(ctx: Ctx, args: Mapping[str, Any]) -> dict[str, Any]:
    """Claim, renew or release the one coordinator lease of a run."""
    conn, run_id = ctx.conn, args["run_id"]
    action = args.get("action") or "claim"
    if action not in LEASE_ACTIONS:
        raise SchemaInvalid(f"action is one of {', '.join(LEASE_ACTIONS)}",
                            details={"action": action})
    host = args["host"]
    if not contracts.is_label("host_kind", host):
        raise SchemaInvalid(f"{host!r} is not a host kind",
                            details={"host": str(host)[:64], "known": contracts.label_values("host_kind")})
    seconds = _coordinator_seconds(args.get("lease_seconds"))
    # The session a coordinator works from is where the host's record of it is read. One
    # that does not say is in the session this call came from, when the environment shows it.
    session_ref = args.get("session_ref") or seams.launch_session_ref(host)
    request = {"run_id": run_id, "host": host, "session_ref": args.get("session_ref"),
               "action": action, "reason": args.get("reason"),
               "lease_seconds": args.get("lease_seconds")}

    def work(tx: sqlite3.Connection) -> dict[str, Any]:
        run = _run(tx, run_id)
        store.check_revision(tx, run_id, args.get("expected_revision"))
        if run["status"] in machines.terminal_states(RUN) and action != "release":
            raise IllegalTransition(
                f"run {run_id} is {run['status']}; a run that has ended has no coordinator",
                details={"run_id": run_id, "status": run["status"]},
            )
        current = store.coordinator_lease(tx, run_id)
        previous = current["owner"] if current is not None else None
        owner = ctx.principal.subject
        if action == "claim":
            lease = store.acquire_coordinator_lease(
                tx, run_id=run_id, host=host, owner=owner, seconds=seconds,
                session_ref=session_ref, actor=ctx.actor,
            )
        elif action == "renew":
            if not store.coordinator_lease_live(current) or current["owner"] != owner:
                raise IllegalTransition(
                    f"{owner!r} does not hold the live coordinator lease of run {run_id}; "
                    "claim it instead of renewing it",
                    details={"run_id": run_id, "holder": previous,
                             "live": store.coordinator_lease_live(current)},
                )
            tx.execute(
                "UPDATE coordinator_leases SET renewed_at=?, expires_at=? WHERE run_id=?",
                (clock.now_iso(), clock.iso_after(seconds), run_id),
            )
            lease = store.coordinator_lease(tx, run_id)
        else:
            if current is None or current["released_at"] is not None or current["owner"] != owner:
                raise IllegalTransition(
                    f"{owner!r} does not hold the coordinator lease of run {run_id}; there is "
                    "nothing of theirs to release",
                    details={"run_id": run_id, "holder": previous},
                )
            # Releasing a lease that has already lapsed is still the holder's to do: it
            # tells the next host the run was handed over, not abandoned.
            tx.execute(
                "UPDATE coordinator_leases SET released_at=?, handoff_reason=? WHERE run_id=?",
                (clock.now_iso(), args.get("reason"), run_id),
            )
            store.append_event(
                tx, kind="run.coordinator_lease", actor=ctx.actor, run_id=run_id, subject_kind=RUN,
                subject_id=run_id, reason=args.get("reason") or "released",
                evidence_level="EXECUTION_OBSERVED",
                payload={"host": current["host"], "lease_id": current["lease_id"], "released": True},
            )
            lease = None
        prev, new = store.bump_revision(tx, run_id)
        store.append_event(
            tx, kind="run.coordinator", actor=ctx.actor, run_id=run_id, subject_kind=RUN,
            subject_id=run_id, prev_revision=prev, new_revision=new, reason=action,
            evidence_level="EXECUTION_OBSERVED",
            payload={"action": action, "host": host, "previous_owner": previous, "owner": owner},
        )
        return {
            "coordinator_lease_ref": lease["lease_id"] if lease is not None else None,
            "expires_at": lease["expires_at"] if lease is not None else None,
            "previous_owner": previous, "revision": new, "action": action, "run_id": run_id,
        }

    response, replayed = store.run_idempotent(
        conn, store.tool_key("gx_coordinator_claim", args["idempotency_key"]),
        "gx_coordinator_claim", request, work,
    )
    return {**response, "replayed": replayed}


# --------------------------------------------------------------------- gx_run_resume

def run_resume(ctx: Ctx, args: Mapping[str, Any]) -> dict[str, Any]:
    """Advance past the pending interrupt, after re-checking every premise.

    Order matters: the lease first (who may write), then the pins (is this still the
    run that was started), then what time and approvals have settled, then the engine's
    own re-validation of the interrupt.

    ``idempotency_key`` (contract r5, optional): the first answer given under it is replayed. The
    key is completed in the transaction that resumes, so a refusal leaves it unused; the answer
    is stored once the nodes after the resume have run, and a second call with the key while the
    first is still advancing waits a bounded time for it, then answers from the run as it stands.
    """
    conn, run_id = ctx.conn, args["run_id"]
    key = store.tool_key("gx_run_resume", args["idempotency_key"]) if args.get("idempotency_key") else None
    request = {name: args.get(name) for name in ("run_id", "interrupt_ref", "submission_refs", "expected_revision")}
    if key is not None and store.stored_response(conn, key) is not None:
        return _replayed_resume(ctx, key, request, before_work=True)
    run = _run(conn, run_id)
    workflow = _pinned_workflow(conn, run) \
        if run["workflow_id"] else None
    if workflow is None:
        raise SchemaInvalid(f"run {run_id} was never started on a workflow", details={"run_id": run_id})
    require_coordinator(ctx,conn,run_id,args.get("coordinator_lease_ref"),renew=False)
    verified_pins = require_pins(run, workflow)
    # The caller's view is checked against the run as it was asked about; what time and
    # decisions have settled is then settled, and stands on its own.
    store.check_revision(conn, run_id, args.get("expected_revision"))
    TeamService(conn, ctx=ctx).lazy(run_id=run_id)
    settled_at = store.current_revision(conn, run_id)
    engine = _engine(ctx, run_id)
    cpu_sample = engine.cpu_resume_sample(engine.waits(run_id).values(), args["interrupt_ref"])
    # One decision under one write lock (SCHEDULING_DESIGN r2 §2.5.3): who may resume, at which
    # revision, with nothing held, with every interrupt item cleared, and the move to ACTIVE
    # with a checkpoint that no longer waits. A refusal at any step changes nothing -- the
    # coordinator lease is renewed only by a resume that is accepted -- and of two resumes of
    # one interrupt the second finds it gone.
    begun: dict[str, Any] = {}

    def resume(tx: sqlite3.Connection) -> dict[str, Any]:
        require_coordinator(ctx, tx, run_id, args.get("coordinator_lease_ref"))
        _check_pins(_run(tx, run_id), verified_pins)
        pending = engine.pending_for(run_id, args["interrupt_ref"], conn=tx)
        _require_no_held_submission(tx, run_id, pending=pending)
        _require_workers_on_policy(tx, run_id, pending)
        begun.update(engine.begin_resume(
            tx, run_id=run_id, interrupt_ref=args["interrupt_ref"],
            submission_refs=args.get("submission_refs") or [], cpu_sample=cpu_sample,
            expected_revision=settled_at, actor=ctx.actor,
        ))
        return {"run_id": run_id, "interrupt_ref": args["interrupt_ref"]}

    if key is None:
        with store.transaction(conn) as tx:
            resume(tx)
    else:
        _first, replayed = store.run_idempotent(conn, key, "gx_run_resume", request, resume)
        if replayed:                     # another call completed the key between the look and the lock
            return _replayed_resume(ctx, key, request)
    try:
        result = engine.advance_resumed(begun)
    except Exception as exc:  # noqa: BLE001 - the resume is on record; what failed after it is reported
        answer = _after_commit(ctx, run_id, exc, acted="resume", with_waiting=True)
    else:
        answer = {
            "status": result["status"], "revision": result["revision"],
            "interrupt_ref": result["interrupt_ref"], "next_actions": result["next_actions"],
            "interrupt": result["interrupt"], "warnings": result["warnings"], "run_id": run_id,
        }
    return store.settle_answer(conn, key, answer) if key is not None else answer


def _replayed_resume(ctx: Ctx, key: str, request: Mapping[str, Any], *, before_work: bool = False) -> dict[str, Any]:
    """A resume whose key was used: the first answer under it, or IDEMPOTENCY_CONFLICT for another
    request. While the first call is still advancing, its answer is waited for a bounded time and
    then given from the run as it stands, saying so."""
    try:
        record = store.begin_idempotent(ctx.conn, key, "gx_run_resume", dict(request))
    except IdempotencyConflict as conflict:
        # The completed key was checked before settling teams, leases or workflow nodes.
        # A conflict after that boundary, or an uncertain original key, keeps its own status.
        if before_work and ctx.conn.execute(
                "SELECT 1 FROM idempotency WHERE idempotency_key=? AND scope='gx_run_resume'"
                " AND side_effect_status='APPLIED' AND completed_at IS NOT NULL", (key,)).fetchone():
            conflict.side_effect_status = "NONE"
        raise
    if not record.replay or record.scope != "gx_run_resume" or record.side_effect_status != "APPLIED":
        raise OutcomeUnknown("the earlier resume has no confirmed outcome; inspect its run and Jobs",
                             details={"idempotency_key": key, "side_effect_status": record.side_effect_status})
    said = "this idempotency key already resumed the run; the answer it was given is returned"
    answer = _await_answer(ctx.conn, key, seconds=START_ANSWER_WAIT_SECONDS)
    if answer is not None:
        return {**answer, "warnings": list(answer.get("warnings") or []) + [said]}
    run = _run(ctx.conn, request["run_id"])
    state = _engine(ctx, request["run_id"]).state(request["run_id"])
    return {"status": run["status"], "revision": int(run["revision"]), "interrupt_ref": run["interrupt_ref"],
            "next_actions": [], "interrupt": state.get("pending_interrupt"), "run_id": request["run_id"],
            "warnings": [said + "; it had not answered yet, so this is the run as it stands (gx_run_status)"]}


def proves_prior_resume_conflict(conn: sqlite3.Connection, *, project_ref: str,
                                rejected_run_id: str, rejected_arguments_sha256: str,
                                owner_run_id: str, owner_arguments_sha256: str) -> bool:
    """Match a historical cross-run key collision to its original completed Job-wait resume.

    The native journal owner supplies an earlier successful resume and the later conflict
    from the same dispatch stream. Both full argument hashes must match existing GX records.
    This read-only check settles only the later request's effect.
    """
    if rejected_run_id == owner_run_id:
        return False

    def request_sha(run_id, client_key, arguments_sha, expected_interrupt=None):
        rows = conn.execute(
            "SELECT wait.new_revision, json_extract(wait.payload_json,'$.interrupt_ref'),"
            " json_extract(lease.payload_json,'$.lease_id')"
            " FROM runs JOIN events AS wait USING(run_id) JOIN events AS lease USING(run_id)"
            " WHERE runs.run_id=? AND runs.project_ref=? AND wait.kind='run.transition'"
            " AND wait.new_state='WAITING_EXTERNAL_JOB' AND lease.kind='run.coordinator_lease'",
            (run_id, project_ref))
        for revision, interrupt_ref, lease_ref in rows:
            if not interrupt_ref or not lease_ref or (expected_interrupt and interrupt_ref != expected_interrupt):
                continue
            request = {"run_id": run_id, "expected_revision": revision,
                       "interrupt_ref": interrupt_ref, "submission_refs": []}
            arguments = {**request, "coordinator_lease_ref": lease_ref, "idempotency_key": client_key}
            if ids.sha256_of_obj(arguments) == arguments_sha:
                return ids.sha256_of_obj(request)
        return None

    prefix = store.tool_key("gx_run_resume", "")
    receipts = conn.execute(
        "SELECT idempotency_key, request_sha, json_extract(response_json,'$.interrupt_ref')"
        " FROM idempotency WHERE scope='gx_run_resume' AND side_effect_status='APPLIED' AND completed_at IS NOT NULL"
        " AND json_extract(response_json,'$.run_id')=? AND json_extract(response_json,'$.answer.run_id')=?",
        (owner_run_id, owner_run_id))
    for key, stored_sha, interrupt in receipts:
        if not key.startswith(prefix) or not interrupt:
            continue
        client_key = key[len(prefix):]
        if request_sha(owner_run_id, client_key, owner_arguments_sha256, interrupt) == stored_sha:
            rejected_sha = request_sha(rejected_run_id, client_key, rejected_arguments_sha256)
            if rejected_sha is not None and rejected_sha != stored_sha:
                return True
    return False


def _after_commit(ctx: Ctx, run_id: str, exc: Exception, *, acted: str, with_waiting: bool) -> dict[str, Any]:
    """The answer of a call whose own decision committed and whose advance then failed.

    The lead's ruling of 2026-11-12 20:04Z: the action the caller asked for happened, so the
    answer is ok, with the failure as a warning and the run's state as it now is.
    OUTCOME_UNKNOWN stays for what it means -- the dispatcher cannot tell whether a commit
    happened -- and would invite a blind retry of an action that is on record. A node that
    raised left ``workflow.node_failed`` and an idle run, which ``continue`` drives.
    """
    LOGGER.warning("%s of run %s committed; the advance after it failed", acted, run_id, exc_info=exc)
    engine = _engine(ctx, run_id)
    code = getattr(exc, "code", None) or type(exc).__name__
    message = str(getattr(exc, "message", None) or exc)[:500]
    run = _run(ctx.conn, run_id)
    waiting = _waiting_for(ctx.conn, run, engine.state(run_id), idle=engine.idle(run_id))
    if waiting is not None and waiting.get("kind") == "idle":
        then = "the run is idle, and gx_run_control action=continue runs the next node again"
    else:
        then = f"the run is {run['status']}; gx_run_status says where it stands"
    response = engine._response(
        run_id, node=(waiting or {}).get("node"), interrupt_ref=run["interrupt_ref"],
        warnings=[f"the {acted} was accepted and is on record; after it, {code}: {message}. {then}"],
    )
    out = {
        "status": response["status"], "revision": response["revision"],
        "interrupt_ref": response["interrupt_ref"], "next_actions": response["next_actions"],
        "interrupt": None, "warnings": response["warnings"], "run_id": run_id,
    }
    if with_waiting:
        out["waiting_for"] = waiting
    return out


def _require_no_held_submission(conn: sqlite3.Connection, run_id: str, *,
                                pending: Mapping[str, Any] | None = None) -> None:
    """A run does not advance while a submission of any of its tasks waits for the user.

    Not only the tasks the interrupt names: a held review, or any side task, means the run
    is waiting for the user's decision about work done outside the agent policy
    (states.json task.held, run.model_policy), and advancing would let it end with the
    decision still open and the work then accepted into an ended run.
    """
    held = recovery.pending_holds(conn, run_id)
    if not held:
        return
    if pending is None:
        checkpoint = store.latest_checkpoint(conn, run_id)
        pending = (store.loads(checkpoint, "state_json", {}) or {}).get("pending_interrupt") or {}
    if pending.get("kind")=="stage":
        held=[item for item in held if conn.execute("SELECT 1 FROM approvals WHERE approval_id=?"
            " AND action_kind IN ('model_switch','worker_refusal')",(item["approval_id"],)).fetchone()]
    if held:
        raise ApprovalRequired(
            f"run {run_id} waits for the user's decision on {len(held)} held submission(s): "
            + ", ".join(f"task {item['task_id']} (approval {item['approval_id']})" for item in held[:5])
            + "; it does not advance until each is granted or refused",
            details={"run_id": run_id,
                     "pending_holds": [{"task_id": item["task_id"], "approval_id": item["approval_id"]}
                                       for item in held[:20]]},
        )


def _require_workers_on_policy(conn: sqlite3.Connection, run_id: str, pending: Mapping[str, Any], *,
                               round_index: int | None = None) -> None:
    """No accepted submission the run is about to build on came from outside the policy.

    A submission is judged against the policy when it is handed in, and held if the
    worker is outside it. This is the later look: a host record read after acceptance
    can show a turn on another model, and a run does not advance over that without the
    user's approval for that submission.
    """
    waits = ("MISMATCH", "REFUSAL_OR_FALLBACK")
    task_ids=list(pending.get("tasks") or [])
    if pending.get("kind")=="stage":
        from . import expansion
        task_ids=[task["task_id"] for task in expansion.stage_tasks(conn,run_id,
            str((pending.get("payload") or {}).get("stage")),round_index)]
    for task_id in task_ids:
        submission = conn.execute(
            "SELECT * FROM submissions WHERE task_id=? AND result='ACCEPTED' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if submission is None or not submission["lease_id"]:
            continue
        verdicts = [row["policy_verdict"] for row in conn.execute(
            "SELECT policy_verdict FROM worker_observations WHERE task_id=? AND lease_id=?",
            (task_id, submission["lease_id"]),
        )]
        found = sorted({verdict for verdict in verdicts if verdict in waits})
        if not found:
            continue
        granted = conn.execute(
            "SELECT 1 FROM approvals WHERE action_kind='model_switch' AND subject_kind='submission'"
            " AND subject_id=? AND subject_sha256=? AND status='GRANTED'",
            (submission["submission_id"], submission["content_sha256"]),
        ).fetchone()
        if granted is None:
            raise ModelPolicyViolation(
                f"run {run_id} cannot advance: the worker of accepted submission "
                f"{submission['submission_id']} (task {task_id}) is on record as "
                f"{', '.join(found)} against the agent policy and no approval covers it",
                details={"run_id": run_id, "task_id": task_id,
                         "submission_id": submission["submission_id"], "policy_verdicts": found},
            )


# -------------------------------------------------------------------- gx_run_control

def _task_no_longer_blocked(conn: sqlite3.Connection, run: sqlite3.Row, need: Mapping[str, Any]) -> bool:
    task_id = (need.get("subject") or {}).get("id")
    row = conn.execute("SELECT status FROM tasks WHERE task_id=? AND run_id=?", (task_id, run["run_id"])).fetchone()
    return row is not None and row["status"] != "BLOCKED"


def _reaction_may_be_tried(conn: sqlite3.Connection, run: sqlite3.Row, need: Mapping[str, Any]) -> bool:
    """A parked reaction of the outbox (subject ``{kind: outbox, id}``) may be tried again.

    A reaction need about anything else (a brief that did not compile names its task) is not
    core's fact: until its owner publishes a predicate, it does not read as cleared."""
    from . import reactor   # the reactor imports the task service; no cycle at call time

    subject = need.get("subject") or {}
    if subject.get("kind") != "outbox" or not subject.get("id"):
        return False
    for item in reactor.parked(conn, run["run_id"]):
        if item["outbox_id"] == subject["id"]:
            return bool(item["cleared"])
    return True                 # it is no longer parked: consumed, or it never was


def _budget_line_has_room(conn: sqlite3.Connection, run: sqlite3.Row, need: Mapping[str, Any]) -> bool:
    subject = need.get("subject") or {}
    line = subject.get("id") or need.get("line")
    budget_ref = subject.get("budget_ref", run["budget_ref"]) if subject.get("admission") else run["budget_ref"]
    budget = seams.budget_state(conn, budget_ref) or {}
    left = (budget.get("available") or {}).get(line)
    required = need.get("required", subject.get("required"))
    if left is None or (left < float(required) if required is not None else left <= 0):
        return False
    if subject.get("admission") and required is not None:
        from .. import budgets
        return next(budgets._project_reservation_errors(conn, budget_ref, {line: required}), None) is None
    return True


def _independent_review_exists(conn: sqlite3.Connection,run: sqlite3.Row,need: Mapping[str,Any]) -> bool:
    subject=need.get("subject") or {}
    if subject.get("kind")!="submission":
        return False
    submission=conn.execute("SELECT s.content_sha256 FROM submissions s JOIN tasks t ON t.task_id=s.task_id"
        " WHERE s.submission_id=? AND t.run_id=? AND s.result='ACCEPTED'",(subject.get("id"),run["run_id"])).fetchone()
    if submission is None:
        return False
    return bool(seams.submission_review_state(conn,subject["id"],submission["content_sha256"])["met"])


def _approval_is_granted(conn: sqlite3.Connection, run: sqlite3.Row, need: Mapping[str, Any]) -> bool:
    approval_id = (need.get("subject") or {}).get("id")
    return bool(approval_id) and seams.approval_status(conn, approval_id) == "GRANTED"


def _no_hold(conn: sqlite3.Connection, run: sqlite3.Row, need: Mapping[str, Any]) -> bool:
    try:
        config.check_holds("orchestration")
    except HoldActive:
        return False
    return True


def _pins_hold(conn: sqlite3.Connection, run: sqlite3.Row, need: Mapping[str, Any]) -> bool:
    try:
        require_pins(run, _pinned_workflow(conn, run))
    except GxError:
        return False
    return True


#: Kinds whose predicate the exec stream publishes. They may read the device or the
#: filesystem, so they are asked before the continue's transaction, never inside it.
EXEC_NEED_KINDS = ("readiness", "device_capacity", "path_reachability")


def _needs_asked_outside(conn: sqlite3.Connection, run: sqlite3.Row) -> dict[str, dict[str, Any]]:
    """The answers of the predicates that may not run under the write lock, keyed by the need."""
    answers: dict[str, dict[str, Any]] = {}
    paused = store.loads(run, "paused_json", {}) or {}
    for need in paused.get("needs") or []:
        if not isinstance(need, Mapping) or need.get("kind") not in EXEC_NEED_KINDS:
            continue
        try:
            answer = seams.exec_need_cleared(conn, need)
        except Exception as exc:  # noqa: BLE001 - a predicate that cannot answer has not said "cleared"
            answer = {"cleared": False, "why": f"{type(exc).__name__}: {exc}"[:300]}
        answers[ids.canonical_json(dict(need))] = answer or {
            "cleared": False, "why": "the exec stream's predicate is not installed"}
    return answers


#: Need kind -> its clearing predicate (design r2.2 §2.8.2: one per kind, published by the
#: stream that holds the fact). Core's own, the two it reaches through a seam, and
#: ``host_quota``, which a person clears by continuing; exec's three are asked outside the
#: lock (:func:`_needs_asked_outside`). A kind that is in neither has no predicate core can
#: call yet: a pause on it is not lifted by ``continue``, and says so.
NEED_CLEARED: dict[str, Any] = {
    "task_blocked": _task_no_longer_blocked,
    "reaction": _reaction_may_be_tried,
    "budget_line": _budget_line_has_room,
    "approval": _approval_is_granted,
    "hold": _no_hold,
    "pin": _pins_hold,
    "host_quota": lambda conn, run, need: True,
    "independent_review": _independent_review_exists,
}


def _pause_needs(conn: sqlite3.Connection, run: sqlite3.Row,
                 outside: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """What still stands between a paused run and continuing. Empty means it may.

    Every need the pause recorded is asked its clearing predicate. A pause on a spent
    budget line made before needs had a vocabulary names its lines under ``exhausted``;
    those are read as before. A hold that stops the engine stands whatever was recorded.
    """
    needs: list[dict[str, Any]] = []
    paused = store.loads(run, "paused_json", {}) or {}
    exhausted = paused.get("exhausted") or []
    if exhausted:
        from ..p0.waits import is_p0_run
        if is_p0_run(run) and paused.get("paused_by") == "backend":
            # Only the previous engine's unconditional pre-node snapshot had all
            # three fields. A specific operation refusal or human pause stays intact.
            edge = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='run.transition'"
                                " AND new_state='PAUSED' ORDER BY event_id DESC LIMIT 1", (run["run_id"],)).fetchone()
            before_node = store.loads(edge, "payload_json", {})
            if (paused.get("reason") == "budget exhausted: " + ", ".join(exhausted)
                    and isinstance(before_node.get("node"), str) and before_node.get("exhausted") == exhausted
                    and isinstance(before_node.get("budget"), dict)
                    and before_node.get("node") == (paused.get("continue_from") or {}).get("node")):
                exhausted = []
    for key in exhausted:
        budget = seams.budget_state(conn, run["budget_ref"]) or {}
        left = (budget.get("available") or {}).get(key)
        if left is None or left <= 0:
            needs.append({"kind": "budget", "line": key,
                          "how": "raise the line under a budget_increase approval"})
    for need in paused.get("needs") or []:
        kind = need.get("kind") if isinstance(need, Mapping) else None
        if kind in (None, "budget"):
            continue                # the line form above, already read from ``exhausted``
        predicate = NEED_CLEARED.get(kind)
        if kind in EXEC_NEED_KINDS:
            answer = (outside or {}).get(ids.canonical_json(dict(need))) or {
                "cleared": False, "why": "its predicate was not asked before this call"}
            if not answer.get("cleared"):
                needs.append({**dict(need), "how": str(answer.get("why") or need.get("what") or "")})
        elif predicate is None:
            needs.append({**dict(need), "how": f"no predicate for a need of kind {kind} is published to core "
                                               "yet; the run is cancelled, or continued once it is"})
        elif not predicate(conn, run, need):
            needs.append({**dict(need), "how": str(need.get("what") or "")})
    if not any(item.get("kind") == "hold" for item in needs):
        try:
            config.check_holds("orchestration")
        except HoldActive as exc:
            needs.append({"kind": "hold", "how": "the hold in config/limits.json is lifted",
                          "detail": exc.message})
    return needs


def _cancel_cleanup_authority(ctx: Ctx, args: Mapping[str, Any]) -> None:
    """Replays may finish cancellation, but cached success grants no authority."""
    if ctx.native_call is not None:
        from ..hostobs.native_context import call_for_ctx

        call = call_for_ctx(ctx)
        if call.thread_id != call.root_thread_id:
            raise PermissionDenied("only the actual native root finishes a run cancellation")
        # API authorization already checked this outstanding callback against the
        # run's original root/session/project and pinned coordinator policy. An
        # ended run cannot acquire a replacement coordinator lease.
        return
    require_coordinator(ctx, ctx.conn, args["run_id"],
                        None if ctx.principal.scope == SCOPE_OPERATOR else args.get("coordinator_lease_ref"),
                        operator_may=True, renew=False)


def _finish_cancel(ctx: Ctx, args: Mapping[str, Any], reason: str) -> dict[str, int]:
    """After the Job owner has been asked, close only unused task-side work."""
    from ..governance import approvals
    from ..registry import Subject

    conn, run_id = ctx.conn, args["run_id"]
    with store.transaction(conn):
        _cancel_cleanup_authority(ctx, args)
        if _run(conn, run_id)["status"] != "CANCELLED":
            raise IllegalTransition("cancellation cleanup requires the recorded CANCELLED run")
        cancelled = TeamService(conn, ctx=ctx).cancel_open(conn, run_id=run_id, actor=ctx.actor, reason=reason)
        released = []
        for budget in conn.execute("SELECT budget_ref FROM budgets WHERE run_id=? ORDER BY rowid DESC", (run_id,)).fetchall():
            state = seams.budget_state(conn, budget["budget_ref"]) or {}
            for held in reversed(state.get("reservations") or []):
                ref = held.get("reservation_ref")
                if held.get("status") != "HELD" or not ref or held.get("subject_kind") == "job":
                    continue
                safe = held.get("subject_kind") == "chain_follower"
                if held.get("subject_kind") == "task":
                    safe = conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND run_id=? AND status='CANCELLED'",
                                        (held["subject_id"], run_id)).fetchone() is not None
                if ref.startswith("fund:"):
                    safe = conn.execute("SELECT 1 FROM budgets WHERE budget_ref=? AND parent_ref=? AND run_id=?",
                                        (ref[len("fund:"):], budget["budget_ref"], run_id)).fetchone() is not None
                if not safe:
                    # All Job holds, including UNKNOWN and missing-job records,
                    # remain with JobService. A funding hold stays while anything
                    # inside its child is HELD, as the original budget owner checks.
                    continue
                try:
                    seams.release_reservation(conn, ref, actor=ctx.actor, reason=reason)
                except GxError:
                    continue
                released.append(ref)
        expired = []
        for row in conn.execute(
                "SELECT a.*,t.hold_json FROM approvals a JOIN tasks t ON t.task_id=a.subject_id"
                " WHERE a.run_id=? AND t.run_id=? AND a.subject_kind='task' AND t.status='CANCELLED'"
                " AND a.action_kind='worker_refusal' AND a.status='PENDING'", (run_id, run_id)).fetchall():
            hold = store.loads(row, "hold_json", {}) or {}
            if hold.get("approval_id") != row["approval_id"] or hold.get("subject_sha256") != row["subject_sha256"]:
                continue
            approvals.invalidate(conn, Subject(kind="task", id=row["subject_id"], sha256=row["subject_sha256"]),
                                 approval_id=row["approval_id"], actor=ctx.actor, reason=reason,
                                 decided_by="run_cancelled")
            expired.append(row["approval_id"])
        if cancelled or released or expired:
            store.append_event(conn, kind="run.cancel_cleanup", actor=ctx.actor, run_id=run_id,
                               subject_kind=RUN, subject_id=run_id, reason=reason,
                               evidence_level="EXECUTION_OBSERVED",
                               payload={"cancelled_tasks": cancelled, "released_holds": released,
                                        "expired_worker_refusals": expired, "decided_by": "backend"})
    return {"cancelled_tasks": len(cancelled), "released_holds": len(released), "expired_worker_refusals": len(expired)}


def _maintenance_control(ctx: Ctx, args: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Thin current-run entry to the existing budget and workflow-pin owners."""
    from .. import budgets
    from . import repin

    run_id, action = args["run_id"], args["action"]
    repin.require_native_root(ctx, _run(ctx.conn, run_id))
    require_coordinator(ctx, ctx.conn, run_id, args.get("coordinator_lease_ref"), renew=False)
    request = {"run_id": run_id, "action": action, "reason": reason}

    def work(tx: sqlite3.Connection) -> dict[str, Any]:
        current = _run(tx, run_id)
        child = ctx.child(conn=tx)
        repin.require_native_root(child, current)
        require_coordinator(child, tx, run_id, args.get("coordinator_lease_ref"))
        store.check_revision(tx, run_id, args.get("expected_revision"))
        if current["status"] in machines.terminal_states(RUN):
            raise IllegalTransition(f"run {run_id} is already {current['status']}")
        result: dict[str, Any] = {"recovery_point": None, "needs": [], "cancelled_tasks": [],
                                  "cancel_requested_jobs": [], "run_id": run_id, "action": action}
        if action == "budget_apply":
            budget_ref = current["budget_ref"]
            budget = tx.execute("SELECT * FROM budgets WHERE budget_ref=? AND run_id=? AND parent_ref IS NULL",
                                (budget_ref, run_id)).fetchone()
            if budget is None:
                raise SchemaInvalid("the run does not name its original root budget")
            profile_ref = budgets.profile_of(tx, budget_ref)
            if profile_ref is None:
                raise SchemaInvalid("the original root budget records no reusable profile")
            before = budgets.state(tx, budget_ref)
            ceilings = budgets.profile(profile_ref)["limits"]
            increases = {key: limit for key, limit in ceilings.items()
                         if before["limits"].get(key) is None or before["limits"][key] < limit}
            for key, limit in sorted(increases.items()):
                subject = budgets.increase_subject(budget_ref, key, limit)
                allowed, _, reasons = budgets.increase_condition(tx, subject, {"key": key, "limit": limit})
                if not allowed:
                    raise ApprovalRequired("the requested line is outside the current reusable budget profile",
                                           details={"budget_ref": budget_ref, "reasons": reasons})
                raised = budgets.request_increase(child, budget_ref, key=key, limit=limit, reason=reason)
                if raised["status"] != "GRANTED":
                    raise ApprovalRequired("the original budget owner did not grant this line",
                                           details={"approval_id": raised["approval_id"]})
                budgets.raise_limit(tx, budget_ref, key=key, limit=limit,
                    approval_id=raised["approval_id"], actor=ctx.actor)
            if any(not budgets.increase_condition(tx, budgets.increase_subject(budget_ref, key, limit),
                    {"key": key, "limit": limit})[0] for key, limit in increases.items()):
                raise ContentHashMismatch("the reusable budget ceiling changed during the adjustment")
            if increases:
                store.bump_revision(tx, run_id)
            result["budget"] = budgets.state(tx, budget_ref)
        else:
            workflow = _pinned_workflow(tx, current)
            result["pins"] = {"old": run_pins(current)}
            current = repin.ensure(child, current, workflow, reason=reason)
            result["pins"]["new"] = run_pins(current)
        fresh = _run(tx, run_id)
        return {**result, "status": fresh["status"], "revision": int(fresh["revision"])}

    response, replayed = store.run_idempotent(ctx.conn,
        store.tool_key("gx_run_control", args["idempotency_key"]), "gx_run_control", request, work)
    return {**response, "replayed": replayed}


def run_control(ctx: Ctx, args: Mapping[str, Any], *, defer_advance: bool = False) -> dict[str, Any]:
    """Control a run, or deliberately adjust its budget or compatible workflow pins."""
    from ..p0 import waits
    conn, run_id = ctx.conn, args["run_id"]
    action = args["action"]
    if action not in CONTROL_ACTIONS:
        raise SchemaInvalid(f"action is one of {', '.join(CONTROL_ACTIONS)}", details={"action": action})
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise SchemaInvalid("run control requires a stated reason",
                            details={"action": action})
    if action in ("budget_apply", "workflow_repin"):
        return _maintenance_control(ctx, args, reason)
    request = {"run_id": run_id, "action": action, "reason": reason}
    if action=="continue":
        run=_run(conn,run_id)
        require_coordinator(ctx,conn,run_id,args.get("coordinator_lease_ref"),operator_may=True,renew=False)
        if run["workflow_id"]:
            try:
                workflow = _pinned_workflow(conn, run)
            except VersionImmutable as exc:
                raise DependencyUnmet("the run's exact approved workflow binding must be restored before continue",
                                      details=exc.details) from exc
            require_pins(run,workflow)
    # Predicates that read the device or the filesystem are asked here, before the write lock.
    asked_outside = _needs_asked_outside(conn, _run(conn, run_id)) if action == "continue" else {}

    def work(tx: sqlite3.Connection) -> dict[str, Any]:
        run = _run(tx, run_id)
        require_coordinator(ctx, tx, run_id, args.get("coordinator_lease_ref"), operator_may=True)
        status = run["status"]
        cancelled: list[str] = []
        cancel_requested: list[str] = []
        needs: list[dict[str, Any]] = []
        recovery_point = None
        engine = _engine(ctx.child(conn=tx), run_id)
        engine_state = engine.state(run_id)
        point = {"status": status, "node": engine_state["node"],
                 "interrupt_ref": run["interrupt_ref"], "checkpoint_seq": engine_state["checkpoint_seq"]}

        if action == "pause":
            store.transition(
                tx, machine=RUN, subject_id=run_id, to_state="PAUSED", actor=ctx.actor,
                expected_revision=args.get("expected_revision"), reason=reason,
                evidence_level="EXECUTION_OBSERVED",
                extra_updates={"paused_json": {"reason": reason, "paused_by": ctx.actor,
                                               "at": clock.now_iso(), "continue_from": point}},
                payload={"action": "pause", "continue_from": point},
            )
            recovery_point = point
        elif action == "continue" and (
                (status == "ACTIVE" and not engine_state["pending_interrupt"])
                or (status in machines.waiting_states(RUN) and engine.idle(run_id))):
            # Not paused: left idle by a node that raised or a process that died between two
            # nodes. There is no state to change; the run is driven on after this transaction.
            # A run another call is still advancing is not idle, and is not driven twice.
            if not engine.idle(run_id):
                # Contract r5: CONCURRENT_RUN while another call is advancing the run.
                raise ConcurrentRun(
                    f"run {run_id} is ACTIVE and another call is advancing it; there is nothing to "
                    "continue until that call ends or its advance lease lapses",
                    details={"run_id": run_id, "status": status},
                )
            store.check_revision(tx, run_id, args.get("expected_revision"))
            recovery_point = point
            store.append_event(
                tx, kind="run.continued_from_idle", actor=ctx.actor, run_id=run_id, subject_kind=RUN,
                subject_id=run_id, reason=reason, evidence_level="EXECUTION_OBSERVED",
                payload={"continue_from": point},
            )
        elif action == "continue":
            if status != "PAUSED":
                raise IllegalTransition(
                    f"run {run_id} is {status}, not PAUSED; there is nothing to continue"
                    + ("; a waiting run continues with gx_run_resume"
                       if status in machines.waiting_states(RUN) else ""),
                    details={"run_id": run_id, "status": status},
                )
            needs = _pause_needs(tx, run, asked_outside)
            if needs:
                # Contract r5 run.rules.paused: refused DEPENDENCY_UNMET with the needs still open,
                # whatever their kind (a spent budget line included).
                raise DependencyUnmet(
                    f"run {run_id} cannot continue yet: " + "; ".join(
                        f"{item.get('kind')}: {item.get('how') or item.get('what')}" for item in needs)[:700],
                    details={"run_id": run_id, "needs": needs},
                )
            paused = store.loads(run, "paused_json", {}) or {}
            recovery_point = paused.get("continue_from") or {}
            target = recovery_point.get("status") or "ACTIVE"
            hops = [target] if machines.can_transition(RUN, "PAUSED", target) else ["ACTIVE", target]
            expected = args.get("expected_revision")
            for hop in hops:
                store.transition(
                    tx, machine=RUN, subject_id=run_id, to_state=hop, actor=ctx.actor,
                    expected_revision=expected, reason=reason, evidence_level="EXECUTION_OBSERVED",
                    extra_updates={"paused_json": None},
                    payload={"action": "continue", "continue_from": recovery_point},
                )
                expected = None
        else:
            if status in machines.terminal_states(RUN):
                raise IllegalTransition(f"run {run_id} is already {status}",
                                        details={"run_id": run_id, "status": status})
            cancelled = TeamService(tx, ctx=ctx.child(conn=tx)).cancel_open(
                tx, run_id=run_id, reason=f"run cancelled: {reason}", actor=ctx.actor)
            cancel_requested = [
                {"job_id": job["job_id"], "status": job["status"]}
                for job in tx.execute("SELECT job_id, status FROM jobs WHERE run_id=? ORDER BY created_at, job_id",
                                      (run_id,))
                if job["status"] not in machines.terminal_states("job")
            ]
            store.transition(
                tx, machine=RUN, subject_id=run_id, to_state="CANCELLED", actor=ctx.actor,
                expected_revision=args.get("expected_revision"), reason=reason,
                evidence_level="EXECUTION_OBSERVED", extra_updates={"interrupt_ref": None},
                payload={"action": "cancel", "cancelled_tasks": cancelled,
                         "cancel_requested_jobs": [item["job_id"] for item in cancel_requested],
                         "was": point},
            )
        fresh = _run(tx, run_id)
        return {"status": fresh["status"], "revision": int(fresh["revision"]),
                "recovery_point": recovery_point, "needs": needs, "cancelled_tasks": cancelled,
                "cancel_requested_jobs": cancel_requested, "run_id": run_id, "action": action}

    response, replayed = store.run_idempotent(
        conn, store.tool_key("gx_run_control", args["idempotency_key"]), "gx_run_control",
        request, work,
    )
    response = {**response, "replayed": replayed}
    if action == "pause" and not waits.is_p0_run(_run(conn, run_id)):
        # Jobs of the run whose tool has not started would start while it is paused: they are
        # ended, after the pause has committed and outside any transaction. A job that is
        # running is left to finish. A replay asks again for whatever is still unstarted.
        try:
            held = seams.hold_run_jobs(conn, run_id=run_id, reason=f"run paused: {reason}", actor=ctx.actor)
        except Exception as exc:  # noqa: BLE001 - the pause is on record; the jobs are reported as not asked
            held = None
            response["warnings"] = list(response.get("warnings") or []) + [
                f"the run is paused; its unstarted jobs could not be asked to end ({type(exc).__name__}: "
                f"{str(exc)[:200]})"]
        if held:
            response["warnings"] = list(response.get("warnings") or []) + [
                f"{len(held)} job(s) of the run whose tool had not started were asked to end: "
                + ", ".join(str(item.get("job_id")) for item in held[:10])]
    if action == "cancel":
        _cancel_cleanup_authority(ctx, args)
        # The jobs are asked after the run's cancellation has committed and outside any
        # transaction: the job service looks at each runner, and one that does not answer
        # holds nothing up. A request is not a cancellation; each answer says which it was.
        # A replay asks again for whatever has still not ended. The answers replace the list
        # of what was open at the cancellation, under the same declared key: one entry per job,
        # with the state the job service left it in.
        answers = seams.cancel_run_jobs(conn, run_id=run_id, reason=f"run cancelled: {reason}",
                                        actor=ctx.actor)
        if answers is None:
            response["warnings"] = list(response.get("warnings") or []) + [
                "no job service is installed: the run's open jobs were listed and nobody was asked to end them"
            ] if response.get("cancel_requested_jobs") else list(response.get("warnings") or [])
        else:
            def cancel_note(item: Mapping[str, Any]) -> str:
                # A job whose cancel raised is answered with status null and the error (the job
                # service, audit J1 residual); the note says so, under no new key.
                error = item.get("error")
                if error:
                    code = error.get("code") if isinstance(error, Mapping) else None
                    message = error.get("message") if isinstance(error, Mapping) else error
                    return f"the cancel raised {code or 'an error'}: {message}"[:200]
                return str(item.get("note") or "")[:200]

            response["cancel_requested_jobs"] = [
                {"job_id": item.get("job_id"), "status": item.get("status"),
                 "note": cancel_note(item)} for item in answers[:60]]
            if len(answers) > 60:
                response["warnings"] = list(response.get("warnings") or []) + [
                    f"{len(answers)} jobs were asked to end; the first 60 are listed and gx_run_status "
                    "lists those still open"]
        finished = _finish_cancel(ctx, args, f"run cancelled: {reason}")
        response["revision"] = store.current_revision(conn, run_id)
        if any(finished.values()):
            response["warnings"] = list(response.get("warnings") or []) + [
                f"cancellation cleanup: {finished['cancelled_tasks']} remaining task(s) cancelled, "
                f"{finished['released_holds']} unused hold(s) released, "
                f"{finished['expired_worker_refusals']} obsolete worker refusal(s) withdrawn"]
    if action == "continue" and not defer_advance:
        from ..p0 import v6_goal
        if v6_goal.is_goal(_run(conn, run_id)):
            _engine(ctx, run_id).advance(run_id=run_id, actor=ctx.actor)
            return response
        # A run that came back to ACTIVE with nothing pending has no interrupt for anyone to
        # resume: it is driven on here, after the state change has committed, so a node never
        # runs inside the control call's transaction. A replay finds the run already moved on,
        # or still idle if the first call died here, and then finishes the job.
        engine = _engine(ctx, run_id)
        with store.transaction(conn) as tx:
            begun = None
            if engine.idle(run_id):
                require_coordinator(ctx, tx, run_id, args.get("coordinator_lease_ref"), operator_may=True, renew=False)
                _require_no_held_submission(tx, run_id)
                begun = engine.begin_advance(tx, run_id=run_id, actor=ctx.actor)
        if begun:
            try:
                advanced = engine.advance_resumed(begun)
            except Exception as exc:  # noqa: BLE001 - the continue is on record; see _after_commit
                # gx_run_control declares no waiting_for, so the state is in the warning only.
                advanced = _after_commit(ctx, run_id, exc, acted="continue", with_waiting=False)
            response.update(status=advanced["status"], revision=advanced["revision"],
                            interrupt_ref=advanced["interrupt_ref"], warnings=advanced["warnings"])
    return response


__all__ = [
    "CONTROL_ACTIONS", "DISPATCH_FOLLOW_DEPTH", "InterruptRequest", "LEASE_ACTIONS", "coordinator_claim",
    "default_registry", "default_workflow_ref", "dispatch_projection", "installed_pins", "next_actions",
    "require_coordinator", "require_pins", "run_control", "run_pins", "run_resume",
    "run_start", "run_status", "set_registry", "stage_view",
]
