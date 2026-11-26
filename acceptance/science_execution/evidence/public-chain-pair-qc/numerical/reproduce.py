"""Refit the published coordinates and check every reported pair, interval, and pass/fail call."""
import csv
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))
from acceptance.v623.study.independent_geometry import FIT_TOL, LIMITS, PAIRS, reference


def main():
    record = json.loads((HERE / "verification.json").read_text())
    measurements = REPO / record["measurements"]["file"]
    if sha256(measurements.read_bytes()).hexdigest() != record["measurements"]["sha256"]:
        raise ValueError("Measurement file digest mismatch")
    with measurements.open(encoding="utf-8", newline="") as stream:
        data = [r for r in csv.DictReader(stream) if r["eligible"] == "true"]
    def key(row):
        return row["entry"], row["mask"], int(row["block_index"]), row["pair"], row["endpoint"]
    rows = {key(row): row for row in data}
    if len(data) != len(rows):
        raise ValueError("Duplicate measurement row")

    count = 0
    max_error = {ep: 0.0 for ep in LIMITS}
    for table in record["tables"]:
        entry, mask = table["entry"], table["mask"]
        source = record["sources"][entry]
        ref = reference((REPO / source["mmcif"]).read_bytes(), entry, source["sha256"], mask)
        positives = {(r["block_index"], r["pair"], r["endpoint"]) for r in table["passes_opening_screen"]}
        for block in ref["blocks"]:
            if not block["eligible"]:
                continue
            for pair in PAIRS:
                for ep in LIMITS:
                    row = rows.pop((entry, mask, block["index"], pair, ep))
                    if row["product_sha256"] != table["output_sha256"] or row["input_sha256"] != source["sha256"]:
                        raise ValueError(f"Input or product digest mismatch for {key(row)}")
                    dims = [int(row[k]) for k in ("first_label_seq_id", "last_label_seq_id", "matched_in_block",
                                                  "matched_outside_block")]
                    if dims != [*block["interval"], len(block["positions"]), block["retained_count"]]:
                        raise ValueError(f"Selection mismatch for {key(row)}")
                    fit = block["pairs"][pair]
                    median = block["median"][ep]
                    control = fit[ep + "_interpretation"]["control_max"]
                    delta = fit["delta_" + ep]
                    values = {"baseline": ref["baseline"][pair][ep], "after_deletion": fit[ep], "absolute_delta": delta,
                              "median_before": median["before"], "median_after": median["after"],
                              "median_absolute_delta": median["delta"], "control_max": control}
                    for field, value in values.items():
                        error = abs(float(row[field]) - value)
                        tol = FIT_TOL[ep] * (2 if field in ("absolute_delta", "median_absolute_delta", "control_max") else 1)
                        if not isfinite(error) or error > tol:
                            raise ValueError(f"Disagreement in {key(row)} {field}: {error}")
                        max_error[ep] = max(max_error[ep], error)
                    screen = record["opening_screen"]
                    passed = (delta >= screen["minimum_delta"][ep] and
                              delta >= screen["minimum_pair_to_median_multiplier"] * median["delta"] and
                              len(block["controls"]) == screen["must_exceed_controls"] and delta > control)
                    if passed != ((block["index"], pair, ep) in positives):
                        raise ValueError(f"Screen disagreement for {key(row)}")
                    count += 1
    if rows or count != record["measurements"]["rows"]:
        raise ValueError("Unexpected measurement rows")
    print(json.dumps({"checked_rows": count, "max_absolute_error_on_reported_fields": max_error}))


if __name__ == "__main__":
    main()
