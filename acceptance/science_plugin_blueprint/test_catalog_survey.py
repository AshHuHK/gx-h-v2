"""Normal research admission and native callbacks, including an interrupted reply delivery."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core import config as gx_config
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.p0 import cli, conversation, research_dialogue, v6_goal
from gx_core.persistence import store


class CatalogSurvey(unittest.TestCase):
    def test_existing_tool_menu_matches_its_catalog_contract(self):
        from gx_core.p0 import v62_native
        old = v62_native.query_tool({})["inputSchema"]
        self.assertEqual(old["required"], ["kind"])
        self.assertNotIn("all", old["properties"]["kind"]["enum"])
        self.assertNotIn("required_outputs", old["properties"])
        self.assertNotIn("provider", old["properties"])
        for packet in ({"catalog_discovery": {"state_key": "new"}}, {"researcher_handoff": True}):
            current = v62_native.query_tool(packet)["inputSchema"]
            self.assertIn("all", current["properties"]["kind"]["enum"])
            self.assertEqual(current["required"], [])

    def test_updates_empty_results_replay_and_cold_delivery(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        if evidence_dir := os.environ.get("GX_CATALOG_SURVEY_EVIDENCE"):
            directory = Path(tempfile.mkdtemp(dir=evidence_dir, prefix="survey-"))
        config = directory / "grant.json"
        config.write_text(json.dumps({"research_effort": "quick", "capability_collections": []}))
        goal = "How do rain-gauge totals differ between the two observation periods?"
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(directory / "state"), "--run-key", "rain-compare",
                                       "--goal", goal, "--goal-config", str(config)]), 0)

        query = {"kind":"all", "query":"quuxnomatchingmethodzz", "pi_method_scope":[]}
        watches = [{"event": "goal_changed", "subject_ref": "run"}]
        def keep_plan(s, lead):
            return s.call(lead, "gx_decide_revision", {"decision": draft(watches=watches)}, "maintain")

        def survey(s, lead):
            packet = s.brief(lead)["catalog_discovery"]
            self.assertEqual(packet["question"], goal)
            self.assertTrue(packet["required_outputs"])
            result = s.call(lead, "gx_query_catalog", query, "survey")
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["items"], [])
            return packet, result

        with CloseStudy(directory, reopen=True) as first:
            initial = first.next_actor("run")
            with self.assertRaises(SchemaInvalid) as refusal:
                first.call(initial, "gx_decide_revision", {"decision": draft(watches=watches)}, "premature")
            self.assertEqual(refusal.exception.details["reason"], "CATALOG_SURVEY_REQUIRED")
            opening, first_result = survey(first, initial)
            for i, keyword in enumerate(("water", "runoff", "gauge", "sensitivity", "database")):
                refinement = first.call(initial, "gx_query_catalog", {"kind": "all", "query": keyword}, f"refine-{i}")
            with patch("gx_core.p0.v62_catalog.query", side_effect=AssertionError("duplicate proactive search")):
                self.assertEqual(first.call(initial, "gx_query_catalog", {"query": "database"}, "same-refinement"),
                                 refinement)
            self.assertEqual(len(first.rows("SELECT event_id FROM events WHERE kind='research.catalog.surveyed'")), 1)

        # The owner committed the survey and native reply; GX disappeared before Codex saw it.
        with CloseStudy(directory, reopen=True) as study:
            study.roots.active_turns[initial.thread] = initial.turn
            with patch("gx_core.p0.v62_catalog.query", side_effect=AssertionError("duplicate work")):
                self.assertEqual(study.call(initial, "gx_query_catalog", query, "reissued"), first_result)
            keep_plan(study, initial)
            self.assertIsNone(v6_goal.ensure_task(study.conn, study.run_id))
            body = b'{"rain_mm":4}'
            ref = ArtifactService(study.conn).publish_bytes(namespace=study.run_id + "/sources",
                kind="operator_source", rel_path="period.json", data=body, run_id=study.run_id,
                media_type="application/json", provenance={"station": "controlled-test"}).artifact_ref
            event = v6_goal.record_observation(study.conn, study.run_id, ref, source="period-1")
            lead = study.next_actor("run")
            changed, _ = survey(study, lead)
            self.assertEqual(changed["through_event"], event["event_id"])
            self.assertIn(ref, [r["artifact_ref"] for r in changed["available_inputs"]])
            keep_plan(study, lead)
            self.assertEqual(v6_goal.record_observation(study.conn, study.run_id, ref, source="period-1"), event)
            self.assertIsNone(v6_goal.ensure_task(study.conn, study.run_id))

            # Exercise the Job owner's durable failure-event boundary. No external Job is simulated as executed.
            with store.transaction(study.conn, transaction_owner="CatalogSurveyTest", operation="inject_failed_settlement",
                                   run_id=study.run_id):
                failed = store.append_event(study.conn, run_id=study.run_id, actor="fixture",
                    kind="v6.node.settled", subject_kind="node", subject_id="fixture-failed-read",
                    payload={"status":"FAILED", "scope":"run", "failure":{"reason":"input unavailable"}})
            after_failure = study.next_actor("run")
            packet, _ = survey(study, after_failure)
            self.assertEqual(packet["through_event"], failed)
            self.assertEqual(packet["updates"][-1]["status"], "FAILED")
            self.assertIn(ref, [item["artifact_ref"] for item in packet["available_inputs"]])
            keep_plan(study, after_failure)

            wording = "Use equal-length observation periods for the comparison."
            conversation.send(study.conn, study.run_id, wording, "pi-equal-periods")
            feedback = research_dialogue.add_feedback(study.conn, study.run_id, "pi-equal-periods",
                                                       goal_version=1, scope="run", text=wording)
            revised = study.next_actor("run")
            packet, _ = survey(study, revised)
            self.assertEqual(packet["through_event"], feedback["event_id"])
            self.assertEqual(packet["pi_update"], wording)
            self.assertEqual(research_dialogue.add_feedback(study.conn, study.run_id, "pi-equal-periods",
                goal_version=1, scope="run", text=wording), feedback)
            response = dict(draft(watches=watches), pi_dispositions=[{
                "feedback_id": feedback["feedback_id"], "outcome": "adopt",
                "reason": "Compare equal-duration windows; retain the single-period reading until a second is available."}])
            study.call(revised, "gx_decide_revision", {"decision": response}, "adopt-condition")
            rows = study.rows("SELECT event_id,subject_id state_key,payload_json FROM events "
                              "WHERE kind='research.catalog.surveyed' ORDER BY event_id")
            self.assertEqual(len(rows), 4)
            self.assertEqual(len({r["state_key"] for r in rows}), 4)
            self.assertEqual(json.loads(rows[0]["payload_json"])["through_event"], opening["through_event"])
            if evidence_dir:
                for row in rows:
                    row["receipt"] = json.loads(row.pop("payload_json"))
                    context = row["receipt"]["context_ref"]
                    frozen, decision_ref = study.conn.execute("SELECT snapshot_json,d.decision_ref "
                        "FROM research_contexts c JOIN v6_decisions d USING(context_ref) WHERE c.context_ref=?",
                        (context,)).fetchone()
                    row.update(catalog_context=json.loads(frozen)["catalog_discovery"], decision_ref=decision_ref)
                (directory / "receipt.json").write_text(json.dumps(
                    {"run_id":study.run_id, "goal":goal,"state":str(directory / "state"), "receipts":rows,
                     "native_transport":"controlled callbacks", "job_failure":"injected settled-event fixture",
                     "cold_replay_equal":True,"evidence_ref":ref}, indent=2) + "\n")


    def test_installed_catalog_change_wakes_once_and_superseding_lease_reuses_survey(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        conf = directory / "config"
        shutil.copytree(gx_config.CONFIG_DIR, conf)
        self.enterContext(patch.object(gx_config, "CONFIG_DIR", conf))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(directory / "state"), "--run-key", "forecast",
                                       "--goal", "Assess variability across our rain gauges."]), 0)
        with CloseStudy(directory, reopen=True) as study:
            decision = {"decision": draft(watches=[{"event": "goal_changed", "subject_ref": "run"}])}
            args = {"kind": "all", "query": "pluviométrie"}
            opening = study.next_actor("run")
            self.assertEqual(study.call(opening, "gx_query_catalog", {**args, "pi_method_scope":[]}, "initial")["total"], 0)
            study.call(opening, "gx_decide_revision", decision, "initial-decision")
            assets = conf / "research_asset_cards.json"
            cards = json.loads(assets.read_bytes())
            cards["cards"].append(dict(cards["cards"][0], reference="rainfall-notes@1", title="Pluviométrie"))
            assets.write_text(json.dumps(cards))
            self.assertEqual(v6_goal.waiting_for(study.conn, study.run_id)["kind"], "research_event")
            next_lead = study.next_actor("run")
            result = study.call(next_lead, "gx_query_catalog", args, "updated")
            self.assertIn("rainfall-notes@1", [row["reference"] for row in result["items"]])
            # Reconstruct the deployment's initial receipt format in this isolated fixture.
            with store.transaction(study.conn, transaction_owner="CatalogSurveyTest", operation="old_format",
                                   run_id=study.run_id):
                study.conn.execute("UPDATE events SET payload_json=json_remove(payload_json,'$.first_candidates',"
                                   "'$.semantic_key') WHERE event_id=?", (result["survey_event"],))
                study.conn.execute("DELETE FROM idempotency WHERE scope='catalog.query'")
            with patch("gx_core.p0.v62_catalog.query", side_effect=AssertionError("old reply re-executed")):
                self.assertEqual(study.call(next_lead, "gx_query_catalog", args, "old-replay"), result)
            study.call(next_lead, "gx_decide_revision", decision, "continue")
            self.assertIsNone(v6_goal.ensure_task(study.conn, study.run_id))

            with store.transaction(study.conn, transaction_owner="CatalogSurveyTest", operation="reissue_task",
                                   run_id=study.run_id):
                store.append_event(study.conn, kind="v6.decision.superseded", run_id=study.run_id, actor="fixture",
                                   subject_kind="task", subject_id=next_lead.task_id, payload={"scope":"run"})
            reissued = study.next_actor("run")
            ctx = study.brief(reissued)["catalog_discovery"]
            self.assertEqual(ctx["survey_event"], result["survey_event"])
            self.assertIn("rainfall-notes@1", [row["reference"] for row in ctx["surveyed_candidates"]])
            # A pruned exact response retains its survey fact, without invented results.
            from gx_core.p0 import catalog_survey
            with patch.object(catalog_survey, "_native_answer", return_value=None):
                rebuilt = catalog_survey.describe(study.conn, study.brief(reissued),
                    {"survey_cursor":ctx["through_event"]}, {})
            self.assertTrue(rebuilt["surveyed_details_unavailable"])
            self.assertNotIn("surveyed_candidates", rebuilt)
            study.call(reissued, "gx_decide_revision", decision, "reuse-survey")
            self.assertEqual(len(study.rows("SELECT event_id FROM events WHERE kind='research.catalog.surveyed'")), 2)
            self.assertIsNone(v6_goal.ensure_task(study.conn, study.run_id))


if __name__ == "__main__":
    unittest.main()
