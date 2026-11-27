"""Independent archive audit of two published S288C sugar-enzyme record sets."""

import argparse
from collections import Counter
from contextlib import ExitStack, closing
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile


CASES = [
    ("maltose", 68796, "3.2.1.20"),
    ("isomaltose", 68864, "3.2.1.10"),
]
FILES = {
    "associations": "indexes/rhea142-sprot2026_03/lookup.sqlite",
    "classes": "indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite",
    "names": "indexes/reaction-names/rhea139-142.sqlite",
    "kinetics": "kinetics/brenda2026.1-sabio20260920-v1.sqlite",
    "features": "features/horizyn-selected/reference-v2.sqlite",
}


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def fasta_headers_and_hashes(path):
    entries = {}
    header = None
    seq = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.startswith(b">"):
                if header:
                    entries[header] = (seq.hexdigest(), length)
                header, sv = line[1:].decode().split()
                require(header not in entries and sv.startswith("SV="), "Invalid pool header")
                seq = hashlib.sha256()
                length = 0
            else:
                seq.update(line.strip())
                length += len(line.strip())
    if header:
        entries[header] = (seq.hexdigest(), length)
    return entries


def raw_enzyme_classes(path, accessions):
    wanted = {ec for _, _, ec in CASES}
    evidence = {ec: {} for ec in wanted}
    current = None
    with path.open() as handle:
        for line_no, line in enumerate(handle, 1):
            if line.startswith("ID   "):
                current = line[5:].strip()
            if current in wanted and line.startswith("DR   "):
                for pair in line[5:].strip().split(";"):
                    if pair.strip():
                        accession, name = [s.strip() for s in pair.split(",")]
                        if accession in accessions:
                            evidence[current][accession] = {"entry_name": name, "line": line_no}
    return evidence


def raw_rhea(path, pool):
    output = {m: {"protein_rows_any_species": 0, "pool_hits": [], "ec_mappings": []}
              for _, m, _ in CASES}
    with tarfile.open(path, "r:gz") as tar:
        for group, filename in (("associations", "rhea2uniprot_sprot.tsv"), ("ec", "rhea2ec.tsv")):
            with io.TextIOWrapper(tar.extractfile("tsv/" + filename)) as table:
                for row in csv.DictReader(table, delimiter="\t"):
                    rec = output.get(int(row["MASTER_ID"]))
                    if rec is None:
                        continue
                    if group == "associations":
                        rec["protein_rows_any_species"] += 1
                        if row["ID"] in pool:
                            rec["pool_hits"].append([row["ID"], int(row["RHEA_ID"]), row["DIRECTION"]])
                    else:
                        rec["ec_mappings"].append([row["ID"], int(row["RHEA_ID"]), row["DIRECTION"]])
    return output


def assay_coverage(db, ec, accession_set, substrate):
    totals = Counter()
    classes = Counter()
    compound_mentions = Counter()
    catalytic_substrates = Counter()
    exact_rows = []
    pmids = set()
    for source, eid, blob in db.execute("SELECT source,entry_id,body FROM assays WHERE ec=? ORDER BY source,entry_id", (ec,)):
        totals[source] += 1
        body = json.loads(blob)
        matching = accession_set.intersection(body.get("accessions") or [])
        if not matching:
            continue
        classes[source + "/" + body["attribution"]] += 1
        for doc in body.get("publications", {}).values():
            if doc.get("pmid"):
                pmids.add(str(doc["pmid"]))
        if source == "BRENDA":
            matched = re.search(r"\{([^{}]+)\}\s*$", body["original"].get("value", ""))
            if matched and matched.group(1).casefold() == substrate:
                compound_mentions[body["attribution"]] += 1
                is_catalytic = body["parameter_field"] in ("km_value", "turnover_number", "kcat_km_value")
                if is_catalytic:
                    catalytic_substrates[body["attribution"]] += 1
                exact_rows.append({"entry_id": eid, "parameter": body["parameter_field"],
                                   "catalytic_substrate_endpoint": is_catalytic,
                                   "assigned_accessions": sorted(body["accessions"]),
                                   "attribution": body["attribution"]})
    return {"all_species_ec_rows": dict(totals),
            "distinct_rows_any_member": sum(classes.values()), "rows_by_source_and_attribution": dict(classes),
            "pmids_in_linked_brenda_rows": sorted(pmids),
            "brenda_rows_with_exact_compound_label": dict(compound_mentions),
            "brenda_catalytic_rows_with_exact_substrate": dict(catalytic_substrates),
            "exact_brenda_archive_locators": exact_rows}


def verify_blast(program, prefix, pool):
    version = subprocess.check_output([str(program), "-version"], text=True).splitlines()[0]
    listing = subprocess.check_output([str(program), "-db", str(prefix), "-entry", "all", "-outfmt", "%a %l"],
                                      text=True)
    members = {}
    for line in listing.splitlines():
        accession, length = line.split()
        require(accession not in members, "Repeated identifier in BLAST database")
        members[accession] = int(length)
    require(members == {ac: n for ac, (_, n) in pool.items()}, "FASTA and BLAST identifiers or lengths differ")
    return {"program_version": version, "matching_ids_and_lengths": len(members), "residue_count": sum(members.values())}


def verify(root, rhea_tar, enzyme_dat, blastdbcmd):
    pool_dir = root / "homology/swissprot-2026_03-tax559292"
    pool = fasta_headers_and_hashes(pool_dir / "pool.fasta")
    pool_meta = json.loads((pool_dir / "source.json").read_bytes())
    blast = json.loads((pool_dir / "s288c.pjs").read_bytes())
    require(sha(pool_dir / "pool.fasta") == pool_meta["pool_sha256"], "FASTA hash changed")
    require(len(pool) == pool_meta["pool_count"] == blast["number-of-sequences"], "Pool record totals disagree")
    require(sum(n for _, n in pool.values()) == blast["number-of-letters"], "Pool letter totals disagree")
    require(all((pool_dir / n).is_file() for n in blast["files"]), "BLAST component missing")
    output = {"schema": "gx.s288c_public_sugar_audit/2", "root": "S288C_ENZYME_ARCHIVE",
              "mode": "read-only independent lookup; archived protein references only", "snapshots": {},
              "cases": {}, "fasta": {k: pool_meta[k] for k in ("pool_id", "pool_sha256", "taxid", "release")},
              "blast": {k: blast[k] for k in ("dbtype", "db-version", "number-of-sequences", "number-of-letters", "files")}}
    if blastdbcmd is not None:
        output["blast"]["actual_database_read"] = verify_blast(blastdbcmd, pool_dir / "s288c", pool)
    with ExitStack() as stack:
        handles = {}
        for name, filename in FILES.items():
            path = (root / filename).resolve()
            db = stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)))
            handles[name] = db
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            output["snapshots"][name] = {"file": filename, "sha256": sha(path),
                                         "tables": {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}}
        assoc = handles["associations"]
        classes = handles["classes"]
        meta = dict(assoc.execute("SELECT key,value FROM meta"))
        ec_meta = dict(classes.execute("SELECT key,value FROM meta"))
        require(meta["swissprot_release"] == ec_meta["swissprot_release"] == pool_meta["release"], "Swiss-Prot releases differ")
        output["releases"] = {k: meta[k] for k in ("swissprot_release", "rhea_release", "enzyme_release")}
        output["releases"]["kinetics_sources"] = json.loads(handles["kinetics"].execute("SELECT metadata FROM snapshot").fetchone()[0])["sources"]
        prov = json.loads(dict(handles["features"].execute("SELECT key,value FROM metadata"))["provenance"])
        output["snapshots"]["features"]["identity"] = {k: prov.get(k) for k in ("encoder", "encoder_revision", "checkpoint_sha256")}
        source = json.loads(meta["source_files"])
        rhea_tar = rhea_tar or Path(source["rhea"]["path"])
        enzyme_dat = enzyme_dat or Path(source["enzyme"]["path"])
        require(sha(rhea_tar) == source["rhea"]["sha256"], "Raw Rhea export hash changed")
        require(sha(enzyme_dat) == source["enzyme"]["sha256"], "Raw ENZYME export hash changed")
        output["flat_sources"] = {k: source[k]["sha256"] for k in ("enzyme", "rhea")}
        original_rhea = raw_rhea(rhea_tar, pool)
        original_ec = raw_enzyme_classes(enzyme_dat, pool)
        for substrate, master, ec in CASES:
            raw = original_rhea[master]
            db_pool = [[a, rid, d] for a, rid, d in assoc.execute(
                "SELECT accession,rhea_id,direction FROM association WHERE master=?", (master,)) if a in pool]
            require(sorted(db_pool) == sorted(raw["pool_hits"]), "Original Rhea and compiled index disagree")
            mapped = sorted([row[0], row[1], row[2]] for row in assoc.execute(
                "SELECT ec,rhea_id,direction FROM reaction_ec WHERE master=?", (master,)))
            require(mapped == sorted(raw["ec_mappings"]), "Rhea EC mapping differs")
            records = {}
            # EC DR links are selected against the taxon-specific FASTA independently of database taxonomy rows.
            for ac, rec in sorted(original_ec[ec].items()):
                tax, seq, sv = classes.execute(
                    "SELECT taxid,sequence,sequence_version FROM protein WHERE accession=?", (ac,)).fetchone()
                require(tax == 559292 and pool[ac][0] == hashlib.sha256(seq.encode()).hexdigest(), "Archived parents differ")
                records[ac] = {**rec, "sequence_sha256": pool[ac][0], "residues": pool[ac][1], "sequence_version": sv}
            expected = {r[0] for r in classes.execute(
                "SELECT DISTINCT e.accession FROM evidence e JOIN protein p USING(accession) WHERE e.ec=? AND p.taxid=559292",
                (ec,))}
            require(expected == set(records), "EC source member set differs")
            definition, = handles["names"].execute("SELECT definition FROM reaction WHERE master=?", (master,)).fetchone()
            output["cases"][substrate] = {"rhea_master": master, "reaction_definition": definition,
                "direction_ids": dict(assoc.execute("SELECT direction,rhea_id FROM direction WHERE master=?", (master,))),
                "raw_rhea": raw, "checked_ec_separately": ec, "ec_taxon_members": records,
                "assays": assay_coverage(handles["kinetics"], ec, set(records), substrate)}
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--rhea-tsv", type=Path, help="Same pinned upstream release at a relocated path")
    parser.add_argument("--enzyme-dat", type=Path, help="Same pinned upstream EC catalogue at a relocated path")
    parser.add_argument("--blastdbcmd", type=Path, help="Verify every BLAST accession and length against the FASTA")
    args = parser.parse_args()
    print(json.dumps(verify(args.asset_root, args.rhea_tsv, args.enzyme_dat, args.blastdbcmd), indent=2, ensure_ascii=False))
