"""Project scopes use whole namespace segments of the form <realm>:<project>[/<segment>].

Real projects can also search an explicitly named read-only shared library.
Synthetic runs search only their own project. Empty scopes match nothing.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Sequence

from ..errors import ArtifactNotFound, SchemaInvalid, ScopeDenied

REALMS = ("real", "synthetic")
LIBRARY_PROJECT = "library"
LIBRARY_SCOPE = f"real:{LIBRARY_PROJECT}"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_namespace(namespace: str) -> tuple[str, str, tuple[str, ...]]:
    """``"real:demo/episodes"`` -> ``("real", "demo", ("episodes",))``.

    The character set is restricted on purpose: namespaces are compared by prefix, and one
    containing ``/..``, whitespace or a wildcard turns prefix comparison into a guess.
    """
    if not isinstance(namespace, str) or ":" not in namespace:
        raise SchemaInvalid(
            f"namespace {namespace!r} must be '<realm>:<project>[/<segment>…]' with realm in {list(REALMS)}",
            details={"namespace": namespace, "realms": list(REALMS)},
        )
    realm, _, rest = namespace.partition(":")
    if realm not in REALMS:
        raise SchemaInvalid(
            f"unknown namespace realm {realm!r}; allowed: {list(REALMS)}",
            details={"namespace": namespace, "realms": list(REALMS)},
        )
    segments = rest.split("/")
    if not segments or not all(_SEGMENT_RE.match(segment) for segment in segments):
        raise SchemaInvalid(
            f"namespace {namespace!r} has an empty or illegal segment; each must match [A-Za-z0-9][A-Za-z0-9._-]*",
            details={"namespace": namespace},
        )
    return realm, segments[0], tuple(segments[1:])


def namespace_in_scope(namespace: str, scope: str) -> bool:
    """True when ``namespace`` is the scope itself or nested under it, by whole segments."""
    parse_namespace(namespace)
    parse_namespace(scope)
    return namespace == scope or namespace.startswith(scope + "/")


def is_synthetic(namespace: str) -> bool:
    return parse_namespace(namespace)[0] == "synthetic"


def project_of(namespace: str) -> str:
    return parse_namespace(namespace)[1]


def for_project(project_ref: str) -> str:
    """The namespace a project's own records live in.

    A project reference that already carries a realm (``synthetic:audit``) is taken as
    written; a bare one is in the ``real`` realm.
    """
    if not isinstance(project_ref, str) or not project_ref.strip():
        raise SchemaInvalid("a project reference is required to name a namespace", details={})
    candidate = project_ref if project_ref.split(":", 1)[0] in REALMS and ":" in project_ref else f"real:{project_ref}"
    parse_namespace(candidate)
    return candidate


def for_run(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """What a run may search: its own project's namespace, and the shared library.

    Read from the run row, never from the caller. A synthetic run gets no library.
    """
    row = conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
    own = for_project(row["project_ref"])
    return [own] if is_synthetic(own) else [own, LIBRARY_SCOPE]


def namespace_for_run(conn: sqlite3.Connection, run_id: str) -> str:
    """Where a run's own sources, evidence and gaps are written."""
    return for_run(conn, run_id)[0]


def normalise_scopes(project_scope: Any) -> list[str]:
    """One scope or several, validated, de-duplicated, order kept."""
    if project_scope is None:
        return []
    items = [project_scope] if isinstance(project_scope, str) else list(project_scope)
    out: list[str] = []
    for item in items:
        parse_namespace(item)
        if item not in out:
            out.append(item)
    return out


def from_argument(value: Any) -> list[str]:
    """The namespaces a ``project_scope`` argument names.

    One namespace, several separated by commas, or a list. A bare project reference
    (``demo``) means that project's own namespace (``real:demo``); the shared library is
    never implied and is named as ``real:library``. This is the one reading of the
    argument: the tool handler and the dispatcher's authorization pass both use it.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise SchemaInvalid("project_scope is a namespace, several separated by commas, or a list", details={})
    return normalise_scopes([part if ":" in part else for_project(part) for part in parts])


def check_principal(principal: Any, scopes: Iterable[str]) -> None:
    """Refuse a scope whose project the principal was not started for.

    The shared library is readable by every principal. This is the procedural boundary
    the design describes, checked a second time here so a handler reached by another
    route than the dispatcher still refuses.
    """
    if principal is None:
        return
    for scope in scopes:
        project = project_of(scope)
        if scope == LIBRARY_SCOPE or scope.startswith(LIBRARY_SCOPE + "/"):
            continue
        if not principal.may_access_project(project):
            raise ScopeDenied(
                f"namespace {scope!r} belongs to project {project!r}, which this principal was not started for",
                details={"scope": scope, "project": project, "allowed": list(principal.project_refs)},
            )


def writable(namespace: str) -> None:
    """Refuse a write into the shared library from anything but an ingest."""
    if namespace == LIBRARY_SCOPE or namespace.startswith(LIBRARY_SCOPE + "/"):
        raise ScopeDenied(
            f"{namespace!r} is the shared inherited library; it is written only by an ingest",
            details={"namespace": namespace},
        )


def sql_scope_clause(column: str, scopes: Sequence[str]) -> tuple[str, list[str]]:
    """``(sql, params)`` matching rows whose namespace is at or under any of ``scopes``.

    ``GLOB`` rather than ``LIKE``: ``_`` and ``%`` are legal namespace characters and
    would be wildcards under ``LIKE``. The segment character set has no ``*``, ``?`` or
    ``[``, so a scope is a literal prefix under ``GLOB``.
    """
    if not scopes:
        return "0", []
    parts: list[str] = []
    params: list[str] = []
    for scope in scopes:
        parts.append(f"({column} = ? OR {column} GLOB ?)")
        params.extend([scope, scope + "/*"])
    return "(" + " OR ".join(parts) + ")", params
