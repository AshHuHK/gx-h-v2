"""Public PI ingress and report export, with controlled official Main and Lead callbacks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT
from acceptance.v622.test_close import CloseStudy, close_decision
from gx_core.errors import SchemaInvalid
from gx_core.p0.driver import Driver
from tests.test_v6_operator_recovery import HumanReceipt


class ReportLanguage(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"

    def cli(self, command, *options):
        run = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), command,
                              "--state", str(self.work / "state"), "--run-id", self.rid, *options],
                             env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def main(self, study, mid, text):
        self.cli("message", "--message-id", mid, "--text", text, "--queue-only")
        driver = Driver(study.conn, study.state, study.run_id)
        self.addCleanup(driver.close)
        row = study.conn.execute("SELECT * FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
        driver._human_turn(HumanReceipt(), row)
        envelope = {"thread_id": "test-native-main", "turn_id": "test-turn-" + mid}
        return lambda tool, args: driver.main_read(tool, args, envelope)

    def register(self, study, mid, text, persistence):
        main = self.main(study, mid, text)
        return main("gx_route_pi_message", {"clauses": [{"kind": "preference", "quote": text,
                                                         "persistence": persistence}]})

    def test_english_goal_with_preseal_preference_exports_the_leads_chinese_conclusion(self):
        with CloseStudy(self.work) as study:
            self.rid = study.run_id
            self.register(study, "report-language", "以后报告用中文。", "conversation")
            self.register(study, "reply-language", "Explain this reply in English.", "turn")
            decision = close_decision([study.materials[0]])
            with self.assertRaises(SchemaInvalid) as english:
                study.call(study.lead, "gx_decide_revision", {"decision": decision}, "english-close")
            self.assertEqual(english.exception.details["reason"], "report_language")
            decision["rationale"] = "两种解释均与当前原件相容，无法判定哪种解释更好。"
            op = decision["ops"][0]
            op.update(reason="本次按已交付的原件结题。", unresolved=["样本之间的独立性未知。"])
            accepted = study.call(study.lead, "gx_decide_revision", {"decision": decision}, "chinese-close")
            study.advance()
            material = study.materials[0]
        publication = self.cli("research")
        page = Path(publication["report"]).read_text()
        result = json.loads(Path(publication["result"]).read_bytes())
        for phrase in ["# 研究结论", op["reason"], decision["rationale"], op["unresolved"][0]]:
            self.assertIn(phrase, page)
        self.assertEqual(result["presentation"]["source_message_id"], "report-language")
        self.assertEqual(result["final_decision"]["decision_ref"], accepted["decision_ref"])
        self.assertIn(material, result["final_decision"]["evidence_refs"])
        self.assertEqual(publication["invocation"]["added"], {"jobs": 0, "host_turns": 0})
        self.assertEqual(Path(self.cli("research")["report"]).read_text(), page)

    def test_measured_brief_and_separate_sealed_reader_keep_the_same_output_and_decision(self):
        from acceptance.v621.test_verified_metrics import METHOD, SOURCE_METADATA
        from acceptance.v623 import test_close_metrics
        from acceptance.v62a.interleavings_fixture import draft
        from tests.test_v6_decisions import branch
        from gx_core.p0 import v6_goal

        config = {"research_effort": "deep", "research_modules": [], "capabilities": [METHOD],
                  "plugins": ["org.gx.protein@0.3.0"]}
        with CloseStudy(self.work, spec_updates=config) as s:
            self.rid = s.run_id
            raw = s.materials[0]
            v6_goal.record_observation(s.conn, s.run_id, raw, source="archive",
                                       acquisition=json.loads(SOURCE_METADATA.read_text()))
            node = s.node(s.lead, "coverage", raw, branch_id="local:source", capability=METHOD,
                          parameters={"mask": "common_backbone"})
            route = branch("source")
            route["budget_limits"]["cpu_seconds"] = 120
            dispatch = draft(route, node, watches=[{"event":"job_settled","subject_ref":"local:coverage"}])
            admitted = s.call(s.lead, "gx_decide_revision", {"decision": dispatch}, "launch")
            self.register(s, "report-zh", "以后报告用中文。", "conversation")
            job = s.settle(admitted["aliases"]["local:coverage"])
            output = job["result_ref"]
            lead = s.next_actor("run")
            self.assertEqual(s.brief(lead)["report_presentation"]["language"], "zh")
            page = test_close_metrics.EndpointClose.read_page(self, s, lead, output, 0, "read-measurement")
            row, = [r for r in page["endpoint_rows"] if r["id"] == "coverage"]
            decision = close_decision([raw, output])
            decision["metric_refs"] = [output + "#" + row["key"]]
            decision["rationale"] = "坐标覆盖为 {{metric:coverage}}，仅据该沉积结构作判断。"
            decision["ops"][0].update(reason="根据具名坐标读数结题。", unresolved=["采样独立性未知。"])
            accepted = s.call(lead, "gx_decide_revision", {"decision": decision}, "adopt")
            s.advance()

        pub = self.cli("research")
        canonical = Path(pub["report"]).read_bytes(), Path(pub["result"]).read_bytes()
        result = json.loads(canonical[1])
        reading, = result["final_decision"]["used_endpoints"]
        self.assertEqual(reading["source_ref"], output)
        self.assertEqual(reading["reading"]["unit"], "1")
        measured = reading["reading"]["display"]
        self.assertIn(measured, canonical[0].decode())
        self.assertIn(raw, result["final_decision"]["evidence_refs"])
        self.assertEqual(result["final_decision"]["rationale"], decision["rationale"])
        mid, quote = "sealed-reader", "Please translate the sealed report into English."
        with CloseStudy(self.work, reopen=True) as s:
            main = self.main(s, mid, quote)
            source = main("gx_project_state", {})["pi_message"]["sealed_report"]
            self.assertEqual(source["decision_ref"], accepted["decision_ref"])
            self.assertIn(measured, source["rationale"])
            translation = {"reason": "Close on the named coordinate measurement.",
                           "rationale": f"Coverage is {measured}; the interpretation applies to this deposited structure.",
                           "unresolved": ["Sampling independence remains unknown."]}
            clause = {"kind": "preference", "quote": quote, "persistence": "turn", "report_language": "en"}
            def save_reader(reader):
                return main("gx_route_pi_message", {"clauses": [{**clause, "reader": reader}]})
            with self.assertRaises(SchemaInvalid):
                save_reader({**translation, "rationale": translation["rationale"] + " The coverage was 100%."})
            good = save_reader(translation)
            self.assertEqual(save_reader(translation), good)
        again = self.cli("research")
        self.assertEqual((Path(again["report"]).read_bytes(), Path(again["result"]).read_bytes()), canonical)
        self.assertEqual(again["invocation"]["added"], {"jobs": 0, "host_turns": 0})
        derived, = again["readers"]
        self.assertEqual(derived["source_message_id"], mid)
        reader = Path(derived["file"]).read_text()
        for item in [accepted["decision_ref"], translation["reason"], translation["rationale"],
                     translation["unresolved"][0], "(REPORT.md)", "(result.json)", raw, output]:
            self.assertIn(item, reader)

        if path := os.getenv("GX_REPORT_LANGUAGE_RECEIPT"):
            Path(path).write_text(json.dumps({
                "transport": "controlled Main/Lead callbacks through GX; real deposited-coordinate CPU Job",
                "provider_calls": 0, "run_id": self.rid, "goal": result["goal"], "preference": result["presentation"],
                "first_report": canonical[0].decode(), "job_id": job["job_id"], "raw_ref": raw, "output_ref": output,
                "endpoint": {"ref": reading["metric_ref"], "event": reading["event_id"], "unit": reading["reading"]["unit"],
                             "display": measured}, "decision_ref": accepted["decision_ref"],
                "closing_decision_event": result["closure"]["committed_event"], "closed_event": result["closure"]["closed_event"],
                "reader": reader, "reader_message_id": mid, "scientific_records_unchanged": True,
                "warm_export_added": again["invocation"]["added"]}, indent=2, ensure_ascii=False) + "\n")

    def test_reader_rejects_lost_limitations_or_altered_quantities_and_physical_units(self):
        from gx_core.p0.report_language import verify_reader
        original = {"reason": "Record the result.", "rationale": "The range was 0.24 Å.",
                    "unresolved": ["Comparability over 25 °C was not tested."]}
        valid = {"reason": "按此结果结题。", "rationale": "极差为 0.24 Å。",
                 "unresolved": ["超过 25 °C 的可比性未验证。"]}
        verify_reader(original, valid, "zh")
        for wrong in [{"rationale": "极差为 0.24 nm。"}, {"rationale": "极差为 0.42 Å。"},
                      {"unresolved": []}]:
            with self.assertRaises(SchemaInvalid):
                verify_reader(original, {**valid, **wrong}, "zh")
