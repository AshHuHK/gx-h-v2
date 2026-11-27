"""Scientific boundary regressions for the read-only S288C lookup methods."""
import json
from hashlib import sha256
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[2] / "gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1"
method = runpy.run_path(str(ROOT / "methods/evidence.py"))
scope = {"schema": "gx.yeast-enzyme-scope/1", "taxid": 559292}
mount = json.loads((ROOT / "methods/mounts.json").read_bytes())


class YeastBoundaries(unittest.TestCase):
    def test_unsupported_substrate_or_strain_does_not_read_snapshot(self):
        for args in ((dict(scope, taxid=4932), "trehalose"), (scope, "other")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                method["run_lookup"](*args, {})

    def test_release_and_strain_are_required_for_acquired_record(self):
        accession = "P32356"
        doc = {"primaryAccession": accession, "organism": {"taxonId": 559292},
               "entryAudit": {"entryVersion": 12, "sequenceVersion": 3}, "entryType": "UniProtKB reviewed (Swiss-Prot)",
               "sequence": {"value": "AAA"}}
        indexed = {"schema": "gx.yeast-enzyme-candidates/1", "taxid": 559292,
                   "candidates": [{"accession": accession, "sequence_sha256": sha256(b"AAA").hexdigest(), "sequence_version": 3}]}
        raw = (json.dumps(doc, indent=2) + "\n").encode()
        original, descriptor, summary = method["fetch"](indexed, accession, lambda _: raw)
        self.assertEqual(original, raw)
        self.assertEqual(descriptor["sha256"], sha256(raw).hexdigest())
        self.assertEqual(descriptor["external_source"]["version"], "12")
        self.assertTrue(summary["matches_index_sequence"])
        for bad in (dict(doc, organism={"taxonId": 4932}), dict(doc, primaryAccession="P00724"),
                    dict(doc, entryAudit={"sequenceVersion": 3}), dict(doc, entryType="unreviewed")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                method["fetch"](indexed, accession, lambda _: json.dumps(bad).encode())
        with self.assertRaises(ValueError):
            method["fetch"](indexed, "P00724", lambda _: self.fail("Out-of-result lookup called external provider"))
        doc["sequence"]["value"] = "BBBB"
        _, _, changed = method["fetch"](indexed, accession, lambda _: json.dumps(doc).encode())
        self.assertFalse(changed["matches_index_sequence"])

    @unittest.skipUnless(Path(mount["enzyme_assets"]).is_dir(), "versioned enzyme-data mount unavailable")
    def test_two_local_inputs_keep_scope_and_primary_paper_distinctions(self):
        tre, _ = method["run_lookup"](scope, "trehalose", mount)
        by_id = {r["accession"]: r for r in tre["candidates"]}
        self.assertEqual(set(by_id), {"P32356", "P35172", "P48016"})
        for accession, row in by_id.items():
            self.assertEqual([a["direction"] for a in row["associations"]], ["unknown"])
            self.assertEqual(len(row["catalytic_blocks"]), 1)
            self.assertIn("rhea2uniprot_sprot.tsv:line:", row["associations"][0]["locator"])
        self.assertEqual(by_id["P35172"]["experimental_paper_pointers"], [])
        self.assertEqual(set(by_id["P32356"]["experimental_paper_pointers"]), {"22320399","29087344"})
        self.assertEqual(tre["counts"]["rhea_rows"] + tre["counts"]["enzyme_rows"], 6)
        self.assertEqual(tre["counts"]["distinct_accessions"], 3)
        sucrose, _ = method["run_lookup"](scope, "sucrose", mount)
        self.assertEqual(sucrose["counts"]["rhea_rows"], 0)
        self.assertEqual(sucrose["candidates"][0]["specificity"], "enzyme_class_only")
        self.assertEqual(sucrose["candidates"][0]["accession"], "P00724")


if __name__ == "__main__":
    unittest.main()
