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
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import GOAL, PRODUCT, draft
from acceptance.v621.test_evidence_gate import node
from acceptance.v621.test_claim_publication import BASE
from acceptance.v622.test_close import CloseStudy, SurveyedStudy, close_decision
from tests.test_v6_decisions import branch
from gx_core.p0 import cli, conversation, research_report, successor, v623_endpoints
from acceptance.v623 import test_close_metrics as receipts
from gx_core.p0.roots import Roots
from gx_core import budgets
from gx_core.persistence import store


class SuccessorStudy(unittest.TestCase):
    def test_previous_inbox_options_are_pinned_once_under_original_request(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "legacy"
        source = work / "custom.tsv"
        with CloseStudy(work) as s:
            parent = s.run_id
            s.call(s.lead, "gx_decide_revision", {"decision":close_decision()}, "close")
            s.advance()
            source.write_text("sample\ttemperature\nA\t17\n")
            options = {"input_files":[{"path":str(source)}]}
            text, mid = "Inspect the PI-selected table.", "pre-freeze-inbox"
            # The previous receipt producer saved the raw options with no role or digest.
            with patch.object(successor, "freeze_files", side_effect=lambda directory, supplied:supplied):
                conversation.send(s.conn, parent, text, mid, successor=options)
            accepted = successor.request(s.conn, s.state, parent, mid, text, options, native_owner=True)
            row = s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (accepted["run_id"],)).fetchone()
            saved = store.loads(row,"inputs_json")
            from gx_core.domain import ids
            self.assertEqual(saved["inputs"]["p0_spec"]["predecessor"]["request_digest"], ids.sha256_of_obj([text,options]))
            self.assertEqual(saved["files"][0]["sha256"], ids.sha256_of_file(source))
            self.assertEqual(saved["files"][0]["role"], "material")
            source.unlink()
            self.assertEqual(successor.request(s.conn, s.state, parent, mid, text, options, native_owner=True), accepted)

    def test_one_frozen_child_and_independent_result(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        work = directory / "trial"
        def run_cli(*args, expected_code=0):
            result = subprocess.run([sys.executable, "-B", str(PRODUCT/"bin/gx-p0"), args[0], "--state",
                                     str(work/"state"), *args[1:]], env={**os.environ, "GX_CODEX_OFFLINE":"1"},
                                    text=True, capture_output=True, timeout=40)
            self.assertEqual(result.returncode, expected_code, result.stderr + result.stdout)
            return json.loads(result.stdout)
        def warm(*, expected_code=0):
            return run_cli("research", "--run-key", "v62a-local-admission", "--goal", GOAL,
                           "--goal-config", str(work/"grant.json"), "--timeout", "1", expected_code=expected_code)
        method = "deposited-block-signed@1"
        parameters = {"mask":"common_ca","endpoint":"angle_deg","effect_minimum":1.0,
                      "zero_limit":0.1,"minimum_evaluable":1}
        def measure(s, lead, material, key, *, extra=()):
            study = branch("study", jobs=1)
            study["budget_limits"]["cpu_seconds"] = 120
            op = dict(node("inventory", material), capability=method, parameters=parameters)
            opened = s.call(lead, "gx_decide_revision", {"decision":draft(study,op,evidence=extra)}, key)
            return opened, s.settle(opened["aliases"]["local:inventory"])
        def conclude(s, result):
            reader = s.next_actor("run")
            ref = result["result_ref"]
            self.assertIn(ref, s.brief(reader)["evidence_refs"])
            receipts.EndpointClose().read_page(s, reader, ref, 0, "read-coordinate")
            metrics = v623_endpoints.registered(s.conn, s.run_id, ref)
            decision = close_decision([ref])
            decision["metric_refs"] = [metrics[0]["metric_ref"]]
            decision["stop_reason"] = "INSUFFICIENT_DATA"
            return s.call(reader, "gx_decide_revision", {"decision":decision}, "stop-after-read")
        grant = {"plugins":["org.gx.block-signed@1.0.0","org.gx.block-influence@1.0.0"],
                 "capabilities":[method,"deposited-block-influence@1"], "research_modules": [],
                 "project":"sealed-pcna", "research_effort":"medium", "turn_token_budget":1000,
                 "input_files":[{"path":str(PRODUCT/"examples/pcna/data/1VYM.cif"), "role":"structure",
                                 "acquisition":str(PRODUCT/"examples/pcna/data/1VYM.json")}]}
        limits = {"job_submissions":20,"worker_tasks":48,"cpu_seconds":1200,"api_calls":48,"tokens":120000,
                  "gpu_jobs":0,"gpu_minutes":0,"wall_hours":2}
        work.mkdir()
        (work/"grant.json").write_text(json.dumps({**grant,"run_budget":limits,"workspace_budget":limits}))
        warm(expected_code=75)
        with SurveyedStudy(work, reopen=True) as s:
            s.lead = s.next_actor("run")
            parent = s.run_id
            acquisition = json.loads((PRODUCT/"examples/pcna/data/1VYM.json").read_bytes())
            _, parent_result = measure(s, s.lead, s.materials[0], "parent-inventory")
            close = conclude(s, parent_result)
            s.advance()
            parent_material = s.materials[0]
            parent_budget = s.conn.execute("SELECT budget_ref FROM runs WHERE run_id=?", (parent,)).fetchone()[0]
            before = budgets.state(s.conn, parent_budget)
            held_turns = [r for r in before["reservations"] if r["subject_kind"] == "native_turn"]
            self.assertTrue(any(r["status"] == "HELD" for r in held_turns))
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
        fresh.unlink()
        def retry(_):
            with closing(cli.existing_db(work/"state", readonly=False)) as db:
                frozen = json.loads(db.execute("SELECT successor_json FROM p0_conversation_messages WHERE message_id=?",
                                               (mid,)).fetchone()[0])
                return successor.request(db, work/"state", parent, mid, question, frozen)
        # Both see the same explicit PI request before either gets its reply.
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(retry, range(2)))
        # The first delivery was lost before a reply. Original input files can disappear.
        receipt = run_cli(*args)["result"]
        self.assertEqual(retry(0), receipt)
        child = receipt["run_id"]
        self.assertEqual(receipt["parent_decision_ref"], close["decision_ref"])
        self.assertNotEqual(child, parent)
        with SurveyedStudy(work, reopen=True) as s:
            s.run_id = child
            s.roots = Roots(s.conn, run_id=child, cwd=s.state, token_budget=10)
            s.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
            after = budgets.state(s.conn, parent_budget)
            self.assertEqual(before["spent"], after["spent"])
            self.assertEqual(before["reserved"], after["reserved"])
            self.assertEqual(held_turns, [r for r in after["reservations"] if r["subject_kind"] == "native_turn"])
            child_budget = budgets.state(s.conn, s.conn.execute(
                "SELECT budget_ref FROM runs WHERE run_id=?", (child,)).fetchone()[0])
            for line, limit in child_budget["limits"].items():
                self.assertLessEqual(limit, before["limits"][line])
                self.assertLessEqual(limit + after["limits"][line], limits[line])
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
            prior = next(f for f in packet["materials"] if f["name"] == "1VYM.cif")
            self.assertNotEqual(prior["artifact_ref"], parent_material)
            prediction = {"target":"analysis", "method":{"capability":method,"output":"signed-summary.json",
                          "parameters":parameters,"metric_id":"missed_present","unit":"1","input_role":"structure"},
                          "observable":"/missed_present", "measurement_schema":"gx.signed-summary/1",
                          "operator":"eq", "threshold":1, "data_refs":[prior["artifact_ref"]]}
            claim = s.call(lead, "gx_stage_claim", dict(BASE, statement="The aggregate misses a directional change.",
                           criteria="missed_present equals one.", scope=acquisition["applicability"], level="open",
                           evidence_refs=[prior["artifact_ref"]], prediction=prediction), "old-endpoint")["claim_ref"]
            opened, result = measure(s, lead, material, "child-inventory", extra=[prior["artifact_ref"],claim])
            registration = opened["claim_revisions"][0]["maturity"]["registration"]
            self.assertTrue(registration["known_at_registration"][prior["sha256"]])
            self.assertEqual(result["status"], "SUCCEEDED")
            endpoint = result["result_ref"]
            conclude(s, result)
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
        self.assertEqual(result_for(child)["prior_study"]["parent_decision_ref"], close["decision_ref"])
        self.assertEqual(parent_export["run_id"], first_export["run_id"])
        self.assertEqual(child_export["run_id"], child)
        if evidence := os.environ.get("GX_SUCCESSOR_RECEIPT"):
            Path(evidence).write_text(json.dumps({"model":"scripted native replies; real CPU Job",
                "request":receipt, "parent_job":parent_result["job_id"], "parent_result":parent_result["result_ref"],
                "known_at_child_registration":registration["known_at_registration"], "child_job":result["job_id"], "child_result":endpoint,
                "child_close":child_anchor, "parent_export":archived["invocation"], "parent_close":archived["closure"]},
                indent=2))
