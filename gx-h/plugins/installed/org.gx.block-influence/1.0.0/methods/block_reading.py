"""Read one chain pair in a selected interval from a completed block-influence table."""
import argparse
import hashlib
import json
from pathlib import Path


def project(raw, pair, index):
    table = json.loads(raw)
    if table["schema"] != "gx.block-influence/1" or pair not in ("AB", "AC", "BC") or index < 0:
        raise ValueError("Expected a block-influence table, chain pair and nonnegative interval index")
    block = next((b for b in table["blocks"] if b["index"] == index), None)
    result = {"schema": "gx.block-reading/1", "input_sha256": hashlib.sha256(raw).hexdigest(),
              "material_sha256": table["input_sha256"], "entry": table["entry"], "mask": table["mask"],
              "pair": pair, "block_index": index, "model": table["model"], "label_entity": table["label_entity"],
              "matched_keys_sha256": table["matched_keys_sha256"],
              "reasons": block["reasons"] if block else ["outside_declared_or_observed_range"],
              "controls": len(block["controls"]) if block else 0}
    values = block["pairs"].get(pair, {}) if block else {}
    for endpoint in ("angle_deg", "rmsd_A"):
        result["baseline_" + endpoint] = table["baseline"][pair][endpoint]
        result["after_" + endpoint] = values.get(endpoint)
        result["delta_" + endpoint] = values.get("delta_" + endpoint)
        result["median_delta_" + endpoint] = block["median"].get(endpoint, {}).get("delta") if block else None
        assessment = values.get(endpoint + "_assessment", {})
        result["control_max_" + endpoint] = assessment.get("control_max")
        result["assessment_" + endpoint] = assessment
    if values.get("reason"):
        result["reasons"] = [*result["reasons"], values["reason"]]
    if table["baseline"][pair]["reason"]:
        result["reasons"] = [*result["reasons"], table["baseline"][pair]["reason"]]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, type=Path)
    parser.add_argument("--pair", required=True, choices=("AB", "AC", "BC"))
    parser.add_argument("--block-index", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = project(args.table.read_bytes(), args.pair, args.block_index)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "cell.json").write_text(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
