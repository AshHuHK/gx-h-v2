"""Measure observed C-alpha coverage of an already numbered, deposited Fab."""
import argparse
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re

from Bio.PDB.MMCIF2Dict import MMCIF2Dict


CDRS = {'CDR1': (27, 38), 'CDR2': (56, 65), 'CDR3': (105, 117)}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rows(cif, table, *fields):
    return zip(*(cif[f'_{table}.{key}'] for key in fields), strict=True)


def chain_measurements(structure, numbering):
    cif = structure
    entry = cif['_entry.id'][0].upper()
    if (numbering.get('schema'), numbering.get('scheme'), numbering.get('entry')) != (
            'gx.archival-fab-numbering/1', 'IMGT', entry):
        raise ValueError('Supply the IMGT assignment for the specified deposited entry')
    chains = numbering['chains']
    if set(chains) != {'VH', 'VL'}:
        raise ValueError('The paired assignment requires both VH and VL')
    pair = {chain['label_asym_id'] for chain in chains.values()}
    if len(pair) != 2:
        raise ValueError('VH and VL must identify two different chains')
    polymer = {e: ''.join(seq.split()) for e, seq, kind in rows(
        cif, 'entity_poly', 'entity_id', 'pdbx_seq_one_letter_code_can', 'type') if kind == 'polypeptide(L)'}
    asym = dict(rows(cif, 'struct_asym', 'id', 'entity_id'))
    descriptions = dict(rows(cif, 'entity', 'id', 'pdbx_description'))
    groups = [(expr, set(ch.split(','))) for aid, expr, ch in rows(
        cif, 'pdbx_struct_assembly_gen', 'assembly_id', 'oper_expression', 'asym_id_list')
              if aid == numbering['assembly_id']]
    identity = set(i for i, typ in rows(cif, 'pdbx_struct_oper_list', 'id', 'type')
                   if typ.lower() == 'identity operation')
    if len(groups) != 1 or groups[0][0] not in identity:
        raise ValueError('Requires an explicitly identified, single identity-operation Fab assembly')
    if {a for a in groups[0][1] if asym.get(a) in polymer} != pair:
        raise ValueError('Selected assembly must contain exactly the assigned two polypeptide chains')

    observed = set()
    for model, ch, entity, idx, atom, symbol, occ, x, y, z in rows(
            cif, 'atom_site', 'pdbx_PDB_model_num', 'label_asym_id', 'label_entity_id', 'label_seq_id',
            'label_atom_id', 'type_symbol', 'occupancy', 'Cartn_x', 'Cartn_y', 'Cartn_z'):
        if model != '1' or ch not in pair or entity != asym[ch] or atom != 'CA' or symbol != 'C':
            continue
        if occ in {'.', '?'} or any(v in {'.', '?'} for v in (x, y, z)):
            continue
        if math.isfinite(float(occ)) and float(occ) > 0 and all(math.isfinite(float(v)) for v in (x, y, z)):
            observed.add((ch, int(idx)))

    output = {}
    for role, assignment in chains.items():
        label, entity = assignment['label_asym_id'], assignment['entity_id']
        expected_role = 'HEAVY CHAIN' if role == 'VH' else 'LIGHT CHAIN'
        types = {'H'} if role == 'VH' else {'K', 'L'}
        if (asym.get(label) != entity or expected_role not in descriptions[entity].upper() or
                assignment['chain_type'] not in types or assignment['polymer_sha256'] != sha(polymer[entity].encode())):
            raise ValueError('Chain, source polymer identity or assigned Fab role mismatch')
        start, end = assignment['first_label_seq_id'], assignment['last_label_seq_id']
        positions = assignment['positions']
        if not (1 <= start <= end <= len(polymer[entity])) or len(positions) != end - start + 1:
            raise ValueError('Numbered positions do not cover the declared polymer domain')
        seen = set()
        previous = 0
        counts = {name: {'matched_positions': 0, 'mapped_positions': 0} for name in CDRS}
        for label_index, (num, insertion) in enumerate(positions, start):
            if (type(num) is not int or not 1 <= num <= 128 or num < previous or
                    not re.fullmatch('[A-Z]{0,2}', insertion) or (num, insertion) in seen):
                raise ValueError('Invalid or duplicate IMGT position')
            seen.add((num, insertion))
            previous = num
            for name, (lo, hi) in CDRS.items():
                if lo <= num <= hi:
                    counts[name]['mapped_positions'] += 1
                    counts[name]['matched_positions'] += int((label, label_index) in observed)
        output[role] = {'label_asym_id': label, 'chain_type': assignment['chain_type'], 'cdr': counts}
    return output


def measure(structure_path, numbering_path):
    raw = Path(structure_path).read_bytes()
    record = gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw
    assignment_raw = Path(numbering_path).read_bytes()
    assignment = json.loads(assignment_raw)
    if sha(record) != assignment['source_mmcif_sha256']:
        raise ValueError('Numbering artifact references a different mmCIF payload')
    if not assignment.get('algorithm') or not assignment.get('version'):
        raise ValueError('Missing numbering method and version')
    data = MMCIF2Dict(io.StringIO(record.decode()))
    output = chain_measurements(data, assignment)
    regions = [region for chain in output.values() for region in chain['cdr'].values()]
    return {'entry': assignment['entry'], 'assembly_id': assignment['assembly_id'], 'scheme': 'IMGT',
            'numbering_method': assignment['algorithm'], 'numbering_version': assignment['version'],
            'input_sha256': sha(raw), 'numbering_sha256': sha(assignment_raw),
            'model': 1, 'chains': output, 'mapped_positions': sum(r['mapped_positions'] for r in regions),
            'matched_positions': sum(r['matched_positions'] for r in regions),
            'mapped_regions': sum(r['mapped_positions'] > 0 for r in regions),
            'measure': 'model-1 positive-occupancy C-alpha coverage of assigned polymer positions in six IMGT CDRs',
            'condition': 'One annotated isolated Fab assembly; inference is limited to deposited coordinate coverage.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--structure', required=True, type=Path)
    parser.add_argument('--numbering', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = measure(args.structure, args.numbering)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'fab_coverage.json').write_text(json.dumps(result, sort_keys=True) + '\n')
