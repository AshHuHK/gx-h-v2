"""Dispatch tool calls from the native Codex adapter using the release-owned SQLite state.

The dispatcher applies the installed tool contract, validates arguments and calls
the owning service. Services own their transactions and external-effect reconciliation.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Iterable, Mapping

from . import authz, contracts, context, registry, schema
from .context import Ctx, Principal
from .domain import ids
from .errors import (
    GxError,
    ImplementationGap,
    OutcomeUnknown,
    PermissionDenied,
    ScopeDenied,
    SchemaInvalid,
    StoreUnavailable,
)
from .mcp import envelope
from .persistence import store
from .registry import ToolResult

LOGGER = logging.getLogger("gx.api")

#: ``Principal.source`` of a command-line call that a person confirmed on a terminal.
#: The command-line client sets it only after reading the confirmation from a TTY; a
#: model's shell has no TTY. It is what a tool marked ``requires_user_interaction``
#: needs on that transport.
SOURCE_INTERACTIVE_TERMINAL = "interactive_terminal"

#: The refusals the dispatcher itself makes, on any tool: another scope's tool, a project
#: the caller was not started for, arguments of the wrong shape, a tool nobody published,
#: a store that cannot be opened, and a handler that raised something that is not a
#: refusal. A tool's own entry in the contract lists what it may refuse with beyond these;
#: a hold, and a path where an artifact reference belongs, are refused under their own
#: codes only by the tools that list them.
DISPATCHER_CODES = (
    "PERMISSION_DENIED", "SCOPE_DENIED", "SCHEMA_INVALID", "IMPLEMENTATION_GAP",
    "STORE_UNAVAILABLE", "OUTCOME_UNKNOWN",
)

class UnknownTool(KeyError):
    """The name is not a tool of the contract. A protocol error, not a refusal."""

    def __init__(self, name: Any, available: list[str]) -> None:
        super().__init__(name)
        self.name = name
        self.available = available

    def __str__(self) -> str:
        return f"unknown tool {self.name!r}"


def all_tool_names() -> list[str]:
    registry.load_providers()
    return sorted(registry.TOOLS)


def _startup() -> None:
    """Load the installed backend providers."""
    registry.load_providers()


# ------------------------------------------------------------------------- listing

def describe_tool(name: str) -> dict[str, Any]:
    """One tool as a client sees it: description, input schema and call annotations.

    ``readOnlyHint`` and ``idempotentHint`` are derived from the contract's ``mutating``
    and ``idempotent_by``, so a client may run reads in parallel and repeat a keyed call.
    A tool whose decision belongs to a person carries the marker that makes an MCP
    client ask on every call in every permission mode.
    """
    if registry.handler_for(name) is None:
        raise UnknownTool(name, all_tool_names())
    spec = contracts.any_tool_spec(name)
    parts = []
    if spec.get("note"):
        parts.append(spec["note"])
    scope = authz.tool_scope(name)
    parts.append(f"scope={scope}, mutating={bool(spec.get('mutating'))}")
    if spec.get("requires_revision"):
        parts.append("requires expected_revision")
    if spec.get("idempotent_by"):
        parts.append(f"idempotent by {spec['idempotent_by']}")
    if spec.get("backend_checks"):
        parts.append("backend checks: " + "; ".join(spec["backend_checks"]))
    if spec.get("errors"):
        parts.append("may refuse with: " + ", ".join(spec["errors"]))
    also = [code for code in DISPATCHER_CODES if code not in (spec.get("errors") or [])]
    if also:
        parts.append("and, like every tool, with: " + ", ".join(also))
    entry: dict[str, Any] = {
        "name": name,
        "description": " | ".join(parts),
        "inputSchema": schema.tool_input_schema(name),
        "annotations": {
            "readOnlyHint": not bool(spec.get("mutating")),
            "idempotentHint": bool(spec.get("idempotent_by")) or not bool(spec.get("mutating")),
        },
    }
    if spec.get("requires_user_interaction"):
        entry["_meta"] = {"anthropic/requiresUserInteraction": True}
    return entry


def list_tools(principal: Principal, *, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Describe the visible tools in the requested catalog and their handler bindings."""
    installed = set(all_tool_names())
    if names is not None:
        installed.intersection_update(names)
    listed = []
    for name in authz.visible_tools(principal, names=installed):
        entry = describe_tool(name)
        entry["bound"] = True
        listed.append(entry)
    return listed


def doctor(principal: Principal | None = None) -> dict[str, Any]:
    """What is installed, what is bound and what is standing in: ``gx doctor``."""
    _startup()

    report = registry.report()
    digest = contracts.contract_digest()
    try:
        path = context.store_path()
    except ScopeDenied:
        path = None
    store_state: dict[str, Any] = {"configured": path is not None}
    if path is not None:
        store_state.update(path=str(path), exists=path.exists())
    if path is not None and path.exists():
        try:
            conn = store.open_store(path)
        except StoreUnavailable as exc:
            store_state.update(openable=False, reason=exc.details.get("reason"), error=exc.message)
        else:
            try:
                meta = store.read_meta(conn)
                store_state.update(
                    openable=True, contract_version=meta.get(store.META_CONTRACT_VERSION),
                    contract_sha256=meta.get(store.META_CONTRACT_SHA256),
                    runs=conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"],
                )
            finally:
                conn.close()
    return {
        "contract": {"version": contracts.contract_version(), "sha256": digest["__combined__"]},
        "store": store_state,
        "principal": (principal or context.principal_from_env()).describe(),
        "registry": report,
        "tools_bound": sorted(registry.TOOLS),
    }


# ------------------------------------------------------------------------ dispatch

def _gap_for(tool: str) -> ImplementationGap:
    providers = registry.load_providers()
    failed = {module: reason for module, reason in providers.items() if reason is not None}
    return ImplementationGap(
        f"{tool} is in the contract and no service has published a handler for it",
        details={"tool": tool, "providers_not_loaded": failed,
                 "completion_status": "DESIGN_ONLY"},
    )


def _current_run(conn: sqlite3.Connection | None, run_id: str | None) -> tuple[int | None, str | None]:
    if conn is None or not run_id:
        return None, None
    try:
        row = conn.execute("SELECT revision, status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    return int(row["revision"]), row["status"]


def dispatch(
    tool: str,
    arguments: Mapping[str, Any] | None,
    *,
    principal: Principal,
    conn: sqlite3.Connection | None = None,
    transport: str = "cli",
    request_id: str | None = None,
    native_call: Any = None,
) -> dict[str, Any]:
    """Run one tool call and return the contract's envelope. Never raises a refusal.

    ``conn`` is for a caller that already holds the store (the MCP server keeps one
    connection for its life; a test passes an in-memory store). Without it the store is
    opened for this call and closed after it. Raises :class:`UnknownTool` for a name the
    contract does not define; everything else comes back as an envelope.
    """
    if not isinstance(tool, str) or tool not in all_tool_names():
        raise UnknownTool(tool, authz.visible_tools(principal, names=all_tool_names()))
    _startup()
    request_id = request_id or ids.new_id("request")
    started = time.monotonic()
    warnings: list[str] = []
    clean: dict[str, Any] = {}
    facts: dict[str, Any] = {}
    owned = False
    spec = contracts.any_tool_spec(tool)

    try:
        try:
            # 0. whose tool it is
            authz.require_scope(tool, principal)

            # 1. the store
            if conn is None:
                conn = store.open_store(context.store_path())
                owned = True

            # 2. the shape of the arguments
            from .hostobs import native_context
            check = schema.check_arguments(tool, arguments, native_workspace=transport == native_context.TRANSPORT)
            warnings.extend(check.warnings())
            if check.authority_claims:
                LOGGER.warning(
                    "ignored client authority claim(s) %s on %s from subject=%s",
                    sorted(check.authority_claims), tool, principal.subject,
                )
            if not check.ok:
                raise SchemaInvalid(
                    f"arguments for {tool} do not match the contract schema",
                    details={"tool": tool, "violations": check.errors[:20]},
                )
            clean = check.clean

            # 3. what the call may reach
            facts = authz._authorize_arguments(tool, principal, clean, conn=conn)
            if (transport == "native_app_server") != (native_call is not None):
                raise ScopeDenied("the native transport requires its controlled callback context; "
                                  "native identity cannot fall back to an ordinary transport")
            if spec.get("requires_user_interaction") and (
                    transport == "native_app_server" or (
                        transport == "cli" and principal.source != SOURCE_INTERACTIVE_TERMINAL)):
                raise PermissionDenied(
                    f"{tool} records a person's decision and needs an interactive terminal: "
                    "use bin/gx-native --decide-approval on a TTY and confirm when asked. A "
                    "standing policy in config/approvals.json is applied by the backend "
                    "when an approval is requested, not through this tool",
                    details={"tool": tool, "transport": transport,
                             "requires_user_interaction": True},
                )

            # 4. the handler
            handler = registry.handler_for(tool)
            if handler is None:
                raise _gap_for(tool)
            ctx = Ctx(conn=conn, principal=principal, tool=tool, request_id=request_id,
                      transport=transport, authz=facts, native_call=native_call)
            ctx.native_call = native_context.authorize_call(
                conn, principal, tool, clean, native_call, raw_arguments=arguments,
            )
            result = ToolResult.coerce(handler(ctx, dict(clean)))
            if ctx.warnings:
                result.warnings = list(result.warnings) + list(ctx.warnings)
            if result.run_id is None:
                run_id = facts.get("run_id") or result.data.get("run_id")
                if isinstance(run_id, str):
                    result.run_id = run_id

            # 5. the envelope and its limits
            body = envelope.success(request_id, result, extra_warnings=warnings)
            revision, status = _current_run(conn, body.get("run_id"))
            if body.get("revision") is None:
                body["revision"] = revision
            if body.get("status") is None:
                body["status"] = status
            body = envelope.enforce_limits(
                body, conn=conn, tool=tool, run_id=body.get("run_id"),
                actor=f"{transport}:{principal.scope}:{principal.subject}",
            )
            LOGGER.info("%s ok run=%s %.0fms", tool, body.get("run_id"),
                        (time.monotonic() - started) * 1000)
            return body

        except GxError as exc:
            creation = facts.get("run_creation")
            if isinstance(creation, dict):
                exc.details = {**exc.details, "run_creation": dict(creation)}
                if exc.side_effect_status == "NONE":
                    exc.side_effect_status = "PARTIAL"
                    exc.suggested_recovery = (
                        "Run creation is committed. Inspect gx_run_status for this run and "
                        "recover the existing workflow; the supplied idempotency key retains "
                        "its original creation refs."
                    )
            # A refusal says which run it concerns. It says where that run stands only
            # when the caller was allowed to reach it: a run id the caller sent is echoed
            # back, but the status and revision of a run it may not see are not.
            run_id = facts.get("run_id") or (
                clean.get("run_id") if isinstance(clean.get("run_id"), str) else None
            )
            revision, status = _current_run(conn, facts.get("run_id"))
            LOGGER.info("%s refused: %s", tool, exc)
            return envelope.bound_failure(envelope.failure(
                request_id, exc, warnings=warnings, run_id=run_id,
                revision=revision, status=status,
            ))
        except Exception as exc:  # noqa: BLE001 - a service bug is still an answer
            # OUTCOME_UNKNOWN, for a query as well as a write. An unhandled exception
            # means we do not know what the backend did before it raised, and blaming the
            # caller's arguments would send it off rewriting a correct request.
            LOGGER.exception("%s raised an unhandled exception", tool)
            wrapped = OutcomeUnknown(
                f"unhandled {type(exc).__name__} in {tool}: {exc}",
                details={"tool": tool, "unhandled": type(exc).__name__,
                         "mutating": bool(spec.get("mutating")),
                         **({"run_creation": dict(facts["run_creation"])}
                            if isinstance(facts.get("run_creation"), dict) else {})},
            )
            run_id = facts.get("run_id")
            revision, status = _current_run(conn, run_id)
            return envelope.bound_failure(envelope.failure(
                request_id, wrapped, warnings=warnings, run_id=run_id,
                revision=revision, status=status,
            ))
    finally:
        if owned and conn is not None:
            conn.close()


__all__ = [
    "DISPATCHER_CODES", "SOURCE_INTERACTIVE_TERMINAL", "UnknownTool", "all_tool_names",
    "describe_tool", "dispatch", "doctor", "list_tools",
]
