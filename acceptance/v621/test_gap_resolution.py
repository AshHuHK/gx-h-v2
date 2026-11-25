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

    def open_gaps(self, gaps):
        opened = self.s.open("inspection", inputs=[self.source], caps=[])
        worker = self.s.bind(opened["task_id"])
        self.decide("yield")
        self.s.call(worker, "gx_read_artifact", {"ref": self.source, "operation_key": "inspect"}, "inspect")
        self.report = self.s.report(worker, self.source, gaps=gaps)
        self.s.next_actor("run")
        opened = self.s.brief(self.s.lead)["research_progress"]["open_gaps"]
        self.decide("await-reading")
        return {g["question"]: g["gap_ref"] for g in opened}

    def record(self, data, source="ambient-station", scope="lab bench"):
        artifact = ArtifactService(self.s.conn).publish_bytes(
            namespace=self.s.run_id + "/readings", rel_path=source + ".json", data=json.dumps(data),
            kind="instrument_measurement", run_id=self.s.run_id, actor="operator", origin="INGEST")
        v6_goal.record_observation(self.s.conn, self.s.run_id, artifact.artifact_ref, source=source,
            acquisition={"source": source, "acquisition": source + "/record-1", "applicability": scope})
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
