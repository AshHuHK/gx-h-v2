"""Rebuild the IMGT position maps from the two published Fab polymer sequences."""
import argparse
import gzip
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from anarci import anarci


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(path, hmmer_dir):
    raw = gzip.decompress(path.read_bytes())
    with gzip.open(path, 'rt') as stream:
        cif = MMCIF2Dict(stream)
    entry = cif['_entry.id'][0].upper()
    if entry not in ('1MCP', '4FAB'):
        raise ValueError('This material builder records the two public Fab calibrations.')
    desc = dict(zip(cif['_entity.id'], cif['_entity.pdbx_description']))
    asym = dict(zip(cif['_struct_asym.entity_id'], cif['_struct_asym.id']))
    poly = list(zip(cif['_entity_poly.entity_id'], cif['_entity_poly.pdbx_seq_one_letter_code_can']))
    sequences = [(entity, ''.join(sequence.split())) for entity, sequence in poly]
    numbered, assignments, _ = anarci(sequences, scheme='imgt', allow={'H', 'K', 'L'},
                                        hmmerpath=str(hmmer_dir.resolve()), ncpu=1)
    chains = {}
    for (entity, sequence), domains, calls in zip(sequences, numbered, assignments, strict=True):
        (domain, start, end), = domains
        call, = calls
        chain_type = call['chain_type']
        role = 'VH' if chain_type == 'H' else 'VL'
        expected = 'HEAVY CHAIN' if role == 'VH' else 'LIGHT CHAIN'
        if expected not in desc[entity].upper() or role in chains:
            raise ValueError('HMM assignment disagrees with deposited entity role')
        local_index = start
        positions = []
        for (position, insertion), residue in domain:
            if residue == '-':
                continue
            if sequence[local_index] != residue:
                raise ValueError('ANARCI alignment disagrees with polymer sequence')
            local_index += 1
            positions.append([position, insertion.strip()])
        if local_index != end + 1:
            raise ValueError('Assigned domain did not consume its reported range')
        chains[role] = {'label_asym_id': asym[entity], 'entity_id': entity, 'chain_type': chain_type,
                        'polymer_sha256': sha256(sequence.encode()), 'first_label_seq_id': start + 1,
                        'last_label_seq_id': end + 1, 'positions': positions}
    return {'schema': 'gx.archival-fab-numbering/1', 'entry': entry, 'scheme': 'IMGT', 'assembly_id': '1',
            'source_mmcif_sha256': sha256(raw), 'algorithm': 'ANARCI', 'version': version('anarci'),
            'chains': chains}


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--hmmer-dir', required=True, type=Path)
    cli.add_argument('materials', type=Path)
    args = cli.parse_args()
    for code in ('1mcp', '4fab'):
        doc = build(args.materials / (code + '.cif.gz'), args.hmmer_dir)
        (args.materials / (code + '.imgt.json')).write_text(json.dumps(doc, sort_keys=True, separators=(',', ':'))+'\n')
