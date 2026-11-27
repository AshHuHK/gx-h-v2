"""Tokenization for word and CJK bigram FTS5 indexes.

SQLite unicode61 groups contiguous CJK characters into one word. Overlapping character
pairs allow partial matches. The same tokenizer processes both queries and documents.
"""

from __future__ import annotations

import re

#: CJK unified ideographs, extension A, and the compatibility block.
_CJK = "㐀-䶿一-鿿豈-﫿"
_CJK_RUN = re.compile(f"[{_CJK}]+")
#: A run of CJK characters, or a run of other word characters (letters and digits of any
#: other script; no underscore, no punctuation).
_RUNS = re.compile(f"[{_CJK}]+|[^\\W_{_CJK}]+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

#: Caps, so a pathological query cannot build an unbounded MATCH expression.
MAX_WORD_TERMS = 32
MAX_BIGRAM_TERMS_CJK = 64
MAX_BIGRAM_TERMS_ASCII = 48

#: Dropped from an English-only query on the bigram channel. Deliberately short: these are
#: the words that appear in every title and say nothing.
STOPWORDS = frozenset(
    "a an the of for and or in on to with by from via using is are as at into towards "
    "toward its their this that".split()
)

#: Strong identifiers recognised inside a query, for identifier routing.
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_ARXIV = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?![\d])")
_PMCID = re.compile(r"\bPMC\d{4,9}\b", re.IGNORECASE)
_PMID = re.compile(r"\bPMID[:\s]*(\d{5,9})\b", re.IGNORECASE)


def has_cjk(text: str) -> bool:
    return bool(text) and _CJK_RUN.search(text) is not None


def cjk_fraction(text: str) -> float:
    if not text:
        return 0.0
    return sum(len(run) for run in _CJK_RUN.findall(text)) / len(text)


def bigram_tokens(text: str) -> list[str]:
    """CJK runs as overlapping character bigrams, every other run as one lower-case word.

    A single CJK character standing alone is kept as itself: dropping it would make a
    one-character term unsearchable, and it cannot form a bigram.
    """
    out: list[str] = []
    for run in _RUNS.findall(text or ""):
        if _CJK_RUN.fullmatch(run):
            if len(run) > 1:
                out.extend(run[i:i + 2] for i in range(len(run) - 1))
            else:
                out.append(run)
        else:
            out.append(run.lower())
    return out


def bigram_text(text: str) -> str:
    """What is stored in ``memory_fts_cjk``: the bigram tokens joined by spaces."""
    return " ".join(bigram_tokens(text))


def word_terms(query: str) -> list[str]:
    """Query terms for the word channel: runs of word characters, exactly as v1 took them.

    A Chinese clause comes out as one term. That is this channel's known limit and the
    reason the bigram channel exists; it is not papered over here.
    """
    return _WORD.findall(query or "")[:MAX_WORD_TERMS]


def bigram_terms(query: str) -> list[str]:
    """Query terms for the bigram channel, de-duplicated in first-seen order.

    A query with any CJK in it keeps every token. An English-only query drops stopwords
    and one-character tokens — a title is otherwise ranked by how often it says "of".
    """
    tokens = list(dict.fromkeys(bigram_tokens(query or "")))
    if has_cjk(query or ""):
        return tokens[:MAX_BIGRAM_TERMS_CJK]
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1][:MAX_BIGRAM_TERMS_ASCII]


def match_expression(terms: list[str]) -> str | None:
    """A safe FTS5 MATCH expression: each term a quoted string literal, joined by OR.

    User text never reaches the query language. Inside a quoted string ``*``, ``NEAR`` and
    the boolean keywords are ordinary characters, so no operator can be smuggled in; a
    query with no terms yields ``None`` and the caller runs no search rather than all of it.
    """
    cleaned = [t for t in terms if t and t.strip()]
    if not cleaned:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in cleaned)


def strong_identifiers(query: str) -> list[tuple[str, str]]:
    """``(kind, value)`` for each DOI, arXiv id, PMCID or PMID written out in the query.

    Values are normalised the way they are stored: DOIs and PMCIDs lower-cased, an arXiv
    id without its version suffix.
    """
    found: list[tuple[str, str]] = []
    text = query or ""
    for match in _DOI.finditer(text):
        found.append(("doi", match.group(0).rstrip(".,;)").lower()))
    for match in _ARXIV.finditer(text):
        found.append(("arxiv", match.group(1)))
    for match in _PMCID.finditer(text):
        found.append(("pmcid", match.group(0).lower()))
    for match in _PMID.finditer(text):
        found.append(("pmid", match.group(1)))
    return list(dict.fromkeys(found))


def names_in_query(query: str, names: list[str]) -> list[str]:
    """Which of ``names`` the query mentions as a whole token, ignoring case.

    Only ASCII names of four characters or more are considered — the rule of GX-harness's
    ``exact_papers`` — because a shorter or non-ASCII name matches by accident too easily
    to be allowed to reorder a ranking.
    """
    hits: list[str] = []
    text = query or ""
    for name in names:
        if len(name) < 4 or not name.isascii():
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", text, re.IGNORECASE):
            hits.append(name)
    return hits


def snippet(body: str, terms: list[str], *, width: int = 240) -> str:
    """A window of the body around the first query term that occurs in it.

    Made from the stored body, not from the index: the bigram index holds text nobody
    should be shown. Falls back to the start of the body when no term occurs verbatim.
    """
    text = body or ""
    if len(text) <= width:
        return text
    lowered = text.lower()
    position = -1
    for term in terms:
        if not term:
            continue
        found = lowered.find(term.lower())
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return text[:width].rstrip() + "…"
    start = max(0, position - width // 4)
    end = min(len(text), start + width)
    start = max(0, end - width)
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
