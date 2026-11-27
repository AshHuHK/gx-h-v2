"""Retrospective audit of the fixed, published Carlin et al. BglB S1 and S3 tables."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median
import xml.etree.ElementTree as ET
from zipfile import ZipFile

METHOD = "bglb-retrospective@1"
TABLES = {
    "kinetics": "b580e7573ece03bf1dba2c0dc27238a12b1bf627eb1307f35a0cedffbc1b2a73",
    "conservation": "d08605c3f5370061b1cdfe5119e215e45338b4ff173f0c56ee244af964bdf0e9",
}
DOI = "10.1371/journal.pone.0147596"
WORD = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
SPLIT = "GX-BglB-S1-site-v1"
NUMBER = r"(?:\d[\d,]*(?:\.\d+)?)"


def table_rows(path, role):
    if hashlib.sha256(path.read_bytes()).hexdigest() != TABLES[role]:
        raise ValueError(f"Expected the original published {role} table identified by its digest")
    with ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
    table, = document.findall("./w:body/w:tbl", WORD)
    return [["".join(t.text or "" for t in cell.findall(".//w:t", WORD)).strip()
             for cell in row.findall("w:tc", WORD)] for row in table.findall("w:tr", WORD)]


def measurement(cell, missing):
    result = {"raw": cell, "value": None, "upper_bound": None, "uncertainty": None}
    if not cell or cell == "ND":
        return {**result, "status": missing}
    if cell.startswith("<"):
        return {**result, "status": "below_reported_bound", "upper_bound": float(cell[1:].strip())}
    match = re.fullmatch(rf"({NUMBER})(?:\s*±\s*(<\s*)?({NUMBER}))?", cell)
    if not match:
        raise ValueError(f"Unrecognized S1 numeric cell {cell!r}")
    uncertainty = None
    if match[3]:
        uncertainty = {"value": float(match[3].replace(",", "")), "relation": "<" if match[2] else "=",
                       "type": "reported_plus_minus"}
    return {**result, "status": "quantified", "value": float(match[1].replace(",", "")), "uncertainty": uncertainty}


def site_split(position):
    digest = hashlib.sha256(f"{SPLIT}:{position}".encode()).digest()
    return "holdout" if int.from_bytes(digest, "big") % 3 == 0 else "development"


def records(kinetics, conservation):
    if kinetics[:2] != [["Mutant", "Protein yield", "KM", "kcat", "KI", "kcat/KM"],
                        ["", "mg/mL", "mM", "min-1", "mM", "M-1min-1"]]:
        raise ValueError("Unexpected S1 headers or units")
    if conservation[0] != ["kcat/KM", "Mutant", "Conservation (%)"]:
        raise ValueError("Unexpected S3 headers")
    # S3 column 1 and its published row order encode outcomes. Neither enters scoring or splitting.
    scores = {row[1]: (int(row[2]), number) for number, row in enumerate(conservation[1:], 2)
              if row[1] != "BglB"}
    result = []
    for number, (label, yield_cell, km, kcat, ki, efficiency) in enumerate(kinetics[2:], 3):
        parent = label == "BglB"
        match = None if parent else re.fullmatch(r"([A-Z])(\d+)([A-Z])", label)
        if not parent and not match:
            raise ValueError(f"Unknown S1 identifier {label}")
        pos = None if parent else int(match[2])
        no_yield = yield_cell == "ND"
        missing = "no_quantifiable_soluble_yield" if no_yield else "not_reported"
        score = scores.get(label)
        result.append({
            "table_row": number, "variant": label, "parent": "BglB", "is_parent": parent,
            "position": pos, "reference_residue": None if parent else match[1],
            "substitution": None if parent else match[3], "partition": "reference" if parent else site_split(pos),
            "soluble_yield": measurement(yield_cell, "below_yield_detection"),
            "kcat_over_KM": measurement(efficiency, missing),
            # The authors used a slope-only fit where no saturation was evident.
            # Blank single-parameter cells do not identify the exact fit or a zero.
            "KM": measurement(km, "no_quantifiable_soluble_yield" if no_yield else "no_reported_fit"),
            "kcat": measurement(kcat, "no_quantifiable_soluble_yield" if no_yield else "no_reported_fit"),
            "KI": measurement(ki, "no_reported_fit" if not no_yield else missing),
            "published_conservation_percent": score[0] if score else None,
            "S3_row": score[1] if score else None,
        })
    return result


def ranks(values):
    ordered = sorted(range(len(values)), key=values.__getitem__)
    output = [0.] * len(values)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[ordered[start]] == values[ordered[end]]:
            end += 1
        for index in ordered[start:end]:
            output[index] = (start + end + 1) / 2
        start = end
    return output


def correlation(xs, ys):
    if len(xs) < 2:
        return None
    x, y = ranks(xs), ranks(ys)
    mid = (len(xs) + 1) / 2
    norm = math.sqrt(sum((a-mid)**2 for a in x) * sum((b-mid)**2 for b in y))
    return sum((a-mid)*(b-mid) for a, b in zip(x, y)) / norm if norm else None


def evaluate(rows):
    wt, = [r for r in rows if r["is_parent"]]
    mutants = [r for r in rows if not r["is_parent"]]
    held = [r for r in mutants if r["partition"] == "holdout"]
    exact = [r for r in held if r["kcat_over_KM"]["status"] == "quantified"]
    paired = [r for r in exact if r["published_conservation_percent"] is not None]
    # Fixed direction for retrospective testing: higher conservation predicts lower measured efficiency.
    observed = [r["kcat_over_KM"]["value"] for r in paired]
    rho = correlation([-r["published_conservation_percent"] for r in paired], observed)
    parent = wt["kcat_over_KM"]["value"]
    baseline_errors = [abs(math.log10(y/parent)) for y in observed]
    return {
        "schema": "gx.bglb-retrospective/1", "method": METHOD, "input_sha256": TABLES,
        "substrate": "pNPG", "endpoint": "kcat/KM", "unit": "M^-1 min^-1",
        "partition_rule": SPLIT, "mutant_count": len(mutants),
        "overall_efficiency_status": dict(Counter(r["kcat_over_KM"]["status"] for r in mutants)),
        "holdout_count": len(held), "holdout_exact": len(exact), "holdout_score_pairs": len(paired),
        "holdout_efficiency_status": dict(Counter(r["kcat_over_KM"]["status"] for r in held)),
        "holdout_absent_score_among_exact": len(exact)-len(paired),
        "holdout_spearman": rho, "constant_wt_spearman": None,
        "constant_wt_spearman_reason": "Every prediction is identical; rank variance is zero.",
        "paired_wt_median_absolute_log10_error": median(baseline_errors) if baseline_errors else None,
        "wt_efficiency": wt["kcat_over_KM"], "wt_source": "S1 Table, row 3, column 6",
        "published_disagreements": {"body_WT_efficiency": 171000, "body_WT_plus_minus": 8000,
            "body_soluble_mutants": 89, "body_below_efficiency_detection": 6,
            "basis": "S1 row data govern this reanalysis. Counts and the WT comparison do not reconcile with the body."},
        "interpretation": "S3 covers a selected subset, mostly alanine substitutions. The holdout is within a single public study, with no independent experimental validation. A rank describes only scored, quantified historical records. A constant WT assigns no ranks.",
        "record_source": DOI + ".s006", "score_source": DOI + ".s008",
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for arg in ("kinetics", "conservation", "out"):
        cli.add_argument("--" + arg, required=True, type=Path)
    args = cli.parse_args()
    rows = records(table_rows(args.kinetics, "kinetics"), table_rows(args.conservation, "conservation"))
    summary = evaluate(rows)
    detail = {"schema": "gx.bglb-published-records/1", "input_sha256": TABLES, "parent": "BglB",
              "source": DOI, "condition_source": DOI + ".s009", "substrate": "p-nitrophenyl-β-D-glucoside",
              "assay": {"pH": 7.5, "medium": "HEPES elution buffer (S1 Text)", "readout_nm": 420,
                        "temperature_C": None, "reported_technical_replicates": 3,
                        "WT_biological_replicates_reported_in_body": 10,
                        "mutant_biological_replicates_by_row": None,
                        "kinetic_detection_limit_M_inv_min_inv": 10, "yield_detection_limit_mg_mL": 0.1},
              "units": {"soluble_yield": "mg/mL", "KM": "mM", "kcat": "min^-1", "KI": "mM",
                        "kcat_over_KM": summary["unit"], "published_conservation_percent": "%"},
              "records": rows}
    args.out.mkdir(exist_ok=True, parents=True)
    for name, data in (("summary.json", summary), ("records.json", detail)):
        (args.out / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
