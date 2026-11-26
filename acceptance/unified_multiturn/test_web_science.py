"""The Run selected in the PI's browser determines the evidence, routes and sent Goal version."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from acceptance.v62a.interleavings_fixture import GOAL, draft
from acceptance.v622.test_close import CloseStudy, close_decision
from gx_core.web.http import BASE, make_server


class ResearchBrowser(unittest.TestCase):
    def test_module_choices_and_sealed_version_follow_the_accepted_record(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "trial"
        with CloseStudy(work) as study:
            db, rid = study.conn, study.run_id
            project, = db.execute("SELECT project_ref FROM runs WHERE run_id=?", (rid,)).fetchone()
            route = BASE + "/" + project
            opened = study.open("inventory", round=1, inputs=[study.materials[-1]], caps=[])
            branch_id, = db.execute("SELECT branch_id FROM v62_modules WHERE run_id=?", (rid,)).fetchone()
            with make_server(study.state, project, 0, run_id=rid) as web:
                thread = threading.Thread(target=web.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
                thread.start()
                def request(path, body=None):
                    with closing(http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=5)) as conn:
                        conn.request("POST" if body else "GET", path, json.dumps(body) if body else None,
                                     {"Content-Type": "application/json"})
                        response = conn.getresponse()
                        return response.status, json.loads(response.read())
                try:
                    code, view = request(route)
                    self.assertEqual(code, 200, view)
                    self.assertIn(branch_id, [branch["branch_id"] for branch in view["branches"]])
                    self.assertIn(study.materials[-1], [item["artifact_ref"] for item in view["material_options"]])
                    self.assertEqual(request(route + "?run_id=other")[0], 409)
                    actor = study.bind(opened["task_id"])
                    study.call(actor, "gx_read_artifact", {"ref": study.materials[-1], "operation_key": "read"}, "read")
                    study.call(study.lead, "gx_decide_revision", {"decision": draft()}, "wait")
                    report = study.report(actor, study.materials[-1])
                    action = study.call(study.next_actor("run"), "gx_decide_revision", {
                        "decision": close_decision([report["report_ref"]])}, "close")
                    study.advance()
                    # Reproduce an old read-only scientific record with a later, misaligned header.
                    db.execute("UPDATE v6_goals SET goal_version=2,goal='Later wording' WHERE run_id=?", (rid,))
                    code, archived = request(route)
                    self.assertEqual((code, archived["goal"], archived["goal_version"]), (200, GOAL, 1))
                    code, evidence = request(route + "/basis?decision_ref=" + action["decision_ref"])
                    self.assertEqual(code, 200, evidence)
                    self.assertEqual(evidence["lead_decisions"][0]["decision_ref"], action["decision_ref"])
                    self.assertIn(report["report_ref"], [r["report_ref"] for r in evidence["released_reports"]])
                    form = {"client_message_id": "explain-close", "run_id": rid, "goal_version": archived["goal_version"],
                            "text": "How did you reach the archived conclusion?"}
                    self.assertEqual(request(route + "/messages", form)[0], 202)
                    form.update(client_message_id="follow-up", successor={"input_files": [{"path": "/any/server/path"}]})
                    self.assertEqual(request(route + "/messages", form)[0], 400)
                    self.assertEqual(db.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0], 2)
                finally:
                    web.shutdown()
                    thread.join()
