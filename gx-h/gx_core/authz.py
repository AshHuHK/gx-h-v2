"""What a caller may reach, re-derived from the backend's own state on every call.

Used by the one dispatcher (:mod:`gx_core.api`) for both transports. The rules:

* **Identity and scope come from how the caller was launched**, never from the request:
  the MCP server's environment, or the command-line client's environment and options.
  :class:`gx_core.context.Principal` is the one definition. A request field named
  ``role``, ``scope`` or ``approved`` never reaches a handler -- the schema step drops
  every undeclared argument -- and would change nothing here if it did.
* **Four scopes.** ``worker`` (a model doing a task) calls the worker tools.
  ``readonly`` calls the worker tools that change nothing. ``host_adapter`` (a hook the
  host runs, never the model) calls the two host tools and may read. ``operator`` (a
  person) calls the operator tools and the worker tools. An operator or host tool is
  refused under any scope but its own.
* **Artifacts are reachable by reference only.** A path, a ``..`` segment or a URL in
  an argument that carries an artifact reference is refused by shape, before any lookup:
  as ``PATH_TRAVERSAL_DENIED`` by a tool that declares that code, and as the format
  error it also is (``SCHEMA_INVALID``) by one that does not, so that no tool answers
  with a code its entry in the contract does not list.
* **A project boundary is a filter, not isolation.** Every call is tied to its run where
  one can be found -- from ``run_id``, or through a task, a lease, a job or an artifact
  -- and a run in a project outside the principal's list is refused. A ``project_ref``
  argument is a project and is compared with that list as written. A ``project_scope``
  argument names namespaces and is read by the module that defines them
  (:mod:`gx_core.knowledge.scopes`), the same two functions the tool handlers use, so
  this pass and a handler cannot disagree about what was asked for.

What this module does not claim: on one machine, under one user, a shell can name any
scope and open the store directly. The scope is a procedural boundary, every record says
which scope and source wrote it, and what a scope cannot buy is a person's decision
(``contracts/mcp_tools.json`` → ``transports.person``).

It decides *whether a tool may be attempted*. Whether the attempt succeeds -- the lease
is live, the revision matches, the approval is granted, the budget has room -- belongs to
the service that owns that state, which raises a code the tool declares. v1 also refused
here on any pending approval and any exhausted budget line, which would have stopped a
user from pausing or cancelling exactly the runs that most need it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import config, contracts
from .context import (
    ENV_PROJECTS,
    ENV_SCOPE,
    ENV_SUBJECT,
    SCOPE_HOST_ADAPTER,
    SCOPE_OPERATOR,
    SCOPE_READONLY,
    SCOPE_WORKER,
    SCOPES,
    Principal,
    principal_from_env,
)
from .domain import ids, machines
from .errors import (
    ArtifactNotFound,
    IllegalTransition,
    ImplementationGap,
    PathTraversalDenied,
    PermissionDenied,
    SchemaInvalid,
    ScopeDenied,
)
from .persistence import store

#: Argument names that carry an artifact reference and must pass the reference check.
_ARTIFACT_REF_ARGS = ("artifact_ref", "artifact_refs", "input_manifest_ref", "bundle_ref")
#: The argument that names one project, compared with the principal's list as written.
_PROJECT_REF_ARG = "project_ref"
#: The argument that names namespaces to look in, read by ``gx_core.knowledge.scopes``.
_PROJECT_SCOPE_ARG = "project_scope"

_ID_ALPHABET = re.escape(ids._B32)  # the kernel's own alphabet, so the two cannot drift
_ARTIFACT_PREFIX = ids.PREFIXES["artifact"]
_ARTIFACT_REF_RE = re.compile(rf"^{re.escape(_ARTIFACT_PREFIX)}_[{_ID_ALPHABET}]{{8,40}}$")


# ------------------------------------------------------------------------- tool sets

def _replays_a_resume(conn: Any, args: Mapping[str, Any]) -> bool:
    """A gx_run_resume whose idempotency key already has an answer (contract r5): it is answered
    with that answer, even though the run it resumed has since ended."""
    key = args.get("idempotency_key")
    return bool(key) and isinstance(key, str) and \
        store.stored_response(conn, store.tool_key("gx_run_resume", key)) is not None


def privileged_names() -> frozenset[str]:
    return frozenset(contracts.privileged_tool_names())


def tool_scope(tool: str) -> str:
    """The scope a tool belongs to: ``worker`` for a worker tool, else the one it names."""
    if tool in privileged_names():
        return contracts.privileged_tool_spec(tool)["scope"]
    contracts.tool_spec(tool)  # KeyError for a name the contract does not define
    return SCOPE_WORKER


def is_mutating(tool: str) -> bool:
    return bool(contracts.any_tool_spec(tool).get("mutating"))


def may_call(tool: str, principal: Principal) -> tuple[bool, str]:
    """``(allowed, reason)`` for one tool under one principal. No state is read."""
    scope = tool_scope(tool)
    if scope == SCOPE_WORKER:
        if principal.scope in (SCOPE_WORKER, SCOPE_OPERATOR):
            return True, ""
        if is_mutating(tool):
            return False, (
                f"{tool} changes state and the {principal.scope} scope only reads"
            )
        return True, ""
    if principal.scope == scope:
        return True, ""
    who = "a person at the operator surface" if scope == SCOPE_OPERATOR else "a hook the host runs"
    return False, (
        f"{tool} belongs to the {scope} scope ({who}) and is refused under the "
        f"{principal.scope} scope"
    )


def require_scope(tool: str, principal: Principal) -> None:
    """Refuse a tool that belongs to another scope. Reads no state and no argument.

    The dispatcher asks this before it opens the store or looks at what was sent: a caller
    that may never call a tool is told so, whatever its arguments were, instead of being
    sent off to repair them for a call that would be refused anyway.
    """
    allowed, reason = may_call(tool, principal)
    if not allowed:
        raise PermissionDenied(
            reason, details={"tool": tool, "scope": principal.scope, "tool_scope": tool_scope(tool)}
        )


def visible_tools(principal: Principal, *, names: Iterable[str] | None = None) -> list[str]:
    """The tools this principal may attempt, for ``tools/list`` and ``gx tools``.

    A tool that would always be refused is not advertised: to a model, a listed tool
    that refuses reads like an invitation to try again differently.
    """
    known = set(contracts.tool_names()) | set(contracts.privileged_tool_names())
    if names is not None:
        known.intersection_update(names)
    return sorted(name for name in known if may_call(name, principal)[0])


def declares_error(tool: str, code: str) -> bool:
    return code in (contracts.any_tool_spec(tool).get("errors") or [])


# ------------------------------------------------------------------------- artifacts

def check_artifact_ref(ref: Any, *, argument: str) -> str:
    """Refuse anything that is not an opaque artifact id.

    The refusal is by *shape*, before any lookup: a path never gets as far as being
    resolved, so there is no window in which a traversal string is passed to the
    filesystem by a code path that forgot to check.
    """
    if not isinstance(ref, str) or not ref:
        raise PathTraversalDenied(
            f"{argument} must be an artifact reference string",
            details={"argument": argument, "value_type": type(ref).__name__},
        )
    looks_like_path = (
        ref.startswith("/")
        or ref.startswith("~")
        or ref.startswith("\\")
        or "/" in ref
        or "\\" in ref
        or ".." in ref
        or "://" in ref
        or ref.startswith("file:")
        or "\x00" in ref
    )
    if looks_like_path or not _ARTIFACT_REF_RE.match(ref):
        raise PathTraversalDenied(
            f"{argument}={ref!r} is not an artifact reference; artifacts are reachable by "
            f"reference only (expected {_ARTIFACT_PREFIX}_<id>), never by path and never by "
            "the name of a kind",
            details={"argument": argument, "expected_prefix": _ARTIFACT_PREFIX},
        )
    return ref


def assert_within(root: Path | str, candidate: Path | str) -> Path:
    """Resolve ``candidate`` and refuse if it leaves ``root``.

    ``Path.resolve`` follows symlinks, which is the point: a link planted inside the
    artifact root that points at ``/etc`` is an escape even though every string
    involved looks relative.
    """
    root_resolved = Path(root).resolve()
    target = Path(candidate)
    if target.is_absolute():
        raise PathTraversalDenied(
            f"absolute paths are refused: {str(target)!r}", details={"root": str(root_resolved)}
        )
    resolved = (root_resolved / target).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathTraversalDenied(
            f"{str(candidate)!r} resolves to {str(resolved)!r}, outside the artifact root",
            details={"root": str(root_resolved), "resolved": str(resolved)},
        )
    return resolved


# -------------------------------------------------------------------- run resolution

def _one(conn: sqlite3.Connection, sql: str, value: str) -> str | None:
    row = conn.execute(sql, (value,)).fetchone()
    return row[0] if row is not None else None


def resolve_run_id(conn: sqlite3.Connection, args: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(run_id, via)`` for a call, from the first argument that leads to a run.

    A worker's calls name a task, a lease, a job or an artifact, not a run. Tying each
    of them back to its run is what lets the project filter apply to them and lets every
    answer say which run and revision it is about.
    """
    value = args.get("run_id")
    if isinstance(value, str) and value:
        return value, "run_id"
    lookups = (
        ("task_id", "SELECT run_id FROM tasks WHERE task_id=?"),
        ("lease_ref", "SELECT t.run_id FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?"),
        ("job_id", "SELECT run_id FROM jobs WHERE job_id=?"),
        ("artifact_ref", "SELECT run_id FROM artifacts WHERE artifact_ref=?"),
        ("bundle_ref", "SELECT run_id FROM artifacts WHERE artifact_ref=?"),
        ("approval_id", "SELECT run_id FROM approvals WHERE approval_id=?"),
    )
    for name, sql in lookups:
        value = args.get(name)
        if isinstance(value, str) and value:
            found = _one(conn, sql, value)
            if found:
                return found, name
    return None, None


# ----------------------------------------------------------------- project boundary

def check_project_scope(principal: Principal, value: Any) -> list[str]:
    """Refuse a ``project_scope`` that names a namespace this principal was not started for.

    Returns the namespaces it names. One reading of the argument, the knowledge service's:
    a bare project is that project's own namespace, several are separated by commas, and
    the shared library is readable by every principal. v2's first cut compared the whole
    string with the principal's project list, which refused ``real:<its own project>`` and
    every scope that named the library beside it.
    """
    try:
        from .knowledge import scopes  # lazy: listing tools loads no service
    except ImportError as exc:
        raise ImplementationGap(
            f"{_PROJECT_SCOPE_ARG} names namespaces and the module that reads them "
            "(gx_core.knowledge.scopes) is not installed",
            details={"argument": _PROJECT_SCOPE_ARG, "module": "gx_core.knowledge.scopes",
                     "reason": str(exc)},
        ) from exc
    named = scopes.from_argument(value)
    try:
        scopes.check_principal(principal, named)
    except ScopeDenied as exc:
        raise ScopeDenied(
            exc.message,
            details={**dict(getattr(exc, "details", None) or {}),
                     "argument": _PROJECT_SCOPE_ARG, "requested": value,
                     "allowed": list(principal.project_refs)},
        ) from exc
    return named


# ----------------------------------------------------------------------- the checks

def authorize(
    tool: str,
    principal: Principal,
    args: Mapping[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Decide whether this principal may attempt this tool with these arguments.

    Returns the facts it established -- ``run_id``, ``project_ref``, ``revision``,
    ``status``, ``via`` -- for the handler and the envelope. Raises a
    :class:`~gx_core.errors.GxError`.
    """
    require_scope(tool, principal)
    return _authorize_arguments(tool, principal, args, conn=conn)


def _authorize_arguments(tool: str, principal: Principal, args: Mapping[str, Any], *,
                         conn: sqlite3.Connection | None) -> dict[str, Any]:
    """Check the requested targets and current state after the caller's tool scope is established."""
    spec = contracts.any_tool_spec(tool)
    context: dict[str, Any] = {
        "run_id": None, "project_ref": None, "revision": None, "status": None, "via": None,
    }

    value = args.get(_PROJECT_REF_ARG)
    if isinstance(value, str) and value and not principal.may_access_project(value):
        raise ScopeDenied(
            f"{_PROJECT_REF_ARG}={value!r} is outside this caller's project scope",
            details={"argument": _PROJECT_REF_ARG, "requested": value,
                     "allowed": list(principal.project_refs)},
        )
    if args.get(_PROJECT_SCOPE_ARG) is not None:
        check_project_scope(principal, args[_PROJECT_SCOPE_ARG])

    for name in _ARTIFACT_REF_ARGS:
        if name not in args:
            continue
        value = args[name]
        refs: Iterable[Any] = value if isinstance(value, list) else [value]
        for ref in refs:
            try:
                check_artifact_ref(ref, argument=name)
            except PathTraversalDenied as exc:
                if declares_error(tool, exc.code):
                    raise
                raise SchemaInvalid(
                    exc.message, details={**exc.details, "reason": "not_an_artifact_reference"},
                ) from exc

    if conn is not None:
        run_id, via = resolve_run_id(conn, args)
        if run_id is not None:
            row = conn.execute(
                "SELECT run_id, project_ref, status, revision FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                if declares_error(tool, "ARTIFACT_NOT_FOUND"):
                    raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
                raise ScopeDenied(
                    f"run {run_id!r} is not visible in this scope",
                    details={"run_id": run_id, "scope": principal.scope},
                )
            if not principal.may_access_project(row["project_ref"]):
                raise ScopeDenied(
                    f"run {run_id!r} belongs to project {row['project_ref']!r}, which is "
                    "outside this caller's project scope",
                    details={"run_id": run_id, "project_ref": row["project_ref"],
                             "allowed": list(principal.project_refs)},
                )
            context.update(run_id=run_id, project_ref=row["project_ref"],
                           revision=int(row["revision"]), status=row["status"], via=via)
            if (tool == "gx_run_resume" and row["status"] in machines.terminal_states("run")
                    and not _replays_a_resume(conn, args)):
                raise IllegalTransition(
                    f"run {run_id} is {row['status']}; a terminal run cannot be resumed",
                    details={"run_id": run_id, "status": row["status"],
                             "terminal": sorted(machines.terminal_states("run"))},
                )

        elif tool == "gx_artifact_get":
            # A project launch brief may precede the project's first run.
            project = _one(conn, "SELECT json_extract(provenance_json, '$.project_ref') FROM artifacts "
                           "WHERE artifact_ref=? AND kind='project_launch_brief' AND origin='BACKEND'",
                           args.get("artifact_ref"))
            if project:
                if not principal.may_access_project(project):
                    raise ScopeDenied("the launch brief belongs to another project")
                context.update(project_ref=project, via="artifact_ref")

    if spec.get("mutating") and declares_error(tool, "HOLD_ACTIVE"):
        config.check_holds(scope="api")

    return context


__all__ = [
    "ENV_PROJECTS", "ENV_SCOPE", "ENV_SUBJECT", "SCOPES", "SCOPE_HOST_ADAPTER",
    "SCOPE_OPERATOR", "SCOPE_READONLY", "SCOPE_WORKER", "Principal", "assert_within",
    "authorize", "check_artifact_ref", "check_project_scope", "declares_error", "is_mutating",
    "may_call", "require_scope",
    "principal_from_env", "privileged_names", "resolve_run_id", "tool_scope",
    "visible_tools",
]
