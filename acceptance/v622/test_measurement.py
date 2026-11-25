"""R1: register a coordinate readout and try to publish an incompatible selection."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, branch, draft
from acceptance.v621.verify_chain import oracle
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest, v6_claims, v6_goal
from gx_core import contracts

METHOD = "deposited-chain-geometry@2"
STATEMENT = "Shared ordered-backbone positions exceed the registered coverage threshold."
SCOPE = "Deposited chains in this coordinate archive"


class MeasurementAdmission(unittest.TestCase):
    def test_incompatible_mask_does_not_support_the_registered_claim(self):
        with tempfile.TemporaryDirectory() as name:
            w = Path(name)
            s = self.enterContext(Scenario(w / "study", spec_updates={
                "research_effort": "deep", "research_modules": [], "plugins": ["org.gx.protein@0.3.0"],
                "capabilities": [METHOD]}))
            raw = s.materials[0]
            source = ArtifactService(s.conn)
            original = source.read_bytes(raw)
            coordinate_file = w / "original.cif"
            coordinate_file.write_bytes(original)
            _, union, masks = oracle.selections(oracle.MMCIF2Dict(str(coordinate_file)))
            counts = {key: [len({atom[0] for atom in masks[key]}), len(union)]
                      for key in ("common_ca", "ordered_backbone")}
            self.assertEqual(counts, {"common_ca": [164, 164], "ordered_backbone": [49, 164]})
            acquisition = json.loads((PRODUCT / "examples/v621-coordinate-analysis/2RE9.json").read_text())
            acquisition["applicability"] = SCOPE
            acq = v6_goal.record_observation(s.conn, s.run_id, raw, source="wwPDB-coordinate",
                                              acquisition=acquisition)
            def claim(actor, key, evidence, level, **more):
                return s.call(actor, "gx_stage_claim", {
                    "statement": STATEMENT, "scope": SCOPE,
                    "criteria": "Apply the ordered_backbone mask; compare its coverage with the registered threshold.",
                    "limitations": "One archive and the stated mask.",
                    "rationale": "Assess the registered selection on this material.",
                    "level": level, "evidence_refs": evidence, **more}, key)["claim_ref"]
            method = {"capability": METHOD, "output": "geometry.json"}
            # The released schema carried the mask only in its human-readable criterion.
            prediction_schema = contracts.native_goal_tool_spec("gx_stage_claim")["inputSchema"]["properties"]["prediction"]
            if "parameters" in prediction_schema["properties"]["method"]["properties"]:
                method.update(parameters={"mask": "ordered_backbone"}, metric_id="coverage", unit="1", input_role="structure")
            pred = {"target": "analysis", "method": method, "observable": "/coverage_fraction",
                    "operator": "gt", "threshold": 0.6, "data_refs": [raw],
                    "measurement_schema": "gx.deposited_chain_geometry/2"}
            plan = claim(s.lead, "register", [raw], "open", prediction=pred)
            def job(actor, label, branch_id):
                return s.node(actor, label, raw, branch_id=branch_id, capability=METHOD, parameters={"mask": label})
            path = {**branch("measurements"), "budget_limits": {"job_submissions": 2, "cpu_seconds": 120}}
            def apply(actor,key,refs,ops=(),watch=None):
                return s.call(actor,"gx_decide_revision", {"decision":draft(*ops, evidence=refs,
                        watches=[{"event":"job_settled","subject_ref":watch}] if watch else None)},key)
            register = apply(s.lead, "register-and-measure", [raw,plan],
                             [path,job(s.lead,"common_ca","local:measurements")], "local:common_ca")
            first = s.settle(register["aliases"]["local:common_ca"])
            def actor_with(ref):
                actor=s.next_actor("run")
                if ref not in s.brief(actor)["evidence_refs"]:
                    apply(actor,"observe-next-"+ref,[],watch=ref)
                    actor=s.next_actor("run")
                self.assertIn(ref,s.brief(actor)["evidence_refs"])
                return actor
            actor=actor_with(first["result_ref"])
            prior,=s.brief(actor)["claims"]
            proposal=claim(actor,"request-support",[first["result_ref"]],"limited_support",
                           previous_claim_ref=prior["claim_revision_id"])
            wrong=apply(actor,"publish-and-obtain-ordered",[first["result_ref"],proposal],
                        [job(actor,"ordered_backbone",register["aliases"]["local:measurements"])],"local:ordered_backbone")
            def external_status():
                output = subprocess.check_output(
                    [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "status", "--state", str(s.state),
                     "--run-id", s.run_id], text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
                return json.loads(output)["claims"][0]
            cold_wrong = external_status()
            second=s.settle(wrong["aliases"]["local:ordered_backbone"])
            final_actor=actor_with(second["result_ref"])
            latest,=s.brief(final_actor)["claims"]
            amended=claim(final_actor,"conclude",[second["result_ref"]],"contested",
                          previous_claim_ref=latest["claim_revision_id"])
            result=apply(final_actor,"publish-final",[second["result_ref"],amended])
            status = external_status()
            doc=result["claim_revisions"][0]
            wrong_doc=wrong["claim_revisions"][0]
            self.assertEqual(status["claim_revision_id"],doc["claim_revision_id"])
            self.assertEqual(v6_claims.read_revision(s.conn,s.run_id,wrong_doc["claim_revision_id"]),wrong_doc)
            trace={"case":"R1","run_id":s.run_id,"paid_calls":0,"input_sha256":hashlib.sha256(original).hexdigest(),
                   "acquisition_event":acq["event_id"],"independent":counts,
                   "registration":register["claim_revisions"][0]["maturity"]["registration"],
                   "jobs":[{k:manifest.read_manifest(s.conn,n["job_id"])[k] for k in
                              ("admission","completion","manifest_hash")} for n in (first,second)],
                   "wrong_mask_publication":wrong_doc,"correct_mask_publication":doc,
                   "operator_status":status,"next_lead":latest,"cold_wrong_mask":cold_wrong}
            if destination:=os.getenv("GX_R1_RECEIPT"):
                Path(destination).write_text(json.dumps(trace,indent=2)+"\n")
            self.assertNotEqual(wrong_doc["publication"]["direction"],"supported",
                                "wrong common_ca mask gave supporting evidence for ordered_backbone")
            self.assertEqual(cold_wrong["publication"], wrong_doc["publication"])
            self.assertFalse(wrong_doc["maturity"]["results"])
            self.assertEqual(doc["publication"]["direction"],"opposed")
            self.assertEqual(doc["science"]["level"],"contested")


if __name__ == "__main__":
    unittest.main()
