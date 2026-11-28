"""Normal research entry and controlled native callbacks for a real instrument plugin."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import draft, branch
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import input_domain
from gx_core import config
from gx_core.capabilities import manifest, registry
from gx_core.errors import SchemaInvalid
from gx_core.p0 import cli, v6_jobs
from gx_core.plugins.catalog import package_digest
from gx_core.plugins import cli as plugins

ROOT = Path(__file__).resolve().parents[3]
CAP = "discharge-concurrent@1"
POLICY = "gx.science-domain/1"


class MethodDomain(unittest.TestCase):
    def setup_method(self, sample, domain=None, package_domain=None, *, remap=False):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        release = self.work / "release"
        shutil.copytree(config.CONFIG_DIR, release / "config")
        packages = release / "config/workflow_packages.json"
        table = json.loads(packages.read_bytes())
        packages.write_text(json.dumps({**table, "plugins":[]}))
        (release / "gx_core").symlink_to(config.HARNESS_ROOT / "gx_core", target_is_directory=True)
        (release / "adapters").symlink_to(config.HARNESS_ROOT / "adapters", target_is_directory=True)
        self.enterContext(patch.object(config, "HARNESS_ROOT", release))
        self.enterContext(patch.object(config, "CONFIG_DIR", release / "config"))
        self.enterContext(patch.object(manifest, "HARNESS_ROOT", release))
        origin = self.work / "import"
        shutil.copytree(ROOT / "examples/plugins/hydro-concurrent", origin)
        source = origin / "method.json"
        data = json.loads(source.read_bytes())
        if domain:
            data["manifest"]["method_card"]["applicable_domain"] = domain
        if package_domain:
            data["manifest"]["applicable_domain"] = package_domain
        source.write_text(json.dumps(data))
        if remap:
            info = origin / "gx_plugin.json"
            plugin = json.loads(info.read_bytes())
            plugin["extensions"].append({"id":"instrument-binding","contract":"goal_method","version":"1.0"})
            info.write_text(json.dumps(plugin))
            (origin / "plugin.py").write_text(
                'def register(ctx):\n    ctx.capabilities.add_manifest("method.json")\n'
                '    ctx.goal_methods.register("discharge-concurrent@1", bind=bind)\n'
                'def bind(ctx, cap, refs, params):\n'
                '    return {"record":ctx.publish_json("derived", "instrument_material", {"channels":3})}, params\n')
        plugins._install(origin, package_digest(origin), release / "plugins/installed", config.CONFIG_DIR / "workflow_packages.json")
        record = self.work / "record.json"
        record.write_text(json.dumps(sample))
        spec = {"research_effort":"quick", "research_modules":[],
                "capabilities":[CAP], "plugins":["org.gx.hydro-concurrent@1.0.0"],
                "input_files":[{"path":str(record),"role":"record"}]}
        path = self.work / "goal.json"
        path.write_text(json.dumps(spec))
        self.enterContext(patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)))
        log = io.StringIO()
        with redirect_stdout(log), redirect_stderr(log):
            status = cli.main(["research", "--state", str(self.work / "state"), "--run-key", "instrument",
                               "--goal", "Determine whether this archive's channels can be compared.", "--goal-config", str(path)])
        self.assertEqual(status, 0, log.getvalue())
        self.s = self.enterContext(CloseStudy(self.work, reopen=True))
        self.actor = self.s.next_actor("run")
        self.s.call(self.actor, "gx_query_catalog", {"kind":"all", "pi_method_scope":[]}, "interpret-goal")
        answer = self.s.call(self.actor, "gx_query_catalog", {"reference":CAP}, "exact")
        summary = self.s.call(self.actor, "gx_query_catalog", {"kind":"method","query":CAP}, "summary")
        self.assertEqual(answer["input_coverage"], summary["items"][0]["input_coverage"])
        return answer

    def check_node(self):
        return v6_jobs.validate_node(self.s.conn, self.s.run_id, {"capability": CAP, "inputs":self.s.materials,
            "depends_on":[], "parameters":{}, "node":"sensor", "branch":"measure"})

    def bind(self):
        return v6_jobs.bind_inputs(self.s.conn, self.s.run_id, CAP, registry.candidate(self.s.conn, CAP),
                                   self.s.materials, {}, writable=True)

    def test_fixed_provider_refuses_wrong_bytes_before_job(self):
        domain = {"schema":POLICY, "inputs":{"record":{"sha256":["a"*64]}}}
        card = self.setup_method({"calibration":"other"}, package_domain=domain)
        self.assertEqual(card["input_coverage"]["domain"]["status"], "unsupported")
        with self.assertRaises(SchemaInvalid):
            self.check_node()
        with self.assertRaises(SchemaInvalid):
            self.bind()
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_supported_properties_match_both_paths(self):
        doc = {"format":"hydrometry/1", "channel_count":2, "flags":[]}
        domain = {"schema":POLICY, "inputs":{"record":{"json_fields":{"format":{"one_of":["hydrometry/1"]},
                  "channel_count":{"min":1,"max":2}, "flags":{"subset_of":["audited"]}}}}}
        digest = hashlib.sha256(json.dumps(doc).encode()).hexdigest()
        card = self.setup_method(doc, domain, {"schema":POLICY, "inputs":{"record":{"sha256":[digest]}}})
        self.assertEqual(card["input_coverage"]["domain"]["status"], "supported")
        self.assertEqual(card["spec"]["method"]["applicable_domain"], domain)
        self.assertEqual(card["spec"]["outputs"][0]["nature"], "analysis")
        self.check_node()
        self.assertEqual(self.bind()[0], {"record":self.s.materials[0]})

    def test_missing_required_fact_stays_unknown(self):
        domain = {"schema":POLICY,"inputs":{"record":{"json_fields":{"calibration":{"one_of":["original"]}}}}}
        answer = self.setup_method({"format":"unknown"}, domain)
        self.assertEqual(answer["input_coverage"]["domain"]["status"], "unknown")
        with self.assertRaises(SchemaInvalid):
            self.check_node()
        with self.assertRaises(SchemaInvalid):
            self.bind()

    def test_old_undeclared_method_still_accepts_inputs(self):
        card = self.setup_method({"format":"original"})
        self.assertEqual(card["input_coverage"]["domain"]["status"], "unknown")
        self.check_node()
        self.assertTrue(self.bind()[0])


    def test_real_measurement_from_normal_goal(self):
        sample = json.loads((ROOT / "acceptance/science_plugin_blueprint/a1/positive/material.json").read_bytes())
        domain = {"schema":POLICY,"inputs":{"record":{"json_fields":{"schema":{"one_of":["gx.hydrometry/1"]}}}},
                  "randomness":{"mode":"deterministic"}}
        exact = self.setup_method(sample, domain)
        self.s.call(self.actor, "gx_query_catalog", {"kind":"all"}, "opening")
        start = branch("comparison")
        start["budget_limits"] = {"cpu_seconds":50, "job_submissions":1}
        node = self.s.node(self.actor, "measurement", self.s.materials[0], capability=CAP, branch_id="local:comparison")
        submitted = self.s.call(self.actor, "gx_decide_revision", {"decision":draft(start, node,
                       watches=[{"subject_ref":"local:measurement","event":"job_settled"}])}, "measure")
        row = self.s.settle(submitted["aliases"]["local:measurement"])
        result = ArtifactService(self.s.conn).read_json(row["result_ref"])
        self.assertEqual((row["status"], result["common_minutes"], result["difference_L"]), ("SUCCEEDED",85,4740))
        if dest := os.environ.get("GX_DOMAIN_RECEIPT"):
            Path(dest).write_text(json.dumps({
                    "ordinary_entry":"gx-p0 research, controlled native callbacks, local CPU worker",
                    "run_id":self.s.run_id, "input_ref":self.s.materials[0], "job_id":row["job_id"],
                    "result_ref":row["result_ref"], "material_sha256":result["input_sha256"],
                    "coverage":exact["input_coverage"], "method":CAP, "common_minutes":result["common_minutes"],
                    "difference_litres":result["difference_L"], "decision_ref":submitted["decision_ref"]
                }, indent=2) + "\n")

    def test_bad_declarations_and_pending_inputs(self):
        def contract(rule):
            return {"schema":POLICY, "inputs":{"record":rule}}
        for rule in ({"sha256":[]}, {"json_fields":{"length":{"min":4,"max":3}}},
                     {"json_fields":{"topology":{"one_of":[]}}}, {"media_types":False}):
            self.assertTrue(input_domain.problems(contract(rule), ["record"], []))
        declared = contract({"json_fields":{"chain_count":{"min":1,"max":3}}})
        self.assertEqual(input_domain.assess({"provider":declared}, {}, None)["status"], "unknown")
        input_domain.require({"provider":declared}, {}, None, resolved=False)
        with self.assertRaises(SchemaInvalid):
            input_domain.require({"provider":declared}, {}, None, resolved=True)


    def test_provider_derived_input_is_checked_after_materialization(self):
        domain = {"schema":POLICY,"inputs":{"record":{"json_fields":{"channels":{"max":2}}}}}
        answer = self.setup_method({"channels":2}, domain, remap=True)
        self.assertEqual(answer["input_coverage"]["domain"]["status"], "supported")
        # A read-only dry binding cannot publish the derived input. The actual binding checks its bytes.
        self.check_node()
        with self.assertRaises(SchemaInvalid) as refused:
            self.bind()
        self.assertIn("method.record.channels", refused.exception.details["excluded"])
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_catalog_listing_isolates_invalid_domain(self):
        # Break a package declaration in an isolated release at installation time.
        self.setup_method({"channels":2})
        release = config.HARNESS_ROOT
        package = self.work / "broken"
        shutil.copytree(ROOT / "examples/plugins/hydro-marginal", package)
        method = package / "method.json"
        doc = json.loads(method.read_bytes())
        doc["manifest"]["applicable_domain"] = {"schema":POLICY,"inputs":{"record":{"sha256":[]}}}
        method.write_text(json.dumps(doc))
        plugins._install(package, package_digest(package), release / "plugins/installed",
                         config.CONFIG_DIR / "workflow_packages.json")
        answer = self.s.call(self.actor, "gx_query_catalog", {"kind":"method","query":"discharge"}, "new")
        self.assertIn(CAP, [r["reference"] for r in answer["items"]])
        self.assertTrue(any("hydro-marginal" in text for text in answer["unavailable_installed_metadata"]))

    def test_waiting_node_checks_actual_upstream_named_output(self):
        from time import monotonic, sleep
        sample = json.loads((ROOT / "acceptance/science_plugin_blueprint/a1/positive/material.json").read_bytes())
        domain = {"schema":POLICY, "inputs":{"record":{"json_fields":{"schema":{"one_of":["gx.hydrometry/1"]}}}}}
        self.setup_method(sample, domain)
        self.s.call(self.actor, "gx_query_catalog", {"kind":"all"}, "opening")
        opened = branch("paired")
        opened["budget_limits"] = {"cpu_seconds":100, "job_submissions":2}
        first = self.s.node(self.actor, "first", capability=CAP, branch_id="local:paired")
        follow = self.s.node(self.actor, "follow", "local:first", capability=CAP, branch_id="local:paired")
        follow.update(depends_on=["local:first"], input_ports=[{"index":0, "output":"paired.json"}])
        submitted = self.s.call(self.actor, "gx_decide_revision", {"decision":draft(opened, first, follow,
                                watches=[{"event":"job_settled", "subject_ref":"local:follow"}])}, "pending-input")
        ids = submitted["aliases"]
        result = self.s.settle(ids["local:first"])
        deadline = monotonic() + 10
        while monotonic() < deadline:
            self.s.advance()
            row = self.s.conn.execute("SELECT status,job_id FROM v6_nodes WHERE node_id=?", (ids["local:follow"],)).fetchone()
            if row["status"] in v6_jobs.NODE_TERMINAL:
                break
            sleep(.03)
        self.assertEqual((result["status"], row["status"],row["job_id"]), ("SUCCEEDED","FAILED",None))
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
