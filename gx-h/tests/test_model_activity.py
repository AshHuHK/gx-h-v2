"""Real GX store and HTTP client, a loopback provider returns recorded model output."""
import json
import socket
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gx_core.artifacts.service import ArtifactService
from gx_core.errors import BudgetExceeded, IdempotencyConflict, SchemaInvalid
from gx_core.persistence import records, store
from gx_core.research import model_activity as activity
from gx_core.research.chat_provider import CAPABILITIES, validated_profile


class ChatHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        srv = self.server
        srv.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        srv.entered.set()
        if srv.mode == "disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if srv.mode == "wait":
            srv.release.wait(8)
        response = {"id": "recorded-http-1", "model": srv.model,
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(srv.answer)}}]}
        if srv.mode != "no_usage":
            response["usage"] = {"prompt_tokens": 83, "completion_tokens": 29}
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def log_message(self, *_):
        pass


class ModelActivityTests(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
        self.srv.entered = threading.Event()
        self.srv.release = threading.Event()
        self.srv.mode = "ok"
        self.srv.model = "secondary-recorded"
        self.srv.requests = []
        thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.release.set)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "gx.db"
        self.conn = store.open_store(self.db)
        self.addCleanup(self.conn.close)
        self.profile = {"provider": "local_chat", "model": self.srv.model, "revision": 1,
                        "endpoint": f"http://127.0.0.1:{self.srv.server_port}/v1/chat/completions",
                        "max_output_tokens": 128, "max_input_bytes": 4096, "timeout_seconds": 5, "max_requests": 1}
        self.run = records.create_run(self.conn, objective="Assess held-out squared error", project_ref="paired-fixture",
                                      actor="test", inputs={"inputs": {"execution_profiles": {"science_assessment": self.profile}}, "files": []})
        self.service = ArtifactService(self.conn)
        self.raw = self.service.put(run_id=self.run, kind="measurement", media_type="text/csv",
                                    content="method,mse\nbaseline,0.01\nvariant,0.19\n",
                                    origin="BACKEND", put_by="project-cpu").artifact_ref
        self.task, self.lease = self.assignment()
        self.srv.answer = {"evidence_refs": [self.raw], "findings": [{"path_id": "variant", "supported": False,
                           "reason": "Held-out MSE exceeds baseline."}]}

    def assignment(self):
        with store.transaction(self.conn):
            task = records.create_task(self.conn, actor="test", run_id=self.run, kind="review_evaluation",
                                       role="reviewer", required_artifacts=["science_assessment"])
            lease = store.issue_lease(self.conn, task_id=task, owner="review-worker", worker_role="reviewer",
                                      seconds=120, attempt=1)["lease_id"]
            self.conn.execute("UPDATE tasks SET lease_id=?, owner='review-worker',attempt=1,status='CLAIMED'"
                              " WHERE task_id=?", (lease, task))
        return task, lease

    def invoke(self, conn=None, **changes):
        args = {"task_id": self.task, "lease_id": self.lease, "output_kind": "science_assessment",
                "instruction": "Determine whether lower error supports the variant.", "input_refs": [self.raw], **changes}
        return activity.run_activity(conn or self.conn, **args)

    def test_receipt_reopens_original_and_budget(self):
        first = self.invoke()
        with closing(store.connect(self.db)) as reopened:
            self.assertEqual(first, self.invoke(reopened))
        self.assertEqual(first["status"], "completed")
        self.assertEqual(self.srv.answer, self.service.read_json(first["artifact_ref"]))
        context = self.service.read_json(first["context_ref"])
        self.assertEqual(context["originals"][0]["content"], "method,mse\nbaseline,0.01\nvariant,0.19\n")
        self.assertEqual(self.srv.requests[0]["max_tokens"], 128)
        self.assertEqual(first["usage"]["completion_tokens"], 29)
        self.assertIsNone(first["cost_usd"])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE task_id=?", (self.task,)).fetchone()[0], "CLAIMED")
        with self.assertRaises(IdempotencyConflict):
            self.invoke(instruction="Changed interpretation")
        other_task, other_lease = self.assignment()
        with self.assertRaises(BudgetExceeded):
            self.invoke(task_id=other_task, lease_id=other_lease)
        self.assertEqual(len(self.srv.requests), 1)

    def test_saved_answer_survives_reassignment(self):
        first = self.invoke()
        old_lease = self.lease
        with store.transaction(self.conn):
            store.release_lease(self.conn, old_lease)
            self.lease = store.issue_lease(self.conn, task_id=self.task, owner="replacement", worker_role="reviewer",
                                          seconds=120, attempt=2)["lease_id"]
            self.conn.execute("UPDATE tasks SET lease_id=?,attempt=2 WHERE task_id=?", (self.lease, self.task))
        self.assertEqual(self.invoke(lease_id=old_lease), first)
        reused = self.invoke()
        self.assertEqual(reused["reused_operation_id"], first["operation_id"])
        self.assertEqual(reused["artifact_ref"], first["artifact_ref"])
        self.assertEqual(len(self.srv.requests), 1)

    def test_response_model_mismatch_keeps_raw_unsubmitted(self):
        self.srv.model = "unselected-provider-model"
        result = self.invoke()
        self.assertEqual(result["status"], "model_mismatch")
        self.assertIsNone(result["artifact_ref"])
        self.assertEqual(self.service.read_json(result["response_ref"])["model"], self.srv.model)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_missing_usage_is_unknown(self):
        self.srv.mode = "no_usage"
        self.assertIsNone(self.invoke()["usage"])

    def test_lost_reply_blocks_duplicate_request_through_recovery(self):
        self.srv.mode = "disconnect"
        result = self.invoke()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(self.invoke()["status"], "outcome_unknown")
        self.assertEqual(len(self.srv.requests), 1)
        with store.transaction(self.conn):
            store.release_lease(self.conn, self.lease)
            self.lease = store.issue_lease(self.conn, task_id=self.task, owner="replacement", worker_role="reviewer",
                                          seconds=120, attempt=2)["lease_id"]
            self.conn.execute("UPDATE tasks SET lease_id=?,attempt=2 WHERE task_id=?", (self.lease, self.task))
        with self.assertRaisesRegex(SchemaInvalid, "unresolved"):
            self.invoke()
        self.assertEqual(len(self.srv.requests), 1)

    def test_cancel_and_parallel_observer_do_not_resubmit(self):
        self.srv.mode = "wait"
        result = {}
        def worker():
            with closing(store.connect(self.db)) as conn:
                result.update(self.invoke(conn))
        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(thread.join, 8)
        self.assertTrue(self.srv.entered.wait(5))
        self.assertEqual(self.invoke()["status"], "outcome_unknown")
        cancel = activity.cancel_activity(self.conn, self.task, self.lease)
        self.assertEqual(cancel["status"], "cancel_requested")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["status"], "cancelled_locally")
        self.assertIsNone(result["remote_stopped"])
        self.assertEqual(result, activity.activity_status(self.conn, self.task, self.lease))
        self.assertEqual(len(self.srv.requests), 1)
        self.assertIsNone(result["artifact_ref"])

    def test_legacy_and_capability_boundaries(self):
        self.conn.execute("UPDATE runs SET inputs_json='{}' WHERE run_id=?", (self.run,))
        with self.assertRaisesRegex(SchemaInvalid, "no configured"):
            self.invoke()
        with self.assertRaises(SchemaInvalid):
            validated_profile({**self.profile, "tools": ["search"]})
        with self.assertRaises(SchemaInvalid):
            validated_profile({**self.profile, "endpoint": "http://private/chat/completions"})
        self.assertFalse(CAPABILITIES["resume"])
        self.assertFalse(CAPABILITIES["confirm_remote_stop"])
        self.assertEqual(len(self.srv.requests), 0)


if __name__ == "__main__":
    unittest.main()
