"""Export reproducible accession and catalytic-evidence tables from the read-only food-enzyme archive."""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

PRODUCT = Path(__file__).resolve().parents[4]
PLUGINS = PRODUCT / "gx-h/plugins/installed"
DATABASE_MEMBER = "indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite"
CATALYTIC_SOURCE = "Swiss-Prot_catalytic_block"
EXPERIMENTAL_PMID = re.compile(r"(?<![\w:])ECO:0000269\|PubMed:([1-9]\d*)(?![\w:])")
CLAIM_EC = re.compile(r"(?:^|;\s*)EC=(\d+\.\d+\.\d+\.\d+)(?=\s*;|$)")


def defaults():
    mounts = json.loads((PLUGINS / "org.gx.yeast-enzyme-evidence/1.0.1/methods/mounts.json").read_bytes())
    pins = json.loads((PLUGINS / "org.gx.yeast-enzyme-archive/1.0.0/snapshots.json").read_bytes())
    return Path(mounts["enzyme_assets"]) / DATABASE_MEMBER, pins["enzyme"]


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def collect(database, expected_sha256, ec="3.2.1.58", taxid=559292):
    database = database.resolve(strict=True)
    with database.open("rb") as source:
        observed = hashlib.file_digest(source, "sha256").hexdigest()
    if observed != expected_sha256:
        raise ValueError(f"Archive digest differs: observed {observed}, expected {expected_sha256}")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        meta = dict(db.execute("SELECT key, value FROM meta"))
        enzyme = db.execute("SELECT name, reaction FROM enzyme WHERE ec=?", (ec,)).fetchone()
        if enzyme is None:
            raise ValueError(f"Missing EC {ec} in archive {observed}")
        members = db.execute("""
            SELECT p.accession, p.entry_name, p.organism, p.name, d.source_ac, d.source_line,
                   EXISTS(SELECT 1 FROM alias other WHERE other.alias=d.source_ac
                            AND other.accession!=p.accession) AS ambiguous_alias
            FROM dr d JOIN alias a ON a.alias=d.source_ac JOIN protein p USING(accession)
            WHERE d.ec=? AND p.taxid=? ORDER BY p.accession, d.source_line, d.source_ac
        """, (ec, taxid))
        records = {}
        for member in members:
            accession = member["accession"]
            record = records.setdefault(accession, {
                "accession": accession, "entry_name": member["entry_name"], "name": member["name"],
                "organism": member["organism"], "enzyme_annotation": [],
            })
            record["enzyme_annotation"].append({
                "source_accession": member["source_ac"], "locator": f"enzyme.dat:line:{member['source_line']}",
                "ambiguous_alias": bool(member["ambiguous_alias"]),
            })

        for accession, record in records.items():
            queried = db.execute("""
                SELECT locator, claim, activity_evidence FROM evidence
                WHERE ec=? AND accession=? AND source=?
                ORDER BY locator, claim, activity_evidence
            """, (ec, accession, CATALYTIC_SOURCE))
            locators = defaultdict(set)
            pmids = set()
            ambiguous = any(a["ambiguous_alias"] for a in record["enzyme_annotation"])
            blocks = []
            for row in queried:
                claim, activity = row["claim"] or "", row["activity_evidence"] or ""
                recorded_ec = set(CLAIM_EC.findall(claim))
                if recorded_ec != {ec}:
                    # The index key alone does not establish which activity the citation supports.
                    ambiguous = True
                locators[row["locator"]].add((claim, activity))
                pmids.update(EXPERIMENTAL_PMID.findall(activity))
                blocks.append({"source": CATALYTIC_SOURCE, "locator": row["locator"], "claim": claim,
                               "activity_evidence": activity, "claim_ec": sorted(recorded_ec)})
            ambiguous |= any(len(versions) > 1 for versions in locators.values())
            record["catalytic_blocks"] = blocks
            record["experimental_paper_pointers"] = [] if ambiguous else sorted(pmids)
            record["pointer_status"] = ("ambiguous_source" if ambiguous else
                                        "experimental_pointer" if pmids else "no_experimental_pointer")

    upstream = json.loads(meta["source_files"])
    candidates = {
        "schema": "gx.yeast-enzyme-candidates/1", "taxid": taxid, "query": {"ec": ec, "ec_name": enzyme["name"],
            "class_scope": enzyme["reaction"]}, "snapshots": {"enzyme_index": {
                "member": DATABASE_MEMBER, "sha256": observed, "enzyme_release": meta["enzyme_release"],
                "swissprot_release": meta["swissprot_release"],
                "sources": {k:v["sha256"] for k,v in sorted(upstream.items())}}},
        "candidates": list(records.values()),
        "limitations": "This panel covers the selected ENZYME class in this strain and release. A PMID links to a publication. Its experimental finding requires examination of the original paper.",
    }
    pointers = {
        "schema": "gx.archived-enzyme-paper-pointers/1",
        "source_sha256": hashlib.sha256(encode(candidates)).hexdigest(),
        "source_field": "candidates[].experimental_paper_pointers[]",
        "scope": "Separate experimental PubMed links in EC-matched catalytic activity evidence. A null retains a missing or ambiguous association. Distinct PMIDs on one accession remain distinct rows.",
        "pointers": [{"entry": r["accession"], "pmid": pmid, "status": r["pointer_status"]}
                     for r in records.values() for pmid in r["experimental_paper_pointers"] or [None]],
    }
    return candidates, pointers


def main():
    database, expected = defaults()
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--database", "--db", type=Path, default=database)
    cli.add_argument("--expected-sha256", default=expected)
    cli.add_argument("--ec", default="3.2.1.58")
    cli.add_argument("--taxid", type=int, default=559292)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    candidates, pointers = collect(args.database, args.expected_sha256, args.ec, args.taxid)
    args.out.mkdir(parents=True, exist_ok=True)
    for filename, value in (("candidates.json", candidates), ("pointers.json", pointers)):
        (args.out / filename).write_bytes(encode(value))


if __name__ == "__main__":
    main()
