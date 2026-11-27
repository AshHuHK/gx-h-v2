"""Count model-1 IMGT CDR coverage from archive PDB fixed-column ATOM records."""
import gzip
import hashlib
import json
import math
from pathlib import Path

from Bio.PDB.MMCIF2Dict import MMCIF2Dict


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count(code, root):
    coords = root / (code + '.pdb.gz')
    cif = root / (code + '.cif.gz')
    manifest = root / (code + '.imgt.json')
    author_coordinates = set()
    with gzip.open(coords, 'rt') as stream:
        for line in stream:
            if line.startswith('ENDMDL'):
                break
            if line[:6].strip() not in ('ATOM', 'HETATM') or line[12:16].strip() != 'CA':
                continue
            if line[76:78].strip() != 'C' or float(line[54:60]) <= 0:
                continue
            if all(math.isfinite(float(line[i:i+8])) for i in (30, 38, 46)):
                author_coordinates.add((line[21], line[22:26].strip(), line[26].strip()))
    numbering = json.loads(manifest.read_text())
    with gzip.open(cif, 'rt') as stream:
        fields = MMCIF2Dict(stream)
    # PDB author numbering can have gaps and insertion codes. This crosswalk
    # carries identities only; all measured coordinates come from the PDB.
    columns = ['asym_id', 'seq_id', 'pdb_strand_id', 'pdb_seq_num', 'pdb_ins_code']
    author_ids = {(label, int(idx)): (chain, num, '' if ins in ('.', '?') else ins)
                  for label, idx, chain, num, ins in zip(*(fields['_pdbx_poly_seq_scheme.' + col]
                                                          for col in columns), strict=True)}
    out = {}
    for role, call in numbering['chains'].items():
        region_counts = []
        mapping = dict(enumerate(call['positions'], call['first_label_seq_id']))
        for begin, end in [(27, 38), (56, 65), (105, 117)]:
            members = [idx for idx, (imgt, _ins) in mapping.items() if begin <= imgt <= end]
            observed = [idx for idx in members if author_ids[call['label_asym_id'], idx] in author_coordinates]
            region_counts.append({'matched': len(observed), 'mapped': len(members)})
        out[role] = region_counts
    return {'entry': code.upper(), 'pdb_gz_sha256': digest(coords), 'pdbx_gz_sha256': digest(cif),
            'numbering_sha256': digest(manifest), 'by_role_cdr1_2_3': out,
            'matched': sum(row['matched'] for chain in out.values() for row in chain),
            'mapped': sum(row['mapped'] for chain in out.values() for row in chain)}


if __name__ == '__main__':
    here = Path(__file__).resolve().parent
    print(json.dumps([count(code, here/'materials') for code in ('1mcp', '4fab')], indent=2))
