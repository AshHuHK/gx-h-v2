"""Verify read-only status and two writable opens on copied P2A ledgers with live holds and settled costs."""
import argparse
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
sys.path.insert(0, str(ROOT / "acceptance"))
from gx_core.p0.cli import existing_db
from v31.historical_migration import column_names, sql_hash, tables

FIXTURES = ROOT / "acceptance/phase2a"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def status(directory, rid):
    proc = subprocess.run([sys.executable, "-B", ROOT / "bin/gx-p0", "status", "--state", directory,
                           "--run-id", rid], cwd=ROOT, check=True, capture_output=True, text=True, timeout=30)
    value = json.loads(proc.stdout)
    assert value["run_id"] == rid
    return value


def verify(source, state, expected):
    state.mkdir()
    if "rollback" in source.parts:
        with tarfile.open(FIXTURES / "rollback/historical-project.tar.gz", "r:gz") as archive:
            # status reads the retained Artifact bytes for the historical scientific assessment.
            archive.extractall(state, members=(e for e in archive if e.name.startswith("artifacts/")), filter="data")
    dbfile = state / "gx.db"
    original = gzip.decompress(source.read_bytes())
    dbfile.write_bytes(original)
    with closing(sqlite3.connect(dbfile)) as db:
        rid, = db.execute("SELECT run_id FROM runs").fetchone()
        columns = {t: column_names(db, t) for t in tables(db) if t != "schema_meta"}
        hashes = {t: sql_hash(db, t, c) for t, c in columns.items()}
        prior_meta = dict(db.execute("SELECT key,value FROM schema_meta"))
        holdings = dict(db.execute("SELECT budget_status, count(*) FROM p0_meter_native_turns GROUP BY budget_status"))
        assert holdings == expected, holdings
    before = status(state, rid)
    assert digest(dbfile.read_bytes()) == digest(original), "cold status wrote to the old release DB"
    versions = []
    for open_number in (1, 2):
        with closing(existing_db(state, readonly=False)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            for table, old_cols in columns.items():
                assert sql_hash(db, table, old_cols) == hashes[table], f"old rows changed in {table}"
            meta = dict(db.execute("SELECT key,value FROM schema_meta"))
            assert prior_meta.items() <= meta.items(), "prior migration records changed"
            versions.append({t: sql_hash(db, t, column_names(db, t)) for t in tables(db)})
        current = status(state, rid)
        for key in ("status", "unsettled_turns", "jobs", "tasks"):
            assert current[key] == before[key], f"cold status differs after open {open_number} for {key}"
    assert versions[0] == versions[1], "second writable open changed state"
    return {"source": str(source.relative_to(ROOT)), "source_gzip_sha256": digest(source.read_bytes()),
            "source_db_sha256": digest(original), "run_id": rid, "status": before["status"],
            "original_tables_verified": len(columns), "historical_budget_statuses": holdings,
            "original_events": hashes["events"], "original_jobs": hashes["jobs"],
            "original_reservations": hashes["budget_reservations"],
            "added_migrations": sorted(meta.keys() - prior_meta.keys()),
            "unchanged_second_open": True, "read_only_status_calls": 3, "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="gx-closure-ledgers-") as tmp:
        results = [
            verify(FIXTURES / "cross-process/restart/after.db.gz", Path(tmp) / "active", {"HELD": 1}),
            verify(FIXTURES / "rollback/gx-b8e4f7baeba668ed.db.gz", Path(tmp) / "paused", {"SETTLED": 3}),
        ]
    args.out.write_text(json.dumps({"passed": True, "evidence": "SCRIPTED", "cases": results},
                                   ensure_ascii=False, indent=2) + "\n")
    print("Verified cold status and two writable opens on both historical projects")


if __name__ == "__main__":
    main()
