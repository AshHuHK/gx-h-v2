"""Build and calibrate a field-mapped analytical method from a cited text artifact."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy


def envelope(key, records):
    return records if key == "$" else {key: records}


def calibrate(engine, fields):
    left, right, value = (fields[key] for key in ("cohort_id", "evidence_id", "evidence_value"))
    population = envelope(fields["cohort_rows"], [{left: x} for x in ("A", "A", "B", "C", "D")])
    readings = envelope(fields["evidence_rows"], [
        {right: "A", value: 0}, {right: "A", value: 0}, {right: "B", value: None},
        {right: "D", value: "record 7"}, {right: "EXTERNAL", value: "record 8"}])
    actual = engine["measure"](population, readings, fields)
    expected = {"numerator": 2, "denominator": 4, "matched_without_value": ["B"], "unmatched_ids": ["C"],
                "outside_cohort_ids": ["EXTERNAL"], "covered_ids": ["A", "D"]}
    checked = {key: actual[key] for key in expected}
    if checked != expected:
        raise ValueError("Calibration disagrees with independent counted IDs")
    empty = envelope(fields["cohort_rows"], [])
    try:
        engine["measure"](empty, readings, fields)
    except ValueError as error:
        if "denominator" not in str(error):
            raise
        failure = str(error)
    else:
        raise ValueError("An empty denominator was silently accepted")
    return {"independent_hand_count": expected, "measured": checked, "empty_cohort_rejected": failure}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "engine", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    keys = ("cohort_rows", "cohort_id", "evidence_rows", "evidence_id", "evidence_value")
    for name in (*keys, "source_quote"):
        p.add_argument("--" + name.replace("_", "-"), required=True)
    args = p.parse_args()
    fields = {name: getattr(args, name) for name in keys}
    engine = runpy.run_path(str(args.engine))
    engine["validate_fields"](fields)
    raw = args.source.read_bytes()
    text = raw.decode("utf-8")
    quote = args.source_quote.strip()
    if not 12 <= len(quote) <= 240 or text.count(quote) != 1:
        raise ValueError("Cite one unique, exact 12–240 character passage from the supplied UTF-8 method source")
    offset = text.index(quote)
    source = {"sha256": hashlib.sha256(raw).hexdigest(), "quote": quote, "line": text.count("\n", 0, offset) + 1,
              "char_offset": offset}
    spec = {"schema": "gx.derived-record-method/1", "fields": fields, "source": source,
            "engine_sha256": hashlib.sha256(args.engine.read_bytes()).hexdigest(),
            "calibration": calibrate(engine, fields)}
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    spec["method"] = "record-coverage-" + digest[:24] + "@1"
    (args.out / "method-spec.json").write_text(json.dumps(spec, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
