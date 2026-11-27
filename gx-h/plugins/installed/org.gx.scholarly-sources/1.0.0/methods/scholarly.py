"""Public scholarly metadata search and open JATS transfer. No accounts or paid providers."""
import base64
import http.client
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

REQUEST = "gx.scholarly-request/1"
SNAPSHOT = "gx.scholarly-search-snapshot/1"
CANDIDATES = "gx.scholarly-candidates/1"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CROSSREF = "https://api.crossref.org/works"
SOURCES = ("europepmc", "crossref")
PAGE_SIZE = 12
LIMIT = 24 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class UpstreamUnavailable(ValueError):
    """Provider failure. Keep it distinguishable from a valid empty result."""


class ProxyUnavailable(ConnectionError):
    """Stop all network calls if the local proxy cannot be reached."""


def retrieve(url):
    if (os.getenv("https_proxy") or os.getenv("HTTPS_PROXY")) != "http://127.0.0.1:7890":
        raise ProxyUnavailable("The scholarly Job requires the configured Clash proxy on 127.0.0.1:7890")
    target = urlsplit(url)
    if target.scheme != "https" or target.netloc not in {"www.ebi.ac.uk", "api.crossref.org"}:
        raise ValueError("Expected an official Europe PMC or Crossref API URL")
    connection = http.client.HTTPSConnection("127.0.0.1", 7890, timeout=25)
    connection.set_tunnel(target.hostname, 443)
    try:
        path = target.path + ("?" + target.query if target.query else "")
        connection.request("GET", path, headers={"Accept-Encoding": "identity", "User-Agent": "GX-H-scholarly/1"})
        response = connection.getresponse()
        if response.status != 200:
            raise UpstreamUnavailable(f"{target.hostname} returned HTTP {response.status}")
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise UpstreamUnavailable("The response exceeds the supported single-document size")
        return raw
    except ConnectionRefusedError as exc:
        raise ProxyUnavailable("Clash refused the connection to 127.0.0.1:7890") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamUnavailable(f"Upstream request through Clash failed: {exc}") from exc
    finally:
        connection.close()


def query_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 600:
        raise ValueError("Supply a non-empty scholarly query of at most 600 characters")
    return " ".join(value.split())


def pmc_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"PMC[1-9][0-9]{0,11}", value.upper()):
        raise ValueError("Select a PMCID such as PMC12345 from candidates.json")
    return value.upper()


def search_urls(query):
    return {
        "europepmc": EPMC + "/search?" + urlencode({
            "query": query, "format": "json", "pageSize": PAGE_SIZE, "resultType": "core"}),
        "crossref": CROSSREF + "?" + urlencode({
            "query.bibliographic": query, "rows": PAGE_SIZE,
            "select": "DOI,title,type,issued,updated-by"}),
    }


def validate_request(document, query):
    if not isinstance(document, dict):
        raise ValueError("Expected a scholarly request or a prior search snapshot")
    if document == {"schema": REQUEST}:
        return
    if document.get("schema") != SNAPSHOT or document.get("query") != query:
        raise ValueError("A saved search snapshot can replay only its original query")
    calls = document.get("calls")
    if not isinstance(calls, list) or [call.get("provider") for call in calls] != list(SOURCES):
        raise ValueError("A scholarly snapshot must retain both original provider outcomes")


def source_rows(provider, raw):
    doc = json.loads(raw)
    if provider == "europepmc":
        records = doc["resultList"]["result"]
        total = int(doc["hitCount"])
    else:
        records = doc["message"]["items"]
        total = int(doc["message"]["total-results"])
    if not isinstance(records, list) or total < 0:
        raise ValueError("Invalid upstream result list")
    rows = []
    for r in records:
        if provider == "europepmc":
            doi = r.get("doi")
            pmcid = r.get("pmcid")
            ids = {k: str(r[k]) for k in ("id", "source", "pmid") if r.get(k)}
            if pmcid:
                ids["pmcid"] = pmc_id(pmcid)
            title = r.get("title")
            categories = (r.get("pubTypeList") or {}).get("pubType", [])
            date = r.get("firstPublicationDate")
            fetchable = bool(pmcid and r.get("isOpenAccess") == "Y" and r.get("inEPMC") == "Y")
        else:
            doi = r.get("DOI")
            ids = {}
            title = " ".join(r.get("title") or [])
            categories = [r["type"]] if r.get("type") else []
            dates = (r.get("issued") or {}).get("date-parts") or [[]]
            date = "-".join(str(n) for n in dates[0]) or None
            fetchable = False
        if isinstance(doi, str) and doi.strip():
            ids["doi"] = doi.strip().casefold().removeprefix("https://doi.org/")
        if not ids:
            continue
        rows.append({"title": title, "identifiers": ids, "catalogue_types": categories,
                     "publication_date": date, "open_jats_candidate": fetchable})
    return rows, total


def merge_results(results):
    """Merge stable identifiers, retain independent provider locators and source ranks."""
    ordered = []
    for provider, rows in results:
        for rank, row in enumerate(rows, 1):
            ids = row["identifiers"]
            aliases = {(key, ids[key]) for key in ("doi", "pmcid", "pmid") if key in ids}
            if not aliases:
                aliases.add((ids["source"], ids["id"]))
            matches = [item for item in ordered if item[0].intersection(aliases)]
            fresh = {"provider": provider, "rank": rank, **row}
            if not matches:
                ordered.append((aliases, [fresh]))
                continue
            best_keys, best_rows = matches[0]
            best_keys.update(aliases)
            best_rows.append(fresh)
            for other in matches[1:]:
                best_keys.update(other[0])
                best_rows.extend(other[1])
                ordered.remove(other)
    papers = []
    for _, provenance in ordered:
        ids = {kind: sorted({p["identifiers"][kind] for p in provenance if kind in p["identifiers"]})
               for kind in ("doi", "pmcid", "pmid")}
        papers.append({"title": provenance[0]["title"], "identifiers": ids,
                       "fetchable_pmcids": sorted({p["identifiers"]["pmcid"] for p in provenance
                                                   if p["open_jats_candidate"]}),
                       "records": provenance})
    return papers


def discover(document, query, download=retrieve):
    query = query_text(query)
    validate_request(document, query)
    replay = document.get("schema") == SNAPSHOT
    urls = search_urls(query)
    calls, results, brief = [], [], []
    for i, (provider, url) in enumerate(urls.items()):
        if replay:
            call = document["calls"][i].copy()
            if call.get("url") != url:
                raise ValueError("The search snapshot URL differs from its frozen query")
        else:
            call = {"provider": provider, "url": url, "retrieved_at": utc_now()}
            try:
                raw = download(url)
            except UpstreamUnavailable as exc:
                call.update(error=str(exc))
            else:
                call.update(raw_b64=base64.b64encode(raw).decode("ascii"), sha256=sha256(raw).hexdigest())
        rows, total = [], None
        if "raw_b64" in call:
            raw = base64.b64decode(call["raw_b64"], validate=True)
            if sha256(raw).hexdigest() != call.get("sha256"):
                raise ValueError("Search snapshot response digest mismatch")
            try:
                rows, total = source_rows(provider, raw)
            except (ValueError, KeyError, TypeError) as exc:
                call["error"] = f"Invalid {provider} search response: {exc}"
        elif not call.get("error"):
            raise ValueError("Missing search snapshot outcome")
        state = "unavailable" if call.get("error") else ("hits" if rows else "empty")
        brief.append({k: call[k] for k in ("provider", "url", "retrieved_at", "sha256", "error") if k in call}
                     | {"status": state, "provider_hits": total, "returned": len(rows)})
        calls.append(call)
        results.append((provider, rows))
    papers = merge_results(results)
    return ({"schema": CANDIDATES, "query": query, "source_mode": "snapshot_replay" if replay else "live_search",
             "returned_before_dedup": sum(len(group) for _, group in results), "unique_count": len(papers),
             "sources": brief, "candidates": papers},
            {"schema": SNAPSHOT, "query": query, "calls": calls})


def selected(candidates, pmcid):
    identifier = pmc_id(pmcid)
    if not isinstance(candidates, dict) or candidates.get("schema") != CANDIDATES:
        raise ValueError("Supply candidates.json from scholarly-search@1")
    for row in candidates["candidates"]:
        if identifier in row["fetchable_pmcids"]:
            return row
    raise ValueError(f"{identifier} has no open JATS full text listed in these search results")


def fetch(candidates, pmcid, download=retrieve):
    pmcid = pmc_id(pmcid)
    selected(candidates, pmcid)
    url = f"{EPMC}/{pmcid}/fullTextXML"
    raw = download(url)
    try:
        article = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Full text unavailable: response is not JATS XML") from exc
    if article.tag != "article" or article.find("body") is None or not any(
            "".join(p.itertext()).strip() for p in article.findall("./body//p")):
        raise ValueError("Full text unavailable: response has no JATS article body paragraphs")
    article_meta = article.find("front/article-meta")
    if article_meta is None:
        raise ValueError("Full text lacks article metadata")
    ids = {el.get("pub-id-type"): (el.text or "").strip() for el in article_meta.findall("article-id")}
    deposited = {("PMC" + val if val.isdecimal() else val.upper()) for k, val in ids.items()
                 if k in {"pmc", "pmcid", "pmcaid"}}
    if deposited != {pmcid}:
        raise ValueError("Full-text accession differs from the selected PMCID")
    digest = sha256(raw).hexdigest()
    version = "sha256:" + digest
    source = "EuropePMC:" + pmcid
    detail = {"source": source, "data": url + "#sha256=" + digest, "sha256": digest,
              "publication": "https://doi.org/" + ids["doi"] if ids.get("doi") else None,
              "applicability": "Full JATS original. Abstract, body, and cited references have distinct locators.",
              "external_source": {"kind": "article", "accession": pmcid, "version": version,
                                  "retrieved_at": utc_now(), "url": url}}
    return raw, detail
