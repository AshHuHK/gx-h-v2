"""Research effort, immutable module grants and progressive discovery of local methods."""

from collections import Counter
from functools import lru_cache
import json

from .. import config
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, VersionImmutable
from ..plugins import goal, loader
from ..plugins.selection import _references
from . import intent

_EFFORT = {"quick": (0, 0), "medium": (4, 3), "deep": (6, 5)}
_DEFAULTS = {"report_bytes": 4096, "brief_bytes": 65536, "catalog_limit": 15, "catalog_bytes": 8192}
_MINIMUM = {"report_bytes": 512, "brief_bytes": 4096, "catalog_limit": 1, "catalog_bytes": 256,
            "max_parallel": 0, "max_rounds": 0}
_COMMON = ("module_id", "version", "purpose", "output", "cost_class", "focuses")
_QUICK = {"effort": "quick", **_DEFAULTS, "max_parallel": 0, "max_rounds": 0}


def _policy(spec):
    effort = spec.get("research_effort", "quick")
    if not isinstance(effort, str) or effort not in _EFFORT:
        raise SchemaInvalid("research_effort must be quick, medium or deep")
    parallel, rounds = _EFFORT[effort]
    overrides = spec.get("module_policy", {})
    if not isinstance(overrides, dict) or overrides.keys() - _MINIMUM.keys():
        raise SchemaInvalid("Unknown research module policy")
    for key, value in overrides.items():
        if type(value) is not int or value < _MINIMUM[key]:
            raise SchemaInvalid(f"module_policy.{key} needs an integer >= {_MINIMUM[key]}")
    if effort == "quick" and (overrides or spec.get("research_modules")):
        raise SchemaInvalid("Select medium or deep effort to delegate research modules")
    return {"effort": effort, "max_parallel": parallel, "max_rounds": rounds, **_DEFAULTS, **overrides}


def _declaration(plugin, key):
    registered = plugin.contributions.research_modules[key]
    return {k: list(v) if isinstance(v, tuple) else v for k, v in registered.items()}


def for_start(spec):
    """Freeze the discovered declaration and its exact installation closure before opening a Run."""
    settings = _policy(spec)
    requested = spec.get("research_modules")
    if requested is not None and (not isinstance(requested, list)
            or any(not isinstance(k, str) or k.count("@") != 1 for k in requested)
            or len(set(requested)) != len(requested)):
        raise SchemaInvalid("research_modules must list distinct module_id@version references")
    if settings["effort"] == "quick":
        return None
    if not isinstance(spec.get("plugins", []), list):
        raise SchemaInvalid("plugins must be a list of exact provider id@version references")
    preferred = set(_references(spec.get("plugins", [])))
    path = config.CONFIG_DIR / "workflow_packages.json"
    roots = [tuple(row["path"].split("/")) for row in json.loads(path.read_text())["plugins"]]
    loaded = loader.bootstrap(selected=roots)
    candidates = {}
    for plugin in loaded.plugins.values():
        for key, definition in plugin.contributions.research_modules.items():
            reference = "@".join(key)
            if (requested is None or reference in requested) and settings["effort"] in definition["efforts"]:
                candidates.setdefault(key, []).append(plugin)
    missing = set(requested or ()) - {"@".join(key) for key in candidates}
    if missing:
        raise SchemaInvalid(f"Unavailable research module grants for {settings['effort']}: {sorted(missing)}")
    modules = []
    for key, owners in sorted(candidates.items()):
        selected = [p for p in owners if (p.pin.id, p.pin.version) in preferred] or owners
        if len(selected) != 1:
            raise SchemaInvalid(f"Select an exact provider in plugins for the research module {'@'.join(key)}")
        plugin = selected[0]
        declaration = _declaration(plugin, key)
        pins = goal._load([plugin.pin]).plugins
        modules.append({**{k: declaration[k] for k in _COMMON}, "plugin": goal._document(plugin.pin),
                        "provider_closure": [goal._document(p) for p in pins],
                        "declaration_sha256": ids.sha256_of_obj(declaration)})
    return {"policy": settings, "modules": modules}


def _inputs(conn, run_id):
    run = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        raise ScopeDenied("Unknown research Run")
    return json.loads(run[0])["inputs"]


def policy(conn, run_id):
    catalog = _inputs(conn, run_id).get("research_catalog")
    return dict(catalog["policy"] if catalog else _QUICK)


@lru_cache(maxsize=128)
def _restore(root, conf, owner, closure):
    return goal._load([owner], closure).get(owner.id, owner.version, owner.digest)


def _methods(conn, run_id, inputs):
    goals = conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    granted = json.loads(goals[0]) if goals else {}
    bans = intent.active_items(conn, run_id, intent.BAN_TYPES)
    return [row for row in inputs["capability_catalog"]
            if granted.get(row["capability_version"]) == row["portable_sha256"]
            and not intent.forbidden_capability(*row["capability_version"].rsplit("@", 1), bans)]


def resolve(conn, run_id, module_id, version):
    """A module's effective methods are intersected again with the individual delegation by its owner."""
    inputs = _inputs(conn, run_id)
    for row in (inputs.get("research_catalog") or {}).get("modules", []):
        if (row["module_id"], row["version"]) != (module_id, version):
            continue
        owner = goal._pin(row["plugin"])
        plugin = _restore(config.HARNESS_ROOT, config.CONFIG_DIR, owner, tuple(map(goal._pin, row["provider_closure"])))
        if (module_id, version) not in plugin.contributions.research_modules:
            raise VersionImmutable("The granted research declaration is unavailable")
        declaration = _declaration(plugin, (module_id, version))
        if ids.sha256_of_obj(declaration) != row["declaration_sha256"]:
            raise VersionImmutable("The granted research declaration changed")
        granted = {method["capability_version"] for method in _methods(conn, run_id, inputs)}
        return {**row, **declaration, "capabilities": [c for c in declaration["capabilities"] if c in granted]}
    raise ScopeDenied(f"Research module {module_id}@{version} is outside this Run's effort or grant")


def _module_summary(row):
    return {k: row[k] for k in _COMMON}


def _method_summary(row):
    method = row.get("method") or {}
    card = method if isinstance(method, dict) else {}
    outputs = row.get("output_summary")
    if outputs is None:
        outputs = [{k: v for k, v in item.items() if k in ("name", "kind", "nature")}
                   if isinstance(item, dict) else {"name": item} for item in row.get("outputs", [])]
    return {"capability_version": row["capability_version"],
            "category": row.get("family", card.get("family", "uncategorized")),
            "purpose": method if isinstance(method, str) else card.get("objective", ""),
            "cost_class": row.get("cost_class", "unspecified"), "outputs": outputs}


def project(conn, run_id, brief):
    """Apply a byte and item threshold to the L2 projection only; keep every entry queryable."""
    inputs = _inputs(conn, run_id)
    catalog = inputs.get("research_catalog")
    if not catalog:
        return brief
    settings = catalog["policy"]
    rows = [_method_summary(row) for row in _methods(conn, run_id, inputs)]
    compact = len(rows) > settings["catalog_limit"] or len(ids.canonical_json(rows).encode()) > settings["catalog_bytes"]
    view = {"mode": "categories" if compact else "summaries", "total": len(rows), "query_tool": "gx_query_catalog"}
    if compact:
        counts = sorted(Counter(row["category"] for row in rows).items())
        categories = {}
        used_bytes = 2
        for name, count in counts:
            item_bytes = len(ids.canonical_json({name: count}).encode()) - 2 + bool(categories)
            if used_bytes + item_bytes > settings["catalog_bytes"]:
                break
            used_bytes += item_bytes
            categories[name] = count
        view["categories"] = categories
        if len(categories) < len(counts):
            view["category_count"] = len(counts)
            view["additional_categories"] = len(counts) - len(categories)
    return {**brief, "capabilities": [] if compact else rows, "catalog_view": view,
            "research_effort": settings["effort"], "module_catalog": [_module_summary(r) for r in catalog["modules"]]}


def query(conn, run_id, brief, args):
    """Read exact or paginated catalogue records within a GX-authenticated, frozen task scope."""
    if (brief.get("run_id") != run_id or not isinstance(args, dict) or args.keys() -
            {"kind", "reference", "category", "query", "offset", "limit"}):
        raise ScopeDenied("Catalog request is outside the current Run or tool contract")
    kind = args.get("kind")
    if kind not in ("module", "method"):
        raise SchemaInvalid("Catalog kind must be module or method")
    for key in ("reference", "query", "category"):
        if key in args and not isinstance(args[key], str):
            raise SchemaInvalid(f"Catalog {key} must be text")
    offset, limit = args.get("offset", 0), args.get("limit", 15)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 40:
        raise SchemaInvalid("Catalog offset must be nonnegative and limit between 1 and 40")
    inputs = _inputs(conn, run_id)
    catalog = inputs.get("research_catalog")
    if not catalog:
        raise ScopeDenied("Research catalog query requires a module-enabled Run")
    scope = brief.get("scope")
    module_scope = isinstance(scope, str) and scope.startswith("module:")
    if scope != "run" and not module_scope:
        raise ScopeDenied("This task has no module catalog grant")
    reference = args.get("reference")
    if kind == "method":
        rows = _methods(conn, run_id, inputs)
        if module_scope:
            allowed = brief.get("allowed_method_ids", [])
            rows = [row for row in rows if row["capability_version"] in allowed]
        if reference is not None:
            for row in rows:
                if row["capability_version"] == reference:
                    return {"kind": kind, "reference": reference, "spec": {k: v for k, v in row.items()
                                              if k not in ("content_artifact_ref", "provider_closure")}}
        entries = [_method_summary(row) for row in rows]
    else:
        rows = catalog["modules"]
        if module_scope:
            rows = [r for r in rows if (r["module_id"], r["version"]) ==
                    (brief.get("module_id"), brief.get("module_version"))]
        if reference is not None:
            for row in rows:
                if row["module_id"] + "@" + row["version"] == reference:
                    spec = resolve(conn, run_id, row["module_id"], row["version"])
                    if module_scope:
                        spec["capabilities"] = [c for c in spec["capabilities"] if c in brief.get("allowed_method_ids", [])]
                        spec["tools"] = [t for t in spec["tools"] if t in brief.get("allowed_tools", [])]
                    return {"kind": kind, "reference": reference, "spec": spec}
        entries = [_module_summary(row) for row in rows]
    if reference is not None:
        raise ScopeDenied("Catalog reference is unavailable in this task's grant")
    category = args.get("category")
    search = args.get("query", "").casefold()
    matches = [entry for entry in entries
               if (not category or category in entry.get("focuses", [entry.get("category")]))
               and search in ids.canonical_json(entry).casefold()]
    end = offset + limit
    return {"kind": kind, "items": matches[offset:end], "total": len(matches),
            "next_offset": end if end < len(matches) else None}
