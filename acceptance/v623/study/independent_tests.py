"""Synthetic cases for the independent numerical review; no deposited coordinates are opened."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from hashlib import sha256
import math
import unittest

import numpy as np

from . import independent_geometry as ref
from .independent_compare import compare


def fabricated(*, perturb=False, motion=False, coordinate_count=104):
    fields = ('group_PDB pdbx_PDB_model_num label_entity_id label_asym_id label_seq_id label_comp_id label_atom_id '
              'type_symbol label_alt_id occupancy B_iso_or_equiv Cartn_x Cartn_y Cartn_z').split()
    lines = ['data_QCX', '_entry.id QCX', 'loop_', '_entity_poly_seq.entity_id', '_entity_poly_seq.num',
             *(f'1 {i}' for i in range(1, 141)), '#', 'loop_', *('_atom_site.' + f for f in fields)]
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    for index, chain in enumerate('ABC'):
        theta = .15 * index
        rot = np.array([[math.cos(theta), -math.sin(theta), 0], [math.sin(theta), math.cos(theta), 0], [0, 0, 1]])
        for i in range(1, coordinate_count + 1):
            for a, name in enumerate(('N', 'CA', 'C', 'O')):
                xyz = np.array([i * .2, math.cos(i * .1) * 5 + .3 * a, math.sin(i * .4) * 7 - .1 * a])
                xyz = rot @ xyz + index
                if perturb and chain == 'C' and i <= 20:
                    xyz[2] += 11
                if motion:
                    xyz = rotation @ xyz + [81, -43, 5]
                coord = ' '.join(f'{v:.9f}' for v in xyz)
                lines.append(f'ATOM 1 1 {chain} {i} ALA {name} {ref.ATOMS[name]} . 1 20 {coord}')
    return ('\n'.join(lines) + '\n').encode()


def compute(content, mask='common_backbone'):
    return ref.reference(content, 'QCX', sha256(content).hexdigest(), mask)


class NumericalReview(unittest.TestCase):
    def test_public_digest_scope_and_mismatch_fail_closed(self):
        raw = fabricated(coordinate_count=1)
        with self.assertRaisesRegex(ValueError, 'digest'):
            ref.reference(raw, 'QCX', '0' * 64, 'common_ca')
        with self.assertRaisesRegex(ValueError, 'entry'):
            ref.reference(raw, 'XXX', sha256(raw).hexdigest(), 'common_ca')
        with self.assertRaisesRegex(ValueError, 'occupancy'):
            compute(raw.replace(b' . 1 20 ', b' . -1 20 '))
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            compute(raw.replace(b' . 1 20 ', b' A 1 20 '))

    def test_sparse_positions_and_nonunique_fit_stay_na(self):
        sparse = compute(fabricated(coordinate_count=40))
        self.assertIsNotNone(sparse['baseline']['AB']['angle_deg'])
        self.assertEqual(sum(b['eligible'] for b in sparse['blocks']), 0)
        self.assertEqual(len(sparse['blocks']), 7)
        xyz = np.array([[i, 0., 0.] for i in range(100)])
        self.assertIsNone(ref.proper_fit(xyz, xyz + [2, 4, 8]))
        # Three noncollinear coplanar points do have a unique proper rotation.
        self.assertIsNotNone(ref.proper_fit(np.eye(3), np.eye(3)))

    def test_block_influence_under_common_rigid_motion_and_distributed_control_identity(self):
        raw = fabricated(perturb=True)
        plain = compute(raw)
        moved = compute(fabricated(perturb=True, motion=True))
        unperturbed = compute(fabricated())
        self.assertEqual(plain['positions_sha256'], moved['positions_sha256'])
        self.assertNotEqual(plain['input_sha256'], moved['input_sha256'])
        self.assertEqual(plain['matched_atoms'], 416)
        self.assertEqual([b['eligible'] for b in plain['blocks']], [True]*5 + [False]*2)
        block = plain['blocks'][0]
        self.assertGreater(block['pairs']['AC']['delta_rmsd_A'], .15)
        self.assertLess(unperturbed['blocks'][0]['pairs']['AC']['delta_rmsd_A'], .001)
        self.assertEqual(len(block['controls']), 20)
        sample = [c['positions'] for c in block['controls']]
        self.assertEqual(len(set(map(tuple, sample))), 20)
        self.assertTrue(all(len(s)==20 and min(s)>20 and len({(i-1)//20 for i in s})>=3 for s in sample))
        self.assertTrue(any(any(i>100 for i in s) for s in sample))
        for pair in ref.PAIRS:
            for metric, tolerance in ref.FIT_TOL.items():
                self.assertAlmostEqual(plain['baseline'][pair][metric], moved['baseline'][pair][metric], delta=tolerance)
                for b, m in zip(plain['blocks'][:5], moved['blocks'][:5], strict=True):
                    for endpoint in (metric, 'delta_' + metric):
                        self.assertAlmostEqual(b['pairs'][pair][endpoint], m['pairs'][pair][endpoint], delta=tolerance)

    def test_numeric_and_structural_tamper_do_not_pass_comparison(self):
        raw = fabricated(perturb=True)
        oracle = compute(raw, 'common_ca')
        root = Path(__file__).resolve().parents[3]
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            structure = tmp / 'synth.cif'
            structure.write_bytes(raw)
            subprocess.run([sys.executable, '-B', root/'gx-h/plugins/installed/org.gx.block-influence/1.0.0/methods/block_influence.py',
                            '--structure', structure, '--mask', 'common_ca', '--fitter',
                            root/'gx-h/adapters/scientific/tl1a_geometry/worker.py', '--out', tmp], check=True)
            candidate = json.loads((tmp/'block-influence.json').read_bytes())
        self.assertEqual(compare(oracle, candidate)['status'], 'PASS')
        candidate['blocks'][0]['controls'][0]['positions'].reverse()
        candidate['blocks'][1]['pairs']['AC']['delta_rmsd_A'] += .003
        candidate['blocks'][2]['controls'].pop()
        del candidate['baseline']['AB']['angle_deg']
        result = compare(oracle, candidate)
        self.assertEqual(result['status'], 'FAIL')
        self.assertGreaterEqual(len(result['errors']), 4)


if __name__ == '__main__':
    unittest.main()
