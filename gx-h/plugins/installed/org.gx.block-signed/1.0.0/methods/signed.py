"""Report directional changes in three fitted chain pairs after omitting each eligible interval."""
import argparse
from collections import Counter
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from statistics import median

PAIRS = ("AB", "AC", "BC")
UNITS = {"angle_deg": "deg", "rmsd_A": "Å"}
MISSED = {"near_zero", "attenuated", "opposed"}


def finite_number(value):
    if type(value) not in (int, float, Decimal):
        raise ValueError("Expected a finite number")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Expected a finite number")
    return number


def readout(before, after):
    # Keep original precision until after the classification, including threshold equality.
    a = finite_number(before) if before is not None else None
    b = finite_number(after) if after is not None else None
    return {"before": a, "after": b, "delta": b - a if a is not None and b is not None else None}


def classify(pairs, middle, e, z):
    if any(row["delta"] is None for row in pairs.values()):
        return "unmeasured"
    large = [row["delta"] for row in pairs.values() if abs(row["delta"]) >= e]
    dm = middle["delta"]
    if not large:
        return "quiet"
    if abs(dm) < z:
        return "near_zero"
    if abs(dm) < e:
        return "attenuated"
    if any((v > 0) != (dm > 0) for v in large):
        return "opposed"
    return "represented"


def summarize(table, endpoint, effect_minimum, zero_limit, minimum_evaluable):
    if table.get("schema") != "gx.block-influence/1" or endpoint not in UNITS:
        raise ValueError("Expected a released block-influence table and physical endpoint")
    e, z = map(finite_number, (effect_minimum, zero_limit))
    if not 0 < z < e or type(minimum_evaluable) is not int or minimum_evaluable < 1:
        raise ValueError("Require 0 < zero_limit < effect_minimum and integer minimum_evaluable >= 1")
    counts = Counter()
    seen = set()
    blocks = []
    for block in table["blocks"]:
        index = block["index"]
        if type(index) is not int or index < 0 or index in seen or type(block["eligible"]) is not bool:
            raise ValueError("Expected a unique nonnegative block index and explicit eligibility decision")
        seen.add(index)
        row = {"block_index": index, "interval": block.get("interval", [20 * index + 1, 20 * index + 20]),
               "reasons": list(block.get("reasons", [])), "omitted_positions": block.get("positions", []),
               "retained_count": block.get("retained_count"), "eligible": block["eligible"]}
        if block["eligible"]:
            pairs = {p: readout(table["baseline"][p][endpoint], block["pairs"][p][endpoint]) for p in PAIRS}
            def middle(stage):
                values = [pair[stage] for pair in pairs.values()]
                return median(values) if None not in values else None
            middle_row = readout(middle("before"), middle("after"))
            label = classify(pairs, middle_row, e, z)
            row.update(pairs=pairs, median=middle_row)
            if label == "unmeasured":
                row["fit_reasons"] = {p: {"before": table["baseline"][p].get("reason"),
                                          "after": block["pairs"][p].get("reason")} for p in PAIRS
                                      if pairs[p]["delta"] is None}
        else:
            label = "ineligible"
        row["classification"] = label
        counts[label] += 1
        blocks.append(row)

    eligible = len(blocks) - counts["ineligible"]
    evaluable = eligible - counts["unmeasured"]
    missed = sum(counts[label] for label in MISSED)
    reason = None
    if evaluable < minimum_evaluable:
        presence, reason = None, "too_few_evaluable_blocks"
    elif missed:
        presence = 1
    elif counts["unmeasured"]:
        presence, reason = None, "unmeasured_eligible_blocks"
    else:
        presence = 0
    return {"endpoint": endpoint, "unit": UNITS[endpoint], "blocks_total": len(blocks), "blocks_eligible": eligible,
            "blocks_evaluable": evaluable, "blocks_unmeasured": counts["unmeasured"],
            "blocks_missed_observed": missed, "missed_present": presence, "unavailable_reason": reason,
            "blocks": blocks}


def measure(structure, engine_path, fitter_path, mask, endpoint, e, z, k):
    spec = importlib.util.spec_from_file_location("released_block_influence", engine_path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    table = engine.measure(structure.read_bytes(), mask, engine.load_fitter(fitter_path))
    result = summarize(table, endpoint, e, z, k)
    blocks = result.pop("blocks")
    scope = {key: table[key] for key in ("entry", "input_sha256", "mask", "model", "label_entity", "chains",
                                         "matched_keys_sha256", "selected_residues", "observed_union_residues")}
    scope.update(method="deposited-block-signed@1", endpoint=endpoint, unit=UNITS[endpoint],
                 effect_minimum=e, zero_limit=z, minimum_evaluable=k)
    return ({"schema": "gx.signed-summary/1", **scope, **result, "detail_output": "signed-blocks.json"},
            {"schema": "gx.signed-blocks/1", **scope, "blocks": blocks})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("structure", "engine", "fitter", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--mask", required=True, choices=("common_backbone", "common_ca"))
    parser.add_argument("--endpoint", required=True, choices=UNITS)
    parser.add_argument("--effect-minimum", required=True, type=Decimal)
    parser.add_argument("--zero-limit", required=True, type=Decimal)
    parser.add_argument("--minimum-evaluable", required=True, type=int)
    args = parser.parse_args()
    summary, blocks = measure(args.structure, args.engine, args.fitter, args.mask, args.endpoint,
                              args.effect_minimum, args.zero_limit, args.minimum_evaluable)
    args.out.mkdir(parents=True, exist_ok=True)
    for filename, data in (("signed-summary.json", summary), ("signed-blocks.json", blocks)):
        (args.out / filename).write_text(json.dumps(data, ensure_ascii=False, allow_nan=False,
                                                   default=float, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
