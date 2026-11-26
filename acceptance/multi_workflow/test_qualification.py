"""Imported count screens date the disclosed count, independently of later fitted endpoints."""
from contextlib import redirect_stdout
import json
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.science_execution.test_signed_native import METHOD, PARAMETERS, signed_claim
from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import branch, draft
from adapters.scientific import coordinate_equivalence as coordinates
from gx_core.artifacts import ArtifactService
from gx_core.p0 import cli, v622_sources


EXAMPLE = Path(__file__).resolve().parents[2] / "examples/pcna"


class QualifiedCounts(unittest.TestCase):
    def test_screened_fields_are_bound_to_the_original_mask_and_reader(self):
        definition = {"output_name": "geometry.json", "metric_id": "coverage", "input_role": "structure",
                      "parameters": {"mask": "common_ca"}}
        field = coordinates.screen_column(definition, coordinates.GEOMETRY_BUNDLE)
        for folder, digest_key in (("data", "sha256"), ("open-data", "coordinate_sha256")):
            doc = json.loads((EXAMPLE/folder/"qualification.json").read_bytes())
            expected = [m[digest_key] for m in doc["materials"]]
            self.assertEqual(list(coordinates.screened_originals(doc, "common_ca", field)), expected)
            self.assertEqual(list(coordinates.screened_originals(doc, "ordered_backbone", field)), [])
            del doc["materials"][0]["masks"]["common_ca"]["observed_union_positions"]
            self.assertEqual(list(coordinates.screened_originals(doc, "common_ca", field)), expected[1:])
        self.assertEqual(list(v622_sources.screening_availability(None, {"capability": METHOD}, {}, {})), [])
        self.assertIsNone(coordinates.screen_column(definition, None))
        self.assertIsNone(coordinates.screen_column(dict(definition, metric_id="max_angle_deg"), coordinates.GEOMETRY_BUNDLE))
        self.assertIsNone(coordinates.screen_column(dict(definition, measurement_schema="unrelated"), coordinates.GEOMETRY_BUNDLE))

    def test_open_goal_known_count_and_unseen_signed_endpoint_share_one_job(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), \
                redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(work/"state"), "--run-key", "qualification",
                                       "--goal-file", str(EXAMPLE/"open-question.md"),
                                       "--goal-config", str(EXAMPLE/"open.json")]), 0)
        s = self.enterContext(CloseStudy(work, reopen=True))
        s.next_actor("run")
        original, _, _, metadata = s.materials
        screen = s.call(s.lead, "gx_read_artifact", {"ref": metadata}, "read-count-screen")
        count = screen["materials"][0]["masks"][PARAMETERS["mask"]]["eligible_20_position_blocks_by_count"]
        screen_event, = s.conn.execute("SELECT event_id FROM events WHERE subject_id=? AND kind='artifact.published'",
                                       (metadata,)).fetchone()
        refs = []
        for metric, unit, threshold in (("blocks_eligible", "blocks", count), ("missed_present", "1", 1)):
            claim = signed_claim(original)
            claim.update(statement=f"Check the registered {metric} on this original.", scope="{{material_scope}}",
                         evidence_refs=[original, metadata], limitations="This deposition and common CA only.")
            claim["prediction"].update(observable="/metrics/" + metric, threshold=threshold)
            claim["prediction"]["method"].update(metric_id=metric, unit=unit)
            refs.append(s.call(s.lead, "gx_stage_claim", claim, "stage-" + metric)["claim_ref"])
        route = branch("route")
        route["budget_limits"]["cpu_seconds"] = 120
        node = s.node(s.lead, "check", original, branch_id="local:route", capability=METHOD, parameters=PARAMETERS)
        adopted = s.call(s.lead, "gx_decide_revision", {"decision": draft(route, node, evidence=[original, metadata, *refs],
                        watches=[{"event": "job_settled", "subject_ref": "local:check"}])}, "adopt")
        job = s.settle(adopted["aliases"]["local:check"])
        summary = ArtifactService(s.conn).read_json(job["result_ref"])
        self.assertEqual(summary["blocks_eligible"], count)
        claims = {c["prediction"]["method"]["metric_id"]: c["maturity"] for c in s.brief(s.next_actor("run"))["claims"]}
        known, = claims["blocks_eligible"]["material_coverage"]
        self.assertTrue(known["known_at_registration"])
        self.assertEqual((known["endpoint_first_available_event_id"], known["qualification"]), (screen_event, "retrospective"))
        self.assertFalse(claims["blocks_eligible"]["results"])
        fresh, = claims["missed_present"]["material_coverage"]
        self.assertFalse(fresh["known_at_registration"])
        self.assertEqual(fresh["qualification"], "prospective")
        self.assertGreater(fresh["endpoint_first_available_event_id"], fresh["registration_event_id"])
        self.assertEqual(claims["missed_present"]["results"][0]["value"], summary["missed_present"])
