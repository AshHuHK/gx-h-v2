"""Independently reproduce a TL1A coordinate result and compare declared atom selections."""
import argparse
import hashlib
import io
import itertools
import json
import math
import platform
from pathlib import Path


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _independent_input(raw):
    from Bio.PDB.MMCIF2Dict import MMCIF2Dict

    cif = MMCIF2Dict(io.StringIO(raw.decode("ascii")))
    def row(field):
        return cif["_atom_site." + field]
    atoms = {chain: {} for chain in ("A", "B", "C")}
    elements = {"N": "N", "CA": "C", "C": "C", "O": "O"}
    for i, chain in enumerate(row("label_asym_id")):
        if chain not in atoms or row("group_PDB")[i] != "ATOM":
            continue
        atom = row("label_atom_id")[i]
        if atom not in elements or float(row("occupancy")[i]) == 0:
            continue
        if (row("pdbx_PDB_model_num")[i] != "1" or row("label_entity_id")[i] != "1"
                or row("type_symbol")[i] != elements[atom] or row("label_alt_id")[i] not in (".", "?")):
            raise ValueError("reference found an ambiguous selected atom")
        key = (int(row("label_seq_id")[i]), row("label_comp_id")[i], atom)
        if key in atoms[chain]:
            raise ValueError(f"reference found a duplicate mapped identity {chain}/{key}")
        atoms[chain][key] = (tuple(float(row("Cartn_" + axis)[i]) for axis in "xyz"),
                             float(row("occupancy")[i]), float(row("B_iso_or_equiv")[i]))
    declared = {int(pos) for eid, pos in zip(cif["_entity_poly_seq.entity_id"], cif["_entity_poly_seq.num"])
                if eid == "1"}
    observed = {key[0] for chain in atoms.values() for key in chain}
    source = {"pdb_id": cif["_entry.id"][0], "model_id": "1", "label_entity_id": "1",
              "chains": list(atoms), "experimental_method": cif["_exptl.method"][0],
              "declared_polymer_residues": len(declared), "unobserved_polymer_positions": sorted(declared - observed),
              "resolution_angstrom": float(cif["_refine.ls_d_res_high"][0])}
    return atoms, source


def numerical_reference(raw, protocol, criteria):
    """Reparse raw CIF with Bio.PDB, select keys independently, and use LAPACK SVD."""
    import Bio
    import numpy as np
    from Bio.SVDSuperimposer import SVDSuperimposer

    if hashlib.sha256(raw).hexdigest() != criteria["source_sha256"]:
        raise ValueError("the reference source differs from the frozen criteria")
    atoms, source = _independent_input(raw)
    all_keys = set().union(*atoms.values())
    common = {k for k in all_keys if all(k in records for records in atoms.values())}
    if protocol == "common_ca":
        chosen = {k for k in common if k[-1] == "CA"}
    elif protocol == "ordered_backbone":
        chosen = set()
        for seq_id, residue in sorted({k[:2] for k in common}):
            keys = [(seq_id, residue, name) for name in ("N", "CA", "C", "O")]
            if all(k in records and records[k][1] == 1.0 and records[k][2] <= 40.0
                   for k in keys for records in atoms.values()):
                chosen.update(keys)
    elif protocol == "common_backbone":
        chosen = common
    else:
        raise ValueError(f"unsupported reference protocol {protocol!r}")
    keys = sorted(chosen)
    n_residues = len({k[0] for k in keys})
    denominator = len({k[0] for k in all_keys})
    fraction = n_residues / denominator
    mapping = {"observed_union_atoms": len(all_keys), "observed_union_residues": denominator,
               "common_backbone_atoms": len(common), "common_backbone_residues": len({k[0] for k in common}),
               "selected_atoms": len(keys), "selected_residues": n_residues,
               "selected_keys_sha256": _digest(keys), "selected_fraction_of_observed_union_residues": fraction}
    points = {chain: np.asarray([records[k][0] for k in keys]) for chain, records in atoms.items()}
    pairs = []
    for fixed, moving in itertools.combinations(atoms, 2):
        if len(keys) < 3:
            continue
        a, b = points[fixed], points[moving]
        covariance = (b - b.mean(axis=0)).T @ (a - a.mean(axis=0))
        sigma = np.linalg.svd(covariance, compute_uv=False)
        if (sigma[1] <= sigma[0] * 1e-12 or (np.linalg.det(covariance) < 0
                and sigma[1] - sigma[2] <= sigma[0] * 1e-12)):
            continue
        fit = SVDSuperimposer()
        fit.set(a, b)
        fit.run()
        rotation = fit.get_rotran()[0]
        angle = math.degrees(math.acos(float(np.clip((np.trace(rotation) - 1) / 2, -1, 1))))
        pairs.append({"fixed_chain": fixed, "moving_chain": moving, "rotation_angle_deg": angle,
                      "fitted_rmsd_angstrom": float(fit.get_rms()), "absolute_departure_from_120_deg": abs(angle - 120)})
    covered = (n_residues >= criteria["coverage"]["min_residues"]
               and fraction >= criteria["coverage"]["min_fraction"] and len(pairs) == 3)
    return {"protocol": protocol, "source": source, "mapping": mapping, "pairs": pairs,
            "status": "measured" if covered else "insufficient",
            "implementation": {"python": platform.python_version(), "biopython": Bio.__version__,
                "numpy": np.__version__, "parser": "Bio.PDB.MMCIF2Dict",
                "fit": "Bio.SVDSuperimposer.SVDSuperimposer (NumPy LAPACK SVD, proper rotation)",
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}


def _pairs(observation):
    return {(p["fixed_chain"], p["moving_chain"]): p for p in observation["pairs"]}


def independent_check(structure_bytes, observation, criteria):
    """Compare independently recovered atom identities, coverage and every reported pair."""
    protocol = observation["protocol"]
    reference = numerical_reference(structure_bytes, protocol, criteria)
    digest = hashlib.sha256(structure_bytes).hexdigest()
    check = {"schema": observation.get("schema") == "gx.tl1a.geometry/1",
             "method": observation.get("method") == "tl1a-geometry@1",
             "source_sha256": observation.get("input_sha256") == digest,
             "source_metadata": all(observation.get("source", {}).get(k) == v for k, v in reference["source"].items()),
             "criteria_spec_sha256": observation.get("criteria_spec_sha256") == _digest(criteria),
             "status": observation.get("status") == reference["status"],
             "mapping": observation.get("mapping") == reference["mapping"],
             "units": observation.get("units") == {"rotation_angle_deg": "degrees", "fitted_rmsd_angstrom": "angstrom"}}
    try:
        actual, expected = _pairs(observation), _pairs(reference)
    except (TypeError, KeyError):
        actual, expected = {}, _pairs(reference)
    check["pair_identities"] = set(actual) == set(expected) and len(observation.get("pairs", [])) == len(expected)
    differences = []
    fields = {"rotation_angle_deg": "angle_abs_tol_deg", "absolute_departure_from_120_deg": "angle_abs_tol_deg",
              "fitted_rmsd_angstrom": "rmsd_abs_tol_angstrom"}
    for pair, original in expected.items():
        delta = {}
        for field in fields:
            value = actual.get(pair, {}).get(field)
            delta[field] = abs(value - original[field]) if type(value) in (int, float) and math.isfinite(value) else None
        passes = all(delta[field] is not None and delta[field] <= criteria["reproduction"][tol]
                     for field, tol in fields.items())
        differences.append({"fixed_chain": pair[0], "moving_chain": pair[1], "absolute_difference": delta,
                            "within_reproduction_tolerance": passes})
    check["numerical_tolerances"] = all(d["within_reproduction_tolerance"] for d in differences)
    return {"status": "matches" if all(check.values()) else "disagrees", "protocol": protocol,
            "source_sha256": digest, "checks": check, "reference": reference, "differences": differences,
            "implementation": reference["implementation"]}


def compare_protocols(observations, criteria):
    """Determine sensitivity across requested selections while retaining incomplete and adverse comparisons."""
    protocols = [o["protocol"] for o in observations]
    expected = {("A", "B"), ("A", "C"), ("B", "C")}
    missing, valid = [], {}
    for obs in observations:
        try:
            pairs = _pairs(obs)
            conforming = (obs["schema"] == "gx.tl1a.geometry/1" and obs["method"] == "tl1a-geometry@1"
                and obs["input_sha256"] == criteria["source_sha256"] and obs["protocol"] in criteria["protocols"]
                and obs["status"] == "measured" and len(obs["pairs"]) == 3 and set(pairs) == expected
                and obs["mapping"]["selected_residues"] >= criteria["coverage"]["min_residues"]
                and obs["mapping"]["selected_fraction_of_observed_union_residues"] >= criteria["coverage"]["min_fraction"]
                and all(math.isfinite(p["rotation_angle_deg"]) and 0 <= p["rotation_angle_deg"] <= 180
                        and math.isfinite(p["fitted_rmsd_angstrom"]) and p["fitted_rmsd_angstrom"] >= 0
                        for p in pairs.values()))
        except (KeyError, TypeError):
            conforming = False
        if conforming and protocols.count(obs["protocol"]) == 1:
            valid[obs["protocol"]] = pairs
        else:
            missing.append({"protocol": obs["protocol"], "issues": obs.get("issues") or
                            ["missing, repeated or ineligible measurement"]})
    cutoffs = criteria["comparison"]
    comparisons = []
    for (left, a), (right, b) in itertools.combinations(valid.items(), 2):
        for pair in sorted(expected):
            da = abs(a[pair]["rotation_angle_deg"] - b[pair]["rotation_angle_deg"])
            dr = abs(a[pair]["fitted_rmsd_angstrom"] - b[pair]["fitted_rmsd_angstrom"])
            crossed = da > cutoffs["max_angle_delta_deg"] or dr > cutoffs["max_rmsd_delta_angstrom"]
            comparisons.append({"protocols": [left, right], "fixed_chain": pair[0], "moving_chain": pair[1],
                "angle_delta_deg": da, "rmsd_delta_angstrom": dr, "status": "sensitive" if crossed else "stable"})
    if missing or not comparisons:
        status, reason = "insufficient", "The requested comparison needs two distinct valid protocols and coverage for each."
    elif any(c["status"] == "sensitive" for c in comparisons):
        status, reason = "sensitive", "At least one pair exceeds an inter-protocol geometry bound."
    else:
        status, reason = "stable", "All measured pairs fall within both inter-protocol geometry bounds."
    return {"status": status, "reason": reason, "protocols": protocols, "comparisons": comparisons,
            "ineligible_protocols": missing, "criteria": cutoffs, "coverage": criteria["coverage"],
            "criteria_spec_sha256": _digest(criteria), "scope": criteria["scope"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", required=True, type=Path)
    parser.add_argument("--criteria", required=True, type=Path)
    parser.add_argument("--observation", type=Path, help="check this output; omit to reproduce all reference protocols")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raw, criteria = args.structure.read_bytes(), json.loads(args.criteria.read_bytes())
    if args.observation:
        result = independent_check(raw, json.loads(args.observation.read_bytes()), criteria)
    else:
        result = {"schema": "gx.tl1a.numeric_reference/1", "input_sha256": hashlib.sha256(raw).hexdigest(),
                  "criteria_sha256": hashlib.sha256(args.criteria.read_bytes()).hexdigest(),
                  "protocols": {name: numerical_reference(raw, name, criteria) for name in criteria["protocols"]}}
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
