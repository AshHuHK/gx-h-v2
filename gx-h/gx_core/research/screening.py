"""Content screens used by native event capture, brief compilation and project memory.

Credential redaction uses the configured patterns and records only match counts.
The instruction scan flags lines in untrusted material for brief assembly.
"""

from __future__ import annotations

import hashlib
import json
import re

from .. import config

#: Deliberately crude, and documented as such.
PATTERNS = (
    r"\bpip3?\s+install\b", r"\bconda\s+install\b", r"\bnpm\s+i(nstall)?\b",
    r"\bcurl\s+\S+\s*\|\s*(ba)?sh\b", r"\bwget\s+http", r"\bgit\s+clone\b",
    r"\bsudo\b", r"\bchmod\s+\+x\b", r"\bbash\s+\S+\.sh\b", r"\brm\s+-rf\b",
    r"\bexport\s+[A-Z_]+=", r"\b(api[_-]?key|token|password)\s*[:=]",
    r"\bupload\s+(your|the)\s+data\b", r"\bdisable\s+(the\s+)?(check|guard|validation)s?\b",
    r"\bignore\s+(all\s+|any\s+|your\s+|the\s+){0,2}(previous|prior|above|earlier)\s+(instructions|rules|guidance)\b",
    r"\byou\s+are\s+now\s+in\s+\w+\s+mode\b",
    r"\bno\s+further\s+(authori[sz]ation|approval)\s+is\s+(required|needed)\b",
    r"\"(approved|is_admin)\"\s*:\s*true",
    r"\bos\.system\s*\(", r"\bsubprocess\.(run|call|Popen)\s*\(",
    # Bound matches for parent directories, local file URLs and credential files.
    r"(?:\.\./){2,32}", r"\bfile:///", r"/etc/(?:passwd|shadow|sudoers)\b",
    r"\.credentials(?:\.json)?\b", r"\.git-credentials\b", r"\.netrc\b", r"\.pgpass\b", r"\.aws/credentials\b",
    r"\.ssh/", r"\bid_(?:rsa|dsa|ecdsa|ed25519)\b", r"\.docker/config\.json\b",
)
_ACTIONABLE = re.compile("|".join(PATTERNS), re.IGNORECASE)
#: A line longer than this is cut before it is scanned: no pattern needs more, and a scan
#: must stay linear on a page that is one enormous line.
_MAX_LINE = 2000


def count(text: str) -> int:
    """How many lines read as an instruction to act, within the per-line scanning bound."""
    return sum(1 for line in (text or "").splitlines() if _ACTIONABLE.search(line[:_MAX_LINE]))


_SCREEN_LISTS = ("credentials", "outbound_only")
_COMPILED: dict[str, tuple[tuple[str, str, re.Pattern[str]], ...]] = {}


def _entries() -> tuple[tuple[str, str, "re.Pattern[str]"], ...]:
    """``(list, id, compiled pattern)`` for configured entries, compiled once per content of ``screens``."""
    screens = (config.limits().get("screens") or {})
    wanted = {name: screens.get(name) or [] for name in _SCREEN_LISTS}
    key = hashlib.sha256(json.dumps(wanted, sort_keys=True).encode("utf-8")).hexdigest()
    found = _COMPILED.get(key)
    if found is None:
        compiled = []
        for name in _SCREEN_LISTS:
            for entry in wanted[name]:
                compiled.append((name, str(entry["id"]), re.compile(str(entry["pattern"]))))
        if not compiled:
            raise RuntimeError("config/limits.json screens contains no configured patterns")
        found = tuple(compiled)
        _COMPILED.clear()
        _COMPILED[key] = found
    return found


def screen_matches(text: str, *, lists: tuple[str, ...] = ("credentials",)) -> list[str]:
    """Return identifiers for matching patterns, omitting matched text."""
    unknown = sorted(set(lists) - set(_SCREEN_LISTS))
    if unknown:
        raise ValueError(f"screens has no list(s) {unknown} here; known: {list(_SCREEN_LISTS)}")
    hits: list[str] = []
    for name, entry_id, pattern in _entries():
        if name in lists and entry_id not in hits and pattern.search(text):
            hits.append(entry_id)
    return hits


def redact(text: str, *, lists: tuple[str, ...] = ("credentials",)) -> tuple[str, dict[str, int]]:
    """Replace matching spans with their screen id and return counts by id."""
    unknown = sorted(set(lists) - set(_SCREEN_LISTS))
    if unknown:
        raise ValueError(f"screens has no list(s) {unknown} here; known: {list(_SCREEN_LISTS)}")
    counts: dict[str, int] = {}
    for name, entry_id, pattern in _entries():
        if name in lists:
            text, found = pattern.subn(f"[redacted: {entry_id}]", text)
            if found:
                counts[entry_id] = counts.get(entry_id, 0) + found
    return text, counts
