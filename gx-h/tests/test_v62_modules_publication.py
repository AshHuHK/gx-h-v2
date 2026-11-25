"""Public scripted module research through Goal admission and the native GX tool surface."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gx_core.domain import ids
from gx_core.errors import ScopeDenied
from gx_core.p0 import v6_native
from tests.test_v62_modules_catalog import HYPOTHESES, LITERATURE, PRODUCT, SURVEY, public_grant

with patch.object(sys, "path", [str(PRODUCT), *sys.path]):
    from acceptance.v62a.interleavings_fixture import Scenario, draft


class ModulePublications(unittest.TestCase):
    def scenario(self, effort, modules, **settings):
        spec = public_grant()
        spec.update(research_effort=effort, research_modules=[name + "@1" for name in modules], **settings)
        if not modules:
            spec["plugins"] = ["org.gx.protein@0.2.0"]
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return self.enterContext(Scenario(directory, spec_updates=spec))

    def open_module(self, s, name, focus, inputs, round_number=1, key=None):
        params = s.open_args(key or "open-" + name, caps=[], inputs=inputs, round=round_number)
        params.update(module_id=name, focus=focus, expected_delivery=focus)
        opened = s.call(s.lead, "gx_open_module", params, params["operation_key"])
        return s.next_actor(opened["scope"])

    def wait_for_reports(self, s):
        s.call(s.lead, "gx_decide_revision", {"decision": draft(watches=[
            {"event": "goal_changed", "subject_ref": "run"}
        ])}, "wait-" + s.lead.task_id)

    def test_quick_has_a_direct_method_and_no_delegation(self):
        s = self.scenario("quick", [])
        brief = s.brief(s.lead)
        self.assertFalse(brief.get("module_catalog"))
        self.assertIn("deposited-chain-geometry@1", [m["capability_version"] for m in brief["capabilities"]])
        tools = s.roots._tools_for(s.roots._binding(s.lead.thread))
        self.assertNotIn("gx_open_module", [t["name"] for t in tools])

    def test_both_catalog_levels_fit_the_wire_budget_and_keep_every_entry_retrievable(self):
        modules = [SURVEY, LITERATURE, HYPOTHESES, "data_inventory"]
        wire_sizes = []
        for limit in (8192, 256):
            s = self.scenario("deep", modules, module_policy={"catalog_limit": 15, "catalog_bytes": limit})
            root = s.roots._binding(s.lead.thread)
            brief = v6_native.brief(root)
            wire_sizes.append(len(root["brief"].encode("utf-8")))
            if limit == 8192:
                self.assertEqual({card["module_id"] for card in brief["module_catalog"]}, set(modules))
                for card in brief["module_catalog"]:
                    self.assertTrue(card["purpose"] and card["cost_class"] and card["output"])
                    self.assertNotIn("instructions", card)
                for card in brief["capabilities"]:
                    self.assertTrue(card["capability_version"] and card["cost_class"] and card["outputs"])
                self.assertEqual(brief["catalog_view"]["mode"], "summaries")
                continue
            self.assertEqual((brief["module_catalog"], brief["capabilities"]), ([], []))
            self.assertTrue(brief["module_catalog_available"])
            for key in ("module_catalog_view", "catalog_view"):
                self.assertEqual(brief[key]["mode"], "categories")
                self.assertLessEqual(len(ids.canonical_json(brief[key]["categories"]).encode("utf-8")), limit)
            self.assertEqual(set(brief["module_catalog_view"]["categories"]),
                             {"data", "model", "workflow", "evaluation", "literature", "mechanism", "hypothesis"})
            seen = []
            offset = 0
            while offset is not None:
                page = s.call(s.lead, "gx_query_catalog", {"kind": "module", "offset": offset, "limit": 2},
                              f"catalog-{offset}")
                seen.extend(row["module_id"] for row in page["items"])
                offset = page["next_offset"]
            self.assertCountEqual(seen, modules)
        self.assertLess(wire_sizes[1], wire_sizes[0])

    def test_medium_survey_publishes_candidates_costs_exclusions_and_gaps_from_public_cards(self):
        s = self.scenario("medium", [SURVEY, "data_inventory"], module_policy={"catalog_limit": 1})
        lead = s.brief(s.lead)
        self.assertEqual(lead["catalog_view"]["mode"], "categories")
        self.assertEqual(lead["module_catalog_view"]["mode"], "categories")
        cards = s.call(s.lead, "gx_query_catalog", {"kind": "module", "category": "evaluation"}, "find-evaluator")
        self.assertEqual([m["module_id"] for m in cards["items"]], [SURVEY])
        mats = {m["role"]: m["artifact_ref"] for m in lead["materials"]}
        with self.assertRaises(ScopeDenied):
            self.open_module(s, SURVEY, "Survey coordinate comparison workflows", [mats["structure_2O0O"]],
                             key="refused-coordinate-input")
        survey = self.open_module(s, SURVEY, "Survey methods for 2O0O", [
            mats["workflow_spec"], mats["model_card"], mats["evaluation_spec"]])
        self.wait_for_reports(s)
        self.assertEqual(s.brief(survey)["allowed_method_ids"], [])
        self.assertNotIn("gx_decide_revision", s.brief(survey)["allowed_tools"])
        with self.assertRaises(ScopeDenied):
            s.call(survey, "gx_read_artifact", {"ref": mats["structure_2O0O"]}, "outside-metadata-grant")
        card = s.call(survey, "gx_read_artifact", {"ref": mats["workflow_spec"]}, "read-workflows")
        models = s.call(survey, "gx_read_artifact", {"ref": mats["model_card"]}, "read-models")
        evaluation = s.call(survey, "gx_read_artifact", {"ref": mats["evaluation_spec"]}, "read-evaluations")
        self.assertEqual({m["id"] for m in models["models"]}, {"PDB:2RE9/model:1", "PDB:2O0O/model:1"})
        findings = []
        expected = {True: "deposited-chain-geometry@1", False: "tl1a-coordinate-quality@1"}
        for candidate in card["candidates"]:
            if candidate["kind"] != "goal_method":
                continue
            eligible = "2O0O" in candidate["supported_entries"]
            self.assertEqual(candidate["id"], expected[eligible])
            exact = s.call(survey, "gx_query_catalog", {"kind": "method", "reference": candidate["id"]},
                           "lookup-" + candidate["id"])
            self.assertEqual(exact["spec"]["capability_version"], candidate["id"])
            reason = candidate["applicability"] if eligible else candidate["limitations"]
            action = "Shortlist" if eligible else "Exclude"
            findings.append({"text": f"{action} {candidate['id']} ({candidate['cost_class']}). {reason}",
                             "source_refs": [mats["workflow_spec"], exact["source_ref"]]})
        self.assertEqual(len(findings), 2)
        findings.append({"text": models["dependency"], "source_refs": [mats["model_card"]]})
        findings.append({"text": evaluation["evaluation_gap"], "source_refs": [mats["evaluation_spec"]]})
        submission = {"operation_key": "sourced-shortlist", "status": "COMPLETE", "findings": findings,
                      "artifact_refs": [mats["workflow_spec"], mats["model_card"], mats["evaluation_spec"]],
                      "gaps": [card["evaluation_gap"]], "next_steps": ["Obtain coverage on the selected input."]}
        receipt = s.call(survey, "gx_submit_module_report", submission, "publish-shortlist")
        self.assertEqual(receipt["status"], "COMPLETE")
        next_lead = s.next_actor("run")
        self.assertIn(receipt["report_ref"], s.brief(next_lead)["evidence_refs"])
        published = s.call(next_lead, "gx_read_artifact", {"ref": receipt["report_ref"]}, "read-shortlist")
        self.assertEqual(published["findings"], findings)
        self.assertFalse(s.rows("SELECT job_id FROM jobs WHERE run_id=?", s.run_id))

    def test_deep_reading_supplies_a_published_source_for_competing_hypotheses(self):
        fixture = Path(self.enterContext(tempfile.TemporaryDirectory())) / "renamed-report.txt"
        fixture.write_text("This original input carries a report-shaped role but has no publication receipt.")
        originals = public_grant()["input_files"] + [{"path": str(fixture), "role": "module_report"}]
        s = self.scenario("deep", [LITERATURE, HYPOTHESES], input_files=originals)
        initial = s.brief(s.lead)
        by_name = {m["name"]: m["artifact_ref"] for m in initial["materials"]}
        with self.assertRaises(ScopeDenied):
            self.open_module(s, HYPOTHESES, "Read a released report", [by_name[fixture.name]],
                             key="refused-unpublished-report")
        xml_ref, note_ref = by_name["deposition-note.xml"], by_name["method-notes.md"]
        reader = self.open_module(s, LITERATURE, "How do deposition metadata bear on mask coverage?", [xml_ref, note_ref])
        self.wait_for_reports(s)
        listing = s.call(reader, "gx_read_source", {"source_ref": xml_ref}, "locators")
        locator = next(row for row in listing["segments"] if row["label"] == "body/source/p/record")
        passage = s.call(reader, "gx_read_source", {"source_ref": xml_ref, "segment_id": locator["segment_id"]},
                         "read-original")
        quote = "3.000 angstroms"
        self.assertIn(quote, passage["text"])
        entry = s.call(reader, "gx_extract_evidence", {
            "source_ref": xml_ref, "segment_id": passage["segment_id"], "receipt_id": passage["receipt_id"],
            "quote": quote, "claim": "The 2O0O note reports a 3.000 angstrom refinement limit.",
            "direction": "supports", "applicability": "Refinement metadata for this deposition.",
            "confidence_basis": "A named field and unit in the registered data note.",
        }, "extract-resolution")
        self.assertTrue(entry["accepted"])
        self.assertEqual(entry["record_nature"], "test_fixture")
        citation = {"text": f"The 2O0O note reports {quote}. Atom coverage remains to be measured.",
                    "source_refs": [xml_ref, entry["evidence_ref"]]}
        contrast_id = next(row["segment_id"] for row in listing["segments"] if row["label"] == "body/source/p/citation")
        contrast = s.call(reader, "gx_read_source", {"source_ref": xml_ref, "segment_id": contrast_id}, "read-citation")
        contrary_quote = "The refinement metadata in the 2O0O coordinate file report 3.0 angstroms for that entry."
        adverse = s.call(reader, "gx_extract_evidence", {
            "source_ref": xml_ref, "segment_id": contrast_id, "receipt_id": contrast["receipt_id"],
            "quote": contrary_quote, "claim": "Both depositions have the 2.1 angstrom resolution in the article title.",
            "direction": "opposes", "applicability": "Title and refinement data in the cited public note.",
            "confidence_basis": "The note distinguishes the cited article's title from the entry's refinement limit.",
        }, "extract-counterexample")
        self.assertTrue(adverse["accepted"])
        counterexample = {"text": contrary_quote, "kind": "counterevidence",
                          "source_refs": [xml_ref, adverse["evidence_ref"]]}
        notes = s.call(reader, "gx_read_source", {"source_ref": note_ref}, "find-mask-method")
        excerpt = None
        for segment in notes["segments"]:
            part = s.call(reader, "gx_read_source", {"source_ref": note_ref, "segment_id": segment["segment_id"]},
                          "mask-note-" + segment["segment_id"])
            if "The common_backbone mask requires" in part["text"]:
                excerpt = part
                break
        self.assertIsNotNone(excerpt)
        method = {"text": excerpt["text"], "source_refs": [note_ref]}
        reading = s.report(reader, xml_ref, "publish-reading", findings=[citation, counterexample, method],
                           artifact_refs=[entry["evidence_ref"], adverse["evidence_ref"]],
                           gaps=["Refinement resolution alone gives no coverage."])
        lead = s.next_actor("run")
        self.assertIn(reading["report_ref"], s.brief(lead)["evidence_refs"])
        analyst = self.open_module(s, HYPOTHESES, "Distinguish mask coverage explanations.",
                                   [reading["report_ref"]], round_number=2)
        self.wait_for_reports(s)
        with self.assertRaises(ScopeDenied):
            s.call(analyst, "gx_read_artifact", {"ref": xml_ref}, "unassigned-original")
        published = s.call(analyst, "gx_read_artifact", {"ref": reading["report_ref"]}, "read-published")
        self.assertEqual(published["findings"], [citation, counterexample, method])
        candidates = [
            {"text": "Occupancy and displacement criteria remove some shared residues. On the same 2O0O model, "
                     "deposited-chain-geometry@1 should measure fewer positions under ordered_backbone than "
                     "common_backbone. Equal counts would falsify this explanation for these masks.",
             "source_refs": [reading["report_ref"]]},
            {"text": "Every shared complete backbone position passes the order criteria in these coordinates. "
                     "The two masks should retain identical positions. A lower ordered-backbone count would "
                     "falsify this alternative. Compare selected positions using the same union denominator.",
             "source_refs": [reading["report_ref"]]},
        ]
        proposed = s.report(analyst, reading["report_ref"], "publish-alternatives", findings=candidates,
                            gaps=published["gaps"], next_steps=["Measure both masks on the same deposition."])
        last_lead = s.next_actor("run")
        final = s.call(last_lead, "gx_read_artifact", {"ref": proposed["report_ref"]}, "inspect-alternatives")
        self.assertEqual(final["findings"], candidates)
        self.assertEqual(final["author"]["task_id"], analyst.task_id)
        self.assertEqual(len(s.rows("SELECT report_ref FROM v62_module_reports WHERE run_id=?", s.run_id)), 2)
