"""The retrieval strategy as a value.

How memory is ranked is not code that a search function owns: it is a small JSON config
that the search *reads*. That is what lets an improvement cycle run an incumbent and a
candidate side by side by content hash, promote one by flipping ``strategy_pointers``, and
flip it back. The tokenizers are code and are not knobs; what a strategy may choose is
which channels are consulted, how they are weighted and fused, how many candidates each
contributes, how much a title counts, whether an identifier named in the query promotes
its source, and how many hits one source may take.

    {"channels": [{"channel": "BM25_WORD", "weight": 1.0},
                  {"channel": "BM25_CJK_BIGRAM", "weight": 1.0}],
     "fusion": "rrf", "rrf_k": 60, "candidates_per_channel": 300,
     "title_weight": 2.0, "identifier_routing": "promote", "max_per_source": 2}

An unknown key is refused, not ignored: a knob the search does not read would let two
configs with different hashes rank identically and call that a comparison.

A channel may also say when it runs: ``"when": "query_has_cjk"`` consults it only for a
query that contains CJK characters (``word-bigram-routed``: the bigram channel joins the
word channel for Chinese questions, and an English query is ranked by words alone). Every
config keeps at least one channel that always runs. ``"when": "always"`` is the default and
is left out of the normalised form, so a config without the knob hashes as it did before.

**Shipped strategies.** A strategy definition that ships in this tree (:data:`SHIPPED`) is
admitted like a shipped workflow: by the sha256 of its definition together with the source
of the modules that implement it (:func:`shipped_digest`), equal to its pin in
``config/shipped.json`` (``strategies``, lead-owned). Admitted, it can be named — by a
search run by content in an improvement cycle, or registered as a candidate asset — without
a gate. Being the default is another matter: only an ACTIVE promotion through governance's
gate moves ``strategy_pointers``, and the pointer can move it back.

The public harness ships lexical channels. A configuration that requests embeddings
receives an explicit unsupported-channel error.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Mapping

from .. import contracts
from ..domain import ids
from ..errors import ImplementationGap, SchemaInvalid

#: The asset id of the retrieval strategy and the key of its row in ``strategy_pointers``.
STRATEGY_KIND = "memory_retrieval"

CHANNEL_WORD = "BM25_WORD"
CHANNEL_CJK = "BM25_CJK_BIGRAM"
CHANNEL_ROUTING = "IDENTIFIER_ROUTING"
CHANNEL_EMBEDDING = "EMBEDDING"
LEXICAL_CHANNELS = (CHANNEL_WORD, CHANNEL_CJK)

EMBEDDING_NOT_BUILT = "the public harness ships lexical retrieval channels only"

_KEYS = {
    "channels", "fusion", "rrf_k", "candidates_per_channel", "title_weight",
    "identifier_routing", "max_per_source",
}
_ROUTING_MODES = ("off", "promote")
#: When a channel runs. ``always`` is the default and is not written into the normalised config.
WHEN_ALWAYS = "always"
WHEN_CJK = "query_has_cjk"
_WHEN = (WHEN_ALWAYS, WHEN_CJK)

# A store with no active strategy pointer uses word BM25.
BUILTIN: dict[str, dict[str, Any]] = {
    "bm25-word": {
        "channels": [{"channel": CHANNEL_WORD, "weight": 1.0}],
        "fusion": "rrf", "rrf_k": 60, "candidates_per_channel": 200,
        "title_weight": 2.0, "identifier_routing": "off", "max_per_source": 0,
    },
    "bm25-cjk-bigram": {
        "channels": [{"channel": CHANNEL_CJK, "weight": 1.0}],
        "fusion": "rrf", "rrf_k": 60, "candidates_per_channel": 300,
        "title_weight": 2.0, "identifier_routing": "off", "max_per_source": 0,
    },
    "word-bigram-rrf": {
        "channels": [{"channel": CHANNEL_WORD, "weight": 1.0}, {"channel": CHANNEL_CJK, "weight": 1.0}],
        "fusion": "rrf", "rrf_k": 60, "candidates_per_channel": 200,
        "title_weight": 2.0, "identifier_routing": "off", "max_per_source": 0,
    },
    "lexical-fusion": {
        "channels": [{"channel": CHANNEL_WORD, "weight": 1.0}, {"channel": CHANNEL_CJK, "weight": 1.0}],
        "fusion": "rrf", "rrf_k": 60, "candidates_per_channel": 300,
        "title_weight": 2.0, "identifier_routing": "promote", "max_per_source": 2,
    },
    # Add the bigram channel only for CJK queries.
    "word-bigram-routed": {
        "channels": [{"channel": CHANNEL_WORD, "weight": 1.0}, {"channel": CHANNEL_CJK, "weight": 1.0, "when": WHEN_CJK}],
        "fusion": "rrf", "rrf_k": 60, "candidates_per_channel": 200,
        "title_weight": 2.0, "identifier_routing": "off", "max_per_source": 0,
    },
}

#: Built-ins that ship as product definitions, admitted by their pin (see the module docstring).
SHIPPED: dict[str, dict[str, Any]] = {
    "word-bigram-routed": {
        "changes_from": "bm25-word",
        "description": ("Word BM25 for every query; the CJK character-bigram channel fused in by reciprocal rank "
                        "only when the query contains CJK characters. A candidate for the default: promoted only "
                        "through the improvement cycle on a split that did not design it."),
    },
}
#: The modules whose source decides what a shipped strategy does; part of its digest.
IMPLEMENTED_BY = ("gx_core/knowledge/strategy.py", "gx_core/knowledge/memory.py", "gx_core/knowledge/tokenize.py")
#: The table of ``config/shipped.json`` that pins shipped strategies, keyed ``memory_retrieval@<name>``.
PIN_TABLE = "strategies"

#: What an empty pointer means.
DEFAULT_BUILTIN = "bm25-word"


def _number(value: Any, name: str, *, low: float, high: float, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SchemaInvalid(f"strategy.{name} must be a number, got {value!r}", details={"knob": name})
    if not low <= float(value) <= high:
        raise SchemaInvalid(
            f"strategy.{name}={value!r} is outside {low}..{high}", details={"knob": name, "low": low, "high": high}
        )
    if integer:
        if float(value) != int(value):
            raise SchemaInvalid(f"strategy.{name} must be a whole number, got {value!r}", details={"knob": name})
        return int(value)
    return float(value)


def validate(config: Any) -> dict[str, Any]:
    """Return the config normalised, or refuse it. Every key is one the search reads."""
    if not isinstance(config, Mapping):
        raise SchemaInvalid("a retrieval strategy is a JSON object", details={"got": type(config).__name__})
    unknown = sorted(set(config) - _KEYS)
    if unknown:
        raise SchemaInvalid(
            f"unknown strategy key(s) {unknown}; known: {sorted(_KEYS)}. A knob the search does not "
            "read would give two configs different hashes and the same ranking.",
            details={"unknown": unknown, "known": sorted(_KEYS)},
        )
    channels_in = config.get("channels")
    if not isinstance(channels_in, list) or not channels_in:
        raise SchemaInvalid("strategy.channels must be a non-empty list", details={})
    known_channels = set(contracts.label_values("retrieval_channel"))
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in channels_in:
        if not isinstance(entry, Mapping) or set(entry) - {"channel", "weight", "when"} or "channel" not in entry:
            raise SchemaInvalid(
                "each strategy channel is {\"channel\": <name>, \"weight\": <number>[, \"when\": <when>]}",
                details={"entry": entry},
            )
        when = entry.get("when", WHEN_ALWAYS)
        if when not in _WHEN:
            raise SchemaInvalid(f"a channel's 'when' is one of {list(_WHEN)}, not {when!r}", details={"when": when})
        name = entry["channel"]
        if name not in known_channels:
            raise SchemaInvalid(
                f"{name!r} is not a retrieval channel the contract defines: {sorted(known_channels)}",
                details={"channel": name},
            )
        if name == CHANNEL_EMBEDDING:
            raise ImplementationGap(EMBEDDING_NOT_BUILT, details={"channel": name})
        if name == CHANNEL_ROUTING:
            raise SchemaInvalid(
                "identifier routing is switched with the identifier_routing knob, not listed as a channel",
                details={"channel": name},
            )
        if name in seen:
            raise SchemaInvalid(f"channel {name!r} is listed twice", details={"channel": name})
        seen.add(name)
        normalised = {"channel": name, "weight": _number(entry.get("weight", 1.0), "channels.weight", low=0.0, high=100.0)}
        if when != WHEN_ALWAYS:
            normalised["when"] = when
        channels.append(normalised)
    if not any(channel["weight"] > 0 for channel in channels):
        raise SchemaInvalid("at least one channel needs a weight above zero", details={})
    if not any(channel["weight"] > 0 and channel.get("when", WHEN_ALWAYS) == WHEN_ALWAYS for channel in channels):
        raise SchemaInvalid(
            "every query needs a channel that always runs: a strategy whose every channel is conditional ranks some "
            "queries with nothing", details={},
        )
    fusion = config.get("fusion", "rrf")
    if fusion != "rrf":
        raise SchemaInvalid(f"unknown fusion {fusion!r}; the only one implemented is 'rrf'", details={"fusion": fusion})
    routing = config.get("identifier_routing", "off")
    if routing not in _ROUTING_MODES:
        raise SchemaInvalid(
            f"identifier_routing {routing!r} is not one of {list(_ROUTING_MODES)}", details={"identifier_routing": routing}
        )
    return {
        "channels": channels,
        "fusion": fusion,
        "rrf_k": _number(config.get("rrf_k", 60), "rrf_k", low=1, high=10000, integer=True),
        "candidates_per_channel": _number(
            config.get("candidates_per_channel", 300), "candidates_per_channel", low=1, high=5000, integer=True
        ),
        "title_weight": _number(config.get("title_weight", 2.0), "title_weight", low=0.0, high=100.0),
        "identifier_routing": routing,
        "max_per_source": _number(config.get("max_per_source", 0), "max_per_source", low=0, high=1000, integer=True),
    }


def config_sha256(config: Mapping[str, Any]) -> str:
    """The content hash of a config: sha256 over its normalised canonical JSON."""
    return ids.sha256_of_obj(validate(config))


def builtin(name: str) -> dict[str, Any]:
    if name not in BUILTIN:
        raise SchemaInvalid(f"no built-in strategy {name!r}; known: {sorted(BUILTIN)}", details={"name": name})
    return validate(BUILTIN[name])


def _config_of(content: Any) -> Any:
    """A strategy's content is either the config itself or an object that holds one under ``config``."""
    if isinstance(content, Mapping) and "config" in content and isinstance(content["config"], Mapping):
        return content["config"]
    return content


def _described(config: dict[str, Any], *, version: str, source: str, asset_sha256: str | None = None) -> dict[str, Any]:
    return {
        "config": config, "version": version, "sha256": ids.sha256_of_obj(config), "source": source,
        "asset_sha256": asset_sha256,
    }


def active(conn: sqlite3.Connection) -> dict[str, Any]:
    """The strategy a search uses when none is passed: ``{config, version, sha256, source, asset_sha256}``.

    The pointer is governance's (``governance.pointers.active``): it follows
    ``strategy_pointers`` to the registered asset version, checks that the registered bytes
    are the sha256 the pointer was flipped to, and hands back the asset's ``config``. A
    pointer that cannot be followed is an error, not a reason to fall back: silently
    ranking with something else is exactly what the pointer exists to prevent. No pointer
    at all means the built-in incumbent.

    ``sha256`` is always the hash of the normalised config, which is what decides a ranking;
    ``asset_sha256`` is the hash of the registered asset version the pointer names.
    """
    from ..governance import pointers  # local: validating or hashing a config needs no governance

    current = pointers.active(conn, STRATEGY_KIND)
    if current is None:
        return _described(builtin(DEFAULT_BUILTIN), version=f"builtin:{DEFAULT_BUILTIN}", source="builtin")
    if current.get("config") is None:
        raise SchemaInvalid(
            f"strategy {current['asset_id']}@{current['version']} has no 'config' object for the search to read",
            details={"asset": f"{current['asset_id']}@{current['version']}", "sha256": current["sha256"]},
        )
    return _described(
        validate(_config_of(current["config"])), version=f"{current['asset_id']}@{current['version']}",
        source="pointer", asset_sha256=current["sha256"],
    )


def bundle_for(
    strategy: Any, *, version: str, description: str, changes_from: str | None = None,
    targets: tuple[str, ...] = ("gx_memory_search",),
) -> dict[str, Any]:
    """The content of a strategy asset version: its manifest, and the config the search reads.

    ``strategy`` is a built-in's name or a config. ``declared_knobs`` lists the knobs in which
    this config differs from ``changes_from`` (a built-in's name or ``<asset>@<version>`` text
    given only as a label) when that is a built-in, and every knob otherwise.
    """
    config = builtin(strategy) if isinstance(strategy, str) else validate(strategy)
    base = builtin(changes_from) if isinstance(changes_from, str) and changes_from in BUILTIN else None
    knobs = sorted(key for key in config if base is None or config[key] != base[key])
    manifest: dict[str, Any] = {
        "strategy_kind": STRATEGY_KIND, "version": str(version), "description": description,
        "declared_knobs": knobs, "targets": list(targets),
    }
    if changes_from is not None:
        manifest["changes_from"] = changes_from
    return {"manifest": manifest, "config": config}


def submit(ctx: Any, strategy: Any, *, version: str, description: str, changes_from: str | None = None,
           run_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    """Register a retrieval strategy as a candidate asset version. Registers; promotes nothing.

    The asset is ``memory_retrieval@<version>``; what makes a version the one the search
    uses is governance's gate and pointer, not this function.
    """
    from ..governance import assets  # local: validating or hashing a config needs no governance

    if isinstance(strategy, str) and strategy in SHIPPED:
        require_admitted(strategy)
    bundle = bundle_for(strategy, version=version, description=description, changes_from=changes_from)
    content_ref = assets.publish_content(ctx, bundle)
    return assets.submit_asset(ctx, kind="STRATEGY", manifest=bundle["manifest"], content_ref=content_ref,
                               origin="AUTHORED", run_id=run_id, task_id=task_id)


def resolve(conn: sqlite3.Connection, strategy: Any | None) -> dict[str, Any]:
    """The strategy for one search: the config passed in, run by content, or the active one.

    A shipped built-in is named only when it is admitted (:func:`require_admitted`).
    """
    if strategy is None:
        return active(conn)
    if isinstance(strategy, str):
        if strategy in SHIPPED:
            require_admitted(strategy)
        return _described(builtin(strategy), version=f"builtin:{strategy}", source="argument")
    config = validate(strategy)
    return _described(config, version=f"sha256:{ids.sha256_of_obj(config)}", source="argument")


# ------------------------------------------------------------------- shipped strategies

def shipped_bundle(name: str) -> dict[str, Any]:
    """The definition of a shipped strategy: the bundle a candidate asset of it would hold."""
    if name not in SHIPPED:
        raise SchemaInvalid(f"no shipped strategy {name!r}; shipped: {sorted(SHIPPED)}", details={"name": name})
    entry = SHIPPED[name]
    return bundle_for(name, version=name, description=entry["description"], changes_from=entry.get("changes_from"))


def _harness_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def shipped_digest(name: str) -> dict[str, Any]:
    """``{"sha256", "sources"}``: the sha256 of the definition and of each implementing module's source.

    Changing the config, the manifest or the code that ranks with it changes the digest, so a
    pin in ``config/shipped.json`` stops matching until the lead looks at the difference.
    """
    import hashlib

    root = _harness_root()
    sources = {}
    for rel in IMPLEMENTED_BY:
        path = root / rel
        sources[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return {"sha256": ids.sha256_of_obj({"definition": shipped_bundle(name), "sources": sources}), "sources": sources}


def _pins() -> dict[str, str]:
    import json

    path = _harness_root() / "config" / "shipped.json"
    try:
        table = json.loads(path.read_text(encoding="utf-8")).get(PIN_TABLE) or {}
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in table.items() if isinstance(value, str) and value}


def shipped_report() -> dict[str, Any]:
    """Every shipped strategy: its digest, its pin, whether they meet. For ``gx doctor`` and the lead."""
    pins = _pins()
    report: dict[str, Any] = {}
    for name in sorted(SHIPPED):
        key = f"{STRATEGY_KIND}@{name}"
        digest = shipped_digest(name)
        matches = digest["sources"] and all(digest["sources"].values()) and digest["sha256"] == pins.get(key)
        report[key] = {"sha256": digest["sha256"], "pinned_sha256": pins.get(key), "matches": bool(matches),
                       "admitted_as": "SHIPPED" if matches else None, "sources": digest["sources"]}
    return report


def require_admitted(name: str) -> dict[str, Any]:
    """The admission of a shipped strategy, or GATE_NOT_MET naming the digest and the pin."""
    from ..errors import GateNotMet

    entry = shipped_report()[f"{STRATEGY_KIND}@{name}"]
    if entry["admitted_as"] != "SHIPPED":
        raise GateNotMet(
            f"shipped strategy {name!r} is not admitted: its definition and implementing source hash to "
            f"{entry['sha256']}, and config/shipped.json pins "
            f"{entry['pinned_sha256'] or 'nothing'} under {PIN_TABLE}.{STRATEGY_KIND}@{name}",
            details={"name": name, "sha256": entry["sha256"], "pinned_sha256": entry["pinned_sha256"]},
        )
    return entry


def retrieval_mode(channels_ran: list[str]) -> str:
    """The contract's label for what actually ran. HYBRID only if an embedding channel did."""
    if CHANNEL_EMBEDDING in channels_ran:
        return "HYBRID"
    if CHANNEL_CJK in channels_ran:
        return "BM25_CJK_BIGRAM"
    if CHANNEL_WORD in channels_ran:
        return "BM25_FTS5"
    if CHANNEL_ROUTING in channels_ran:
        return "IDENTIFIER_ROUTING"
    return "BM25_FTS5"
