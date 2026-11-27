"""Verify three completed live studies against public source data, without calling models."""
import argparse
from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
import re
from math import dist, log10, sqrt
from pathlib import Path
import sqlite3
from statistics import correlation, median
from xml.dom import minidom
from zipfile import ZipFile

from acceptance.science_capabilities.structure_controls.verify import read_pdb


ROOT = Path(__file__).resolve().parents[2]
CONTROL = Path(__file__).with_name("structure_controls")


def rows(path):
    with ZipFile(path) as archive:
        document = minidom.parseString(archive.read("word/document.xml"))
    table = document.getElementsByTagName("w:tbl")[0]
    result = []
    for row in table.getElementsByTagName("w:tr"):
        values = []
        for cell in row.getElementsByTagName("w:tc"):
            text = "".join(t.firstChild.nodeValue for t in cell.getElementsByTagName("w:t") if t.firstChild)
            values.append(text.strip())
        result.append(values)
    return result


def reported_value(cell):
    return float(cell.split("±")[0].replace(",", "").strip())


def midranks(values):
    return [1 + sum(x < value for x in values) + (values.count(value) - 1) / 2 for value in values]


def recalc(data):
    assay = rows(data / "S1-table.docx")
    conservation = rows(data / "S3-table.docx")
    wt = reported_value(assay[2][5])
    scores = {row[1]: -int(row[2]) for row in conservation[1:] if row[2].isdigit()}
    holdout = []
    counts = Counter()
    for variant, soluble_yield, _, _, _, efficiency in assay[3:]:
        if soluble_yield == "ND":
            status = "yield_unquantifiable"
        elif efficiency.startswith("<"):
            status = "left_censored"
        else:
            status = "quantified"
        counts[status] += 1
        key = "GX-BglB-S1-site-v1:" + variant[1:-1]
        if int(sha256(key.encode()).hexdigest(), 16) % 3 == 0:
            holdout.append((variant, reported_value(efficiency) if status == "quantified" else None))
    pairs = [(scores[tag], val) for tag, val in holdout if val is not None and tag in scores]
    scores, outcomes = zip(*pairs)
    return {
        "wt_efficiency_M_inv_min_inv": wt,
        "overall_efficiency_status": dict(counts),
        "holdout_count": len(holdout),
        "holdout_exact": sum(value is not None for _, value in holdout),
        "holdout_score_pairs": len(pairs),
        "holdout_spearman": correlation(midranks(scores), midranks(outcomes)),
        "paired_wt_median_absolute_log10_error": median(abs(log10(v/wt)) for v in outcomes),
        "constant_wt_spearman": None,
    }


def top7(outputs):
    live, = outputs["qc.json"]
    source = CONTROL/"materials/1QYS.pdb"
    sequence, models, _ = read_pdb(source)
    atoms = models[1]
    positions = {p for p, _ in atoms}
    ca = [xyz for (_, name), xyz in atoms.items() if name == "CA"]
    complete = [p for p in positions if all((p, a) in atoms for a in ("N", "CA", "C", "O"))]
    missing = sorted(set(range(1, len(sequence)+1)) - positions)
    radius = sqrt(sum(dist(a,b)**2 for a in ca for b in ca) / (2 * len(ca)**2))
    assert live["declared_residues"] == len(sequence) == 106
    assert live["observed_residues"] == live["ca_count"] == len(ca) == 92
    assert live["complete_backbones"] == len(complete) and live["missing_label_positions"] == missing
    error = abs(radius-live["ca_radius_of_gyration_A"])
    assert error < 1e-9
    return {"independent_material":"1QYS.pdb (separately archived legacy PDB, same deposition)",
            "source_sha256":sha256(source.read_bytes()).hexdigest(), "denominator":len(sequence),
            "observed_CAs":len(ca), "complete_backbones":len(complete), "missing_label_positions":missing,
            "ca_pairwise_radius_A":radius, "absolute_difference_A":error, "tolerance_A":1e-9,
            "scope":"Experimentally resolved 3–94 fragment. These data do not establish a whole-chain radius."}


def macrocycle(outputs):
    original, minus = sorted(outputs["qc.json"], key=lambda r:-r["declared_closure"])
    assert original["closure_link_ids"] == ["covale2"] and minus["closure_link_ids"] == []
    assert original["models"] == minus["models"]
    source = CONTROL/"materials/6BEN.pdb"
    _, models, links = read_pdb(source)
    assert {("A",8,"C"),("A",1,"N")} in links
    distances = [dist(v[8,"C"], v[1,"N"]) for _,v in sorted(models.items())]
    errors = [abs(row["distance_A"]-distances[row["model"]-1]) for row in original["models"]]
    assert len(models) == len(errors) == 20 and max(errors) < 1e-9
    raw = (CONTROL/"materials/6BEN.cif").read_bytes().splitlines(keepends=True)
    control = (CONTROL/"materials/6BEN-minus-closure.cif").read_bytes()
    assert sum(r.startswith(b"covale2 ") for r in raw) == 1
    assert b"".join(r for r in raw if not r.startswith(b"covale2 ")) == control
    saved = json.loads((CONTROL/"verification.json").read_bytes())["coordinate_methods"]
    assert saved["same_geometry_after_removal"] and saved["topology_models_tested"] == len(models)
    return {"independent_material":"6BEN.pdb; same deposition; solution NMR model-wise endpoint distances",
            "source_sha256":sha256(source.read_bytes()).hexdigest(),"models":len(models),
            "declared_link_original":1,"declared_link_control":0,"identical_coordinate_outputs":True,
            "single_removed_record":"covale2","distance_A_model_1":distances[0],"distance_range_A":[min(distances), max(distances)],
            "maximum_absolute_distance_difference_A":max(errors),"tolerance_A":1e-9,
            "scope":"Missing declaration in the deletion control supplies no physical evidence of an open molecule."}


def bglb(outputs):
    result, = outputs["summary.json"]
    source = ROOT/"examples/bglb-retrospective/data"
    ref = recalc(source)
    numerical = ("holdout_count","holdout_exact","holdout_score_pairs","holdout_spearman","paired_wt_median_absolute_log10_error")
    diffs = {k:abs(ref[k]-result[k]) for k in numerical}
    assert max(diffs.values()) < 1e-12 and result["wt_efficiency"]["value"] == ref["wt_efficiency_M_inv_min_inv"]
    assert result["constant_wt_spearman"] is None and ref["overall_efficiency_status"] == {"quantified":78,"left_censored":8,"yield_unquantifiable":14}
    assert result["overall_efficiency_status"] == {"quantified":78,"below_reported_bound":8,"no_quantifiable_soluble_yield":14}
    return {"independent_method":"Transcribe the originals with DOM. Rank by counting strictly smaller and tied observations. Apply Pearson correlation to midranks using statistics.correlation.",
            "sources":{name:sha256((source/name).read_bytes()).hexdigest() for name in ("S1-table.docx","S3-table.docx")},
            "independent_values":ref,"absolute_differences":diffs,"tolerance":1e-12,
            "scope":"Within-study evaluation on quantified, scored records. WT in S1 and body differs by 5000. Constant rank has zero variance."}


def report_coverage(check, report, rationale):
    # Each field is a specific reported measurement or limitation. Omissions here
    # concern the short Markdown export; the full decision is retained separately.
    patterns = {
        top7: {"coverage_92_106": r"92\s*/\s*106", "observed_interval_3_94": r"3[–—-]94",
               "fragment_radius_12_216_A": r"12\.216"},
        bglb: {"holdout_41": r"(?<!\d)41(?!\d)", "quantified_30": r"(?<!\d)30(?!\d)",
               "paired_14": r"(?<!\d)14(?!\d)", "spearman_0_2596": r"0\.2596",
               "wt_error_0_3882": r"0\.3882"},
        macrocycle: {"first_model_distance_1_335_A": r"1\.335", "20_models": r"(?<!\d)20(?!\d)", "removed_link_id": "covale2",
                     "paired_distances_agree": r"距离.{0,9}(相等|相同|一致)"}
    }[check]
    return {key: {"short_markdown": bool(re.search(pattern, report)), "full_decision": bool(re.search(pattern, rationale))}
            for key, pattern in patterns.items()}


def lead_geometry_receipt(state):
    """Find the Lead's executed source-coordinate comparison in the official session."""
    for session in (state.parent / "codex/sessions").rglob("*.jsonl"):
        selected = set()
        with session.open() as handle:
            for line in handle:
                record = json.loads(line)
                item = record.get("payload", {})
                if record.get("type") != "response_item":
                    continue
                code = item.get("input", "")
                if item.get("name") == "exec" and "Math.hypot" in code and "_atom_site" in code:
                    selected.add(item["call_id"])
                if item.get("call_id") not in selected or "output" not in item:
                    continue
                for part in item["output"]:
                    text = part.get("text", "").strip()
                    if not text.startswith("{"):
                        continue
                    result = json.loads(text)
                    if result.get("count") == 20 and result.get("allMatch") and result.get("onlyClosureRow"):
                        fields = ("count", "first", "min", "max", "origQCMatches", "controlQCMatches",
                                  "atomColumnsIdentical", "onlyClosureRow", "allMatch")
                        return {"official_call_id": item["call_id"], "observed": {k: result[k] for k in fields}}
    raise ValueError("Missing official Lead source-coordinate comparison receipt")


def inspect(state, check):
    result_file, = state.glob("research/*/result.json")
    result = json.loads(result_file.read_bytes())
    closure = result["closure"]
    assert result["goal_status"] == "CLOSED" and closure["committed_event"] < closure["closed_event"]
    outputs = {}
    jobs = []
    for job in result["jobs"]:
        assert job["status"] == "SUCCEEDED"
        published = []
        for item in job["outputs"]:
            filename = item["output_name"]
            if filename not in {"summary.json", "records.json", "qc.json"}:
                continue
            raw = (state/"artifacts"/result["run_id"]/"jobs"/job["job_id"]/"files"/filename).read_bytes()
            assert sha256(raw).hexdigest() == item["sha256"]
            outputs.setdefault(filename, []).append(json.loads(raw))
            published.append({key:item[key] for key in ("output_name","artifact_ref","sha256")})
        jobs.append({"id":job["job_id"],"method":job["method"]["id"],"inputs":job["inputs"],"outputs":published})
    decision = result["final_decision"]
    receipts = [{k:d[k] for k in ("event_id","delivery_kind","scope","turn_id","receipt")}
                for d in result["deliveries"] if d["scope"]=="run"]
    ref = check(outputs)
    if check is macrocycle:
        ref["lead_raw_coordinate_recalculation"] = lead_geometry_receipt(state)
        observed = ref["lead_raw_coordinate_recalculation"]["observed"]
        assert observed["origQCMatches"] and observed["controlQCMatches"]
        assert abs(observed["min"] - ref["distance_range_A"][0]) < ref["tolerance_A"]
        assert abs(observed["max"] - ref["distance_range_A"][1]) < ref["tolerance_A"]
    db_file = (state/"gx.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(db_file, uri=True)) as db:
        _, event_kind = db.execute("select event_id,kind from events where event_id=?", (closure["closed_event"],)).fetchone()
        assert event_kind == "v6.run.closed"
    citations = decision["used_endpoints"]
    receipt_ids = {r["event_id"] for r in receipts}
    assert citations and all(v["event_id"] in receipt_ids and v["event_id"] < closure["committed_event"] for v in citations)
    assert result["invocation"]["after"]["host_turns"] > 0
    report = result_file.with_name("REPORT.md").read_bytes()
    return {"run_id":result["run_id"],"official_host_turns":result["invocation"]["after"]["host_turns"],
            "jobs":jobs,"lead_delivery_receipts":receipts,"decision_ref":decision["decision_ref"],
            "committed_event":closure["committed_event"],"closed_event":closure["closed_event"],
            "registered_classifications":[p["classification"] for p in result["predictions"]],
            "reason":decision["rendered"]["reason"],"scientific_rationale":decision["rendered"]["rationale"],
            "readings_cited":[{"id":v["reading"]["metric_id"],"value":v["reading"]["display"],
                               "output_ref":v["source_ref"],"delivery_event":v["event_id"]} for v in decision["used_endpoints"]],
            "independent_control":ref,"result_sha256":sha256(result_file.read_bytes()).hexdigest(),
            "report_sha256":sha256(report).hexdigest(),
            "report_coverage":report_coverage(check, report.decode(), decision["rendered"]["rationale"])}


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("live_root",type=Path)
    cli.add_argument("output",type=Path)
    args=cli.parse_args()
    receipt={name:inspect(args.live_root/("live-"+name)/"state",f) for name,f in
             (("top7",top7),("macrocycle",macrocycle),("bglb",bglb))}
    args.output.write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+"\n")
