"""Exercise publication through the Goal, acquirer, native decision and operator endpoints."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v6_claims


ROOT = Path(__file__).resolve().parents[2]
METRIC = "public.thermometer.celsius/1"
SCOPE = "Morning samples from the north station"
BASE = dict(statement="The observed morning temperature exceeds ten degrees Celsius.", scope=SCOPE,
            criteria="Temperature in Celsius exceeds ten.", limitations="North station mornings only.",
            rationale="Audit the original proposition against the registered endpoint.")


class ClaimPublication(unittest.TestCase):
    def start(self, policy=None):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        plan = self.folder / "station.json"
        plan.write_text('{"survey":"north-morning"}')
        config = {"research_effort": "deep", "research_modules": [], "capabilities": [], "plugins": [],
                  "input_files": [{"role": "material", "path": str(plan)}]}
        if policy is not None:
            config["evidence_policy"] = policy
        self.gx = self.enterContext(Scenario(self.folder / "goal", spec_updates=config))
        raw = self.gx.materials[0]
        self.rule = {"data_refs": [raw], "observable": "/measurement/value", "operator": "gt", "threshold": 10,
                     "target": "experimental_observation", "measurement_schema": METRIC}
        self.initial = self.adopt(self.gx.lead, "initial", [raw], "open", prediction=self.rule)

    def cli(self, verb, *args):
        argv = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), verb, "--state", str(self.gx.state),
                "--run-id", self.gx.run_id, *map(str, args)]
        return json.loads(subprocess.check_output(argv, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"}))

    def observe(self, name, body, data=None, scope=SCOPE):
        src, meta = self.folder / (name + ".json"), self.folder / (name + "-acquisition.json")
        src.write_text(json.dumps(body))
        metadata = {k: name + "-" + k for k in ("data", "acquisition", "sample", "source", "sampling")}
        metadata.update(publication=[], processing="calibrated thermometer", applicability=scope,
                        data_refs=self.gx.materials[:1] if data is None else data)
        meta.write_text(json.dumps(metadata))
        return self.cli("observe", "--source", name, "--input", src, "--acquisition", meta)["artifact_ref"]

    def reading(self, name, value):
        return self.observe(name, {"schema": METRIC, "measurement": {"value": value}})

    def adopt(self, actor, key, refs, level, **fields):
        staged = self.gx.call(actor, "gx_stage_claim", {**BASE, "level": level, "evidence_refs": refs, **fields}, key)
        decision = self.gx.call(actor, "gx_decide_revision",
                                {"decision": draft(evidence=[*refs, staged["claim_ref"]])}, "adopt-" + key)
        return decision["claim_revisions"][0]

    def delivered_claim(self):
        lead = self.gx.next_actor("run")
        row = self.gx.conn.execute("SELECT brief FROM p0_roots WHERE lease_id=?", (lead.lease_id,)).fetchone()
        brief = json.loads(row[0][row[0].index("{"):])
        return lead, brief["claims"][0], hashlib.sha256(row[0].encode()).hexdigest()

    def trace(self, case, result, brief_sha=None):
        folder = os.getenv("GX_V621_CLAIM_RECEIPTS")
        if folder:
            evidence = {key: result[key] for key in ("claim_ref", "claim_revision_id", "decision_id", "context_ref",
                        "assessment_cursor", "availability_cursor", "science", "publication") if key in result}
            evidence.update(publication_revision_sha256=ids.sha256_of_obj(result), delivered_brief_sha256=brief_sha,
                            run_id=self.gx.run_id, prior_registration=self.initial["maturity"]["registration"],
                            measured_results=result["maturity"]["results"], events=self.gx.rows(
                                "SELECT event_id,kind,subject_id FROM events WHERE run_id=? AND kind IN "
                                "('v6.claim.adopted','v6.observation.added','v62.claim.reassessed') ORDER BY event_id",
                                self.gx.run_id), paid_model_calls=0)
            folder_path = Path(folder)
            folder_path.mkdir(parents=True, exist_ok=True)
            (folder_path / (case + ".json")).write_text(json.dumps(evidence, indent=2) + "\n")

    def test_adverse_registered_reading_is_published_consistently(self):
        self.start()
        observed = self.reading("adverse", 7)
        lead, previous, _ = self.delivered_claim()
        self.assertEqual(("tested", "opposed"), (previous["maturity"]["stage"], previous["maturity"]["direction"]))
        published = self.adopt(lead, "propose-support", [observed], "limited_support",
                               previous_claim_ref=previous["claim_revision_id"])
        operator, = self.cli("status")["claims"]
        self.assertEqual(operator["claim_revision_id"], published["claim_revision_id"])
        self.assertEqual(["contested"] * 3, [published["level"], published["science"]["level"], operator["level"]],
                         msg=f"GX tested/opposed became {published['science']['level']} in public results")
        self.assertEqual(published["publication"]["proposed_level"], "limited_support")
        self.assertEqual(published["publication"]["opposing_refs"], [observed])
        self.assertEqual(published["lead_interpretation"]["rationale"], BASE["rationale"])
        self.assertIn("exceeds the opposed evidence", published["rationale"])
        self.assertEqual(v6_claims.read_revision(self.gx.conn, self.gx.run_id, self.initial["claim_revision_id"]),
                         self.initial)
        self.observe("schedule", {"schedule": "second review"}, data=[])
        _, delivered, brief_sha = self.delivered_claim()
        self.assertEqual(published["publication"], delivered["publication"])
        self.trace("opposed", published, brief_sha)

    def test_new_mixed_reading_withdraws_support_without_overwriting_original_receipt(self):
        self.start()
        warm = self.reading("warm", 14)
        lead, previous, _ = self.delivered_claim()
        supported = self.adopt(lead, "qualified-support", [warm], "limited_support",
                               previous_claim_ref=previous["claim_revision_id"])
        self.assertEqual(("limited_support", "supported"), (supported["level"], supported["publication"]["direction"]))
        self.trace("supported", supported)
        cool = self.reading("cool", 7)
        _, revised, brief_sha = self.delivered_claim()
        self.assertEqual(("mixed", "mixed", "limited_support"),
                         (revised["level"], revised["publication"]["direction"], revised["publication"]["proposed_level"]))
        self.assertEqual(([warm], [cool]), (revised["publication"]["supporting_refs"], revised["publication"]["opposing_refs"]))
        self.assertEqual(revised["publication"], self.cli("status")["claims"][0]["publication"])
        self.assertEqual(supported, v6_claims.read_revision(self.gx.conn, self.gx.run_id, supported["claim_revision_id"]))
        self.trace("mixed", revised, brief_sha)

    def test_changed_endpoint_cannot_borrow_old_refutation_and_narrow_revision_survives(self):
        self.start()
        observed = self.reading("adverse", 7)
        lead, previous, _ = self.delivered_claim()
        self.assertEqual("opposed", previous["maturity"]["direction"])
        with self.assertRaises(SchemaInvalid):
            self.gx.call(lead, "gx_stage_claim", {**BASE, "scope": "Every station on Earth",
                         "level": "refuted", "evidence_refs": [observed]}, "overbroad")
        changed = self.adopt(lead, "changed-endpoint", [observed], "refuted", statement="The reference signal exceeds ten.",
                             prediction={**self.rule, "observable": "/measurement/reference"},
                             previous_claim_ref=previous["claim_revision_id"])
        self.assertEqual(("insufficient", "undetermined", []), (changed["level"], changed["publication"]["direction"],
                                                               changed["publication"]["opposing_refs"]))
        self.assertEqual(previous, v6_claims.read_revision(self.gx.conn, self.gx.run_id, previous["claim_revision_id"]))
        self.trace("different-endpoint", changed)
        self.observe("calendar", {"name": "follow-up"}, data=[])
        lead, cur, _ = self.delivered_claim()
        narrow = self.adopt(lead, "limit-claim", [observed], "open", scope="Morning thermometer number two",
                            previous_claim_ref=cur["claim_revision_id"])
        self.assertEqual("open", narrow["publication"]["level"])
        self.assertEqual([], narrow["publication"]["opposing_refs"])

    def test_legacy_goal_retains_historical_publication_on_cold_read(self):
        self.start(policy="gx.evidence/6.2")
        observed = self.reading("historical", 7)
        lead, previous, _ = self.delivered_claim()
        old = self.adopt(lead, "historical-support", [observed], "limited_support",
                         previous_claim_ref=previous["claim_revision_id"])
        self.assertEqual(("limited_support", "opposed"), (old["science"]["level"], old["maturity"]["direction"]))
        self.assertNotIn("publication", old)
        self.assertEqual(old, self.cli("status")["claims"][0])

    def test_published_independently_reviewed_literature_supports_its_scoped_statement(self):
        from gx_core.p0 import v62_sources as sources, v6_goal
        from tests.test_v62_sources import SourceGoal
        f = SourceGoal()
        self.addCleanup(f.doCleanups)
        f.setUp()
        src = f.refs["note.txt"]
        _, worker = f.open(f.lead, [src])
        read = f.paragraph(worker)
        interpretation = f.interpretation(read)
        found = f.call(worker, sources.EXTRACT, interpretation)
        f.call(f.lead, "gx_decide_revision", {"decision": draft()})
        f.publish(worker, [src, found["evidence_ref"]], "A located observation from the first visit.")
        lead = f.bind(v6_goal.ensure_task(f.conn, f.run))
        _, reviewer = f.open(lead, [src, found["evidence_ref"]], 2)
        f.call(lead, "gx_decide_revision", {"decision": draft()})
        independent = f.paragraph(reviewer)
        self.assertNotEqual(read["receipt_id"], independent["receipt_id"])
        judgment = f.call(reviewer, sources.REVIEW, {"evidence_ref": found["evidence_ref"],
                          "receipt_id": independent["receipt_id"], "quote_sha256_seen": found["quote_sha256"],
                          "verdict": "SUPPORTED", "note": "The original signed note describes the named wall."})
        rr = judgment["review_ref"]
        f.publish(reviewer, [src, found["evidence_ref"], rr], "The passage supports the statement within its survey.")
        lead = f.bind(v6_goal.ensure_task(f.conn, f.run))
        candidate = {**BASE, "statement": interpretation["claim"], "scope": interpretation["applicability"],
                     "criteria": "The actual field note explicitly describes dampness.", "evidence_refs": [rr],
                     "limitations": "Western transect only.", "level": "limited_support"}
        with self.assertRaises(SchemaInvalid):
            f.call(lead, "gx_stage_claim", {**candidate, "scope": "All sites"}, "reject-broader-survey")
        proposed = f.call(lead, "gx_stage_claim", candidate)
        result = f.call(lead, "gx_decide_revision", {"decision": draft(evidence=[rr, proposed["claim_ref"]])})
        published, = result["claim_revisions"]
        self.assertEqual((published["level"], published["maturity"]["stage"], published["publication"]["direction"]),
                         ("limited_support", "grounded", "supported"))
        self.assertEqual(published["publication"]["supporting_refs"], [rr])
