"""Identifiers for the rows governance writes.

``gx_core.domain.ids`` is lead-owned and has the kinds of contract v2 (check runs, reviews,
adjudications, cycles, imports). The few kinds that exist only inside this stream get their
prefix here, in the same layout, so one id still says what it addresses. A kind the domain
table knows always comes from the domain table; a local prefix that collides with one is an
error at import, not a second id space.
"""

from __future__ import annotations

import secrets
import time

from ..domain import ids as _domain

#: Kinds only this stream uses. ``ledger`` names one ``gx_ingest`` run (``imports.ledger_ref``).
_LOCAL_PREFIXES = {
    "ledger": "gxlg",
}

_B32 = "0123456789abcdefghjkmnpqrstvwxyz"

_clash = (set(_LOCAL_PREFIXES) & set(_domain.PREFIXES)) | (
    set(_LOCAL_PREFIXES.values()) & set(_domain.PREFIXES.values())
)
if _clash:  # pragma: no cover - two tables, checked once at import
    raise RuntimeError(f"governance id kinds or prefixes collide with gx_core.domain.ids: {sorted(_clash)}")


def _b32(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_B32[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(kind: str) -> str:
    """``<prefix>_<48-bit ms timestamp><40 bits of randomness>``, sortable by creation."""
    if kind in _domain.PREFIXES:
        return _domain.new_id(kind)
    if kind not in _LOCAL_PREFIXES:
        raise KeyError(
            f"unknown id kind {kind!r}; known: {sorted(set(_domain.PREFIXES) | set(_LOCAL_PREFIXES))}"
        )
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    return f"{_LOCAL_PREFIXES[kind]}_{_b32(ms, 10)}{_b32(secrets.randbits(40), 8)}"
