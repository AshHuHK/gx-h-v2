"""Public configuration, project pinning, and rejection before inference."""

from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gx_core.errors import ScopeDenied
from gx_core.hostobs import host_policy, policy
from gx_core.p0 import deployment, profiles, schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


class ModelProfiles(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.profile = self.home / "roles.json"
        self.profile.write_text(json.dumps({
            "default": {"provider": "openai", "model": "project-primary"},
            "task_classes": {"VERIFICATION": {"provider": "analysis_service", "model": "analysis-model"}},
            "roles": {"evidence_reviewer": {"tools": ["gx_read_artifact", "gx_submit_product"]}},
        }))
        self.codex_config = self.home / "config.toml"
        self.config_text = ('[model_providers.analysis_service]\nname="Analysis"\n'
                            'base_url="https://responses.example.invalid/v1"\nwire_api="responses"\n')
        self.codex_config.write_text(self.config_text)
        env = patch.dict(os.environ, {"CODEX_HOME":temp.name, "GX_P0_PROFILE":str(self.profile),
                                      "GX_P0_TEST":"0", "GX_P0_TEST_PROVIDER":"", "GX_P0_PROVIDER":"openai"})
        env.start()
        self.addCleanup(env.stop)

    def test_role_class_and_task_requirements_survive_configuration_change(self):
        doc, digest, host = host_policy.p0_host()
        saved = {"agent_policy_json": json.dumps(doc), "agent_policy_sha256": digest, "host_json": json.dumps(host)}
        self.profile.unlink()
        with patch.dict(os.environ, {"GX_P0_MODEL":"a-different-deployment", "GX_P0_PROVIDER":"elsewhere"}):
            route = host_policy.p0_role_for_run(saved, "evidence_reviewer")
            self.assertEqual(route, {"provider":"analysis_service", "model":"analysis-model", "effort":"max",
                                     "tools":["gx_read_artifact", "gx_submit_product"]})
            self.assertEqual(host_policy.p0_provider_for_run(saved), "openai")
            requirement = policy.requirement_of_task({"role":"evidence_reviewer"}, doc)
            self.assertEqual((requirement["model"], requirement["allowed_efforts"]), ("analysis-model", ["max"]))
            self.assertEqual(host_policy.p0_role_for_run(saved, "main")["effort"], "ultra")
            self.assertEqual(host_policy.p0_role_for_run(saved, "executor")["effort"], "xhigh")
            self.assertEqual(host_policy.p0_role_for_run(saved, "planner")["effort"], "ultra")
        self.assertNotIn("https://", json.dumps([doc,host]))

    def test_unconfigured_or_chat_only_provider_and_missing_credential_do_not_launch(self):
        for config, error in [
            ("", "Configure model_providers.analysis_service"),
            (self.config_text.replace('"responses"','"chat"'), "requires wire_api=responses"),
            (self.config_text+'env_key="GX_P0_TEST_UNSET_CREDENTIAL"\n', "credential environment variable"),
        ]:
            with self.subTest(condition=error), patch.object(deployment, "AppServerClient") as client:
                self.codex_config.write_text(config)
                with redirect_stdout(StringIO()) as out:
                    self.assertEqual(deployment._run(Namespace(command="doctor", smoke=True)), 1)
                report = json.loads(out.getvalue())
                self.assertFalse(report["configured"])
                self.assertEqual(report["inference"], "not_run")
                self.assertIn(error, report["providers"]["analysis_service"]["error"])
                client.assert_not_called()

    def test_unsupported_effort_or_tool_fails_before_dispatch(self):
        self.profile.write_text(json.dumps({"roles":{"main":{"effort":" "}}}))
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            host_policy.p0_policy()
        with self.assertRaisesRegex(ValueError, "gx_unavailable"):
            profiles.selected_tools([{"name":"gx_submit_product"}], ["gx_unavailable"])
        catalog = {"analysis-model": ["xhigh"]}
        with self.assertRaisesRegex(ValueError, "analysis-model with max"):
            profiles.require_catalog_entry(catalog, "analysis_service", "analysis-model", "max")
        with patch.dict(os.environ, {"GX_P0_TEST":"1"}):
            with self.assertRaisesRegex(ValueError, "loopback base_url"):
                profiles.provider_endpoint("analysis_service", profiles.codex_config(), test_mode=True)

    def test_selected_efforts_come_from_the_model_catalog_and_survive_reopen(self):
        self.profile.write_text(json.dumps({"roles":{"main":{"effort":"low"}},
             "task_classes":{"VERIFICATION":{"provider":"analysis_service","model":"analysis-model","effort":"balanced"}}}))
        doc, digest, host = host_policy.p0_host()
        saved = {"agent_policy_json":json.dumps(doc),"agent_policy_sha256":digest,"host_json":json.dumps(host)}
        self.assertTrue(doc["p0_check_catalog"])
        self.assertEqual(doc["p0_role_profiles"]["main"]["effort"], "low")
        self.profile.unlink()
        route = host_policy.p0_role_for_run(saved, "evidence_reviewer")
        self.assertEqual(route["effort"], "balanced")
        self.assertEqual(policy.requirement_of_task({"role":"evidence_reviewer"}, doc)["allowed_efforts"], ["balanced"])
        self.assertEqual(policy.requirement_of_task({"task_class":"VERIFICATION"}, doc)["allowed_efforts"], ["balanced"])
        profiles.require_catalog_entry({"analysis-model":["balanced","low"]}, "analysis_service", "analysis-model", "balanced")
        with self.assertRaisesRegex(ValueError, "analysis-model with balanced"):
            profiles.require_catalog_entry({"analysis-model":["low"]}, "analysis_service", "analysis-model", "balanced")

    def test_offline_profiles_and_catalog_pagination_use_only_selected_providers(self):
        with patch.object(deployment, "AppServerClient") as host, redirect_stdout(StringIO()) as out:
            self.assertEqual(deployment._run(Namespace(command="profiles")), 0)
        self.assertEqual(set(json.loads(out.getvalue())["providers"]), {"openai", "analysis_service"})
        host.assert_not_called()
        client = Mock()
        client.request.side_effect = [
            {"data":[{"model":"hidden", "hidden":True}], "nextCursor":"page-2"},
            {"data":[{"model":"analysis-model", "supportedReasoningEfforts":[{"reasoningEffort":"max"}]}]},
        ]
        self.assertEqual(profiles.model_catalog(client, "analysis_service"), {"analysis-model":["max"]})
        self.assertEqual(client.request.call_args_list[-1].args[1]["modelProvider"], "analysis_service")
        self.assertEqual(client.request.call_args_list[-1].args[1]["cursor"], "page-2")

    def test_advertised_tools_and_callback_admission_use_the_same_saved_route(self):
        self.profile.write_text(json.dumps({"roles":{"main":{"tools":["gx_project_state"]}}}))
        doc, digest, host = host_policy.p0_host()
        conn = store.open_store(self.home / "gx.db")
        self.addCleanup(conn.close)
        schema.install(conn)
        rid = records.create_run(conn, objective="Check native route", project_ref="profiles", actor="test",
                                 agent_policy=doc, host=host, pins={"agent_policy_sha256": digest})
        read = Mock(return_value={"saved":True})
        roots = Roots(conn, run_id=rid, cwd=self.home, main_call=read,
                      main_tools=[{"name":"gx_project_state"},{"name":"gx_propose_intent"}])
        with patch("gx_core.p0.roots.tempfile.gettempdir", return_value=self.home):
            row = roots._new_row()
        self.assertEqual(roots._settings(row,new=True)["dynamicTools"], [{"name":"gx_project_state"}])
        roots._bind(row, {"id":"test-thread", "cwd":row["cwd"]})
        roots.active_turns["test-thread"] = "turn"
        call = {"threadId":"test-thread", "turnId":"turn", "callId":"1", "arguments":{}}
        with self.assertRaisesRegex(ScopeDenied, "does not expose"):
            roots._call({**call, "tool":"gx_propose_intent"})
        read.assert_not_called()
        self.assertEqual(roots._call({**call, "tool":"gx_project_state"}), {"saved":True})
        wrong = {"model":"other-model", "modelProvider":row["provider"], "reasoningEffort":row["effort"],
                 "approvalPolicy":"never", "sandbox":{"type":"readOnly", "networkAccess":False}}
        with self.assertRaisesRegex(ScopeDenied, "effective model"):
            roots._verify_settings(row, wrong)
        recorded = json.loads(roots._row(row["root_key"])["effective_profile_json"])
        self.assertEqual(recorded["model"], "other-model")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM p0_root_deliveries").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
