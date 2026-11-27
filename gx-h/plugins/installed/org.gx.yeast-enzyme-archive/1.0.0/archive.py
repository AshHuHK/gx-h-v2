"""Query frozen S288C annotation and assay archives. A row is an archival assertion."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

TAXID = 559292
QUERY = "gx.yeast-archive-query/1"
RECORDS = "gx.yeast-archive-parents/1"
PANEL = {
    "maltose": (68796, "3.2.1.20", ("麥芽糖", "麦芽糖", "d-maltose")),
    "isomaltose": (68864, "3.2.1.10", ("異麥芽糖", "异麦芽糖")),
    "trehalose": (32675, "3.2.1.28", ("海藻糖", "alpha,alpha-trehalose")),
    "lactose": (10076, "3.2.1.23", ("乳糖",)),
}
FILES = {
    "reactions": "indexes/reaction-names/rhea139-142.sqlite",
    "rhea": "indexes/rhea142-sprot2026_03/lookup.sqlite",
    "enzyme": "indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite",
    "kinetics": "kinetics/brenda2026.1-sabio20260920-v1.sqlite",
}


def canonical_query(document):
    if (not isinstance(document, dict) or set(document) != {"schema", "taxid", "substrate"}
            or document["schema"] != QUERY or document["taxid"] != TAXID):
        raise ValueError("Provide one gx.yeast-archive-query/1 with taxid 559292 and substrate.")
    value = document["substrate"]
    if isinstance(value, str):
        for name, (_, _, aliases) in PANEL.items():
            if value.strip().casefold() in (name, *aliases):
                return {"schema": QUERY, "taxid": TAXID, "substrate": name}
    raise ValueError("Outside the archived S288C substrate panel: " + ", ".join(PANEL))


def read_db(root, key):
    path = (root / FILES[key]).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return closing(db)


def sha_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_snapshots(root, pins):
    for key, filename in FILES.items():
        if sha_file(root / filename) != pins[key]:
            raise ValueError(f"{key} snapshot contents differ from the registered archive version")


def annotation(rows, parents, bucket):
    for row in rows:
        accession = row["accession"]
        metadata = {"accession": accession, "name": row["name"], "sequence_version": row["sequence_version"],
                    "sequence_sha256": hashlib.sha256(row["sequence"].encode("ascii")).hexdigest()}
        if accession not in parents:
            parents[accession] = {**metadata, "reaction_links": [], "ec_links": []}
        elif any(parents[accession][k] != metadata[k] for k in ("sequence_version", "sequence_sha256")):
            raise ValueError(f"Conflicting sequence identity across archives for {accession}")
        keys = ("rhea_id", "direction", "source_line") if bucket == "reaction_links" else ("ec", "source_ac", "source_line")
        parents[accession][bucket].append({key: row[key] for key in keys})


def assay_evidence(root, ec, names, doc):
    """Count distinct entries; a grouped row remains shared even with one in-panel accession."""
    terms = {doc["substrate"], *PANEL[doc["substrate"]][2]}
    by_accession = defaultdict(Counter)
    classified = Counter()
    archive_rows = Counter()
    examples = {}
    with read_db(root, "kinetics") as db:
        meta = json.loads(db.execute("SELECT metadata FROM snapshot").fetchone()[0])
        for row in db.execute("SELECT source,entry_id,body FROM assays WHERE ec=? ORDER BY source,entry_id", (ec,)):
            source = row["source"]
            archive_rows[source] += 1
            body = json.loads(row["body"])
            listed = set(body.get("accessions") or [])
            matched = sorted(listed & names)
            if not matched:
                continue
            grouped = len(listed) != 1 or body.get("attribution") != "accession_linked_assay"
            attribution = "grouped_accession_rows" if grouped else "single_accession_rows"
            classified[attribution] += 1
            classified[source] += 1
            original = body.get("original") or {}
            # Braced compounds in inhibition or pH records have a different meaning.
            field = body.get("parameter_field")
            ligand = re.search(r"\{([^{}]+)\}", str(original.get("value", ""))) if field in {
                "km_value", "turnover_number", "kcat_km_value"} else None
            term = ligand.group(1).strip().casefold() if ligand else None
            scope = ("target_substrate_rows" if term in terms else "other_named_substrate_rows") if term else "unresolved_substrate_rows"
            classified[scope] += 1
            classified["single_target_rows" if not grouped else "grouped_target_rows"] += int(scope == "target_substrate_rows")
            for accession in matched:
                by_accession[accession][attribution] += 1
                by_accession[accession][scope] += 1
            key = (scope, attribution)
            if key not in examples:
                comment = str(original.get("comment") or "")
                conditions = {}
                for k, pattern in (("pH", r"\bpH\s*(\d+(?:\.\d+)?)\b"), ("temperature_C", r"(\d+(?:\.\d+)?)\s*°\s*C")):
                    m = re.search(pattern, comment, re.IGNORECASE)
                    if m:
                        conditions[k] = float(m.group(1))
                examples[key] = {"source": source, "entry_id": row["entry_id"], "attribution": attribution,
                                 "accessions": matched, "substrate_scope": scope, "compound_in_value": term,
                                 "parameter_field": field, "reported_conditions": conditions}
    totals = {key: classified[key] for key in ("single_accession_rows", "grouped_accession_rows", "single_target_rows",
        "grouped_target_rows", "other_named_substrate_rows", "unresolved_substrate_rows", "target_substrate_rows")}
    return ({"snapshot": meta["id"], "ec_indexed_rows_all_organisms": dict(archive_rows),
             "linked_unique_rows_by_source": {key: classified[key] for key in ("BRENDA", "SABIO-RK")},
             **totals, "examples": list(examples.values()),
             "scope": "Reference by accession. Group records are counted once per entry in the union. "
                      "Target match requires an exact named compound on a substrate-dependent field. "
                      "Strain, preparation and activity are not confirmed by accession alone."}, by_accession)


def lookup(document, root):
    doc = canonical_query(document)
    pins = json.loads(Path(__file__).with_name("snapshots.json").read_text())
    verify_snapshots(root, pins)
    return _lookup(doc, root, pins)


def _lookup(doc, root, pins):
    master, context_ec, _ = PANEL[doc["substrate"]]
    parents = {}
    with read_db(root, "reactions") as db:
        row = db.execute("SELECT definition,ec FROM reaction WHERE master=?", (master,)).fetchone()
        exact = {"master": master, "equation": row["definition"], "listed_ec": json.loads(row["ec"])}
    with read_db(root, "rhea") as db:
        md = dict(db.execute("SELECT key,value FROM meta"))
        if md["rhea_release"] != "142" or md["swissprot_release"] != "2026_03":
            raise ValueError("Unexpected Rhea/Swiss-Prot release")
        rows = db.execute("""SELECT p.*,a.rhea_id,a.direction,a.source_line FROM association a
            JOIN protein p USING(accession) WHERE a.master=? AND p.taxid=? ORDER BY p.accession,a.source_line""", (master, TAXID))
        annotation(rows, parents, "reaction_links")
    with read_db(root, "enzyme") as db:
        md = dict(db.execute("SELECT key,value FROM meta"))
        if md["enzyme_release"] != "28-Jan-2026" or md["swissprot_release"] != "2026_03":
            raise ValueError("Unexpected ENZYME/Swiss-Prot release")
        title = db.execute("SELECT name FROM enzyme WHERE ec=?", (context_ec,)).fetchone()[0]
        rows = db.execute("""SELECT p.*,d.ec,d.source_ac,d.source_line FROM dr d JOIN alias a ON a.alias=d.source_ac
            JOIN protein p USING(accession) WHERE d.ec=? AND p.taxid=? ORDER BY p.accession,d.source_line""", (context_ec, TAXID))
        annotation(rows, parents, "ec_links")
    assays, counts = assay_evidence(root, context_ec, set(parents), doc)
    for accession, item in parents.items():
        item["assay_row_counts"] = dict(counts[accession])
    return {"schema": RECORDS, "query": doc, "source_digests": pins,
            "releases": {"Rhea":"142","Swiss-Prot":"2026_03","ENZYME":"28-Jan-2026","assays":assays["snapshot"]},
            "exact_reaction": exact, "context_ec": context_ec, "context_ec_name": title,
            "counts": {"distinct_accessions":len(parents), "exact_accessions":sum(bool(v["reaction_links"]) for v in parents.values()),
                       "class_accessions":sum(bool(v["ec_links"]) for v in parents.values())},
            "parents": [parents[k] for k in sorted(parents)], "assay_coverage": assays,
            "limitations": "EC is a separate class-level search even when the reaction lists that EC. "
                           "UN direction is unspecified. Missing links in these snapshots do not establish absent activity."}


def compare(query, prior, root):
    if not isinstance(prior, dict) or prior.get("schema") != RECORDS:
        raise ValueError("Supply the archived parent-records.json alongside the new query")
    current = lookup(query, root)
    verified = _lookup(canonical_query(prior["query"]), root, current["source_digests"])
    if verified != prior:
        raise ValueError("Transferred record content differs from the pinned source snapshot")
    old_ids = {r["accession"] for r in prior["parents"]}
    new_ids = {r["accession"] for r in current["parents"]}
    return {"schema":"gx.yeast-archive-comparison/1", "prior_query":prior["query"], "new_query":current["query"],
            "prior_parents":[{k:p[k] for k in ("accession","sequence_version","sequence_sha256")} for p in prior["parents"]],
            "prior_counts":prior["counts"], "prior_assays":prior["assay_coverage"], "current":current,
            "new_only_accessions":sorted(new_ids-old_ids), "shared_accessions":sorted(new_ids & old_ids),
            "interpretation":"Archive coverage under separate reaction and EC definitions. Distinct entries can describe one experiment."}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=("lookup","compare"))
    cli.add_argument("mount_member", type=Path)
    cli.add_argument("query", type=Path)
    cli.add_argument("out", type=Path)
    cli.add_argument("prior", nargs="?", type=Path)
    args = cli.parse_args()
    root = args.mount_member.parents[2]
    scope = json.loads(args.query.read_bytes())
    if args.action == "lookup":
        name, answer = "parent-records.json", lookup(scope, root)
    else:
        name, answer = "comparison.json", compare(scope, json.loads(args.prior.read_bytes()), root)
    (args.out / name).write_text(json.dumps(answer, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
