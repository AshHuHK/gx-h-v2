"""Equal-weight C-alpha RMSD on explicitly mapped polymer positions in two coordinate files."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from Bio.PDB.MMCIF2Dict import MMCIF2Dict


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positions(path, chain, wanted):
    cif = MMCIF2Dict(str(path))
    fields = ("pdbx_PDB_model_num label_asym_id label_seq_id label_atom_id type_symbol "
              "label_alt_id occupancy Cartn_x Cartn_y Cartn_z").split()
    points = {}
    for model, asym, seq, atom, element, alt, occupancy, x, y, z in zip(
            *(cif["_atom_site." + f] for f in fields), strict=True):
        if model != "1" or asym != chain or atom != "CA" or element != "C" or seq in (".", "?"):
            continue
        i = int(seq)
        if i not in wanted or float(occupancy) <= 0:
            continue
        if alt not in (".", "?") or i in points:
            raise ValueError(f"Ambiguous C-alpha at {chain}:{i}")
        xyz = np.asarray([float(x), float(y), float(z)])
        if not np.isfinite(xyz).all():
            raise ValueError(f"Nonfinite coordinate at {chain}:{i}")
        points[i] = xyz
    return points


def score(prediction, reference, mapping):
    data = json.loads(mapping.read_bytes())
    if data["coordinate_system"] != "mmcif_label_seq_id_model_1":
        raise ValueError("Use mmCIF label polymer positions in coordinate model 1.")
    pairs = data["pairs"]
    if not isinstance(pairs, list) or not pairs or not all(
            isinstance(p, list) and len(p) == 2 and all(type(x) is int and x > 0 for x in p) for p in pairs):
        raise ValueError("Supply a nonempty list of positive [prediction, reference] position pairs.")
    a, b = map(set, zip(*pairs, strict=True))
    if len(a) != len(pairs) or len(b) != len(pairs):
        raise ValueError("Residue mapping must be one-to-one.")
    pred = positions(prediction, data["prediction_chain"], a)
    ref = positions(reference, data["reference_chain"], b)
    matches = [(i, j) for i, j in pairs if i in pred and j in ref]
    missing = [{"pair": [i,j], "prediction_missing": i not in pred, "reference_missing": j not in ref}
               for i,j in pairs if i not in pred or j not in ref]
    if len(matches) < 3:
        raise ValueError(f"Only {len(matches)}/{len(pairs)} coordinate pairs are available; require three.")
    x = np.stack([pred[i] for i, _ in matches])
    y = np.stack([ref[j] for _, j in matches])
    x -= x.mean(axis=0)
    y -= y.mean(axis=0)
    if min(np.linalg.matrix_rank(x), np.linalg.matrix_rank(y)) < 2:
        raise ValueError("Collinear coordinates cannot specify a unique superposition.")
    u, _, vt = np.linalg.svd(x.T @ y)
    rotation = u @ np.diag([1., 1., np.linalg.det(u @ vt)]) @ vt
    rmsd = float(np.sqrt(((x @ rotation - y) ** 2).sum() / len(matches)))
    return {"input_sha256": digest(prediction), "reference_sha256": digest(reference),
            "mapping_sha256": digest(mapping), "model": "1", "prediction_chain": data["prediction_chain"],
            "reference_chain": data["reference_chain"], "mapped_positions": len(pairs),
            "matched_positions": len(matches), "missing_pairs": missing,
            "matched_pairs": matches, "calpha_rmsd_A": rmsd, "units": "angstrom"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prediction", "reference", "mapping", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = score(args.prediction, args.reference, args.mapping)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "agreement.json").write_text(json.dumps(report, indent=2) + "\n")
