"""Apply additive, independently versioned P0 migrations to the existing GX database."""

from functools import lru_cache
import hashlib
from pathlib import Path
import sqlite3

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
        for key, digest, statements in pending:
            row = tx.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
            if row and row[0] == digest:
                continue
            if row:
                raise ValueError(f"Incompatible P0 migration: {key}")
            for statement in statements:
                tx.execute(statement)
            tx.execute("INSERT INTO schema_meta(key,value) VALUES(?,?)", (key, digest))
