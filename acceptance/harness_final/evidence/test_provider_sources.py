"""Comparison of two access routes to a record and an equally valued independent reading."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_final.catalog.evaluate import unrestricted_opening
from acceptance.harness_refactor.catalog import test_open_discovery
from acceptance.v621 import test_claim_publication
from acceptance.v623.test_chronology import observe
from gx_core.p0 import research_evidence


class ProviderProvenance(unittest.TestCase):
    admit = test_open_discovery.OpenDiscovery.admit

    def test_rest_mcp_and_separately_acquired_equal_reading(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        material = []
        for label in ("first-visit", "independent-visit"):
            path = folder / (label + ".json")
            path.write_text(json.dumps({"scheduled_sample":label}))
            material.append({"role":"material", "path":str(path)})
        study, lead = self.admit("How often did the registered north station readings exceed ten Celsius?",
                                 {"input_files":material, "research_effort":"deep"})
        # Use the existing claim and operator ingress adapters on this actual gx research Goal.
        f = test_claim_publication.ClaimPublication()
        f.gx, f.folder = study, folder
        study.call(lead, "gx_query_catalog", {"kind":"all", **unrestricted_opening(study,lead)}, "survey")
        rule = {"data_refs":study.materials,"observable":"/measurement/value","operator":"gt","threshold":10,
                "target":"experimental_observation","measurement_schema":test_claim_publication.METRIC}
        claim = f.adopt(lead, "register", study.materials, "open", prediction=rule)
        def deliver(route, index):
            record = "north-station/2024-archive/"+str(index)
            # Two transports may serialize the same revision differently. The
            # canonical database record and acquisition describe their common origin.
            body = {"schema":test_claim_publication.METRIC,"measurement":{"value":12.5},
                    "record":record, "access_route":route}
            identity = {axis:record+"/"+axis for axis in ("data","source","sample","sampling","acquisition")}
            return observe(f, route, test_claim_publication.SCOPE, json.dumps(body).encode(),
                           data=[study.materials[index]], acquisition=identity)
        rest, mcp = deliver("REST", 0), deliver("MCP", 0)
        from acceptance.v62a.interleavings_fixture import draft
        waiting, interim, _ = f.delivered_claim()
        first = f.cli("status")["claims"][0]["maturity"]
        self.assertEqual(interim["maturity"]["stage"], first["stage"])
        self.assertEqual(first["stage"], "tested")
        self.assertEqual({r["artifact_ref"] for r in first["results"]}, {rest, mcp})
        pair, = first["replication"]
        self.assertEqual(set(pair["result_refs"]), {rest,mcp})
        self.assertEqual(pair["relation"], "same_data_method_consistency")
        study.call(waiting, "gx_decide_revision", {"decision":draft(evidence=[rest,mcp])}, "retain-original")
        independent = deliver("second-monitor", 1)
        actor, projected, _ = f.delivered_claim()
        full = f.cli("status")["claims"][0]["maturity"]
        self.assertEqual(full["stage"], "replicated")
        self.assertEqual({r["value"] for r in full["results"]}, {12.5})
        self.assertEqual({r["artifact_ref"] for r in full["results"]}, {rest,mcp,independent})
        groups = {frozenset(p["result_refs"]):p["relation"] for p in full["replication"]}
        self.assertEqual(groups[frozenset((rest, mcp))], "same_data_method_consistency")
        self.assertEqual(groups[frozenset((rest, independent))], "independent_acquisitions")
        self.assertEqual(groups[frozenset((mcp, independent))], "independent_acquisitions")
        self.assertEqual(projected["maturity"]["stage"], full["stage"])
        pi = research_evidence.evidence(study.conn, study.run_id)
        materials = {i["artifact_ref"]:i for i in pi["materials"]}
        self.assertEqual({materials[ref]["provenance"][-1]["source"] for ref in (rest,mcp)}, {"REST","MCP"})
        published = pi["predictions"][0]["assessments"][-1]
        self.assertEqual(published["replication"], full["replication"])
        if path := os.environ.get("GX_FINAL_PROVIDER_SOURCE_RECEIPT"):
            record = {"transport":"SCRIPTED_CALLBACK", "live_rest_or_mcp_calls":0, "run_id":study.run_id,
                      "registered_claim":claim["hypothesis_id"], "after_two_routes":{"stage":first["stage"],
                      "results":first["results"], "pair":pair}, "after_independent":{"stage":full["stage"],
                      "replication":full["replication"]}, "delivered_lead_task":actor.task_id,
                      "routes":{k:[p["source"] for p in materials[k]["provenance"] if "source" in p]
                               for k in (rest,mcp,independent)}}
            Path(path).write_text(json.dumps(record,indent=2)+"\n")
