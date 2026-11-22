"""Exercise the loopback API, project inbox and resumable milestone stream."""

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gx_core import project_messages
from gx_core.web import projects
from gx_core.web.http import BASE, make_server


class WebBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name).resolve()
        self.project = "demo"
        self.route = BASE + "/" + self.project
        self.prompt = self.state / "question.md"
        self.prompt.write_text("# Demo\nRead the two rows.\n")
        self.server = make_server(self.state, self.project, 0, prompt=self.prompt)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
        return conn.getresponse()

    def test_snapshot_and_page_before_start(self):
        self.assertEqual(self.request("GET", BASE).status, 200)
        page = self.request("GET", "/")
        self.assertIn(b"Milestones", page.read())
        snapshot = self.request("GET", self.route)
        self.assertEqual(snapshot.status, 200)
        self.assertEqual(json.loads(snapshot.read())["status"], "NEEDS_PROMPT")
        self.assertEqual(self.request("GET", BASE + "/another").status, 404)

    def test_launch_uses_cli_and_pinned_prompt(self):
        with patch("gx_core.web.projects.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b'{"status":"BACKGROUND_STARTED"}'
            receipt = self.request("POST", self.route + "/messages", {"kind": "launch"})
            self.assertEqual(receipt.status, 202)
            self.assertEqual(json.loads(receipt.read())["status"], "BACKGROUND_STARTED")
            argv = run.call_args.args[0]
            self.assertEqual(Path(argv[0]).name, "gx-project")
            self.assertEqual(argv[1:], ["--project", "demo", "--state-dir", str(self.state), "--background",
                                        "--prompt-file", str(self.prompt)])
        denied = self.request("POST", self.route + "/messages", {"kind": "launch"},
                              {"Origin": "https://different.invalid"})
        self.assertEqual(denied.status, 403)

    def test_feedback_is_deduplicated_and_conflicts_are_visible(self):
        body = {"kind": "feedback", "client_message_id": "same", "text": "Compare both windows."}
        self.assertEqual(self.request("POST", self.route + "/messages", body).status, 409)
        # The selected native host is running; exercise the real project inbox.
        (self.state / "native-session.json").write_text("{}")
        with patch.object(projects, "snapshot", return_value={"status": "NATIVE_SESSION_ACTIVE"}):
            for _ in range(2):
                response = self.request("POST", self.route + "/messages", body)
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read())["delivery"], "queued")
            self.assertEqual(self.request("POST", self.route + "/messages", {**body, "text": "new"}).status, 409)
        self.assertEqual(project_messages.take_for_turn(self.state, "root"), [{"id": "same", "text": body["text"]}])
        self.assertEqual(project_messages.take_for_turn(self.state, "root"), [])

    def test_event_cursor_replays_only_later_project_transitions(self):
        with sqlite3.connect(self.state / "gx.db") as db:
            db.executescript("""
                CREATE TABLE runs (run_id TEXT, project_ref TEXT);
                INSERT INTO runs VALUES ('r1','demo'),('r2','other');
                CREATE TABLE events (event_id INTEGER PRIMARY KEY,run_id TEXT,ts TEXT,subject_kind TEXT,
                                     subject_id TEXT,prev_state TEXT,new_state TEXT);
                INSERT INTO events VALUES (1,'r1','time','task','task-1','READY','RUNNING'),
                                          (2,'r2','time','task','other-1','RUNNING','ACCEPTED'),
                                          (3,'r1','time','task','task-1','RUNNING','ACCEPTED');
            """)
        stream = self.request("GET", self.route + "/events?after=0", headers={"Last-Event-ID": "1"})
        self.assertEqual(stream.status, 200)
        self.assertTrue(stream.getheader("Content-Type").startswith("text/event-stream"))
        while (line := stream.readline()) and not line.startswith(b"id:"):
            pass
        self.assertEqual(line.rstrip(), b"id: 3")
        event = json.loads(stream.readline().removeprefix(b"data: "))
        self.assertEqual((event["subject"], event["status"]), ("task-1", "ACCEPTED"))
        stream.close()

    def test_feedback_wakes_the_existing_driver_and_stays_queued_on_launch_failure(self):
        (self.state / "project-driver.json").write_text("{}")
        ready = {"status": "READY", "queued_feedback": True}
        with (patch.object(projects, "snapshot", return_value={"status": "NO_PROGRESS"}),
              patch.object(projects, "run_project", return_value=ready),
              patch.object(projects, "native_active", return_value=False),
              patch.object(projects, "launch_project", side_effect=[OSError("driver unavailable"),
                                                                     {"status": "BACKGROUND_STARTED"}]) as launch):
            body = {"kind": "feedback", "client_message_id": "once", "text": "Revisit window A."}
            self.assertEqual(self.request("POST", self.route + "/messages", body).status, 503)
            self.assertEqual(project_messages.delivery_status(self.state, "once"), "queued")
            response = self.request("POST", self.route + "/messages", body)
            self.assertEqual(response.status, 202)
            self.assertEqual(json.loads(response.read())["project_status"], "BACKGROUND_STARTED")
            launch.assert_called_with(self.state, self.project)
        self.assertEqual(len(project_messages.take_for_turn(self.state, "root")), 1)
