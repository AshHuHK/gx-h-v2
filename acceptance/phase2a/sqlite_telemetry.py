"""DB01/DB02. Exercise the real Driver writer and the SQLite transaction boundaries."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gx-h"))
from gx_core import metrics
from gx_core.p0.driver import enqueue
from gx_core.persistence import records, store
from performance import SQLITE_TIMES, transaction_summary


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json(path, data):
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


def contend(db, path, body_run, waiting_run, connection, content):
    """The first enqueue holds its writer until the second connection enters BEGIN."""
    connected, holding, attempting = threading.Event(), threading.Event(), threading.Event()

    def hold():
        holding.set()
        require(attempting.wait(10), "second connection never entered BEGIN IMMEDIATE")
        time.sleep(.15)
        return 0

    def other_writer():
        with closing(store.connect(path)) as other:
            connection.current = other
            other.set_trace_callback(lambda sql: attempting.set() if sql == "BEGIN IMMEDIATE" else None)
            connected.set()
            require(holding.wait(10), "first connection never acquired its writer")
            enqueue(other, waiting_run, {}, content, "waiting-message")

    db.create_function("hold_fixture_writer", 0, hold)
    db.execute("CREATE TEMP TRIGGER hold_enqueue BEFORE INSERT ON p0_operator_messages "
               "BEGIN SELECT hold_fixture_writer(); END")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(other_writer)
        require(connected.wait(10), "second SQLite connection failed to open")
        enqueue(db, body_run, {}, content, "holding-message")
        future.result(timeout=10)
    db.execute("DROP TRIGGER hold_enqueue")


class Undo(Exception):
    """An intentional failure in a managed transaction."""


def exercise_rollbacks(db, run_id, content):
    with store.transaction(db, transaction_owner="Acceptance", operation="nested_rollback", run_id=run_id):
        enqueue(db, run_id, {}, content, "retained-message")
        with store.transaction(db):
            try:
                with store.transaction(db):
                    enqueue(db, run_id, {}, content, "discarded-message")
                    raise Undo
            except Undo:
                pass
    try:
        with store.transaction(db, transaction_owner="Acceptance", operation="body_rollback", run_id=run_id):
            enqueue(db, run_id, {}, content, "outer-rollback-message")
            raise Undo
    except Undo:
        pass
    db.execute("CREATE TEMP TABLE parent(id INTEGER PRIMARY KEY)")
    db.execute("CREATE TEMP TABLE child(ref REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    try:
        with store.transaction(db, transaction_owner="Acceptance", operation="deferred_constraint", run_id=run_id):
            db.execute("INSERT INTO child VALUES(1)")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("the invalid deferred foreign key committed")
    # An externally opened transaction owns its own end. Its nested savepoints emit no writes.
    db.execute("BEGIN IMMEDIATE")
    enqueue(db, run_id, {}, content, "raw-rollback-message")
    db.execute("ROLLBACK")


def run(out):
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("the SQLite evidence directory must be empty")
    path, trace = out / "gx.db", out / "metrics.jsonl"
    content = "synthetic-private-input-for-telemetry-check"
    connection = threading.local()
    observed_writer_states = []
    emit = metrics.record

    def observe_sink(kind, **fields):
        if kind == "db_transaction":
            observed_writer_states.append(connection.current.in_transaction)
            require(not connection.current.in_transaction, "telemetry sink called inside the writer")
        emit(kind, **fields)

    with closing(store.open_store(path)) as db:
        body_run, waiting_run = [
            records.create_run(db, objective="SQLite writer fixture", project_ref="db-acceptance", actor="fixture")
            for _ in range(2)
        ]
        connection.current = db
        with patch.dict(os.environ, GX_METRICS_FILE=str(trace)), patch.object(metrics, "record", observe_sink):
            with metrics.identity(prompt=content, run_id="unrelated-ambient-run"):
                contend(db, path, body_run, waiting_run, connection, content)
                exercise_rollbacks(db, body_run, content)
                with closing(store.connect(path, timeout=.035)) as impatient:
                    connection.current = impatient
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        enqueue(impatient, waiting_run, {}, content, "timeout-message")
                    except sqlite3.OperationalError as exc:
                        require(exc.sqlite_errorcode == sqlite3.SQLITE_BUSY, "expected SQLite writer timeout")
                    else:
                        raise AssertionError("second writer acquired the held lock")
                    finally:
                        db.execute("ROLLBACK")
                        connection.current = db
        messages = sorted(row[0] for row in db.execute("SELECT message_id FROM p0_operator_messages"))
        require(messages == ["holding-message", "retained-message", "waiting-message"], "rollback lost isolation")
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check failed")
        require(not db.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign key check failed")

    raw = trace.read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    require(len(rows) == len(observed_writer_states) == 6, "expected six physical writer attempts")
    allowed_fields = {"kind", "observed_ns", "transaction_owner", "operation", "commit_or_rollback",
                      "rows_changed", "lock_acquire_ms", *SQLITE_TIMES, *metrics._FIELDS}
    require(all(row.keys() == allowed_fields for row in rows), "unexpected fields in database telemetry")
    require(content not in raw and "unrelated-ambient-run" not in raw, "content or unrelated run leaked")
    for row in rows:
        require(row["kind"] == "db_transaction" and row["run_id"] in (body_run, waiting_run), "wrong identity")
        total = row["lock_wait_ms"] + row["body_ms"] + row["finish_ms"]
        require(row["duration_ms"] == total and row["lock_acquire_ms"] == row["lock_wait_ms"], "timing mismatch")
    driver = [row for row in rows if (row["transaction_owner"], row["operation"]) == ("Driver", "enqueue_message")]
    require(len(driver) == 3, "missing production Driver attribution")
    holding, waiting = [next(r for r in driver if r["run_id"] == rid and r["commit_or_rollback"] == "commit")
                        for rid in (body_run, waiting_run)]
    timeout = next(r for r in driver if r["commit_or_rollback"] == "begin_failed")
    require(holding["rows_changed"] == waiting["rows_changed"] == 1, "wrong committed row count")
    require(holding["body_ms"] >= 120 and holding["body_ms"] > holding["lock_wait_ms"], "hold misattributed")
    require(waiting["lock_wait_ms"] >= 120 and waiting["lock_wait_ms"] > waiting["body_ms"], "wait misattributed")
    require(timeout["lock_wait_ms"] >= 20 and timeout["body_ms"] == timeout["finish_ms"] == timeout["rows_changed"] == 0,
            "failed admission counted a writer body")
    nested = {r["operation"]: (r["commit_or_rollback"], r["rows_changed"])
              for r in rows if r["transaction_owner"] == "Acceptance"}
    require(nested == {"nested_rollback": ("commit", 1), "body_rollback": ("rollback", 0),
                       "deferred_constraint": ("rollback", 0)}, "incorrect savepoint or rollback outcome")
    summary = transaction_summary(rows)
    for field in SQLITE_TIMES:
        require(summary["slow_samples"][field][0][field] == max(r[field] for r in rows), "missing slowest writer")
    write_json(out / "summary.json", summary)
    write_json(out / "observations.json", {
        "evidence_class": "SCRIPTED", "checks": {"DB01": True, "DB02": True},
        "holder_run_id": body_run, "waiting_run_id": waiting_run, "injected_hold_ms": 150,
        "persisted_messages": messages, "writer_attempts": len(rows), "metrics_under_writer": sum(observed_writer_states),
        "integrity_check": "ok", "foreign_key_violations": 0,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output)
