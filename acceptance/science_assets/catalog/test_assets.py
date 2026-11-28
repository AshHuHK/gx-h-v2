"""Exercise static assets via normal research admission and the native catalog callback."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core import config
from gx_core.errors import ScopeDenied
from gx_core.p0 import cli, v6_goal


def archived(ref="sky-reading@archive"):
    return {"reference": ref, "kind": "method", "category": "model", "title": "Calibrated sky reading",
            "purpose": "Document an earlier sky-response model", "canonical_method_id": "archived-sky-response",
            "aliases": ["天空校準", "天空校准"], "digest": "e" * 64,
            "provider_binding": {"provider_id": "archive-telescope", "release": "2018"},
            "inputs": ["observation"], "outputs": [{"name": "sky.json", "kind": "file:sky-reading",
                                                    "nature": "analysis"}],
            "source": {"workspace": "observatory", "path": "sky/model-card.json"}}


class AssetCatalog(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.conf = self.workspace / "config"
        shutil.copytree(config.CONFIG_DIR, self.conf)
        self.enterContext(patch.object(config, "CONFIG_DIR", self.conf))
        self.legacy = self.conf / "research_asset_cards.json"
        self.extra = self.conf / "science_asset_inventory.json"
        self.extra.unlink(missing_ok=True)
        with patch.object(cli, "run_until_stopped",
                          side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(self.workspace / "state"), "--run-key", "asset-review",
                                       "--research-effort", "deep", "--goal", "Which records cover our two sky observations?"]), 0)

    def test_historical_method_has_details_and_cannot_select_or_create_a_job(self):
        data = json.loads(self.legacy.read_bytes())
        card = archived()
        data["cards"].append(card)
        self.legacy.write_text(json.dumps(data))
        self.extra.write_text(json.dumps({"cards": [card, archived("sky-companion@legacy")]}))
        with CloseStudy(self.workspace, reopen=True) as study:
            lead = study.next_actor("run")
            for kind in ("all", "method"):
                first = study.call(lead, "gx_query_catalog", {"kind": kind, "query": "天空校準", "limit": 1,
                                   **({"pi_method_scope": []} if kind == "all" else {}),
                                   "required_outputs": ["sky.json"]}, f"list-{kind}")
                self.assertEqual(first["total"], 2)
                self.assertEqual(first["next_offset"], 1)
                last = study.call(lead, "gx_query_catalog", {"kind": kind, "query": "天空校准", "limit": 1,
                                  "offset": 1, "required_outputs": ["sky.json"]}, f"page-{kind}")
                self.assertEqual(last["next_offset"], None)
                rows = first["items"] + last["items"]
                self.assertEqual({row["reference"] for row in rows}, {card["reference"], "sky-companion@legacy"})
                self.assertTrue(all(row["output_fit"] == "declared" and not row["execution_granted"] for row in rows))
                detail = study.call(lead, "gx_query_catalog", {"kind": kind, "reference": card["reference"]},
                                    f"detail-{kind}")
                self.assertEqual(detail["spec"]["source"], card["source"])
                self.assertEqual(detail["spec"]["provider_binding"], card["provider_binding"])
                self.assertEqual(detail["status"], "reference_only")
                self.assertIsNone(detail["provider"])
                self.assertNotIn("source_ref", detail)
            phrased = study.call(lead, "gx_query_catalog", {"kind": "all", "query": "請找出天空校準的模型"},
                                 "natural-language-alias")
            self.assertEqual(phrased["total"], 2)
            with self.assertRaises(ScopeDenied):
                study.call(lead, "gx_select_capability", {"method": card["reference"], "action": "enable",
                                                         "revision": 0}, "select-historical")
            self.assertEqual(study.rows("SELECT job_id FROM jobs"), [])

    def test_public_method_survey_can_read_archived_card_without_method_grant(self):
        self.extra.write_text(json.dumps({"cards": [archived()]}))
        with CloseStudy(self.workspace, reopen=True) as study:
            lead = study.next_actor("run")
            study.call(lead, "gx_query_catalog", {"kind": "all", "pi_method_scope": []}, "first-survey")
            opening = {**study.open_args("survey", inputs=[], caps=[]), "module_id": "method_workflow_survey",
                       "focus": "model"}
            declared = study.call(lead, "gx_open_module", opening, "request-survey")
            researcher = study.bind(declared["task_id"])
            self.assertEqual(study.brief(researcher)["allowed_method_ids"], [])
            details = study.call(researcher, "gx_query_catalog",
                                 {"kind": "method", "reference": archived()["reference"]}, "read-old-model")
            self.assertEqual(details["spec"]["source"], archived()["source"])
            self.assertFalse(details["execution_granted"])
            self.assertNotIn("source_ref", details)

    def test_new_index_wakes_once_then_withdraws_and_cold_replay_keeps_history(self):
        args = {"kind": "all", "query": "天空校準"}
        decision = {"decision": draft(watches=[{"event": "goal_changed", "subject_ref": "run"}])}
        with CloseStudy(self.workspace, reopen=True) as study:
            first = study.next_actor("run")
            self.assertEqual(study.call(first, "gx_query_catalog", {**args, "pi_method_scope": []}, "not-added")["total"], 0)
            study.call(first, "gx_decide_revision", decision, "wait")
            self.extra.write_text(json.dumps({"cards": [archived()]}))
            self.assertEqual(v6_goal.waiting_for(study.conn, study.run_id)["kind"], "research_event")
            second = study.next_actor("run")
            added = study.call(second, "gx_query_catalog", args, "added")
            self.assertEqual(added["total"], 1)
            self.assertEqual(study.call(second, "gx_query_catalog", args, "added-again"), added)
            study.call(second, "gx_decide_revision", decision, "wait-after-add")
            self.extra.unlink()
        with CloseStudy(self.workspace, reopen=True) as reopened:
            self.assertEqual(v6_goal.waiting_for(reopened.conn, reopened.run_id)["kind"], "research_event")
            third = reopened.next_actor("run")
            self.assertEqual(reopened.call(third, "gx_query_catalog", args, "withdrawn")["total"], 0)
            with self.assertRaises(ScopeDenied):
                reopened.call(third, "gx_query_catalog", {"kind": "method", "reference": archived()["reference"]},
                              "withdrawn-card")
            receipt = reopened.rows("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                    second.thread, "added")
            self.assertEqual(json.loads(receipt[0]["result_json"]), added)
            changes = reopened.rows("SELECT payload_json FROM events WHERE kind='research.catalog.updated'")
            self.assertEqual(len(changes), 3)

    def test_unreadable_inventory_and_malformed_row_leave_partial_results(self):
        self.extra.write_text("{invalid")
        with CloseStudy(self.workspace, reopen=True) as study:
            lead = study.next_actor("run")
            result = study.call(lead, "gx_query_catalog", {"kind": "all", "query": "Crossref", "pi_method_scope": []}, "corrupt")
            self.assertTrue(result["items"])
            self.assertTrue(result["unavailable_asset_metadata"])
            self.extra.write_text(json.dumps({"cards": [None, {"kind": "method"}, archived()]}))
            result = study.call(lead, "gx_query_catalog", {"kind": "method", "query": "archived-sky-response"},
                                "partially-readable")
            self.assertEqual(result["total"], 1)
            self.assertTrue(result["unavailable_asset_metadata"])
