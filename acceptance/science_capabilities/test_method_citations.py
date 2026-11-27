"""A native public-method reference supports a Lead decision about procedure."""
from contextlib import redirect_stdout
from io import StringIO
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a import interleavings_fixture as fx
from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.errors import ScopeDenied
from gx_core.p0 import cli, v621_metrics, v621_policy, v6_decisions
from gx_core.plugins.catalog import package_digest
from gx_core.plugins.cli import _install


class MethodCitations(unittest.TestCase):
    def test_exact_published_card_supports_its_own_lead_decision_only(self):
        study = self.enterContext(fx.Scenario(Path(self.enterContext(tempfile.TemporaryDirectory())),
            spec_updates={"research_effort": "deep", "research_modules": [], "evidence_policy": v621_policy.VERSION}))
        actor = study.lead
        task = dict(study.conn.execute("SELECT * FROM v6_decision_tasks WHERE task_id=?", (actor.task_id,)).fetchone())
        card = study.call(actor, "gx_query_catalog", {"kind":"method", "reference":"fixture-grid@1"}, "method-card")
        self.assertFalse(card["execution_granted"])
        ref = card["source_ref"]
        decision = fx.draft(evidence=[ref])

        def visible(*, record=task, lease=actor.lease_id, thread=actor.thread, turn=actor.turn, draft=decision):
            return v6_decisions._visible(study.conn, record, lease, thread, turn, draft)

        for identity in ({"turn":"other-turn"}, {"thread":"other-thread"}, {"lease":"other-lease"},
                         {"record":dict(task, task_id="other-task")}, {"record":dict(task, run_id="other-run")},
                         {"record":dict(task, scope="module:elsewhere")}):
            with self.subTest(identity=next(iter(identity))), self.assertRaises(ScopeDenied):
                visible(**identity)
        with self.assertRaises(ScopeDenied):
            visible(draft=fx.draft({"op":"close_run","claim_refs":[ref]}, evidence=[ref]))
        self.assertEqual(v621_metrics.from_output(study.conn, study.run_id, ref), [])
        source = ArtifactService(study.conn).path_of(ref)
        original = source.read_bytes()
        source.write_bytes(original + b" ")
        with self.assertRaises(ScopeDenied):
            visible()
        source.write_bytes(original)
        study.conn.execute("SAVEPOINT changed_receipt")
        try:
            study.conn.execute("""UPDATE p0_root_calls SET result_json=json_set(result_json,'$.spec.method.estimator',?)
                               WHERE thread_id=? AND call_id='method-card'""", ("different method", actor.thread))
            with self.assertRaises(ScopeDenied):
                visible()
        finally:
            study.conn.execute("ROLLBACK TO changed_receipt")
            study.conn.execute("RELEASE changed_receipt")
        study.conn.execute("SAVEPOINT catalog_list")
        try:
            study.conn.execute("""UPDATE p0_v6_native_calls
                               SET request_json=json_remove(request_json,'$.arguments.reference')
                               WHERE thread_id=? AND call_id='method-card'""", (actor.thread,))
            with self.assertRaises(ScopeDenied):
                visible()
        finally:
            study.conn.execute("ROLLBACK TO catalog_list")
            study.conn.execute("RELEASE catalog_list")
        self.assertNotIn(ref, visible())   # Scientific graph inputs keep their original frozen scope.
        self.assertNotIn(ref, json.loads(task["visible_refs_json"]))
        result = study.call(actor, "gx_decide_revision", {"decision":decision}, "cite-method")
        self.assertEqual(result["outcome"], "maintained")
        self.assertFalse(study.rows("SELECT job_id FROM jobs"))

    def test_public_method_workers_cite_only_their_own_delivery(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        deployment = work / "release"
        shutil.copytree(config.CONFIG_DIR, deployment / "config")
        for name in ("gx_core", "adapters"):
            (deployment / name).symlink_to(config.HARNESS_ROOT / name, target_is_directory=True)
        catalog = deployment / "config/workflow_packages.json"
        catalog.write_text('{"plugins":[],"examples":{}}')
        # This plugin grants both ordinary tools. The shipped read-only survey intentionally omits decide.
        package = work / "package"
        package.mkdir()
        (package / "gx_plugin.json").write_text(json.dumps({"id":"org.example.methods","version":"1.0.0",
            "api_version":"1.0","kind":"bundle","entrypoint":"plugin:register","gx_compat":">=3.0,<4.0",
            "dependencies":[],"extensions":[{"id":"methods","contract":"research_module","version":"1"}]}))
        (package / "plugin.py").write_text(
            "def register(ctx):\n"
            "    ctx.research_modules.register(module_id='methods',version='1',purpose='Compare methods',"
            "output='module_report_bundle',cost_class='local_read',focuses=['model'],"
            "instructions='Decide whether the published method is applicable.',capabilities=[],"
            "input_roles=[],catalog_access='public_methods',"
            "tools=['gx_query_catalog','gx_decide_revision','gx_submit_module_report'])\n")
        _install(package, package_digest(package), deployment / "plugins/installed", catalog)
        self.enterContext(patch.object(config, "CONFIG_DIR", deployment / "config"))
        self.enterContext(patch.object(config, "HARNESS_ROOT", deployment))
        settings = work / "goal.json"
        settings.write_text(json.dumps({"project":"method-citation","research_effort":"deep","capabilities":[],
            "input_files":[],"research_modules":["methods@1"], "evidence_policy":v621_policy.VERSION}))
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main(["start", "--state",str(work / "study/state"),"--run-key","method-citation",
                             "--goal","Can a delegated researcher cite the exact method inspected?",
                             "--goal-config",str(settings)])
        self.assertEqual(code, 0, output.getvalue())
        s = self.enterContext(fx.Scenario(work / "study", reopen=True))
        lead = s.next_actor("run")
        def open_worker(key):
            args = dict(s.open_args(key, inputs=[], caps=[]), module_id="methods", focus="model")
            return s.bind(s.call(lead, "gx_open_module", args, key)["task_id"])
        first = open_worker("first")
        other = open_worker("other")
        query = {"kind":"method", "reference":"fixture-grid@1"}
        card = s.call(first, "gx_query_catalog", query, "exact")
        self.assertEqual(card, s.call(first, "gx_query_catalog", query, "same-result"))
        ref = card["source_ref"]
        decision = {"decision":fx.draft(evidence=[ref])}
        with self.assertRaises(ScopeDenied) as denied:
            s.call(other, "gx_decide_revision", decision, "borrowed-citation")
        self.assertEqual(denied.exception.details["field"], "evidence_refs[0]")
        task = dict(s.conn.execute("SELECT * FROM v6_decision_tasks WHERE task_id=?", (first.task_id,)).fetchone())
        for fake in (other.scope, "run"):
            with self.assertRaises(ScopeDenied):
                v6_decisions._visible(s.conn, dict(task,scope=fake), first.lease_id, first.thread, first.turn,
                                      decision["decision"])
        # A transport record alone cannot replace the module owner's typed return and artifact pin.
        s.conn.execute("SAVEPOINT changed_response")
        try:
            s.conn.execute("""UPDATE p0_root_calls SET result_json=json_set(result_json,'$.altered',1)
                WHERE thread_id=? AND call_id IN ('exact','same-result')""", (first.thread,))
            with self.assertRaises(ScopeDenied):
                v6_decisions._visible(s.conn,task,first.lease_id,first.thread,first.turn,decision["decision"])
        finally:
            s.conn.execute("ROLLBACK TO changed_response")
            s.conn.execute("RELEASE changed_response")
        self.assertNotIn(ref, json.loads(task["visible_refs_json"]))
        adopted = s.call(first, "gx_decide_revision", decision, "own-citation")
        self.assertEqual(adopted["outcome"], "maintained")
        self.assertEqual(card, s.call(other, "gx_query_catalog", query, "own-query"))
        self.assertEqual(s.call(other, "gx_decide_revision", decision, "own-citation")["outcome"], "maintained")
        self.assertFalse(s.rows("SELECT job_id FROM jobs"))
