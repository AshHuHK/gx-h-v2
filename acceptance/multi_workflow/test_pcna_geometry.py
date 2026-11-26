"""Published PCNA geometry controls plus synthetic chain, occupancy and uncertainty cases."""
from pathlib import Path
import importlib.util
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO/"gx-h"
METHODS = HARNESS/"plugins/installed/org.gx.pcna-structure/1.0.0/methods"


def module(name):
    directory = METHODS if name=="geometry_metrics" else METHODS.parent/"job-scripts"
    spec = importlib.util.spec_from_file_location("test_pcna_"+name, directory/(name+".py"))
    obj = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = obj
    spec.loader.exec_module(obj)
    return obj


selection, geometry, metrics = (module(name) for name in ("pcna_selection","geometry","geometry_metrics"))
ENGINE = geometry.load("pcna_geom_test_block",HARNESS/"plugins/installed/org.gx.block-influence/1.0.0/methods/block_influence.py")
FITTER = ENGINE.load_fitter(HARNESS/"adapters/scientific/tl1a_geometry/worker.py")
PARAMS = dict(mask="common_ca",endpoint="rmsd_A",effect_minimum=.05,zero_limit=.01,minimum_evaluable=10,pair="12",block_index=7)


def cif_source():
    """Entity 2, three non-ABC label chains, decoy partner chain and alternate atom locations."""
    header = """data_MODEL
_entry.id MODEL
loop_
_struct_ref.id
_struct_ref.entity_id
_struct_ref.db_name
_struct_ref.pdbx_db_accession
1 1 UNP P99999
2 2 UNP P12004
loop_
_entity_poly.entity_id
_entity_poly.type
1 'polypeptide(L)'
2 'polypeptide(L)'
loop_
_struct_asym.id
_struct_asym.entity_id
A 1
ETA 2
X 2
Y1 2
loop_
_entity_poly_seq.entity_id
_entity_poly_seq.num
2 1
2 2
2 3
2 4
loop_
"""
    atom_fields = "group_PDB pdbx_PDB_model_num label_entity_id label_asym_id label_seq_id label_comp_id label_atom_id type_symbol label_alt_id occupancy B_iso_or_equiv Cartn_x Cartn_y Cartn_z"
    lines = [header,*["_atom_site."+s for s in atom_fields.split()]]
    for chain in ("Y1","X","ETA"):
        for pos in (1,2,3,4):
            for atom,el in (("N","N"),("CA","C"),("C","C"),("O","O"),("CB","C")):
                variants = [(".",1.,2)]
                if atom in ("CA","CB"):
                    if pos==1:
                        variants = [("B",.6,16),("A",.4,8)]
                    elif pos==2:
                        variants = [("B",.5,12),("A",.5,4)]
                    elif pos==3:
                        variants = [("B",.6,12),("A",.4,5)] if atom=="CA" else [("B",.4,12),("A",.6,5)]
                for alt,occ,x in variants:
                    lines.append(f"ATOM 1 2 {chain} {pos} ALA {atom} {el} {alt} {occ} 20 {x} {pos} 2")
            lines.append(f"ATOM 1 2 {chain} {pos} ALA H H . 1 20 1 1 1")
    lines.extend(["ATOM 1 1 A 2 ARG CA C . 1 10 0 0 0","ATOM 2 2 X 3 GLY CA C . 1 5 0 0 0"])
    return ("\n".join(lines)+"\n").encode()


class SelectionTests(unittest.TestCase):
    def test_label_entity_altloc_tie_and_conflict(self):
        s = selection.select(cif_source())
        self.assertEqual((s.entity,s.chains),("2",("ETA","X","Y1")))
        c = s.molecules["X"]
        self.assertEqual((c[1]["CA"].coord[0], c[2]["CA"].coord[0]),(16.,4.))
        self.assertEqual(s.alternate_residues["X"],{1:"B",2:"A"})
        self.assertEqual(s.excluded_residues["X"],{3:"incompatible_highest_occupancy_conformers"})
        self.assertNotIn("H",c[1])
        coords,keys,union = geometry.atom_keys(s,"common_backbone")
        self.assertEqual(len(coords),3)
        self.assertEqual(len(keys),12)
        self.assertEqual(union,{1,2,3,4})

    def test_ambiguous_entities_and_asymmetric_unit_require_resolution(self):
        raw = cif_source()
        for broken in (raw.replace(b'P99999',b'P12004'),raw.replace(b'ETA 2',b'ETA 1'),
                       raw.replace(b'Y1 2\nloop_',b'Y1 2\nZ 2\nloop_')):
            with self.subTest(raw=broken[:12]), self.assertRaises(ValueError):
                selection.select(broken)

    def test_linked_reference_accession_fallback_and_conflict(self):
        extra = b"loop_\n_struct_ref_seq.ref_id\n_struct_ref_seq.pdbx_db_accession\n2 P12004\n"
        raw = cif_source().replace(b"loop_\n_entity_poly.entity_id",extra+b"loop_\n_entity_poly.entity_id")
        self.assertEqual(selection.select(raw.replace(b"2 2 UNP P12004",b"2 2 UNP ?")).entity,"2")
        with self.assertRaisesRegex(ValueError,"Conflicting"):
            selection.select(raw.replace(b"2 2 UNP P12004",b"2 2 UNP WRONG"))


def small_table(pair,med,control, count=20):
    row = {"delta":pair,"control_count":count,"control_max":control}
    other = {"delta":.0,"control_count":count,"control_max":control}
    return {"schema":"gx.pcna-geometry-table/1","mask":"common_ca","endpoint":"rmsd_A","selected_residues":88,
            "observed_union_residues":90,"baseline":{"12":1.,"23":1.,"31":1.},
            "blocks":[{"block_index":7,"eligible":True,"median":{"delta":med},"pairs":{"12":row,"23":other,"31":other}}]}


class ControlTests(unittest.TestCase):
    def test_robust_effect_and_noise_bounds_and_missing_controls(self):
        par = {**PARAMS,"minimum_evaluable":1}
        for change,med,ctrl,n,expected in [
                (.080,.005,.020,20,1), (-.080,.070,.020,20,1), (.080,.070,.020,20,0),
                (.060,.025,.080,20,0), (.060,.025,.059,20,None), (.050,.025,.001,20,None),
                (.100,.048,.001,20,None), (.090,.001,None,19,None), (.020,.010,None,0,0)]:
            with self.subTest(change=change,med=med,ctrl=ctrl,n=n):
                result = metrics.assess(small_table(change,med,ctrl,n),par)
                self.assertEqual(result["noise_resolved_missed_present"],expected)

    def test_native_prior_numeric_requires_an_observed_readout(self):
        t = small_table(.06,.025,.059)
        par = {**PARAMS,"minimum_evaluable":1}
        def known(metric,settings,table=t):
            return metrics.sufficient_metric(settings,metric,metrics.METHOD,"geometry-table.json",table)
        self.assertTrue(known("missed_present",par))
        self.assertFalse(known("noise_resolved_missed_present",par))
        self.assertFalse(known("missed_present",PARAMS))
        self.assertTrue(known("pair_delta_A",par))
        self.assertFalse(known("pair_delta_deg",par))
        self.assertFalse(known("pair_delta_A",{**par,"block_index":43}))
        t["blocks"][0]["median"]["delta"] = None
        t["blocks"][0]["pairs"]["23"]["delta"] = None
        self.assertTrue(known("pair_delta_A",par))
        self.assertFalse(known("median_delta_A",par))
        self.assertFalse(known("missed_present",par))


class PublishedTests(unittest.TestCase):
    def test_ca_141_160_boundary_and_dispersed_controls(self):
        # The archived independent SVD used these versions of the deposited originals.
        old = [("data/1VYM.cif","22b94186ce87e4d0b4a8e48c47ed129cea5b9900d5f6b4f9820833e879490b02",
                .04999839288375596,"quiet",1),
               ("open-data/3JA9.cif","71782ebe0e53793db2991a2e96c7863878564105fe618bcbe3e0949547993382",
                .05000045977170853,"attenuated",2)]
        for path,digest,delta,label,missed in old:
            with self.subTest(path=path):
                s = selection.select((REPO/"examples/pcna"/path).read_bytes())
                self.assertEqual(s.input_sha256,digest)
                t = geometry.physical_table(s,"common_ca","rmsd_A",ENGINE,FITTER)
                self.assertEqual(t["pair_labels"]["31"],["C","A"])
                out = metrics.assess(t,PARAMS,include_rows=True)
                row = out["annotations"][7]
                self.assertAlmostEqual(out["pair_delta_A"],delta,places=10)
                self.assertEqual((out["blocks_evaluable"],out["blocks_missed"]),(13,missed))
                self.assertEqual((out["blocks_noise_resolved_missed"],out["noise_resolved_missed_present"]),(1,1))
                self.assertEqual((row["classification"],row["noise_resolved_missed"],row["pair_effect_boundary"]),(label,False,["12"]))
                self.assertGreater(out["control_max_A"],abs(out["pair_delta_A"])+.004)
                b = t["blocks"][7]
                sets = [tuple(c["omitted_positions"]) for c in b["controls"]]
                self.assertEqual(len(set(sets)),20)
                for positions in sets:
                    self.assertEqual(len(positions),len(b["omitted_positions"]))
                    self.assertFalse(set(positions) & set(b["omitted_positions"]))
                    self.assertGreaterEqual(len({(p-1)//20 for p in positions}),3)
                self.assertEqual(set(b["controls"][0]["pair_deltas"]),{"12","23","31"})


if __name__=="__main__":
    unittest.main()
