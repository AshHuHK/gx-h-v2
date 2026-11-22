"""End-to-end science fixture with a loopback HTTP peer that answers only from its handoff."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from examples.science_judgment import judge
from gx_core.artifacts.service import ArtifactService
from gx_core.persistence import store


class RecordedAssessment(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        context = json.loads(request["messages"][1]["content"])
        if not self.server.requests:
            self.server.requests.append({"http_status": 429, "context": context})
            self.send_error(429)
            return
        originals = {doc["artifact_ref"]: doc["content"] for doc in context["originals"]}

        class Handoff:
            @staticmethod
            def read_json(ref):
                return json.loads(originals[ref])

        result = judge(Handoff(), context["task_input"])
        self.server.requests.append({"model": request["model"], "context": context, "reply": result})
        answer = {"id": f"http-fixture-{len(self.server.requests)}", "model": request["model"],
                  "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class ScienceActivityCycle(unittest.TestCase):
    def test_assessment_crosses_http_and_decision_uses_saved_evidence(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RecordedAssessment)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        server.requests = []
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile_file = root / "profiles.json"
            profile_file.write_text(json.dumps({"science_assessment": {
                "provider": "local_chat", "model": "recorded-assessor", "revision": 1,
                "endpoint": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "max_requests": 3, "max_output_tokens": 1500, "max_input_bytes": 64000,
                "timeout_seconds": 12}}))
            def step(action, *args):
                proc = subprocess.run(["bin/gx-science", action, "--state", temp, *args],
                                      text=True, capture_output=True, timeout=90)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)
            started = step("start", "--execution-profiles", str(profile_file))
            done = step("resume")
            self.assertEqual(done["status"], "COMPLETED", done)
            self.assertEqual(len(done["rounds"]), 2, done)
            self.assertEqual(len(server.requests), 3)
            self.assertEqual([t["status"] for t in done["tasks"]], ["ACCEPTED"] * 4)
            self.assertEqual(server.requests[0]["context"], server.requests[1]["context"])
            self.assertEqual(len(done["jobs"]), 3)
            self.assertEqual(done["jobs"], step("resume")["jobs"])
            self.assertEqual(len(server.requests), 3)
            with closing(store.connect(root / "offline_native/gx.db")) as conn:
                artifacts = ArtifactService(conn)
                receipts = [json.loads(r["response_json"]) for r in conn.execute(
                    "SELECT response_json FROM idempotency WHERE scope='research.model_activity' ORDER BY rowid")]
                for result in receipts:
                    self.assertIsNone(result["usage"])
                    self.assertEqual(result["observed_model"], "recorded-assessor")
                    self.assertTrue(artifacts.verify(result["response_ref"])["ok"])
                decisions = [artifacts.read_json(r["artifact_ref"]) for r in done["rounds"]]
            self.assertEqual(len(decisions[0]["continue_paths"]), 1)
            self.assertFalse(decisions[1]["continue_paths"])
            if directory := os.getenv("GX_P08_TEST_EVIDENCE"):
                Path(directory).write_text(json.dumps({"mode": "integration_recorded",
                    "backend": "loopback HTTP recorded assessor; scripted native fixture decision worker",
                    "start": started, "resumed": done, "http_exchanges": server.requests,
                    "receipts": receipts, "decisions": decisions}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
