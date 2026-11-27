"""A4. Follow a read-only method survey into a real coordinate Job; keep a sourced gap."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.multi_workflow.test_pcna_native import INTERFACE, MATERIALS, PLUGIN
from acceptance.v62a.interleavings_fixture import PRODUCT, draft
from acceptance.v622.test_close import SurveyedStudy
from acceptance.v623 import test_close_metrics
from gx_core.p0 import cli, manifest, v621_metrics


class MethodSurvey(unittest.TestCase):
    def test_installed_method_measured_and_document_only_candidate_remains_a_gap(self):
        folder = os.environ.get("GX_MULTITURN_METHOD_RECEIPTS")
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="survey-", dir=folder)).resolve()
        else:
            work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        coord_dir, digest = MATERIALS["1VYM"]
        coord = PRODUCT / "examples" / "pcna" / coord_dir / "1VYM.cif"
        # Use the shipped scientific software documentation and its bibliography offline.
        from Bio import __version__ as biopython_version
        docs = importlib.import_module("Bio.PDB.DSSP").__doc__.splitlines()
        wilke = next(line.strip() for line in docs if "Tien et al." in line)
        source_note = {"source": f"Biopython {biopython_version}, Bio.PDB.DSSP, module documentation",
                       "method": docs[0], "normalization_reference": wilke}
        note = work / "dssp-reference.json"
        note.write_text(json.dumps(source_note))
        config = {"research_effort": "deep", "research_modules": ["method_workflow_survey@1"],
                  "plugins": [PLUGIN], "capabilities": [INTERFACE],
                  "input_files": [{"role": "structure", "path": str(coord), "sha256": digest,
                                   "acquisition": str(coord.with_suffix(".json"))},
                                  {"role": "method_spec", "path": str(note)}]}
        grant = work / "grant.json"
        grant.write_text(json.dumps(config))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, args, rid, timeout: cli.snapshot(conn, rid)), \
                contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(["research", "--state", str(work / "state"), "--run-key", "method-survey",
                               "--goal", "Which interface QC observations are available on these deposited coordinates?",
                               "--goal-config", str(grant)])
        self.assertEqual(status, 0)
        s = self.enterContext(SurveyedStudy(work, reopen=True))
        lead = s.next_actor("run")
        coord_ref, note_ref = s.materials
        args = {**s.open_args("commission-survey", inputs=[note_ref], caps=[]), "module_id": "method_workflow_survey",
                "focus": "evaluation", "expected_delivery": "Identify an applicable installed readout and any unresolved alternative."}
        opened = s.call(lead, "gx_open_module", args, "commission")
        survey = s.bind(opened["task_id"])
        hits = s.call(survey, "gx_query_catalog", {"kind": "method", "query": "pcna-interface"}, "search-interface")
        self.assertIn(INTERFACE, [item["capability_version"] for item in hits["items"]])
        card = s.call(survey, "gx_query_catalog", {"reference": INTERFACE}, "open-card")
        card_ref = card["source_ref"]
        self.assertEqual(s.call(survey, "gx_read_artifact", {"ref": card_ref}, "read-card"), card["spec"])
        self.assertEqual(s.call(survey, "gx_read_artifact", {"ref": note_ref}, "read-note"), source_note)
        missing = s.call(survey, "gx_query_catalog", {"kind": "method", "query": "dssp"}, "search-dssp")
        self.assertEqual((missing["total"], missing["items"]), (0, []))
        gap = "The DSSP relative accessibility readout has no matching method in the Run catalog."
        s.call(lead, "gx_decide_revision", {"decision": draft()}, "await-survey")
        receipt = s.report(survey, card_ref, findings=[
            {"text": "Use the catalogued PCNA interface measurement on a named deposited chain pair.",
             "source_refs": [card_ref]},
            {"text": f"{gap} The manual cites Tien and colleagues for the Wilke normalization.", "source_refs": [note_ref]}],
            artifact_refs=[card_ref, note_ref], gaps=[gap])
        lead = s.next_actor("run")
        report = s.call(lead, "gx_read_artifact", {"ref": receipt["report_ref"]}, "read-survey")
        self.assertEqual(report["findings"][1]["source_refs"], [note_ref])
        self.assertEqual(report["gaps"], [gap])
        self.assertEqual(s.rows("SELECT job_id FROM jobs"), [])
        branch = {"op": "open_branch", "branch": "local:contacts",
                  "hypothesis": "The first pair of deposited subunits has interfacial contacts.",
                  "alternative": "No residue contacts are resolved between this pair.",
                  "method_family": INTERFACE, "discriminating_observation": "Distinct contacting residue pairs.",
                  "budget_limits": {"cpu_seconds": 400, "job_submissions": 1},
                  "stop_condition": "Read the interface contact count for the deposited pair."}
        node = s.node(lead, "measure", coord_ref, branch_id=branch["branch"], capability=INTERFACE,
                      parameters={"pair": "12", "block_index": -1})
        node["question"] = "How many distinct residue pairs contact across the first two subunits?"
        adopted = s.call(lead, "gx_decide_revision", {"decision": draft(branch, node,
            evidence=[coord_ref, receipt["report_ref"]], watches=[{"event": "job_settled", "subject_ref": node["node"]}])},
            "follow-survey")
        settled = s.settle(adopted["aliases"][node["node"]])
        job = settled["job_id"]
        jobs = s.rows("SELECT job_id, capability_id, status FROM jobs")
        self.assertEqual(jobs, [{"job_id": job, "capability_id": INTERFACE.split("@")[0], "status": "SUCCEEDED"}])
        completed = manifest.read_manifest(s.conn, job)["completion"]
        named = next(o for o in completed["outputs"] if o["output_name"] == "interface.json")
        self.assertEqual(named["source_inputs"][0]["sha256"], digest)
        lead = s.next_actor("run")
        if named["artifact_ref"] not in s.brief(lead)["evidence_refs"]:
            s.call(lead, "gx_decide_revision", {"decision": draft(watches=[
                {"event": "job_settled", "subject_ref": settled["node_id"]}])}, "await-settlement")
            lead = s.next_actor("run")
        offset = 0
        while offset is not None:
            key = f"read-endpoints-{offset}"
            page = test_close_metrics.EndpointClose.read_page(self, s, lead, named["artifact_ref"], offset, key)
            rows = [r for r in page["endpoint_rows"] if r["id"] == "n_contact_residue_pairs"]
            if rows:
                break
            offset = page["next_endpoint_offset"]
        self.assertEqual(len(rows), 1)
        endpoint, = rows
        reading, = [r for r in v621_metrics.from_output(s.conn, s.run_id, named["artifact_ref"])
                    if r["metric_id"] == endpoint["id"]]
        self.assertEqual((reading["job_id"], reading["value"], reading["unit"]), (job, 33, "pairs"))
        self.assertEqual(endpoint["value"], "33 pairs")
        received, = s.rows("SELECT event_id FROM events WHERE kind='v623.endpoints.received' AND actor=? "
                           "AND json_extract(payload_json,'$.receipt')=?", lead.thread, key)
        if folder:
            path = Path(folder)
            checks = {"run_id": s.run_id, "retained_state": str(work / "state"), "transport": "scripted native acknowledgements", "paid_model_calls": 0,
                      "survey_scope": survey.scope, "method_card": card_ref, "report": receipt["report_ref"],
                      "lead_decision_ref": adopted["decision_ref"], "input_ref": coord_ref, "input_sha256": digest,
                      "jobs": jobs, "output_name": named["output_name"], "output_ref": named["artifact_ref"],
                      "output_sha256": named["sha256"], "endpoint": endpoint, "metric_ref": reading["metric_ref"], "received_event": received["event_id"],
                      "uninstalled_candidate": {"source_ref": note_ref, "source": source_note, "gap": gap,
                                                "catalog_matches": missing["total"], "jobs": []}}
            (path / "method-survey.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
