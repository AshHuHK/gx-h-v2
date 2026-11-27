"""Join public search windows, verified JATS transfers and received original paragraphs at report time."""
from collections import defaultdict
import json

from gx_core.artifacts import ArtifactService
from gx_core.errors import GxError
from gx_core.p0 import manifest


def _aliases(paper):
    aliases = []
    for field, values in paper["identifiers"].items():
        if field in ("doi", "pmcid", "pmid"):
            for value in values:
                if value:
                    aliases.append((field, value.casefold()))
    for row in paper["records"]:
        ids = row.get("identifiers", {})
        if ids.get("source") and ids.get("id"):
            aliases.append(("source:" + ids["source"].casefold(), str(ids["id"])))
    return aliases


def _keys(pages):
    # A later window or another source can bridge a DOI-only and a PMCID-only record.
    parent = {}

    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for page in pages:
        for paper in page["candidates"]:
            aliases = _aliases(paper)
            if aliases:
                first = root(aliases[0])
                for alias in aliases[1:]:
                    parent[root(alias)] = first
    return {key: root(key) for key in parent}


def summarize(pages, originals):
    keys = _keys(pages)
    linked = defaultdict(list)
    selected_from = defaultdict(list)
    for original in originals:
        accession = ("pmcid", original["accession"].casefold())
        if accession in keys:
            linked[keys[accession]].append(original)
        for ref in original["input_candidates"]:
            selected_from[ref].append(original["job_id"])

    def access(keys_seen, opened, abstracts):
        full = keys_seen.intersection(linked)
        copies = [o for key in full for o in linked[key]]
        locations = {(copy["sha256"], segment) for copy in copies for segment in copy["segment_ids"]}
        return {
            "count": len(keys_seen),
            "marked_open_access": len(opened),
            "verified_full_text": len(full),
            "abstract_only": len(abstracts - full),
            "unknown_or_metadata_only": len(keys_seen - full - abstracts),
            "located_full_text": sum(any(o["read_receipts"] for o in linked[key]) for key in full),
            "located_read_receipts": sum(copy["read_receipts"] for copy in copies),
            "distinct_original_locations": len(locations),
        }

    all_keys, all_open, all_abstract = set(), set(), set()
    views = []
    for page in pages:
        by_source = defaultdict(lambda: (set(), set(), set()))
        for paper in page["candidates"]:
            aliases = _aliases(paper)
            if not aliases:
                continue
            key = keys[aliases[0]]
            all_keys.add(key)
            for row in paper["records"]:
                seen, opened, abstracts = by_source[row["provider"]]
                seen.add(key)
                if row.get("marked_open_access") or row.get("open_jats_candidate"):
                    opened.add(key)
                    all_open.add(key)
                if row.get("abstract_returned"):
                    abstracts.add(key)
                    all_abstract.add(key)
        windows = [{**source, "scientific_access": access(*by_source[source["provider"]])}
                   for source in page["sources"]]
        views.append({
            "job_id": page["job_id"],
            "candidates_ref": page["artifact_ref"],
            "query": page["query"],
            "mode": page["source_mode"],
            "sources": windows,
            "merged_page_history": page.get("merged"),
            "original_job_ids_from_this_output": selected_from[page["artifact_ref"]],
        })
    return {
        "schema": "gx.publication-coverage/1",
        "windows": views,
        "originals": originals,
        "unique_publications": access(all_keys, all_open, all_abstract),
        "live_search_jobs": sum(p["source_mode"] == "live_search" for p in pages),
        "replay_jobs": sum(p["source_mode"] == "snapshot_replay" for p in pages),
        "interpretation": (
            "Window counts describe the listing. scientific_access matches its papers to verified transfers "
            "and received paragraphs anywhere in this Run. original_job_ids_from_this_output lists transfers "
            "made using this particular candidate output. Stable identifiers link representations across "
            "queries and providers. Older windows without abstract metadata contribute to unknown coverage."
        ),
    }


def report(conn, run_id):
    candidates = conn.execute("""SELECT a.artifact_ref,a.job_id,a.sha256
        FROM artifacts a JOIN jobs j ON j.job_id=a.job_id AND j.run_id=a.run_id
        WHERE a.run_id=? AND a.kind='scholarly_candidates' AND a.origin='JOB_OUTPUT'
        AND a.published=1 AND j.status='SUCCEEDED' ORDER BY a.rowid""", (run_id,)).fetchall()
    if not candidates:
        return None
    assets = ArtifactService(conn)
    pages, gaps = [], []
    for ref, job, digest in candidates:
        try:
            doc = json.loads(assets.read_bytes(ref, expected_sha256=digest))
            if doc["schema"] != "gx.scholarly-candidates/1":
                raise ValueError("Unrecognized scholarly candidate schema")
            # Only parse the report's public projection. Other domain plugins can publish this file kind.
            page = {key: doc[key] for key in ("query", "sources", "candidates", "source_mode")}
            for source in page["sources"]:
                if not isinstance(source["provider"], str):
                    raise ValueError("Missing provider name")
            for paper in page["candidates"]:
                _aliases(paper)
                if any(not isinstance(row["provider"], str) for row in paper["records"]):
                    raise ValueError("Missing candidate provider")
            pages.append({**page, "merged": doc.get("merged"), "job_id": job, "artifact_ref": ref})
        except (OSError, GxError, ValueError, KeyError, TypeError, AttributeError) as error:
            gaps.append({"job_id": job, "artifact_ref": ref, "reason": str(error)})
    originals = []
    received = defaultdict(list)
    for item in conn.execute("""SELECT r.source_id,r.segment_id FROM p0_roots root
        JOIN p0_root_calls c ON c.thread_id=root.thread_id
        JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
        JOIN source_reads r ON r.receipt_id=json_extract(c.result_json,'$.receipt_id') AND r.principal=root.thread_id
        JOIN sources s ON s.source_id=r.source_id AND s.namespace=root.run_id
        WHERE root.run_id=? AND json_extract(n.request_json,'$.tool')='gx_read_source'
          AND s.blob_artifact_ref=json_extract(c.result_json,'$.source_ref')
          AND r.segment_id=json_extract(c.result_json,'$.segment_id')
          AND r.text_sha256=json_extract(c.result_json,'$.text_sha256')
        ORDER BY c.rowid""", (run_id,)):
        received[item[0]].append(item[1])
    for source in conn.execute("""SELECT source_id,blob_artifact_ref,blob_sha256,provenance_json
        FROM sources WHERE namespace=? AND json_extract(provenance_json,'$.source_kind')='job_acquired'
        AND json_extract(provenance_json,'$.external_source.kind')='article'
        AND json_extract(provenance_json,'$.source_identity_status')='matched_accession' ORDER BY rowid""", (run_id,)):
        source_id, ref, digest, raw = source
        provenance = json.loads(raw)
        job = provenance["job_id"]
        try:
            receipt = manifest.read_manifest(conn, job)
            if receipt["admission"]["run_id"] != run_id or receipt["manifest_hash"] != provenance["completion_sha256"]:
                raise ValueError("Acquisition manifest differs from the registered original")
        except (GxError, ValueError) as error:
            gaps.append({"job_id": job, "artifact_ref": ref, "reason": str(error)})
            continue
        inputs = [p["artifact_ref"] for p in receipt["admission"]["inputs"]]
        segments = received[source_id]
        unique = sorted(set(segments))
        originals.append({
            "accession": provenance["external_source"]["accession"],
            "sha256": digest,
            "job_id": job,
            "artifact_ref": ref,
            "input_candidates": inputs,
            "read_receipts": len(segments),
            "distinct_locations": len(unique),
            "segment_ids": unique,
        })
    result = summarize(pages, originals)
    result["unavailable"] = gaps
    return result
