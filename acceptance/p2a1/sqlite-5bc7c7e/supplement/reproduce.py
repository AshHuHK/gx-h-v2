"""Supplement frozen transaction measurements for Reactor and the inherited optional input-copy path."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

repo = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
sys.path[:0] = [str(repo), str(repo / "gx-h"), str(repo / "gx-h/tests")]
from acceptance.closure.profile import test_environment
from acceptance.p2a1.sqlite_boundaries import Observer
from gx_core import api
from gx_core.artifacts import ArtifactService
from gx_core.context import Principal
from gx_core.domain import ids
from gx_core.persistence import store

out.mkdir(parents=True)
observer = Observer()
with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    env = test_environment(work)
    trace = work / "metrics.jsonl"
    env["GX_METRICS_FILE"] = str(trace)
    src = work / "fixture.dat"
    src.write_bytes(b"x" * 1_048_576)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    original = Path.read_bytes
    def read(path):
        if path == src:
            observer.observe("selected input .read_bytes")
        return original(path)

    with patch.dict(os.environ, env, clear=True), observer.track_transactions(), patch.object(Path, "read_bytes", read):
        with closing(store.open_store(work / "gx.db")) as conn:
            with observer.sample(ArtifactService, "publish_bytes"):
                receipt = api.dispatch("gx_run_start", {
                    "objective": "Measure the legacy input copying path", "project_ref": "copy-check",
                    "budget_profile": "cpu_only", "workflow": {"id": "module_collaboration", "version": "1"},
                    "input_files": [{"path": str(src), "sha256": digest}],
                    "inputs": {"modules": [{"key": "counts", "question": "How many synthetic rows?"}]},
                    "idempotency_key": "copied-input",
                }, principal=Principal(subject="fixture", scope="operator"), conn=conn)
            assert receipt["ok"], receipt.get("error")
        test = "test_phase2a_resume_policy.BranchResumePolicy.test_two_reactors_observing_same_ready_stage_consume_it_once"
        suite = unittest.defaultTestLoader.loadTestsFromName(test)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        assert result.wasSuccessful()
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    measured = [r for r in rows if r.get("transaction_owner") in ("Reactor", "Run")]
    (out / "transactions.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in measured))
    held = [{"call": name, "owner": owner, "operation": op, "count": n}
            for (name, owner, op), n in observer.inside.items()]
    assert any(r["call"] == "selected input .read_bytes" and r["owner"] == "Run" and r["operation"] == "run_start" for r in held)
    reactors = [r for r in measured if r["transaction_owner"] == "Reactor" and r["operation"] == "drive_science"]
    assert len(reactors) == 2 and len({r["run_id"] for r in reactors}) == 1
    facts = {"input_bytes": src.stat().st_size, "input_digest": digest, "observed_inside_transaction": held,
             "fixture_result": "PASS", "reactor_test": test, "reactor_transactions": len(reactors),
             "reactor_rows_changed": [r["rows_changed"] for r in reactors]}
    (out / "result.json").write_text(json.dumps(facts, indent=2) + "\n")
