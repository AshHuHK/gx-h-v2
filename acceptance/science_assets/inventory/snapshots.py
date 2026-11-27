"""Read the indexed native SQLite snapshots without opening any legacy runtime."""
import argparse
import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def inspect_snapshots(table, assets):
    with table.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    report = {"sqlite":[], "query": {"rhea_id":32676, "taxon_id":559292}}
    for number, row in enumerate(rows, 2):
        if row["collection"] != "原生 v2 酶索引" or not row["id"].endswith(".sqlite"):
            continue
        path = assets / row["source_path"]
        entry = {"inventory_line":number, "bytes":path.stat().st_size}
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            entry["tables_observed"] = sorted(tables)
            if {"association","protein","direction"}.issubset(tables):
                master, query_direction = db.execute("SELECT master,direction FROM direction WHERE rhea_id=?",
                                                    (32676,)).fetchone()
                matches = db.execute("""
                    SELECT DISTINCT a.accession, a.direction FROM association a
                    JOIN protein p ON p.accession=a.accession
                    WHERE p.taxid=? AND a.master=? ORDER BY a.accession,a.direction
                """, (559292,master)).fetchall()
                entry["release_fields"] = dict(db.execute("SELECT key,value FROM meta WHERE key IN "
                                        "('rhea_release','swissprot_release','enzyme_release','schema_version')"))
                entry["yeast_annotation"] = {"rhea_master":master, "directional_record":query_direction,
                                             "distinct_records":[{"accession":a, "record_direction":direction}
                                                                 for a,direction in matches]}
            if {"assays","snapshot"}.issubset(tables):
                entry["assays_by_source"] = dict(db.execute("SELECT source,count(*) FROM assays GROUP BY source"))
        report["sqlite"].append(entry)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--table-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect_snapshots(args.table_root/"data.tsv", args.asset_root),ensure_ascii=False,indent=2))
