"""Release-scoped native state and single-client ownership.

This is a local deployment boundary, not hostile same-UID isolation. It keeps a
new native release from accidentally opening an older product database. It does
not recover native capacity or adopt another release's runs. Compatible API-only
updates use the existing store schema owner, without a second migration protocol.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from . import config, contracts

RELEASE_ID = "gx-h-native-v1"
STATE_MARKER = ".gx-native-state.json"


class ReleaseStateError(ValueError):
    """The native entry has not established ownership of this state directory."""


@dataclass(frozen=True)
class NativeState:
    directory: Path
    db_path: Path
    release_id: str
    contract_sha256: str


def write_native_bytes(path: Path, encoded: bytes, *, mode: int | None = None) -> None:
    """Publish a complete native state file and sync the directory entry."""
    fd, temp = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _marker_for(directory: Path) -> dict[str, str]:
    return {
        "schema": "gx.native_release_state/1",
        "release_id": RELEASE_ID,
        "source_root": str(contracts.HARNESS_ROOT.resolve()),
        "state_root": str(directory),
        "contract_sha256": contracts.contract_digest()["__combined__"],
    }


def _check_path(directory: Path, env: Mapping[str, str]) -> tuple[Path, dict[str, str]]:
    directory = directory.expanduser().resolve()
    deployment = contracts.CONFIG_DIR / "native_release.json"
    if not deployment.is_file():
        raise ReleaseStateError("native release descriptor is missing")
    declared = json.loads(deployment.read_text(encoding="utf-8"))
    if (declared.get("schema") != "gx.native_release/1"
            or declared.get("release_id") != RELEASE_ID
            or declared.get("source_mode") != "codex_native_events_v1"
            or declared.get("source_root") != "."):
        raise ReleaseStateError("native release descriptor has a different version")
    for raw in declared.get("forbidden_state_roots", []):
        forbidden = Path(raw).resolve()
        if directory == forbidden or forbidden in directory.parents:
            raise ReleaseStateError("legacy product state is outside this release")
    db = directory / "gx.db"
    if env.get("GX_DB") and Path(env["GX_DB"]).expanduser().resolve() != db:
        raise ReleaseStateError("GX_DB conflicts with the explicit native state directory")
    for path in (db, directory / STATE_MARKER, directory / ".native.lock"):
        if path.is_symlink():
            raise ReleaseStateError("native state entries must not be symbolic links")
    expected = _marker_for(directory)
    marker = directory / STATE_MARKER
    if marker.exists():
        try:
            actual = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReleaseStateError("native state marker is unreadable") from exc
        identity = set(expected) - {"contract_sha256"}
        if (not isinstance(actual, dict) or set(actual) != set(expected)
                or any(actual.get(key) != expected[key] for key in identity)
                or not isinstance(actual.get("contract_sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", actual["contract_sha256"]) is None):
            raise ReleaseStateError("native state belongs to another release or source")
        if actual["contract_sha256"] != expected["contract_sha256"] and not db.exists():
            raise ReleaseStateError("changed contract has no owned database to verify")
    elif any((directory / name).exists() for name in ("gx.db", "gx.db-wal", "gx.db-shm")):
        raise ReleaseStateError("an existing database without this release marker will not be opened")
    return directory, expected


def _verify_database(db: Path, expected_sha: str) -> None:
    """Read our already-owned directory's header with WAL-aware SQLite read-only.

    No SQL writes or migration occur. SQLite may maintain a WAL shared-memory
    sidecar, so this deliberately makes no byte-for-byte no-side-effects claim.
    Legacy paths/unmarked directories have already been refused without SQLite.
    """
    if not db.exists():
        return
    before = db.stat()
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            meta = dict(conn.execute("SELECT key,value FROM schema_meta"))
    except sqlite3.Error as exc:
        raise ReleaseStateError("native database metadata is unreadable; no recovery or migration attempted") from exc
    after = db.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ReleaseStateError("native database identity changed during preflight")
    digest = contracts.contract_digest()
    if (meta.get("contract_sha256") == expected_sha
            and meta.get("db_schema_sha256") == digest["db_schema.sql"]
            and meta.get("contract_version") == str(contracts.contract_version())):
        return
    previous_sha = meta.get("contract_sha256", "")
    migration = json.loads((contracts.CONFIG_DIR / "native_contract_migrations.json").read_text())
    known_schema = any(route.get("contract_sha256") == previous_sha and
                       route.get("db_schema_sha256") == meta.get("db_schema_sha256")
                       for route in migration.get("additive_schema_sources", []))
    if (re.fullmatch(r"[a-f0-9]{64}", previous_sha) is None
            or meta.get("contract_version") != str(contracts.contract_version())
            or (meta.get("db_schema_sha256") != digest["db_schema.sql"] and not known_schema)):
        raise ReleaseStateError("native database contract differs incompatibly from this release")
    # Same-DDL API updates and explicitly declared additive DDL sources pass
    # to the existing schema owner. It records the old/new contract at connect.
    # The marker retains its original contract identity; no historical run is edited.


@contextmanager
def native_state(directory: str | Path | None = None, *,
                 env: Mapping[str, str] | None = None) -> Iterator[NativeState]:
    """Lock and bind one release state before any writable SQLite connection.

    An existing unmarked directory is refused before SQLite or lock-file writes.
    A database in an owned directory is checked read-only before writable connect,
    recovery or migration. Only the supported native entry uses this
    context; ordinary legacy transports never gain native identity from it.
    """
    directory, expected = _check_path(
        Path(directory) if directory is not None else contracts.HARNESS_ROOT / "var" / RELEASE_ID,
        os.environ if env is None else env,
    )
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(directory / ".native.lock", flags, 0o600)
    old_override = config.RUNTIME_VAR_DIR
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseStateError("another native client owns this state directory") from exc
        # Check again under the ownership lock before installing a new marker.
        directory, expected = _check_path(directory, {} if env is None else env)
        marker = directory / STATE_MARKER
        if not marker.exists():
            with marker.open("x", encoding="utf-8") as fh:
                json.dump(expected, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        _verify_database(directory / "gx.db", expected["contract_sha256"])
        config.RUNTIME_VAR_DIR = directory
        yield NativeState(directory, directory / "gx.db", RELEASE_ID, expected["contract_sha256"])
    finally:
        config.RUNTIME_VAR_DIR = old_override
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
