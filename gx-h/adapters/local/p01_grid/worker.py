"""Compute the implementation package's bounded, synthetic score on the declared grid."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = args.grid.read_bytes()
    spec = json.loads(raw)
    if (spec.get("synthetic") is not True or spec["method_id"] != "fixture-grid"
            or spec["method_version"] != "1"):
        raise ValueError("expected synthetic fixture-grid version 1")
    delay = float(spec.get("delay_seconds", 0))
    if not 0 <= delay <= 5:
        raise ValueError("delay_seconds must be between zero and five")
    observations = []
    for candidate in spec["candidates"]:
        x = candidate["x"]
        if type(x) not in (int, float) or not math.isfinite(x):
            raise ValueError("x must be a finite number")
        observations.append({"candidate_id": candidate["candidate_id"], "x": x,
                             "score": -(x - 2) ** 2})
    time.sleep(delay)
    result = {"synthetic": True, "scientific_claims": False, "method_id": spec["method_id"],
              "method_version": spec["method_version"], "operation_id": args.operation,
              "input_sha256": hashlib.sha256(raw).hexdigest(), "observations": observations}
    (args.out / "observations.json").write_text(json.dumps(result, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
