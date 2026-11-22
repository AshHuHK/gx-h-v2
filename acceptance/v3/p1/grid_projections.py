"""Compare composed research record readers with the pinned v2.1 scientific readers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from contextlib import ExitStack, closing

from gx_core.p0 import science_inputs
from gx_core.research import records
from test_p0_review_sources import SourceDuringReview


ROOT = Path(__file__).resolve().parents[3]
READS = ("source_status", "review_history", "task_context")
CASES = (
    "test_cold_restart_between_source_commit_and_processing",
    "test_source_commits_after_old_review_and_while_decision_is_running",
    "test_source_between_completed_report_checkpoint_and_run_close",
)


def compare_read(name, original, current, counts):
    def compare(*args, **kwargs):
        expected = original(*args, **kwargs)
        observed = current(*args, **kwargs)
        if observed != expected:
            raise AssertionError(f"{name} changed its stored scientific projection")
        counts[name] += 1
        return observed
    return compare


def source_races():
    counts = {name: 0 for name in READS}
    with ExitStack() as stack:
        for name in READS:
            paired = compare_read(name, getattr(science_inputs, name), getattr(records, name), counts)
            stack.enter_context(patch.object(science_inputs, name, paired))
        suite = unittest.TestSuite(SourceDuringReview(name) for name in CASES)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.skipped or result.testsRun != len(CASES) or not all(counts.values()):
        raise AssertionError("late-source cases or their paired reads did not complete")
    return {"tests": list(CASES), "compared_calls": counts}


def historical_reads(workspace):
    path = (workspace / "gx.db").resolve(strict=True)
    if not (path.parent / "artifacts").is_dir():
        raise ValueError("provide a copied GX workspace including its artifact store")
    counts = {name: 0 for name in READS}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        runs = db.execute("SELECT run_id,workflow_sha256 FROM runs WHERE workflow_id='science_grid' "
                          "AND workflow_version='2' ORDER BY run_id").fetchall()
        if not runs:
            raise ValueError("the historical workspace has no science_grid@2 Run")
        for run in runs:
            run_id = run["run_id"]
            for name in ("source_status", "review_history"):
                compare_read(name, getattr(science_inputs, name), getattr(records, name), counts)(db, run_id)
            compare = compare_read("task_context", science_inputs.task_context, records.task_context, counts)
            for task_id, in db.execute("SELECT task_id FROM tasks WHERE run_id=? ORDER BY task_id", (run_id,)):
                compare(db, run_id, task_id)
    return {"workspace": str(workspace), "database_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "runs": [dict(run) for run in runs], "compared_calls": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", action="append", type=Path, default=[], help="quiescent v2.1 workspace copy")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sources = [Path(records.__file__), Path(science_inputs.__file__), Path(__file__),
               ROOT / "gx-h/tests/test_p0_review_sources.py"]
    receipt = {
        "scope": "Local CPU grid, late human sources, accepted worker products and historical read compatibility",
        "command": shlex.join(["env", "PYTHONDONTWRITEBYTECODE=" + os.environ.get("PYTHONDONTWRITEBYTECODE", ""),
                               "PYTHONPATH=" + os.environ.get("PYTHONPATH", ""), sys.executable, *sys.argv]),
        "release_head_at_execution": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "tested_source_sha256": {str(p.resolve().relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sources},
        "source_races": source_races(),
        "historical": [historical_reads(workspace) for workspace in args.workspace],
        "exit_code": 0,
    }
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
