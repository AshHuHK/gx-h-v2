"""Public depositions and counterexamples for chemistry-aware coverage."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
from Bio.PDB.MMCIF2Dict import MMCIF2Dict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
WORKER = ROOT / "gx-h/plugins/installed/org.gx.archival-molecule-qc/1.0.0/job-scripts/peptide.py"
CYCLIC = ROOT / "gx-h/plugins/installed/org.gx.deposited-structure-qc/1.0.0/job-scripts/qc.py"
CYCLIC_MATERIALS = ROOT / "acceptance/science_capabilities/structure_controls/materials"


def worker(path):
    spec = spec_from_file_location("qc_worker", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchivalPeptideTests(unittest.TestCase):
    def test_chignolin_and_trpzip_position_denominators(self):
        method = worker(WORKER)
        chig = method.measure(HERE / "materials/1UAO.cif")
        self.assertEqual((10, 10, 10, 18), tuple(chig[k] for k in
                         ("declared_positions", "observed_positions", "backbone_positions", "model_count")))
        self.assertEqual(chig["annotation_status"], "category_absent")
        self.assertEqual(chig["non_peptide_components"], [])
        trp = method.measure(HERE / "materials/1LE0.cif")
        self.assertEqual((13, 13, 12, 12, 20), tuple(trp[k] for k in
                         ("declared_positions", "observed_positions", "peptide_residue_positions", "backbone_positions", "model_count")))
        self.assertTrue(all(r["observed_positions"] == 13 and r["backbone_positions"] == 12 for r in trp["models"]))
        cap, = trp["non_peptide_components"]
        self.assertEqual((13, "NH2"), (cap["label_seq_id"], cap["comp_id"]))
        link, = cap["in_chain_covalent_links"]
        self.assertEqual(("covale1", 12), (link["id"], link["partner_position"]))
        self.assertIsNone(trp["predicted_confidence"])

    def test_absent_declaration_and_missing_component_metadata(self):
        cif = MMCIF2Dict(HERE / "materials/1LE0.cif")
        for name in tuple(cif):
            if name.startswith("_struct_conn."):
                del cif[name]
        cif["_chem_comp.type"][cif["_chem_comp.id"].index("NH2")] = "?"
        result = worker(WORKER).summarize(cif)
        self.assertEqual([13], result["unknown_chemistry_positions"])
        self.assertEqual([], result["non_peptide_components"])
        self.assertEqual("category_absent", result["connection_status"])
        self.assertEqual(13, result["declared_positions"])

    def test_same_model_and_compatible_alternate_required_for_backbone(self):
        cif = MMCIF2Dict(HERE / "materials/1UAO.cif")
        for i, (model, pos, atom) in enumerate(zip(cif["_atom_site.pdbx_PDB_model_num"],
                cif["_atom_site.label_seq_id"], cif["_atom_site.label_atom_id"])):
            if model == "1" and pos == "2" and atom in ("N", "CA", "C", "O"):
                cif["_atom_site.label_alt_id"][i] = "A" if atom == "CA" else "B"
        result = worker(WORKER).summarize(cif)
        self.assertEqual(9, result["backbone_positions"])
        self.assertEqual(10, result["models"][1]["backbone_positions"])

    def test_explicit_modification_and_wrong_chain_scope(self):
        cif = MMCIF2Dict(HERE / "materials/1UAO.cif")
        for name, value in {"label_asym_id": ["A","B"], "label_seq_id": ["1","1"],
                            "label_comp_id":["GLY","GLY"], "parent_comp_id":["?","?"],
                            "details":["test annotation", "elsewhere"]}.items():
            cif["_pdbx_struct_mod_residue."+name] = value
        result = worker(WORKER).summarize(cif)
        self.assertEqual(result["declared_modified_positions"], 1)
        self.assertEqual(result["modified_residues"][0]["parent_comp_id"], None)

    def test_unchanged_archived_macrocycle_and_deletion_control(self):
        measure = worker(CYCLIC).measure
        original = measure(CYCLIC_MATERIALS / "6BEN.cif", "macrocycle")
        control = measure(CYCLIC_MATERIALS / "6BEN-minus-closure.cif", "macrocycle")
        self.assertEqual((1, 0), (original["declared_closure"], control["declared_closure"]))
        self.assertEqual(original["models"], control["models"])
        self.assertEqual((20, ["covale2"]), (original["mapped_models"], original["closure_link_ids"]))


if __name__ == "__main__":
    unittest.main()
