"""Custodian's read-from-original mmCIF and proper SVD block-influence reference."""
import argparse
from hashlib import sha256
from io import StringIO
from itertools import combinations
import json
import math
from pathlib import Path
from statistics import median

import Bio
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np

CHAINS = ('A', 'B', 'C')
PAIRS = tuple(a + b for a, b in combinations(CHAINS, 2))
ATOMS = {'N': 'N', 'CA': 'C', 'C': 'C', 'O': 'O'}
MASKS = ('common_ca', 'common_backbone')
LIMITS = {'angle_deg': .5, 'rmsd_A': .15}
FIT_TOL = {'angle_deg': .01, 'rmsd_A': .001}


def digest(value):
    return sha256(json.dumps(value, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def rows(cif, table, fields):
    return zip(*(cif[f'_{table}.{key}'] for key in fields.split()), strict=True)


def select(raw, accession, expected_sha, mask):
    actual = sha256(raw).hexdigest()
    if actual != expected_sha or mask not in MASKS:
        raise ValueError('Wrong original file digest or unknown protocol mask')
    cif = MMCIF2Dict(StringIO(raw.decode('utf-8')))
    if [entry.upper() for entry in cif.get('_entry.id', [])] != [accession.upper()]:
        raise ValueError('Original entry ID differs from the registered accession')
    atoms = {c: {} for c in CHAINS}
    residues = {c: {} for c in CHAINS}
    fields = ('group_PDB pdbx_PDB_model_num label_entity_id label_asym_id label_seq_id label_comp_id '
              'label_atom_id type_symbol label_alt_id occupancy B_iso_or_equiv Cartn_x Cartn_y Cartn_z')
    for group, model, entity, chain, seq, res, atom, element, alt, occ, b, *xyz in rows(cif, 'atom_site', fields):
        if (group, model, entity) != ('ATOM', '1', '1') or chain not in atoms or atom not in ATOMS:
            continue
        occupancy = float(occ)
        if not 0 <= occupancy <= 1:
            raise ValueError('Invalid atom occupancy')
        if occupancy == 0:
            continue
        key = int(seq), res, atom
        point = tuple(map(float, xyz))
        if (res in ('.', '?') or residues[chain].setdefault(key[0], res) != res or key[0] < 1 or element != ATOMS[atom] or alt not in ('.', '?') or key in atoms[chain]
                or not all(math.isfinite(n) for n in (*point, float(b)))):
            raise ValueError('Ambiguous or nonfinite deposited backbone coordinates')
        atoms[chain][key] = point
    union = sorted({k[0] for chain in atoms.values() for k in chain})
    candidates = set.intersection(*(set(a) for a in atoms.values()))
    complete = {k[:2] for k in candidates if all((*k[:2], name) in candidates for name in ATOMS)}
    keys = sorted(k for k in candidates if (k[2] == 'CA' if mask == 'common_ca' else k[:2] in complete))
    # Multi-component identical label positions cannot each have the preregistered equal residue weight.
    positions = sorted({k[0] for k in keys})
    if len(keys) != len(positions) * (1 if mask == 'common_ca' else 4):
        raise ValueError('Ambiguous matched residue identity')
    declared = (sorted({int(seq) for ent, seq in rows(cif, 'entity_poly_seq', 'entity_id num') if ent == '1'})
                if '_entity_poly_seq.num' in cif else [])
    revisions = (list(rows(cif, 'pdbx_audit_revision_history', 'major_revision minor_revision revision_date'))
                 if '_pdbx_audit_revision_history.revision_date' in cif else [])
    meta = {'entry': accession.upper(), 'input_sha256': actual, 'mask': mask, 'model': '1', 'label_entity': '1',
            'chains': list(CHAINS), 'positions': positions, 'positions_sha256': digest(positions),
            'matched_keys_sha256': digest(keys), 'matched_atoms': len(keys), 'union_positions': union,
            'matched_residues': [[seq, residues['A'][seq]] for seq in positions],
            'declared_positions': declared, 'revisions': revisions,
            'observed_by_chain': {c: sorted({k[0] for k in atoms[c]}) for c in CHAINS}}
    xyz = {c: np.array([atoms[c][k] for k in keys], dtype=float).reshape(-1, 3) for c in CHAINS}
    return meta, keys, xyz


def proper_fit(fixed, moving):
    if len(fixed) < 3:
        return None
    left = moving - moving.mean(axis=0)
    right = fixed - fixed.mean(axis=0)
    u, singular, vt = np.linalg.svd(left.T @ right)
    correction = -1 if np.linalg.det(u @ vt) < 0 else 1
    # The second singular value must constrain a second direction. An orientation reversing
    # covariance with two equally small singular values also has multiple optimal proper fits.
    if singular[0] == 0 or singular[1] + correction * singular[2] <= 1e-12 * singular[0]:
        return None
    rotation = (u * np.array([1, 1, correction])) @ vt
    residual = left @ rotation - right
    angle = np.degrees(np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1)))
    return {'angle_deg': float(angle), 'rmsd_A': float(np.sqrt(np.sum(residual**2) / len(fixed)))}


def control_sets(positions, count, source_sha, mask, target):
    blocks = {(seq - 1) // 20 for seq in positions}
    if len(positions) < count or len(blocks) < 3 or count < 3:
        return
    seen = set()
    for k in range(1024):
        prefix = f'GX-BLOCK-QC-1|{source_sha}|{mask}|{target}|{k}|'
        ranked = sorted(positions, key=lambda n: (sha256((prefix + str(n)).encode('ascii')).digest(), n))
        selected = tuple(sorted(ranked[:count]))
        if selected in seen or len({(n - 1) // 20 for n in selected}) < 3:
            continue
        seen.add(selected)
        yield k, list(selected)
        if len(seen) == 20:
            break


def reference(raw, accession, expected_sha, mask):
    meta, keys, coordinates = select(raw, accession, expected_sha, mask)
    key_positions = np.array([k[0] for k in keys], dtype=int)
    positions = meta['positions']
    n, union = len(positions), meta['union_positions']

    def refits(removed):
        keep = ~np.isin(key_positions, removed)
        return {p: proper_fit(coordinates[p[0]][keep], coordinates[p[1]][keep]) for p in PAIRS}

    base = refits([])
    upper = max([0, *union, *meta['declared_positions']])
    blocks = []
    for ix in range((upper + 19) // 20):
        removed = [seq for seq in positions if (seq - 1) // 20 == ix]
        left = n - len(removed)
        exclusions = []
        if n < 60:
            exclusions.append('fewer_than_60_before')
        if not union:
            exclusions.append('empty_observed_union')
        elif n * 5 < 3 * len(union):
            exclusions.append('coverage_below_60_percent')
        if len(removed) < 10:
            exclusions.append('fewer_than_10_in_block')
        if left < 60:
            exclusions.append('fewer_than_60_after')
        if left * 5 < n * 3:
            exclusions.append('retained_below_60_percent')
        blocks.append({'index': ix, 'positions': removed, 'eligible': not exclusions, 'retained_count': left,
                       'reasons': exclusions, 'interval': [20 * ix + 1, 20 * ix + 20],
                       'positions_sha256': digest(removed), 'pairs': {}, 'median': {}, 'controls': []})
    for block in blocks:
        if not block['eligible']:
            continue
        removed = block['positions']
        dropped = set(removed)
        block['retained_keys_sha256'] = digest([k for k in keys if k[0] not in dropped])
        f = refits(removed)
        def measure(fits):
            out = {}
            for pair in PAIRS:
                row = dict(fits[pair] or dict.fromkeys(LIMITS))
                for endpoint in LIMITS:
                    row['delta_' + endpoint] = (abs(row[endpoint] - base[pair][endpoint])
                                                if fits[pair] and base[pair] else None)
                out[pair] = row
            return out
        block['pairs'] = measure(f)
        pool = [seq for seq in positions if (seq - 1) // 20 != block['index']]
        for k, selected in control_sets(pool, len(removed), expected_sha, mask, block['index']):
            fits = measure(refits(selected))
            block['controls'].append({'k': k, 'positions': selected, 'positions_sha256': digest(selected),
                                      'pairs': fits})
        for ep, limit in LIMITS.items():
            delta = 'delta_' + ep
            deltas = {p: block['pairs'][p][delta] for p in PAIRS}
            gap = (abs(median(f[p][ep] for p in PAIRS) - median(base[p][ep] for p in PAIRS))
                   if all(v is not None for v in deltas.values()) else None)
            block['median'][ep] = {'delta': gap,
                                   'before': median(base[p][ep] for p in PAIRS) if gap is not None else None,
                                   'after': median(f[p][ep] for p in PAIRS) if gap is not None else None,
                                   'exceptions': [p for p in PAIRS if gap is not None and deltas[p] > limit],
                                   'concealed': None if gap is None else
                                   gap <= limit and any(v > limit for v in deltas.values())}
            for pair, value in deltas.items():
                controls = [c['pairs'][pair][delta] for c in block['controls']]
                enough = len(controls) == 20 and None not in controls
                beyond = value > max(controls) if enough and value is not None else None
                block['pairs'][pair][ep + '_interpretation'] = {
                    'control_max': max(controls) if enough else None,
                    'threshold_margin': None if value is None else value - limit,
                    'boundary': None if value is None else abs(value - limit) <= 2 * FIT_TOL[ep],
                    'over_tolerance': None if value is None else value > limit,
                    'above_all_20_and_over_tolerance': None if beyond is None else beyond and value > limit,
                }
    return {**meta, 'parser': 'Bio.PDB.MMCIF2Dict ' + Bio.__version__, 'solver': 'NumPy SVD ' + np.__version__,
            'fit_tolerances': FIT_TOL, 'thresholds': LIMITS,
            'baseline': {pair: values or dict.fromkeys(LIMITS) for pair, values in base.items()}, 'blocks': blocks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--structure', type=Path, required=True)
    parser.add_argument('--input-sha256', required=True)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--mask', required=True, choices=MASKS)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = reference(args.structure.read_bytes(), args.entry, args.input_sha256, args.mask)
    with args.output.open('x') as out:
        json.dump(report, out, allow_nan=False, indent=2)
        out.write('\n')


if __name__ == '__main__':
    main()
