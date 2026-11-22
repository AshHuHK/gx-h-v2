"""The one place governance reaches the artifact store.

The artifact service is core's: it keeps the bytes beside the store they belong to
(``store.data_root``) and re-hashes them on every read. What this module adds is the one
convention governance needs — **content is addressed by its sha256** — so the same bytes
are one artifact whoever registers them, and a version's sha256 does not depend on where
the bytes came from or how they were formatted.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Mapping

from ..artifacts.service import ArtifactService, PublishedArtifact
from ..domain import ids
from ..errors import ArtifactNotFound


def service(conn: sqlite3.Connection) -> ArtifactService:
    return ArtifactService(conn)


def row(conn: sqlite3.Connection, artifact_ref: str) -> sqlite3.Row:
    found = conn.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (artifact_ref,)).fetchone()
    if found is None:
        raise ArtifactNotFound(
            f"no artifact {artifact_ref!r}; a missing artifact is not an empty result",
            details={"artifact_ref": artifact_ref},
        )
    return found


def recorded_sha256(conn: sqlite3.Connection, artifact_ref: str) -> str:
    return str(row(conn, artifact_ref)["sha256"])


def verify(conn: sqlite3.Connection, artifact_ref: str) -> dict[str, Any]:
    """Re-hash the bytes behind a reference. Reports; does not raise on a mismatch."""
    return service(conn).verify(artifact_ref)


def read_bytes(conn: sqlite3.Connection, artifact_ref: str) -> bytes:
    """The bytes behind a reference, re-hashed against the recorded sha256 first."""
    return service(conn).read_bytes(artifact_ref)


def read_json(conn: sqlite3.Connection, artifact_ref: str) -> Any:
    return service(conn).read_json(artifact_ref)


def canonical_bytes(obj: Any) -> bytes:
    """The byte form governance hashes: sorted keys, no insignificant whitespace, UTF-8."""
    return ids.canonical_json(obj).encode("utf-8")


def publish_content_addressed(
    conn: sqlite3.Connection,
    *,
    data: bytes,
    kind: str,
    actor: str,
    filename: str = "content.json",
    media_type: str = "application/json",
    provenance: Mapping[str, Any] | None = None,
    area: str = "assets",
    origin: str = "BACKEND",
) -> PublishedArtifact:
    """Publish bytes under ``<area>/<sha[:2]>/<sha>/<filename>``. The same bytes are one artifact."""
    sha256 = hashlib.sha256(data).hexdigest()
    return service(conn).publish_bytes(
        namespace=f"{area}/{sha256[:2]}/{sha256}",
        rel_path=filename,
        kind=kind,
        data=data,
        media_type=media_type,
        provenance=dict(provenance or {}),
        expected_sha256=sha256,
        actor=actor,
        origin=origin,
        put_by=actor,
    )
