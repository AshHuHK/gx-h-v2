"""Independently verify stored public yeast study artifacts and read/decision receipts. Read-only, offline."""
from argparse import ArgumentParser
import base64
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

# Examples selected for scientifically consequential links to methods, results and limitations.
ANCHORS = {
    "trehalose": {"PMC11540316": {"p18", "p26", "p50", "p55"}, "PMC11206128": {"p9.2"},
                  "PMC3953731": {"p240", "p272"}, "PMC7124472": {"p125", "p194", "p195"}},
    "sucrose": {"PMC11462787": {"p19", "p65"}, "PMC3153487": {"p66"},
                "PMC5021340": {"p81", "p82", "p362"}, "PMC10456111": {"p111", "p129"}},
}
TEXT_TAGS = {"p", "title", "article-title", "td", "th", "mixed-citation"}


def digest(raw):
    return sha256(raw).hexdigest()


def words(text):
    return " ".join(text.split())


def artifact_bytes(state, row):
    raw = (state / "artifacts" / row["namespace"] / row["rel_path"]).read_bytes()
    if digest(raw) != row["sha256"]:
        raise ValueError("Stored bytes differ from " + row["artifact_ref"])
    return raw


def table_check(article, pmcid):
    # Preserve the row/column mapping across repeated zeroes and similar strain names.
    tables = {"PMC3953731": ("t0010", {"Trehalose", "Isomaltose"}, 6),
              "PMC5021340": ("pone.0162838.t004", {"Gal 2%", "Gal 2% + Glu 2%"}, 2)}
    if pmcid not in tables:
        return None
    table_id, wanted, columns = tables[pmcid]
    wrap = article.find(f".//table-wrap[@id='{table_id}']")
    if wrap is None:
        raise ValueError(f"Missing expected table {table_id} in {pmcid}")
    rows = [[words("".join(c.itertext())) for c in row if c.tag in ("td", "th")]
            for row in wrap.iter("tr")]
    selected = [row[:columns] for row in rows if row and row[0] in wanted]
    if len(selected) != len(wanted):
        raise ValueError(f"Missing expected comparison in {pmcid}/{table_id}")
    return {"id": table_id, "headers": [r[:columns] for r in rows[:2]], "rows": selected}


def article_receipts(conn, src, raw, examples):
    article = ET.fromstring(raw)
    meta = article.find("front/article-meta")
    ids = {v.get("pub-id-type"): v.text for v in meta.findall("article-id")}
    pmcid = Path(src["title"]).stem
    deposited = {v if (v or "").startswith("PMC") else "PMC" + (v or "")
                 for key, v in ids.items() if key in {"pmc", "pmcid", "pmcaid"}}
    if deposited != {pmcid}:
        raise ValueError("Raw original does not match " + pmcid)
    original = words(" ".join("".join(p.itertext()) for p in article.iter()
                              if p.tag.rsplit("}", 1)[-1] in TEXT_TAGS))
    reads = conn.execute("""SELECT DISTINCT r.segment_id,r.text_sha256,s.text FROM source_reads r
        JOIN source_segments s USING(source_id,segment_id) WHERE source_id=?""", (src["source_id"],)).fetchall()
    matched = sum(words(p["text"]) in original and digest(p["text"].encode()) == p["text_sha256"] for p in reads)
    proofs = conn.execute("""SELECT e.evidence_id,e.anchored_segment_id seg,e.quote,e.review_status,
        rr.receipt_id,rr.text_sha256,d.scope,s.label,s.text FROM evidence e
        JOIN source_reads rr ON e.receipt_id=rr.receipt_id AND e.source_id=rr.source_id AND rr.segment_id=e.anchored_segment_id
        JOIN v6_decision_tasks d ON rr.task_id=d.task_id JOIN source_segments s ON
        s.source_id=e.source_id AND s.segment_id=e.anchored_segment_id
        WHERE e.source_id=? ORDER BY e.rowid""", (src["source_id"],)).fetchall()
    original_matches = segment_matches = 0
    receipts = []
    selected = set()
    for p in proofs:
        in_segment = words(p["quote"]) in words(p["text"])
        in_original = words(p["quote"]) in original
        segment_matches += in_segment
        original_matches += in_original
        if p["seg"] in examples.get(pmcid, ()) and p["seg"] not in selected:
            selected.add(p["seg"])
            receipts.append({"segment": p["seg"], "locator": p["label"], "read_scope": p["scope"],
                             "read_receipt": p["receipt_id"], "paragraph_sha256": p["text_sha256"],
                             "evidence": p["evidence_id"], "review_status": p["review_status"],
                             "quote_in_segment": in_segment, "quote_in_xml": in_original})
    return {"pmcid": pmcid, "doi": ids.get("doi"), "article_type": article.get("article-type"),
            "coverage": src["text_coverage"], "distinct_read_segments": len(reads),
            "read_segments_matched_to_xml": matched, "located_extractions": len(proofs),
            "extract_quotes_in_segment": segment_matches, "extract_quotes_in_xml": original_matches,
            "example_receipts": receipts}, table_check(article, pmcid)


def originals(conn, state, study):
    docs, checked_tables = [], {}
    rows = conn.execute("""SELECT s.source_id,s.title,s.kind,s.text_coverage,s.blob_sha256,s.provenance_json,
        a.artifact_ref,a.namespace,a.rel_path,a.sha256,a.job_id FROM sources s JOIN artifacts a
        ON a.artifact_ref=s.blob_artifact_ref WHERE a.origin='JOB_OUTPUT' AND s.kind IN ('jats','json') ORDER BY s.rowid""")
    for row in rows:
        raw = artifact_bytes(state, row)
        if digest(raw) != row["blob_sha256"]:
            raise ValueError("The registered source hash differs from the stored bytes")
        provenance = json.loads(row["provenance_json"])["external_source"]
        doc = {"source": row["title"], "url": provenance["url"], "job": row["job_id"],
               "ref": row["artifact_ref"], "sha256": row["sha256"]}
        if row["kind"] == "jats":
            extra, table = article_receipts(conn, row, raw, ANCHORS[study])
            doc.update(extra)
            if table:
                checked_tables[row["sha256"]] = table
        else:
            item = json.loads(raw)
            doc.update(taxon=item.get("organism", {}).get("taxonId"), accession=item.get("primaryAccession"),
                       gene=[g.get("geneName", {}).get("value") for g in item.get("genes", [])],
                       reviewed_entry_version=item.get("entryAudit", {}).get("entryVersion"))
            doc["alpha_trehalase_GO_evidence"] = [
                prop["value"] for db in item.get("uniProtKBCrossReferences", []) if db.get("id") == "GO:0004555"
                for prop in db.get("properties", []) if prop.get("key") == "GoEvidenceType"]
            doc["distinct_read_segments"] = conn.execute(
                "SELECT count(DISTINCT segment_id) FROM source_reads WHERE source_id=?", (row["source_id"],)
            ).fetchone()[0]
        docs.append(doc)
    return docs, checked_tables


def search_receipts(conn, state):
    queries = conn.execute("""SELECT j.job_id,j.status,n.spec_json FROM jobs j JOIN v6_nodes n USING(job_id)
        WHERE j.capability_id='scholarly-search' ORDER BY j.rowid""").fetchall()
    samples = []
    if queries:
        for i in sorted({0, len(queries) // 2, len(queries) - 1}):
            q = queries[i]
            doc = {"job": q["job_id"], "query": json.loads(q["spec_json"])["parameters"]["query"], "status": q["status"]}
            record = conn.execute("SELECT * FROM artifacts WHERE job_id=? AND kind='scholarly_candidates'",
                                  (q["job_id"],)).fetchone()
            if record:
                candidate = json.loads(artifact_bytes(state, record))
                doc.update(index_ref=record["artifact_ref"], returned=candidate["returned_before_dedup"],
                           unique=candidate["unique_count"], providers={s["provider"]: s["status"] for s in candidate["sources"]})
            samples.append(doc)
    return {"jobs": len(queries), "examples": samples}


def selected_index_only(conn, state, wanted):
    """Track selected catalogue hits. An abstract in an archived API payload is not a paragraph read."""
    seen = {}
    query = "SELECT * FROM artifacts WHERE kind='scholarly_snapshot' ORDER BY rowid"
    for a in conn.execute(query):
        snap = json.loads(artifact_bytes(state, a))
        for call in snap["calls"]:
            if call["provider"] != "europepmc" or "raw_b64" not in call:
                continue
            raw = base64.b64decode(call["raw_b64"], validate=True)
            if digest(raw) != call["sha256"]:
                raise ValueError("Search snapshot response changed")
            records = json.loads(raw)["resultList"]["result"]
            for row in records:
                pmid = row.get("pmid")
                if pmid in wanted and pmid not in seen:
                    seen[pmid] = {"pmid": pmid, "catalogue_title": row.get("title"), "doi": row.get("doi"),
                                  "catalogue_pmcid": row.get("pmcid"), "open_access": row.get("isOpenAccess"),
                                  "abstract_in_raw_provider_payload": bool(row.get("abstractText")),
                                  "search_job": a["job_id"], "snapshot_ref": a["artifact_ref"],
                                  "snapshot_sha256": a["sha256"], "raw_provider_sha256": call["sha256"]}
    return list(seen.values())


def verify(state, study):
    with closing(sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # Keep a consistent snapshot if the same Run is still advancing.
        one = conn.execute("SELECT r.run_id,r.status run_state,g.status goal_state FROM runs r JOIN v6_goals g USING(run_id)").fetchone()
        articles, tables = originals(conn, state, study)
        reports = []
        for m in conn.execute("""SELECT m.module_id,m.scope,m.status,rm.report_ref,a.namespace,a.rel_path,a.sha256,
                a.artifact_ref FROM v62_modules m LEFT JOIN v62_module_reports rm USING(module_instance_id)
                LEFT JOIN artifacts a ON a.artifact_ref=rm.report_ref ORDER BY m.rowid"""):
            summary = {k: m[k] for k in ("module_id", "scope", "status", "report_ref")}
            if m["report_ref"]:
                public = json.loads(artifact_bytes(state, m))
                summary.update(author=public["author"]["kind"], findings=len(public["findings"]),
                               cited_source_ref_count=len({ref for item in public["findings"] for ref in item["source_refs"]}))
                if summary["author"] == "GX":
                    summary["gaps"] = public["gaps"]
            reports.append(summary)
        decisions = []
        for d in conn.execute("""SELECT v.decision_ref,v.request_json FROM v6_decisions v
                JOIN v6_decision_tasks dt USING(task_id) WHERE dt.scope='run' ORDER BY v.rowid"""):
            decision = json.loads(d["request_json"])["decision"]
            ops = [op["op"] for op in decision["ops"]]
            if ops:
                decisions.append({"decision": d["decision_ref"], "ops": ops,
                                  "cited_module_reports": [m["report_ref"] for m in reports
                                                           if m["report_ref"] in decision.get("evidence_refs", [])]})
        enacted_close = None
        if one["goal_state"] == "CLOSED":
            closure = conn.execute("""SELECT e.event_id,e.ts,v.decision_ref,v.task_id,v.turn_id,v.request_json,
                v.receipt_json FROM v6_goals g JOIN v6_decisions v ON g.stop_decision_ref=v.decision_ref
                JOIN events e ON e.run_id=g.run_id AND e.kind='v6.run.closed'
                AND json_extract(e.payload_json,'$.decision_id')=g.stop_decision_ref
                AND json_extract(e.payload_json,'$.op_digest')=g.stop_op_digest""").fetchone()
            if closure is None:
                raise ValueError("Sealed study has no matching close event")
            receipt = json.loads(closure["receipt_json"])
            decision = json.loads(closure["request_json"])["decision"]
            operation = next(op["spec"] for op in receipt["resolved_ops"] if op["spec"]["op"] == "close_run")
            refs = receipt.get("evidence_refs", decision["evidence_refs"])
            enacted_close = {k: closure[k] for k in ("ts", "event_id", "decision_ref", "task_id", "turn_id")}
            enacted_close.update(claim_refs=operation["claim_refs"], evidence_refs=refs,
                                 cited_reports=[m["report_ref"] for m in reports if m["report_ref"] in refs])
        pi = []
        for m in conn.execute("""SELECT m.message_id,r.turn_id,r.status,r.response FROM p0_conversation_messages m
                                 LEFT JOIN p0_main_replies r USING(message_id) ORDER BY m.rowid"""):
            pi.append({"message_id": m["message_id"], "reply_status": m["status"], "turn": m["turn_id"],
                       "answer_sha256": digest(m["response"].encode()) if m["response"] else None})
        return {**dict(one), "as_of": dict(conn.execute("SELECT event_id,ts FROM events ORDER BY event_id DESC LIMIT 1").fetchone()),
                "provider_roots": dict(conn.execute("SELECT provider,count(*) FROM p0_roots GROUP BY provider")),
                "jobs": [dict(row) for row in conn.execute("""SELECT capability_id,status,count(*) count FROM jobs
                              GROUP BY capability_id,status ORDER BY capability_id,status""")],
                "search": search_receipts(conn, state),
                "selected_bibliographic_hits": selected_index_only(conn, state, {"7730323"} if study == "trehalose"
                                                                   else {"363706", "3902817"}),
                "originals": articles, "independent_tables_by_sha256": tables,
                "in_run_evidence_reviews": [dict(row) for row in conn.execute(
                    "SELECT independence_level,verdict,count(*) count FROM evidence_reviews GROUP BY 1,2 ORDER BY 1,2")],
                "module_reports": reports, "public_messages": pi, "adopted_lead_actions": decisions, "enacted_close": enacted_close}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, type=Path, help="directory containing live-trehalose and live-sucrose")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = {study: verify(args.runs / ("live-" + study) / "state", study) for study in ANCHORS}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
