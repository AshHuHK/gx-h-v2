"""Make portable, metadata-only cohorts and independently count PMCID coverage from original response bytes."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
QUERIES = {
    "urban": ("gxr_01m8fdgmbaxg08wd5c", "gxj_01m8fdgndddxhd0ek8", "paging-v2"),
    "coastal": ("gxr_01m8fd9bzcpwt0qr1n", "gxj_01m8fddtn41yeg87ae", "literature"),
}


def save(path, item):
    data = (json.dumps(item, indent=2, ensure_ascii=False) + "\n").encode()
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Stored evidence differs: {path}. Select a fresh output directory for a new series.")
        return
    path.write_bytes(data)


def ingest(inputs_root, output):
    output.mkdir(exist_ok=True, parents=True)
    sources = []
    for name, (rid, jid, project) in QUERIES.items():
        folder = output / name
        folder.mkdir(exist_ok=True)
        relative = Path(project)/"state/artifacts"/rid/"jobs"/jid/"files/search-snapshot.json"
        raw = (inputs_root / relative).read_bytes()
        snap = json.loads(raw)
        calls = [c for c in snap["calls"] if urlsplit(c["url"]).hostname == "www.ebi.ac.uk"]
        if len(calls) != 1:
            raise ValueError("Expected one Europe PMC reply for this window")
        response = base64.b64decode(calls[0]["raw_b64"], validate=True)
        rows = json.loads(response)["resultList"]["result"]
        cohort = [{"record_key": r["source"] + ":" + r["id"]} for r in rows]
        evidence = [{"record_key": r["source"] + ":" + r["id"], "pmcid": r.get("pmcid")} for r in rows]
        save(folder/"cohort.json", {"records": cohort})
        save(folder/"evidence.json", {"metadata": evidence})
        # This reference runs on original publisher bytes before either derived input is read.
        conn = sqlite3.connect(":memory:")
        try:
            counts = conn.execute("""SELECT count(*),sum(has_locator) FROM (
                SELECT value->>'$.source',value->>'$.id',max(length(trim(coalesce(value->>'$.pmcid','')))>0) has_locator
                FROM json_each(?, '$.resultList.result') GROUP BY 1,2)""", (response,)).fetchone()
        finally:
            conn.close()
        candidates = {"input_files": [{"role": role, "file": fname,
                      "sha256": hashlib.sha256((folder / fname).read_bytes()).hexdigest()}
                      for role, fname in (("cohort","cohort.json"),("evidence","evidence.json"))]}
        sources.append({"study": name, "query": snap["query"], "source_run": rid, "source_job": jid,
                        "logical_snapshot": str(relative), "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
                        "provider_response_sha256": hashlib.sha256(response).hexdigest(), "source_rows": len(rows),
                        "reference_count": {"denominator": counts[0], "numerator": counts[1]}, **candidates})
    save(output/"provenance.json", sources)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    ingest(args.archive_root, args.output)
