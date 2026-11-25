"""Independently re-read the same original mmCIF and map; verify all four comparison rows."""
import argparse
from collections import defaultdict
from hashlib import sha256
from io import StringIO
from itertools import combinations
import json
import math
from pathlib import Path
from statistics import median

import Bio
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np

from acceptance.v623.study.independent_geometry import FIT_TOL, LIMITS, proper_fit, rows


def reread(raw, mapping_raw, interval, mask):
    source_sha = sha256(raw).hexdigest()
    mapping = json.loads(mapping_raw)
    if mapping["schema"] != "gx.coordinate-position-map/1" or mapping["structure_sha256"] != source_sha:
        raise ValueError("Independent reader: wrong map or original")
    chains = mapping["chains"]
    if len(chains) != 3 or len(set(chains)) != 3 or not all(isinstance(v, str) and v not in ("?", ".", "") for v in
                                                            [*chains, mapping["reference"], mapping["model"], mapping["entity"]]):
        raise ValueError("Independent reader: invalid chain/model/entity/reference binding")
    cif = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    entry, = cif["_entry.id"]
    declared = {int(n) for ent, n in rows(cif, "entity_poly_seq", "entity_id num") if ent == mapping["entity"]}
    positions = mapping["segments"][interval]
    if interval not in ("21-40", "101-120") or not isinstance(positions, list) or len(positions) != 20:
        raise ValueError("Independent reader: expected twenty positions in a registered reference interval")
    present = [p for p in positions if p is not None]
    if (not all(type(p) is int and p > 0 and p in declared for p in present)
            or any(a >= b for a, b in zip(present, present[1:]))):
        raise ValueError("Independent reader: ambiguous or out-of-domain target positions")

    # Build residue inventories before applying the selected mask. A variant, repeated
    # or ambiguous atom invalidates that atom; a mixed residue identity invalidates the site.
    components = defaultdict(set)
    samples = defaultdict(list)
    fields = ("group_PDB label_entity_id pdbx_PDB_model_num label_asym_id label_seq_id "
              "label_comp_id label_atom_id type_symbol label_alt_id occupancy Cartn_x Cartn_y Cartn_z")
    elements = {"CA": "C", "N": "N", "C": "C", "O": "O"}
    names = {"common_ca": ["CA"], "common_backbone": sorted(elements)}[mask]
    for group, entity, model, chain, number, residue, atom, element, alt, occ, *xyz in rows(cif, "atom_site", fields):
        if (group, entity, model) != ("ATOM", mapping["entity"], mapping["model"]) or chain not in chains or atom not in elements:
            continue
        occupancy = float(occ)
        if not 0 <= occupancy <= 1:
            raise ValueError("Independent reader: invalid occupancy")
        if not occupancy:
            continue
        n = int(number)
        if n not in declared or element != elements[atom]:
            raise ValueError("Independent reader: undeclared position or wrong element")
        components[chain, n].add(residue)
        if atom in names:
            point = [float(x) for x in xyz]
            if not all(map(math.isfinite, point)):
                raise ValueError("Independent reader: nonfinite coordinate")
            samples[chain, n, atom].append(point if alt in ("?", ".") else None)

    union = {n for c, n in components}
    matched = []
    for n in sorted(union):
        ids = [components[c, n] for c in chains]
        if not (ids[0] == ids[1] == ids[2] and len(ids[0]) == 1 and not ids[0].intersection({"?", "."})):
            continue
        if all(len(samples[c, n, a]) == 1 and samples[c, n, a][0] is not None for c in chains for a in names):
            matched.append(n)
    omitted = set(present).intersection(matched)
    retained = [n for n in matched if n not in omitted]
    counts = {"selected_residues": len(matched), "observed_union_residues": len(union),
              "retained_residues": len(retained), "omitted_residues": len(omitted)}
    reasons = []
    if len(matched) < 60:
        reasons.append("fewer_than_60_before")
    if not union or len(matched) < 0.6 * len(union):
        reasons.append("insufficient_observed_coverage")
    if len(omitted) < 10:
        reasons.append("fewer_than_10_mapped_matched_in_interval")
    if len(retained) < 60:
        reasons.append("fewer_than_60_after")
    if len(retained) < 0.6 * len(matched):
        reasons.append("retained_below_60_percent")

    def arrays(selected):
        return {c: np.asarray([samples[c, n, a][0] for n in selected for a in names], dtype=float)
                .reshape(-1, 3) for c in chains}

    scope = {"schema": "gx.mapped-segment/2", "method": "deposited-block-influence@2", "entry": entry,
             "input_sha256": source_sha, "mapping_sha256": sha256(mapping_raw).hexdigest(),
             **{k: mapping[k] for k in ("model", "entity", "reference", "chains")}, "target_positions": positions,
             **counts, "reasons": reasons}
    return scope, arrays(matched), arrays(retained)


def verify(structure_raw: bytes, mapping_raw: bytes, table: dict) -> dict:
    """Raise on a scope, applicability or numerical discrepancy; include both independent fits and residuals."""
    endpoint = table["endpoint"]
    fit_tol = FIT_TOL[endpoint]
    threshold = LIMITS[endpoint]
    delta_tol = 2 * fit_tol
    unit = {"angle_deg": "deg", "rmsd_A": "Å"}[endpoint]
    scope, before, after = reread(structure_raw, mapping_raw, table["interval"], table["mask"])
    for k, value in {**scope, "threshold": threshold, "unit": unit, "tolerance": delta_tol}.items():
        if table.get(k) != value:
            raise ValueError(f"Independent source/selection mismatch in {k}")

    actual = {}
    for (i, a), (j, b) in combinations(enumerate(scope["chains"], 1), 2):
        pair = f"pair{i}{j}"
        fits = [None, None] if scope["reasons"] else [proper_fit(x[a], x[b]) for x in (before, after)]
        actual[pair] = {"chains": [a, b], **dict(zip(("before", "after"), [f[endpoint] if f else None for f in fits]))}
    middle = {"chains": scope["chains"]}
    for stage in ("before", "after"):
        values = [v[stage] for v in actual.values()]
        middle[stage] = median(values) if None not in values else None
    actual["median"] = middle
    if set(table["rows"]) != set(actual):
        raise ValueError("Expected three labeled chain pairs and one representative row")
    diffs = {}
    max_fit = max_delta = None
    for key, independent in actual.items():
        official = table["rows"][key]
        if official["chains"] != independent["chains"]:
            raise ValueError(f"Mismatched deposited chains in {key}")
        a, b = independent["before"], independent["after"]
        independent["delta"] = abs(a-b) if None not in (a, b) else None
        diffs[key] = {}
        for field in ("before", "after", "delta"):
            value, expected = official[endpoint][field], independent[field]
            tolerance = delta_tol if field == "delta" else fit_tol
            if value is None and expected is None:
                diffs[key][field] = None
                continue
            if expected is None or type(value) not in (int, float) or not math.isfinite(value) or abs(value-expected) > tolerance:
                raise ValueError(f"SVD disagreement in {key}.{field}: product={value}, independent={expected}, allowed={tolerance}")
            gap = abs(value-expected)
            diffs[key][field] = gap
            if field == "delta":
                max_delta = max(gap, max_delta or 0)
            else:
                max_fit = max(gap, max_fit or 0)
    # Apply the strict threshold to the released values and retain a separate boundary
    # flag. Rounding or SVD noise near a cutoff cannot strengthen a borderline conclusion.
    reported = {k: v[endpoint]["delta"] for k, v in table["rows"].items()}
    exceptions = [k for k in ("pair12", "pair13", "pair23") if reported[k] is not None and reported[k] > threshold]
    boundary = [k for k in actual if reported[k] is not None and abs(reported[k]-threshold) <= delta_tol]
    representative = reported["median"]
    concealed = None if representative is None else bool(exceptions and representative <= threshold)
    if representative is None:
        outcome = "UNDETERMINED"
    elif boundary:
        outcome = "BORDERLINE"
    elif concealed:
        outcome = "CONCEALED"
    elif representative > threshold:
        outcome = "MEDIAN_ALSO_OVER_LIMIT"
    else:
        outcome = "REPRESENTATIVE_WITHIN_LIMIT"
    if (table["exceptions"], table["borderline"], table["concealed"], table["interpretation"]) != (exceptions, boundary, concealed, outcome):
        raise ValueError("Released scientific interpretation disagrees with its unrounded rows")
    return {"status": "PASS",
            "parser": "Bio.PDB.MMCIF2Dict " + Bio.__version__, "solver": "NumPy SVD " + np.__version__,
            "fit_tolerance": fit_tol, "delta_tolerance": delta_tol, "max_fit_difference": max_fit,
            "max_delta_difference": max_delta, "unit": unit, "interpretation": outcome,
            **scope, "schema": "gx.independent-mapped-segment/1", "endpoint": endpoint, "mask": table["mask"],
            "interval": table["interval"], "independent_rows": actual, "absolute_differences": diffs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("structure", "mapping", "table", "out"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.structure.read_bytes(), args.mapping.read_bytes(), json.loads(args.table.read_bytes()))
    with args.out.open("x") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
