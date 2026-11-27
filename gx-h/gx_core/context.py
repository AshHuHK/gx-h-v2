"""Who is asking, over which transport, on which connection.

One small object is handed to every tool handler, bundle validator, check and brief
section. It carries the store connection and the principal the backend settled on, and
nothing a caller could have supplied to authorize itself.

Both transports build it the same way: the MCP server from its launch environment, the
command-line client from its environment and options. The scope is one of four names. On
one machine a shell can name any of them, so a scope is a procedural boundary and every
record says which scope and which source wrote it; what a scope cannot buy is a person's
decision, which is taken where a person is (see ``contracts/mcp_tools.json`` under
``transports.person``).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import config, contracts

SCOPE_WORKER = "worker"
SCOPE_HOST_ADAPTER = "host_adapter"
SCOPE_OPERATOR = "operator"
SCOPE_READONLY = "readonly"
SCOPES = (SCOPE_WORKER, SCOPE_HOST_ADAPTER, SCOPE_OPERATOR, SCOPE_READONLY)

ENV_SCOPE = "GX_SCOPE"
ENV_PROJECTS = "GX_PROJECTS"
ENV_SUBJECT = "GX_SUBJECT"
ENV_DB = "GX_DB"

#: The short tag an actor string carries for each transport in ``labels.transport``.
_TRANSPORT_TAG = {"mcp_stdio": "mcp", "cli": "cli", "native_app_server": "native"}


@dataclass(frozen=True)
class Principal:
    """Who the backend believes it is acting for, and how it came to believe it."""

    subject: str
    scope: str = SCOPE_WORKER
    project_refs: tuple[str, ...] = ("*",)
    source: str = "launch_environment"
    #: Whatever the client said about itself. Recorded for correlation, never consulted.
    client_claims: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"unknown scope {self.scope!r}; allowed: {list(SCOPES)}")

    @property
    def allows_all_projects(self) -> bool:
        return "*" in self.project_refs

    def may_access_project(self, project_ref: str | None) -> bool:
        if project_ref is None:
            return True
        return self.allows_all_projects or project_ref in self.project_refs

    def describe(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "scope": self.scope,
            "project_refs": list(self.project_refs),
            "source": self.source,
            "isolation_claim": "single trust domain; this is not cross-tenant isolation",
        }


def principal_from_env(env: Mapping[str, str] | None = None, *, source: str = "launch_environment") -> Principal:
    """Build the principal from the environment. Fails closed on an unknown scope."""
    env = os.environ if env is None else env
    scope = (env.get(ENV_SCOPE) or SCOPE_WORKER).strip().lower()
    if scope not in SCOPES:
        raise ValueError(
            f"{ENV_SCOPE}={scope!r} is not a known scope; allowed: {list(SCOPES)}. "
            "Refusing to start rather than guessing a privilege level."
        )
    raw_projects = (env.get(ENV_PROJECTS) or "*").strip()
    projects = tuple(part.strip() for part in raw_projects.split(",") if part.strip()) or ("*",)
    subject = (env.get(ENV_SUBJECT) or f"{scope}@local").strip()
    return Principal(subject=subject, scope=scope, project_refs=projects, source=source)


def store_path(env: Mapping[str, str] | None = None) -> Path:
    """The one store both transports open: ``GX_DB`` when set, otherwise ``var/gx.db``.

    v1 opened no store at all when its variable was unset, so the configuration it shipped
    answered every stateful tool with a gap. Unset now means the default file, which is
    created on first use.
    """
    descriptor = contracts.CONFIG_DIR / "native_release.json"
    if descriptor.exists():
        from .errors import ScopeDenied
        if config.RUNTIME_VAR_DIR is None:
            raise ScopeDenied("this native-only release opens databases through bin/gx-native; "
                              "ordinary CLI/MCP database entry is disabled before connect")
        return config.RUNTIME_VAR_DIR / "gx.db"
    env = os.environ if env is None else env
    raw = (env.get(ENV_DB) or "").strip()
    return Path(raw) if raw else config.db_path()


@dataclass
class Ctx:
    """What a handler, validator, check or brief section may rely on. Deliberately small."""

    conn: sqlite3.Connection
    principal: Principal
    tool: str = ""
    request_id: str = ""
    transport: str = "cli"
    #: Facts the authorization pass already established (run row, project, phase).
    authz: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Internal callback context supplied only by the controlled native adapter.
    #: Neither CLI/MCP arguments nor launch environment populate this field.
    native_call: Any = None

    def __post_init__(self) -> None:
        if self.transport not in contracts.label_values("transport"):
            raise ValueError(f"unknown transport {self.transport!r}")

    @property
    def actor(self) -> str:
        """The string recorded in the event log. Correlation, never authentication."""
        return f"{_TRANSPORT_TAG[self.transport]}:{self.principal.scope}:{self.principal.subject}"

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def child(self, **changes: Any) -> "Ctx":
        """The same request seen by another component, sharing the warning list."""
        fields = {
            "conn": self.conn, "principal": self.principal, "tool": self.tool,
            "request_id": self.request_id, "transport": self.transport,
            "authz": self.authz, "warnings": self.warnings,
            "native_call": self.native_call,
        }
        fields.update(changes)
        return Ctx(**fields)
