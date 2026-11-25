"""Export full-precision pair/interval measurements and per-deposit summaries from GX products.

Blank numeric/boolean cells mean that the input product has no value for that
comparison. Interval eligibility, fit reasons and control counts explain missing
results. Every row pins the original mmCIF, selected atom set and exact GX table.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path


ENDPOINTS = {"angle_deg": "delta_angle_deg", "rmsd_A": "delta_rmsd_angstrom"}
PAIRS = ("AB", "AC", "BC")
SOURCE = ("entry", "mask", "model", "label_entity", "input_sha256", "matched_keys_sha256", "product_sha256")
ENDPOINT = ("endpoint", "unit", "report_threshold", "fit_abs_tolerance", "delta_abs_tolerance")
CELL = (*SOURCE, "block_index", "first_label_seq_id", "last_label_seq_id", "eligible", "eligibility_reasons",
        "matched_in_block", "matched_outside_block", "block_positions_sha256", "retained_keys_sha256",
        "pair", *ENDPOINT, "baseline", "baseline_fit_reason", "after_deletion", "after_fit_reason",
        "absolute_delta", "threshold_margin", "over_threshold", "borderline", "control_sets", "valid_control_sets",
        "required_control_sets", "control_max", "exceeds_dispersed_range", "median_before", "median_after",
        "median_absolute_delta", "median_concealed", "median_exception_pairs")
SUMMARY = (*SOURCE, *ENDPOINT, "matched_atoms", "selected_residues", "observed_union_residues",
           "total_blocks", "eligible_blocks", "ineligible_blocks", "ineligible_reason_counts",
           "usable_three_pair_blocks", "eligible_pair_cells", "measured_pair_cells", "over_threshold_cells",
           "borderline_cells", "dispersed_comparable_cells", "above_dispersed_cells", "concealed_blocks",
           "max_absolute_delta", "max_block_index", "max_pair", "global_assessable", "all_within_threshold")


def rows(product, digest, protocol):
    if product["schema"] != "gx.block-influence/1" or product["method"] != "deposited-block-influence@1":
        raise ValueError("Expected a full gx.block-influence/1 product from deposited-block-influence@1")
    source = {key: product[key] for key in SOURCE if key != "product_sha256"}
    source["product_sha256"] = digest
    cells, summaries = [], []
    blocks = product["blocks"]
    reasons = {}
    for block in blocks:
        for reason in block["reasons"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    required_controls = protocol["dispersed_reference"]["unique_sets"]

    for name, protocol_key in ENDPOINTS.items():
        definition = protocol["endpoints"][protocol_key]
        endpoint = {**source, "endpoint": name, "unit": definition["unit"],
                    "report_threshold": definition["report_tolerance"],
                    "fit_abs_tolerance": definition["single_fit_absolute_tolerance"],
                    "delta_abs_tolerance": definition["delta_absolute_tolerance"]}
        delta = "delta_" + name
        comparisons = []
        for block in blocks:
            median = block["median"][name] if block["eligible"] else {}
            for pair in PAIRS:
                base = product["baseline"][pair]
                reading = block["pairs"][pair] if block["eligible"] else {}
                assessment = reading[name + "_assessment"] if block["eligible"] else {}
                comparisons.append({
                    **endpoint, "block_index": block["index"], "first_label_seq_id": block["interval"][0],
                    "last_label_seq_id": block["interval"][1], "eligible": block["eligible"],
                    "eligibility_reasons": block["reasons"], "matched_in_block": len(block["positions"]),
                    "matched_outside_block": block["retained_count"], "block_positions_sha256": block["positions_sha256"],
                    "retained_keys_sha256": block.get("retained_keys_sha256"), "pair": pair, "baseline": base[name],
                    "baseline_fit_reason": base["reason"], "after_deletion": reading.get(name),
                    "after_fit_reason": reading.get("reason"), "absolute_delta": reading.get(delta),
                    "threshold_margin": assessment.get("margin"), "over_threshold": assessment.get("over_limit"),
                    "borderline": assessment.get("borderline"), "control_sets": len(block["controls"]),
                    "valid_control_sets": sum(c["pairs"][pair][delta] is not None for c in block["controls"]),
                    "required_control_sets": required_controls, "control_max": assessment.get("control_max"),
                    "exceeds_dispersed_range": assessment.get("exceeds_dispersed_range"),
                    "median_before": median.get("before"), "median_after": median.get("after"),
                    "median_absolute_delta": median.get("delta"), "median_concealed": median.get("concealed"),
                    "median_exception_pairs": median.get("exceptions") if median.get("delta") is not None else None,
                })
        summary = product["summary"][name]
        eligible = summary["eligible_blocks"]
        summaries.append({
            **endpoint, "matched_atoms": product["matched_atoms"], "selected_residues": product["selected_residues"],
            "observed_union_residues": product["observed_union_residues"], "total_blocks": len(blocks),
            "eligible_blocks": eligible, "ineligible_blocks": len(blocks) - eligible, "ineligible_reason_counts": reasons,
            "usable_three_pair_blocks": summary["usable_blocks"], "eligible_pair_cells": eligible * len(PAIRS),
            "measured_pair_cells": sum(c["absolute_delta"] is not None for c in comparisons),
            "over_threshold_cells": len(summary["exceptions"]),
            "borderline_cells": sum(c["borderline"] is True for c in comparisons),
            "dispersed_comparable_cells": sum(c["exceeds_dispersed_range"] is not None for c in comparisons),
            "above_dispersed_cells": sum(c["exceeds_dispersed_range"] is True for c in comparisons),
            "concealed_blocks": summary["concealed_blocks"], "max_absolute_delta": summary["max"],
            "max_block_index": summary["block_index"], "max_pair": summary["pair"],
            "global_assessable": summary["global_assessable"], "all_within_threshold": summary["all_within_limit"],
        })
        cells.extend(comparisons)
    return cells, summaries


def write_table(path, fields, data):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for row in data:
            writer.writerow({key: json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                             if isinstance(value, (list, dict, bool)) else value for key, value in row.items()})


def export(paths, output):
    protocol = json.loads(Path(__file__).with_name("operator").joinpath("reference_protocol.json").read_bytes())
    cells, summaries, seen = [], [], set()
    for path in paths:
        raw = path.read_bytes()
        product = json.loads(raw)
        key = tuple(product[k] for k in ("entry", "input_sha256", "model", "label_entity", "mask"))
        if key in seen:
            raise ValueError(f"Duplicate deposit and mask: {key}")
        seen.add(key)
        measured, aggregate = rows(product, hashlib.sha256(raw).hexdigest(), protocol)
        cells.extend(measured)
        summaries.extend(aggregate)
    output.mkdir(parents=True, exist_ok=False)
    tables = {}
    for filename, fields, data in (("pair_intervals.csv", CELL, cells), ("deposit_summary.csv", SUMMARY, summaries)):
        path = output / filename
        write_table(path, fields, data)
        tables[filename] = {"rows": len(data), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return tables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", nargs="+", action="extend", required=True, type=Path,
                        help="Full published block-influence JSON files")
    parser.add_argument("--output", required=True, type=Path, help="New directory for the UTF-8 CSV tables")
    args = parser.parse_args()
    try:
        result = export(args.product, args.output)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Cannot export tables: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
