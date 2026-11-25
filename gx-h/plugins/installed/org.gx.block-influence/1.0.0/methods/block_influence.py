"""Descriptive leave-one-block-out sensitivity of deposited asymmetric-unit chain pairs."""
import argparse
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import statistics

FITTER_SHA = "f6a589ae44c53725d8066988cda86b1212aad19d26f89660a2e49a3e96f13003"
ENDPOINTS = {"angle_deg": (0.5, 0.02), "rmsd_A": (0.15, 0.002)}
PAIRS = ("AB", "AC", "BC")
MASKS = ("common_backbone", "common_ca")


def load_fitter(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != FITTER_SHA:
        raise ValueError("The released Horn coordinate fitter differs from its immutable digest")
    spec = importlib.util.spec_from_file_location("released_coordinate_fit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def block_of(position):
    return (position - 1) // 20


def selection(raw, mask, fit):
    if mask not in MASKS:
        raise ValueError("Expected common_backbone or common_ca")
    lines = raw.decode("utf-8").splitlines()
    entries = [line.split() for line in lines if line.startswith("_entry.id ")]
    if (len(entries) != 1 or len(entries[0]) != 2 or entries[0][1] in (".", "?")
            or sum(line.lower().startswith("data_") for line in lines) != 1
            or sum(line.strip() == "_atom_site.group_PDB" for line in lines) != 1):
        raise ValueError("Expected one mmCIF entry and one tabular atom_site loop")
    atoms = {chain: {} for chain in fit.CHAINS}
    residue_names = {chain: {} for chain in fit.CHAINS}
    for row in fit._loop_rows(lines, "_atom_site."):
        chain, name = row["label_asym_id"], row["label_atom_id"]
        if ((row["group_PDB"], row["label_entity_id"], row["pdbx_PDB_model_num"]) != ("ATOM", "1", "1")
                or chain not in atoms or name not in fit.BACKBONE):
            continue
        occ = float(row["occupancy"])
        if not 0 <= occ <= 1:
            raise ValueError("Invalid occupancy")
        if occ == 0:
            continue
        pos, res = int(row["label_seq_id"]), row["label_comp_id"]
        key = pos, res, name
        if (pos < 1 or res in ("?", ".") or row["type_symbol"] != fit.BACKBONE[name]
                or row["label_alt_id"] not in ("?", ".") or key in atoms[chain]
                or residue_names[chain].setdefault(pos, res) != res):
            raise ValueError("Ambiguous backbone or residue mapping in model 1, entity 1")
        xyz = tuple(float(row["Cartn_" + axis]) for axis in "xyz")
        if not all(math.isfinite(x) for x in (*xyz, float(row["B_iso_or_equiv"]))):
            raise ValueError("Nonfinite coordinate or B factor")
        atoms[chain][key] = xyz
    union = set().union(*(names.keys() for names in residue_names.values()))
    declared = set()
    if any(line.strip() == "_entity_poly_seq.num" for line in lines):
        declared = {int(row["num"]) for row in fit._loop_rows(lines, "_entity_poly_seq.") if row["entity_id"] == "1"}
        if any(pos < 1 for pos in declared):
            raise ValueError("Invalid declared polymer position")
    common = set.intersection(*(set(mapping) for mapping in atoms.values()))
    complete = {k[:2] for k in common if all((*k[:2], atom) in common for atom in fit.BACKBONE)}
    keys = sorted(k for k in common if (k[2] == "CA" if mask == "common_ca" else k[:2] in complete))
    return entries[0][1].strip("'\""), atoms, keys, union, declared, residue_names


def fits(atoms, keys, fit):
    rows = {}
    for pair in PAIRS:
        try:
            angle, rmsd = fit.fit([atoms[pair[0]][k] for k in keys], [atoms[pair[1]][k] for k in keys])
            rows[pair] = {"angle_deg": angle, "rmsd_A": rmsd, "reason": None}
        except ValueError as error:
            rows[pair] = {"angle_deg": None, "rmsd_A": None, "reason": str(error)}
    return rows


def compare(rows, baseline):
    for pair, values in rows.items():
        for endpoint in ENDPOINTS:
            before, after = baseline[pair][endpoint], values[endpoint]
            values["delta_" + endpoint] = abs(before - after) if before is not None and after is not None else None


def dispersed(positions, input_sha, mask, index, size):
    if len(positions) < size or len({block_of(p) for p in positions}) < 3:
        return
    seen = set()
    for k in range(1024):
        prefix = f"GX-BLOCK-QC-1|{input_sha}|{mask}|{index}|{k}|"
        ordered = sorted(positions, key=lambda p: (hashlib.sha256((prefix + str(p)).encode("ascii")).digest(), p))
        sample = tuple(sorted(ordered[:size]))
        if sample in seen or len({block_of(p) for p in sample}) < 3:
            continue
        seen.add(sample)
        yield k, list(sample)
        if len(seen) == 20:
            return


def median_effect(rows, baseline, endpoint):
    before = [baseline[p][endpoint] for p in PAIRS]
    after = [rows[p][endpoint] for p in PAIRS]
    if None in before or None in after:
        return dict(before=None, after=None, delta=None, concealed=None, exceptions=[])
    med_before, med_after = statistics.median(before), statistics.median(after)
    delta = abs(med_after - med_before)
    limit = ENDPOINTS[endpoint][0]
    exceptions = [p for p in PAIRS if rows[p]["delta_" + endpoint] > limit]
    return dict(before=med_before, after=med_after, delta=delta, exceptions=exceptions,
                concealed=delta <= limit and bool(exceptions))


def assess_blocks(positions, keys, atoms, baseline, input_sha, mask, indices, union, fit):
    blocks = []
    for index in indices:
        local = [p for p in positions if block_of(p) == index]
        remaining = len(positions) - len(local)
        reasons = []
        if len(positions) < 60:
            reasons.append("fewer_than_60_before")
        if not union:
            reasons.append("empty_observed_union")
        elif 5 * len(positions) < 3 * len(union):
            reasons.append("coverage_below_60_percent")
        if len(local) < 10:
            reasons.append("fewer_than_10_in_block")
        if remaining < 60:
            reasons.append("fewer_than_60_after")
        if 5 * remaining < 3 * len(positions):
            reasons.append("retained_below_60_percent")
        block = dict(index=index, interval=[20 * index + 1, 20 * index + 20], positions=local,
                     positions_sha256=fit.object_digest(local), retained_count=remaining,
                     eligible=not reasons, reasons=reasons, pairs={}, median={}, controls=[])
        blocks.append(block)
        if reasons:
            continue
        retained = [k for k in keys if block_of(k[0]) != index]
        block["retained_keys_sha256"] = fit.object_digest(retained)
        rows = fits(atoms, retained, fit)
        compare(rows, baseline)
        block["pairs"] = rows
        block["median"] = {e: median_effect(rows, baseline, e) for e in ENDPOINTS}
        pool = [p for p in positions if block_of(p) != index]
        for k, sample in dispersed(pool, input_sha, mask, index, len(local)):
            removed = set(sample)
            controls = fits(atoms, [key for key in keys if key[0] not in removed], fit)
            compare(controls, baseline)
            block["controls"].append({"k": k, "positions": sample, "positions_sha256": fit.object_digest(sample),
                                      "pairs": controls})
        for pair, values in rows.items():
            for endpoint, (limit, tolerance) in ENDPOINTS.items():
                delta = values["delta_" + endpoint]
                controls = [c["pairs"][pair]["delta_" + endpoint] for c in block["controls"]]
                sufficient = len(controls) == 20 and None not in controls
                values[endpoint + "_assessment"] = {
                    "over_limit": delta > limit if delta is not None else None,
                    "margin": delta - limit if delta is not None else None,
                    "borderline": abs(delta - limit) <= tolerance if delta is not None else None,
                    "control_max": max(controls) if sufficient else None,
                    "exceeds_dispersed_range": delta > limit and delta > max(controls)
                        if sufficient and delta is not None else None}
    return blocks


def summary(blocks, endpoint):
    values = [(row["delta_" + endpoint], b["index"], p) for b in blocks for p, row in b["pairs"].items()
              if row["delta_" + endpoint] is not None]
    usable = [b for b in blocks if b["eligible"] and b["median"][endpoint]["delta"] is not None]
    eligible = sum(b["eligible"] for b in blocks)
    exceptions = [{"block_index": i, "pair": p, "delta": d} for d, i, p in values if d > ENDPOINTS[endpoint][0]]
    maximum = min(values, key=lambda x: (-x[0], x[1], x[2])) if values else (None, None, None)
    return {"max": maximum[0], "block_index": maximum[1], "pair": maximum[2], "exceptions": exceptions,
            "concealed_blocks": [b["index"] for b in usable if b["median"][endpoint]["concealed"]],
            "usable_blocks": len(usable), "eligible_blocks": eligible, "global_assessable": len(usable) >= 3,
            "all_within_limit": not exceptions if len(usable) >= 3 and len(usable) == eligible else None}


def measure(raw, mask, fit):
    sha = hashlib.sha256(raw).hexdigest()
    entry, atoms, keys, union, declared, mappings = selection(raw, mask, fit)
    positions = sorted({k[0] for k in keys})
    baseline = fits(atoms, keys, fit)
    last = max(union | declared, default=0)
    indices = range(block_of(last) + 1) if last else ()
    blocks = assess_blocks(positions, keys, atoms, baseline, sha, mask, indices, union, fit)
    return {"schema": "gx.block-influence/1", "method": "deposited-block-influence@1", "entry": entry,
            "model": "1", "label_entity": "1", "chains": list(fit.CHAINS), "input_sha256": sha, "mask": mask,
            "matched_keys_sha256": fit.object_digest(keys), "matched_atoms": len(keys),
            "positions": positions, "selected_residues": len(positions), "union_positions": sorted(union),
            "observed_union_residues": len(union), "declared_positions": sorted(declared),
            "observed_chains": {c: sorted(mappings[c]) for c in fit.CHAINS},
            "matched_residues": [[p, mappings["A"][p]] for p in positions],
            "eligible_blocks": sum(b["eligible"] for b in blocks),
            "baseline": baseline, "blocks": blocks, "summary": {e: summary(blocks, e) for e in ENDPOINTS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", required=True, type=Path)
    parser.add_argument("--fitter", required=True, type=Path)
    parser.add_argument("--mask", required=True, choices=MASKS)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = measure(args.structure.read_bytes(), args.mask, load_fitter(args.fitter))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "block-influence.json").write_text(json.dumps(result, separators=(",", ":"), allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
