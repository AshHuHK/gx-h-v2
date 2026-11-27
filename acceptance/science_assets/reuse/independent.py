"""Independent fixed-column wwPDB PDB readback and equal-mass pairwise radius check."""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import argparse
import json
import math


def measure(pdb_path, chain):
    residues = defaultdict(dict)
    declared_count = None
    model = 1
    for line in pdb_path.read_text().splitlines():
        if line.startswith('SEQRES') and line[11] == chain:
            declared_count = int(line[13:17])
        if line.startswith('MODEL '):
            model = int(line[10:14])
        if model != 1 or not line.startswith('ATOM  ') or line[21] != chain or float(line[54:60]) <= 0:
            continue
        if line[16] != ' ':
            raise ValueError('PDB alternate locations are outside this independent comparison')
        atom = line[12:16].strip()
        position = (line[22:26], line[26])
        if atom in residues[position]:
            raise ValueError('PDB atom identity is ambiguous')
        residues[position][atom] = tuple(float(line[i:i+8]) for i in (30,38,46))
    if not declared_count:
        raise ValueError('No declared sequence for this chain')
    ca = [atoms['CA'] for atoms in residues.values() if 'CA' in atoms]
    pair_sum = math.fsum(math.dist(a,b)**2 for i,a in enumerate(ca) for b in ca[i+1:])
    return {'pdb_sha256': sha256(pdb_path.read_bytes()).hexdigest(), 'denominator': declared_count,
            'observed': len(residues), 'c_alpha': len(ca),
            'complete_n_ca_c_o': sum(set(('N','CA','C','O')) <= atoms.keys() for atoms in residues.values()),
            'radius_of_gyration_A': math.sqrt(pair_sum / len(ca)**2)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdb', type=Path)
    ap.add_argument('--chain', default='A')
    args = ap.parse_args()
    print(json.dumps(measure(args.pdb, args.chain), indent=2))
