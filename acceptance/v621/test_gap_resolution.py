"""Public Goal callbacks for question-specific closure of research gaps."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v6_goal


class GapResolution(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        self.s = self.enterContext(Scenario(self.work))
        self.source = self.s.materials[-1]

    def decide(self, key, **fields):
        decision = {**draft(), **fields}
        return self.s.call(self.s.lead, "gx_decide_revision", {"decision": decision}, key)

    def open_gaps(self, gaps, **details):
        opened = self.s.open("inspection", inputs=[self.source], caps=[])
        worker = self.s.bind(opened["task_id"])
        self.decide("yield")
        self.s.call(worker, "gx_read_artifact", {"ref": self.source, "operation_key": "inspect"}, "inspect")
        self.report = self.s.report(worker, self.source, gaps=gaps, **details)
        self.s.next_actor("run")
        opened = self.s.brief(self.s.lead)["research_progress"]["open_gaps"]
        self.decide("await-reading")
        return {g["question"]: g["gap_ref"] for g in opened}

    def record(self, data, source="ambient-station", scope="lab bench", *, target=None):
        artifact = ArtifactService(self.s.conn).publish_bytes(
            namespace=self.s.run_id + "/readings", rel_path=source + ".json", data=json.dumps(data),
            kind="instrument_measurement", run_id=self.s.run_id, actor="operator", origin="INGEST")
        v6_goal.record_observation(self.s.conn, self.s.run_id, artifact.artifact_ref, source=source,
            acquisition={"source": source, "acquisition": source + "/record-1", "applicability": scope,
                         "data_refs": [target or self.source]})
        return artifact.artifact_ref

    def test_unrelated_authorized_observation_cannot_close_two_narrative_gaps(self):
        gaps = self.open_gaps(["What is the calibration drift?", "What is the reference viscosity?"])
        ref = self.record({"schema": "station.1", "ambient_rain_mm": 7})
        self.s.next_actor("run")
        self.assertIn(ref, self.s.brief(self.s.lead)["evidence_refs"])
        try:
            self.decide("try-unrelated", evidence_refs=[ref], resolved_gaps=list(gaps.values()))
        except SchemaInvalid:
            pass
        rows = self.s.rows("SELECT gap_id,status FROM gaps WHERE run_id=? ORDER BY gap_id", self.s.run_id)
        trace = {"run_id": self.s.run_id, "report": self.report, "observation": ref, "gap_states": rows,
                 "resolutions": self.s.rows("SELECT event_id,payload_json FROM events WHERE run_id=? "
                                            "AND kind='v62.gap.resolved'", self.s.run_id)}
        if path := os.environ.get("GX_GAP_REGRESSION_RECEIPT"):
            Path(path).write_text(json.dumps(trace, indent=2) + "\n")
        self.assertEqual([r["status"] for r in rows], ["OPEN", "OPEN"], trace)

    def bench_gaps(self):
        declarations = [{"question": "Drift?", "observable": "/drift", "op": "finite"},
                        {"question": "Reference viscosity?", "observable": "/viscosity", "op": "gt", "threshold": 2}]
        for entry in declarations:
            entry.update(data_ref=self.source, applicability="lab bench", nature="observed",
                         measurement_schema="public.instrument/1")
        return self.open_gaps([g["question"] for g in declarations], gap_criteria=declarations)

    def test_only_applicable_question_resolves_and_batch_is_atomic(self):
        gaps = self.bench_gaps()
        ref = self.record({"schema": "public.instrument/1", "drift": 0.25})
        self.s.next_actor("run")
        with self.assertRaises(SchemaInvalid) as rejected:
            self.decide("attempt-both", evidence_refs=[ref], resolved_gaps=list(gaps.values()))
        result = {r["gap_ref"]: r for r in rejected.exception.details["gap_results"]}
        self.assertEqual(result[gaps["Drift?"]]["reason"], "matched")
        self.assertEqual(result[gaps["Reference viscosity?"]]["rejections"][0]["reason"], "missing_observable_or_schema")
        if path := os.environ.get("GX_GAP_REFUSAL_RECEIPT"):
            record = self.s.rows("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                 self.s.lead.thread, "attempt-both")
            Path(path).write_text(json.dumps({"run_id": self.s.run_id, "report": self.report, "evidence_ref": ref,
                "turn_id": self.s.lead.turn, "lease_id": self.s.lead.lease_id,
                "refusal": json.loads(record[0]["result_json"])["decision_refusal"],
                "resolution_events": self.s.rows("SELECT event_id FROM events WHERE kind='v62.gap.resolved'"),
                "paid_model_calls": 0}, indent=2) + "\n")
        self.assertEqual([r["status"] for r in self.s.rows("SELECT status FROM gaps")], ["OPEN", "OPEN"])
        receipt = self.decide("only-drift", evidence_refs=[ref], resolved_gaps=[gaps["Drift?"]])
        self.assertEqual(receipt["gap_resolutions"][0]["matches"][0]["value"], 0.25)
        fresh = self.next_progress()
        self.assertEqual([g["question"] for g in fresh["open_gaps"]], ["Reference viscosity?"])
        self.assertEqual(len(fresh["resolved_gaps"]), 1)
        self.assertEqual(fresh["resolved_gaps"][0]["decision_ref"], receipt["decision_ref"])

    def next_progress(self):
        self.record({"signal": True}, source="new-note")
        self.s.next_actor("run")
        return self.s.brief(self.s.lead)["research_progress"]

    def test_two_fields_of_one_verified_observation_survive_reopen_and_replay(self):
        gaps = self.bench_gaps()
        ref = self.record({"schema": "public.instrument/1", "drift": 0.3, "viscosity": 3.7})
        self.s.next_actor("run")
        kwargs = dict(evidence_refs=[ref], resolved_gaps=list(gaps.values()))
        receipt = self.decide("both", **kwargs)
        self.assertEqual(self.decide("both", **kwargs), receipt)
        self.s.stack.close()
        with Scenario(self.work, reopen=True) as resumed:
            events = [json.loads(r["payload_json"]) for r in resumed.rows(
                "SELECT payload_json FROM events WHERE kind='v62.gap.resolved' ORDER BY event_id")]
            self.assertEqual(len(events), 2)
            self.assertEqual({r["gap_ref"] for r in events}, set(gaps.values()))
            self.assertEqual({r["matches"][0]["value"] for r in events}, {0.3, 3.7})
            self.assertTrue(all(r["matches"][0]["artifact_ref"] == ref and
                                r["criterion"]["data_ref"] == self.source and
                                r["decision_ref"] == receipt["decision_ref"] for r in events))
            self.assertEqual(len(resumed.rows("SELECT * FROM jobs")), 0)
            if path := os.environ.get("GX_GAP_MATCH_RECEIPT"):
                Path(path).write_text(json.dumps({"run_id":resumed.run_id, "report":self.report,
                    "decision":receipt, "resolution_events":events, "jobs":0, "paid_model_calls":0}, indent=2) + "\n")

    def test_reading_for_another_material_or_scope_cannot_answer_this_question(self):
        gaps = self.bench_gaps()
        both = {"schema": "public.instrument/1", "drift": 1, "viscosity": 3}
        wrong_sample = self.record(both, target=self.s.materials[0], source="other-material")
        wrong_scope = self.record(both, source="other-scope", scope="outdoor bench")
        below = self.record({**both, "viscosity": 2}, source="below")
        self.s.next_actor("run")
        with self.assertRaises(SchemaInvalid) as rejected:
            self.decide("mismatches", evidence_refs=[wrong_sample, wrong_scope, below],
                        resolved_gaps=[gaps["Reference viscosity?"]])
        [gap] = rejected.exception.details["gap_results"]
        self.assertEqual({r["reason"] for r in gap["rejections"]},
                         {"different_material", "different_scope", "threshold_not_met"})
        self.assertEqual(self.s.rows("SELECT event_id FROM events WHERE kind='v62.gap.resolved'"), [])

    def test_old_measurement_redelivered_under_a_new_name_does_not_resolve(self):
        body = {"schema": "public.instrument/1", "drift": 0.31}
        self.record(body, source="first-record")
        self.decide("receive-first-record")
        self.s.next_actor("run")
        gaps = self.bench_gaps()
        copy = self.record(body, source="duplicate-bytes")
        self.s.next_actor("run")
        with self.assertRaises(SchemaInvalid) as rejected:
            self.decide("duplicate", evidence_refs=[copy], resolved_gaps=[gaps["Drift?"]])
        [failed] = rejected.exception.details["gap_results"]
        self.assertEqual(failed["rejections"][0]["reason"], "already_available")

    def test_scope_fence_rejects_unpublished_readout_and_unauthorized_gap(self):
        from gx_core.errors import ScopeDenied
        gaps = self.bench_gaps()
        ref = self.record({"schema": "public.instrument/1", "drift": 0.25})
        # Published to GX after the founder's previous context. No new task was bound.
        self.s.next_actor("run")
        private = ArtifactService(self.s.conn).publish_bytes(
            namespace=self.s.run_id + "/private", rel_path="control.json", data='{"drift":0.25}',
            kind="instrument_measurement", actor="operator", origin="INGEST", run_id=self.s.run_id)
        with self.assertRaises(ScopeDenied):
            self.decide("unpublished", evidence_refs=[private.artifact_ref], resolved_gaps=[gaps["Drift?"]])
        with self.assertRaises(ScopeDenied):
            self.decide("other-gap", evidence_refs=[ref], resolved_gaps=["gap:other-study"])
        self.assertEqual(self.s.rows("SELECT event_id FROM events WHERE kind='v62.gap.resolved'"), [])
