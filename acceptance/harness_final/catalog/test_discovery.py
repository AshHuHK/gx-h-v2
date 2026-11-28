"""Bilingual search uses verified metadata without awarding a science capability."""
import json
from types import SimpleNamespace
import unittest

from acceptance.harness_final.catalog.evaluate import unrestricted_opening
from acceptance.harness_refactor.catalog import test_open_discovery
from gx_core.p0 import catalog_index, catalog_terms


class BilingualDiscovery(unittest.TestCase):
    admit = test_open_discovery.OpenDiscovery.admit

    def test_bilingual_and_reference_only(self):
        study, lead = self.admit("What do these public records address?")
        study.call(lead, "gx_query_catalog", {"kind":"all", **unrestricted_opening(study,lead)}, "opening")
        for term, expected in (("環肽", "deposited-macrocycle-qc@1"), ("cyclic peptide", "deposited-macrocycle-qc@1"),
                               ("乳糖", "yeast-archive-lookup@2"), ("lactose", "yeast-archive-lookup@2")):
            with self.subTest(term=term):
                result = study.call(lead, "gx_query_catalog", {"kind":"all", "query":term, "limit":5}, term)
                item = next((i for i in result["items"] if i["reference"] == expected), None)
                self.assertIsNotNone(item)
                self.assertEqual(item["status"], "selectable")
                self.assertTrue(item["capability_facts"]["installed"])
                self.assertFalse(item["execution_granted"])
        archived = study.call(lead, "gx_query_catalog", {"query":"gx-understand@legacy"}, "archived")
        self.assertTrue(all(i["capability_facts"]["reference_only"] for i in archived["items"]))
        self.assertTrue(archived["items"])
        empty = study.call(lead, "gx_query_catalog", {"query":"qzvzorplax"}, "empty")
        self.assertEqual(empty["total"], 0)
        detail = study.call(lead, "gx_query_catalog", {"reference":"yeast-archive-lookup@2"}, "details")
        self.assertEqual(set(detail["spec"]["names"]), {"en","zh-Hant","zh-Hans"})
        self.assertIn("乳糖", detail["spec"]["aliases"])

    def test_author_declared_terms_and_pinned_translation_identity(self):
        name = {"en":"Original records", "zh-Hant":"原始記錄"}
        package = SimpleNamespace(pin=SimpleNamespace(id="example.test", version="1"),
                                  manifest=SimpleNamespace(labels={"gx_catalog_terms":"names.json"}),
                                  read_file=lambda _:json.dumps({"read@1":{"names":name}}))
        errors = []
        local, translations = catalog_terms.declarations([package], errors)
        self.assertEqual(local[("example.test@1", "read@1")]["names"], name)
        self.assertEqual((translations, errors), ({}, []))
        text = {"sources":[{"provider":"example.test@1","method":"read@1","portable_sha256":"11"*32}], "names":name}
        package.manifest.labels = {"gx_catalog_translations":"labels.json"}
        package.read_file = lambda _:json.dumps([text])
        _, records = catalog_terms.declarations([package], errors)
        self.assertIn(("example.test@1", "read@1","11"*32), records)
        self.assertNotIn(("example.test@1", "read@1","00"*32), records)
        package.read_file = lambda _:b"invalid"
        self.assertEqual(catalog_terms.declarations([package], errors), ({},{}))
        self.assertEqual(errors, ["example.test@1 / labels.json"])

    def test_localized_methods_keep_io_and_boundary(self):
        scientific = [c for c in catalog_index.installed_methods() if c.get("names")]
        self.assertGreater(len(scientific), 20)
        for c in scientific:
            self.assertTrue(c["inputs"] is not None and c["outputs"])
            self.assertTrue(c.get("limitations") or c.get("method"))
