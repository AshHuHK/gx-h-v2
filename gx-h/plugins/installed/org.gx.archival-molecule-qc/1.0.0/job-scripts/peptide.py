"""Measure archived short-polypeptide coordinate and chemistry coverage."""
import argparse
from collections import defaultdict
import hashlib
import json
from math import isfinite
from pathlib import Path

from Bio.PDB.MMCIF2Dict import MMCIF2Dict

BACKBONE = {"N", "CA", "C", "O"}
UNKNOWN = {".", "?"}
STANDARD_COMPONENTS = set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split())


def rows(cif, prefix, fields):
    return zip(*(cif[f"{prefix}.{field}"] for field in fields.split()), strict=True)


def chain_positions(cif):
    entities = {entity for entity, kind in rows(cif, "_entity_poly", "entity_id type")
                if kind.startswith("polypeptide(")}
    chains = [(chain, entity) for chain, entity in rows(cif, "_struct_asym", "id entity_id")
              if entity in entities]
    if len(chains) != 1:
        raise ValueError("Requires one polypeptide label chain")
    chain, entity = chains[0]
    positions = {}
    for ent, number, comp in rows(cif, "_entity_poly_seq", "entity_id num mon_id"):
        if ent != entity:
            continue
        pos = int(number)
        if pos in positions:
            raise ValueError("Microheterogeneous positions are unsupported")
        positions[pos] = comp
    if not 2 <= len(positions) <= 64:
        raise ValueError("Supports 2–64 declared polymer positions including terminal groups")
    return chain, entity, positions


def annotated_residues(cif, chain, positions):
    prefix = "_pdbx_struct_mod_residue"
    if f"{prefix}.label_asym_id" not in cif:
        return [], "category_absent"
    result = []
    for asym, num, comp, parent in rows(cif, prefix, "label_asym_id label_seq_id label_comp_id parent_comp_id"):
        if asym == chain:
            pos = int(num)
            if comp != positions.get(pos):
                raise ValueError("Modification annotation disagrees with the declared polymer")
            result.append({"label_seq_id": pos, "comp_id": comp,
                           "parent_comp_id": None if parent in UNKNOWN else parent})
    return result, "category_present"


def covalent_links(cif, chain, positions):
    """Index only same-copy, same-chain deposited covalent links to other positions."""
    links = defaultdict(list)
    if "_struct_conn.id" not in cif:
        return links, "category_absent"
    fields = "id conn_type_id ptnr1_label_asym_id ptnr1_label_seq_id ptnr1_label_comp_id ptnr1_symmetry "
    fields += "ptnr2_label_asym_id ptnr2_label_seq_id ptnr2_label_comp_id ptnr2_symmetry"
    for link, kind, a, n, c1, s1, b, m, c2, s2 in rows(cif, "_struct_conn", fields):
        if kind != "covale" or not (a == b == chain and s1 == s2 == "1_555"):
            continue
        if n in UNKNOWN or m in UNKNOWN or n == m:
            continue
        i, j = int(n), int(m)
        if c1 != positions.get(i) or c2 != positions.get(j):
            raise ValueError("Connection disagrees with declared label positions")
        for site, neighbor in ((i, j), (j, i)):
            links[site].append({"id": link, "partner_position": neighbor,
                                "partner_component": positions[neighbor]})
    return links, "category_present"


def model_coverage(cif, chain, entity, positions, amino):
    atoms = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    fields = ("pdbx_PDB_model_num label_asym_id label_entity_id label_seq_id label_comp_id "
              "label_atom_id type_symbol label_alt_id occupancy Cartn_x Cartn_y Cartn_z")
    for model, asym, ent, num, comp, name, element, alt, occ, x, y, z in rows(cif, "_atom_site", fields):
        # Include models in which the selected chain has no observed eligible atoms.
        model_atoms = atoms[int(model)]
        if asym != chain or ent != entity or element.upper() in {"H", "D"}:
            continue
        if occ in UNKNOWN or any(value in UNKNOWN for value in (x, y, z)):
            continue
        occupancy = float(occ)
        if not isfinite(occupancy) or occupancy <= 0 or not all(isfinite(float(v)) for v in (x, y, z)):
            continue
        pos = int(num)
        if comp != positions.get(pos):
            raise ValueError("Coordinate residue disagrees with the declared polymer")
        model_atoms[pos][alt].add(name)
    if 1 not in atoms:
        raise ValueError("Archive has no model 1")
    results = []
    for model, sites in sorted(atoms.items()):
        complete = 0
        for pos in amino.intersection(sites):
            conformers = sites[pos]
            shared = conformers.get(".", set())
            # Unknown alternate identity can show positional coverage but cannot complete a coherent backbone.
            complete += BACKBONE <= shared or any(
                BACKBONE <= shared | labels for alt, labels in conformers.items() if alt not in UNKNOWN)
        results.append({"model": model, "observed_positions": len(sites), "backbone_positions": complete,
                        "observed_peptide_residues": len(amino.intersection(sites)),
                        "missing_label_positions": sorted(positions.keys() - sites.keys())})
    return results


def summarize(cif):
    methods = cif["_exptl.method"]
    if not all(any(word in method.upper() for word in ("NMR", "DIFFRACTION", "MICROSCOPY"))
               for method in methods):
        raise ValueError("Requires an experimental NMR, diffraction or microscopy deposition")
    chain, entity, positions = chain_positions(cif)
    types = dict(rows(cif, "_chem_comp", "id type")) if "_chem_comp.id" in cif else {}
    amino, unclassified, other, nonstandard = set(), [], [], []
    links, connection_status = covalent_links(cif, chain, positions)
    for pos, comp in sorted(positions.items()):
        kind = types.get(comp)
        if kind is None or kind in UNKNOWN or kind.lower() == "peptide-like" or comp in {"UNK", "ASX", "GLX", ".", "?"}:
            unclassified.append(pos)
        elif "peptide" in kind.lower():
            amino.add(pos)
            if comp not in STANDARD_COMPONENTS:
                nonstandard.append({"label_seq_id": pos, "comp_id": comp, "chem_comp_type": kind})
        else:
            other.append({"label_seq_id": pos, "comp_id": comp, "chem_comp_type": kind,
                          "in_chain_covalent_links": links[pos]})
    modified, annotation_status = annotated_residues(cif, chain, positions)
    models = model_coverage(cif, chain, entity, positions, amino)
    model_one = next(record for record in models if record["model"] == 1)
    return {"entry": cif["_entry.id"][0], "experimental_methods": methods, "chain": chain, "entity": entity,
            "declared_positions": len(positions), "peptide_residue_positions": len(amino),
            "model": 1, "observed_positions": model_one["observed_positions"],
            "backbone_positions": model_one["backbone_positions"], "model_count": len(models), "models": models,
            "declared_modified_positions": len({site["label_seq_id"] for site in modified}),
            "annotation_status": annotation_status, "modified_residues": modified,
            "unknown_chemistry_positions": unclassified, "non_peptide_components": other,
            "nonstandard_peptide_components": nonstandard,
            "connection_status": connection_status, "predicted_confidence": None}


def measure(path):
    return {"schema": "gx.archival-peptide-coverage/1", "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            **summarize(MMCIF2Dict(path))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.structure)
    args.out.mkdir(exist_ok=True, parents=True)
    (args.out / "peptide-qc.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
