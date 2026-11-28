"""Rebuild the paired curatorial evidence from the named public archive and publisher XML."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
PMCID = "PMC3953731"
DOI = "10.1016/j.fob.2014.02.004"
ROW = "3.2.1.10/specific_activity/59"

def write(name, doc):
    (HERE / "packet" / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")

def build(archive, article):
    with archive.open("rb") as f:
        archive_sha = hashlib.file_digest(f, "sha256").hexdigest()
    reference = json.loads((HERE.parents[1] / "science_assets/enzyme/sources/reference.json").read_bytes())
    assert archive_sha == reference["snapshots"]["kinetics"]["sha256"]
    db = sqlite3.connect(archive.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        record, = db.execute("SELECT body FROM assays WHERE source='BRENDA' AND entry_id=?", (ROW,)).fetchone()
    finally:
        db.close()
    record = json.loads(record)
    item = record["original"]
    paper = record["publications"][item["references"][0]]
    protein = record["protein_records"][item["proteins"][0]]
    initial = {"record": "E1", "database": "BRENDA 2026.1", "locator": ROW,
               "linked_pubmed": paper["pmid"], "accessions": record["accessions"],
               "organism_field": protein["organism"], "parameter": record["parameter_field"],
               "assay_value_as_archived": item["value"], "assay_annotation": item["comment"],
               "strain_field": None, "units_on_this_export_row": None}
    initial["worksheet_under_review"] = {"record": "W1", "status": "curator's unverified interpretation",
        "column_label": "second independent specific-activity measurement for S288C",
        "number": 54, "source_supplied_by_curator": "Deng et al. (2014), Table 2, Ima3p"}
    raw = article.read_bytes()
    xml = ET.fromstring(raw)
    assert DOI in [e.text for e in xml.findall(".//article-id")]
    table = xml.find(".//table-wrap[@id='t0010']")
    rows = [[" ".join("".join(cell.itertext()).split()) for cell in row] for row in table.findall(".//tr")]
    column = rows[1].index("Ima3p")
    assert rows[0][-1] == "Specific activities (μmol/min/mg)"
    matches = {r[0]:r[column] for r in rows[2:] if r[0] in ("α-pNPG", "Isomaltose")}
    assert matches == {"α-pNPG":"3.9 (54)", "Isomaltose":"7.2 (100)"}
    foot = " ".join(" ".join("".join(node.itertext()).split()) for node in table.findall("table-wrap-foot"))
    assert "reference for each enzyme the velocity on isomaltose (set to 100)" in foot
    assert "pNPG that was assayed at 5 mM" in foot and "pH 7, 30 °C" in foot
    assert "CEN.PK113-5D" in "".join(xml.find(".//sec[@id='s0080']").itertext())
    late = {"record":"E2", "source":"Deng et al., FEBS Open Bio 4 (2014), 200–212",
            "doi":DOI, "pmcid":PMCID, "pmid":paper["pmid"],
            "table_locator":"Table 2 (JATS t0010), column Ima3p; rows α-pNPG and Isomaltose",
            "unit_in_heading":"μmol/min/mg", "verbatim_cells":matches,
            "table_note":{"brackets":"relative velocity in percent of that enzyme's isomaltose velocity",
                          "pH":7, "temperature_C":30, "pNPG_mM":5,
                          "summary":"mean of five experiments"},
            "strain_context":{"Introduction / s0005":"S288c is the genome discussed.",
                              "Experimental procedures / s0080":"The reported expression host is CEN.PK113-5D."}}
    write("initial.json", initial)
    write("late.json", late)
    write("sources.json", {"E1":{"sqlite_sha256":archive_sha,"table":"assays","source":"BRENDA","entry_id":ROW},
          "E2":{"url":"https://www.ebi.ac.uk/europepmc/webservices/rest/"+PMCID+"/fullTextXML",
                "article_sha256":hashlib.sha256(raw).hexdigest(),"locators":["t0010","s0005","s0080"]},
          "W1":"Counterfactual proposed worksheet entry authored for this evaluation. It is not a published result."})

if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("archive", type=Path)
    cli.add_argument("article", type=Path)
    args=cli.parse_args()
    build(args.archive, args.article)
