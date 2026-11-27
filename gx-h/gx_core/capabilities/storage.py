"""Where a store keeps the files of this stream.

A store is a database file and the directories beside it (``store.data_root``, core's):

    <data root>/artifacts               published artifacts, and ``.staging`` beneath them
    <data root>/jobs/<job_id>/          one directory per job, owned by its runner
    <data root>/jobs/gpu.lock           the lease GPU jobs queue on

v1 wrote job directories under the repository's ``var/`` whichever database was open, so
a run against a scratch store still wrote into the tree. Nothing here names ``var/``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..artifacts import ArtifactService
from ..persistence import store


def store_dir(conn: sqlite3.Connection) -> Path:
    return store.data_root(conn)


def jobs_root(conn: sqlite3.Connection) -> Path:
    path = store.data_root(conn) / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_service(conn: sqlite3.Connection) -> ArtifactService:
    return ArtifactService(conn)


__all__ = ["artifact_service", "jobs_root", "store_dir"]
