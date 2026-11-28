"""Independent read-only check of the four measured cells and the public archive entries."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ARCHIVE = HERE.parents[3] / "gx-h/var/science-assets/yeast-archive"


def read_rows(relative, query, params):
    with closing(sqlite3.connect((ARCHIVE/relative).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        return db.execute(query,params).fetchall()


def check(article):
    raw = article.read_bytes()
    reg = json.loads((HERE/"preregistered.json").read_text())
    sha = hashlib.sha256(raw).hexdigest()
    if sha != reg["article_sha256"]:
        raise ValueError("Expected the frozen original")
    root = ET.fromstring(raw)
    table = root.find(".//table-wrap[@id='t0010']/table")
    header = ["".join(cell.itertext()) for cell in table.findall("./thead/tr")[1]]
    row = ["".join(cell.itertext()) for cell in table.findall("./tbody/tr")[5]]
    if row[0] != reg["row"]["expected_substrate"]:
        raise ValueError("Published row label differs from the registered locator")
    exact = {r[0] for r in read_rows("indexes/rhea142-sprot2026_03/lookup.sqlite",
        "SELECT DISTINCT p.accession FROM protein p JOIN association a USING(accession) WHERE p.taxid=? AND a.master=?",
        (559292,32675))}
    by_ec = {r[0] for r in read_rows("indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite",
        "SELECT DISTINCT p.accession FROM protein p JOIN alias a USING(accession) JOIN dr d ON d.source_ac=a.alias "
        "WHERE p.taxid=? AND d.ec=?", (559292,"3.2.1.28"))}
    assays = [json.loads(r[0]) for r in read_rows("kinetics/brenda2026.1-sabio20260920-v1.sqlite",
        "SELECT body FROM assays WHERE ec=?", ("3.2.1.28",))]
    linked = [r for r in assays if set(r.get("accessions") or ()) & (exact|by_ec)]
    single = [r for r in linked if len(set(r.get("accessions") or ()))==1 and r.get("attribution")=="accession_linked_assay"]
    fields = sorted({r.get("parameter_field") for r in linked})
    return {"article_sha256":sha,"table":"Table 2 / t0010","locator":reg["row"]["locator"],"substrate":row[0],
            "specific_activity_units":"μmol/min/mg","published_native_columns":dict(zip(header[2:6],row[2:6])),
            "target_in_abstract":row[0].lower() in "".join(root.find(".//abstract").itertext()).lower(),
            "archive_strain_taxid":559292,"exact_reaction_id":"RHEA:32675","EC":"3.2.1.28",
            "reaction_linked_accessions":sorted(exact),"class_linked_accessions":sorted(by_ec),
            "all_organism_assay_rows_at_ec":len(assays),"assay_rows_for_named_accessions":len(linked),
            "single_accession_rows":len(single),"grouped_accession_rows":len(linked)-len(single),
            "linked_parameter_fields":fields,
            "linked_source_ids":[[r["source"],r["entry_id"]] for r in linked],
            "scope":"Published purified measurements and pinned archive snapshot. Absence of accession-linked rows cannot establish inactivity in vivo."}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])),indent=2,ensure_ascii=False))
