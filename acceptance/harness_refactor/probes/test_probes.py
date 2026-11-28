"""Submit minimal numerical probe drafts through the ordinary research CLI and native callback."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import SurveyedStudy
from gx_core import config, contracts
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import manifest as execution, v6_goal, v62_outputs
from gx_core.plugins import cli as plugin_cli
from gx_core.plugins.catalog import package_digest

ROOT = Path(__file__).resolve().parents[3]
CAP = "instrument-series@1"


def start(case, *, outputs=("readings.json",), samples=1, old=False, delegated=False, with_metric=False, single_role="readings"):
    tmp = Path(case.enterContext(tempfile.TemporaryDirectory()))
    release = tmp / "package/gx-h"
    release.mkdir(parents=True)
    # The real executable resolves its own gx_core/config from this isolated checkout.
    for name in ("contracts", "adapters", "runtime", "workflows"):
        (release / name).symlink_to(config.HARNESS_ROOT / name, target_is_directory=True)
    shutil.copytree(config.HARNESS_ROOT / "gx_core", release / "gx_core",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(config.CONFIG_DIR, release / "config")
    (release.parent / "bin").mkdir()
    shutil.copy2(ROOT / "bin/gx-p0", release.parent / "bin/gx-p0")
    table = release / "config/workflow_packages.json"
    table.write_text(json.dumps({**json.loads(table.read_bytes()), "plugins": []}))
    fixture = tmp / "plugin"
    shutil.copytree(ROOT / "gx-h/tests/fixtures/v62_output", fixture)
    method = json.loads((fixture / "series.json").read_bytes())
    method["manifest"]["outputs"] = [
        dict(name=name, kind="file:instrument_summary", required=True, nature="analysis", source_roles=["readings"])
        for name in outputs]
    if with_metric:
        method["manifest"]["outputs"][0]["metric_specs"] = {
            "mean": {"op": "decimal/1", "reader": "json/1", "pointer": "/readings/0", "unit": "mV", "places": 2}}
    (fixture / "series.json").write_text(json.dumps(method))
    (fixture / "measure.py").write_text(
        'from pathlib import Path\nimport json, sys\n'
        'record=json.loads(Path(sys.argv[1]).read_bytes())\n'
        'values=record["readings"]\n'
        f'for name in {list(outputs)!r}:\n'
        '    (Path(sys.argv[2])/name).write_text(json.dumps({"readings":[sum(values)/len(values)]}))\n')
    plugin_cli._install(fixture, package_digest(fixture), release / "plugins/installed", table)
    data = []
    for i in range(samples):
        path = tmp / f"gauge_{i}.json"
        path.write_text(json.dumps({"readings": [4 + i, 6 + i, 8 + i]}))
        data.append({"role": single_role if samples == 1 else f"sample_{i}", "path": str(path)})
    spec = dict(research_modules=[], research_effort="quick", capability_collections=[],
                capabilities=[CAP], plugins=["org.example.instrument-series@1.0.0"], input_files=data)
    if delegated:
        spec.update(research_modules=["ad_hoc@1"], research_effort="medium")
    if old:
        spec.pop("capability_collections")
        spec.update(evidence_policy="gx.evidence/6.2.3", historical_replay=True)
    state = tmp / "study/state"
    role = tmp / "goal.json"
    role.write_text(json.dumps(spec))
    cmd = [sys.executable, "-B", str(release.parent / "bin/gx-p0"), "research", "--state", str(state),
           "--run-key", "archived-gauge", "--goal", "Inspect the calibrated gauge readings and check the summary.",
           "--goal-config", str(role), "--timeout", "0.000001"]
    out = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    case.assertEqual(out.returncode, 75, out.stderr + out.stdout)
    for owner, key, value in [(config, "HARNESS_ROOT", release), (config, "CONFIG_DIR", release / "config"),
                              (contracts, "HARNESS_ROOT", release), (manifest, "HARNESS_ROOT", release)]:
        case.enterContext(patch.object(owner, key, value))
    study = case.enterContext(SurveyedStudy(state.parent, reopen=True))
    study.next_actor("run")
    return study


def probe(refs, *, cap=CAP):
    return {"op": "add_node", "capability": cap, "question": "What mean does this record give?", "inputs": refs}


class Probes(unittest.TestCase):
    def test_two_steps_bind_real_output_and_charge_each_job_once(self):
        s = start(self)
        first = dict(probe(s.materials), node="local:read")
        second = dict(probe(["local:read"]), depends_on=[], independent_repeat=True)
        body = {"rationale": "Compare the archived mean with a fresh summary using the same scale.", "ops": [first, second]}
        actor = s.lead
        result = s.call(actor, "gx_decide_revision", {"decision": body}, "two-step")
        ops = json.loads(s.rows("SELECT request_json FROM v6_decisions")[0]["request_json"])["decision"]["ops"]
        account, a, b = ops
        self.assertEqual(account["budget_limits"], {"job_submissions": 2, "cpu_seconds": 30})
        self.assertEqual(b["input_ports"], [{"index": 0, "output": "readings.json"}])
        self.assertEqual(b["depends_on"], [a["node"]])
        src, dst = (s.settle(result["aliases"][op["node"]]) for op in (a, b))
        port, = v62_outputs.published_outputs(s.conn, s.run_id, src["job_id"])
        received, = execution.read_manifest(s.conn, dst["job_id"])["admission"]["inputs"]
        self.assertEqual((received["artifact_ref"], received["sha256"]), (port["artifact_ref"], port["sha256"]))
        final_port, = v62_outputs.published_outputs(s.conn, s.run_id, dst["job_id"])
        values = [ArtifactService(s.conn).read_json(row["artifact_ref"]) for row in (port, final_port)]
        self.assertEqual(values, [{"readings": [6.]}, {"readings": [6.]}])
        self.assertEqual(s.call(actor, "gx_decide_revision", {"decision": body}, "two-step"), result)
        self.assertEqual(len(s.rows("SELECT job_id FROM jobs")), 2)
        if path := os.environ.get("GX_PROBE_RECEIPT"):
            Path(path).write_text(json.dumps({"type":"SCRIPTED_CALLBACK", "run_id": s.run_id,
                "decision_event": result["event_id"], "input": s.materials[0], "operations": ops,
                "source_job": src["job_id"], "consuming_job": dst["job_id"], "selected_output": port,
                "consumed": received, "final_output": final_port, "readbacks": values, "replayed_job_count": 2}, indent=2)+"\n")

    def test_ambiguous_upstream_and_sample_choices_return_together(self):
        s = start(self, outputs=("range.json", "mean.json"), samples=2)
        sources = [dict(probe(s.materials[:1]), node="local:first"), probe(["local:first"]), probe([])]
        with self.assertRaises(SchemaInvalid) as exc:
            s.call(s.lead, "gx_decide_revision", {"decision":{"ops": sources, "rationale":"Check the uncertainty."}}, "ambiguous")
        choices = {v["field"]:v["options"] for v in exc.exception.details["choices"]}
        self.assertEqual(set(choices["ops[1].input_ports[0]"]), {"mean.json","range.json"})
        self.assertEqual(set(choices["ops[2].inputs"]), set(s.materials))
        self.assertIsNone(s.head())
        self.assertEqual(s.rows("SELECT 1 FROM jobs"), [])

    def test_single_material_and_method_revision_are_uniquely_bound(self):
        s = start(self)
        op = probe([], cap="instrument-series")
        del op["inputs"]
        result = s.call(s.lead, "gx_decide_revision", {"decision":{"rationale":"Find the initial reading.", "ops":[op]}}, "first")
        node = json.loads(s.rows("SELECT spec_json FROM v6_nodes")[0]["spec_json"])
        self.assertEqual(node["inputs"], s.materials)
        self.assertEqual(node["capability"], CAP)
        self.assertTrue(result["aliases"])

    def test_historical_decision_retains_aggregate_input_selection(self):
        from acceptance.v62a.interleavings_fixture import branch, draft
        s = start(self, old=True)
        account = dict(branch("legacy"), budget_limits={"job_submissions": 2, "cpu_seconds": 30})
        first = dict(probe(s.materials), node="local:source", branch=account["branch"],
                     parameters={}, depends_on=[])
        second = dict(probe(["local:source"]), node="local:child", branch=account["branch"],
                      parameters={}, depends_on=["local:source"], independent_repeat=True)
        s.call(s.lead, "gx_decide_revision", {"decision": draft(account, first, second)}, "historical")
        saved = json.loads(s.rows("SELECT request_json FROM v6_decisions")[0]["request_json"])
        self.assertNotIn("input_ports", saved["decision"]["ops"][2])

    def test_lead_cannot_consume_undelivered_module_node(self):
        from acceptance.v62a.interleavings_fixture import draft
        s = start(self, delegated=True)
        args = s.open_args("gauge", inputs=s.materials, caps=[CAP])
        args.update(module_id="ad_hoc", focus="Gauge measurements",
                    expected_delivery="Report the observed value with the archived output.")
        opened = s.call(s.lead, "gx_open_module", args, "open-gauge")
        worker = s.bind(opened["task_id"])
        s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-reading")
        result = s.submit_node(worker, "summary", capability=CAP)
        node = result["aliases"]["local:summary"]
        job = s.settle(node)
        v6_goal.record_observation(s.conn, s.run_id, s.materials[0], source="operator-check")
        lead = s.next_actor("run")
        denied = dict(probe([node]), depends_on=[node])
        with self.assertRaisesRegex(ScopeDenied, "delivered artifact"):
            s.call(lead, "gx_decide_revision", {"decision":{"ops":[denied], "rationale":"Recheck the reading."}}, "undelivered")
        self.assertEqual(len(s.rows("SELECT job_id FROM jobs")), 1)
        self.assertTrue(job["result_ref"])

    def test_scalar_measurement_uses_pinned_default_unit_and_rejects_an_incompatible_unit(self):
        from gx_core.p0 import v622_measurement
        s = start(self, with_metric=True)
        method = {"capability": CAP, "output": "readings.json"}
        prediction = {"method": method, "observable": "/metrics/mean"}
        pinned = v622_measurement.pin(s.conn, s.run_id, prediction)["measurement_spec"]
        self.assertEqual((pinned["unit"], pinned["input_role"], pinned["metric_id"]), ("mV", "readings", "mean"))
        with self.assertRaisesRegex(SchemaInvalid, "unit"):
            v622_measurement.pin(s.conn, s.run_id, dict(prediction, method=dict(method, unit="V")))

    def test_field_aliases_bind_the_same_route_and_preserve_bad_conflicts(self):
        # Operator records 11–15 used these labels in succession on one real Goal.
        from acceptance.v62a.interleavings_fixture import branch
        from gx_core.p0 import v624_operations
        route, job = branch("paper"), dict(probe([]), branch="local:paper", node="local:search")
        route["budget_limits"] = {"job_submissions": 1, "cpu_seconds": 15}
        canonical = {"ops": [route, job], "rationale": "Summarize the gauge."}
        for branch_key, node_key in (("ref", "ref"), ("branch_id", "node_id"), ("id", "id"), ("name", "name")):
            with self.subTest(branch_key=branch_key):
                a, b = dict(route), dict(job)
                a[branch_key], b[node_key] = a.pop("branch"), b.pop("node")
                self.assertEqual(v624_operations.canonical_labels(dict(canonical, ops=[a, b])), canonical)
        s = start(self)
        with self.assertRaises(SchemaInvalid) as exc:
            s.call(s.lead, "gx_decide_revision", {"decision": dict(canonical, ops=[dict(route, ref="local:other"), job])},
                   "conflict")
        self.assertEqual(set(exc.exception.details["options"]), {"local:paper", "local:other"})
        self.assertIsNone(s.head())
        del route["branch"]
        del job["node"]
        accepted = s.call(s.lead, "gx_decide_revision", {"decision": canonical}, "bind-referenced")
        self.assertIn("local:paper", accepted["aliases"])
        node, = s.rows("SELECT spec_json FROM v6_nodes")
        self.assertEqual(json.loads(node["spec_json"])["branch"], accepted["aliases"]["local:paper"])

    def test_one_unmatched_material_role_still_requires_codex_to_choose(self):
        s = start(self, single_role="reference_only")
        with self.assertRaises(SchemaInvalid) as exc:
            s.call(s.lead, "gx_decide_revision", {"decision": {"ops": [probe([])],
                   "rationale": "Choose the relevant archived material."}}, "uncertain-role")
        choice, = exc.exception.details["choices"]
        self.assertEqual((choice["roles"], choice["options"]), (["readings"], s.materials))
        self.assertIsNone(s.head())

    def test_scoped_sample_unit_and_visible_route_choices_arrive_in_one_refusal(self):
        from acceptance.v62a.interleavings_fixture import branch, draft
        s = start(self, samples=2, with_metric=True)
        lead = s.lead
        registrations = []
        for label in ("earlier", "later"):
            route = branch(label)
            body = dict(draft(route), comparison_plan={
                "branch": route["branch"], "material_role": "readings", "reference_refs": [s.materials[0]],
                "method": {"capability": CAP, "parameters": {}, "output_name": "readings.json",
                           "metric_id": "mean", "unit": "mV"}})
            registered = s.call(lead, "gx_decide_revision", {"decision": body}, label)["comparison"]
            registrations.append(registered)
            v6_goal.record_observation(s.conn, s.run_id, s.materials[0], source="review-" + label)
            lead = s.next_actor("run")
        route = branch("check")
        route["budget_limits"]["wall_seconds"] = 1
        stop = [{"op": "stop_branch", "branch": r["branch"], "reason": "Withdraw pending independent review."}
                for r in registrations]
        original_head = s.head()
        body = draft(route, dict(probe([]), branch=route["branch"]), *stop)
        with self.assertRaises(SchemaInvalid) as exc:
            s.call(lead, "gx_decide_revision", {"decision": body}, "mixed-selection")
        details = exc.exception.details
        self.assertEqual(details["field"], "ops[0].budget_limits.wall_seconds")
        choices = {row["field"]: row["options"] for row in details["choices"]}
        self.assertIn("cpu_seconds", choices["ops[0].budget_limits"])
        self.assertEqual(set(choices["ops[1].inputs"]), set(s.materials))
        self.assertEqual(set(choices["comparison_id"]), {r["comparison_id"] for r in registrations})
        self.assertEqual(s.head(), original_head)
        self.assertFalse(s.rows("SELECT 1 FROM jobs"))
        body["ops"][1]["capability"] = "ungranted@1"
        with self.assertRaises(SchemaInvalid) as outside:
            s.call(lead, "gx_decide_revision", {"decision": body}, "budget-before-scope")
        self.assertEqual(outside.exception.details["field"], details["field"])
        self.assertNotIn("choices", outside.exception.details)
        del route["budget_limits"]["wall_seconds"]
        with self.assertRaises(ScopeDenied):
            s.call(lead, "gx_decide_revision", {"decision": body}, "outside-scope")
        self.assertEqual(s.head(), original_head)
