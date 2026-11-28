"""Check a deployed archive against known positive, zero and error controls."""
import runpy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[2]
PACKAGE = PRODUCT / "gx-h/plugins/installed/org.gx.yeast-enzyme-archive/1.0.0"
MOUNT = PRODUCT / "gx-h/var/science-assets/yeast-archive"
archive = SimpleNamespace(**runpy.run_path(str(PACKAGE / "archive.py")))


def query(substrate, **changes):
    return {"schema": "gx.yeast-archive-query/1", "taxid": 559292, "substrate": substrate, **changes}


site_archive = unittest.skipUnless(MOUNT.is_dir(), "Install the licensed S288C archive with mount.py")


class Archive(unittest.TestCase):
    def test_scope_and_absent_mount(self):
        for invalid in (query("maltose", taxid=4932), query("unregistered"), query("maltose", release="old")):
            with self.assertRaises(ValueError):
                archive.canonical_query(invalid)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            archive.lookup(query("lactose"), Path(tmp))

    @site_archive
    def test_distinct_reaction_enzyme_class_and_grouping(self):
        malt = archive.lookup(query("麥芽糖"), MOUNT)
        iso = archive.lookup(query("isomaltose"), MOUNT)
        self.assertEqual(malt["exact_reaction"]["listed_ec"], ["3.2.1.48"])
        self.assertEqual(malt["context_ec"], "3.2.1.20")
        self.assertEqual([p["accession"] for p in malt["parents"]], ["P38158","P53341"])
        self.assertEqual(len(iso["parents"]), 5)
        for doc in (malt, iso):
            self.assertEqual(doc["counts"]["exact_accessions"], 0)
            self.assertTrue(all(len(p["sequence_sha256"]) == 64 for p in doc["parents"]))
        self.assertEqual(iso["assay_coverage"]["grouped_accession_rows"], 15)
        self.assertEqual((iso["assay_coverage"]["single_target_rows"], iso["assay_coverage"]["grouped_target_rows"]), (10, 1))
        self.assertGreater(malt["assay_coverage"]["other_named_substrate_rows"], 0)

    @site_archive
    def test_true_zero_and_positive_direction_control(self):
        zero = archive.lookup(query("lactose"), MOUNT)
        self.assertEqual(zero["counts"]["distinct_accessions"], 0)
        positive = archive.lookup(query("trehalose"), MOUNT)
        self.assertEqual(positive["counts"]["exact_accessions"], 3)
        self.assertEqual({link["direction"] for p in positive["parents"] for link in p["reaction_links"]}, {"UN"})

    @site_archive
    def test_handoff_content_and_relocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "second-site"
            other.symlink_to(MOUNT.resolve(), target_is_directory=True)
            first = archive.lookup(query("maltose"), MOUNT)
            self.assertEqual(first, archive.lookup(query("maltose"), other))
            verified = archive.compare(query("isomaltose"), first, other)
            self.assertEqual(verified["new_only_accessions"], ["P0CW40","P0CW41","P40884","P53051","Q08295"])
            wrong = json.loads(json.dumps(first))
            wrong["parents"][0]["sequence_version"] += 1
            with self.assertRaises(ValueError):
                archive.compare(query("isomaltose"), wrong, other)
