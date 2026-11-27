"""Search the public Atom catalogue and retrieve the exact preprint revision it lists."""
import argparse
import base64
from datetime import datetime, timezone
from hashlib import sha256
import http.client
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

REQUEST = "gx.scholarly-request/1"
SNAPSHOT = "gx.scholarly-search-snapshot/1"
CANDIDATES = "gx.scholarly-candidates/1"
API = "https://export.arxiv.org/api/query"
ARCHIVE = "https://arxiv.org"
ATOM = "{http://www.w3.org/2005/Atom}"
TOTAL = "{http://a9.com/-/spec/opensearch/1.1/}totalResults"
BASE_ID = r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z][a-z0-9.\-]*/[0-9]{7})"
VERSIONED = re.compile(BASE_ID + r"v[1-9][0-9]*")
MAX_BYTES = 32 * 1024 * 1024


class UpstreamUnavailable(ValueError):
    """arXiv returned a failure rather than a catalogue result."""


def retrieve(url):
    target = urlsplit(url)
    if target.scheme != "https" or target.netloc not in ("export.arxiv.org", "arxiv.org"):
        raise ValueError("Select an official arXiv HTTPS URL")
    # An explicit CONNECT tunnel prevents environment-dependent direct access.
    conn = http.client.HTTPSConnection("127.0.0.1", 7890, timeout=30)
    conn.set_tunnel(target.hostname, 443)
    try:
        conn.request("GET", target.path + ("?" + target.query if target.query else ""),
                     headers={"User-Agent": "GX-H-arXiv/1", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        if response.status != 200:
            raise UpstreamUnavailable(f"{target.hostname} returned HTTP {response.status}")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise UpstreamUnavailable("The returned document exceeds 32 MiB")
        return raw
    except ConnectionRefusedError as exc:
        raise ConnectionError("Clash refused 127.0.0.1:7890") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamUnavailable(f"arXiv transport via Clash failed: {type(exc).__name__}") from exc
    finally:
        conn.close()


def query_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 600:
        raise ValueError("Supply an arXiv query between 1 and 600 characters")
    return value.strip()


def search_url(query):
    # The API's field/Boolean grammar is available to the investigator.
    return API + "?" + urlencode({"search_query": query, "max_results": 8})


def validate_request(document, query):
    if document == {"schema": REQUEST}:
        return
    if not isinstance(document, dict) or document.get("schema") != SNAPSHOT or document.get("query") != query:
        raise ValueError("Replay requires the original arXiv search query")
    calls = document.get("calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ValueError("Replay requires one original arXiv API response")
    if calls[0].get("provider") != "arxiv" or calls[0].get("url") != search_url(query):
        raise ValueError("The frozen provider or query endpoint differs")


def parse_feed(raw):
    root = ET.fromstring(raw)
    if root.tag != ATOM + "feed":
        raise ValueError("Missing Atom feed")
    total = int(root.findtext(TOTAL) or "")
    if total < 0:
        raise ValueError("Negative hit count")
    records = []
    for entry in root.findall(ATOM + "entry"):
        address = urlsplit(entry.findtext(ATOM + "id") or "")
        identifier = address.path.removeprefix("/abs/")
        if (address.hostname != "arxiv.org" or not address.path.startswith("/abs/") or
                not re.fullmatch(BASE_ID + r"(?:v[1-9][0-9]*)?", identifier)):
            raise ValueError("The Atom response contains an API error or invalid arXiv accession")
        def field(tag):
            return " ".join((entry.findtext(ATOM + tag) or "").split()) or None
        pdf = ARCHIVE + "/pdf/" + identifier
        has_pdf = any(link.get("type") == "application/pdf" and
                      link.get("href", "").removesuffix(".pdf") in (pdf, pdf.replace("https:", "http:", 1))
                      for link in entry.findall(ATOM + "link"))
        revision = bool(VERSIONED.fullmatch(identifier))
        records.append({"arxiv_id": identifier, "title": field("title"), "summary": field("summary"),
                        "authors": [a.text.strip() for a in entry.findall(ATOM+"author/"+ATOM+"name") if a.text],
                        "published": field("published"), "updated": field("updated"),
                        "doi": entry.findtext("{http://arxiv.org/schemas/atom}doi"),
                        "abs_url": ARCHIVE + "/abs/" + identifier,
                        "pdf_url": pdf if has_pdf and revision else None,
                        "availability": "versioned_pdf" if has_pdf and revision else "metadata_only"})
    if total < len(records) or (total and not records):
        raise ValueError("Inconsistent first-page hit count")
    return records, total


def discover(document, query, download=retrieve):
    query = query_text(query)
    validate_request(document, query)
    replay = document["schema"] == SNAPSHOT
    if replay:
        call = dict(document["calls"][0])
    else:
        url = search_url(query)
        call = {"provider": "arxiv", "url": url, "retrieved_at": datetime.now(timezone.utc).isoformat()}
        try:
            raw = download(url)
        except UpstreamUnavailable as exc:
            call["error"] = str(exc)
        else:
            call.update(raw_b64=base64.b64encode(raw).decode("ascii"), sha256=sha256(raw).hexdigest())
    total, rows = None, []
    if "raw_b64" in call:
        raw = base64.b64decode(call["raw_b64"], validate=True)
        if sha256(raw).hexdigest() != call.get("sha256"):
            raise ValueError("arXiv snapshot SHA-256 mismatch")
        try:
            rows, total = parse_feed(raw)
        except (ValueError, ET.ParseError) as exc:
            call["error"] = f"Invalid arXiv Atom response: {exc}"
    elif not call.get("error"):
        raise ValueError("Missing frozen arXiv outcome")
    status = "unavailable" if call.get("error") else ("hits" if rows else "empty")
    info = {k: v for k, v in call.items() if k != "raw_b64"}
    info.update(status=status, provider_hits=total, returned=len(rows))
    return ({"schema": CANDIDATES, "provider": "arxiv", "query": query,
             "source_mode": "snapshot_replay" if replay else "live_search", "sources": [info], "candidates": rows},
            {"schema": SNAPSHOT, "query": query, "calls": [call]})


def select(document, identifier):
    if not isinstance(identifier, str) or not VERSIONED.fullmatch(identifier):
        raise ValueError("Select a versioned arXiv accession, including vN")
    if document.get("schema") != CANDIDATES or document.get("provider") != "arxiv":
        raise ValueError("Select the candidates.json output of arxiv-search@1")
    for row in document["candidates"]:
        if row["arxiv_id"] == identifier and row["pdf_url"] == ARCHIVE + "/pdf/" + identifier:
            return row
    raise ValueError("The selected revision lacks a PDF in this search result")


def fetch(document, identifier, download=retrieve):
    row = select(document, identifier)
    url = row["pdf_url"]
    raw = download(url)
    if not raw.startswith(b"%PDF-"):
        raise ValueError("The versioned arXiv endpoint did not return a PDF")
    digest = sha256(raw).hexdigest()
    source = "arXiv:" + identifier
    descriptor = {"source": source, "sha256": digest, "data": url + "#sha256=" + digest,
                  "publication": row["abs_url"], "applicability": "Versioned preprint. Embedded text, figures and cited publications have different evidential scope.",
                  "external_source": {"kind": "article", "accession": identifier, "url": url,
                                      "version": "sha256:" + digest, "retrieved_at": datetime.now(timezone.utc).isoformat()}}
    return raw, descriptor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("search", "fetch"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--query")
    parser.add_argument("--arxiv-id")
    args = parser.parse_args()
    document = json.loads(args.source.read_bytes())
    if args.action == "search":
        results, snapshot = discover(document, args.query)
        outputs = {"candidates.json": results, "search-snapshot.json": snapshot}
    else:
        raw, meta = fetch(document, args.arxiv_id)
        (args.out / "article.pdf").write_bytes(raw)
        outputs = {"acquisition.json": meta}
    for name, value in outputs.items():
        (args.out/name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
