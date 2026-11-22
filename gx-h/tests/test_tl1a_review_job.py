"""Cold reviewer handoff through the public dynamic-tool callback and real detached CPU owner."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, compute, manifest
from gx_core.p0.driver import context
from gx_core.p0.roots import Roots
from gx_core.persistence import store
from tests.test_tl1a_geometry import OfflineIngress, product_for, submit_peer


def wire(roots, tid, cid, args, *, name="gx_recalculate_geometry", samples=None):
    request = {"threadId": tid, "turnId": "turn", "callId": cid, "tool": name, "arguments": args}
    unexpected = []
    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name in {"_request", "_send", "independent_check", "numerical_reference", "_admit", "_start"}:
            unexpected.append(frame.f_code.co_name)
    old = sys.getprofile()
    start = time.monotonic()
    try:
        sys.setprofile(profile)
        reply = roots._callback(SimpleNamespace(message={"method": "item/tool/call", "params": request}))
    finally:
        sys.setprofile(old)
    elapsed = round((time.monotonic() - start) * 1000, 3)
    if samples is not None:
        samples.append({"tool": name, "call": cid, "callback_ms": elapsed, "nested_rpc_or_compute": unexpected})
    if unexpected or not reply["success"]:
        raise AssertionError((unexpected, reply))
    return json.loads(reply["contentItems"][0]["text"])


def reviewer(conn, directory, run, task_id):
    tools = service.hooks_for_run(conn, run).build_worker_tools(conn, run)
    roots = Roots(conn, run_id=run, cwd=directory, worker_tools=tools.specs, worker_call=tools.call)
    row = roots._claim(task_id)
    tid = row["thread_id"] or "test-root-" + task_id
    if not row["thread_id"]:
        conn.execute("UPDATE p0_roots SET state='ready',thread_id=? WHERE root_key=?", (tid, row["root_key"]))
        conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (tid, row["lease_id"]))
    roots.active_turns[tid] = "turn"
    return roots, tid


class ReviewJob(unittest.TestCase):
    def test_scientific_disagreement_and_unavailable_fit(self):
        from adapters.scientific.tl1a_geometry.worker import measure
        folder = config.HARNESS_ROOT / "adapters/scientific/tl1a_geometry"
        data = folder / "data"
        structure = (data / "2RE9.cif").read_bytes()
        criteria = json.loads((data / "criteria.json").read_bytes())
        observation = measure(structure, criteria, "common_ca")
        observation["pairs"][0]["rotation_angle_deg"] += 7.0
        encoded = json.dumps(observation)
        batch = {"evidence_ref": "fault-injection-original-observation", "routes": {"common_ca": {
            "measurement_json": encoded, "measurement_sha256": hashlib.sha256(encoded.encode()).hexdigest()}}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "observations.json").write_text(json.dumps(batch))
            def reproduce(frozen):
                (out / "criteria.json").write_text(json.dumps(frozen))
                subprocess.run([sys.executable, "-I", "-B", str(folder / "review_batch.py"), "--method", str(folder / "review.py"),
                    "--structure", str(data / "2RE9.cif"), "--criteria", str(out / "criteria.json"),
                    "--observations", str(out / "observations.json"), "--out", str(out)], check=True)
                return json.loads((out / "checks.json").read_bytes())["results"]["common_ca"]
            altered = reproduce(criteria)
            self.assertEqual(altered["status"], "disagrees")
            self.assertFalse(altered["checks"]["numerical_tolerances"])
            unavailable = reproduce({**criteria, "source_sha256": "0" * 64})
            self.assertEqual(unavailable["status"], "unavailable")
            self.assertNotIn("differences", unavailable)
            self.assertIn("source differs", unavailable["error"])

    def test_queue_wait_cold_resume_and_formal_results(self):
        self.exercise_study(6, [2, 3], record=True)

    def test_four_job_cap_funds_one_review_then_stops(self):
        self.exercise_study(4, [2])

    def exercise_study(self, cap, protocol_counts, *, record=False):
        samples, checks = [], []
        path = config.HARNESS_ROOT.parent / "examples/tl1a_geometry_v3/project.json"
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state, patch.object(cli, "Driver", OfflineIngress):
            with closing(store.open_store(state.db_path)) as conn:
                spec = json.loads(path.read_text())
                spec["max_jobs"] = cap
                run = cli.start(conn, Path(tmp), spec)["run_id"]
            for _ in range(16):
                with closing(store.open_store(state.db_path)) as conn:
                    compute.drain(conn, run)
                    for (job_id,) in conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)).fetchall():
                        JobService(conn).wait(job_id, 30)
                    reactor.lazy(context(conn), run)
                    reactor.drive_science(context(conn), run)
                    snapshot = cli.snapshot(conn)
                    if snapshot["status"] == "COMPLETED":
                        break
                    tasks = conn.execute("SELECT * FROM tasks WHERE run_id=? AND status IN ('READY','CLAIMED','RUNNING') "
                                         "ORDER BY rowid", (run,)).fetchall()
                    for task in tasks:
                        inputs = store.loads(task, "input_json")
                        if inputs["output_kind"] != "geometry_assessment":
                            submit_peer(conn, tmp, run, task)
                            continue
                        roots, tid = reviewer(conn, tmp, run, task["task_id"])
                        inputs = store.loads(conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone(), "input_json")
                        assets = ArtifactService(conn)
                        frozen = assets.read_json(inputs["review_inputs_ref"])
                        protocols = list(frozen["routes"])
                        arg = {"protocol": protocols[0]}
                        first = wire(roots, tid, "request", arg, samples=samples)
                        jid = first["job_id"]
                        if not conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (jid,)).fetchone():
                            self.assertEqual(first["status"], "PROPOSED")
                            self.assertEqual(wire(roots, tid, "request", arg), first)
                            repeated = wire(roots, tid, "request-again", {"protocol": protocols[-1]}, samples=samples)
                            self.assertEqual(repeated["job_id"], jid)
                            parked = wire(roots, tid, "park", {"job_id": jid}, name="gx_wait_job", samples=samples)
                            self.assertTrue(parked["waiting"])
                            self.assertEqual(conn.execute("SELECT COUNT(*) FROM p0_job_proposals WHERE job_id=?", (jid,)).fetchone()[0], 1)
                            continue  # Disconnect before admission. The next connection resumes the original durable request.
                        proof = manifest.read_manifest(conn, jid)
                        self.assertEqual(proof["admission"]["owner"]["thread_id"], tid)
                        self.assertEqual(proof["admission"]["owner"]["lease_id"], task["lease_id"])
                        self.assertEqual(proof["admission"]["task_id"], task["task_id"])
                        self.assertEqual(proof["completion"]["status"], "SUCCEEDED")
                        output, = proof["completion"]["outputs"]
                        self.assertEqual(output["runner_sha256"], output["sha256"])
                        result = assets.read_json(output["artifact_ref"])
                        for protocol in protocols:
                            raw = frozen["routes"][protocol]
                            self.assertEqual(raw["measurement_json"].encode(), assets.read_bytes(raw["measurement_ref"]))
                            out = wire(roots, tid, "collect-" + protocol, {"protocol": protocol}, samples=samples)
                            self.assertEqual(out["status"], "matches")
                            self.assertEqual(out["job_id"], jid)
                            self.assertEqual(out["manifest_hash"], proof["manifest_hash"])
                            self.assertEqual(out["request_call_id"], "request")
                            self.assertEqual(out["observation_job_id"], raw["observation_job_id"])
                            self.assertEqual(out["job_output_ref"], output["artifact_ref"])
                            self.assertEqual(out["differences"], result["results"][protocol]["differences"])
                            self.assertTrue(all(out["checks"].values()))
                            self.assertTrue(all(p["within_reproduction_tolerance"] for p in out["differences"]))
                            previous = wire(roots, tid, "collect-again-" + protocol, {"protocol": protocol})
                            self.assertEqual(out["check_ref"], previous["check_ref"])
                        checks.append({"job": jid, "reviewer": tid, "protocols": protocols, "manifest_hash": proof["manifest_hash"],
                            "artifact_ref": output["artifact_ref"], "sha256": output["sha256"], "scheduler_ref": proof["completion"]["backend_job_id"],
                            "runner": {k: proof["completion"]["runner"].get(k) for k in ("runner_pid", "tool_pid", "started_at", "finished_at")},
                            "max_angular_error_deg": max(pair["absolute_difference"]["rotation_angle_deg"] for v in result["results"].values() for pair in v["differences"]),
                            "max_rmsd_error_angstrom": max(pair["absolute_difference"]["fitted_rmsd_angstrom"] for v in result["results"].values() for pair in v["differences"])})
                        body = product_for(roots, task, inputs, tid)
                        wire(roots, tid, "submit", {"product": body}, name="gx_submit_product")
            self.assertEqual(snapshot["status"], "COMPLETED")
            self.assertEqual(len(snapshot["jobs"]), cap)
            self.assertEqual([len(c["protocols"]) for c in checks], protocol_counts)
            self.assertEqual(len(snapshot["rounds"]), len(protocol_counts))
            with closing(store.open_store(state.db_path)) as conn:
                before = cli.snapshot(conn)
                compute.drain(conn, run)
                reactor.lazy(context(conn), run)
                reactor.drive_science(context(conn), run)
                after = cli.snapshot(conn)
                self.assertEqual((before["jobs"], before["report_ref"]), (after["jobs"], after["report_ref"]))
                assessed = [ArtifactService(conn).read_json(r["assessment_ref"])["status"] for r in after["rounds"]]
                self.assertEqual(assessed, ["stable", "insufficient"][:len(protocol_counts)])
                if not record and (evidence := os.environ.get("GX_REVIEW_JOB_EVIDENCE")):
                    Path(evidence).with_name("four-job-cap.json").write_text(json.dumps({"formal_job_limit": cap,
                        "run": run, "jobs_charged": len(after["jobs"]), "rounds": len(protocol_counts),
                        "assessments": assessed, "status": after["status"], "report_ref": after["report_ref"]}, indent=2) + "\n")
                if record and (evidence := os.environ.get("GX_REVIEW_JOB_EVIDENCE")):
                    Path(evidence).write_text(json.dumps({"mode": "offline scripted tasks, production Root tool callback and detached local CPU Jobs",
                        "workflow": "tl1a_geometry@3", "workflow_sha256": conn.execute("SELECT workflow_sha256 FROM runs WHERE run_id=?", (run,)).fetchone()[0],
                        "hooks_sha256": service.hooks_for_run(conn, run).digest(),
                        "run": run, "status": after["status"], "report_ref": after["report_ref"], "jobs": [j["job_id"] for j in after["jobs"]],
                        "assessments": assessed, "cold_checks": checks, "callback_samples": samples, "external_model_calls": 0}, indent=2) + "\n")
