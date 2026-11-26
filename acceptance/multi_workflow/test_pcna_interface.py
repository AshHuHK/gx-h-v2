"""Check contact, burial and missingness conventions for the public PCNA interface method."""
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

from Bio.PDB.Atom import Atom
from Bio.PDB.Chain import Chain
from Bio.PDB.Residue import Residue
import numpy as np

DIRECTORY = (Path(__file__).resolve().parents[2] / "gx-h/plugins/installed"
             / "org.gx.pcna-structure/1.0.0/job-scripts")


def load(name, directory=DIRECTORY):
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def molecule(name, sites):
    chain = Chain(name)
    for position, x in sites:
        res = Residue((" ",position," "), "ALA", "")
        chain.add(res)
        res.add(Atom("CA", np.array([x,0.,0.]),0.,1.," "," CA ",position,"C"))
    return chain


class Interfaces(unittest.TestCase):
    def test_residue_contact_pair_not_atom_count_and_analytical_burial(self):
        api=load("interfaces")
        names=("D","E","F")
        a=molecule("D",[(1,0.),(25,50.)])
        b=molecule("E",[(1,4.)])
        c=molecule("F",[(1,100.)])
        selection=SimpleNamespace(chains=names,molecules=dict(zip(names,(a,b,c))),declared_positions=(1,25),
                                  entry="TEST",entity="2",model="1",input_sha256="test")
        summary,table=api.measure(selection)
        self.assertEqual((summary["chain_left"],summary["chain_right"]), ("D","E"))
        self.assertEqual(summary["n_contact_residue_pairs"],1)
        # Two expanded carbon spheres (vdW 1.7 + probe 1.4), d=4 Å; two hidden spherical caps.
        radius=3.1
        expected=4*math.pi*radius*(radius-2)
        self.assertAlmostEqual(summary["buried_area_A2"],expected,delta=4.)
        self.assertEqual(table["pairs"]["23"]["whole"]["buried_area_A2"],0.)
        window=table["pairs"]["12"]["windows"][1]
        self.assertEqual(window["observed_positions"],[1,0])
        self.assertIsNone(window["n_contact_residue_pairs"])
        selector=load("interface_known", DIRECTORY.parent / "methods")
        self.assertTrue(selector.sufficient_metric({}, "n_contact_residue_pairs", "pcna-interface@1",
                                                  "interface-table.json", table))
        def known(key,block):
            return selector.sufficient_metric({"pair":key,"block_index":block},"buried_area_A2",
                "pcna-interface@1","interface-table.json",table)
        self.assertTrue(known("23",-1))
        self.assertFalse(known("12",1))
        # A second contacting heavy atom on the same residue must not count the contact twice.
        a[1].add(Atom("CB",np.array([1.,0.,0.]),0.,1.," "," CB ",31,"C"))
        self.assertEqual(api.contacting_residues(a,b),{(1,1)})

    def test_frozen_reference_counts_and_local_burial(self):
        import sys
        spec=importlib.util.spec_from_file_location("gx_pcna_selector_test",DIRECTORY/"pcna_selection.py")
        selector=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=selector
        self.addCleanup(sys.modules.pop,spec.name,None)
        spec.loader.exec_module(selector)
        cif=Path(__file__).resolve().parents[2]/"examples/pcna/data/1VYM.cif"
        summary,tab=load("interfaces").measure(selector.select(cif.read_bytes()),"31",4)
        self.assertEqual([tab["pairs"][p]["whole"]["n_contact_residue_pairs"] for p in ("12","23","31")],[33,30,36])
        self.assertEqual(summary["label_interval"],[81,100])
        for p in tab["pairs"].values():
            self.assertGreater(p["whole"]["buried_area_A2"],0.)
            self.assertAlmostEqual(p["whole"]["buried_area_A2"],
                                   sum(w["buried_area_A2"] or 0 for w in p["windows"]),places=6)
