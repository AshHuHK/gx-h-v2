"""Real DSH process and GX scientific workflow using a recorded local Messages model."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from examples.science_judgment import judge
from gx_core.artifacts import ArtifactService
from gx_core.persistence import store
from gx_core.research import dsh_activity
from tests import test_model_activity
from tests.test_dsh_runtime import recorded_model

ROOT = Path(__file__).resolve().parents[1]


def reply(body):
    prompt = next(block["text"] for m in body["messages"] for block in m["content"]
                  if block["type"] == "text" and '"output_token_budget"' in block["text"])
    context = json.loads(prompt.split("\n", 1)[1])
    class Evidence:
        def read_json(self, ref):
            return json.loads(next(a["content"] for a in context["originals"] if a["artifact_ref"] == ref))
    return json.dumps(judge(Evidence(), context["task_input"]))


@unittest.skipUnless((dsh_activity.RUNTIME / ".venv/bin/python").is_file(), "install runtime/dsh-sdk/.venv first")
class DSHActivityTest(unittest.TestCase):
    def test_gx_run_uses_sdk_model_then_returns_to_native_decision(self):
        with tempfile.TemporaryDirectory() as temp, recorded_model(respond=reply) as (url, requests, _):
            directory = Path(temp)
            config = {"runtime":"dsh_sdk", "provider":"local_messages", "endpoint":url + "/v1/messages",
                      "model":"deepseek-v4-flash", "revision":1, "max_requests":2, "timeout_seconds":40,
                      "max_output_tokens":900, "max_input_bytes":40000}
            profiles = directory / "profiles.json"
            profiles.write_text(json.dumps({"science_assessment":config}))
            command = [str(ROOT / "bin/gx-science"), "run", "--state", temp, "--execution-profiles", str(profiles)]
            run = json.loads(subprocess.check_output(command, cwd=ROOT, text=True))
            self.assertEqual(run["status"], "COMPLETED")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all("tools" not in r and r["max_tokens"] == 900 for r in requests))
            conn = store.open_store(directory / "offline_native/gx.db")
            self.addCleanup(conn.close)
            docs = ArtifactService(conn)
            reports = []
            for row in conn.execute("SELECT payload_json FROM events WHERE kind='model_activity.completed'"):
                receipt = json.loads(row[0])
                self.assertEqual(receipt["observed_model"], config["model"])
                self.assertTrue(receipt["session_id"].startswith("gx-"))
                self.assertEqual(receipt["usage"]["output_tokens"], 10)
                self.assertEqual(docs.describe(receipt["artifact_ref"])["kind"], "science_assessment")
                reports.append(receipt)
            self.assertEqual(len(reports),2)
            second = json.loads(subprocess.check_output(command, cwd=ROOT, text=True))
            self.assertEqual(second["report_ref"], run["report_ref"])
            self.assertEqual(len(requests), 2)
            if target := os.environ.get("GX_DSH_GX_EVIDENCE"):
                Path(target).write_text(json.dumps({"execution_mode":"real_sdk_recorded_local_messages",
                    "final":run, "provider_requests":len(requests), "resumed_new_requests":0, "receipts":reports},
                    indent=2) + "\n")

    def test_reported_model_mismatch_keeps_response_and_refuses_delivery(self):
        # Use the same Gx task/acquired lease/original as the direct provider tests.
        fixture = test_model_activity.ModelActivityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with recorded_model(replies=['{"result":"wrong model"}'], response_model="unselected-model") as (url, calls, _):
            profile = {**fixture.profile, "runtime":"dsh_sdk", "model":"deepseek-v4-flash", "provider":"local_messages",
                       "endpoint":url + "/v1/messages", "timeout_seconds": 30}
            fixture.conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (
                json.dumps({"execution_profiles":{"science_assessment":profile}}), fixture.run))
            result = fixture.invoke()
            self.assertEqual(result["status"], "model_mismatch")
            self.assertEqual(result["observed_model"], "unselected-model")
            self.assertEqual(result["provider_request_id"], "msg-1")
            self.assertIsNone(result["artifact_ref"])
            self.assertEqual(fixture.invoke(), result)
            self.assertEqual(len(calls), 1)
