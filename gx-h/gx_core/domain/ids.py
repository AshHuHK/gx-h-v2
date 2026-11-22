"""Identifier and fingerprint helpers.

Ids are time-ordered and carry their kind, so a mis-routed id is obvious in a log
instead of silently addressing the wrong table.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from typing import Any

PREFIXES = {
    "run": "gxr",
    "path": "gxp",
    "task": "gxt",
    "lease": "gxl",
    "job": "gxj",
    "artifact": "gxa",
    "eval": "gxe",
    "evidence": "gxv",
    "episode": "gxx",
    "checkpoint": "gxc",
    "budget": "gxb",
    "approval": "gxap",
    "source": "gxs",
    "doc": "gxd",
    "worker": "gxw",
    "request": "gxq",
    "interrupt": "gxi",
    # v2: one kind per table whose rows the backend mints an id for. Ids a person or a
    # bundle declares (asset ids, dataset ids, case ids, segment ids) are not minted here.
    "search": "gxls",
    "receipt": "gxrc",
    "gap": "gxg",
    "review": "gxrv",
    "query": "gxmq",
    "import": "gxim",
    "submission": "gxsb",
    "check_run": "gxck",
    "adjudication": "gxad",
    "objective": "gxo",
    "host_event": "gxhe",
    "reservation": "gxrs",
    "probe": "gxpr",
    "heldout_read": "gxhr",
    "cycle": "gxcy",
    "coordinator_lease": "gxcl",
    # P1/P3 additive r6 identities.
    "team": "gxtm",
    "team_message": "gxtmsg",
    "hypothesis": "gxh",
    "research_context": "gxcx",
    "research_round": "gxrr",
    "hypothesis_proposal": "gxhup",
    "host_pool": "gxhpool",
    "host_pool_slot": "gxhps",
}

_B32 = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford-ish, no i/l/o/u


def _b32(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_B32[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(kind: str) -> str:
    """``<prefix>_<48-bit ms timestamp><40 bits of randomness>``, lexicographically sortable."""
    if kind not in PREFIXES:
        raise KeyError(f"unknown id kind {kind!r}; known: {sorted(PREFIXES)}")
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = secrets.randbits(40)
    return f"{PREFIXES[kind]}_{_b32(ms, 10)}{_b32(rnd, 8)}"


def kind_of(identifier: str) -> str | None:
    prefix = identifier.split("_", 1)[0]
    for kind, pfx in PREFIXES.items():
        if pfx == prefix:
            return kind
    return None


def require_kind(identifier: str, kind: str) -> str:
    actual = kind_of(identifier)
    if actual != kind:
        raise ValueError(f"{identifier!r} is a {actual!r} id, expected {kind!r}")
    return identifier


# ------------------------------------------------------------------ canonical hashes

def canonical_json(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of_obj(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_of_file(path: str | os.PathLike[str], *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


PATH_FINGERPRINT_FIELDS = (
    "hypothesis",
    "method_family",
    "method_sequence",
    "tool_and_skill_versions",
    "input_manifest_ref",
    "eval_spec_ref",
    "parameters",
)


def path_fingerprint(spec: dict[str, Any]) -> str:
    """Fingerprint of a candidate path.

    Two paths with the same fingerprint are the same experiment: re-running one without
    new evidence spends budget to learn nothing. The field list lives in one place so
    the planner and the duplicate check can never diverge.
    """
    reduced = {field: spec.get(field) for field in PATH_FINGERPRINT_FIELDS}
    return sha256_of_obj(reduced)


def idempotency_key(scope: str, payload: dict[str, Any]) -> str:
    """Deterministic key for a side-effecting request, derived from its own content."""
    return f"{scope}:{sha256_of_obj(payload)[:32]}"
