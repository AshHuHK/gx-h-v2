#!/usr/bin/env python3
"""Capture and export a Goal decision window for independent review."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "gx-h"))
import verify


def rows(conn, sql, run):
    return [dict(r) for r in conn.execute(sql, (run,))]


def tables(conn, run):
    """Limit private source exports to this run, including indirect job and turn records."""
    owned = ("runs", "v6_goals", "v6_decision_tasks", "v6_graph_versions", "v6_decisions",
             "v6_branches", "v6_nodes", "v6_job_attempts", "hypotheses", "research_contexts", "p0_roots", "p0_root_deliveries",
             "jobs", "artifacts", "events", "p0_meter_native_turns", "coordinator_leases")
    source = {t: rows(conn, f"SELECT * FROM {t} WHERE run_id=?", run) for t in owned}
    joins = {
        "p0_root_calls": "thread_id IN (SELECT thread_id FROM p0_roots WHERE run_id=?)",
        "p0_v6_native_calls": "thread_id IN (SELECT thread_id FROM p0_roots WHERE run_id=?)",
        "p0_execution_manifests": "job_id IN (SELECT job_id FROM jobs WHERE run_id=?)",
        "budgets": "run_id=?",
        "budget_reservations": "budget_ref IN (SELECT budget_ref FROM budgets WHERE run_id=?)",
        "hypothesis_revisions": "hypothesis_id IN (SELECT hypothesis_id FROM hypotheses WHERE run_id=?)",
    }
    for table, predicate in joins.items():
        source[table] = rows(conn, f"SELECT * FROM {table} WHERE {predicate}", run)
    verify.need(len(source["v6_goals"]) == 1, "capture requires an existing Goal-only run")
    return source


def snapshot(db, run):
    with closing(sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True,
                                 isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        source = tables(conn, run)
        from gx_core.p0.v6_costs import resource_report
        costs = resource_report(conn, run)
        conn.rollback()
    stat = db.stat()
    return {"schema": "gx.v6.observer/1", "run_id": run,
            "captured_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
            "kernel_boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "observer_pid": os.getpid(), "db_file_id": [stat.st_dev, stat.st_ino],
            "rows": source, "costs": costs}


def checked_artifact(root, row):
    ns = (root / row["namespace"]).resolve()
    verify.need(root in ns.parents, "artifact namespace escaped state")
    path = (ns / row["rel_path"]).resolve()
    verify.need(ns in path.parents, "artifact path escaped namespace")
    data = path.read_bytes()
    verify.need(len(data) == row["bytes"] and verify.sha(data) == row["sha256"],
                "GX artifact bytes differ: " + str(row["artifact_ref"]))
    return data


def publish_dir(dest, build):
    dest = dest.absolute()
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if dest.exists():
        raise ValueError(f"choose a new output directory: {dest}")
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".gx-export-") as temp:
        work = Path(temp) / "output"
        work.mkdir(mode=0o700)
        result = build(work)
        os.rename(work, dest)
    return result


def capture(db, run, out, artifacts=None):
    source = snapshot(db, run)
    raw_root = (artifacts or (db.parent / "artifacts")).resolve()
    def build(work):
        files = work / "artifacts"
        files.mkdir()
        for row in source["rows"]["artifacts"]:
            data = checked_artifact(raw_root, row)
            target = files / row["sha256"]
            if not target.exists():
                target.write_bytes(data)
        snap = verify.stable(source)
        (work / "capture.json").write_bytes(snap)
        return {"run_id": run, "sha256": verify.sha(snap), "jobs": len(source["rows"]["jobs"]),
                "decisions": len(source["rows"]["v6_decisions"]), "captured_ns": source["captured_ns"]}
    return publish_dir(out, build)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="make a private, read-only run-scoped snapshot")
    c.add_argument("--db", type=Path, required=True)
    c.add_argument("--run", required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--artifacts-root", type=Path)
    a = sub.add_parser("bundle", help="project one observed window or three matched windows")
    a.add_argument("--mode", choices=("observed", "controls"), required=True)
    a.add_argument("--window", type=Path, action="append", required=True)
    a.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "capture":
            result = capture(args.db, args.run, args.out, args.artifacts_root)
        else:
            from project_window import make_bundle
            result = publish_dir(args.out, lambda dest: make_bundle(dest, args.mode, args.window))
        print(json.dumps(result, ensure_ascii=False))
        if result.get("consistent") is False:
            return 2
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
