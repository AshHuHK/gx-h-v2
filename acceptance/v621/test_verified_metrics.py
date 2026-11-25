"""GX renders the archived value in the actual released report and native Lead brief."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, branch, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v6_native, v621_metrics as metrics

METHOD = "deposited-chain-geometry@2"
RATIO = {"reader":"json/1", "op":"ratio/1", "numerator":"/n", "denominator":"/d",
         "scope":{"mask":"/mask"}, "unit":"1", "places":3, "bounds":[0,1]}
SCALAR = {"reader":"csv-row/1", "op":"decimal/1", "pointer":"/reading", "places":3, "unit":"1"}
SOURCE_METADATA = PRODUCT / "examples/v621-coordinate-analysis/2RE9.json"
SCOPE = json.loads(SOURCE_METADATA.read_bytes())["applicability"]


class DisplayContract(unittest.TestCase):
    def test_raw_precision_csv_and_unreadable_inputs(self):
        self.assertEqual(metrics.definition_errors({"coverage":RATIO, "reading":SCALAR}), [])
        values = []
        for raw in ("0.06439", "0.06441"):
            projected = metrics._read(("reading\n"+raw+"\n").encode(), "csv-row/1", "instrument.csv", "text/csv")
            observed = metrics.calculate(SCALAR, projected)
            values.append(observed)
            self.assertEqual("0.064", observed["display"])
            self.assertEqual(raw, observed["raw_value"])
        self.assertLess(Decimal(values[0]["raw_value"]), Decimal("0.0644"))
        self.assertGreater(Decimal(values[1]["raw_value"]), Decimal("0.0644"))
        for raw,reader,name,media in [(b"hello", "json/1", "notes.txt", "text/plain"),
                                     (b"x,x\n1,2\n","csv-row/1","invalid.csv","text/csv")]:
            with self.assertRaises(ValueError):
                metrics._read(raw, reader, name, media)
        with self.assertRaises(KeyError):
            metrics.calculate(SCALAR, {"unknown":5})
        for n,d in ((149,0),(1,0.5),(-1,10),(3,2),(True,4)):
            with self.assertRaises(ValueError):
                metrics.calculate(RATIO, {"n":n,"d":d,"mask":"ordered"})
        with self.assertRaises(ValueError):
            metrics.calculate(SCALAR, {"reading":float("inf")})
        self.assertTrue(metrics.definition_errors({"coverage":{**RATIO, "caller_tolerance": 1}}))
        converted = metrics.calculate({**SCALAR, "op":"difference/1", "left":"/a","right":"/b",
                                      "unit":"mV","source_unit":"V","scale":1000},
                                     {"a":0.065,"b":0.001})
        self.assertEqual(converted["display"], "64.000 mV")


class QuantitativePublication(unittest.TestCase):
    def test_original_receipt_real_job_report_and_delivered_numbers(self):
        if work := os.environ.get("GX_V621_METRICS_WORK"):
            tmp = Path(work).resolve()
            if tmp.is_relative_to(PRODUCT.resolve()):
                raise ValueError("Retained evidence must be outside the product checkout")
            tmp.mkdir(parents=True, exist_ok=False, mode=0o700)
        else:
            tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        spec = {"research_effort":"deep","research_modules":["data_inventory@2"],
                "plugins":["org.gx.protein@0.3.0"],"capabilities":[METHOD]}
        s = self.enterContext(Scenario(tmp/"study",spec_updates=spec))
        raw = s.materials[0]
        service = ArtifactService(s.conn)
        sha = service.describe(raw)["sha256"]
        metadata = tmp/"acquisition.json"
        metadata.write_bytes(SOURCE_METADATA.read_bytes())
        argv = [sys.executable, "-B", str(PRODUCT/"bin/gx-p0"), "observe", "--state", str(s.state),
                "--run-id",s.run_id,"--artifact-ref",raw,"--sha256",sha,"--source","public-coordinate-record",
                "--acquisition",str(metadata)]
        published = subprocess.run(argv,capture_output=True,text=True,check=True)
        self.assertEqual(sha, hashlib.sha256(service.read_bytes(raw)).hexdigest())
        p = s.open_args("inventory", inputs=[raw], caps=[METHOD])
        p["module_version"]="2"
        opened = s.call(s.lead,"gx_open_module",p,"commission")
        actor = s.bind(opened["task_id"])
        s.call(s.lead,v6_native.DECIDE,{"decision":draft()},"wait")
        prev = s.submit_node(actor,"coverage",input_ref=raw,capability=METHOD,parameters={"mask":"ordered_backbone"})
        node = s.settle(prev["aliases"]["local:coverage"])
        worker = s.next_actor(opened["scope"])
        ref = node["result_ref"]
        entry, = metrics.from_output(s.conn,s.run_id,ref)
        self.assertEqual((entry["numerator"],entry["denominator"],entry["display"]),(49,164,"49/164 (0.299)"))
        self.assertEqual(entry["inputs"][0]["sha256"],sha)
        self.assertEqual(entry["scope"]["mask"],"ordered_backbone")
        rejected=[]
        for text in ("The measured coverage is 149/164.", "The measured coverage is {{metric:coverage|mask=common_ca}}.",
                     "Change the unit to {{metric:coverage|unit=mV}}.", "The count is {{metric:coverage|numerator=149}}."):
            with self.assertRaises(SchemaInvalid) as exc:
                s.report(worker, ref, key="rejected-"+str(len(rejected)), text=text)
            rejected.append({"input":text,"reason":str(exc.exception)})
        tmpl = "Archive 2RE9 from 2007, source record-2.cif: {{metric:coverage|mask=ordered_backbone|unit=1}}."
        accepted = s.report(worker, ref, key="publish",text=tmpl)
        document = service.read_json(accepted["report_ref"])
        self.assertEqual(document["metrics"],[entry])
        self.assertIn(entry["display"],document["findings"][0]["text"])
        self.assertEqual(document["interpretation"]["findings"][0]["text"],tmpl)
        lead = s.next_actor("run")
        packet = v6_native.brief(s.roots._binding(lead.thread))
        if not any(r["report_ref"] == accepted["report_ref"] for r in packet["module_reports"]):
            # The operator's acquisition may have triggered this already frozen decision first.
            s.call(lead,v6_native.DECIDE,{"decision":draft()},"await-report")
            lead = s.next_actor("run")
            packet = v6_native.brief(s.roots._binding(lead.thread))
        report, = [r for r in packet["module_reports"] if r["report_ref"] == accepted["report_ref"]]
        self.assertEqual(report["metrics"],[entry])
        self.assertIn("49/164 (0.299)",report["findings"][0]["text"])
        claim = dict(statement="Archive coverage is {{metric:coverage}}.", scope=SCOPE,
                     criteria="Common positions in the admitted selection.",
                     limitations="The count describes only these deposited coordinates.",
                     rationale="The named output measures the recorded mask.",
                     level="open", evidence_refs=[ref])
        for bad in ("The coverage is 149/164.", "The measured ratio is 2007/2024."):
            with self.assertRaises(SchemaInvalid) as exc:
                s.call(lead,"gx_stage_claim",{**claim,"statement":bad},"wrong-claim-"+str(len(rejected)))
            rejected.append({"claim":bad,"reason":str(exc.exception)})
        staged = s.call(lead,"gx_stage_claim",claim,"stage-conclusion")
        conclusion = s.call(lead,"gx_decide_revision",{"decision":draft(evidence=[ref,staged["claim_ref"]])},"publish-claim")
        final, = conclusion["claim_revisions"]
        self.assertEqual(final["statement"],"Archive coverage is 49/164 (0.299).")
        self.assertEqual(final["metrics"],[entry])
        trace = {"run_id":s.run_id,"paid_model_calls":0,"provider_tokens":None,"source_event":json.loads(published.stdout),
                 "job_id":node["job_id"],"module":opened["module_instance_id"],"report":accepted,
                 "metric":entry,"delivered_report":report,"lead_lease":lead.lease_id,
                 "formal_claim":final,"rejected":rejected,"formal_findings":document["findings"],"jobs":len(s.rows("SELECT * FROM jobs"))}
        if path:=os.getenv("GX_V621_METRICS_TRACE"):
            Path(path).write_text(json.dumps(trace,indent=2,ensure_ascii=False)+"\n")
