"""Ordinary module reports may cite verifiable bibliography while measured values still need evidence."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.catalog import test_open_discovery
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid


ARTICLE = """<article><front><journal-meta><journal-title-group><journal-title>Field Notes</journal-title>
</journal-title-group></journal-meta><article-meta><article-id pub-id-type="doi">10.1234/field.123</article-id>
<article-id pub-id-type="pmid">7654321</article-id><article-id pub-id-type="pmcid">PMC7654321</article-id>
<title-group><article-title>Spot measurements</article-title></title-group>
<contrib-group><contrib contrib-type="author"><name><surname>Chen</surname></name></contrib></contrib-group>
<pub-date><year>2014</year></pub-date><volume>4</volume><fpage>200</fpage><lpage>212</lpage>
</article-meta></front><body><p>Spot readings require instrument calibration.</p></body>
<floats-group><table-wrap id="sample"><label>Table 2</label></table-wrap></floats-group></article>"""
CITE = "Chen et al., Field Notes 4 (2014), 200–212, DOI 10.1234/field.123, PMID 7654321, PMCID PMC7654321."


class Bibliography(unittest.TestCase):
    admit = test_open_discovery.OpenDiscovery.admit

    def report(self, parts, *, errors=()):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        material = work/"paper.xml"
        material.write_text(ARTICLE)
        other = work/"other.xml"
        other.write_text(ARTICLE.replace("2014", "2001").replace("10.1234/field.123", "10.2345/other"))
        study, lead = self.admit("How do the source notes describe sampling?",
            {"research_effort":"deep","research_modules":["scholarly_investigation@1"],
             "input_files":[{"role":"material","path":str(x)} for x in (material,other)]})
        study.call(lead,"gx_query_catalog",{"kind":"all","pi_method_scope":[]},"open")
        grant=study.open_args("source",caps=[],inputs=study.materials)
        grant.update(module_id="scholarly_investigation",focus="literature",
                     expected_delivery="Read the supplied original and report its provenance.")
        member=study.call(lead, "gx_open_module",grant,"source")
        actor=study.bind(member["task_id"])
        for i,ref in enumerate(study.materials):
            page=study.call(actor,"gx_read_source",{"source_ref":ref},"pages-"+str(i))
            study.call(actor,"gx_read_source",{"source_ref":ref,"segment_id":page["segments"][0]["segment_id"]},"read-"+str(i))
        for i,(message,idx) in enumerate(errors):
            with self.subTest(message=message), self.assertRaises(SchemaInvalid):
                study.report(actor,study.materials[0],key="bad-"+str(i),
                             findings=[{"text":message, "source_refs":study.materials if idx is None else [study.materials[idx]]}])
        result=study.report(actor,study.materials[0],key="valid",text=parts)
        bundle=ArtifactService(study.conn).read_json(result["report_ref"])
        self.assertEqual(bundle["verified_metrics"],[])
        self.assertIn(parts,[x["text"] for x in bundle["findings"]])
        return result

    def test_citation_and_digest_are_artifact_grounded(self):
        self.report(CITE+" Its SHA-256 is "+hashlib.sha256(ARTICLE.encode()).hexdigest()+".",
                    errors=[(CITE.replace("200–212","200–213"),0)])

    def test_scope_and_scientific_scalars(self):
        self.report("Chen et al. (2014), Table 2, identifies the sampling plan.",
                    errors=[("Chen et al. (2014) measured the sample.",1)])

    def test_false_byline_and_mixed_citation(self):
        self.report(CITE,errors=[(CITE.replace("Chen et al.", "Lewis et al."), 0)])
        self.report(CITE,errors=[("Chen et al. (2014), DOI 10.2345/other.", None)])

    def test_citation_never_certifies_a_reading(self):
        self.report(CITE,errors=[("Chen et al. (2014), Table 2, measured 4 mg in this sample.",0)])
