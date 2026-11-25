"""Install the public packages and exercise their scope, provenance and effort contracts."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from gx_core import config
from gx_core.plugins import cli, loader
from gx_core.plugins.catalog import package_digest


PRODUCT = Path(__file__).resolve().parents[2]
EXAMPLES = PRODUCT / "examples/plugins"
MATERIALS = EXAMPLES / "v62_public_materials"
SURVEY = "method_workflow_survey"
LITERATURE = "offline_literature_mechanism"
HYPOTHESES = "hypothesis_space"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_grant():
    grant = json.loads((MATERIALS / "grant.json").read_bytes())
    for item in grant["input_files"]:
        item["path"] = str((MATERIALS / item["path"]).resolve())
    return grant


class PublicMaterials(unittest.TestCase):
    def test_all_three_offline_formats_preserve_scientific_passages_and_locators(self):
        from gx_core.p0 import v62_sources

        passages = [
            ("deposition-excerpt.txt", "text", "_refine.ls_d_res_high                            2.100", "lines "),
            ("method-notes.md", "markdown", "ordered_backbone mask adds full occupancy", "lines "),
            ("deposition-note.xml", "jats", "3.000 angstroms", "body/source/p/record"),
        ]
        for name, format_name, quote, locator in passages:
            with self.subTest(material=name):
                data = (MATERIALS / name).read_bytes()
                coverage, segments = v62_sources._segments(data, format_name)
                matching = [s for s in segments if quote in s[4]]
                self.assertEqual(len(matching), 1)
                segment, = matching
                self.assertTrue(segment[3].startswith(locator), segment)
                self.assertEqual(segment[5], hashlib.sha256(segment[4].encode()).hexdigest())
                self.assertEqual((coverage, segments), v62_sources._segments(data, format_name))

    def test_reference_cards_and_goal_pin_the_original_public_sources(self):
        grant = public_grant()
        for item in grant["input_files"]:
            self.assertEqual(digest(Path(item["path"])), item["sha256"], item["path"])
        for file, candidates in (("models.json", "models"), ("workflows.json", "candidates"),
                                 ("evaluations.json", "candidates")):
            document = json.loads((MATERIALS / file).read_bytes())
            self.assertIn("public_", document["resource_type"])
            for candidate in document[candidates]:
                with self.subTest(candidate=candidate["id"]):
                    source = candidate["source"]
                    content = PRODUCT / source["path"]
                    self.assertEqual(digest(content), source["sha256"])
                    locator = source["locator"]
                    if locator.startswith("/"):
                        node = json.loads(content.read_bytes())
                        for key in locator[1:].split("/"):
                            node = node[key]
                        self.assertTrue(node)
                    self.assertTrue(candidate["cost_class"])
        inputs = {Path(item["path"]).name: item for item in grant["input_files"]}
        for name in ("method-notes.md", "deposition-excerpt.txt", "deposition-note.xml"):
            self.assertEqual(inputs[name]["role"], "test_fixture")
        original = Path(inputs["2RE9.cif"]["path"]).read_text().splitlines()
        excerpt = (MATERIALS / "deposition-excerpt.txt").read_text().splitlines()
        for line in excerpt:
            if line.startswith("_"):
                self.assertIn(line, [row.rstrip() for row in original])
        jats = ElementTree.parse(MATERIALS / "deposition-note.xml")
        source = jats.find(".//p[@id='record']")
        self.assertIn("3.000 angstroms", "".join(source.itertext()))
        record = Path(inputs["2O0O.cif"]["path"]).read_text().splitlines()
        self.assertTrue(any(line.split() == ["_refine.ls_d_res_high", "3.000"] for line in record))
        self.assertIn("10.1016/j.bbrc.2007.09.097", "".join(jats.find(".//p[@id='citation']").itertext()))


class PublicModuleCatalog(unittest.TestCase):
    def test_new_identity_installs_without_a_core_change_and_grants_stay_distinct(self):
        from gx_core.errors import SchemaInvalid
        from gx_core.p0 import v62_catalog

        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        approved = base / "config/workflow_packages.json"
        approved.parent.mkdir()
        approved.write_text('{"plugins": []}')
        install_root = base / "plugins/installed"
        core = base / "gx_core"
        shutil.copytree(PRODUCT / "gx-h/gx_core", core, ignore=shutil.ignore_patterns("__pycache__"))
        self.enterContext(patch.object(config, "HARNESS_ROOT", base))
        self.enterContext(patch.object(config, "CONFIG_DIR", approved.parent))

        def install(source):
            return cli._install(source, package_digest(source), install_root, approved)

        def core_files():
            return {p.relative_to(core): digest(p) for p in core.rglob("*") if p.is_file()}

        survey_pin = install(PRODUCT / "gx-h/plugins/installed/org.gx.research-survey/1.0.0")
        before = v62_catalog.for_start({"research_effort": "deep"})
        unchanged = core_files()
        unit_pin = install(EXAMPLES / "v62_reading_check")
        self.assertEqual(core_files(), unchanged)
        self.assertIsNone(v62_catalog.for_start({"research_effort": "quick"}))
        medium = v62_catalog.for_start({"research_effort": "medium"})
        self.assertEqual([m["module_id"] for m in medium["modules"]], [SURVEY])
        self.assertEqual(set(medium["modules"][0]["focuses"]), {"model", "workflow", "evaluation"})
        self.assertEqual({m["module_id"] for m in before["modules"]}, {SURVEY, LITERATURE, HYPOTHESES})
        deep = v62_catalog.for_start({"research_effort": "deep"})
        self.assertEqual({m["module_id"] for m in deep["modules"]}, {SURVEY, LITERATURE, HYPOTHESES, "unit_notation_check"})
        with self.assertRaises(SchemaInvalid):
            v62_catalog.for_start({"research_effort": "medium", "research_modules": [HYPOTHESES + "@1"]})
        selected = v62_catalog.for_start({"research_effort": "deep", "research_modules": ["unit_notation_check@1"]})
        self.assertEqual([m["plugin"]["sha256"] for m in selected["modules"]], [unit_pin["sha256"]])

        package = loader.bootstrap(selected=[(survey_pin["id"], survey_pin["version"])]).get(
            survey_pin["id"], survey_pin["version"], survey_pin["sha256"])
        declarations = package.contributions.research_modules
        survey, literature, hypotheses = (declarations[(name, "1")] for name in (SURVEY, LITERATURE, HYPOTHESES))
        self.assertEqual(survey["catalog_access"], "public_methods")
        self.assertFalse(survey["capabilities"])
        self.assertNotIn("gx_decide_revision", survey["tools"])
        self.assertEqual(set(survey["input_roles"]), {"method_spec", "workflow_spec", "model_card", "evaluation_spec"})
        self.assertIn("gx_review_evidence", literature["tools"])
        self.assertIn("gx_extract_evidence", literature["tools"])
        self.assertNotIn("gx_extract_evidence", hypotheses["tools"])
        self.assertTrue(hypotheses["published_inputs_only"])
        self.assertNotIn("test_fixture", hypotheses["input_roles"])
        self.assertIn("module_report", hypotheses["input_roles"])
