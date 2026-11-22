"""A plugin supplies an ordinary numerical Job and canonical capability bundle."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core import budgets, config
from gx_core.capabilities import manifest, registry, shipped, storage
from gx_core.context import Ctx, Principal
from gx_core.domain import ids
from gx_core.errors import CapabilityNotApproved, ContentHashMismatch, SchemaInvalid, ScopeDenied
from gx_core.jobs.service import JobService
from gx_core.jobs.workflow import submit_workflow_job
from gx_core.p0 import compute, manifest as job_manifest
from gx_core.persistence import records, store
from gx_core.plugins.catalog import package_digest
from gx_core.plugins.loader import bootstrap


PLUGIN = "org.test.measurements"
VERSION = "1.0.0"
CAP = "finite-range@1"
REF = "capability-manifests/range.json"
WORKER = """import json
from pathlib import Path
import sys

points = json.loads(Path(sys.argv[1]).read_bytes())
(Path(sys.argv[2]) / "range.json").write_text(json.dumps({"range": max(points) - min(points)}))
"""
PARSER = """import json
from pathlib import Path

def validate_inputs(inputs, parameters):
    return [] if len(json.loads(Path(inputs["points"]).read_bytes())) >= 2 else ["expected two measurements"]

def parse(job):
    return {}
"""


class CapabilityAttachments(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.harness = self.work / "gx-h"
        installed = self.harness / "plugins/installed"
        self.directory = installed / PLUGIN / VERSION
        scripts = self.directory / "capability-manifests"
        scripts.mkdir(parents=True)
        # Subprocesses use the actual adapter host and the existing Job runner.
        for name in ("gx_core", "adapters"):
            (self.harness / name).symlink_to(config.HARNESS_ROOT / name, target_is_directory=True)
        self.enterContext(patch.object(manifest, "HARNESS_ROOT", self.harness))
        (self.directory / "gx_plugin.json").write_text(json.dumps({
            "id": PLUGIN, "version": VERSION, "api_version": "1.0", "kind": "bundle",
            "gx_compat": ">=2.1,<4", "entrypoint": "plugin:register", "dependencies": [],
            "extensions": [{"id": "numeric-range", "contract": "capability", "version": "1.0"}]}))
        (self.directory / "plugin.py").write_text(f"def register(ctx):\n    ctx.capabilities.add_manifest({REF!r})\n")
        (scripts / "worker.py").write_text(WORKER)
        (scripts / "parser.py").write_text(PARSER)
        worker = "capability-manifests/worker.py"
        parser = "capability-manifests/parser.py"
        prefix = f"plugins/installed/{PLUGIN}/{VERSION}/"
        method = {
            "capability_id": "finite-range", "version": "1", "kind": "UTILITY", "env_id": "control-plane-python",
            "entry": {"argv_template": ["{env.python}", "-I", "-B", "{entry_script}", "{in.points}", "{out.dir}"]},
            "weights": [], "required_data": [],
            "method_card": {"family": "numerical summary", "mechanism": "subtract minimum from maximum",
                            "objective": "measure the range", "estimator": "sample range", "training_data_lineage": "none"},
            "source": {"path": "{harness}/" + prefix + worker, "sha256": hashlib.sha256(WORKER.encode()).hexdigest()},
            "licence": "UNVERIFIED", "licence_basis": "test fixture", "limitations": "finite fixture points",
            "resources": {"cpu_threads": 1, "wall_seconds": 10},
            "outputs": [{"name": "range.json", "kind": "file:range", "required": True}],
        }
        (self.directory / REF).write_text(json.dumps({"manifest": method, "entry_script": worker, "parser": parser}))
        # Compute the operator's release pin independently of the registration path.
        self.digest = ids.sha256_of_obj({"manifest": method,
            "entry_script": {"path": prefix + worker, "sha256": hashlib.sha256(WORKER.encode()).hexdigest()},
            "parser": {"path": prefix + parser, "sha256": hashlib.sha256(PARSER.encode()).hexdigest()}})
        catalog = self.work / "installed.json"
        catalog.write_text(json.dumps({"plugins": [{"path": f"{PLUGIN}/{VERSION}", "sha256": package_digest(self.directory)}]}))
        self.plugin = bootstrap(installed, catalog, [(PLUGIN, VERSION)]).get(PLUGIN, VERSION)
        config_dir = self.work / "config"
        config_dir.mkdir()
        self.shipped_file = config_dir / "shipped.json"
        self.shipped_file.write_text(json.dumps({"capabilities": {}}))
        for file in config.CONFIG_DIR.glob("*.json"):
            if file.name != "shipped.json":
                (config_dir / file.name).symlink_to(file)
        self.enterContext(patch.object(config, "CONFIG_DIR", config_dir))

    def context(self, name):
        conn = store.open_store(self.work / name / "gx.db")
        self.addCleanup(conn.close)
        return Ctx(conn=conn, principal=Principal("operator", "operator"), tool="attach")

    def trust(self, digest):
        self.shipped_file.write_text(json.dumps({"capabilities": {CAP: digest}}))

    def run_with(self, ctx, receipts=None):
        budget = budgets.create_root(ctx.conn, run_id=None, actor=ctx.actor)
        inputs = {"plugin_binding": {"capabilities": receipts}} if receipts is not None else {}
        with store.transaction(ctx.conn) as tx:
            run = records.create_run(tx, objective="Measure the range", project_ref="range-test", actor=ctx.actor,
                                     budget_ref=budget, inputs=inputs)
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(tx, machine="run", subject_id=run, to_state=state, actor=ctx.actor)
        return run

    def test_approved_package_to_real_job_and_original_artifact(self):
        self.trust(self.digest)
        ctx = self.context("first")
        self.assertEqual(self.plugin.contributions.capabilities, (REF,))
        receipt = shipped.attach(ctx, REF, package=self.plugin)
        self.assertEqual(receipt["portable_sha256"], self.digest)
        self.assertEqual(registry.resolve(ctx.conn, CAP)["admission"], "SHIPPED")
        found, = registry.inspect(ctx.conn, capability_id="finite-range")["capabilities"]
        self.assertEqual(found["manifest_sha256"], receipt["manifest_sha256"])
        bundle = storage.artifact_service(ctx.conn).read_json(receipt["content_artifact_ref"])
        self.assertEqual(manifest.content_sha256(bundle), receipt["manifest_sha256"])
        self.assertEqual(bundle["entry_script"]["path"], f"plugins/installed/{PLUGIN}/{VERSION}/capability-manifests/worker.py")
        changes = ctx.conn.total_changes
        self.assertEqual(shipped.attach(ctx, REF, package=self.plugin), receipt)
        self.assertEqual(shipped.attach(ctx, CAP), receipt)
        self.assertEqual(ctx.conn.total_changes, changes)
        other = shipped.attach(self.context("second"), REF, package=self.plugin)
        self.assertEqual(other["portable_sha256"], receipt["portable_sha256"])
        self.assertNotEqual(other["manifest_sha256"], receipt["manifest_sha256"])

        rid = self.run_with(ctx, [receipt])
        compute._registered(ctx.conn, rid, CAP)
        artifacts = storage.artifact_service(ctx.conn)
        points = artifacts.publish_bytes(namespace=rid, rel_path="points.json", kind="job_input",
                                         data="[2,17,-4]", run_id=rid, media_type="application/json")
        request = dict(run_id=rid, operation_id="measure", family="range", capability=CAP,
                       inputs={"points": points.artifact_ref}, parameters={})
        job = submit_workflow_job(ctx.conn, **request)
        completed = JobService(ctx.conn).wait(job["job_id"], timeout_seconds=15)
        self.assertEqual(completed["status"], "SUCCEEDED", completed.get("failure"))
        output, = [r for r in completed["outputs"] if r["output_name"] == "range.json"]
        self.assertEqual(artifacts.read_json(output["artifact_ref"]), {"range": 21})
        (self.directory / "capability-manifests/worker.py").unlink()
        replay = submit_workflow_job(ctx.conn, **request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job_id"], job["job_id"])

    def test_sibling_and_different_method_pin_cannot_discover_or_launch(self):
        self.trust(self.digest)
        ctx = self.context("siblings")
        receipt = shipped.attach(ctx, REF, package=self.plugin)
        selected = self.run_with(ctx, [receipt])
        self.assertTrue(registry.inspect(ctx.conn, run_id=selected, capability_id="finite-range")["capabilities"])
        for sibling in (self.run_with(ctx), self.run_with(ctx, [{**receipt, "manifest_sha256": "0" * 64}])):
            self.assertFalse(registry.inspect(ctx.conn, run_id=sibling, capability_id="finite-range")["capabilities"])
            with self.assertRaisesRegex(ScopeDenied, "composition"):
                compute._registered(ctx.conn, sibling, CAP)
            job_manifest.check_capability(ctx.conn, sibling, CAP, review_probe=True)
            artifacts = storage.artifact_service(ctx.conn)
            points = artifacts.publish_bytes(namespace=sibling, rel_path="points.json", kind="job_input",
                                             data="[1,3]", run_id=sibling, media_type="application/json")
            with self.assertRaisesRegex(SchemaInvalid, "composition"):
                submit_workflow_job(ctx.conn, run_id=sibling, operation_id="measure", family="range",
                                    capability=CAP, inputs={"points": points.artifact_ref}, parameters={})
        self.assertEqual(ctx.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_unreviewed_candidate_and_old_governance_decision_are_preserved(self):
        ctx = self.context("draft")
        receipt = shipped.attach(ctx, REF, package=self.plugin)
        rid = self.run_with(ctx, [receipt])
        for trusted in (False, True):
            if trusted:
                self.trust(self.digest)
            self.assertEqual(shipped.attach(ctx, REF, package=self.plugin), receipt)
            self.assertEqual(registry.asset_row(ctx.conn, "finite-range", "1")["status"], "DRAFT")
            with self.assertRaises(CapabilityNotApproved):
                registry.resolve(ctx.conn, CAP)
            with self.assertRaises(ScopeDenied):
                compute._registered(ctx.conn, rid, CAP)

    def test_mismatched_release_pin_and_changed_script_cannot_register(self):
        ctx = self.context("mismatch")
        self.trust("e" * 64)
        with self.assertRaises(ContentHashMismatch):
            shipped.attach(ctx, REF, package=self.plugin)
        self.assertIsNone(registry.asset_row(ctx.conn, "finite-range", "1"))
        self.trust(self.digest)
        (self.directory / "capability-manifests/worker.py").write_text("# new worker bytes\n")
        with self.assertRaises(ContentHashMismatch):
            shipped.attach(ctx, REF, package=self.plugin)
        self.assertIsNone(registry.asset_row(ctx.conn, "finite-range", "1"))
