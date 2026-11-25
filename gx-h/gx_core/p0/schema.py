"""Apply additive, independently versioned P0 migrations to the existing GX database."""

from functools import lru_cache
import hashlib
from pathlib import Path
import re
import sqlite3

from ..errors import StoreUnavailable
from ..persistence import store


@lru_cache(maxsize=1)
def migrations():
    parts = []
    for path in sorted(Path(__file__).parent.glob("*.sql")):
        raw = path.read_bytes()
        statements, statement = [], ""
        for line in raw.decode().splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                statements.append(statement)
                statement = ""
        if statement.strip():
            raise ValueError(f"Unterminated SQL migration in {path.name}")
        parts.append((f"p0.migration.{path.name}", hashlib.sha256(raw).hexdigest(), statements))
    return parts


# These exact, released migrations now have a second representation in the
# formal contract. Their original bytes remain the migration ledger identity.
_PROMOTED = frozenset(f"p0.migration.{name}" for name in (
    "v6.sql", "v6_job_indexes.sql", "v6_funding.sql", "v6_native.sql",
    "zzzzzzzzzz_v6_job_attempts.sql",
))
_CREATE = re.compile(r"^CREATE (?:TABLE|INDEX|TRIGGER) (\w+)\b", re.MULTILINE)


@lru_cache(maxsize=1)
def _promoted_objects():
    objects = {}
    for key, digest, statements in migrations():
        if key not in _PROMOTED:
            continue
        for statement in statements:
            create = _CREATE.search(statement)
            if create is None:
                raise ValueError(f"Unsupported statement in released migration {key}")
            # SQLite stores CREATE text from the first keyword, omits the final
            # semicolon and strips IF NOT EXISTS. This compares the entire FK,
            # CHECK, UNIQUE, partial-index predicate, or trigger body.
            sql = statement[create.start():].strip().removesuffix(";")
            objects[create[1]] = (key, digest, sql)
    return objects


def verify_promoted(conn, *, require_all):
    """Check objects already installed and identify DDL still owed by each unreceipted migration."""
    missing = {}
    meta = store.read_meta(conn)
    installed = dict(conn.execute(
        "SELECT lower(name),sql FROM sqlite_schema "
        "WHERE lower(name) GLOB 'v6_*' OR lower(name)='p0_v6_native_calls'"
    ))
    for name, (key, digest, sql) in _promoted_objects().items():
        if key in meta and meta[key] != digest:
            raise ValueError(f"Applied P0 migration changed on disk: {key}")
        if name not in installed and not require_all and key not in meta:
            missing.setdefault(key, []).append(sql)
            continue
        if installed.get(name) != sql:
            raise StoreUnavailable(
                f"Published V6 schema object {name} differs from {key}",
                details={"reason": "contract_shape_mismatch", "object": name, "migration": key},
            )
    return missing


def install(conn):
    applied = store.read_meta(conn)
    pending = []
    for key, digest, statements in migrations():
        if applied.get(key) == digest:
            continue
        if key in applied:
            raise ValueError(f"Applied P0 migration changed on disk: {key}")
        pending.append((key, digest, statements))
    if not pending:
        return
    with store.transaction(conn, transaction_owner="Store", operation="install_p0_schema") as tx:
        promoted_pending = any(key in _PROMOTED for key, _, _ in pending)
        # An operator-only writable open keeps the old global contract stamp and
        # may arrive before a full store open has installed the promoted objects.
        missing = verify_promoted(tx, require_all=False) if promoted_pending else {}
        for key, digest, statements in pending:
            row = tx.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
            if row and row[0] == digest:
                continue
            if row:
                raise ValueError(f"Incompatible P0 migration: {key}")
            remaining = missing.get(key, ()) if key in _PROMOTED else statements
            for statement in remaining:
                tx.execute(statement)
            tx.execute("INSERT INTO schema_meta(key,value) VALUES(?,?)", (key, digest))
        if promoted_pending:
            verify_promoted(tx, require_all=True)
