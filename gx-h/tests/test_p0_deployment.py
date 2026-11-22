"""Regression checks for the pinned OAuth launcher and role effort selection."""

from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gx_core.p0 import deployment, profiles


def profile_environment(**values):
    # All settings, including bypass-list examples, stay out of the process environment.
    return patch.object(profiles, "os", SimpleNamespace(environ=values))


class DeploymentTests(unittest.TestCase):
    def test_roles_and_task_classes(self):
        expected = {
            "main": "ultra", "role.coordinator": "ultra", "team-lead": "ultra",
            "PLANNING": "max", "REASONING": "max", "coding": "max", "adapter_author": "max",
            "evidence_reviewer": "max", "VERIFICATION": "max", "simple": "xhigh",
            "EXECUTION": "xhigh", "TESTING": "xhigh",
        }
        with profile_environment():
            for role, effort in expected.items():
                self.assertEqual(profiles.role_profile(role), {"effort": effort, "model": "gpt-6.2-astra"})
            self.assertEqual(profiles.provider_name(), "openai")
        with profile_environment(GX_P0_TEST="1", GX_P0_TEST_PROVIDER="local", GX_P0_MODEL="fixture"):
            for role in expected:
                self.assertEqual(profiles.role_profile(role), {"model": "fixture", "effort": "xhigh"})
            argv = profiles.app_server_argv()
            self.assertIn('model_provider="local"', argv)
            self.assertIn('agents.default_subagent_reasoning_effort="xhigh"', argv)
            self.assertTrue(any("expose_spawn_agent_model_overrides=false" in flag for flag in argv))
        with profile_environment(GX_P0_TEST_PROVIDER="local"):
            with self.assertRaisesRegex(ValueError, "GX_P0_TEST=1"):
                profiles.codex_argv()
        with profile_environment(GX_P0_TEST="1", GX_P0_TEST_PROVIDER="local"):
            argv = profiles.app_server_argv(test_mode=False, provider="openai")
            self.assertNotIn('model_reasoning_effort="xhigh"', argv)
            self.assertIn('model_provider="openai"', argv)
            self.assertEqual(profiles.effort_for("main", test_mode=False), "ultra")
        with profile_environment():
            self.assertIn('model_provider="local"', profiles.app_server_argv(test_mode=True, provider="local"))

    def test_proxy_settings_preserve_restrictions_and_fail_closed(self):
        with profile_environment(NO_PROXY="localhost,example.invalid", no_proxy="localhost,127.0.0.1"):
            env = profiles.proxy_settings()
            self.assertEqual(env["NO_PROXY"], "localhost")
            self.assertEqual(env["no_proxy"], env["NO_PROXY"])
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                self.assertEqual(env[key], "http://127.0.0.1:7890")
            for key in ("ALL_PROXY", "all_proxy"):
                self.assertEqual(env[key], "socks5h://127.0.0.1:7891")
        with patch.object(profiles.socket, "create_connection", side_effect=ConnectionRefusedError) as connect:
            with self.assertRaisesRegex(ConnectionError, "127.0.0.1:7890"):
                profiles.clash_env()
            connect.assert_called_once_with(("127.0.0.1", 7890), timeout=2)

    def test_status_reports_only_predefined_modes(self):
        for output, rc, expected in [
            ("Logged in using ChatGPT", 0, "chatgpt"),
            ("API key configured - [masked fixture]", 0, "other_auth"),
            ("Not logged in", 1, "signed_out"),
            ("A storage error with private details", 1, "unavailable"),
        ]:
            with self.subTest(state=expected), patch.object(deployment.subprocess, "run") as invoked:
                invoked.return_value = SimpleNamespace(stdout="", stderr=output, returncode=rc)
                self.assertEqual(deployment._login_state({}), expected)
                self.assertEqual(invoked.call_args.args[0][-2:], ["login", "status"])

    def test_existing_oauth_login_reuses_authorization(self):
        with profile_environment(), patch.object(deployment, "clash_env", return_value={}), \
             patch.object(deployment, "_version", return_value=profiles.CODEX_VERSION), \
             patch.object(deployment, "_login_state", return_value="chatgpt"), \
             patch.object(deployment.subprocess, "run") as invoked, redirect_stdout(StringIO()) as out:
            self.assertEqual(deployment._login(Namespace(command="login")), 0)
            self.assertEqual(json.loads(out.getvalue()), {"chatgpt_oauth": True, "login": "chatgpt"})
            invoked.assert_not_called()

    def test_account_or_model_mismatch_stops_before_inference(self):
        env = {"HTTP_PROXY": "http://127.0.0.1:7890", "ALL_PROXY": "socks5h://127.0.0.1:7891",
               "NO_PROXY": "localhost"}
        for account, models in [("apiKey", []), ("chatgpt", ["xhigh"])]:
            with self.subTest(account=account), profile_environment(), \
                 patch.object(deployment, "clash_env", return_value=env), \
                 patch.object(deployment, "_version", return_value=profiles.CODEX_VERSION), \
                 patch.object(deployment, "AppServerClient") as factory, redirect_stdout(StringIO()) as out:
                client = factory.return_value.__enter__.return_value
                rows = [{"model": profiles.DEFAULT_MODEL,
                         "supportedReasoningEfforts": [{"reasoningEffort": v} for v in models]}]
                client.request.side_effect = [{}, {"account": {"type": account}}, {"data": rows}]
                self.assertEqual(deployment._doctor(Namespace(smoke=True)), 1)
                self.assertFalse(json.loads(out.getvalue())["ready"])
                self.assertNotIn("turn/start", [call.args[0] for call in client.request.call_args_list])

    def test_smoke_checks_selected_thread_turn_and_final_message(self):
        client = Mock()
        client.request.side_effect = [
            {"thread": {"id": "t"}, "reasoningEffort": "xhigh", "model": "model", "modelProvider": "openai"},
            {"turn": {"id": "r"}},
        ]
        def receive(predicate, *, timeout):
            for thread, turn, text in [("another", "r", "GX_P0_OAUTH_OK"), ("t", "r", "GX_P0_OAUTH_OK"),
                                       ("t", "r", "different final text")]:
                message = {"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
                           "item": {"type": "agentMessage", "text": text}}}
                self.assertFalse(predicate(SimpleNamespace(message=message)))
            self.assertTrue(predicate(SimpleNamespace(message={"method": "turn/completed", "params": {
                "threadId": "t", "turn": {"id": "r", "status": "completed"}}})))
        client.run_until.side_effect = receive
        result = deployment._smoke(client, "/tmp", "openai", "model")
        self.assertFalse(result["matched"])
        self.assertEqual(client.request.call_args_list[1].args[1]["effort"], "xhigh")


if __name__ == "__main__":
    unittest.main()
