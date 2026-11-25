"""Measure matched backbone geometry of a released crystal-coordinate file."""
import argparse
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path

DEPOSITIONS = {
    "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b": "2RE9",
    "7e9381a6184ae8bc7f9c9423248c25133e135f0857814834cdd88ea8e9eb1103": "2O0O",
}
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
    if source_sha not in DEPOSITIONS:
        raise ValueError("Supply a supported deposited coordinate revision")
    atoms = {chain: {} for chain in fit.CHAINS}
    for row in fit._loop_rows(raw.decode("ascii").splitlines(), "_atom_site."):
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
    return {"schema":"gx.deposited_chain_geometry/1", "method":"deposited-chain-geometry@1",
            "entry":DEPOSITIONS[source_sha], "input_sha256":source_sha, "mask":protocol,
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
