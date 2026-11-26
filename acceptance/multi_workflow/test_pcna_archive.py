"""Check archive discovery, deposition identity and the outbound route against the released methods."""
import hashlib
import importlib.util
import json
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from Bio.PDB import MMCIFIO
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from adapters.scientific import archive_coordinates
from acceptance.multi_workflow.test_pcna_geometry import selection

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "gx-h/plugins/installed/org.gx.pcna-archive/1.0.0"
SPEC = importlib.util.spec_from_file_location("pcna_archive_worker", PACKAGE / "methods/archive.py")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)
REQUEST = {"schema": archive.REQUEST, "accession": "P12004"}
ENTRY = ROOT / "examples/pcna/data/1VYM.cif"


def record():
    return {"primaryAccession":"P12004","organism":{"taxonId":9606},"entryAudit":{"entryVersion":7},
            "uniProtKBCrossReferences":[{"database":"PDB","id":"1VYM","properties":[{"key":"Method","value":"X-ray"}]},
                                         {"database":"PDB","id":"3JA9","properties":[]},
                                         {"database":"GO","id":"GO:0000000","properties":[]}]}


def cif_bytes(document):
    stream = StringIO()
    writer = MMCIFIO()
    writer.set_dict(document)
    writer.save(stream)
    return stream.getvalue().encode("utf-8")


def duplicate_row(document, category, original, **changes):
    for key, column in document.items():
        if key.startswith(category + "."):
            column.append(changes.get(key.split(".", 1)[1], column[original]))


class ArchiveMethods(unittest.TestCase):
    def test_live_selection_and_pinned_input_return_scope_and_exact_source(self):
        raw = json.dumps(record()).encode()
        online = Mock(return_value=raw)
        selected = archive.search(json.dumps({**REQUEST, "entry_ids":["1vym"]}).encode(), online)
        online.assert_called_once_with(archive.UNIPROT)
        self.assertEqual(selected["record_version"], 7)
        self.assertEqual(selected["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(selected["source_mode"], "live_uniprot_record")
        self.assertEqual(selected["candidates"], [{"entry_id":"1VYM","method":"X-ray","resolution":None,"chains":None}])
        local = archive.search(raw, Mock(side_effect=AssertionError("Unexpected download")))
        self.assertEqual(local["source_mode"], "provided_uniprot_record")
        self.assertEqual([r["entry_id"] for r in local["candidates"]], ["1VYM","3JA9"])
        for change in ({"organism":{"taxonId":10090}}, {"primaryAccession":"OTHER"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Homo sapiens"):
                archive.search(json.dumps(record() | change).encode())

    def test_fetch_preserves_original_bytes_and_canonical_existing_descriptor(self):
        raw = ENTRY.read_bytes()
        expected = json.loads(ENTRY.with_suffix(".json").read_bytes())
        for selection in (REQUEST, {**REQUEST, "entry_ids":["1VYM"]},
                          {"schema":archive.CANDIDATES,"accession":"P12004","candidates":[{"entry_id":"1VYM"}]}):
            with self.subTest(selection=selection):
                download = Mock(return_value=raw)
                observed, meta, context = archive.fetch(selection, "1vym", archive_coordinates, download)
                download.assert_called_once_with("https://files.rcsb.org/download/1VYM.cif")
                self.assertEqual(observed, raw)
                self.assertEqual(meta, expected)
                self.assertEqual(context["structure_sha256"], meta["sha256"])
                self.assertEqual(context["experimental_methods"], ["X-RAY DIFFRACTION"])
                self.assertTrue(any(r["value_A"] == "2.30" or float(r["value_A"]) == 2.3 for r in context["resolution"]))
                self.assertIsNone(context["apo_status"])
                proof = archive_coordinates.verify("chemical/x-mmcif", [meta["source"],meta["data"]], meta["sha256"],
                                                  lambda: observed, [], [], None, verify_revision=True)
                self.assertEqual(proof["entry_id"], "1VYM")

    def test_linked_only_accession_qualifies_and_stamps_the_downloaded_original(self):
        original_descriptor = json.loads(ENTRY.with_suffix(".json").read_bytes())
        for primary in ("?", None):
            with self.subTest(primary=primary):
                doc = MMCIF2Dict(str(ENTRY))
                self.assertEqual(set(doc["_struct_ref_seq.pdbx_db_accession"]), {"P12004"})
                if primary is None:
                    del doc["_struct_ref.pdbx_db_accession"]
                else:
                    doc["_struct_ref.pdbx_db_accession"] = [primary]
                doc["_struct_ref.db_name"] = ["UNIPROTKB"]
                raw = cif_bytes(doc)
                expected = selection.select(raw)
                observed, meta, context = archive.fetch(REQUEST, "1VYM", archive_coordinates, Mock(return_value=raw))
                self.assertEqual((expected.entity, expected.chains), ("1", ("A", "B", "C")))
                self.assertEqual(observed, raw)
                self.assertEqual(meta["source"], original_descriptor["source"])
                self.assertEqual(meta["applicability"], original_descriptor["applicability"])
                self.assertEqual(meta["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(context["structure_sha256"], meta["sha256"])
                entity, = context["pcna"]["entities"]
                self.assertEqual((entity["entity_id"], tuple(entity["label_chains"])), (expected.entity, expected.chains))

    def assert_ineligible(self, document):
        raw = cif_bytes(document)
        with self.assertRaises(ValueError):
            selection.select(raw)
        with self.assertRaises(ValueError):
            archive.fetch(REQUEST, "1VYM", archive_coordinates, Mock(return_value=raw))

    def test_conflicting_linked_accession_is_ineligible(self):
        doc = MMCIF2Dict(str(ENTRY))
        doc["_struct_ref_seq.pdbx_db_accession"][0] = "P99999"
        self.assert_ineligible(doc)

    def test_chains_from_two_human_pcna_entities_cannot_form_one_qualified_trimer(self):
        doc = MMCIF2Dict(str(ENTRY))
        duplicate_row(doc, "_entity", 0, id="77")
        duplicate_row(doc, "_entity_poly", 0, entity_id="77")
        duplicate_row(doc, "_struct_ref", 0, id="77", entity_id="77")
        duplicate_row(doc, "_entity_src_gen", 0, entity_id="77")
        for chains, entities in (("_struct_asym.id", "_struct_asym.entity_id"),
                                 ("_atom_site.label_asym_id", "_atom_site.label_entity_id")):
            for i, chain in enumerate(doc[chains]):
                if chain == "C":
                    doc[entities][i] = "77"
        self.assert_ineligible(doc)

    def test_four_declared_chains_remain_ineligible_when_only_three_have_coordinates(self):
        doc = MMCIF2Dict(str(ENTRY))
        duplicate_row(doc, "_struct_asym", 0, id="UNOBSERVED")
        self.assertNotIn("UNOBSERVED", doc["_atom_site.label_asym_id"])
        self.assert_ineligible(doc)

    def test_partner_and_experimental_metadata_are_deposition_specific(self):
        raw = (ROOT / "examples/pcna/data/9VGW.cif").read_bytes()
        _, _, context = archive.fetch(REQUEST, "9VGW", archive_coordinates, Mock(return_value=raw))
        partners = context["other_deposited_polymers"]["entities"]
        self.assertTrue(partners and all(r["description"] and r["label_chains"] for r in partners))
        self.assertTrue(all(r["entity_id"] != context["pcna"]["entities"][0]["entity_id"] for r in partners))
        self.assertIsNone(context["direct_binding"])
        self.assertIsNone(context["ligand_roles"])

    def test_authorization_and_provider_identity_precede_publication(self):
        no_download = Mock(side_effect=AssertionError("Unexpected download"))
        for source, entry in (({**REQUEST, "entry_ids":["1VYM"]}, "3JA9"), (REQUEST, "1VYM?x=1"),
                              ({**REQUEST, "url":"https://elsewhere.invalid"}, "1VYM")):
            with self.subTest(source=source, entry=entry), self.assertRaises(ValueError):
                archive.fetch(source, entry, archive_coordinates, no_download)
        with self.assertRaisesRegex(ValueError, "differs"):
            archive.fetch(REQUEST, "3JA9", archive_coordinates, Mock(return_value=ENTRY.read_bytes()))
        with self.assertRaisesRegex(ValueError, "verified archive revision"):
            archive.fetch(REQUEST, "1VYM", archive_coordinates, Mock(return_value=b"data_1VYM\n_entry.id 1VYM\n"))

    def test_qualifies_real_entities_and_chain_mappings_without_assuming_labels(self):
        doc = {"_struct_ref.id":["1"], "_struct_ref.db_name":["UNP"], "_struct_ref.entity_id":["8"], "_struct_ref.pdbx_db_accession":["P12004"],
               "_entity_src_gen.entity_id":["8"], "_entity_src_gen.pdbx_gene_src_ncbi_taxonomy_id":["9606"],
               "_entity_poly.entity_id":["8"], "_entity_poly.type":["polypeptide(L)"],
               "_struct_asym.id":list("LMN"), "_struct_asym.entity_id":["8"]*3, "_atom_site.group_PDB":["ATOM"]*3,
               "_atom_site.pdbx_PDB_model_num":["1"]*3, "_atom_site.label_asym_id":list("LMN"),
               "_atom_site.label_entity_id":["8"]*3, "_pdbx_struct_assembly_gen.assembly_id":["2"],
               "_pdbx_struct_assembly_gen.oper_expression":["1"], "_pdbx_struct_assembly_gen.asym_id_list":["J,L,M,N"]}
        self.assertEqual(archive.pcna_chains(doc), (list("LMN"), ["8"]))
        for change in ({"_entity_src_gen.pdbx_gene_src_ncbi_taxonomy_id":["10090"]},
                       {"_entity_poly.type":["polydeoxyribonucleotide"]},
                       {"_atom_site.label_entity_id":["8","other","8"]}, {"_atom_site.pdbx_PDB_model_num":["1","2","1"]},
                       {"_struct_ref.pdbx_db_accession":["OTHER"]},
                       {"_pdbx_struct_assembly_gen.asym_id_list":["L,M"]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                archive.pcna_chains(doc | change)

    def test_https_redirect_stays_on_provider_and_only_connects_to_clash(self):
        with patch.dict("os.environ", {"https_proxy":"http://127.0.0.1:7890"}), \
                patch.object(archive.http.client, "HTTPSConnection") as connect:
            conn=connect.return_value
            response=conn.getresponse.return_value
            response.status=302
            response.getheader.return_value="https://other.invalid/cif"
            with self.assertRaisesRegex(ValueError, "original HTTPS provider"):
                archive.retrieve(archive.UNIPROT)
            connect.assert_called_once_with("127.0.0.1", 7890, timeout=25)
            conn.set_tunnel.assert_called_once_with("rest.uniprot.org",443)
        with patch.dict("os.environ", {"https_proxy":"http://unapproved.invalid:42"}), \
                patch.object(archive.http.client, "HTTPSConnection") as connect:
            with self.assertRaisesRegex(ValueError, "Clash"):
                archive.retrieve(archive.UNIPROT)
            connect.assert_not_called()

    def test_manifests_bind_the_worker_and_observed_original_to_the_descriptor(self):
        digest = hashlib.sha256((PACKAGE/"methods/archive.py").read_bytes()).hexdigest()
        for name in ("search", "fetch"):
            manifest = json.loads((PACKAGE/f"methods/{name}.json").read_bytes())["manifest"]
            self.assertEqual(manifest["source"]["sha256"],digest)
            outputs = {out["name"]:out for out in manifest["outputs"]}
            if name == "fetch":
                self.assertEqual(outputs["structure.cif"]["acquired_material"],
                                 {"metadata_output":"acquisition.json","role":"structure"})
                self.assertEqual(outputs["structure.cif"]["nature"],"observed")
                self.assertEqual(outputs["structure.cif"]["media_type"],"chemical/x-mmcif")
                self.assertTrue(outputs["acquisition.json"]["required"])
                self.assertEqual(outputs["acquisition.json"]["media_type"],"application/json")


if __name__ == "__main__":
    unittest.main()
