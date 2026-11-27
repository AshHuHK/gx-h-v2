"""A scripted native transport, the normal Goal entry, two real detached CPU Jobs, and a new method view."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a import interleavings_fixture as fx
from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest
from gx_core.p0 import capability_views, cli, conversation, conversation_delivery, v62_outputs
from gx_core.p0.roots import Roots
from gx_core.errors import SchemaInvalid
from acceptance.science_capabilities import record_method
from acceptance.v622.test_close import CloseStudy, close_decision
from acceptance.v623 import test_close_metrics as endpoint_fixture
from gx_core.plugins import development
from gx_core.plugins.catalog import package_digest
from gx_core.plugins.cli import _install

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/record-method-authoring"
BUILDER = "derive-record-coverage@1"
PARAMS = {"source_quote": "an asynchronous job API service to map the identifiers of one database to another",
          "cohort_rows": "candidates", "cohort_id": "accession", "evidence_rows": "pointers",
          "evidence_id": "entry", "evidence_value": "pmid"}


class DerivedMethod(unittest.TestCase):
    def test_authored_method_reaches_same_goal_job_and_cited_decision(self):
        self.exercise_recipe(change_calibration=False)

    def test_changed_calibration_runs_a_fresh_job(self):
        self.exercise_recipe(change_calibration=True)

    def exercise_recipe(self, change_calibration):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        release = work / "release/gx-h"
        shutil.copytree(config.HARNESS_ROOT, release, ignore=shutil.ignore_patterns("var", "__pycache__", ".venv", "tests"))
        self.enterContext(patch.object(config, "HARNESS_ROOT", release))
        self.enterContext(patch.object(config, "CONFIG_DIR", release / "config"))
        self.enterContext(patch.object(manifest, "HARNESS_ROOT", release))
        pkg = ROOT / "gx-h/plugins/installed/org.gx.record-method-builder/1.0.0"
        _install(pkg, package_digest(pkg), release / "plugins/installed", config.CONFIG_DIR / "workflow_packages.json")
        state = work / "study/state"
        grant = json.loads((EXAMPLE / "trehalose/goal.json").read_bytes())
        for item in grant["input_files"]:
            item["path"] = str((EXAMPLE / "trehalose" / item["path"]).resolve())
        grant.pop("budget_profile", None)
        grant["max_parallel_roots"] = 5
        grant.update(workspace_budget={"job_submissions":10,"api_calls":10,"tokens":100000,"cpu_seconds":240,
                                      "worker_tasks":24,"gpu_jobs":0,"gpu_minutes":0,"wall_hours":1})
        grant["run_budget"] = grant["workspace_budget"]
        cfg = work / "grant.json"
        cfg.write_text(json.dumps(grant))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, cli.main(["start", "--state", str(state), "--run-key", "develop-record-method",
                        "--goal", (EXAMPLE / "trehalose/question.md").read_text(), "--goal-config", str(cfg)]), output.getvalue())
        s = self.enterContext(CloseStudy(state.parent, reopen=True))
        actor = s.next_actor("run")
        # Use material roles; presentation order is independent of argv order.
        files = json.loads(s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0])["files"]
        inputs = {f["role"]: f["artifact_ref"] for f in files}
        route = fx.branch("records")
        route.update(budget_limits={"job_submissions": 2, "cpu_seconds": 60})
        node = s.node(actor, "derive", inputs["method_source"], branch_id="local:records",
                      capability=BUILDER, parameters=PARAMS)
        adopted = s.call(actor, "gx_decide_revision", {"decision": fx.draft(route, node,
                    evidence=[inputs["method_source"]], watches=[{"event":"job_settled","subject_ref":"local:derive"}])}, "develop")
        first = s.settle(adopted["aliases"]["local:derive"])
        spec_ref = v62_outputs.published_outputs(s.conn, s.run_id, first["job_id"])[0]["artifact_ref"]
        actor = s.next_actor("run")
        with self.assertRaisesRegex(SchemaInvalid, "original Job output"):
            s.call(actor, "gx_install_record_method", {"build_ref": inputs["cohort"], "expected_revision": 0},
                   "untrusted-input")
        install_args = {"build_ref": spec_ref, "expected_revision": 0}
        installed = s.call(actor, "gx_install_record_method", install_args, "install")
        self.assertEqual(installed, s.call(actor, "gx_install_record_method", install_args, "install"))
        method = installed["capability_version"]
        self.assertEqual(1, capability_views.current(s.conn, s.run_id)["revision"])
        self.assertEqual(installed, s.call(actor, "gx_install_record_method", install_args, "same-build-new-call"))
        self.assertEqual(method, development.skills(s.conn, s.run_id)[0]["reference"])
        # Freeze a new brief after the view changed, and commission the developed method there.
        s.call(actor, "gx_decide_revision", {"decision": fx.draft(evidence=[spec_ref])}, "read-build")
        actor = s.next_actor("run")
        measured = s.node(actor, "audit", inputs["cohort"], branch_id=adopted["aliases"]["local:records"], capability=method)
        measured["inputs"].append(inputs["evidence"])
        measured["depends_on"] = [first["node_id"]]
        dispatched = s.call(actor, "gx_decide_revision", {"decision": fx.draft(measured, evidence=[inputs["cohort"]],
                     watches=[{"event":"job_settled","subject_ref":"local:audit"}])}, "audit")
        job = s.settle(dispatched["aliases"]["local:audit"])
        published = v62_outputs.published_outputs(s.conn, s.run_id, job["job_id"])[0]["artifact_ref"]
        result = ArtifactService(s.conn).read_json(published)
        self.assertEqual((result["numerator"], result["denominator"], result["matched_without_value"]), (2, 3, ["P35172"]))
        actor = s.next_actor("run")
        read = s.call(actor, "gx_read_artifact", {"ref": published}, "read-coverage")
        self.assertEqual(read["denominator"], 3)
        saved = s.call(actor, "gx_save_recipe", {
            "recipe_id": "accession-evidence-coverage", "version": "1", "title": "Located papers per accession",
            "decision_point": {"step": "coverage", "output": v62_outputs.published_outputs(s.conn, s.run_id, job["job_id"])[0]["output_name"],
                               "question": "Which sources can resolve the remaining evidence gap?",
                               "actions": ["inspect linked papers", "seek independent originals"],
                               "stop_condition": "The available cohort has been accounted for."},
            "calibration": {"step": "setup", "scope": "Accession-level source coverage; case-sensitive identifiers"},
            "nodes": [{"slot": "coverage", "node_id": job["node_id"]}, {"slot": "setup", "node_id": first["node_id"]}],
            "inputs": [{"name": role, "artifact_ref": inputs[role]} for role in ("cohort", "evidence", "method_source")],
            "units": "Fraction of unique cohort accessions", "id_alignment": "Case-sensitive Swiss-Prot accession",
            "missing_values": "A null pointer or absent match stays in the population."}, "save")
        decision = close_decision([published])
        endpoint = endpoint_fixture.EndpointClose.read_page(self, s, actor, published, 0, "read-coverage-endpoint")
        decision["metric_refs"] = [published + "#" + endpoint["endpoint_rows"][0]["key"]]
        decision["rationale"] = "Prioritize the two entries with paper pointers for source review; the other entry remains unclassified."
        accepted = s.call(actor, "gx_decide_revision", {"decision": decision}, "cite-readout")
        proof = dict(run_id=s.run_id, provider="scripted_native_transport", model_calls=0, source=installed["source"],
                     method=method, plugin=installed["plugin"], builder_job=first["job_id"], build_ref=spec_ref,
                     execution_job=job["job_id"], measurement_ref=published, reading=result,
                     cited_decision_ref=accepted["decision_ref"], calibration=installed["calibration"])
        s.advance()
        parent = s.run_id
        self.assertEqual(record_method.report(s.conn, parent)["lead_decisions_citing_coverage"][0]["decision_ref"],
                         accepted["decision_ref"])
        options = record_method.followup(s.conn, parent)
        question = (EXAMPLE / "sucrose/question.md").read_text()
        conversation.send(s.conn, parent, question, "sucrose-followup-01", successor=options)
        recorded = s.conn.execute("SELECT successor_json FROM p0_conversation_messages WHERE message_id=?",
                                  ("sucrose-followup-01",)).fetchone()[0]
        s.run_id = conversation_delivery.start_successor(s.conn, state, parent, "sucrose-followup-01", question,
                                                         json.loads(recorded), native_owner=True)["run_id"]
        client = s.roots.client
        s.roots = Roots(s.conn, run_id=s.run_id, cwd=state, token_budget=10)
        s.roots.client = client
        actor = s.next_actor("run")
        selected = s.call(actor, "gx_select_capability", {"method": method, "action": "enable", "revision": 0}, "select")
        self.assertEqual(1, selected["revision"])
        s.call(actor, "gx_decide_revision", {"decision": fx.draft()}, "wait-for-view")
        actor = s.next_actor("run")
        files = json.loads(s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0])["files"]
        child_inputs = {f["role"]: f["artifact_ref"] for f in files}
        card = s.call(actor, "gx_query_catalog", {"kind": "recipe", "reference": saved["reference"]}, "find-recipe")
        self.assertEqual(saved["digest"], card["spec"]["digest"])
        params = {"recipe": saved["reference"], "digest": saved["digest"], "branch": "local:sucrose", "prefix": "sucrose",
                  "calibration_scope": saved["calibration"]["scope"],
                  "inputs": {role: child_inputs[role] for role in ("cohort", "evidence", "method_source")}}
        calibration_asset = release / "plugins/installed/org.gx.record-method-builder/1.0.0/template/coverage.py"
        original_bytes = calibration_asset.read_bytes()
        calibration_asset.write_bytes(original_bytes + b"\n# Changed calibration environment.\n")
        try:
            changed_asset = s.call(actor, "gx_instantiate_recipe", params, "changed-environment")
            self.assertEqual("run_on_target", changed_asset["calibration"]["status"])
        finally:
            calibration_asset.write_bytes(original_bytes)
        if change_calibration:
            params["calibration_parameters"] = {**PARAMS, "source_quote": "an asynchronous job API service"}
        alternatives = s.call(actor, "gx_instantiate_recipe", dict(params, calibration_scope="new assay"), "different-scope")
        self.assertEqual("run_on_target", alternatives["calibration"]["status"])
        repeat = s.call(actor, "gx_instantiate_recipe", dict(params, independent_repeat=True), "independent")
        self.assertTrue(all(op["independent_repeat"] for op in repeat["ops"]))
        expansion = s.call(actor, "gx_instantiate_recipe", params, "reuse")
        self.assertEqual("coverage", expansion["ops"][0]["recipe_decision"]["step"])
        self.assertEqual(1 + int(change_calibration), len(expansion["ops"]))
        self.assertEqual("run_on_target" if change_calibration else "referenced", expansion["calibration"]["status"])
        if not change_calibration:
            self.assertEqual(first["job_id"], expansion["calibration"]["job_id"])
        branch = fx.branch("sucrose")
        branch["budget_limits"].update(cpu_seconds=60, job_submissions=2)
        child = s.call(actor, "gx_decide_revision", {"decision": fx.draft(branch, *expansion["ops"],
                    evidence=[child_inputs["cohort"]], watches=[{"event":"job_settled",
                    "subject_ref":expansion["ops"][0]["node"]}])}, "measure-again")
        if change_calibration:
            recalibrated = s.settle(child["aliases"][expansion["ops"][1]["node"]])
            self.assertNotEqual(first["job_id"], recalibrated["job_id"])
        child_job = s.settle(child["aliases"][expansion["ops"][0]["node"]])
        out = v62_outputs.published_outputs(s.conn, s.run_id, child_job["job_id"])[0]["artifact_ref"]
        second = ArtifactService(s.conn).read_json(out)
        self.assertEqual((second["numerator"], second["denominator"], second["matched_without_value"]), (0, 1, ["P00724"]))
        actor = s.next_actor("run")
        checkpoint = s.brief(actor)["recipe_decisions"][0]
        self.assertEqual((out, child_job["job_id"]), (checkpoint["evidence"]["artifact_ref"], checkpoint["job_id"]))
        self.assertNotIn(published, json.dumps(checkpoint))
        s.call(actor, "gx_read_artifact", {"ref": out}, "read-sucrose")
        close = close_decision([out])
        endpoint = endpoint_fixture.EndpointClose.read_page(self, s, actor, out, 0, "read-sucrose-endpoint")
        close["metric_refs"] = [out + "#" + endpoint["endpoint_rows"][0]["key"]]
        close["rationale"] = "The lone sucrose accession has no experimental paper pointer here; look for outside sources."
        adopted = s.call(actor, "gx_decide_revision", {"decision": close}, "close-sucrose")
        s.advance()
        self.assertEqual("CLOSED", s.conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (s.run_id,)).fetchone()[0])
        self.assertEqual(1 + int(change_calibration), len(record_method.report(s.conn, s.run_id)["jobs"]))
        proof["saved_recipe"] = saved
        proof["successor"] = {"run_id": s.run_id, "job_id": child_job["job_id"], "reading": second, "output_ref": out,
                              "decision_point": checkpoint, "calibration": expansion["calibration"],
                              "cited_decision_ref": adopted["decision_ref"], "recipe_digest": card["spec"]["digest"]}
        if path := os.environ.get("GX_CHANGED_CALIBRATION_RECEIPT" if change_calibration else "GX_RECORD_METHOD_RECEIPT"):
            Path(path).write_text(json.dumps(proof, indent=2, ensure_ascii=False) + "\n")

    def test_missing_keys_are_never_dropped_from_a_denominator_and_foreign_ids_do_not_count(self):
        import runpy
        pkg = ROOT / "gx-h/plugins/installed/org.gx.record-method-builder/1.0.0"
        calculate = runpy.run_path(str(pkg / "template/coverage.py"))["measure"]
        fields = {"cohort_rows": "$", "cohort_id": "key", "evidence_rows": "$",
                  "evidence_id": "accession", "evidence_value": "reading"}
        with self.assertRaisesRegex(ValueError, "identifier"):
            calculate([{"key": "A"}, {"key": None}], [{"accession": "A", "reading": "ok"}], fields)
        with self.assertRaisesRegex(ValueError, "identifier cannot"):
            calculate([{"key": "A"}], [{"accession": "A"}], dict(fields, evidence_value="accession"))
        actual = calculate([{"key":"A"}, {"key":"a"}], [{"accession":"A", "reading":0},
                           {"accession":"B", "reading":17}, {"accession":"a", "reading":"  "}], fields)
        self.assertEqual((actual["numerator"], actual["denominator"], actual["outside_cohort_ids"]), (1, 2, ["B"]))
