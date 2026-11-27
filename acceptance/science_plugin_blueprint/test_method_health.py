"""Recover a technical failure from an actual offline CPU Job through the native catalogue callback."""
from contextlib import nullcontext
import base64
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import GOAL, PRODUCT, draft
from acceptance.v621.test_evidence_gate import node
from acceptance.v622.test_close import SurveyedStudy
from gx_core import config
from gx_core.domain import clock
from gx_core.p0 import coordinator, method_health
from gx_core.plugins import goal
from tests.test_v6_decisions import branch

CAP = "scholarly-search@1"
QUERY = "instrument uncertainty"
UNKNOWN = {"status": "unobserved"}


class MethodHealth(unittest.TestCase):
    def test_catalogue_observes_fault_after_settlement_and_forgets_changed_or_stale_identity(self):
        work = os.getenv("GX_METHOD_HEALTH_WORK")
        if work:
            Path(work).mkdir(mode=0o700, parents=True, exist_ok=False)
        with nullcontext(work) if work else tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve()
            data = runpy.run_path(str(PRODUCT / "gx-h/plugins/installed/org.gx.scholarly-sources/1.0.0/methods/scholarly.py"))
            # The provider URL is valid. This deliberately damaged archived response fails within
            # the existing search worker, after admission. It has no live network dependency.
            calls = [{"provider": provider, "url": url, "raw_b64": "e30=", "sha256": "0" * 64}
                     for provider, url in data["search_urls"](QUERY).items()]
            material = directory / "damaged.json"
            material.write_text(json.dumps({"schema": data["SNAPSHOT"], "query": QUERY, "calls": calls}))
            zero_raw = json.dumps({"hitCount": 0, "resultList": {"result": []}}).encode()
            calls[0].update(raw_b64=base64.b64encode(zero_raw).decode(), sha256=hashlib.sha256(zero_raw).hexdigest())
            calls[1] = {"url": calls[1]["url"], "provider": "crossref", "error": "recorded provider unavailable"}
            valid = directory / "partial-coverage.json"
            valid.write_text(json.dumps({"schema": data["SNAPSHOT"], "query": QUERY, "calls": calls}))
            config_options = {"capabilities": [CAP], "capability_collections": [], "plugins": ["org.gx.scholarly-sources@1.0.0"],
                              "research_effort": "deep", "research_modules": [], "evidence_policy": "gx.evidence/6.2.4",
                              "run_budget": {"job_submissions": 4, "worker_tasks": 8, "cpu_seconds": 300,
                                             "api_calls": 8, "tokens": 8000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1},
                              "input_files": [{"role": "damaged", "path": str(material)}, {"role": "partial", "path": str(valid)}]}
            with SurveyedStudy(directory / "run", spec_updates=config_options) as s:
                def exact(actor, key):
                    answer = s.call(actor, "gx_query_catalog", {"kind": "method", "reference": CAP}, key)
                    return answer["execution_health"]

                self.assertEqual(exact(s.lead, "first-health"), UNKNOWN)
                b = branch("study", jobs=2)
                b["budget_limits"]["cpu_seconds"] = 200
                operation = dict(node("search", s.materials[0]), capability=CAP, parameters={"query": QUERY})
                proposal = draft(b, operation, watches=[{"event": "job_settled", "subject_ref": "local:search"}])
                submitted = s.call(s.lead, "gx_decide_revision", {"decision": proposal}, "offline-search")
                node_id = submitted["aliases"]["local:search"]
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    s.advance()
                    status, job_id = s.conn.execute("SELECT status,job_id FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
                    if status == "FAILED":
                        break
                    time.sleep(.035)
                self.assertEqual(status, "FAILED")
                lead = s.next_actor("run")
                seen = exact(lead, "read-health")
                self.assertEqual((seen["job_id"], seen["status"], seen["observed"], seen["input_exclusion"]),
                                 (job_id, "FAILED", "nonzero_exit", False))
                shortlist = s.call(lead, "gx_query_catalog", {"kind": "method", "query": CAP}, "short-health")
                self.assertEqual(next(m["execution_health"] for m in shortlist["items"] if m["reference"] == CAP), seen)
                methods = list(goal.catalog(s.conn, s.run_id))
                other = dict(methods[0], manifest_sha256="f" * 64)
                self.assertEqual(method_health.recent(s.conn, s.run_id, [other]), {})
                other = dict(methods[0], plugin={**methods[0]["plugin"], "sha256": "f" * 64})
                self.assertEqual(method_health.recent(s.conn, s.run_id, [other]), {})
                second = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "start",
                    "--state", str(s.state), "--run-key", "method-health-peer", "--goal",
                    GOAL, "--goal-config",
                    str(s.directory / "grant.json")], capture_output=True, text=True,
                    env={**os.environ, "GX_CODEX_OFFLINE": "1"}, timeout=30)
                self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
                coordinator.drain_starts(s.conn, s.state)
                peer, = s.conn.execute("SELECT run_id FROM p0_workspace_runs WHERE run_key='method-health-peer'").fetchone()
                inherited = method_health.recent(s.conn, peer, goal.catalog(s.conn, peer))[CAP]
                self.assertEqual(inherited["scope"], "same_deployment")
                self.assertNotIn("job_id", inherited)
                env = method_health.environments.get(s.conn, seen["environment"])
                changed = {**env, "spec": {**env["spec"], "lockfile_ref": "changed-environment"}}
                with patch.object(method_health.environments, "get", return_value=changed):
                    self.assertEqual(method_health.recent(s.conn, s.run_id, methods), {})
                with patch.object(config, "HARNESS_ROOT", directory):
                    self.assertEqual(method_health.recent(s.conn, s.run_id, methods), {})
                with patch.object(method_health.os, "uname", return_value=type("Host", (), {"nodename": "other"})()):
                    self.assertEqual(method_health.recent(s.conn, s.run_id, methods), {})
                with patch.object(clock, "_offset_seconds", method_health.WINDOW_SECONDS + 1):
                    self.assertEqual(method_health.recent(s.conn, s.run_id, methods), {})
                operation = dict(node("valid", s.materials[1], target=submitted["aliases"]["local:study"]),
                                 capability=CAP, parameters={"query": QUERY})
                proposal = draft(operation, watches=[{"event": "job_settled", "subject_ref": "local:valid"}])
                resubmitted = s.call(lead, "gx_decide_revision", {"decision": proposal}, "different-material")
                result = s.settle(resubmitted["aliases"]["local:valid"])
                final_lead = s.next_actor("run")
                success = exact(final_lead, "new-result")
                self.assertEqual((success["status"], success["job_id"]), ("SUCCEEDED", result["job_id"]))
                artifact = s.call(final_lead, "gx_read_artifact", {"ref": result["result_ref"]}, "read-source-coverage")
                coverage = {p["provider"]: p["status"] for p in artifact["sources"]}
                self.assertEqual(coverage, {"europepmc": "empty", "crossref": "unavailable"})
                if target := os.getenv("GX_METHOD_HEALTH_TRACE"):
                    Path(target).write_text(json.dumps({"run_id": s.run_id, "node_id": node_id,
                        "input": s.materials[0], "decision": submitted["decision_ref"], "job_id": job_id,
                        "before": UNKNOWN, "after": seen, "peer_run_id": peer, "peer_health": inherited,
                        "retained_state": str(s.state) if work else None, "changed_input": s.materials[1],
                        "latest_job": success, "latest_result_ref": result["result_ref"], "source_coverage": coverage, "changed_release_host_provider_and_expired": UNKNOWN,
                        "transport": "normal GX start/native tool callback, scripted Lead, actual offline CPU Job"},
                        indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
