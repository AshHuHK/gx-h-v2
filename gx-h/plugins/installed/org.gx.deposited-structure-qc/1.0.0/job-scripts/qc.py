"""Read a deposited single-chain polypeptide without inferring covalent bonds."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean

from Bio.PDB.MMCIF2Dict import MMCIF2Dict


def rows(cif, category, fields):
    columns = [cif[category + "." + name] for name in fields.split()]
    return zip(*columns, strict=True)


def polymer(cif):
    polymers = {entity for entity, kind in rows(cif, "_entity_poly", "entity_id type")
                if kind.startswith("polypeptide")}
    chains = [(chain, entity) for chain, entity in rows(cif, "_struct_asym", "id entity_id")
              if entity in polymers]
    if len(chains) != 1:
        raise ValueError("Requires one deposited polypeptide label chain")
    chain, entity = chains[0]
    sequence = {}
    for ent, pos, residue in rows(cif, "_entity_poly_seq", "entity_id num mon_id"):
        if ent == entity:
            if int(pos) in sequence:
                raise ValueError("Microheterogeneous sequence positions are outside this method")
            sequence[int(pos)] = residue
    if len(sequence) < 2:
        raise ValueError("A chain with at least two declared positions is required")
    return chain, entity, sequence


def coordinates(cif, chain, entity, sequence):
    models = defaultdict(dict)
    atom_fields = ("pdbx_PDB_model_num label_asym_id label_entity_id label_seq_id label_comp_id "
                   "label_atom_id type_symbol label_alt_id occupancy Cartn_x Cartn_y Cartn_z")
    for model, asym, ent, pos, comp, atom, element, alt, occ, x, y, z in rows(cif, "_atom_site", atom_fields):
        if asym != chain or ent != entity or element.upper() in {"H", "D"} or float(occ) <= 0:
            continue
        position = int(pos)
        if sequence.get(position) != comp:
            raise ValueError("Coordinate residue identity differs from the declared polymer")
        point = tuple(map(float, (x, y, z)))
        atoms = models[int(model)]
        if alt not in {".", "?"} or (position, atom) in atoms or not all(map(math.isfinite, point)):
            raise ValueError("Ambiguous conformer or invalid coordinate")
        atoms[position, atom] = point
    if 1 not in models:
        raise ValueError("No model 1 polymer coordinates")
    return models


def endpoint_link(cif, chain, sequence):
    first, last = min(sequence), max(sequence)
    expected = {(chain, str(first), sequence[first], "N"), (chain, str(last), sequence[last], "C")}
    if "_struct_conn.id" not in cif:
        return []
    fields = ["id", "conn_type_id"]
    for partner in ("ptnr1", "ptnr2"):
        fields.extend(partner + "_" + suffix for suffix in
                      ("label_asym_id", "label_seq_id", "label_comp_id", "label_atom_id", "symmetry"))
    matches = []
    for name, kind, c1, n1, r1, a1, s1, c2, n2, r2, a2, s2 in rows(cif, "_struct_conn", " ".join(fields)):
        if (kind == "covale" and s1 == s2 == "1_555"
                and {(c1, n1, r1, a1), (c2, n2, r2, a2)} == expected):
            matches.append(name)
    return matches


def angle(a, b, c):
    left = [x - y for x, y in zip(a, b)]
    right = [x - y for x, y in zip(c, b)]
    denom = math.hypot(*left) * math.hypot(*right)
    if denom == 0:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x*y for x, y in zip(left, right)) / denom))))


def ring_model(model, atoms, sequence):
    first, last = min(sequence), max(sequence)
    endpoints = [(last, "CA"), (last, "C"), (first, "N"), (first, "CA")]
    ca_last, carbon, nitrogen, ca_first = [atoms.get(key) for key in endpoints]
    # Keep terminal geometry observable even when the native bond assertion is absent.
    positions = sorted(sequence)
    neighbors = {tuple(sorted((p, q))) for p, q in zip(positions, positions[1:] + positions[:1])}
    pairs_tested = close = 0
    by_position = defaultdict(list)
    for (position, _), point in atoms.items():
        by_position[position].append(point)
    for i, p in enumerate(positions):
        for q in positions[i + 1:]:
            if (p, q) in neighbors:
                continue
            for a in by_position[p]:
                for b in by_position[q]:
                    pairs_tested += 1
                    close += math.dist(a, b) < 2
    return {"model": model, "mapped_endpoint_atoms": sum(key in atoms for key in endpoints),
            "endpoint_atom_denominator": 4,
            "distance_A": math.dist(carbon, nitrogen) if carbon and nitrogen else None,
            "ca_c_n_deg": angle(ca_last, carbon, nitrogen) if ca_last and carbon and nitrogen else None,
            "c_n_ca_deg": angle(carbon, nitrogen, ca_first) if carbon and nitrogen and ca_first else None,
            "heavy_pairs_lt_2_A": close, "nonneighbor_heavy_pair_denominator": pairs_tested}


def measure(path, mode):
    cif = MMCIF2Dict(str(path))
    chain, entity, sequence = polymer(cif)
    models = coordinates(cif, chain, entity, sequence)
    schema = "gx.deposited-" + mode + "-qc/1"
    document = {"schema": schema, "entry": cif["_entry.id"][0], "chain": chain, "entity": entity,
                "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "declared_residues": len(sequence), "model_count": len(models)}
    if mode == "backbone":
        atoms = models[1]
        observed = {p for p, _ in atoms}
        calpha = [point for (_, name), point in atoms.items() if name == "CA"]
        center = tuple(map(fmean, zip(*calpha))) if calpha else None
        document.update(model=1, observed_residues=len(observed), ca_count=len(calpha),
                        complete_backbones=sum(all((p, a) in atoms for a in ("N", "CA", "C", "O"))
                                               for p in sequence),
                        missing_label_positions=sorted(sequence.keys() - observed),
                        ca_radius_of_gyration_A=(math.sqrt(fmean(math.dist(c, center)**2 for c in calpha))
                                                if center else None))
    else:
        if len(sequence) > 64:
            raise ValueError("This short-peptide ring method supports up to 64 positions")
        links = endpoint_link(cif, chain, sequence)
        records = [ring_model(model, atoms, sequence) for model, atoms in sorted(models.items())]
        # A declaration is per deposition. Coordinates and mapping completeness are per model.
        first, last = min(sequence), max(sequence)
        kinds = dict(rows(cif, "_chem_comp", "id type"))
        document.update(declared_closure=int(bool(links)), closure_link_ids=links,
                        topology="declared_head_to_tail" if links else "missing_head_to_tail_declaration",
                        endpoint_mapping=[{"label_asym_id": chain, "label_seq_id": pos, "comp_id": sequence[pos],
                                          "atom_id": atom} for pos, atom in
                                          ((last, "CA"), (last, "C"), (first, "N"), (first, "CA"))],
                        residue_chemistry=[{"label_seq_id": p, "comp_id": r, "type": kinds[r]}
                                           for p, r in sorted(sequence.items())],
                        models=records, mapped_models=sum(row["mapped_endpoint_atoms"] == 4 for row in records),
                        model_1_distance_A=next(row["distance_A"] for row in records if row["model"] == 1))
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("backbone", "macrocycle"), required=True)
    parser.add_argument("--structure", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.structure, args.mode)
    args.out.mkdir(exist_ok=True, parents=True)
    (args.out/"qc.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
