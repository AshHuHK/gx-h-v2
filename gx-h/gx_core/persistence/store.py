"""The single authoritative store.

Design rules this module exists to enforce:

* One authority. The workflow engine, a task board and a JSON file must not each hold
  a version of "the real task state" that can be rewritten independently.
* Every mutation is inside one transaction with its event. A graph that advanced while
  the task never landed is the failure mode this prevents.
* Optimistic concurrency by run revision. A caller that read state at revision 7 may
  not write at revision 9 without looking again.
* Side-effecting requests record their intent *before* the external call, so a lost
  response is recoverable by reconciliation rather than by guessing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .. import config, contracts, metrics
from ..domain import clock, ids, machines
from ..jobs import wakeup
from ..errors import (
    ArtifactNotFound,
    BudgetExceeded,
    IdempotencyConflict,
    OutcomeUnknown,
    SchemaInvalid,
    LeaseExpired,
    LeaseHeld,
    StaleRevision,
    StoreUnavailable,
)

SCHEMA_META_KEY = "db_schema_sha256"
#: The two keys ``contracts/db_schema.sql`` says every store carries.
META_CONTRACT_VERSION = "contract_version"
META_CONTRACT_SHA256 = "contract_sha256"


# --------------------------------------------------------------------------- connect

def connect(path: str | Path | None = None, *, timeout: float = 30.0) -> sqlite3.Connection:
    from ..jobs.run_resources import process_observation
    process_observation()
    target = Path(path) if path is not None else config.db_path()
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    if str(target) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The store's own statement of which contract wrote it. Empty for a fresh file."""
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if has is None:
        return {}
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM schema_meta")}


def _stamp(conn: sqlite3.Connection) -> None:
    digest = contracts.contract_digest()
    _set_meta(conn, SCHEMA_META_KEY, digest["db_schema.sql"])
    _set_meta(conn, META_CONTRACT_VERSION, str(contracts.contract_version()))
    _set_meta(conn, META_CONTRACT_SHA256, digest["__combined__"])
    if "created_at" not in read_meta(conn):
        _set_meta(conn, "created_at", clock.now_iso())


def _ddl_statements() -> list[str]:
    """The frozen DDL as single statements, so it can run inside a transaction.

    ``executescript`` commits before it runs and after every statement, which is what
    made a store visible half-made. ``sqlite3.complete_statement`` finds the statement
    ends, so a semicolon inside a string or a trigger body is not taken for one.
    """
    statements: list[str] = []
    buffer = ""
    for line in contracts.db_schema_sql().splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip().strip(";").strip():
                statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def migrate(conn: sqlite3.Connection) -> str:
    """Apply the authoritative DDL and stamp the contract that wrote this store.

    Idempotent on a store this contract wrote. It is the *creation* path: deciding
    whether an existing file may be opened at all is :func:`open_store`'s job, which
    never calls this on a store another contract generation wrote.

    The tables and the stamp are one transaction. Another process that opens the file
    meanwhile sees no tables at all or a finished, stamped store -- never tables with no
    stamp, which is exactly what a file that is not ours looks like. Created that way,
    31 of 32 processes started together on a new tree were told the store was not theirs.
    """
    if conn.in_transaction:
        for statement in _ddl_statements():
            conn.execute(statement)
        _stamp(conn)
    else:
        with transaction(conn, transaction_owner="Store", operation="migrate_schema") as tx:
            for statement in _ddl_statements():
                tx.execute(statement)
            _stamp(tx)
    return contracts.contract_digest()["db_schema.sql"]


_DDL_TABLE = re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+)", re.MULTILINE)


def _declared_columns() -> dict[str, list[sqlite3.Row]]:
    """Table -> ``PRAGMA table_info`` rows, as the frozen DDL declares them."""
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        sql = contracts.db_schema_sql()
        reference.executescript(sql)
        return {
            table: list(reference.execute(f"PRAGMA table_info({table})"))
            for table in _DDL_TABLE.findall(sql)
        }
    finally:
        reference.close()


def _ended_reasons() -> tuple[str, ...]:
    """The reasons a BLOCKED task has ended its chain (labels.blocked_reason.ended); none before r5."""
    axis = contracts.labels().get("blocked_reason") or {}
    return tuple(axis.get("ended") or ())


def _resync_same_major(conn: sqlite3.Connection, meta: dict[str, str]) -> None:
    """Bring a store of this major version up to a contract that was re-frozen since.

    Within one major version the contract only grows: new tables, and new columns that
    are nullable or carry a default. Those are added here and the move is recorded. A
    difference this cannot make -- a column that is gone, a type that changed, a new
    column that existing rows could not satisfy -- is refused by name, because a store
    that silently lacks a column the code writes fails later, somewhere else.
    """
    declared = _declared_columns()
    problems: list[str] = []
    additions: list[tuple[str, str]] = []
    present = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in declared.items():
        if table not in present:
            continue  # created below by the DDL's own IF NOT EXISTS
        have = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        want = {row["name"]: row for row in columns}
        for name in sorted(set(have) - set(want)):
            problems.append(f"{table}.{name} exists in the store and not in the contract")
        for name, column in want.items():
            if name in have:
                if (have[name]["type"] or "").upper() != (column["type"] or "").upper():
                    problems.append(
                        f"{table}.{name} is {have[name]['type']} in the store and "
                        f"{column['type']} in the contract"
                    )
                continue
            if column["pk"] or (column["notnull"] and column["dflt_value"] is None):
                problems.append(f"{table}.{name} is new and cannot be added to existing rows")
                continue
            clause = f"{name} {column['type']}".strip()
            if column["notnull"]:
                clause += " NOT NULL"
            if column["dflt_value"] is not None:
                clause += f" DEFAULT {column['dflt_value']}"
            additions.append((table, clause))
    if problems:
        raise StoreUnavailable(
            "the store was written under an earlier freeze of this contract version and "
            "differs in ways that cannot be added in place: " + "; ".join(problems[:6]),
            details={"reason": "contract_shape_mismatch", "problems": problems,
                     "store_contract_sha256": meta.get(META_CONTRACT_SHA256),
                     "installed_contract_sha256": contracts.contract_digest()["__combined__"]},
        )
    previous = meta.get(META_CONTRACT_SHA256)
    with transaction(conn, transaction_owner="Store", operation="resync_schema") as tx:
        if read_meta(tx).get(META_CONTRACT_SHA256) == contracts.contract_digest()["__combined__"]:
            return  # another process brought it up to date while this one waited for the lock
        # A published P0 install may already own these names. Validate before
        # any ALTER/CREATE or new stamp could hide a missing or divergent object.
        from ..p0.schema import verify_promoted
        verify_promoted(tx, require_all=False)
        for table, clause in additions:
            tx.execute(f"ALTER TABLE {table} ADD COLUMN {clause}")
        added = {(table, clause.split()[0]) for table, clause in additions}
        if ("tasks", "attempts_consumed") in added:
            # Before contract r5 every attempt started counted against max_attempts. A store brought
            # up keeps that count for the tasks it holds rather than handing their attempts back.
            tx.execute("UPDATE tasks SET attempts_consumed = attempt")
        if ("tasks", "blocked_reason") in added:
            # A task that ended its chain before r5 said why only in its hold; r5 says it on the row too.
            tx.execute("UPDATE tasks SET blocked_reason = json_extract(hold_json, '$.reason') WHERE status='BLOCKED'"
                       " AND json_valid(hold_json) AND json_extract(hold_json, '$.reason') IN ("
                       + ",".join("?" for _ in _ended_reasons()) + ")", _ended_reasons())
        # New tables and indexes: every statement of the DDL is IF NOT EXISTS.
        for statement in _ddl_statements():
            tx.execute(statement)
        _stamp(tx)
        append_event(
            tx, kind="store.contract_resynced", actor="backend", subject_kind="store",
            reason="the contract was re-frozen within the same major version",
            evidence_level="EXECUTION_OBSERVED",
            payload={"from_sha256": previous,
                     "to_sha256": contracts.contract_digest()["__combined__"],
                     "from_db_schema_sha256": meta.get(SCHEMA_META_KEY),
                     "to_db_schema_sha256": contracts.contract_digest()["db_schema.sql"],
                     "added_columns": [f"{table}: {clause}" for table, clause in additions]},
        )


def contract_pin_matches(conn, pinned, installed):
    """A historical Run keeps its pin after a declared additive migration of this store."""
    if pinned == installed:
        return True
    if conn is None or not pinned:
        return False
    digest = contracts.contract_digest()
    if installed != digest["__combined__"]:
        return False
    # Runs retain their original pins through multiple additive releases. Follow
    # only chronological receipts in this store with each release's declared DDL.
    declared = contracts.additive_schema_sources()
    if pinned not in declared:
        return False
    stamp = read_meta(conn)
    current = (installed, digest["db_schema.sql"])
    if (stamp.get(META_CONTRACT_SHA256), stamp.get(SCHEMA_META_KEY)) != current:
        return False
    allowed = set(declared.items()) | {current}
    reached = {(pinned, declared[pinned])}
    receipts = conn.execute(
        "SELECT json_extract(payload_json,'$.from_sha256'),json_extract(payload_json,'$.from_db_schema_sha256'),"
        "json_extract(payload_json,'$.to_sha256'),json_extract(payload_json,'$.to_db_schema_sha256') "
        "FROM events WHERE kind='store.contract_resynced' AND actor='backend' AND subject_kind='store' "
        "ORDER BY event_id"
    )
    for old_sha, old_ddl, new_sha, new_ddl in receipts:
        target = (new_sha, new_ddl)
        if (old_sha, old_ddl) in reached and target in allowed:
            if target == current:
                return True
            reached.add(target)
    return False


def ensure_schema(conn: sqlite3.Connection) -> str:
    """Create the schema in an empty file, or decide whether an existing one may be used.

    Returns ``"created"``, ``"current"`` or ``"resynced"``. Raises ``STORE_UNAVAILABLE``
    for a file some other contract generation wrote, and for a file that is not a store
    of ours at all. v1 restamped whatever it opened with the digest installed at the
    time, so a store never said which contract its rows were written under.
    """
    meta = read_meta(conn)
    installed_version = contracts.contract_version()
    installed_sha = contracts.contract_digest()["__combined__"]
    if not meta:
        # Nothing here yet, as far as this connection has seen. Take the write lock and
        # look again: of several processes started on a new tree, one creates the store
        # and the others wait here and find it made.
        created = False
        with transaction(conn, transaction_owner="Store", operation="initialize_schema") as tx:
            meta = read_meta(tx)
            if not meta:
                others = tx.execute(
                    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
                    " AND name NOT LIKE 'sqlite_%' AND name <> 'schema_meta'"
                ).fetchone()["n"]
                if others:
                    raise StoreUnavailable(
                        "the file holds tables and no schema_meta stamp: it is not a store this "
                        "backend created, and it is not adopted by writing a stamp onto it",
                        details={"reason": "not_a_gx_store", "tables": int(others)},
                    )
                migrate(tx)
                created = True
        if created:
            return "created"

    stamped = meta.get(META_CONTRACT_VERSION)
    if stamped is None:
        # v1 wrote db_schema_sha256 and contract_combined_sha256 and no version.
        stamped = "1" if "contract_combined_sha256" in meta or SCHEMA_META_KEY in meta else "unknown"
    if stamped != str(installed_version):
        raise StoreUnavailable(
            f"the store was written under contract version {stamped} and this backend "
            f"runs version {installed_version}; no migration is named for that pair",
            details={"reason": "contract_major_mismatch", "store_contract_version": stamped,
                     "installed_contract_version": installed_version,
                     "store_contract_sha256": meta.get(META_CONTRACT_SHA256)
                     or meta.get("contract_combined_sha256")},
        )
    if meta.get(META_CONTRACT_SHA256) == installed_sha:
        return "current"
    _resync_same_major(conn, meta)
    return "resynced"


def _refuse_a_file_that_is_not_ours(target: Path) -> None:
    """Look at an existing file read-only before it is ever opened for writing.

    :func:`connect` switches a file to write-ahead logging, which rewrites its header.
    Done first, that changed somebody else's database on the way to refusing it. So an
    existing file is opened read-only, and one that holds tables and no stamp of ours is
    refused with its bytes untouched. Whether a stamped store is one this backend may
    use is :func:`ensure_schema`'s question.
    """
    if str(target) == ":memory:":
        return
    try:
        if not target.is_file() or target.stat().st_size == 0:
            return
    except OSError:
        return
    uri = "file:" + urllib.parse.quote(str(target.resolve())) + "?mode=ro"
    try:
        probe = sqlite3.connect(uri, uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise StoreUnavailable(
            f"the store at {target} could not be opened: {type(exc).__name__}: {exc}",
            details={"reason": "open_failed", "path": str(target), "error": type(exc).__name__},
        ) from exc
    try:
        tables = {row[0] for row in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    except sqlite3.Error as exc:
        raise StoreUnavailable(
            f"{target} cannot be read as a store: {type(exc).__name__}: {exc}",
            details={"reason": "locked" if "locked" in str(exc).lower() else "unreadable",
                     "path": str(target), "error": type(exc).__name__},
        ) from exc
    finally:
        probe.close()
    if tables and "schema_meta" not in tables:
        raise StoreUnavailable(
            "the file holds tables and no schema_meta stamp: it is not a store this backend "
            "created, and it is not adopted by writing a stamp onto it",
            details={"reason": "not_a_gx_store", "tables": len(tables), "path": str(target)},
        )


_RECOVERY_HANDLERS: dict[str, Callable[[sqlite3.Connection], Any]] = {}


def register_recovery(name: str, handler: Callable[[sqlite3.Connection], Any]) -> None:
    """Publish a local-effect recovery invoked after schema admission on normal open.

    Persistence imports no effect owner. A handler must not recursively open this store
    and must leave an ordinary open read-only when it has no pending local journal.
    """
    existing = _RECOVERY_HANDLERS.get(name)
    if existing is not None and (existing.__module__, existing.__qualname__) != (handler.__module__, handler.__qualname__):
        raise ValueError(f"store recovery {name!r} is already registered")
    _RECOVERY_HANDLERS[name] = handler


def open_store(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the store, creating it on first use. Never restamps another contract's file.

    Every way this can fail is ``STORE_UNAVAILABLE`` with the reason in ``details``: the
    path cannot be created or written, the file is locked, it is not SQLite, or it was
    written under another contract version. None of those is a missing service, which is
    what v1 called all of them.
    """
    target = Path(path) if path is not None else config.db_path()
    deadline = time.monotonic() + OPEN_LOCK_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return _open_once(target)
        except StoreUnavailable as exc:
            # A file that is being created, or switched to write-ahead logging, by another
            # process is locked for a moment in ways the busy handler is not consulted
            # for. That is not a store that cannot be opened: wait and look again.
            if exc.details.get("reason") != "locked" or time.monotonic() >= deadline:
                if exc.details.get("reason") == "locked":
                    exc.details["attempts"] = attempt
                raise
            time.sleep(min(0.05 * attempt, 0.5))


#: How long :func:`open_store` keeps trying a store another process has locked.
OPEN_LOCK_WAIT_SECONDS = 30.0


def _is_lock(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _open_once(target: Path) -> sqlite3.Connection:
    try:
        _refuse_a_file_that_is_not_ours(target)
        conn = connect(target)
    except StoreUnavailable:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise StoreUnavailable(
            f"the store at {target} could not be opened: {type(exc).__name__}: {exc}",
            details={"reason": "locked" if _is_lock(exc) else "open_failed", "path": str(target),
                     "error": type(exc).__name__},
        ) from exc
    try:
        ensure_schema(conn)
        from ..p0.schema import install
        install(conn)
    except StoreUnavailable as exc:
        conn.close()
        exc.details.setdefault("path", str(target))
        raise
    except sqlite3.Error as exc:
        conn.close()
        raise StoreUnavailable(
            f"the store at {target} could not be read or initialised: {type(exc).__name__}: {exc}",
            details={"reason": "locked" if _is_lock(exc) else "unreadable",
                     "path": str(target), "error": type(exc).__name__},
        ) from exc
    except BaseException:
        conn.close()
        raise
    try:
        for handler in tuple(_RECOVERY_HANDLERS.values()):
            handler(conn)
    except BaseException:
        conn.close()
        raise
    return conn


_MEMORY_ROOTS: dict[int, Any] = {}


def data_root(conn: sqlite3.Connection) -> Path:
    """The directory that holds everything this store's rows point at.

    A store and its files travel together: artifacts, run inboxes and outboxes and job
    directories live beside the database file (``var/`` for the default store), never at
    a fixed place in the source tree. An artifact row names a namespace and a relative
    path, so a store opened from somewhere else and resolved against the tree's own
    ``var/artifacts`` would serve another store's bytes under this one's hashes.

    An in-memory store gets a private temporary directory for the life of the process,
    so a unit test that passes no root cannot write into the real ``var/``.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    file = row[2] if row is not None else ""  # (seq, name, file)
    if file:
        return Path(file).resolve().parent
    key = id(conn)
    holder = _MEMORY_ROOTS.get(key)
    if holder is None:
        import tempfile

        holder = tempfile.TemporaryDirectory(prefix="gx-memory-store-")
        _MEMORY_ROOTS[key] = holder
    return Path(holder.name)


# sqlite3.Connection has no __dict__, so nesting depth is tracked beside it, keyed by
# identity. The entry is removed when depth returns to zero, so a recycled id cannot
# inherit a stale depth from a closed connection.
_SAVEPOINT_DEPTH: dict[int, int] = {}


@dataclass
class _TransactionEnd:
    owns_transaction: bool
    commit: list[Callable[[], None]] = field(default_factory=list)
    rollback: list[Callable[[], None]] = field(default_factory=list)
    changes_at_entry: int | None = None
    discarded_changes: int = 0


_TRANSACTION_END: dict[int, list[_TransactionEnd]] = {}

# The active Driver observes progress on its own connection. Other writers send a
# hint after commit, including another connection in this same process.
driver_connections: set[sqlite3.Connection] = set()


def _notify_driver(conn: sqlite3.Connection) -> None:
    try:
        location = conn.execute("PRAGMA database_list").fetchone()[2]
    except sqlite3.Error:
        return  # Failure to send a local hint does not change the outcome of a completed transaction.
    if location:
        wakeup.notify(os.path.dirname(location))


def on_transaction_end(
    conn: sqlite3.Connection, *, on_commit: Callable[[], None] | None = None,
    on_rollback: Callable[[], None] | None = None,
) -> None:
    """Enlist a local effect in the current managed unit of work.

    A successful savepoint transfers its callbacks to its parent. Rollback calls only
    that scope's callbacks, in reverse order; commit callbacks run after the outermost
    COMMIT succeeds. Raw SQL transactions cannot enlist effects: their eventual outcome
    cannot be observed here. Durable recovery remains the effect owner's responsibility.
    """
    stack = _TRANSACTION_END.get(id(conn), [])
    if not stack or not stack[0].owns_transaction:
        raise SchemaInvalid("a transactional effect needs an outer store.transaction, not a raw SQL transaction")
    if on_commit is not None:
        stack[-1].commit.append(on_commit)
    if on_rollback is not None:
        stack[-1].rollback.append(on_rollback)


def _finish_transaction(callbacks: Sequence[Callable[[], None]], *, committed: bool,
                        original: BaseException | None = None) -> None:
    failures: list[BaseException] = []
    for callback in callbacks:
        try:
            callback()
        except BaseException as exc:
            failures.append(exc)
    if failures:
        raise OutcomeUnknown(
            "the database transaction ended but a local effect could not be reconciled; inspect its durable journal",
            details={"database_committed": committed,
                     "transaction_error": type(original).__name__ if original else None,
                     "effect_errors": [{"type": type(exc).__name__, "message": str(exc)[:1000]}
                                       for exc in failures]},
        ) from (original or failures[0])


@contextmanager
def transaction(
    conn: sqlite3.Connection, *, transaction_owner: str = "unattributed",
    operation: str = "unattributed", run_id: str | None = None,
    created_run: dict[str, Any] | None = None,
) -> Iterator[sqlite3.Connection]:
    """A re-entrant unit of work.

    The outermost call issues ``BEGIN IMMEDIATE`` so two writers serialise rather than
    one discovering the conflict at COMMIT. A nested call opens a SAVEPOINT instead, so
    services compose: a job submit can wrap a capability check, a job row and an
    artifact row in one atomic unit even though each of those also wants to be atomic on
    its own. Without this, every service grows its own join-or-begin helper and the
    copies drift apart.

    Rolling back an inner block releases only its savepoint; the outer block decides
    whether the whole unit survives. Optional telemetry measures the physical writer
    once. Callers supply constant owner/operation labels and the run they already own.
    """
    key = id(conn)
    depth = _SAVEPOINT_DEPTH.get(key, 0)
    outer = depth == 0 and not conn.in_transaction
    name = f"gx_sp_{depth + 1}"
    timed = outer and metrics.enabled()
    if timed:
        started = time.monotonic_ns()
    try:
        conn.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {name}")
    except sqlite3.Error:
        if timed:
            elapsed = (time.monotonic_ns() - started) / 1_000_000
            metrics.record_transaction(transaction_owner, operation, run_id, elapsed, 0., 0.,
                                       "begin_failed", 0)
        raise
    if timed:
        acquired = time.monotonic_ns()
    released = None
    stack = _TRANSACTION_END.setdefault(key, [])
    # total_changes is a connection counter, including writes later undone. Track
    # discarded savepoints in memory so rows_changed describes committed row changes.
    counting = outer or (stack and stack[0].changes_at_entry is not None)
    frame = _TransactionEnd(owns_transaction=outer,
                            changes_at_entry=conn.total_changes if counting else None)
    stack.append(frame)
    _SAVEPOINT_DEPTH[key] = depth + 1
    try:
        try:
            yield conn
        finally:
            if timed:
                body_end = time.monotonic_ns()
        conn.execute("COMMIT" if outer else f"RELEASE {name}")
    except BaseException as exc:
        # COMMIT itself can fail (for example a deferred constraint); the effects must
        # then be undone just as they are after an exception in the body.
        try:
            if outer:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
        except BaseException as rollback_error:
            raise OutcomeUnknown("the database transaction could not be rolled back; local journals were retained",
                                 details={"transaction_error": type(exc).__name__,
                                          "rollback_error": str(rollback_error)[:1000]}) from exc
        if timed:
            released = time.monotonic_ns()
            result, rows_changed = "rollback", 0
        elif not outer and frame.changes_at_entry is not None:
            stack[-2].discarded_changes += conn.total_changes - frame.changes_at_entry
        _finish_transaction(list(reversed(frame.rollback)), committed=False, original=exc)
        raise
    else:
        if outer:
            rows_changed = conn.total_changes - frame.changes_at_entry - frame.discarded_changes
            if timed:
                released = time.monotonic_ns()
                result = "commit"
            try:
                _finish_transaction(frame.commit, committed=True)
            finally:
                if rows_changed and conn not in driver_connections:
                    _notify_driver(conn)
        elif len(stack) > 1:
            stack[-2].commit.extend(frame.commit)
            stack[-2].rollback.extend(frame.rollback)
            stack[-2].discarded_changes += frame.discarded_changes
    finally:
        stack.pop()
        if not stack:
            _TRANSACTION_END.pop(key, None)
        if depth:
            _SAVEPOINT_DEPTH[key] = depth
        else:
            _SAVEPOINT_DEPTH.pop(key, None)
        if timed and released is not None:
            metrics.record_transaction(transaction_owner, operation, (created_run or {}).get("run_id", run_id),
                                       (acquired - started) / 1_000_000,
                                       (body_end - acquired) / 1_000_000,
                                       (released - body_end) / 1_000_000, result, rows_changed)


def _j(value: Any) -> str:
    return ids.canonical_json(value)


_COLUMN_CACHE: dict[tuple[int, str], frozenset[str]] = {}


def _columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    key = (id(conn), table)
    cached = _COLUMN_CACHE.get(key)
    if cached is None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cached = frozenset(row["name"] for row in rows)
        _COLUMN_CACHE[key] = cached
    return cached


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in _columns(conn, table)


def loads(row: sqlite3.Row | None, column: str, default: Any = None) -> Any:
    if row is None:
        return default
    raw = row[column]
    if raw in (None, ""):
        return default
    return json.loads(raw)


# ---------------------------------------------------------------------------- events

def append_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    actor: str,
    run_id: str | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    prev_state: str | None = None,
    new_state: str | None = None,
    prev_revision: int | None = None,
    new_revision: int | None = None,
    reason: str | None = None,
    evidence_level: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    if evidence_level is not None:
        contracts.require_label("evidence_level", evidence_level)
    cur = conn.execute(
        "INSERT INTO events(run_id, ts, kind, actor, subject_kind, subject_id, prev_state,"
        " new_state, prev_revision, new_revision, reason, evidence_level, payload_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, clock.now_iso(), kind, actor, subject_kind, subject_id, prev_state,
            new_state, prev_revision, new_revision, reason, evidence_level, _j(payload or {}),
        ),
    )
    return int(cur.lastrowid)


def events_for(conn: sqlite3.Connection, run_id: str, *, limit: int = 200) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY event_id LIMIT ?", (run_id, limit)
        )
    )


# -------------------------------------------------------------------------- revisions

def current_revision(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute("SELECT revision FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such run {run_id!r}")
    return int(row["revision"])


def check_revision(conn: sqlite3.Connection, run_id: str, expected: int | None) -> int:
    actual = current_revision(conn, run_id)
    if expected is not None and int(expected) != actual:
        raise StaleRevision(
            f"run {run_id} is at revision {actual}, caller expected {expected}",
            details={"run_id": run_id, "expected": expected, "actual": actual},
        )
    return actual


def bump_revision(conn: sqlite3.Connection, run_id: str, expected: int | None = None) -> tuple[int, int]:
    prev = check_revision(conn, run_id, expected)
    new = prev + 1
    conn.execute(
        "UPDATE runs SET revision=?, updated_at=? WHERE run_id=?",
        (new, clock.now_iso(), run_id),
    )
    return prev, new


# ------------------------------------------------------------------------ transitions

@dataclass
class TransitionResult:
    subject_id: str
    machine: str
    prev_state: str
    new_state: str
    prev_revision: int | None = None
    new_revision: int | None = None
    event_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_SUBJECTS: dict[str, tuple[str, str, str]] = {
    # machine -> (table, primary key column, status column). Every machine of
    # contracts/states.json that has rows; plugin_runtime describes processes, not rows.
    "run": ("runs", "run_id", "status"),
    "path": ("paths", "path_id", "status"),
    "task": ("tasks", "task_id", "status"),
    "team": ("teams", "team_id", "status"),
    "job": ("jobs", "job_id", "status"),
    "asset": ("assets", "asset_id", "status"),
    "approval": ("approvals", "approval_id", "status"),
    "evidence": ("evidence", "evidence_id", "status"),
    "eval_spec": ("eval_specs", "eval_spec_id", "status"),
    "environment": ("environments", "env_id", "status"),
    "source": ("sources", "source_id", "status"),
    "gap": ("gaps", "gap_id", "status"),
    "objective": ("objectives", "objective_id", "status"),
    "rsi_cycle": ("rsi_cycles", "cycle_id", "status"),
    "worker_observation": ("worker_observations", "worker_observation_id", "state"),
    "reference_review": ("references_reviewed", "reference_id", "review_state"),
}

#: Machines whose rows are addressed by ``(id, version)``.
_VERSIONED = frozenset({"asset", "eval_spec"})


def transition(
    conn: sqlite3.Connection,
    *,
    machine: str,
    subject_id: str,
    to_state: str,
    actor: str,
    run_id: str | None = None,
    expected_revision: int | None = None,
    reason: str | None = None,
    extra_updates: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    evidence_level: str | None = None,
    bump: bool = True,
    version: str | None = None,
) -> TransitionResult:
    """Check, apply and record a state change as one unit.

    Caller must already be inside :func:`transaction`.

    ``bump`` moves the run's revision. It is right for a change that a coordinator's
    next run-level write should have to look at again (a run transition, an accepted
    plan). It is wrong for what a worker does under a task lease: sixteen workers
    claiming in parallel would each invalidate the others' ``expected_revision``, and a
    query that settles a lapsed lease would move the revision by being asked. Those
    callers pass ``bump=False``; the lease is what protects them.
    """
    if machine not in _SUBJECTS:
        raise KeyError(f"no table mapping for machine {machine!r}")
    table, pk, status_col = _SUBJECTS[machine]

    if machine in _VERSIONED:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {pk}=? AND version=?", (subject_id, version)
        ).fetchone()
    else:
        row = conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (subject_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such {machine} {subject_id!r}")

    prev_state = row[status_col]
    machines.check_transition(machine, prev_state, to_state, subject_id=subject_id)

    effective_run = run_id
    if effective_run is None and machine == "run":
        effective_run = subject_id
    elif effective_run is None and "run_id" in row.keys():
        effective_run = row["run_id"]

    prev_rev = new_rev = None
    if effective_run is not None:
        if bump:
            prev_rev, new_rev = bump_revision(conn, effective_run, expected_revision)
        else:
            prev_rev = new_rev = check_revision(conn, effective_run, expected_revision)
    # The event of a change that did not move the revision carries no revision: an event
    # with one says "the run's revision went from this to that", and here it did not.
    event_prev, event_new = (prev_rev, new_rev) if bump else (None, None)

    sets = [f"{status_col}=?"]
    params: list[Any] = [to_state]
    if _has_column(conn, table, "updated_at"):
        # Not every subject table carries one: `jobs` tracks submitted_at/observed_at/
        # finished_at instead, and `assets` tracks promoted_at. Setting it unconditionally
        # made job and asset transitions fail with "no such column".
        sets.append("updated_at=?")
        params.append(clock.now_iso())
    for column, value in (extra_updates or {}).items():
        sets.append(f"{column}=?")
        params.append(_j(value) if isinstance(value, (dict, list)) else value)
    if machine in _VERSIONED:
        params.extend([subject_id, version])
        where = f"{pk}=? AND version=?"
    else:
        params.append(subject_id)
        where = f"{pk}=?"
    conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE {where}", params)

    event_id = append_event(
        conn,
        kind=f"{machine}.transition",
        actor=actor,
        run_id=effective_run,
        subject_kind=machine,
        subject_id=subject_id,
        prev_state=prev_state,
        new_state=to_state,
        prev_revision=event_prev,
        new_revision=event_new,
        reason=reason,
        evidence_level=evidence_level,
        payload=payload,
    )
    if machine == "run" and "PAUSED" in (prev_state, to_state):
        from ..p0 import waits
        waits.on_run_transition(conn, row, to_state, event_id)
    return TransitionResult(
        subject_id=subject_id,
        machine=machine,
        prev_state=prev_state,
        new_state=to_state,
        prev_revision=prev_rev,
        new_revision=new_rev,
        event_id=event_id,
    )


# ------------------------------------------------------------------------- idempotency

@dataclass
class IdempotencyRecord:
    key: str
    scope: str
    fresh: bool
    replay: bool
    response: dict[str, Any] | None
    side_effect_status: str
    created_at: str
    note: str | None = None
    """Diagnostic text from :func:`mark_idempotent_unknown`, never an answer to return.

    It is carried here rather than in ``response`` precisely so that a caller cannot
    hand it back as the result of the operation.
    """

    unverified_response: dict[str, Any] | None = None
    """A completed response whose side effect is ``UNKNOWN``.

    Real content — it may carry the external reference a reconciler needs — but not an
    answer: nobody confirmed the effect applied. Reconcile against the external system
    and then complete the record properly. Never return this to a caller as a result.
    """


def begin_idempotent(
    conn: sqlite3.Connection, key: str, scope: str, request: dict[str, Any]
) -> IdempotencyRecord:
    """Claim an idempotency key before performing a side effect.

    Three outcomes, which callers must handle differently:

    * ``fresh`` — first time; go ahead, then call :func:`complete_idempotent`.
    * ``replay`` — a *completed* response exists; return it without redoing the work.
    * neither — the key was claimed but never completed. The side effect may have
      happened. Reconcile against the external system; never blindly resubmit.

    ``replay`` is keyed on ``completed_at``, not on the mere presence of
    ``response_json``. :func:`mark_idempotent_unknown` records a diagnostic note in
    that column while deliberately leaving the row uncompleted, so testing
    ``response is not None`` reported the third outcome as a replay and handed the
    note back as the operation's answer — "we do not know" arriving as a success
    payload. Seven call sites in this tree return ``record.response`` on seeing
    ``record.replay``; only one of them also checked ``side_effect_status``. The
    invariant therefore belongs here, once, rather than in each of them.
    """
    request_sha = ids.sha256_of_obj(request)
    now = clock.now_iso()
    try:
        conn.execute(
            "INSERT INTO idempotency(idempotency_key, scope, request_sha, created_at)"
            " VALUES(?,?,?,?)",
            (key, scope, request_sha, now),
        )
        return IdempotencyRecord(key, scope, True, False, None, "NONE", now)
    except sqlite3.IntegrityError:
        pass

    row = conn.execute(
        "SELECT * FROM idempotency WHERE idempotency_key=?", (key,)
    ).fetchone()
    if row is None:  # pragma: no cover - only under concurrent delete
        raise OutcomeUnknown("idempotency row vanished")
    if row["request_sha"] != request_sha:
        raise IdempotencyConflict(
            f"idempotency key {key!r} was used with a different request body",
            details={"key": key, "scope": row["scope"], "stored_scope": row["scope"]},
        )
    stored = loads(row, "response_json")
    completed = row["completed_at"] is not None
    effect_unknown = row["side_effect_status"] == "UNKNOWN"
    # ``replay`` means "safe to return this as the answer", which requires both that the
    # row completed AND that its side effect is known to have applied. The two conditions
    # close two different doors onto the same defect: ``mark_idempotent_unknown`` leaves
    # the row uncompleted, while ``complete_idempotent(..., side_effect_status="UNKNOWN")``
    # stores a real response whose external effect nobody confirmed. Returning either one
    # tells the caller the work is done when it may not be.
    replay = completed and stored is not None and not effect_unknown
    return IdempotencyRecord(
        key=key,
        scope=row["scope"],
        fresh=False,
        replay=replay,
        # Anything that is not a replay is withheld from ``response`` by construction, so
        # a caller cannot return it as a result even by accident.
        response=stored if replay else None,
        side_effect_status=row["side_effect_status"],
        created_at=row["created_at"],
        note=(stored or {}).get("note") if not completed else None,
        unverified_response=stored if (completed and effect_unknown) else None,
    )


def complete_idempotent(
    conn: sqlite3.Connection,
    key: str,
    response: dict[str, Any],
    *,
    side_effect_status: str = "APPLIED",
) -> None:
    if side_effect_status not in contracts.errors()["side_effect_status"]:
        raise ValueError(f"unknown side_effect_status {side_effect_status!r}")
    conn.execute(
        "UPDATE idempotency SET response_json=?, side_effect_status=?, completed_at=?"
        " WHERE idempotency_key=?",
        (_j(response), side_effect_status, clock.now_iso(), key),
    )


def mark_idempotent_unknown(conn: sqlite3.Connection, key: str, note: str = "") -> None:
    """Record that a side effect's outcome is unknown, so recovery reconciles.

    ``completed_at`` is deliberately left NULL: the request was claimed and never
    completed, which is exactly the third outcome :func:`begin_idempotent` documents.
    The note lands in ``response_json`` for the audit trail but is surfaced as
    ``IdempotencyRecord.note``, never as ``response``.
    """
    conn.execute(
        "UPDATE idempotency SET side_effect_status='UNKNOWN', response_json=? WHERE idempotency_key=?",
        (_j({"note": note}), key),
    )


def forget_idempotent(conn: sqlite3.Connection, key: str) -> None:
    """Give back a key claimed in this transaction, for a request that was refused.

    For the one shape :func:`run_idempotent` cannot express: a refusal that must leave a
    record behind (an observation of the worker that was turned away). The caller keeps
    the transaction, drops the claim and raises after the commit, so the same key can be
    used for the corrected request and is not left "claimed and never completed".
    """
    conn.execute(
        "DELETE FROM idempotency WHERE idempotency_key=? AND completed_at IS NULL", (key,)
    )


def stored_response(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """The completed response stored under ``key``, or None. Reads only."""
    row = conn.execute("SELECT response_json, completed_at FROM idempotency WHERE idempotency_key=?",
                       (key,)).fetchone()
    if row is None or row["completed_at"] is None or not row["response_json"]:
        return None
    return json.loads(row["response_json"])


def settle_answer(conn: sqlite3.Connection, key: str, answer: dict[str, Any]) -> dict[str, Any]:
    """Store ``answer`` as the answer of a completed key unless one is stored; return the stored one.

    For a keyed call whose work goes on after its key was completed (a run start goes on
    to run its workflow): the first complete answer written wins, and every call with the
    key -- the first one included -- returns that one, so the same request under the same
    key gets the same answer however many processes sent it. The key must be completed.
    """
    with transaction(conn, transaction_owner="Store", operation="settle_answer", run_id=answer.get("run_id")) as tx:
        stored = stored_response(tx, key)
        if stored is None:
            raise OutcomeUnknown(f"idempotency key {key!r} has no completed record to settle an answer on",
                                 details={"idempotency_key": key})
        if stored.get("answer") is None:
            stored["answer"] = answer
            tx.execute("UPDATE idempotency SET response_json=? WHERE idempotency_key=?", (_j(stored), key))
        # Read back, so the writer returns the stored form too -- the same keys in the same
        # order as every later call gets, not the dict it happened to build.
        return dict(stored_response(tx, key)["answer"])


def tool_key(tool: str, key: str) -> str:
    """The stored form of a client's idempotency key for one tool.

    The ``idempotency`` table is keyed on the key alone. A client that numbers its keys
    ``k-1, k-2`` per tool would otherwise collide across tools and be told it had sent
    "a different request body" under a key it never used for that tool.
    """
    return f"tool:{tool}:{key}"


def run_idempotent(
    conn: sqlite3.Connection, key: str, scope: str, request: dict[str, Any],
    fn: Callable[[sqlite3.Connection], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Run ``fn`` at most once for ``key`` inside one transaction. Returns ``(response, replayed)``.

    The claim, the work and the completion share one unit, so a refusal rolls the claim
    back with everything else and the key can be used again -- a refused request has
    not been made. A completed key returns its stored response without running ``fn``.

    Use it for a side effect that lives entirely in the store. A call that reaches
    outside (a scheduler, a download) must persist its intent before the outside call
    and reconcile afterwards, which one enclosing transaction cannot do.
    """
    starting_run = scope == "gx_run_start"
    created_run = {} if starting_run else None
    with transaction(conn, transaction_owner="Run" if starting_run else "Store",
                     operation="run_start" if starting_run else "run_idempotent",
                     run_id=request.get("run_id"), created_run=created_run) as tx:
        record = begin_idempotent(tx, key, scope, request)
        if record.replay and record.response is not None:
            if starting_run:
                created_run["run_id"] = record.response.get("run_id")
            return dict(record.response), True
        if not record.fresh:
            raise OutcomeUnknown(
                f"idempotency key {key!r} was claimed and never completed; the effect may "
                "have been applied",
                details={"idempotency_key": key, "scope": scope,
                         "side_effect_status": record.side_effect_status},
            )
        response = fn(tx)
        if starting_run:
            created_run["run_id"] = response["run_id"]
        complete_idempotent(tx, key, response)
        return response, False


# ------------------------------------------------------------------------------ leases

def issue_lease(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    owner: str,
    worker_role: str,
    seconds: int,
    attempt: int,
    principal: str | None = None,
) -> sqlite3.Row:
    """Issue an exclusive lease, refusing while another unexpired lease is live.

    ``owner`` is the actor string that may submit under the lease. ``principal`` is the
    subject behind it without transport or scope, which is what an independence check
    compares: the same worker reaching the backend over the command line and over MCP
    is one principal, not two.
    """
    live = conn.execute(
        "SELECT * FROM leases WHERE task_id=? AND released_at IS NULL", (task_id,)
    ).fetchall()
    for row in live:
        if not clock.is_past(row["expires_at"]):
            raise LeaseHeld(
                f"task {task_id} is leased by {row['owner']!r} until {row['expires_at']}",
                details={"task_id": task_id, "owner": row["owner"], "expires_at": row["expires_at"]},
            )
        conn.execute(
            "UPDATE leases SET released_at=? WHERE lease_id=?",
            (clock.now_iso(), row["lease_id"]),
        )
    lease_id = ids.new_id("lease")
    conn.execute(
        "INSERT INTO leases(lease_id, task_id, owner, principal, worker_role, issued_at,"
        " expires_at, attempt) VALUES(?,?,?,?,?,?,?,?)",
        (lease_id, task_id, owner, principal, worker_role, clock.now_iso(),
         clock.iso_after(seconds), attempt),
    )
    return conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()


def validate_lease(
    conn: sqlite3.Connection, lease_ref: str, *, task_id: str | None = None
) -> sqlite3.Row:
    """Return the live lease or raise. A timed-out former owner cannot submit."""
    row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_ref,)).fetchone()
    if row is None:
        raise LeaseExpired(f"no such lease {lease_ref!r}", details={"lease_ref": lease_ref})
    if row["released_at"] is not None:
        raise LeaseExpired(
            f"lease {lease_ref} was released at {row['released_at']}",
            details={"lease_ref": lease_ref, "released_at": row["released_at"]},
        )
    if clock.is_past(row["expires_at"]):
        raise LeaseExpired(
            f"lease {lease_ref} expired at {row['expires_at']}",
            details={"lease_ref": lease_ref, "expires_at": row["expires_at"]},
        )
    if task_id is not None and row["task_id"] != task_id:
        raise LeaseExpired(
            f"lease {lease_ref} belongs to task {row['task_id']}, not {task_id}",
            details={"lease_ref": lease_ref, "lease_task": row["task_id"], "task_id": task_id},
        )
    return row


def release_lease(conn: sqlite3.Connection, lease_ref: str) -> None:
    conn.execute(
        "UPDATE leases SET released_at=? WHERE lease_id=? AND released_at IS NULL",
        (clock.now_iso(), lease_ref),
    )


def expire_stale_leases(conn: sqlite3.Connection) -> list[str]:
    """Release every lease whose deadline passed. Returns the affected task ids."""
    rows = conn.execute("SELECT * FROM leases WHERE released_at IS NULL").fetchall()
    touched = []
    for row in rows:
        if clock.is_past(row["expires_at"]):
            conn.execute(
                "UPDATE leases SET released_at=? WHERE lease_id=?",
                (clock.now_iso(), row["lease_id"]),
            )
            touched.append(row["task_id"])
    return touched


# ----------------------------------------------------------------- coordinator lease

COORDINATOR_LEASE_SECONDS = 3600


def coordinator_lease(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM coordinator_leases WHERE run_id=?", (run_id,)).fetchone()


def coordinator_lease_live(row: sqlite3.Row | None) -> bool:
    return row is not None and row["released_at"] is None and not clock.is_past(row["expires_at"])


def acquire_coordinator_lease(
    conn: sqlite3.Connection, *, run_id: str, host: str, owner: str,
    seconds: int = COORDINATOR_LEASE_SECONDS, force_handoff_reason: str | None = None,
    session_ref: str | None = None, actor: str | None = None,
) -> sqlite3.Row:
    """One effective coordinator per run, so two clients cannot both dispatch work.

    A live lease held by somebody else refuses with ``LEASE_HELD``. The same owner on the
    same host may claim again -- a coordinator that restarted and lost its reference --
    and gets a new lease id, which retires the old one. Whoever is given the lease, the
    previous holder's reference stops working from this row onward: every run-level
    write goes through :func:`require_coordinator_lease`.
    """
    row = coordinator_lease(conn, run_id)
    if coordinator_lease_live(row):
        same = row["host"] == host and row["owner"] == owner
        if not same and force_handoff_reason is None:
            raise LeaseHeld(
                f"run {run_id} is coordinated by {row['owner']!r} on {row['host']!r} until {row['expires_at']}",
                details={"run_id": run_id, "owner": row["owner"], "host": row["host"],
                         "expires_at": row["expires_at"]},
            )
    previous = None if row is None else {
        "owner": row["owner"], "host": row["host"], "lease_id": row["lease_id"],
        "expired": clock.is_past(row["expires_at"]), "released_at": row["released_at"],
    }
    # Its own id kind, so a task lease handed in where the coordinator lease is wanted is
    # a visibly different thing and not merely a row that fails to match.
    lease_id = ids.new_id("coordinator_lease")
    now = clock.now_iso()
    conn.execute(
        "INSERT INTO coordinator_leases(run_id, lease_id, host, owner, session_ref, issued_at,"
        " renewed_at, expires_at, handoff_reason) VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(run_id) DO UPDATE SET lease_id=excluded.lease_id, host=excluded.host,"
        " owner=excluded.owner, session_ref=excluded.session_ref, issued_at=excluded.issued_at,"
        " renewed_at=NULL, expires_at=excluded.expires_at, released_at=NULL,"
        " handoff_reason=excluded.handoff_reason",
        (run_id, lease_id, host, owner, session_ref, now, None, clock.iso_after(seconds),
         force_handoff_reason),
    )
    append_event(
        conn, kind="run.coordinator_lease", actor=actor or owner, run_id=run_id,
        subject_kind="run", subject_id=run_id,
        reason=force_handoff_reason or ("acquired" if previous is None else "taken over"),
        evidence_level="EXECUTION_OBSERVED",
        payload={"host": host, "owner": owner, "lease_id": lease_id, "previous": previous,
                 "session_ref": session_ref},
    )
    return coordinator_lease(conn, run_id)


def require_coordinator_lease(
    conn: sqlite3.Connection, *, run_id: str, lease_ref: str | None, renew: bool = True,
) -> sqlite3.Row:
    """Refuse a run-level write unless ``lease_ref`` is the run's live coordinator lease.

    This is the check v1 never made: its lease was a row that only tests wrote, so a
    coordinator whose lease had lapsed and been taken over could still claim, submit and
    resume the run to its end. An accepted write renews the lease for the length it was
    issued for, so a coordinator that keeps working keeps the run.
    """
    row = coordinator_lease(conn, run_id)
    if row is None:
        raise LeaseExpired(
            f"run {run_id} has no coordinator lease; claim it with gx_coordinator_claim",
            details={"run_id": run_id, "reason": "no_coordinator_lease"},
        )
    holder = {"owner": row["owner"], "host": row["host"], "expires_at": row["expires_at"]}
    if not lease_ref or row["lease_id"] != lease_ref:
        raise LeaseExpired(
            f"{lease_ref!r} is not the coordinator lease of run {run_id}: it is held by "
            f"{row['owner']!r} on {row['host']!r}",
            details={"run_id": run_id, "reason": "not_the_coordinator_lease", "holder": holder},
        )
    if row["released_at"] is not None:
        raise LeaseExpired(
            f"coordinator lease {lease_ref} was released at {row['released_at']}",
            details={"run_id": run_id, "reason": "released", "released_at": row["released_at"]},
        )
    if clock.is_past(row["expires_at"]):
        raise LeaseExpired(
            f"coordinator lease {lease_ref} expired at {row['expires_at']}; claim the run "
            "again with gx_coordinator_claim before writing to it",
            details={"run_id": run_id, "reason": "expired", "expires_at": row["expires_at"]},
        )
    if renew:
        granted_from = row["renewed_at"] or row["issued_at"]
        length = max(1.0, (clock.parse(row["expires_at"]) - clock.parse(granted_from)).total_seconds())
        conn.execute(
            "UPDATE coordinator_leases SET renewed_at=?, expires_at=? WHERE run_id=?",
            (clock.now_iso(), clock.iso_after(length), run_id),
        )
        row = coordinator_lease(conn, run_id)
    return row


def release_coordinator_lease(
    conn: sqlite3.Connection, *, run_id: str, lease_ref: str, actor: str, reason: str | None = None,
) -> sqlite3.Row:
    row = require_coordinator_lease(conn, run_id=run_id, lease_ref=lease_ref, renew=False)
    conn.execute(
        "UPDATE coordinator_leases SET released_at=?, handoff_reason=? WHERE run_id=?",
        (clock.now_iso(), reason, run_id),
    )
    append_event(
        conn, kind="run.coordinator_lease", actor=actor, run_id=run_id,
        subject_kind="run", subject_id=run_id, reason=reason or "released",
        evidence_level="EXECUTION_OBSERVED",
        payload={"host": row["host"], "lease_id": lease_ref, "released": True},
    )
    return coordinator_lease(conn, run_id)


# ----------------------------------------------------------------------------- outbox

#: The topics of a trigger row (``docs/v2/SCHEDULING_DESIGN.md`` r2.2 §2.4.1), each written
#: by the stream whose transaction makes its fact true: core, exec, govern (two), host (two).
#: They are the label axis ``trigger_topic`` of contract r5 (:func:`trigger_topics`).
#: What every trigger's payload carries: the run the fact belongs to (None for a fact of
#: the store that no run owns), the id of what the fact is about, and the event that
#: records it (None only when another table records it, and the payload then names that row).
TRIGGER_KEYS = ("run_id", "subject", "event_id")


def trigger_topics() -> tuple[str, ...]:
    """The topics of labels.trigger_topic (contract r5): the one list of what a trigger row may be."""
    return tuple(contracts.label_values("trigger_topic"))


def enqueue_outbox(
    conn: sqlite3.Connection, *, topic: str, payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> int:
    """Write one outbox row inside the caller's transaction. Returns its id.

    A row of a trigger topic is what a reaction is started from, and five streams write
    them, so its shape is checked here, where a wrong row fails in its writer's own test
    and not silently in the reader: the payload carries :data:`TRIGGER_KEYS` with a
    subject, and the key is ``<topic>:<occurrence>``. Everything else in a payload is
    identifiers and state words. A trigger says "look": the reaction reads the fact from
    its owner's rows, so a stale or forged row moves nothing.

    One row per key, ever: with a key that already has a row nothing is written and that
    row's id is returned, so a fact announced twice starts one reaction. When the store
    has ``outbox.run_id`` (contract r5) the payload's run is written there too; no writer
    changes its call for that.
    """
    if topic in trigger_topics():
        missing = [key for key in TRIGGER_KEYS if key not in payload]
        if missing or not isinstance(payload.get("subject"), str) or not payload["subject"]:
            raise SchemaInvalid(
                f"a {topic} trigger carries {', '.join(TRIGGER_KEYS)} in its payload, with the id of "
                f"what the fact is about as subject; this one lacks {', '.join(missing) or 'a subject'}",
                details={"topic": topic, "missing": missing, "payload_keys": sorted(payload)},
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.startswith(topic + ":") \
                or len(idempotency_key) <= len(topic) + 1:
            raise SchemaInvalid(
                f"a {topic} trigger is keyed '{topic}:<occurrence>', one row per occurrence of the fact",
                details={"topic": topic, "idempotency_key": idempotency_key},
            )
    if idempotency_key is not None:
        existing = conn.execute(
            "SELECT outbox_id FROM outbox WHERE idempotency_key=? ORDER BY outbox_id LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return int(existing["outbox_id"])
    if _has_column(conn, "outbox", "run_id"):
        cur = conn.execute(
            "INSERT INTO outbox(created_at, topic, payload_json, idempotency_key, run_id) VALUES(?,?,?,?,?)",
            (clock.now_iso(), topic, _j(payload), idempotency_key, payload.get("run_id")),
        )
    else:
        cur = conn.execute(
            "INSERT INTO outbox(created_at, topic, payload_json, idempotency_key) VALUES(?,?,?,?)",
            (clock.now_iso(), topic, _j(payload), idempotency_key),
        )
    return int(cur.lastrowid)


def pending_triggers(conn: sqlite3.Connection, run_id: str, *, limit: int = 200,
                     topics: Sequence[str] | None = None) -> list[sqlite3.Row]:
    """The trigger rows of a run that no reaction has consumed, oldest first. Reads only.

    A row is the run's by its payload, and by the column where the store has one: a row
    written before the column existed carries the run in its payload alone. ``topics``
    narrows it to some trigger topics: the reactor asks only for those it has a reaction
    to, so rows that wait for a reaction not built yet never take the window of those it can read.
    """
    known = trigger_topics()
    topics = known if topics is None else tuple(topic for topic in topics if topic in known)
    if not topics:
        return []
    of_run = "json_extract(payload_json, '$.run_id')"
    if _has_column(conn, "outbox", "run_id"):
        of_run = f"COALESCE(run_id, {of_run})"
    return conn.execute(
        f"SELECT * FROM outbox WHERE consumed_at IS NULL AND topic IN ({','.join('?' for _ in topics)})"
        f" AND {of_run}=? ORDER BY outbox_id LIMIT ?", (*topics, run_id, int(limit)),
    ).fetchall()


def claim_outbox(
    conn: sqlite3.Connection, *, consumer: str, topics: Sequence[str] | None = None, limit: int = 20
) -> list[sqlite3.Row]:
    if topics:
        placeholders = ",".join("?" for _ in topics)
        query = (
            f"SELECT * FROM outbox WHERE consumed_at IS NULL AND topic IN ({placeholders})"
            " ORDER BY outbox_id LIMIT ?"
        )
        rows = conn.execute(query, (*topics, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE consumed_at IS NULL ORDER BY outbox_id LIMIT ?", (limit,)
        ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE outbox SET attempts=attempts+1 WHERE outbox_id=?", (row["outbox_id"],)
        )
    return list(rows)


def complete_outbox(conn: sqlite3.Connection, outbox_id: int, consumer: str) -> bool:
    """Mark a row consumed. False when another call had consumed it already."""
    cur = conn.execute(
        "UPDATE outbox SET consumed_at=?, consumer=? WHERE outbox_id=? AND consumed_at IS NULL",
        (clock.now_iso(), consumer, outbox_id),
    )
    return cur.rowcount == 1


def fail_outbox(conn: sqlite3.Connection, outbox_id: int, error: str) -> None:
    """Record why the last attempt on a row failed, and that there was one."""
    conn.execute("UPDATE outbox SET last_error=?, attempts=attempts+1 WHERE outbox_id=?",
                 (error[:2000], outbox_id))


# ---------------------------------------------------------------------------- budgets

def create_budget(
    conn: sqlite3.Connection, *, limits_json: dict[str, Any], run_id: str | None = None,
    parent_ref: str | None = None,
) -> str:
    """A child budget may never exceed what remains of its parent."""
    budget_ref = ids.new_id("budget")
    if parent_ref is not None:
        parent = conn.execute(
            "SELECT * FROM budgets WHERE budget_ref=?", (parent_ref,)
        ).fetchone()
        if parent is None:
            raise ArtifactNotFound(f"no such parent budget {parent_ref!r}")
        parent_limits = loads(parent, "limits_json", {})
        parent_spent = loads(parent, "spent_json", {})
        parent_reserved = loads(parent, "reserved_json", {})
        for key, requested in limits_json.items():
            if not isinstance(requested, (int, float)) or isinstance(requested, bool):
                continue
            ceiling = parent_limits.get(key)
            if ceiling is None:
                continue
            available = ceiling - parent_spent.get(key, 0) - parent_reserved.get(key, 0)
            if requested > available:
                raise BudgetExceeded(
                    f"child budget {key}={requested} exceeds remaining parent {available}",
                                        details={"key": key, "requested": requested, "available": available},
                )
        for key, requested in limits_json.items():
            if isinstance(requested, (int, float)) and not isinstance(requested, bool):
                parent_reserved[key] = parent_reserved.get(key, 0) + requested
        conn.execute(
            "UPDATE budgets SET reserved_json=?, updated_at=? WHERE budget_ref=?",
            (_j(parent_reserved), clock.now_iso(), parent_ref),
        )
    now = clock.now_iso()
    conn.execute(
        "INSERT INTO budgets(budget_ref, parent_ref, run_id, limits_json, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (budget_ref, parent_ref, run_id, _j(limits_json), now, now),
    )
    return budget_ref


def spend_budget(conn: sqlite3.Connection, budget_ref: str, key: str, amount: float) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM budgets WHERE budget_ref=?", (budget_ref,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such budget {budget_ref!r}")
    limits_json = loads(row, "limits_json", {})
    spent = loads(row, "spent_json", {})
    ceiling = limits_json.get(key)
    new_total = spent.get(key, 0) + amount
    if ceiling is not None and new_total > ceiling:
        raise BudgetExceeded(
            f"budget {budget_ref} key {key}: {new_total} would exceed {ceiling}",
                        details={"budget_ref": budget_ref, "key": key, "ceiling": ceiling, "would_be": new_total},
        )
    spent[key] = new_total
    conn.execute(
        "UPDATE budgets SET spent_json=?, updated_at=? WHERE budget_ref=?",
        (_j(spent), clock.now_iso(), budget_ref),
    )
    return spent


def budget_state(conn: sqlite3.Connection, budget_ref: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM budgets WHERE budget_ref=?", (budget_ref,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such budget {budget_ref!r}")
    return {
        "budget_ref": budget_ref,
        "parent_ref": row["parent_ref"],
        "limits": loads(row, "limits_json", {}),
        "spent": loads(row, "spent_json", {}),
        "reserved": loads(row, "reserved_json", {}),
    }


# ------------------------------------------------------------------------- checkpoints

def save_checkpoint(
    conn: sqlite3.Connection, *, run_id: str, workflow_id: str, workflow_version: str,
    node: str, state: dict[str, Any], interrupt_ref: str | None = None,
) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM checkpoints WHERE run_id=?", (run_id,)
    ).fetchone()
    seq = int(row["s"]) + 1
    checkpoint_id = ids.new_id("checkpoint")
    conn.execute(
        "INSERT INTO checkpoints(checkpoint_id, run_id, workflow_id, workflow_version, node,"
        " seq, state_json, interrupt_ref, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (checkpoint_id, run_id, workflow_id, workflow_version, node, seq, _j(state),
         interrupt_ref, clock.now_iso()),
    )
    return checkpoint_id


def latest_checkpoint(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
    ).fetchone()


# ------------------------------------------------------------------------- approvals

def request_approval(
    conn: sqlite3.Connection, *, run_id: str | None, action_kind: str, request: dict[str, Any]
) -> str:
    approval_id = ids.new_id("approval")
    conn.execute(
        "INSERT INTO approvals(approval_id, run_id, action_kind, request_json, status, requested_at)"
        " VALUES(?,?,?,?,?,?)",
        (approval_id, run_id, action_kind, _j(request), "PENDING", clock.now_iso()),
    )
    append_event(
        conn, kind="approval.requested", actor="backend", run_id=run_id,
        subject_kind="approval", subject_id=approval_id, reason=action_kind,
        evidence_level="EXECUTION_OBSERVED", payload={"action_kind": action_kind},
    )
    return approval_id


def approval_status(conn: sqlite3.Connection, approval_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such approval {approval_id!r}")
    return row["status"]
