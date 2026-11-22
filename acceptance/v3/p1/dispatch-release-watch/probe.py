"""Measure a stable slot watch, with the SQLite query plan of its actual owner."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from unittest.mock import patch


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_watch(conn, error, n):
    watch = error.slot_watch
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert not watch.opened(conn)
    finally:
        conn.set_trace_callback(None)
    assert len(statements) == 1
    sql = statements[0]
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    # json_each enumerates only the observed occupants. Every durable relation
    # is probed by primary key or index, including the correlated wait/turn rows.
    assert not any(line.startswith("SCAN ") and "json_each" not in line for line in plan), plan
    assert any("root_key=?" in line or "INTEGER PRIMARY KEY (rowid=?)" in line for line in plan), plan
    before = conn.total_changes
    samples = []
    for _ in range(n):
        start = time.perf_counter_ns()
        opened = watch.opened(conn)
        end = time.perf_counter_ns()
        assert not opened
        samples.append(end - start)
    assert conn.total_changes == before
    durations = sorted(samples)
    return {"reason": error.details, "watched_occupants": len(watch.occupants), "plan": plan,
            "sql": sql, "n": n, "wall_us": {"p50": statistics.median(durations)/1e3,
            "p95": durations[math.ceil(.95*n)-1]/1e3, "max": max(durations)/1e3},
            "writes": conn.total_changes-before, "raw_wall_ns": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--history", type=int, default=128)
    args = parser.parse_args()
    src = args.source.resolve()
    sys.path.insert(0, str(src / "gx-h"))
    from gx_core.errors import HoldActive
    from gx_core.p0 import activity
    from tests import test_v21_teams as fixture
    from tests.test_v3_dispatch import CapacityWake

    candidate = CapacityWake()
    candidate.setUp()
    f = candidate.f
    data = {"source_head": subprocess.check_output(["git","-C",str(src),"rev-parse","HEAD"], text=True).strip(),
            "source_sha256": {str(p): sha(src/p) for p in map(Path, ("gx-h/gx_core/p0/team_admission.py",
                "gx-h/gx_core/p0/activity.py", "gx-h/gx_core/p0/driver.py", "gx-h/tests/test_v3_dispatch.py"))},
            "script_sha256": sha(Path(__file__)), "command": [sys.executable,*sys.argv],
            "utc": datetime.now(timezone.utc).isoformat(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "released_historical_roots": args.history, "watches": {}}
    with ExitStack() as stack:
        stack.callback(candidate.doCleanups)
        rid, _ = f.run_case("study", (fixture.group("reading", ["evidence_reviewer"]),), maximum=3)
        owner = f.roots(rid)
        prior = f.create(rid)["task_id"]
        for _ in range(args.history):
            owner._claim(prior)
            f.yield_task(prior)
        target = f.create(rid)["task_id"]
        holder, finish = candidate.paid_turn(rid, prior)
        def denial():
            try:
                owner._claim(target)
            except HoldActive as exc:
                return exc
            raise AssertionError("expected capacity holding the target")
        error = denial()
        assert error.details["scope"] == "team"
        data["watches"]["team"] = inspect_watch(f.db, error, args.samples)
        model_policy = {"parallel_turns":{"openai":1}}
        with patch.object(activity, "_policy", return_value=model_policy):
            error = denial()
            assert error.details["reason"] == "provider_backoff"
            data["watches"]["provider"] = inspect_watch(f.db, error, args.samples)
        f.yield_task(prior)
        seen = denial()
        assert not seen.slot_watch.opened(f.db)
        finish()
        assert seen.slot_watch.opened(f.db)
        assert owner._claim(target)["attempt"] == 1
        data["final_counts"] = {t:f.db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                                for t in ("tasks", "leases", "p0_roots", "p0_root_deliveries", "p0_root_turn_usage")}
        data["turn_fixture"] = "A locally scripted delivery receipt runs the production turn and lease owners."
    args.out.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    print(json.dumps({w:v["wall_us"] for w,v in data["watches"].items()}))


if __name__ == "__main__":
    main()
