import gzip
import importlib.util
import json
from pathlib import Path
import unittest

from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from acceptance.science_assets.modalities.fab.independent import count

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parents[3] / 'gx-h/plugins/installed/org.gx.archival-molecule-qc/1.0.0/job-scripts/fab.py'
spec = importlib.util.spec_from_file_location('archival_fab_qc', SCRIPT)
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)


def read_data(code):
    with gzip.open(HERE/'materials'/(code + '.cif.gz'), 'rt') as stream:
        structure = MMCIF2Dict(stream)
    return structure, json.loads((HERE/'materials'/(code + '.imgt.json')).read_bytes())


class FabArchiveTests(unittest.TestCase):
    def test_both_public_fabs_match_separate_archive_reader(self):
        for code, expected in [('1mcp', 55), ('4fab', 50)]:
            with self.subTest(code=code):
                qc_data = qc.measure(HERE/'materials'/(code + '.cif.gz'), HERE/'materials'/(code + '.imgt.json'))
                other = count(code, HERE/'materials')
                self.assertEqual(qc_data['mapped_regions'], 6)
                self.assertEqual((qc_data['mapped_positions'], qc_data['matched_positions']), (expected, expected))
                self.assertEqual(qc_data['scheme'], 'IMGT')
                for chain, regions in other['by_role_cdr1_2_3'].items():
                    for i, region in enumerate(regions, 1):
                        actual = qc_data['chains'][chain]['cdr']['CDR'+str(i)]
                        self.assertEqual((actual['mapped_positions'], actual['matched_positions']),
                                         (region['mapped'], region['matched']))

    def test_cross_deposition_numbering_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'different mmCIF'):
            qc.measure(HERE/'materials/4fab.cif.gz', HERE/'materials/1mcp.imgt.json')

    def test_swapped_chain_roles_are_rejected(self):
        cif, doc = read_data('1mcp')
        s = doc['chains']
        s['VH'], s['VL'] = s['VL'], s['VH']
        with self.assertRaisesRegex(ValueError, 'role mismatch'):
            qc.chain_measurements(cif, doc)

    def test_missing_chain_from_assembly_is_rejected(self):
        cif, doc = read_data('1mcp')
        rows = cif['_pdbx_struct_assembly_gen.asym_id_list']
        rows[0] = ','.join(c for c in rows[0].split(',') if c != doc['chains']['VL']['label_asym_id'])
        with self.assertRaisesRegex(ValueError, 'exactly the assigned two'):
            qc.chain_measurements(cif, doc)

    def test_unobserved_light_chain_keeps_its_mapped_denominator(self):
        cif, doc = read_data('4fab')
        for i, label in enumerate(cif['_atom_site.label_asym_id']):
            if label == doc['chains']['VL']['label_asym_id']:
                cif['_atom_site.occupancy'][i] = '0.0'
        data = qc.chain_measurements(cif, doc)
        self.assertEqual(sum(c['matched_positions'] for c in data['VL']['cdr'].values()), 0)
        self.assertEqual(sum(c['mapped_positions'] for c in data['VL']['cdr'].values()), 23)
        self.assertEqual(sum(c['matched_positions'] for c in data['VH']['cdr'].values()), 27)


if __name__ == '__main__':
    unittest.main()
