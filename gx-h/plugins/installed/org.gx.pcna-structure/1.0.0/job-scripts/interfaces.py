"""Observed PCNA–PCNA residue contacts and pairwise solvent-accessible surface burial."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

from Bio.PDB import NeighborSearch, ShrakeRupley
from Bio.PDB.Model import Model

CUTOFF_A = 4.0
PROBE_A = 1.4
SPHERE_POINTS = 100
WINDOW = 20
PAIRS = {"12": (0, 1), "23": (1, 2), "31": (2, 0)}


def residue_areas(chain):
    return {residue.id[1]: sum(atom.sasa for atom in residue) for residue in chain}


def contacting_residues(left, right):
    tree = NeighborSearch(list(right.get_atoms()))
    return {(atom.parent.id[1], other.parent.id[1])
            for atom in left.get_atoms() for other in tree.search(atom.coord, CUTOFF_A)}


def interval_summary(positions, burial, contacts, block):
    if block == -1:
        covered = positions
        local_contacts = contacts
        bounds = None
    else:
        lo, hi = WINDOW * block + 1, WINDOW * (block + 1)
        covered = [{i for i in side if lo <= i <= hi} for side in positions]
        local_contacts = [(i, j) for i, j in contacts if i in covered[0] or j in covered[1]]
        bounds = [lo, hi]
    measurable = all(covered)
    sides = [sum(table[i] for i in selected) for table, selected in zip(burial, covered)]
    return {"block_index": block, "label_interval": bounds,
            "observed_positions": [len(side) for side in covered],
            "n_contact_residue_pairs": len(local_contacts) if measurable else None,
            "buried_area_A2": sum(sides) if measurable else None,
            "buried_area_by_chain_A2": sides if measurable else [None, None],
            "unavailable_reason": None if measurable else "one_or_both_chains_lack_observed_window_positions"}


def measure(selection, pair="12", block_index=-1):
    if pair not in PAIRS or type(block_index) is not int or block_index < -1:
        raise ValueError("Choose pair 12, 23 or 31 and a block_index of -1 or greater")
    chains = [selection.molecules[name] for name in selection.chains]
    last_position = max([*selection.declared_positions, *(r.id[1] for c in chains for r in c)])
    n_blocks = (last_position + WINDOW - 1) // WINDOW
    if block_index >= n_blocks:
        raise ValueError(f"Requested interval is outside the deposited sequence ({n_blocks} windows)")
    sr = ShrakeRupley(probe_radius=PROBE_A, n_points=SPHERE_POINTS)
    isolated = []
    for chain in chains:
        sr.compute(chain)
        isolated.append(residue_areas(chain))
    tables = {}
    for key, (i, j) in PAIRS.items():
        complex_model = Model(0)
        for n in (i, j):
            complex_model.add(chains[n].copy())
        sr.compute(complex_model)
        burial = []
        for n in (i, j):
            in_pair = residue_areas(complex_model[chains[n].id])
            burial.append({pos: area - in_pair[pos] for pos, area in isolated[n].items()})
        positions = [set(isolated[i]), set(isolated[j])]
        contacts = sorted(contacting_residues(chains[i], chains[j]))
        tables[key] = {
            "chains": [chains[i].id, chains[j].id], "contacting_label_seq_id_pairs": contacts,
            "whole": interval_summary(positions, burial, contacts, -1),
            "windows": [interval_summary(positions, burial, contacts, block) for block in range(n_blocks)]
        }
    table = tables[pair]
    chosen = table["whole"] if block_index == -1 else table["windows"][block_index]
    identity = {"schema": "gx.pcna-interfaces/1", "method": "pcna-interface@1", "entry": selection.entry,
                "model": selection.model, "input_sha256": selection.input_sha256, "label_entity": selection.entity,
                "label_chains": list(selection.chains), "pair": pair, "chain_left": table["chains"][0],
                "chain_right": table["chains"][1], "contact_cutoff_A": CUTOFF_A, "sasa_probe_A": PROBE_A,
                "sasa_points": SPHERE_POINTS, "window_width": WINDOW}
    return {**identity, **chosen}, {**identity, "schema": "gx.pcna-interface-table/1", "pairs": tables}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--pair", choices=PAIRS, required=True)
    parser.add_argument("--block-index", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("pcna_selection", args.selection)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    summary, table = measure(module.select(args.structure.read_bytes()), args.pair, args.block_index)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in (("interface.json", summary), ("interface-table.json", table)):
        (args.out / name).write_text(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
