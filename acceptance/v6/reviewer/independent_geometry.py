"""Recompute the three fitted geometry protocols directly from the observed project's CIF."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import sys

import Bio
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np

COORD_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
CHAINS = "ABC"
NAMES = {"N", "CA", "C", "O"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verified(path, expected):
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == expected, f"content digest differs for {path.name}")
    return data


def bytes_path(state, row):
    path = state / "artifacts" / row["namespace"] / row["rel_path"]
    verified(path, row["sha256"])
    return path


def selections(cif):
    atoms = {c: {} for c in CHAINS}
    fields = ("group_PDB", "pdbx_PDB_model_num", "label_entity_id", "label_asym_id", "label_seq_id",
              "label_comp_id", "label_atom_id", "label_alt_id", "occupancy", "B_iso_or_equiv",
              "Cartn_x", "Cartn_y", "Cartn_z")
    for group, model, entity, chain, res, comp, atom, alt, occ, bfactor, x, y, z in zip(
            *(cif["_atom_site." + field] for field in fields), strict=True):
        if (group, model, entity) != ("ATOM", "1", "1") or chain not in CHAINS or atom not in NAMES:
            continue
        if float(occ) <= 0:
            continue
        key = (int(res), comp, atom)
        require(alt == "." and key not in atoms[chain], "unexpected alternate occupancy on pinned source")
        atoms[chain][key] = (list(map(float, (x, y, z))), float(occ), float(bfactor))
    union = {k[0] for rows in atoms.values() for k in rows}
    common = set.intersection(*(set(rows) for rows in atoms.values()))
    residues = {k[:2] for k in common}
    full = {r for r in residues if all((*r, name) in common for name in NAMES)}
    bb = sorted(k for k in common if k[:2] in full)
    ordered = {r for r in full if all(atoms[c][(*r, a)][1] == 1.0 and atoms[c][(*r, a)][2] <= 40
                                      for c in CHAINS for a in NAMES)}
    return atoms, union, {"common_backbone": bb, "common_ca": sorted(k for k in common if k[2] == "CA"),
                          "ordered_backbone": [k for k in bb if k[:2] in ordered]}


def fit(x, y):
    left, right = x - x.mean(axis=0), y - y.mean(axis=0)
    u, _, vt = np.linalg.svd(left.T @ right)
    d = np.diag([1.0, 1.0, np.sign(np.linalg.det(u @ vt))])
    rotation = u @ d @ vt
    diff = left @ rotation - right
    rmsd = np.sqrt(np.sum(diff * diff, axis=1).mean())
    angle = np.degrees(np.arccos(np.clip((rotation.trace() - 1) / 2, -1, 1)))
    return float(rmsd), float(angle)


def from_project(state):
    con = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row, = con.execute("SELECT * FROM artifacts WHERE sha256=? AND kind='run_input'", (COORD_SHA,))
    coord = bytes_path(state, row)
    artifacts = list(con.execute("SELECT * FROM artifacts WHERE kind='tl1a_geometry' ORDER BY job_id"))
    return coord, row["run_id"], row["artifact_ref"], [(a, json.loads(bytes_path(state, a).read_bytes())) for a in artifacts]


def from_bundle(bundle, anchor):
    require(bundle not in anchor.parents, "pass the separately held digest")
    metadata = verified(bundle / "bundle.json", json.loads(anchor.read_bytes())["bundle_sha256"])
    run, = json.loads(metadata)["runs"]
    packet_ref = run["packet"]
    packet = json.loads(verified(bundle / packet_ref["file"], packet_ref["sha256"]))
    structure = bundle / packet["coordinates"]["file"]
    verified(structure, COORD_SHA)
    geometry = []
    for obs in packet["observations"]:
        value = obs["value"]
        if value.get("method") == "tl1a-geometry@1":
            source = verified(bundle / "objects" / obs["result_sha256"], obs["result_sha256"])
            require(json.loads(source) == value, "observation differs from CPU output")
            a = {"artifact_ref": obs["id"], "job_id": obs["source_job_id"], "sha256": obs["result_sha256"]}
            geometry.append((a, value))
    turn = run["turn"]
    brief_ref = json.loads(verified(bundle / turn["file"], turn["sha256"]))["native_user_brief"]
    brief = verified(bundle / brief_ref["file"], brief_ref["sha256"]).decode()
    materials = json.loads(brief.split("\n", 1)[1])["materials"]
    structure_ref, = [a["artifact_ref"] for a in materials if a["sha256"] == COORD_SHA]
    return structure, run["run_id"], structure_ref, geometry


def compute(coord, run_id, source_ref, geometry):
    cif = MMCIF2Dict(str(coord))
    atoms, union, masks = selections(cif)
    declared = {int(p) for p, e in zip(cif["_entity_poly_seq.num"], cif["_entity_poly_seq.entity_id"]) if e == "1"}
    result = {"scope": "Single 2RE9 deposition, asymmetric-unit chains A/B/C, entity and model 1",
              "run_id": run_id, "source_ref": source_ref, "source_sha256": COORD_SHA,
              "parser": "Bio.PDB.MMCIF2Dict " + Bio.__version__, "method": "Kabsch proper rotation, NumPy SVD " + np.__version__,
              "reference_implementation": "live CPU Job using Horn quaternions and Jacobi eigensolver",
              "observed_union": len(union), "declared": len(declared), "unobserved_positions": sorted(declared - union),
              "reproduction_tolerances": {"angstrom": .001, "degrees": .01},
              "eligibility": {"minimum_positions": 60, "minimum_union_fraction": .60},
              "across_eligible_methods": {"angstrom": .15, "degrees": .5}, "protocols": {}}
    for artifact, observed in geometry:
        require(observed["input_sha256"] == COORD_SHA, "CPU Job used another coordinate source")
        protocol = observed["protocol"]
        keys = masks[protocol]
        n = len({k[0] for k in keys})
        mapping = observed["mapping"]
        counts = (mapping["selected_atoms"], mapping["selected_residues"], mapping["observed_union_residues"])
        require((len(keys), n, len(union)) == counts, "independent mapping counts differ")
        key_hash = hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()
        require(key_hash == mapping["selected_keys_sha256"], "atom mapping digest differs")
        out = {"job_id": artifact["job_id"], "metric_ref": artifact["artifact_ref"], "result_sha256": artifact["sha256"],
               "key_sha256": key_hash, "atoms": len(keys), "positions": n, "coverage": n / len(union),
               "eligible": n >= 60 and n / len(union) >= .60, "pairs": {}}
        for a, b in itertools.combinations(CHAINS, 2):
            x, y = (np.array([atoms[c][k][0] for k in keys]) for c in (a, b))
            rmsd, angle = fit(x, y)
            pair, = [p for p in observed["pairs"] if (p["fixed_chain"], p["moving_chain"]) == (a, b)]
            errors = [abs(rmsd - pair["fitted_rmsd_angstrom"]), abs(angle - pair["rotation_angle_deg"])]
            require(errors[0] <= .001 and errors[1] <= .01, f"{protocol} {a}/{b} exceeds reproduction tolerance")
            out["pairs"][a + "/" + b] = {"rmsd_angstrom": rmsd, "angle_degrees": angle,
                                         "absolute_error_angstrom": errors[0], "absolute_error_degrees": errors[1]}
        result["protocols"][protocol] = out
    require(len(geometry) == len(masks) and set(result["protocols"]) == set(masks), "incomplete protocols")
    scores = [v for m in result["protocols"].values() for v in m["pairs"].values()]
    result["maximum_disagreement"] = {u: max(v["absolute_error_" + u] for v in scores) for u in ("angstrom", "degrees")}
    full, ca = (result["protocols"][m]["pairs"] for m in ("common_backbone", "common_ca"))
    result["eligible_mask_deltas"] = {pair: {k: abs(full[pair][k] - ca[pair][k]) for k in ("rmsd_angstrom", "angle_degrees")}
                                       for pair in full}
    return result


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    inputs = cli.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--state", type=Path)
    inputs.add_argument("--bundle", type=Path)
    cli.add_argument("--anchor", type=Path)
    opts = cli.parse_args()
    if opts.bundle and not opts.anchor:
        cli.error("--bundle requires --anchor")
    data = from_project(opts.state.resolve()) if opts.state else from_bundle(opts.bundle.resolve(), opts.anchor.resolve())
    result = compute(*data)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, allow_nan=False)
    print()
