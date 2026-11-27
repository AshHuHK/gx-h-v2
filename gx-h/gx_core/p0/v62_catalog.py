"""Research effort, immutable module grants and progressive discovery of local methods."""

from collections import Counter
from functools import lru_cache
import json
from pathlib import Path

from .. import config
from ..capabilities import manifest, shipped
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, VersionImmutable
from ..plugins import goal, loader
from ..plugins.author_api import _copy_schema
from ..plugins.selection import _references
from . import intent, v621_policy

_EFFORT = {"quick": (0, 0), "medium": (4, 3), "deep": (6, 5)}
_DEFAULTS = {"report_bytes": 4096, "brief_bytes": 65536, "catalog_limit": 15, "catalog_bytes": 8192,
             "no_progress_rounds": 2}
_MINIMUM = {"report_bytes": 512, "brief_bytes": 4096, "catalog_limit": 1, "catalog_bytes": 256,
            "max_parallel": 0, "max_rounds": 0, "no_progress_rounds": 1}
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
    return _copy_schema(registered, freeze=False)


def for_start(spec):
    """Freeze the discovered declaration and its exact installation closure before opening a Run."""
    settings = _policy(spec)
    branch_delegation = spec.get("branch_delegation", True)
    if type(branch_delegation) is not bool or (not branch_delegation and settings["effort"] == "quick"):
        raise SchemaInvalid("branch_delegation must be boolean; a restricted delegation grant needs medium or deep effort")
    requested = spec.get("research_modules")
    if requested is not None and (not isinstance(requested, list)
            or any(not isinstance(k, str) or k.count("@") != 1 for k in requested)
            or len(set(requested)) != len(requested)):
        raise SchemaInvalid("research_modules must list distinct module_id@version references")
    if settings["effort"] == "quick":
        return None
    if not spec.get("historical_replay"):
        settings["stagnation_policy"] = "advisory"
    from .v62_sources import TOOL_NAMES
    if not isinstance(spec.get("plugins", []), list):
        raise SchemaInvalid("plugins must be a list of exact provider id@version references")
    preferred = set(_references(spec.get("plugins", [])))
    path = config.CONFIG_DIR / "workflow_packages.json"
    release = json.loads(path.read_text())
    roots = [tuple(row["path"].split("/")) for row in release["plugins"]]
    version = v621_policy.for_start(spec)
    defaults = release.get("default_research_module_versions", {}).get(version, {})
    loaded = loader.bootstrap(selected=roots)
    candidates = {}
    for plugin in loaded.plugins.values():
        for key, definition in plugin.contributions.research_modules.items():
            if definition.get("method_profiles") and version not in v621_policy.LIVE_SCIENCE:
                continue
            reference = "@".join(key)
            if requested is None and defaults.get(key[0], key[1]) != key[1]:
                continue
            if (requested is None or reference in requested) and settings["effort"] in definition["efforts"]:
                candidates.setdefault(key, []).append(plugin)
    missing = set(requested or ()) - {"@".join(key) for key in candidates}
    if missing:
        raise SchemaInvalid(f"Unavailable research module grants for {settings['effort']}: {sorted(missing)}")
    modules = []
    deep = settings["effort"] == "deep"
    independent_science_access = deep and version in v621_policy.GOVERNED
    lead_source_tools = set(TOOL_NAMES) if independent_science_access else set()
    survey = False
    for key, owners in sorted(candidates.items()):
        selected = [p for p in owners if (p.pin.id, p.pin.version) in preferred] or owners
        if len(selected) != 1:
            raise SchemaInvalid(f"Select an exact provider in plugins for the research module {'@'.join(key)}")
        plugin = selected[0]
        declaration = _declaration(plugin, key)
        if deep and not independent_science_access:
            lead_source_tools.update(set(declaration["tools"]).intersection(TOOL_NAMES))
        survey |= declaration.get("catalog_access") == "public_methods"
        pins = goal._load([plugin.pin]).plugins
        modules.append({**{k: declaration[k] for k in _COMMON}, "plugin": goal._document(plugin.pin),
                        "provider_closure": [goal._document(p) for p in pins],
                        "declaration_sha256": ids.sha256_of_obj(declaration)})
    return {"policy": settings, "modules": modules,
            **({"branch_delegation": False} if not branch_delegation else {}),
            **({"source_tools": sorted(lead_source_tools)} if lead_source_tools else {}),
            **({"lead_method_cards": True} if independent_science_access else {}),
            **({"public_methods": _release_method_cards()} if independent_science_access or survey else {})}


def _release_method_cards():
    """Read only the operator's shipped public methods, without attaching executables or visiting study data."""
    cards = []
    for cap, path in sorted(shipped.MANIFESTS.items()):
        declaration = config.HARNESS_ROOT / path
        if not declaration.is_file():
            continue
        bundle = json.loads(declaration.read_bytes())
        for slot in ("entry_script", "parser"):
            relative = bundle.get(slot)
            if relative is not None:
                if Path(relative).is_absolute() or ".." in Path(relative).parts or not manifest.allowed_script_path(relative):
                    raise VersionImmutable("The public method references a script outside the release")
                bundle[slot] = {"path": relative, "sha256": ids.sha256_of_file(config.HARNESS_ROOT / relative)}
        pin = shipped.portable_digest(bundle)
        method = bundle["manifest"]
        if shipped.pin(cap) != pin or manifest.capability_version(method) != cap:
            raise VersionImmutable("The public method metadata differs from the registered release")
        cards.append(_public_card({"capability_version": cap, "portable_sha256": pin,
            "method": method["method_card"], "inputs": manifest.input_roles(method), "outputs": method["outputs"],
            "limitations": method.get("limitations"), "cost_class": "gpu" if method["resources"].get("gpu") else "local_cpu"}))
    return cards


def _public_card(row, *, metric_contract=False):
    """Project only scientific metadata. Provider paths, evaluation payloads, schemas and artifact links stay private."""
    method = row.get("method")
    if isinstance(method, dict):
        method = {key: method[key] for key in ("family", "objective", "mechanism", "estimator", "training_data_lineage")
                  if isinstance(method.get(key), str)}
    card = {key: row[key] for key in ("capability_version", "portable_sha256", "family", "cost_class", "limitations")
            if isinstance(row.get(key), str)}
    card["method"] = method if isinstance(method, (dict, str)) else {}
    card["inputs"] = [item for item in row.get("inputs", ()) if isinstance(item, str)]
    card["outputs"] = [{k: item[k] for k in ("name", "kind", "nature") if isinstance(item.get(k), str)}
                       if isinstance(item, dict) else {"name": item}
                       for item in (row.get("output_summary") or row.get("outputs") or ()) if isinstance(item, (str, dict))]
    if metric_contract:
        definitions = {item["name"]: item for item in row.get("outputs", ()) if isinstance(item, dict)}
        for output in card["outputs"]:
            declared = definitions.get(output["name"], {})
            if declared.get("metric_specs"):
                output.update(metric_specs=declared["metric_specs"], source_roles=declared["source_roles"])
    return card


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
    return [row for row in goal.catalog(conn, run_id)
            if granted.get(row["capability_version"]) == row["portable_sha256"]
            and not intent.forbidden_capability(*row["capability_version"].rsplit("@", 1), bans)]


def _fits_profile(method, profiles):
    inputs = set(method.get("inputs", ()))
    card = method.get("method")
    family = method.get("family") or (card.get("family") if isinstance(card, dict) else None)
    for output in method.get("outputs", ()):
        if not isinstance(output, dict):
            continue
        for profile in profiles:
            if "output_nature" in profile:
                # A classified measurement is eligible only with a named scientific output
                # whose declared data dependencies belong to this exact method.
                sources = output.get("source_roles")
                if (output.get("nature") == profile["output_nature"] and output.get("name") and output.get("kind")
                        and sources and set(sources) <= inputs):
                    return True
            elif (family == profile["method_family"] and profile["input_role"] in inputs
                  and output.get("kind") == profile["output_kind"]):
                return True
    return False


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
        methods = _methods(conn, run_id, inputs)
        if declaration.get("method_profiles"):
            allowed = [method["capability_version"] for method in methods
                       if _fits_profile(method, declaration["method_profiles"])]
        else:
            granted = {method["capability_version"] for method in methods}
            allowed = [c for c in declaration["capabilities"] if c in granted]
        return {**row, **declaration, "capabilities": allowed}
    raise ScopeDenied(f"Research module {module_id}@{version} is outside this Run's effort or grant")


def effective_methods(conn, run_id, grant, *, methods=None):
    """Intersect this delegation with the live Run pins, immutable module policy and active bans."""
    assigned = set(grant["capabilities"])
    profiles = grant.get("method_profiles")
    if not profiles:
        return assigned
    if methods is None:
        methods = _methods(conn, run_id, _inputs(conn, run_id))
    return {m["capability_version"] for m in methods
            if m["capability_version"] in assigned and _fits_profile(m, profiles)}


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


def _catalog_view(rows, settings, categories_for):
    compact = len(rows) > settings["catalog_limit"] or len(ids.canonical_json(rows).encode()) > settings["catalog_bytes"]
    view = {"mode": "categories" if compact else "summaries", "total": len(rows), "query_tool": "gx_query_catalog"}
    if compact:
        counts = sorted(Counter(name for row in rows for name in categories_for(row)).items())
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
    return ([] if compact else rows), view


def project(conn, run_id, brief):
    """Project a bounded catalog of methods and modules, keeping all records available by query."""
    inputs = _inputs(conn, run_id)
    catalog = inputs.get("research_catalog")
    if not catalog:
        return brief
    settings = catalog["policy"]
    methods, view = _catalog_view([_method_summary(row) for row in _methods(conn, run_id, inputs)],
                                  settings, lambda row: [row["category"]])
    modules, module_view = _catalog_view([_module_summary(r) for r in catalog["modules"]],
                                         settings, lambda row: row["focuses"])
    return {**brief, "capabilities": methods, "catalog_view": view, "research_effort": settings["effort"],
            "module_catalog": modules,
            **({"branch_delegation": False} if catalog.get("branch_delegation") is False else {}),
            **({"module_catalog_view": module_view, "module_catalog_available": True}
               if module_view["mode"] == "categories" else {}),
            **({"source_tools": catalog["source_tools"]} if catalog.get("source_tools") else {})}


def query(conn, run_id, brief, args):
    """Read exact or paginated catalogue records within a GX-authenticated, frozen task scope."""
    if (brief.get("run_id") != run_id or not isinstance(args, dict) or args.keys() -
            {"kind", "reference", "category", "query", "offset", "limit"}):
        raise ScopeDenied("Catalog request is outside the current Run or tool contract")
    kind = args.get("kind")
    if kind not in ("module", "method", "database", "research", "skill", "recipe"):
        raise SchemaInvalid("Unknown research catalogue kind")
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
    if kind in ("database", "research", "skill", "recipe"):
        from . import catalog_resources
        return catalog_resources.query(conn, run_id, brief, kind, args, _methods(conn, run_id, inputs))
    if kind == "method":
        rows = _methods(conn, run_id, inputs)
        survey = module_scope and brief.get("catalog_access") == "public_methods"
        public_lead = scope == "run" and catalog.get("lead_method_cards")
        if survey:
            from .v62_modules import require_active
            grant = json.loads(require_active(conn, run_id, scope)["grant_json"])
            if grant.get("catalog_access") != "public_methods" or "gx_query_catalog" not in grant["tools"]:
                raise ScopeDenied("Public method discovery is outside the module grant")
        if survey or public_lead:
            bans = intent.active_items(conn, run_id, intent.BAN_TYPES)
            cards = {r["capability_version"]: r for r in catalog.get("public_methods", ())
                     if not intent.forbidden_capability(*r["capability_version"].rsplit("@", 1), bans)}
            granted = {r["capability_version"] for r in rows}
            cards.update({r["capability_version"]: r if public_lead else _public_card(r) for r in rows})
            rows = [cards[k] for k in sorted(cards)]
        elif module_scope:
            allowed = brief.get("allowed_method_ids", [])
            if v621_policy.live_science(conn, run_id):
                from .v62_modules import _module
                module = _module(conn, run_id, scope)
                allowed = effective_methods(conn, run_id, json.loads(module["grant_json"]), methods=rows)
            rows = [row for row in rows if row["capability_version"] in allowed]
        if reference is not None:
            for row in rows:
                if row["capability_version"] == reference:
                    return {"kind": kind, "reference": reference, "spec": {k: v for k, v in row.items()
                                              if k not in ("content_artifact_ref", "provider_closure")},
                            **({"execution_granted": reference in granted} if public_lead else {})}
        entries = [_method_summary(row) for row in rows]
        if public_lead:
            for item in entries:
                item["execution_granted"] = item["capability_version"] in granted
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
