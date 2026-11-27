"""Recoverable single-page discovery over the published Europe PMC and Crossref services."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import runpy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REQUEST = "gx.scholarly-request/1"
SNAPSHOT = "gx.scholarly-page-snapshot/1"
PROVIDERS = ("europepmc", "crossref")


def validate_search(document, query, action):
    if not isinstance(query, str) or not query.strip() or len(query) > 600:
        raise ValueError("Supply a nonempty bibliographic query of at most 600 characters")
    if action == "first" and document == {"schema": REQUEST}:
        return
    if action not in ("next", "replay"):
        raise ValueError("First takes a source request. For a saved page choose next or replay.")
    if not isinstance(document, dict) or document.get("schema") != SNAPSHOT:
        raise ValueError("Next and replay require the named search-snapshot.json from a prior page")
    if document.get("query") != " ".join(query.split()):
        raise ValueError("To change the question, start a fresh search with its request material")
    calls = document.get("calls")
    if not isinstance(calls, list) or len(calls) != len(PROVIDERS):
        raise ValueError("A saved page must retain both original provider outcomes")
    if any(not isinstance(c, dict) for c in calls) or [c.get("provider") for c in calls] != list(PROVIDERS):
        raise ValueError("Unexpected bibliographic sources in the saved page")
    for call in calls:
        if type(call.get("page")) is not int or call["page"] < 1:
            raise ValueError("Each archived provider outcome needs a positive page number")
        if "skipped" in call:
            if call["skipped"] not in {"exhausted", "continuation_unknown", "unknown"}:
                raise ValueError("Unrecognized archived coverage")
            continue
        if (type(call.get("offset")) is not int or call["offset"] < 0 or
                not isinstance(call.get("cursor"), str) or not call["cursor"]):
            raise ValueError("Each fetched page needs its original cursor and offset")
        if "error" not in call and not {"raw_b64", "sha256"} <= call.keys():
            raise ValueError("Each successful page needs the original response bytes and digest")


def page_url(base, cursor):
    parts = urlsplit(base)
    args = dict(parse_qsl(parts.query))
    args["cursorMark" if parts.hostname == "www.ebi.ac.uk" else "cursor"] = cursor
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(args), ""))


def read_page(api, call):
    provider = call["provider"]
    detail = {k: call[k] for k in ("provider", "page", "url", "retrieved_at", "skipped", "sha256", "error")
              if k in call}
    detail.update(returned=0, provider_hits=None, next_cursor=None, has_more=None)
    if "skipped" in call:
        detail.update(status="not_queried", coverage=call["skipped"],
                      has_more=False if call["skipped"] == "exhausted" else None)
        return [], detail
    if "raw_b64" in call:
        raw = base64.b64decode(call["raw_b64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != call.get("sha256"):
            raise ValueError("Search snapshot payload digest mismatch")
    if "error" in call:
        detail.update(status="unavailable", coverage="unknown")
        return [], detail
    try:
        payload = json.loads(raw)
        if provider == "europepmc" and str(payload.get("hitCount")) == "0" and payload.get("resultList") == {}:
            rows, total, observed, cursor = [], 0, 0, None
        else:
            rows, total = api["source_rows"](provider, raw)
            if provider == "europepmc":
                cursor = payload.get("nextCursorMark")
                observed = len(payload["resultList"]["result"])
            else:
                cursor = payload["message"].get("next-cursor")
                observed = len(payload["message"]["items"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        detail.update(status="unavailable", error=f"Invalid {provider} response: {exc}", coverage="unknown")
        return [], detail
    # Crossref can repeat an opaque cursor while advancing; compare response pages when continuing.
    seen = call["offset"] + observed
    end = total == 0 or seen >= total
    if end:
        coverage, more, cursor = "exhausted", False, None
    elif not observed or not isinstance(cursor, str) or not cursor:
        coverage, more, cursor = "continuation_unknown", None, None
    else:
        coverage, more = "partial", True
    detail.update(status="hits" if rows else "empty", provider_hits=total, returned=observed,
                  indexed_records=len(rows), offset_before=call["offset"], seen_through=seen,
                  next_cursor=cursor, has_more=more, coverage=coverage)
    return rows, detail


def discover(api, document, query, action, download=None):
    query = api["query_text"](query)
    validate_search(document, query, action)
    download = api["retrieve"] if download is None else download
    urls = api["search_urls"](query)
    calls, groups, summaries = [], [], []
    for index, provider in enumerate(PROVIDERS):
        previous = document["calls"][index] if action != "first" else None
        if previous and "skipped" not in previous and previous.get("url") != page_url(urls[provider], previous["cursor"]):
            raise ValueError("The archived catalogue URL differs from this query and cursor")
        if action == "replay":
            call = previous.copy()
        else:
            page = previous["page"] + 1 if previous else 1
            call = {"provider": provider, "page": page}
            offset, cursor = 0, "*"
            if previous:
                _, prior = read_page(api, previous)
                if not prior["next_cursor"]:
                    call["skipped"] = prior["coverage"]
                else:
                    cursor, offset = prior["next_cursor"], prior["seen_through"]
            if "skipped" not in call:
                url = page_url(urls[provider], cursor)
                call.update(url=url, cursor=cursor, offset=offset, retrieved_at=api["utc_now"]())
                try:
                    raw = download(url)
                except api["UpstreamUnavailable"] as exc:
                    call["error"] = str(exc)
                else:
                    digest = hashlib.sha256(raw).hexdigest()
                    if previous and previous.get("sha256") == digest:
                        call["error"] = "Provider repeated the prior page; continuation is unknown"
                    call.update(raw_b64=base64.b64encode(raw).decode("ascii"), sha256=digest)
        rows, detail = read_page(api, call)
        calls.append(call)
        groups.append((provider, rows))
        summaries.append(detail)
    if action == "next" and all("skipped" in call for call in calls):
        raise ValueError("Neither publisher supplied a usable continuation; revise the query or inspect prior results")
    papers = api["merge_results"](groups)
    return {"schema": api["CANDIDATES"], "query": query, "sources": summaries, "candidates": papers,
            "unique_count": len(papers), "returned_before_dedup": sum(len(rows) for _, rows in groups),
            "source_mode": "snapshot_replay" if action == "replay" else "live_search",
            "coverage_scope": "Listed page only. Duplicate publication IDs refer to the same publication; "
                              "independence of underlying experiments requires inspecting samples and conditions."}, {
                "schema": SNAPSHOT, "query": query, "calls": calls}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["search", "fetch"])
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--query")
    p.add_argument("--action")
    p.add_argument("--pmcid")
    args = p.parse_args()
    api = runpy.run_path(str(args.base))
    source = json.loads(args.source.read_bytes())
    if args.operation == "search":
        candidates, snapshot = discover(api, source, args.query, args.action)
        documents = {"candidates.json": candidates, "search-snapshot.json": snapshot}
    else:
        raw, record = api["fetch"](source, args.pmcid)
        (args.out / "article.xml").write_bytes(raw)
        documents = {"acquisition.json": record}
    for name, data in documents.items():
        (args.out / name).write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
