"""Recompute completed coordinate Job products using the release's independent SVD readers."""

from hashlib import sha256
from io import StringIO
import math

import Bio
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np

from acceptance.v6.reviewer.independent_geometry import selections
from acceptance.v623.study import independent_geometry as reference
from acceptance.v623.study.independent_compare import compare


def verify_geometry(raw: bytes, product: dict) -> dict:
    """Check the submitted mask, mapping counts and all three whole-chain fits."""
    cif = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    entry, = cif["_entry.id"]
    # The v6 reviewer was written for a source spelling an absent alt ID as '.'.
    # Version 2 accepts either CIF missing-value token for the same selection.
    cif["_atom_site.label_alt_id"] = ["." if x == "?" else x for x in cif["_atom_site.label_alt_id"]]
    atoms, union, masks = selections(cif)
    mask = product.get("mask")
    if mask not in masks or not union or any(not atoms[c] for c in reference.CHAINS):
        raise ValueError("Unknown geometry mask or missing chain coordinates")
    keys = masks[mask]
    selected = len({k[0] for k in keys})
    mapping = {
        "schema": "gx.deposited_chain_geometry/2", "method": "deposited-chain-geometry@2",
        "entry": entry, "input_sha256": sha256(raw).hexdigest(), "mask": mask,
        "model": "1", "label_entity": "1", "chains": list(reference.CHAINS),
        "observed_union_residues": len(union), "selected_residues": selected,
        "selected_atoms": len(keys), "matched_keys_sha256": reference.digest(keys),
        "coverage_fraction": selected / len(union),
    }
    if set(product) != set(mapping) | {"pairs"}:
        raise ValueError("Geometry product fields differ from the version 2 schema")
    for name, expected in mapping.items():
        value = product[name]
        if (type(expected) is int and type(value) is not int) or type(value) is bool or value != expected:
            raise ValueError(f"Geometry source or selection mismatch in {name}")

    xyz = {c: np.array([atoms[c][k][0] for k in keys], dtype=float).reshape(-1, 3) for c in reference.CHAINS}
    pairs = {}
    for row in product["pairs"]:
        a, b = row["fixed_chain"], row["moving_chain"]
        pair = a + b
        if (a, b) not in (("A", "B"), ("A", "C"), ("B", "C")) or pair in pairs:
            raise ValueError("Unexpected or duplicate geometry chain pair")
        svd = reference.proper_fit(xyz[a], xyz[b])
        if svd is None:
            if set(row) != {"fixed_chain", "moving_chain", "unavailable"}:
                raise ValueError(f"Nonidentifiable geometry reported as numerical for {pair}")
            reasons = ({"fewer than three matched atoms"} if len(keys) < 3 else
                       {"zero covariance", "overlap does not identify a unique proper rotation"})
            if row["unavailable"] not in reasons:
                raise ValueError(f"Inconsistent missing-fit reason for {pair}")
            pairs[pair] = {"angle_deg": None, "rmsd_A": None, "reason": row["unavailable"]}
            continue
        fields = {"angle_deg": "rotation_angle_deg", "rmsd_A": "fitted_rmsd_angstrom"}
        if set(row) != {"fixed_chain", "moving_chain", *fields.values()}:
            raise ValueError(f"Missing or unexpected metrics in identifiable geometry for {pair}")
        differences = {}
        for endpoint, field in fields.items():
            seen = row[field]
            if (type(seen) not in (int, float) or not math.isfinite(seen)
                    or seen < 0 or (endpoint == "angle_deg" and seen > 180)):
                raise ValueError(f"Invalid {pair} {field}")
            differences[endpoint] = abs(seen - svd[endpoint])
            if differences[endpoint] > reference.FIT_TOL[endpoint]:
                raise ValueError(f"Reproduction tolerance exceeded for {pair} {field}")
        pairs[pair] = {**svd, "absolute_difference": differences}
    if pairs.keys() != set(reference.PAIRS):
        raise ValueError("Incomplete geometry pairs")
    return {
        **mapping, "status": "PASS", "source_parser": f"Bio.PDB.MMCIF2Dict {Bio.__version__}",
        "independent_solver": f"NumPy SVD {np.__version__}", "fit_tolerances": dict(reference.FIT_TOL),
        "units": {"angle_deg": "degree", "rmsd_A": "angstrom"}, "pairs": pairs,
        "evaluable_pairs": sum(row["angle_deg"] is not None for row in pairs.values()),
    }


def _changes(before, after):
    signed = None if before is None or after is None else after - before
    return {"before": before, "after": after, "signed_change": signed,
            "absolute_change": None if signed is None else abs(signed)}


def verify_block(raw: bytes, product: dict) -> dict:
    """Check the complete deletion/control table; retain independent per-interval readouts."""
    expected = reference.reference(raw, product["entry"], product["input_sha256"], product["mask"])
    if product["entry"] != expected["entry"]:
        # The reference normalizes the ID to uppercase; this product preserves its source spelling.
        original_entry, = MMCIF2Dict(StringIO(raw.decode("utf-8")))["_entry.id"]
        if product["entry"] != original_entry:
            raise ValueError("Block product entry differs from the source")
        expected["entry"] = original_entry
    result = compare(expected, product)
    if result["status"] != "PASS":
        raise ValueError(f"Block product differs at {result['errors'][0]['at']}")
    blocks = []
    for block, actual in zip(expected["blocks"], product["blocks"], strict=True):
        if not block["eligible"] and actual.get("median") != {}:
            raise ValueError(f"Ineligible block {block['index']} has an invalid median record")
        metrics = {}
        for endpoint, median in block["median"].items():
            pairs = {}
            for pair, row in block["pairs"].items():
                change = _changes(expected["baseline"][pair][endpoint], row[endpoint])
                pairs[pair] = {**change, "control_max": row[endpoint + "_interpretation"]["control_max"]}
            metrics[endpoint] = {"pairs": pairs, "median": _changes(median["before"], median["after"])}
        blocks.append({"index": block["index"], "interval": block["interval"], "eligible": block["eligible"],
                       "reasons": block["reasons"], "removed_positions": block["positions"],
                       "retained_count": block["retained_count"], "metrics": metrics})
    return {**result, "method": "deposited-block-influence@1", "baseline": expected["baseline"],
            "units": {"angle_deg": "degree", "rmsd_A": "angstrom"}, "blocks": blocks}
