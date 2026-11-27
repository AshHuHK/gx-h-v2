"""Read-only joins of versioned yeast annotations, and comparison with current public records."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import subprocess

TAXID = 559292
SCOPE = "gx.yeast-enzyme-scope/1"
CANDIDATES = "gx.yeast-enzyme-candidates/1"
# ENZYME's invertase EC covers a class of fructofuranosides. It is not an exact Rhea match.
SUBSTRATES = {
    "trehalose": (32675, "3.2.1.28", "alpha,alpha-trehalose + H2O = alpha-D-glucose + beta-D-glucose"),
    "sucrose": (33795, "3.2.1.26", "sucrose + H2O = beta-D-fructose + alpha-D-glucose"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def check_scope(scope):
    if scope != {"schema": SCOPE, "taxid": TAXID}:
        raise ValueError("Supply a gx.yeast-enzyme-scope/1 input with taxid 559292 (S288C)")


def check_selection(document, accession):
    if document.get("schema") != CANDIDATES or document.get("taxid") != TAXID:
        raise ValueError("Use candidates.json from the S288C reaction lookup")
    if accession not in {item["accession"] for item in document["candidates"]}:
        raise ValueError("The selected accession must occur in the submitted candidates")


def open_snapshot(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True))


def source_metadata(db):
    values = dict(db.execute("SELECT key, value FROM meta"))
    if values["swissprot_release"] != "2026_03" or values["enzyme_release"] != "28-Jan-2026":
        raise ValueError("ENZYME or Swiss-Prot snapshot differs from the method version")
    return {"swissprot_release": values["swissprot_release"], "enzyme_release": values["enzyme_release"],
            "sources": {key: item["sha256"] for key, item in json.loads(values["source_files"]).items()}}


def run_lookup(scope, substrate, mount):
    check_scope(scope)
    if substrate not in SUBSTRATES:
        raise ValueError("Select trehalose or sucrose")
    master, ec, equation = SUBSTRATES[substrate]
    root = Path(mount["enzyme_assets"]) / "indexes"
    found = {}
    sequences = {}

    def candidate(accession, name, version, sequence):
        digest = sha256(sequence.encode("ascii")).hexdigest()
        record = found.setdefault(accession, {"accession": accession, "name": name, "sequence_version": version,
               "sequence_sha256": digest, "associations": [], "enzyme_annotation": [], "catalytic_blocks": {}})
        if (record["sequence_version"], record["sequence_sha256"]) != (version, digest):
            raise ValueError(f"Conflicting sequence for {accession} across snapshots")
        sequences[accession] = sequence
        return record

    with open_snapshot(root / "rhea142-sprot2026_03" / "lookup.sqlite") as db:
        rhea = source_metadata(db)
        release, = db.execute("SELECT value FROM meta WHERE key='rhea_release'").fetchone()
        if release != "142":
            raise ValueError("Expected Rhea release 142")
        rhea["rhea_release"] = release
        for accession, name, version, sequence, rid, direction, line in db.execute("""
            SELECT a.accession,p.name,p.sequence_version,p.sequence,a.rhea_id,a.direction,a.source_line
            FROM association a JOIN protein p USING(accession)
            WHERE a.master=? AND p.taxid=? ORDER BY a.accession,a.source_line""", (master, TAXID)):
            rec = candidate(accession, name, version, sequence)
            rec["associations"].append({"rhea_id": rid, "direction_code": direction,
                "direction": {"LR":"left_to_right", "RL":"right_to_left", "BI":"bidirectional"}.get(direction, "unknown"),
                "locator": f"rhea2uniprot_sprot.tsv:line:{line}"})
            for block, desc, activity, direction_evidence in db.execute("""
                SELECT block,description,activity_evidence,direction_evidence FROM evidence
                WHERE accession=? AND master=? ORDER BY block""", (accession, master)):
                loc = f"{accession}:CC:CATALYTIC ACTIVITY:{block}"
                rec["catalytic_blocks"][loc] = {"locator": loc, "claim": desc, "activity_evidence": activity,
                                                "direction_evidence": direction_evidence}
    with open_snapshot(root / "enzyme-food-20260128-sprot2026_03" / "lookup.sqlite") as db:
        enzyme = source_metadata(db)
        enzyme_name, class_scope = db.execute("SELECT name,reaction FROM enzyme WHERE ec=?", (ec,)).fetchone()
        for accession, name, version, sequence, line in db.execute("""
            SELECT p.accession,p.name,p.sequence_version,p.sequence,d.source_line
            FROM dr d JOIN alias a ON d.source_ac=a.alias JOIN protein p ON p.accession=a.accession
            WHERE d.ec=? AND p.taxid=? ORDER BY p.accession,d.source_line""",(ec, TAXID)):
            rec = candidate(accession, name, version, sequence)
            rec["enzyme_annotation"].append({"ec": ec, "locator": f"enzyme.dat:line:{line}"})
            for loc, claim, activity, direction in db.execute("""
                SELECT locator,claim,activity_evidence,direction_evidence FROM evidence
                WHERE ec=? AND accession=? AND source='Swiss-Prot_catalytic_block' ORDER BY locator""",(ec, accession)):
                rec["catalytic_blocks"].setdefault(loc, {"locator": loc, "claim": claim, "activity_evidence": activity,
                                                        "direction_evidence": direction})
    for rec in found.values():
        rec["catalytic_blocks"] = list(rec["catalytic_blocks"].values())
        experimental = {pmid for block in rec["catalytic_blocks"] for pmid in
            re.findall(r"ECO:0000269\|PubMed:(\d+)", block["activity_evidence"])}
        rec["experimental_paper_pointers"] = sorted(experimental)
        rec["evidence_level"] = "annotation_cites_experiment" if experimental else "annotation_only"
        rec["specificity"] = "exact_reaction_annotation" if rec["associations"] else "enzyme_class_only"
    return {"schema": CANDIDATES, "taxid": TAXID, "substrate": substrate, "retrieved_at": now(),
            "query": {"rhea_master": master, "rhea_equation": equation, "ec": ec, "ec_name": enzyme_name,
                      "class_scope": class_scope}, "snapshots": {"rhea_index": rhea, "enzyme_index": enzyme},
            "counts": {"rhea_rows": sum(len(r["associations"]) for r in found.values()),
                       "enzyme_rows": sum(len(r["enzyme_annotation"]) for r in found.values()),
                       "distinct_accessions": len(found), "unique_catalytic_blocks": sum(len(r["catalytic_blocks"]) for r in found.values())},
            "candidates": [found[key] for key in sorted(found)], "limitations":
            "Annotations can repeat the same Swiss-Prot catalytic block. PubMed identifiers are pointers for primary reading. UN does not resolve direction. A class hit does not establish activity on the named substrate."}, sequences


def homology(sequences, mount):
    base = Path(mount["enzyme_assets"]) / "homology/swissprot-2026_03-tax559292"
    source = json.loads((base / "source.json").read_text())
    if (source["release"], source["taxid"]) != ("2026_03", TAXID):
        raise ValueError("Unexpected BLAST database")
    fields = "qseqid sseqid pident qstart qend sstart send qlen slen evalue bitscore"
    command = [mount["blastp"], "-query", "-", "-db", str(base / "s288c"), "-num_threads", "1", "-evalue",
               "1e-5", "-max_hsps", "1", "-max_target_seqs", str(source["pool_count"]), "-outfmt", "6 " + fields]
    query = "".join(f">{accession}\n{sequence}\n" for accession, sequence in sequences.items())
    raw = subprocess.run(command, input=query, text=True, capture_output=True, check=True).stdout if sequences else ""
    by_subject = {}
    group_by_identity = {}
    for row in raw.splitlines():
        seed, subject, ident, qs, qe, ss, se, qlen, slen, ev, score = row.split("\t")
        # Legacy BLAST database uses plain accessions, but allow its original sp|...|... FASTA IDs.
        if subject.startswith("sp|"):
            subject = subject.split("|")[1]
        qcov = (abs(int(qe) - int(qs)) + 1) / int(qlen)
        scov = (abs(int(se) - int(ss)) + 1) / int(slen)
        if float(ident) < 30 or min(qcov, scov) < .7:
            continue
        by_subject.setdefault(subject, []).append({"seed": seed, "identity_pct": float(ident),
             "query_coverage": qcov, "subject_coverage": scov, "evalue": float(ev), "bitscore": float(score)})
    for subject, matches in by_subject.items():
        for hit in matches:
            seed = hit["seed"]
            if subject in sequences and subject != seed and hit["identity_pct"] >= 90:
                group_by_identity.setdefault(seed, set()).add(subject)
    families = []
    remaining = set(sequences)
    while remaining:
        representative = min(remaining)
        group = {representative} | (group_by_identity.get(representative, set()) & remaining)
        remaining -= group
        families.append(sorted(group))
    return {"schema":"gx.yeast-homology/1", "pool":source, "blastp":subprocess.check_output([mount["blastp"], "-version"], text=True).splitlines()[0],
            "query_accessions":sorted(sequences), "criteria":{"evalue_max":1e-5,"identity_min_pct":30,
            "min_query_and_subject_coverage":0.7,"seed_group_representative_identity_min_pct":90,
            "coverage":"single best HSP span / full length on each side"},
            "raw_hsps":len(raw.splitlines()),"distinct_passing_subjects":len(by_subject),
            "non_seed_subjects":sorted(set(by_subject) - set(sequences)), "seed_groups":families,
            "matches_by_subject":by_subject,
            "scope":"Sequence similarity identifies homologs, not measured substrate activity. Query self-hits are marked by accession."}, raw


def check_record(raw, accession):
    doc = json.loads(raw)
    if doc.get("primaryAccession") != accession or doc.get("organism", {}).get("taxonId") != TAXID:
        raise ValueError("Returned UniProt accession or strain differs from the lookup")
    if doc.get("entryType") != "UniProtKB reviewed (Swiss-Prot)":
        raise ValueError("Expected reviewed Swiss-Prot entry")
    audit = doc["entryAudit"]
    if not all(isinstance(audit.get(key), int) and audit[key] > 0 for key in ("entryVersion", "sequenceVersion")):
        raise ValueError("Missing UniProt record/sequence versions")
    return doc


def fetch(document, accession, retrieve):
    check_selection(document, accession)
    url = f"https://rest.uniprot.org/uniprotkb/{accession}.json"
    raw = retrieve(url)
    doc = check_record(raw, accession)
    version = str(doc["entryAudit"]["entryVersion"])
    digest = sha256(raw).hexdigest()
    sequence = doc["sequence"]["value"]
    sequence_digest = sha256(sequence.encode("ascii")).hexdigest()
    seed = next(c for c in document["candidates"] if c["accession"] == accession)
    descriptor = {"source":f"UniProtKB:{accession}/entry/{version}", "data":f"{url}#sha256={digest}", "sha256":digest,
                  "acquisition":None, "sample":None, "publication":None, "processing":None, "sampling":None,
                  "applicability":"Reviewed public database annotation for S. cerevisiae S288C; original UniProtKB JSON.",
                  "external_source":{"kind":"database_record","accession":accession,"version":version,"retrieved_at":now(),"url":url}}
    version_info = {"accession":accession, "entry_version":int(version),"sequence_version":doc["entryAudit"]["sequenceVersion"],
                    "sequence_sha256":sequence_digest,"sequence_length":len(sequence),"sequence_locator":"/sequence/value",
                    "matches_index_sequence":sequence_digest == seed["sequence_sha256"] and doc["entryAudit"]["sequenceVersion"] == seed["sequence_version"],
                    "entry_url":url, "source_sha256":digest, "taxid":TAXID,
                    "catalytic_block_locators":[f"/comments/{i}/reaction" for i,c in enumerate(doc.get("comments", [])) if c.get("commentType") == "CATALYTIC ACTIVITY"]}
    return raw, descriptor, version_info
