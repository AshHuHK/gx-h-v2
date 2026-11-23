"""Compare every historical table in a byte-for-byte copy before and after two writable GX opens."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing

ROOT = Path(__file__).resolve().parents[2]


def encode(value):
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    raise TypeError(type(value).__name__)


def column_names(db, table):
    return [r[1] for r in db.execute('PRAGMA table_info("' + table + '")')]


def tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def sql_hash(db, table, columns):
    query = 'SELECT ' + ','.join('"' + c + '"' for c in columns) + ' FROM "' + table + '"'
    rows = sorted(json.dumps(tuple(r), separators=(",", ":"), ensure_ascii=False, default=encode) for r in db.execute(query))
    return {"rows": len(rows), "sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}


def main(args):
    from gx_core.p0.cli import existing_db
    from gx_core.orchestration.service import default_registry
    source = args.source.resolve()
    baseline = json.loads(args.baseline.read_bytes())["checkpoint"]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert digest == baseline["db_sha256"], "historical DB differs from retained original receipt"
    assert not Path(str(source) + "-wal").exists() or not Path(str(source) + "-wal").stat().st_size
    with tempfile.TemporaryDirectory() as temp:
        private = Path(temp)
        target = private / "gx.db"
        shutil.copyfile(source, target)
        with closing(sqlite3.connect(target)) as db:
            original_columns = {t: column_names(db, t) for t in tables(db)}
            old = {t: sql_hash(db, t, cols) for t, cols in original_columns.items() if t != "schema_meta"}
            old_meta = dict(db.execute("SELECT key,value FROM schema_meta"))
        for t in ("science_settlements", "science_decisions", "p0_root_calls", "submissions", "evidence", "budget_reservations"):
            assert old[t]["rows"] > 0, t + " needs populated source rows"
        checks = []
        for i in (1, 2):
            with closing(existing_db(private, readonly=False)) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                foreign = [tuple(r) for r in db.execute("PRAGMA foreign_key_check")]
                assert integrity == "ok" and not foreign
                assert all(sql_hash(db,t,cols) == old[t] for t,cols in original_columns.items() if t != "schema_meta")
                meta = dict(db.execute("SELECT key,value FROM schema_meta"))
                assert old_meta.items() <= meta.items()
                run, = db.execute("SELECT run_id,workflow_version,workflow_sha256 FROM runs")
                selected = default_registry(db, workflow_id="science_grid", version="2").get("science_grid", "2").digest()
                assert run["workflow_version"] == "2" and selected == run["workflow_sha256"] == baseline["run"]["workflow_sha256"]
                full = {t: sql_hash(db,t,column_names(db,t)) for t in tables(db)}
                if i == 1:
                    first = full
                else:
                    assert full == first, "second open rewrote state"
                extras = [c for c in column_names(db,"science_settlements") if c not in original_columns["science_settlements"]]
                for c in extras:
                    assert db.execute('SELECT count(*) FROM science_settlements WHERE "'+c+'" IS NOT NULL').fetchone()[0] == 0
                checks.append({"writable_open": i, "integrity": integrity, "foreign_key_violations": foreign,
                               "unchanged_original_table_count": len(old), "original_meta_preserved": True})
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        return {"passed": True, "source": str(source), "source_db_sha256": digest, "source_run": dict(run),
                "source_build": json.loads(args.baseline.read_bytes())["source_install"]["git_commit"],
                "candidate_release": str(args.install.resolve()), "candidate_workflow_digest": selected,
                "original_columns_on_migrated_tables": {t: original_columns[t] for t in ("science_settlements","science_decisions")},
                "exact_original_row_hashes": old, "new_p0_migrations": sorted(set(meta)-set(old_meta)),
                "added_settlement_columns_null_on_old_rows": extras, "writable_opens": checks,
                "second_open_exact_including_new_tables": True}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--install", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    opts = p.parse_args()
    sys.path.insert(0,str(opts.install.resolve()/"gx-h"))
    result = {"passed": False}
    try:
        result = main(opts)
    except Exception as err:
        result["error"] = repr(err)
        raise
    finally:
        opts.out.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
