"""Check the published direction at a public Goal's native and operator entry points."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.domain import ids
from gx_core.p0 import v6_claims

ROOT = Path(__file__).resolve().parents[2]
METRIC = "public.thermometer.celsius/1"
SCOPE = "Morning samples from the north station"
BASE = dict(statement="The observed morning temperature exceeds ten degrees Celsius.", scope=SCOPE,
            criteria="Temperature in Celsius exceeds ten.", limitations="North station mornings only.",
            rationale="Audit the original proposition against the registered endpoint.")


class ClaimPublication(unittest.TestCase):
    def test_adverse_registered_reading_is_published_consistently(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        plan = folder / "station.json"
        plan.write_text('{"survey":"north-morning"}')
        with Scenario(folder / "goal", spec_updates={"research_effort": "deep", "research_modules": [],
                      "capabilities": [], "plugins": [], "input_files": [{"role": "material", "path": str(plan)}]}) as s:
            raw = s.materials[0]
            rule = {"data_refs": [raw], "observable": "/measurement/value", "operator": "gt", "threshold": 10,
                    "target": "experimental_observation", "measurement_schema": METRIC}
            initial = s.call(s.lead, "gx_stage_claim", {**BASE, "level": "open", "evidence_refs": [raw],
                             "prediction": rule}, "register-before-result")
            accepted = s.call(s.lead, "gx_decide_revision", {"decision": draft(evidence=[raw, initial["claim_ref"]])},
                              "adopt-registration")["claim_revisions"][0]
            src = folder / "instrument.json"
            src.write_text(json.dumps({"schema": METRIC, "measurement": {"value": 7}}))
            meta = folder / "acquisition.json"
            meta.write_text(json.dumps({**{k: "north-01" for k in ("data","acquisition","sample","source","sampling")},
                                        "publication": [], "processing": "calibrated thermometer",
                                        "applicability": SCOPE, "data_refs": [raw]}))
            def cli(verb, *args):
                argv = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), verb, "--state", str(s.state),
                        "--run-id", s.run_id, *map(str, args)]
                return json.loads(subprocess.check_output(argv, text=True, env={**os.environ,"GX_CODEX_OFFLINE":"1"}))
            observed = cli("observe","--source","thermometer","--input",src,"--acquisition",meta)["artifact_ref"]
            lead = s.next_actor("run")
            previous, = s.brief(lead)["claims"]
            self.assertEqual(("tested","opposed"), (previous["maturity"]["stage"],previous["maturity"]["direction"]))
            proposal = {**BASE,"level":"limited_support","evidence_refs":[observed],
                        "previous_claim_ref":previous["claim_revision_id"]}
            staged = s.call(lead, "gx_stage_claim", proposal, "propose-support")
            decision = s.call(lead, "gx_decide_revision",
                              {"decision":draft(evidence=[observed,staged["claim_ref"]])}, "adopt-interpretation")
            published, = decision["claim_revisions"]
            operator, = cli("status")["claims"]
            self.assertEqual(operator["claim_revision_id"], published["claim_revision_id"])
            self.assertEqual(["contested"]*3, [published["level"],published["science"]["level"],operator["level"]],
                             msg=f"GX {published['maturity']['stage']}/{published['maturity']['direction']} "
                                 f"became {published['science']['level']} in the public adoption and status")
            self.assertEqual(published["publication"]["proposed_level"],"limited_support")
            self.assertEqual(published["publication"]["opposing_refs"],[observed])
            self.assertEqual(v6_claims.read_revision(s.conn,s.run_id,accepted["claim_revision_id"]), accepted)
            cli("observe","--source","review-request","--input",plan)
            subsequent = s.next_actor("run")
            row = s.conn.execute("SELECT brief FROM p0_roots WHERE lease_id=?", (subsequent.lease_id,)).fetchone()
            self.assertIn(ids.canonical_json(published["publication"]), row["brief"])
