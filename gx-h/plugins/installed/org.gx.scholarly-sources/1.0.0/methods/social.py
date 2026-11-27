"""Ingest a PI-supplied public community link or a plain-text export, without fetching it."""

import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit


def article_identity(value):
    """Adapt the legacy wechat_article articleUrl and xhs_research note_url shapes."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("A public article URL is required")
    url = urlsplit(value)
    if url.scheme != "https" or url.username is not None or url.password is not None or url.port is not None:
        raise ValueError("Expected a public HTTPS community article link")
    if url.hostname == "mp.weixin.qq.com":
        if re.fullmatch(r"/s/[A-Za-z0-9_-]+", url.path):
            return "wechat", f"https://mp.weixin.qq.com{url.path}", f"wechat:{url.path[3:]}"
        query = parse_qs(url.query)
        fields = {"__biz": r"[A-Za-z0-9_+/=-]+", "mid": r"[0-9]+", "idx": r"[0-9]+"}
        if url.path == "/s" and all(
            len(query.get(k, [])) == 1 and re.fullmatch(pattern, query[k][0])
            for k, pattern in fields.items()
        ):
            stable = {k: query[k][0] for k in fields}
            identity = "wechat:" + urlencode(stable)
            # Public article signature may be needed to open this form of link.
            if len(query.get("sn", [])) == 1 and re.fullmatch(r"[A-Fa-f0-9]{32}", query["sn"][0]):
                stable["sn"] = query["sn"][0]
            return "wechat", "https://mp.weixin.qq.com/s?" + urlencode(stable), identity
    if url.hostname in {"www.xiaohongshu.com", "www.rednote.com"}:
        match = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?", url.path)
        if match:
            note_id = match[1].lower()
            return "xiaohongshu", f"https://{url.hostname}/explore/{note_id}", f"xiaohongshu:{note_id}"
    raise ValueError("Expected an identifiable public WeChat article or Xiaohongshu note")


def primary_candidates(paragraphs):
    references = []
    seen = set()
    for paragraph in paragraphs:
        for match in re.finditer(
            r"\bPMID\s*[:：]?\s*([0-9]{1,9})(?![0-9])|\b(10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+)",
            paragraph["text"], re.IGNORECASE,
        ):
            identifier = "PMID:" + match[1] if match[1] else "doi:" + match[2].rstrip(".;").lower()
            if identifier not in seen:
                seen.add(identifier)
                references.append({"identifier": identifier, "locator": paragraph["locator"]})
    return references


def clean_export_link(match):
    # Public exporters often repeat their share link in article.txt. Keep the
    # article identity in readable passages without copying access parameters.
    try:
        return article_identity(match[0])[1]
    except ValueError:
        return match[0]


def social_intake(raw: bytes) -> dict:
    """Hash the exact submitted JSON and index its supplied text. A link alone yields no passages."""
    row = json.loads(raw.decode("utf-8"))
    if not isinstance(row, dict) or set(row) - {"url", "text", "title", "captured_at"}:
        raise ValueError("Expected url and optional plain-text text, title, captured_at fields")
    provider, canonical_url, source_id = article_identity(row.get("url"))
    for field in ("text", "title", "captured_at"):
        if field in row and not isinstance(row[field], str):
            raise ValueError(f"{field} must be a string")

    body = row.get("text", "")
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(
        r"https://(?:www\.(?:xiaohongshu|rednote)\.com|mp\.weixin\.qq\.com)/[a-zA-Z0-9_./?=&%+#~-]+",
        clean_export_link, normalized,
    )
    paragraphs = []
    for chunk in re.split(r"\n[ \t]*\n+", normalized):
        if chunk.strip():
            paragraphs.append({"locator": f"paragraph:{len(paragraphs) + 1}", "text": chunk.strip()})
    return {
        "source_id": source_id,
        "provider": provider,
        "canonical_url": canonical_url,
        "acquisition_mode": "manual_import" if paragraphs else "link_only",
        "evidence_kind": "community_lead",
        "disposition": "requires_primary_source_followup",
        "title": row.get("title"),
        "captured_at": row.get("captured_at"),
        "metadata_origin": "as_supplied",
        "passage_normalization": "line endings, outer whitespace, canonical community links",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if paragraphs else None,
        "paragraphs": paragraphs,
        "primary_source_candidates": primary_candidates(paragraphs),
    }
