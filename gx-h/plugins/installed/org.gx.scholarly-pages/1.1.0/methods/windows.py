"""Recoverable single-page discovery over the published Europe PMC and Crossref services."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import runpy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REQUEST = "gx.scholarly-request/1"
SNAPSHOT = "gx.scholarly-page-snapshot/2"
OLD_SNAPSHOT = "gx.scholarly-page-snapshot/1"
PROVIDERS = ("europepmc", "crossref")


def validate_search(document, query, action):
    if not isinstance(query, str) or not query.strip() or len(query) > 600:
        raise ValueError("Supply a nonempty bibliographic query of at most 600 characters")
    if action == "first" and document == {"schema": REQUEST}:
        return
    if action not in ("next", "replay"):
        raise ValueError("First takes a source request. For a saved page choose next or replay.")
    if not isinstance(document, dict) or document.get("schema") not in (SNAPSHOT, OLD_SNAPSHOT):
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


def _summary(records, prior=(), complete=True):
    """Union all published aliases, including a later record linking two earlier identifiers."""
    groups = {}
    owners = {}
    old_keys = {key for item in prior for key in item["keys"]}
    for number, item in enumerate((*prior, *records)):
        keys = set(item["keys"])
        matches = {owners[k] for k in keys if k in owners}
        merged = {"keys": keys, "open": item["open"], "abstract": item["abstract"]}
        for matched in matches:
            earlier = groups.pop(matched)
            keys.update(earlier["keys"])
            merged["open"] |= earlier["open"]
            merged["abstract"] |= earlier["abstract"]
        groups[number] = merged
        for key in keys:
            owners[key] = number
    papers = [dict(row, keys=sorted(row["keys"])) for row in groups.values()]
    fresh = sum(old_keys.isdisjoint(row["keys"]) for row in papers)
    with_abstract = sum(row["abstract"] for row in papers)
    counters = {
        "new_unique": fresh if complete else None,
        "cumulative_unique": len(papers),
        "history_complete": complete,
        "access": {"denominator": len(papers), "marked_open_access": sum(row["open"] for row in papers),
                   "abstract_only": with_abstract, "content_unknown": len(papers) - with_abstract,
                   "full_text_obtained": 0, "localized_reads": 0},
    }
    return papers, counters


def _keys(ids):
    keys = [kind + ":" + ids[kind] for kind in ("doi", "pmcid", "pmid") if ids.get(kind)]
    if ids.get("source") and ids.get("id"):
        keys.append("epmc:" + ids["source"] + ":" + ids["id"])
    return keys


def _page_candidates(groups, metadata):
    """Apply the same identity joins to the visible list and to the cumulative counts."""
    papers, _ = _summary(metadata)
    by_key = {key: i for i, paper in enumerate(papers) for key in paper["keys"]}
    records = {}
    for provider, rows in groups:
        for rank, row in enumerate(rows, 1):
            i = by_key[_keys(row["identifiers"])[0]]
            records.setdefault(i, []).append({"provider": provider, "rank": rank, **row})
    candidates = []
    for provenance in records.values():
        ids = {key: sorted({p["identifiers"][key] for p in provenance if key in p["identifiers"]})
               for key in ("doi", "pmcid", "pmid")}
        candidates.append({"title": provenance[0]["title"], "identifiers": ids,
                           "fetchable_pmcids": sorted({p["identifiers"]["pmcid"] for p in provenance
                                                       if p["open_jats_candidate"]}), "records": provenance})
    return candidates


def _provider_rows(api, provider, raw):
    payload = json.loads(raw)
    if provider == "europepmc":
        records = payload["resultList"].get("result")
        total_value, cursor = payload.get("hitCount"), payload.get("nextCursorMark")
        if records is None and str(total_value) == "0" and payload["resultList"] == {}:
            records = []
    else:
        records = payload["message"]["items"]
        total_value, cursor = payload["message"].get("total-results"), payload["message"].get("next-cursor")
    if not isinstance(records, list):
        raise ValueError("Invalid upstream result list")
    total = None if total_value is None else int(total_value)
    if total is not None and total < 0:
        raise ValueError("Invalid upstream hit count")
    # Reuse the original provider-specific identity and open-JATS parser. A placeholder total
    # is only needed inside that older parser; an unreported upstream total remains null outside.
    rows = []
    metadata = []
    for record in records:
        if provider == "europepmc":
            envelope = {"resultList": {"result": [record]}, "hitCount": 1}
        else:
            envelope = {"message": {"items": [record], "total-results": 1}}
        parsed, _ = api["source_rows"](provider, json.dumps(envelope).encode())
        if not parsed:
            continue
        row = parsed[0]
        keys = _keys(row["identifiers"])
        if not keys:
            continue
        marked_open = record.get("isOpenAccess") == "Y"
        has_abstract = bool(str(record.get("abstractText") or record.get("abstract") or "").strip())
        rows.append(dict(row, marked_open_access=marked_open, abstract_returned=has_abstract))
        metadata.append({"keys": keys, "open": marked_open, "abstract": has_abstract})
    return rows, metadata, len(records), total, cursor


def read_page(api, call):
    provider = call["provider"]
    detail = {k: call[k] for k in ("provider", "page", "url", "retrieved_at", "skipped", "sha256", "error")
              if k in call}
    prior = call.get("history_before", [])
    # Version 1 stored an offset but discarded identities on earlier pages.
    complete = call.get("history_complete", call.get("offset") == 0)
    offset = call.get("offset", 0)
    detail.update(returned=0, indexed_records=0, offset_before=offset, seen_through=offset,
                  provider_hits=None, next_cursor=None, has_more=None)
    rows, metadata = [], []
    if "skipped" in call:
        detail.update(status="not_queried", coverage=call["skipped"],
                      has_more=False if call["skipped"] == "exhausted" else None)
    elif "raw_b64" in call:
        raw = base64.b64decode(call["raw_b64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != call.get("sha256"):
            raise ValueError("Search snapshot payload digest mismatch")
        try:
            rows, metadata, observed, total, cursor = _provider_rows(api, provider, raw)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            detail.update(status="unavailable", coverage="unknown", error=f"Invalid {provider} response: {exc}")
        else:
            detail.update(status="hits" if observed else "empty", provider_hits=total, returned=observed,
                          indexed_records=len(rows), seen_through=offset + observed)
    else:
        detail.update(status="unavailable", coverage="unknown")
    papers, counts = _summary(metadata, prior, complete)
    detail.update(counts)
    if detail["status"] in ("hits", "empty"):
        seen_keys = {key for item in prior for key in item["keys"]}
        known_repeat = bool(metadata) and all(any(key in seen_keys for key in item["keys"]) for item in metadata)
        cursor_id = hashlib.sha256(cursor.encode()).hexdigest() if isinstance(cursor, str) and cursor else None
        seen_cursors = call.get("cursor_history", []) + [hashlib.sha256(call["cursor"].encode()).hexdigest()]
        if "error" in call:  # Old snapshots marked repeated bytes; retain the observation and its uncertainty.
            coverage, more = "continuation_unknown", None
        elif (total == 0 and not observed and offset == 0) or (
                complete and total is not None and total > 0 and counts["cumulative_unique"] == total):
            coverage, more = "exhausted", False
        elif not observed or not cursor_id or (known_repeat and cursor_id in seen_cursors):
            coverage, more = "continuation_unknown", None
        else:
            coverage, more = "partial", True
        detail.update(coverage=coverage, has_more=more, next_cursor=cursor if more else None,
                      all_identified_rows_seen_before=known_repeat)
    return rows, detail, papers, metadata


def discover(api, document, query, action, download=None):
    query = api["query_text"](query)
    validate_search(document, query, action)
    download = api["retrieve"] if download is None else download
    urls = api["search_urls"](query)
    calls, groups, summaries, prior_papers, just_seen = [], [], [], [], []
    for index, provider in enumerate(PROVIDERS):
        previous = document["calls"][index] if action != "first" else None
        if previous and "skipped" not in previous and previous.get("url") != page_url(urls[provider], previous["cursor"]):
            raise ValueError("The archived catalogue URL differs from this query and cursor")
        if action == "replay":
            call = previous.copy()
        else:
            call = {"provider": provider, "page": previous["page"] + 1 if previous else 1,
                    "offset": 0, "history_complete": True, "history_before": [], "cursor_history": []}
            cursor = "*"
            if previous:
                _, prior, prior_ids, _ = read_page(api, previous)
                call.update(offset=prior["seen_through"], history_before=prior_ids,
                            history_complete=prior["history_complete"], cursor_history=previous.get("cursor_history", []))
                if prev_cursor := previous.get("cursor"):
                    token = hashlib.sha256(prev_cursor.encode()).hexdigest()
                    if token not in call["cursor_history"]:
                        call["cursor_history"] = [*call["cursor_history"], token]
                cursor = prior["next_cursor"]
                if cursor is None:
                    call["skipped"] = prior["coverage"]
            if "skipped" not in call:
                url = page_url(urls[provider], cursor)
                call.update(url=url, cursor=cursor, retrieved_at=api["utc_now"]())
                try:
                    raw = download(url)
                except api["UpstreamUnavailable"] as exc:
                    call["error"] = str(exc)
                else:
                    call.update(raw_b64=base64.b64encode(raw).decode("ascii"), sha256=hashlib.sha256(raw).hexdigest())
        rows, detail, _, observations = read_page(api, call)
        calls.append(call)
        groups.append((provider, rows))
        summaries.append(detail)
        prior_papers.extend(call.get("history_before", []))
        just_seen.extend(observations)
    if action == "next" and all("skipped" in call for call in calls):
        raise ValueError("Neither publisher supplied a usable continuation; revise the query or inspect prior results")
    papers = _page_candidates(groups, just_seen)
    _, merged = _summary(just_seen, prior_papers, all(s["history_complete"] for s in summaries))
    return {"schema": api["CANDIDATES"], "query": query, "sources": summaries, "candidates": papers,
            "unique_count": len(papers), "returned_before_dedup": sum(len(rows) for _, rows in groups),
            "merged": merged, "source_mode": "snapshot_replay" if action == "replay" else "live_search",
            "coverage_scope": "Candidates and unique_count describe this page. Merged and source cumulative_unique "
                "count linked publications through this page; if history_complete is false, only surviving IDs "
                "are counted. Totals are provider estimates, rows may repeat. Access counts describe the search "
                "operation: marked-open overlaps abstract/metadata coverage. Actual full-text fetch and localized "
                "reading belong to the later acquisition and source-reading receipts."}, {
                "schema": document["schema"] if action == "replay" else SNAPSHOT, "query": query, "calls": calls}


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
