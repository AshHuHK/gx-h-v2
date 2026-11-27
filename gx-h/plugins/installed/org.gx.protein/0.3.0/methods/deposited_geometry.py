"""Measure matched backbone geometry of a released crystal-coordinate file."""
import argparse
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path

FIT_SOURCE = "f6a589ae44c53725d8066988cda86b1212aad19d26f89660a2e49a3e96f13003"
PROTOCOLS = ("common_ca", "common_backbone", "ordered_backbone")


def released_method(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != FIT_SOURCE:
        raise ValueError("Released coordinate fitting code differs from its pin")
    spec = importlib.util.spec_from_file_location("gx_deposited_fit", path)
    method = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(method)
    return method


def measure(raw, protocol, fit):
    source_sha = hashlib.sha256(raw).hexdigest()
    lines = raw.decode("utf-8").splitlines()
    entries = [line.split() for line in lines if line.startswith("_entry.id ")]
    blocks = [line for line in lines if line.lower().startswith("data_")]
    atom_tables = [line for line in lines if line.strip() == "_atom_site.group_PDB"]
    if (len(blocks) != 1 or len(entries) != 1 or len(entries[0]) != 2
            or entries[0][1] in {"?", "."} or len(atom_tables) != 1):
        raise ValueError("Expected a single mmCIF entry and one tabular atom_site loop")
    entry_id = entries[0][1].strip("'\"")
    atoms = {chain: {} for chain in fit.CHAINS}
    for row in fit._loop_rows(lines, "_atom_site."):
        name, chain = row["label_atom_id"], row["label_asym_id"]
        if (row["group_PDB"], row["label_entity_id"], row["pdbx_PDB_model_num"]) != ("ATOM", "1", "1"):
            continue
        if name not in fit.BACKBONE or chain not in atoms:
            continue
        occ = float(row["occupancy"])
        if not 0 <= occ <= 1:
            raise ValueError("Invalid occupancy")
        if occ == 0:
            continue
        key = int(row["label_seq_id"]), row["label_comp_id"], name
        if row["type_symbol"] != fit.BACKBONE[name] or row["label_alt_id"] not in (".", "?") or key in atoms[chain]:
            raise ValueError("Ambiguous atom in the deposited backbone")
        xyz = tuple(float(row["Cartn_" + axis]) for axis in "xyz")
        b = float(row["B_iso_or_equiv"])
        if not all(math.isfinite(item) for item in (*xyz, b)):
            raise ValueError("Coordinate or B factor is not finite")
        atoms[chain][key] = fit.Atom(xyz, occ, b)
    union = {key[0] for chain in atoms.values() for key in chain}
    if not union or any(not records for records in atoms.values()):
        raise ValueError("The method requires observed backbone positions in each selected chain")
    common = set.intersection(*(set(chain) for chain in atoms.values()))
    residues = {key[:2] for key in common}
    complete = {r for r in residues if all((*r, a) in common for a in fit.BACKBONE)}
    if protocol == "common_ca":
        keys = sorted(k for k in common if k[2] == "CA")
    elif protocol == "common_backbone":
        keys = sorted(k for k in common if k[:2] in complete)
    elif protocol == "ordered_backbone":
        ordered = {r for r in complete if all(atoms[c][(*r,a)].occupancy == 1 and
                    atoms[c][(*r,a)].b_factor <= 40 for a in fit.BACKBONE for c in atoms)}
        keys = sorted(k for k in common if k[:2] in ordered)
    else:
        raise ValueError("Unknown mask")
    pairs = []
    for fixed, moving in itertools.combinations(atoms, 2):
        result = {"fixed_chain": fixed, "moving_chain": moving}
        try:
            angle, rmsd = fit.fit([atoms[fixed][k].xyz for k in keys], [atoms[moving][k].xyz for k in keys])
            result.update(rotation_angle_deg=angle, fitted_rmsd_angstrom=rmsd)
        except ValueError as exc:
            result["unavailable"] = str(exc)
        pairs.append(result)
    n = len({k[0] for k in keys})
    return {"schema":"gx.deposited_chain_geometry/2", "method":"deposited-chain-geometry@2",
            "entry":entry_id, "input_sha256":source_sha, "mask":protocol,
            "model":"1", "label_entity":"1", "chains":list(atoms), "observed_union_residues":len(union),
            "selected_residues":n, "selected_atoms":len(keys), "coverage_fraction":n / len(union),
            "matched_keys_sha256":fit.object_digest(keys), "pairs":pairs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", type=Path, required=True)
    parser.add_argument("--fitter", type=Path, required=True)
    parser.add_argument("--mask", choices=PROTOCOLS, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.structure.read_bytes(), args.mask, released_method(args.fitter))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "geometry.json").write_text(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
