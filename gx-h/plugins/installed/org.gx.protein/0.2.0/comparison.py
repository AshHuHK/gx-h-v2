"""Frozen criteria and comparison functions from the released coordinate methods at 47fc728d."""
import hashlib
import itertools
import json
import math

SOURCE_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
CHAINS = ("A", "B", "C")
PROTOCOLS = ("common_backbone", "common_ca", "ordered_backbone")
PAIRS = tuple(itertools.combinations(CHAINS, 2))


def digest(data):
    return hashlib.sha256(data).hexdigest()

def object_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8"))

def validate_criteria(doc):
    if not isinstance(doc, dict) or doc.get("schema") != "gx.tl1a.criteria/1":
        raise ValueError("expected gx.tl1a.criteria/1")
    mapping = {"chains": list(CHAINS), "label_entity_id": "1", "model_id": "1",
               "keys": ["label_seq_id", "label_comp_id", "label_atom_id"],
               "pairs_fixed_moving": [list(pair) for pair in PAIRS]}
    if (doc.get("method") != "tl1a-geometry@1" or doc.get("source_sha256") != SOURCE_SHA
            or doc.get("mapping") != mapping or set(doc.get("protocols", {})) != set(PROTOCOLS)):
        raise ValueError("the criteria must identify the 2RE9 source and versioned atom mapping")
    coverage = doc.get("coverage")
    if (not isinstance(coverage, dict) or coverage.get("denominator") != "observed_union_residues"
            or type(coverage.get("min_residues")) is not int or coverage["min_residues"] < 1):
        raise ValueError("specify a positive residue count against observed_union_residues")
    fields = {"coverage": ("min_fraction",),
              "comparison": ("max_angle_delta_deg", "max_rmsd_delta_angstrom"),
              "reproduction": ("angle_abs_tol_deg", "rmsd_abs_tol_angstrom")}
    for section, names in fields.items():
        if not isinstance(doc.get(section), dict):
            raise ValueError(f"missing criteria section {section}")
        for name in names:
            value = doc[section].get(name)
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{section}.{name} must be a finite, nonnegative number")
    if not 0 < coverage["min_fraction"] <= 1:
        raise ValueError("coverage.min_fraction must be in (0, 1]")

def _pairs(observation):
    return {(p["fixed_chain"], p["moving_chain"]): p for p in observation["pairs"]}

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
            "criteria_spec_sha256": object_digest(criteria), "scope": criteria["scope"]}
