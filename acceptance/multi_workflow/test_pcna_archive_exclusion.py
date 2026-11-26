"""Deliver fixed PCNA fetch failures through real detached Jobs and acknowledged native briefs."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from Bio.PDB.MMCIF2Dict import MMCIF2Dict

from acceptance.multi_workflow.acquired_material_handoff import FETCH, open_module
from acceptance.multi_workflow.test_pcna_archive import ENTRY, cif_bytes
from acceptance.v622.test_close import CloseStudy, actual_packet
from acceptance.v62a.interleavings_fixture import branch, draft, save
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest as capabilities
from gx_core.p0 import manifest, v6_goal

MARKER = "UNRELATED-ARCHIVE-PROCESS-DIAGNOSTIC"
# Only the detached entry's HTTPS response is replaced. Its original parser, error
# handling, runner exit, sealed log, graph settlement and brief delivery are exercised.
FIXED_RESPONSE = """
import http.client
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

response_path, entry, *arguments = sys.argv[1:]
sys.argv = [entry, *arguments]
with patch.object(http.client, 'HTTPSConnection') as tcp:
    if response_path.endswith('.error'):
        tcp.side_effect = RuntimeError(Path(response_path).read_text())
    else:
        http = tcp.return_value.getresponse.return_value
        http.status = 200
        http.read.return_value = Path(response_path).read_bytes()
    try:
        runpy.run_path(entry, run_name='__main__')
    finally:
        tcp.assert_called_once_with('127.0.0.1', 7890, timeout=25)
"""


def response(work, case, entry):
    if case == "unknown":
        path = work / "process.error"
        path.write_text(MARKER)
        return path
    doc = MMCIF2Dict(str(ENTRY.with_name(entry + ".cif")))
    if case == "single":
        for i, chain in enumerate(doc["_atom_site.label_asym_id"]):
            if chain in ("B", "C"):
                doc["_atom_site.pdbx_PDB_model_num"][i] = "2"
    else:
        generators = doc["_pdbx_struct_assembly_gen.asym_id_list"]
        doc["_pdbx_struct_assembly_gen.asym_id_list"] = ["A,B"] * len(generators)
    path = work / "response.cif"
    path.write_bytes(cif_bytes(doc))
    return path


def wait_for_failure(study, node_id):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        study.advance()
        node, = study.rows("SELECT node_id,status,result_ref,job_id FROM v6_nodes WHERE node_id=?", node_id)
        if node["status"] not in {"PENDING", "RUNNING"}:
            return node
        time.sleep(.035)
    raise AssertionError(f"Job failed to settle before deadline: {node}")


class ArchiveFailureHandoff(unittest.TestCase):
    def exercise(self, owner, case, entry, reason_code):
        session = os.environ.get("GX_PCNA_EXCLUSION_WORK")
        if session:
            work = Path(session).resolve() / (owner + "-" + case)
            work.mkdir(parents=True, exist_ok=False)
        else:
            work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        original = response(work, case, entry)
        stub = work / "fixed_http.py"
        stub.write_text(FIXED_RESPONSE)
        request = work / "archive.json"
        save(request, {"schema": "gx.pcna-archive-request/1", "accession": "P12004", "entry_ids": [entry]})
        config = {"plugins": ["org.gx.investigation@1.0.0", "org.gx.pcna-archive@1.0.0"],
                  "research_modules": ["investigation@1"], "capabilities": [FETCH],
                  "input_files": [{"path": str(request), "role": "archive"}]}
        normal_plan = capabilities.launch_plan

        def local_transport(*args, **kwargs):
            plan = normal_plan(*args, **kwargs)
            self.assertIn("fetch", plan.argv)
            argv = list(plan.argv)
            offset = argv.index(plan.entry_script)
            argv[offset:offset] = [str(stub), str(original)]
            return replace(plan, argv=argv)

        with CloseStudy(work / "project", spec_updates=config) as study:
            archive, = study.materials
            actor, scope = study.lead, "run"
            ops = []
            if owner == "module":
                opened = open_module(study, "check", [archive], [FETCH], "Check the deposited assembly.", 1)
                actor, scope = study.bind(opened["task_id"]), opened["scope"]
                study.call(study.lead, "gx_decide_revision", {"decision": draft()}, "wait")
                branch_id = study.brief(actor)["module"]["branch_id"]
            else:
                route = branch("archive")
                route["budget_limits"]["cpu_seconds"] = 120
                ops.append(route)
                branch_id = route["branch"]
            node = study.node(actor, "fetch", archive, branch_id=branch_id, capability=FETCH,
                              parameters={"entry_id": entry})
            queued = study.call(actor, "gx_decide_revision", {"decision": draft(*ops, node, watches=[
                {"event": "job_settled", "subject_ref": node["node"]}])}, "fetch")
            with patch.object(capabilities, "launch_plan", local_transport):
                failed = wait_for_failure(study, queued["aliases"][node["node"]])
            self.assertEqual(failed["status"], "FAILED")
            self.assertIsNone(failed["result_ref"])
            job = manifest.read_manifest(study.conn, failed["job_id"])
            self.assertEqual(job["completion"]["status"], "FAILED")
            self.assertEqual(job["admission"]["parameters"]["entry_id"], entry)
            log, = [item for item in job["completion"]["logs"] if item["kind"] == "job_stderr"]
            stderr = ArtifactService(study.conn).read_bytes(log["artifact_ref"]).decode()
            if case == "unknown":
                self.assertIn(MARKER, stderr)
            else:
                marker = "GX_PCNA_EXCLUSION " + ("observed 1" if case == "single" else "assembly")
                self.assertEqual(stderr.rstrip(), marker)
            settled, = study.rows("SELECT event_id,reason,payload_json FROM events WHERE run_id=? "
                                  "AND kind='v6.node.settled' AND subject_id=?", study.run_id, failed["node_id"])
            received = actual_packet(study, study.next_actor(scope))
            projected, = [n for n in received["packet"]["nodes"] if n["node_id"] == failed["node_id"]]
            safe_receipt = {"run_id": study.run_id, "owner": owner, "case": case, "job_id": failed["job_id"],
                            "node_id": failed["node_id"], "stderr_ref": log["artifact_ref"],
                            "settlement": json.loads(settled["payload_json"]), "brief_sha256": received["text_sha256"],
                            "brief_task": received["task"], "received_node": projected,
                            "job_detail": job["completion"]["failure"].get("decision_detail"),
                            "fixture": "Locally altered archive response or injected process failure. Native replies were scripted.",
                            "model_calls": 0}
            if session:
                save(work / "receipt.json", safe_receipt)
            self.assertNotIn(MARKER, json.dumps(received["packet"]))
            self.assertNotIn(MARKER, json.dumps(settled))
            failure = projected.get("failure")
            self.assertIsInstance(failure, dict, projected)
            self.assertEqual(failure, job["completion"]["failure"].get("decision_detail"))
            self.assertEqual(failure, safe_receipt["settlement"].get("failure"))
            self.assertEqual(failure["entry_id"], entry)
            self.assertEqual(failure["reason_code"], reason_code)
            self.assertTrue(failure["summary"].strip())
            self.assertEqual(failure["kind"], "job_failure" if case == "unknown" else "archive_exclusion")
            if case == "single":
                self.assertEqual(failure.get("chain_count"), 1)
            if owner == "module":
                # Wake a fresh Lead before the module reports. Its node card may show coarse Job status.
                v6_goal.record_observation(study.conn, study.run_id, archive, source="operator-recheck")
                outside = actual_packet(study, study.next_actor("run"))["packet"]
                self.assertFalse(any(n.get("failure") for n in outside["nodes"] if n["node_id"] == failed["node_id"]))
                self.assertNotIn(reason_code, json.dumps(outside))
                self.assertNotIn(MARKER, json.dumps(outside))

    def test_closed_reasons_reach_the_owner_and_other_diagnostics_stay_in_the_log(self):
        for owner in ("lead", "module"):
            for case, entry, code in (("single", "1VYM", "pcna_chain_count"),
                                      ("assembly", "9VGW", "pcna_assembly_mismatch"),
                                      ("unknown", "3JA9", "nonzero_exit")):
                with self.subTest(owner=owner, case=case):
                    self.exercise(owner, case, entry, code)
