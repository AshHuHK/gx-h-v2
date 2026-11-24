"""Verify read-only status and two writable opens on copied P2A ledgers with live holds and settled costs."""
import argparse
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
sys.path.insert(0, str(ROOT / "acceptance"))
from gx_core.p0.cli import existing_db
from v21_staging import quiescent_copy, stage_source
from v31.historical_migration import column_names, sql_hash, tables

FIXTURES = ROOT / "acceptance/phase2a"
FROZEN = ROOT / "acceptance/closure/runs/frozen-3a08a9d"
OLD_RELEASE = "3a08a9d1c36759686d29743c75d789cd959848a1"
TURN_FIELDS = ("reservation_ref", "budget_status", "confidence", "token_budget", "settled_tokens")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def status(directory, rid, release=ROOT):
    dbfile = directory / "gx.db"
    before = digest(dbfile.read_bytes())
    proc = subprocess.run([sys.executable, "-B", release / "bin/gx-p0", "status", "--state", directory,
                           "--run-id", rid], cwd=release, check=True, capture_output=True, text=True, timeout=30)
    value = json.loads(proc.stdout)
    assert value["run_id"] == rid
    assert digest(dbfile.read_bytes()) == before, "read-only status changed the historical database"
    return value


def turn_amounts(reply):
    return [{field: row[field] for field in TURN_FIELDS} for row in reply["metering"]["turns"]]


def token_balance(reply):
    risk = reply["metering"]["risk"]
    return {field: risk[field] for field in ("settled_tokens", "reserved_tokens") if field in risk}


def verify(state, expected):
    dbfile = state / "gx.db"
    original_sha = digest(dbfile.read_bytes())
    with closing(sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True)) as db:
        rid, = db.execute("SELECT run_id FROM runs").fetchone()
        columns = {t: column_names(db, t) for t in tables(db) if t != "schema_meta"}
        hashes = {t: sql_hash(db, t, c) for t, c in columns.items()}
        prior_meta = dict(db.execute("SELECT key,value FROM schema_meta"))
        holdings = dict(db.execute("SELECT budget_status, count(*) FROM p0_meter_native_turns GROUP BY budget_status"))
        assert holdings == expected, holdings
        turns = [dict(zip(TURN_FIELDS, row)) for row in db.execute(
            "SELECT " + ",".join(TURN_FIELDS) + " FROM p0_meter_native_turns ORDER BY sequence")]
        timestamps = dict(db.execute(
            "SELECT m.reservation_ref,coalesce(m.terminal_at,b.settled_at,b.created_at) "
            "FROM p0_meter_turns m JOIN budget_reservations b USING(reservation_ref) "
            "WHERE m.confidence IN ('UNRESOLVED','HISTORICAL_SETTLED')"))
    before = status(state, rid)
    assert turn_amounts(before) == turns
    versions = []
    for open_number in (1, 2):
        with closing(existing_db(state, readonly=False)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            for table, old_cols in columns.items():
                assert sql_hash(db, table, old_cols) == hashes[table], f"old rows changed in {table}"
            meta = dict(db.execute("SELECT key,value FROM schema_meta"))
            assert prior_meta.items() <= meta.items(), "prior migration records changed"
            assert db.execute("SELECT count(*) FROM p0_usage_releases").fetchone()[0] == 0
            migrated = dict(db.execute("SELECT reservation_ref,unresolved_at FROM p0_meter_turns "
                                       "WHERE unresolved_at IS NOT NULL"))
            assert migrated == timestamps, (migrated, timestamps)
            versions.append({t: sql_hash(db, t, column_names(db, t)) for t in tables(db)})
        current = status(state, rid)
        assert turn_amounts(current) == turns and token_balance(current) == token_balance(before)
        for key in ("status", "unsettled_turns", "jobs", "tasks"):
            assert current[key] == before[key], f"cold status differs after open {open_number} for {key}"
    assert versions[0] == versions[1], "second writable open changed state"
    return {"source_db_sha256": original_sha, "run_id": rid, "status": before["status"],
            "original_tables_verified": len(columns), "historical_budget_statuses": holdings,
            "original_events": hashes["events"], "original_jobs": hashes["jobs"],
            "original_reservations": hashes["budget_reservations"],
            "original_budgets": hashes["budgets"], "turn_amounts": turns, "token_balance": token_balance(before),
            "backfilled_unresolved_at": timestamps, "operator_releases_after_open": 0,
            "added_migrations": sorted(meta.keys() - prior_meta.keys()),
            "unchanged_second_open": True, "read_only_status_calls": 3, "passed": True}


def phase2a_ledger(source, state, expected):
    state.mkdir()
    if "rollback" in source.parts:
        with tarfile.open(FIXTURES / "rollback/historical-project.tar.gz", "r:gz") as archive:
            archive.extractall(state, members=(e for e in archive if e.name.startswith("artifacts/")), filter="data")
    compressed = source.read_bytes()
    (state / "gx.db").write_bytes(gzip.decompress(compressed))
    return {**verify(state, expected), "source": str(source.relative_to(ROOT)),
            "source_gzip_sha256": digest(compressed)}


def closure_ledger(work, legacy, case, image, expected, confidence, settled_tokens):
    state = work / case
    state.mkdir()
    dbfile = state / "gx.db"
    dbfile.write_bytes(image)
    backup = work / (case + "-backup")
    quiescent_copy(state, backup)
    report = verify(state, expected)
    assert report["token_balance"] == {"settled_tokens": settled_tokens, "reserved_tokens": 100}
    matching = [t["reservation_ref"] for t in report["turn_amounts"] if t["confidence"] == confidence]
    assert len(matching) == 1 and set(matching) == set(report["backfilled_unresolved_at"])

    # Keep the migrated DB intact. Only the saved pre-upgrade snapshot returns to the old CLI.
    state.rename(work / (case + "-upgraded"))
    shutil.copytree(backup, state)
    backup_sha = digest((backup / "gx.db").read_bytes())
    restored = status(state, report["run_id"], legacy)
    assert restored["status"] == report["status"] and turn_amounts(restored) == report["turn_amounts"]
    assert token_balance(restored) == report["token_balance"]
    assert digest(dbfile.read_bytes()) == backup_sha
    with closing(sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True)) as db:
        assert "p0_usage_releases" not in tables(db)
        balances = sql_hash(db, "budgets", column_names(db, "budgets"))
        assert balances == report["original_budgets"]
        assert sql_hash(db, "events", column_names(db, "events")) == report["original_events"]
    return {**report, "restored_backup_sha256": backup_sha, "restored_status": restored["status"],
            "restored_token_balance": token_balance(restored), "restored_budgets": balances}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="gx-closure-ledgers-") as tmp:
        work = Path(tmp)
        results = [
            phase2a_ledger(FIXTURES / "cross-process/restart/after.db.gz", work / "active", {"HELD": 1}),
            phase2a_ledger(FIXTURES / "rollback/gx-b8e4f7baeba668ed.db.gz", work / "paused", {"SETTLED": 3}),
        ]
        legacy = work / "old-release"
        release = stage_source(work, legacy, OLD_RELEASE)
        manifest = set((FROZEN / "raw-files.sha256").read_text().splitlines())
        closure = {}
        cases = [
            ("U05", {"HELD": 1, "SETTLED": 1}, "UNRESOLVED", 130),
            ("B05", {"HELD": 1, "SETTLED": 2}, "HISTORICAL_SETTLED", 120),
        ]
        with tarfile.open(FROZEN / "raw.tar.gz") as archive:
            for case, expected, confidence, settled in cases:
                member = f"usage/evidence/{case}/ledger.db"
                image = archive.extractfile(member).read()
                assert f"{digest(image)}  {member}" in manifest, f"altered frozen ledger {member}"
                closure[case] = {**closure_ledger(work, legacy, case, image, expected, confidence, settled),
                                 "source": str((FROZEN / "raw.tar.gz").relative_to(ROOT)), "member": member}
    args.out.write_text(json.dumps({"passed": True, "evidence": "SCRIPTED", "cases": results,
                                    "rollback_revision": release["git_commit"], "closure_ledgers": closure},
                                   ensure_ascii=False, indent=2) + "\n")
    print("Verified four migrated ledgers and restored both closure backups under the original release")


if __name__ == "__main__":
    main()
