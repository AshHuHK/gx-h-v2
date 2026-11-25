"""Public negative controls for the new-pair review and admission boundaries. No inference."""
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pair
import score


def public_turn(i, text=None, receipt="report-a", actual=True):
    source = {"artifact_ref": "measured", "sha256": "test-result", "scoped_operations": []}
    reports = []
    if text:
        reports = [{"ref": receipt, "sources": [source], "delivered": {"report_ref": receipt,
                    "findings": [{"text": text, "source_refs": ["measured"]}], "gaps": []}}]
    goal = "Measure using the originally selected sample."
    return {"thread_id": "public-thread-" + str(i), "turn_id": str(i), "scope": "run", "brief_sha256": str(i),
            "frozen_brief": "GX research brief\n" + json.dumps({"goal": goal, "observations": []}),
            "reports": reports, "decision": {"rationale": goal + " The observed coverage determines the next step.",
            "new_jobs": ["changed-job"] if actual else [], "withdrawn": [], "preplanned_jobs": [],
            "cited": [receipt] if text else [], "ops": [], "base": {"version": 1}}}


def review_for(evidence, marked):
    packet = score.brief_packet(evidence)
    return {"reviewer": "reader", "packet_sha256": pair.digest(packet), "windows": [
        {"window": w["window"], "report_ref": mark[0] if mark else None, "quote": mark[1] if mark else "",
         "reason": "Public unit control."} for w, mark in zip(packet["windows"], marked)]}


class TestPair(unittest.TestCase):
    def test_unknown_or_reference_only_is_not_an_opportunity(self):
        evidence = {"turns": [public_turn(1)], "missing": []}
        with self.assertRaisesRegex(ValueError, "absent"):
            score.lock_opportunities(evidence, review_for(evidence, [("report-a","known")]), "reader")
        evidence["turns"][0]["reports"] = [{"ref": "report-a", "sources": [], "delivered": {"report_ref": "report-a"}}]
        with self.assertRaisesRegex(ValueError, "matching report text"):
            score.lock_opportunities(evidence, review_for(evidence, [("report-a","known")]), "reader")

    def test_republished_source_and_genuinely_new_statistic_from_same_file(self):
        evidence = {"turns": [public_turn(1, "Too few positions."),
                              public_turn(2, "Too few positions.", receipt="report-b")], "missing": []}
        marks = [None, ("report-b", "Too few positions")]
        with self.assertRaisesRegex(ValueError, "already"):
            score.lock_opportunities(evidence, review_for(evidence, marks), "reader")
        earlier = "The source table declares 100 expected rows. The observed count has not been reported."
        evidence = {"turns":[public_turn(1, earlier),
                    public_turn(2, "Only 30 rows were observed, below the prespecified 50.", receipt="report-b")], "missing":[]}
        review = review_for(evidence, [None, ("report-b", "Only 30 rows were observed")])
        with self.assertRaisesRegex(ValueError, "Compare the earlier"):
            score.lock_opportunities(evidence, review, "reader")
        review["windows"][1]["comparison"] = {"earlier_text":earlier, "earlier_contained_endpoint":False,
                                               "difference":"The observed row count first appeared here."}
        self.assertTrue(score.lock_opportunities(evidence, review, "reader")["windows"][1]["is_new"])

    def test_change_maintain_unsupported_alternative_and_automatic(self):
        for condition in ("change", "maintain"):
            evidence = {"turns": [public_turn(1), public_turn(2, "Coverage is documented.")], "missing": []}
            sealed = score.lock_opportunities(evidence, review_for(evidence, [None, ("report-a", "Coverage is documented")]), "reader")
            judgments = [{"window": k["window"], "disposition": "NO_OPPORTUNITY", "reason": "Opening packet.",
                          "unsupported_support": False} for k in sealed["windows"]]
            last = judgments[-1]
            last.update(disposition=condition.upper(), scope_appropriate=True, rationale_excerpt="observed coverage",
                        prior_plan_excerpt="originally selected sample", accepted_action="UNLISTED",
                        alternative_reason="Independent public test of an alternative.")
            review = {"reviewer": "adjudicator", "dossier_sha256": "public", "windows": judgments}
            answer = {"condition": condition, "acceptable_actions": ["Prespecified approach"]}
            check = lambda: score.adjudicate(evidence, sealed, review, answer, "adjudicator", "public")
            self.assertTrue(check()["passed"])
            self.assertTrue(check()["windows"][-1]["unlisted"])
            evidence["turns"][-1]["decision"]["cited"] = []
            with self.assertRaisesRegex(ValueError, "citation"):
                check()
            evidence["turns"][-1]["decision"]["cited"] = ["report-a"]
            judgments[0]["unsupported_support"] = True
            self.assertTrue(check()["stop_unsupported"])
            self.assertFalse(check()["passed"])
            judgments[0]["unsupported_support"] = False
            evidence["turns"][-1]["decision"].update(new_jobs=[], preplanned_jobs=["old-job"])
            last["disposition"] = "AUTOMATIC_ONLY"
            self.assertFalse(check()["passed"])
            self.assertFalse(check()["strict_cited_work_change"])
            if condition == "change":
                last["disposition"] = "CHANGE"
                with self.assertRaisesRegex(ValueError, "verified follow-up"):
                    check()
                evidence["turns"][-1]["decision"]["commissioned_jobs"] = ["actually-executed-new-module-job"]
                self.assertTrue(check()["passed"])

    def test_scoped_assignment_requires_parent_delivery_real_method_and_matching_method_parameters(self):
        parent = public_turn(1)
        parent.update(delivered_at="1", commissions=[{"call_id":"open"}],
                      frozen_brief="GX research brief\n" + json.dumps({"observations":[
                          {"artifact_ref":"raw", "observation":{"text":"opaque assignment"}}]}))
        evidence = {"turns":[parent], "intervention_event":3, "assignment_ref":"raw", "assignment_text":"opaque assignment"}
        trace = {"kind":"job", "event_id":6, "settled_event":8, "capability":"public-test@1",
                 "parameters":{"mask":"allocated"}, "input_sha256":["original"], "output":{"output_name":"coverage", "nature":"analysis"}}
        report = {"author_kind":"MODULE","module_open_event":5,"published_event":10,"parent_window":[parent["thread_id"],"1"],
                  "parent_call":"open","sources":[{"artifact_ref":"obs","scoped_operations":[trace]}]}
        self.assertEqual(score.assignment_traces(evidence, report, ["obs"]), [trace])
        rule = {"kind":"job","capability":"public-test@1","parameters":{"mask":"allocated"},"input_sha256":"original",
                "output_name":"coverage","nature":"analysis"}
        self.assertTrue(score.matches_assay(trace, rule))
        self.assertFalse(score.matches_assay(dict(trace, output={"output_name":"other","nature":"model_prediction"}), rule))
        self.assertFalse(score.matches_assay(trace, dict(rule, parameters={"mask":"other"})))
        self.assertFalse(score.assignment_traces(evidence, report, ["another"]))
        self.assertFalse(score.assignment_traces(evidence, dict(report, author_kind="GX"), ["obs"]))
        self.assertFalse(score.assignment_traces(dict(evidence, intervention_event=7), report, ["obs"]))
        self.assertFalse(score.assignment_traces(dict(evidence, assignment_text="other"), report, ["obs"]))

    def test_old_packets_remain_exposed_on_move_or_wrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp)
            case = inputs / "cases/01.json"
            case.parent.mkdir()
            old = pair.ROOT / "acceptance/v61/adaptive/controls_cases/h81.json"
            case.write_bytes(old.read_bytes())
            with self.assertRaisesRegex(ValueError, "exposed"):
                pair.case_file(inputs, "01")
            case.write_text(json.dumps({"version":"relocated","intervention":{
                            "kind":"supplement","text":old.read_text()}}))
            with self.assertRaisesRegex(ValueError, "previously exposed"):
                pair.case_file(inputs, "01")

    def test_unfunded_and_self_planted_result_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, "no spending authorization"):
            pair.gate({"funding":None}, None, "01", "test", None)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            pair.save_new(work / "01/score.json", {"passed":True})
            summary = pair.status(work)
            self.assertEqual((summary["admitted"], summary["passed"], summary["science_status"]), (0, 0, "NOT_RUN"))
            with self.assertRaisesRegex(ValueError, "independent reviewer"):
                pair.verified_score(work, "01", {}, "registration", "different")
            with self.assertRaises(FileExistsError):
                pair.save_new(work / "01/score.json", {"passed":True})
        self.assertEqual(pair.status()["science_status"], "NOT_RUN")

    def test_scripted_session_cannot_satisfy_live_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            pair.save_new(w / "01/terminal.json", {"run_id":"fixture", "status":"CLOSED"})
            pair.save_new(w / "01/admission.json", {"registration_sha256":"pin", "slot":"01"})
            pair.save_new(w / "01/preflight.json", {"account_type":"chatgpt", "ready":True, "provider":"openai"})
            fake = {"turns":[{"native":{"model_provider":"gx_loopback","model":pair.PROTOCOL["model"],
                                       "effort":pair.PROTOCOL["model_effort"]}}]}
            with patch.object(pair, "extract", return_value=fake), self.assertRaisesRegex(ValueError, "scripted"):
                pair.observe(w, "01", {}, "pin")

    def test_two_public_openings_match_before_any_inference(self):
        example = pair.ROOT / "examples/v62a-data-inventory/grant.json"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            inputs = work / "public"
            inputs.mkdir()
            (inputs / "goal.md").write_text("Compare the coverage of the two public coordinate deposits.")
            grant = pair.read(example)
            grant["research_effort"] = "deep"
            for item in grant["input_files"]:
                item["path"] = str((example.parent / item["path"]).resolve())
            pair.save_new(inputs / "grant.json", grant)
            shared = pair.common(inputs)
            a, b = [pair.prepare_slot(work / slot, inputs, shared) for slot in pair.SLOTS]
            self.assertEqual(a["initial_conditions_sha256"], b["initial_conditions_sha256"])
            self.assertEqual(a["tools_sha256"], b["tools_sha256"])
            self.assertNotEqual(a["brief_sha256"], b["brief_sha256"])
            self.assertEqual(pair.status(work)["admitted"], 0)
            from gx_core.p0.profiles import proxy_settings
            folder = work / "01"
            env = dict(HOME=str(folder / "seed-home"), PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                       GX_P0_PROFILE=str(folder / "seed-home/profile.json"), GX_CODEX_OFFLINE="1",
                       PYTHONDONTWRITEBYTECODE="1", **proxy_settings())
            case = folder / "public-supplement.json"
            pair.save_new(case, {"kind":"supplement", "text":"Inspect deposited public source A for this demonstration."})
            mid = pair.message_id("public-test", "01")
            result = pair.submit_assignment(env, folder, a["run_id"], mid, case)
            replay = pair.submit_assignment(env, folder, a["run_id"], mid, case)
            self.assertEqual(result, replay)
            with closing(pair.database(folder)) as db:
                observations = db.execute("SELECT payload_json FROM events WHERE run_id=? AND subject_id=? "
                                          "AND kind='v6.observation.added'", (a["run_id"], result["source_id"])).fetchall()
                self.assertEqual([pair.parse(r[0])["artifact_ref"] for r in observations], [result["artifact_ref"]])
                self.assertEqual(pair.opening(db, a["run_id"]), a)


if __name__ == "__main__":
    unittest.main()
