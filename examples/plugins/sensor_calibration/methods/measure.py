"""Summarize paired voltage measurement errors by the supplied period labels."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path


def measure(data):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    columns = ("period", "reading_mV", "reference_mV")
    if tuple(reader.fieldnames or ()) != columns:
        raise ValueError("Expected CSV header " + ",".join(columns))
    errors = {}
    for row in reader:
        period = row["period"].strip()
        if not period or None in row or any(row[column] is None for column in columns):
            raise ValueError(f"Incomplete measurement at line {reader.line_num}")
        error = float(row["reading_mV"]) - float(row["reference_mV"])
        if not math.isfinite(error):
            raise ValueError(f"Non-finite measurement error at line {reader.line_num}")
        errors.setdefault(period, []).append(error)
    if not errors:
        raise ValueError("At least one paired measurement is required")

    periods = []
    signs = {}
    for period, values in errors.items():
        n = len(values)
        periods.append({"period": period, "n": n, "mean_error": math.fsum(values) / n,
                        "mean_absolute_error": math.fsum(map(abs, values)) / n,
                        "root_mean_square_error": math.hypot(*values) / math.sqrt(n)})
        signs[period] = {"positive": sum(value > 0 for value in values),
                         "negative": sum(value < 0 for value in values),
                         "zero": values.count(0)}
    return {"schema": "org.example.sensor-calibration/1", "method": "sensor-calibration@1",
            "unit": "mV", "input_sha256": hashlib.sha256(data).hexdigest(),
            "periods": periods, "error_sign_counts": signs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.samples.read_bytes())
    (args.out / "calibration.json").write_text(json.dumps(result, allow_nan=False) + "\n", encoding="utf-8")
