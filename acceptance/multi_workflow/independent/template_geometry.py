"""Compare deposited trimer coordinates under one common proper rigid transform per chain mapping."""

from decimal import Decimal
from hashlib import sha256
from io import StringIO
from itertools import permutations
from math import sqrt

from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np

from acceptance.v6.reviewer.independent_geometry import NAMES, selections
from acceptance.v623.study.independent_geometry import CHAINS, digest, rows


def _source(raw, entry):
    cif = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    if cif["_entry.id"] != [entry]:
        raise ValueError("Coordinate entry differs from requested ID")
    cif["_atom_site.label_alt_id"] = ["." if v == "?" else v for v in cif["_atom_site.label_alt_id"]]
    atoms, _, masks = selections(cif)
    exact = {chain: {} for chain in CHAINS}
    fields = ("group_PDB pdbx_PDB_model_num label_entity_id label_asym_id label_seq_id label_comp_id "
              "label_atom_id occupancy Cartn_x Cartn_y Cartn_z")
    for group, model, entity, chain, seq, comp, name, occ, x, y, z in rows(cif, "atom_site", fields):
        if (group, model, entity) != ("ATOM", "1", "1") or chain not in exact or name not in NAMES or float(occ) <= 0:
            continue
        key = int(seq), comp, name
        if key in atoms[chain]:
            point = tuple(Decimal(v) for v in (x, y, z))
            if not all(v.is_finite() for v in point):
                raise ValueError("Non-finite coordinate")
            exact[chain][key] = point
    return exact, masks


def _integers(values, scale):
    # Convert the exact printed rationals to a shared integer lattice, without Decimal context rounding.
    def convert(x):
        sign, digits, exponent = x.as_tuple()
        n = int("".join(map(str, digits))) * 10 ** (exponent - scale)
        return -n if sign else n
    return [tuple(convert(x) for x in point) for point in values]


def _subtract(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _anchors(centered):
    indices = []
    for i, v in enumerate(centered):
        if not indices:
            if any(v):
                indices.append(i)
        elif len(indices) == 1:
            if any(_cross(centered[indices[0]], v)):
                indices.append(i)
        elif _dot(_cross(centered[indices[0]], centered[indices[1]]), v):
            indices.append(i)
            break
    return indices


def _signature(point, axes):
    return (_dot(point, point), *(_dot(point, axis) for axis in axes))


def _is_congruent(target, anchor_ids, template_signatures, template_volume):
    # Equal norms and dot products against a basis determine every relative point.
    # In three dimensions matching the handedness also rules out a pure reflection.
    centered = [_subtract(point, target[0]) for point in target]
    axes = [centered[i] for i in anchor_ids]
    if len(axes) == 3 and _dot(_cross(axes[0], axes[1]), axes[2]) != template_volume:
        return False
    return all(sig == _signature(point, axes) for sig, point in zip(template_signatures, centered))


def _fit(template, target):
    mu, nu = template.mean(axis=0), target.mean(axis=0)
    x, y = template - mu, target - nu
    u, singular, vt = np.linalg.svd(x.T @ y)
    sign = -1 if np.linalg.det(u @ vt) < 0 else 1
    rotation = (u * [1, 1, sign]) @ vt
    residual = np.linalg.norm(x @ rotation - y, axis=1)
    return {
        "rmsd_A": float(np.sqrt(np.mean(residual**2))), "max_residual_A": float(residual.max()),
        "unique_rotation": bool(singular[0] and singular[1] + sign*singular[2] > singular[0]*1e-12),
        "rotation_row_vector": rotation.tolist(), "translation_A": (nu - mu @ rotation).tolist(),
    }


def _compare_mask(template, target, mask):
    a_xyz, a_masks = template
    b_xyz, b_masks = target
    a, b = set(a_masks[mask]), set(b_masks[mask])
    common = sorted(a & b)
    sets = {"template": sorted(a), "target": sorted(b), "intersection": common,
            "template_only": sorted(a-b), "target_only": sorted(b-a)}
    result = {"keys": {k: {"atoms_per_chain": len(v), "positions": len({x[0] for x in v}),
                           "sha256": digest(v)} for k, v in sets.items()},
              "identical_key_sets": a == b, "matched_points_all_chains": 3*len(common)}
    if not common:
        return {**result, "status": "no_shared_atom_keys", "exact_full_mask": None,
                "best_mapping": None, "permutations": []}

    first = [a_xyz[c][k] for c in CHAINS for k in common]
    second = {c: [b_xyz[c][k] for k in common] for c in CHAINS}
    exp_a = [v.as_tuple().exponent for point in first for v in point]
    exp_b = [v.as_tuple().exponent for points in second.values() for point in points for v in point]
    scale = min(min(exp_a), min(exp_b))
    prec_a = {"finest": 10.0**min(exp_a), "coarsest": 10.0**max(exp_a)}
    prec_b = {"finest": 10.0**min(exp_b), "coarsest": 10.0**max(exp_b)}
    rounding_bound = sqrt(3) * (prec_a["coarsest"] + prec_b["coarsest"]) / 2
    result["reported_coordinate_step_A"] = {"template": prec_a, "target": prec_b}
    result["two_input_rounding_bound_A"] = rounding_bound

    first_int = _integers(first, scale)
    centered = [_subtract(point, first_int[0]) for point in first_int]
    ids = _anchors(centered)
    axes = [centered[i] for i in ids]
    volume = _dot(_cross(axes[0], axes[1]), axes[2]) if len(ids) == 3 else None
    sigs = [_signature(point, axes) for point in centered]
    second_int = {c: _integers(points, scale) for c, points in second.items()}
    x = np.asarray(first, dtype=float)
    y = {c: np.asarray(points, dtype=float) for c, points in second.items()}
    fits = []
    for order in permutations(CHAINS):
        fit = _fit(x, np.concatenate([y[c] for c in order]))
        fit["target_chains_for_template_ABC"] = "".join(order)
        joined = [point for c in order for point in second_int[c]]
        fit["exact_shared_coordinate_congruence"] = _is_congruent(joined, ids, sigs, volume)
        fit["exact_full_mask"] = a == b and fit["exact_shared_coordinate_congruence"]
        fits.append(fit)
    best = min(fits, key=lambda f: f["rmsd_A"])
    identity = fits[0]
    span = max(1., float(np.max(np.abs(x))), *(float(np.max(np.abs(v))) for v in y.values()))
    tie_tol = 64*np.finfo(float).eps*span
    return {**result, "status": "measured", "affine_dimension_template": len(ids), "permutations": fits,
            "exact_full_mask": any(f["exact_full_mask"] for f in fits),
            "exact_full_mask_mappings": [f["target_chains_for_template_ABC"] for f in fits if f["exact_full_mask"]],
            "best_mapping": best["target_chains_for_template_ABC"], "best_rmsd_A": best["rmsd_A"],
            "best_max_residual_A": best["max_residual_A"],
            "identity_rmsd_A": identity["rmsd_A"], "identity_max_residual_A": identity["max_residual_A"],
            "identity_minus_best_rmsd_A": identity["rmsd_A"] - best["rmsd_A"],
            "rmsd_numerical_tie_tolerance_A": float(tie_tol),
            "tied_best_mappings": [f["target_chains_for_template_ABC"] for f in fits if f["rmsd_A"] - best["rmsd_A"] <= tie_tol],
            "best_rmsd_exceeds_rounding_bound": bool(best["rmsd_A"] > rounding_bound + tie_tol)}


def compare_template_geometry(template_raw, target_raw, template_id, target_id, masks):
    """Compare only the caller's shared published masks; no coordinate files or Jobs are opened."""
    masks = tuple(masks)
    if len(set(masks)) != len(masks) or set(masks) - {"common_ca", "common_backbone", "ordered_backbone"}:
        raise ValueError("Pass distinct published geometry mask names")
    template = _source(template_raw, template_id)
    target = _source(target_raw, target_id)
    return {
        "template": {"entry": template_id, "sha256": sha256(template_raw).hexdigest()},
        "target": {"entry": target_id, "sha256": sha256(target_raw).hexdigest()},
        "scope": "Model 1, entity 1, label chains A/B/C. Match local label_seq_id, residue and atom name. "
                 "For each permitted chain mapping fit one rotation (det +1) and translation to the pooled trimer.",
        "fit_convention": "Template row vectors multiplied by rotation, plus translation, map to target xyz in angstroms.",
        "precision": "Coordinate steps describe printed decimal places. The bound assumes rounding to nearest in "
                     "both input files. It is a conservative upper bound on achievable RMSD if a common "
                     "rigid transform and coordinate rounding alone explain the discrepancy. It is not experimental uncertainty.",
        "exact_criterion": "The two independently selected atom-key sets must match, and the deposited decimal coordinates "
                           "must be exactly congruent by a proper isometry under at least one chain permutation. "
                           "Exact congruence is tested with integer dot products and handedness; numerical tolerances only "
                           "identify RMSD ties. A fit to a partial intersection cannot establish full-mask equivalence. "
                           "This comparison evaluates selected coordinates. It does not evaluate individual Job "
                           "endpoints, their decision thresholds, or per-input control sampling.",
        "masks": {mask: _compare_mask(template, target, mask) for mask in masks},
    }
