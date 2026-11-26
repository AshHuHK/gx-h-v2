"""A sealed Goal, two concurrent requesters, cold reattachment and a real child CPU Job."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import GOAL, PRODUCT, draft
from acceptance.v621.test_evidence_gate import node
from acceptance.v622.test_close import CloseStudy, close_decision
from tests.test_v6_decisions import branch
from gx_core.p0 import cli, research_report, successor, v623_endpoints
from acceptance.v623 import test_close_metrics as receipts
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class SuccessorStudy(unittest.TestCase):
    def test_one_frozen_child_and_independent_result(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        work = directory / "trial"
        def run_cli(*args):
            result = subprocess.run([sys.executable, "-B", str(PRODUCT/"bin/gx-p0"), args[0], "--state",
                                     str(work/"state"), *args[1:]], env={**os.environ, "GX_CODEX_OFFLINE":"1"},
                                    text=True, capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)
        def warm():
            return run_cli("research", "--run-key", "v62a-local-admission", "--goal", GOAL,
                           "--goal-config", str(work/"grant.json"), "--timeout", "1")
        method = "deposited-block-signed@1"
        grant = {"plugins":["org.gx.block-signed@1.0.0"], "capabilities":[method], "research_modules": [],
                 "input_files":[{"path":str(PRODUCT/"examples/pcna/data/1VYM.cif"), "role":"structure"}]}
        with CloseStudy(work, spec_updates=grant) as s:
            parent = s.run_id
            close = s.call(s.lead, "gx_decide_revision", {"decision":close_decision()}, "close")
            s.advance()
            parent_material = s.materials[0]
        first_export = warm()
        fresh = directory/"new-coordinate.cif"
        fresh.write_bytes((PRODUCT/"examples/pcna/open-data/3TBL.cif").read_bytes())
        options = {"parent_material_refs":[parent_material], "input_files":[
            {"path":str(fresh), "role":"structure", "acquisition":str(PRODUCT/"examples/pcna/open-data/3TBL.json")}]}
        question = "On the newly selected coordinates, measure deposited coordinate coverage."
        mid = "successor-cpu"
        selected = directory/"selected.json"
        selected.write_text(json.dumps(options))
        args = ("message", "--run-id",parent,"--message-id",mid,"--text",question,
                "--successor","--successor-config",str(selected))
        run_cli(*args, "--queue-only")
        def retry(_):
            with closing(cli.existing_db(work/"state", readonly=False)) as db:
                return successor.request(db, work/"state", parent, mid, question, options)
        # Both see the same explicit PI request before either gets its reply.
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(retry, range(2)))
        fresh.unlink()
        # The first delivery was lost before a reply. Original input files can disappear.
        receipt = run_cli(*args)["result"]
        self.assertEqual(retry(0), receipt)
        child = receipt["run_id"]
        self.assertEqual(receipt["parent_decision_ref"], close["decision_ref"])
        self.assertNotEqual(child, parent)
        with CloseStudy(work, reopen=True) as s:
            s.run_id = child
            s.roots = Roots(s.conn, run_id=child, cwd=s.state, token_budget=10)
            s.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
            lead = s.next_actor("run")
            packet = s.brief(lead)
            self.assertEqual(packet["goal"], question)
            self.assertEqual(packet["prior_research"]["parent_decision_ref"], close["decision_ref"])
            self.assertEqual(packet["prior_research"]["prior_conclusion"]["unresolved"],
                             ["Sampling independence remains unknown."])
            self.assertEqual([r[0] for r in s.conn.execute("SELECT run_id FROM v6_goals ORDER BY rowid")], [parent,child])
            self.assertEqual(len(s.rows("SELECT 1 FROM outbox WHERE topic='p0.workspace.start'")), 1)
            self.assertEqual(s.rows("SELECT goal_version,status FROM v6_goals WHERE run_id=?", parent),
                             [{"goal_version":1,"status":"CLOSED"}])
            self.assertEqual(s.rows("SELECT goal,goal_version FROM v6_goals WHERE run_id=?", child),
                             [{"goal":question,"goal_version":1}])
            material = next(f["artifact_ref"] for f in packet["materials"] if f["name"] == fresh.name)
            study = branch("study", jobs=1)
            study["budget_limits"]["cpu_seconds"] = 120
            op = dict(node("inventory", material), capability=method,
                      parameters={"mask":"common_ca","endpoint":"angle_deg","effect_minimum":1.0,
                                  "zero_limit":0.1,"minimum_evaluable":1})
            opened = s.call(lead, "gx_decide_revision", {"decision":draft(study,op)}, "inspect")
            result = s.settle(opened["aliases"]["local:inventory"])
            self.assertEqual(result["status"], "SUCCEEDED")
            endpoint = result["result_ref"]
            reader = s.next_actor("run")
            self.assertIn(endpoint, s.brief(reader)["evidence_refs"])
            receipts.EndpointClose().read_page(s, reader, endpoint, 0, "read-new-coordinate")
            measured = v623_endpoints.registered(s.conn, child, endpoint)
            decision = close_decision([endpoint])
            decision["metric_refs"] = [measured[0]["metric_ref"]]
            decision["stop_reason"] = "INSUFFICIENT_DATA"
            s.call(reader, "gx_decide_revision", {"decision":decision}, "stop-after-read")
            s.advance()
            child_anchor = research_report.completed_close(s.conn, child)
        child_export = run_cli("research", "--run-id", child, "--timeout", "1")
        parent_export = warm()
        def result_for(rid):
            return json.loads((work/"state"/"research"/rid/"result.json").read_bytes())
        archived = result_for(parent)
        self.assertEqual(archived["invocation"]["added"], {"host_turns":0,"jobs":0})
        self.assertEqual(archived["final_decision"]["decision_ref"], close["decision_ref"])
        self.assertEqual(result_for(child)["closure"]["decision_ref"], child_anchor["decision_ref"])
        self.assertEqual(result_for(child)["final_decision"]["evidence_refs"], [endpoint])
        self.assertEqual(parent_export["run_id"], first_export["run_id"])
        self.assertEqual(child_export["run_id"], child)
        if evidence := os.environ.get("GX_SUCCESSOR_RECEIPT"):
            Path(evidence).write_text(json.dumps({"model":"scripted native replies; real CPU Job",
                "request":receipt, "child_job":result["job_id"], "child_result":endpoint,
                "child_close":child_anchor, "parent_export":archived["invocation"], "parent_close":archived["closure"]},
                indent=2))
