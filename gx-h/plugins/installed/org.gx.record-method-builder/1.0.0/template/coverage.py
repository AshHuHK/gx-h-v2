"""Coverage of unique cohort identifiers by at least one located evidence value."""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path


FIELDS = ("cohort_rows", "cohort_id", "evidence_rows", "evidence_id", "evidence_value")


def validate_fields(fields):
    if set(fields) != set(FIELDS):
        raise ValueError("Supply both record-array fields, both ID fields, and the evidence value field")
    for name, field in fields.items():
        if name.endswith("_rows") and field == "$":
            continue
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", field):
            raise ValueError("Select literal JSON field names, or $ for a top-level array")
    if fields["evidence_id"] == fields["evidence_value"]:
        raise ValueError("An identifier cannot also stand for its evidence value")


def rows(data, field):
    value = data if field == "$" else data.get(field) if isinstance(data, dict) else None
    if not isinstance(value, list) or any(not isinstance(record, dict) for record in value):
        raise ValueError(f"Expected a JSON record array at {field!r}")
    return value


def identifier(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid record identifier at {field!r}")
    return value.strip()


def observed(value):
    if value is None or isinstance(value, str):
        return bool(value and value.strip())
    if type(value) == int or type(value) == float and math.isfinite(value):
        return True
    raise ValueError("Evidence values must be text, finite numbers or null")


def measure(cohort, evidence, fields):
    validate_fields(fields)
    members = rows(cohort, fields["cohort_rows"])
    records = rows(evidence, fields["evidence_rows"])
    ids = {identifier(row, fields["cohort_id"]) for row in members}
    if not ids:
        raise ValueError("Empty declared cohort; the evidence coverage denominator is undefined")
    covered, matched, external = set(), set(), set()
    for row in records:
        key = identifier(row, fields["evidence_id"])
        if key not in ids:
            external.add(key)
            continue
        matched.add(key)
        if observed(row.get(fields["evidence_value"])):
            covered.add(key)
    return {"numerator": len(covered), "denominator": len(ids), "coverage": len(covered)/len(ids),
            "unit": "fraction of unique cohort IDs", "covered_ids": sorted(covered),
            "matched_without_value": sorted(matched - covered), "unmatched_ids": sorted(ids - matched),
            "outside_cohort_ids": sorted(external), "cohort_rows": len(members), "evidence_rows": len(records)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("cohort", "evidence", "spec", "out"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_bytes())
    cohort, evidence = args.cohort.read_bytes(), args.evidence.read_bytes()
    result = measure(json.loads(cohort), json.loads(evidence), spec["fields"])
    result.update(schema="gx.record-coverage/1", method=spec["method"], source=spec["source"],
                  cohort_sha256=hashlib.sha256(cohort).hexdigest(), evidence_sha256=hashlib.sha256(evidence).hexdigest())
    (args.out / "coverage.json").write_text(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
