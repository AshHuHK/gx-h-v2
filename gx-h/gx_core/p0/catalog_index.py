"""Read-only discovery over release-pinned cards, projected through the caller's current scope."""
from collections import Counter
from functools import lru_cache
import json
import re
import unicodedata

from .. import config
from ..capabilities import manifest, shipped
from ..errors import GxError, SchemaInvalid, ScopeDenied
from ..plugins import goal, loader
from . import capability_views, catalog_resources, intent, method_health, v62_catalog


def installed_methods():
    """Return verified public method declarations. A changed install catalogue invalidates the snapshot."""
    return _snapshot()[0]


def _snapshot():
    paths = [config.CONFIG_DIR / name for name in ("workflow_packages.json", "shipped.json")]
    revisions = tuple((p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
    return _installed(config.HARNESS_ROOT, config.CONFIG_DIR, revisions)


@lru_cache(maxsize=4)
def _installed(root, directory, revisions):
    release = json.loads((directory / "workflow_packages.json").read_text())
    packages = loader.bootstrap(selected=[tuple(row["path"].split("/")) for row in release["plugins"]])
    unavailable = ["@".join(key) for key in packages.failures]
    builtins = {c["capability_version"]: c for c in v62_catalog._release_method_cards(unavailable=unavailable)}
    rows = []
    provided = set()
    for package in packages.plugins.values():
        labels = package.manifest.labels
        collections = [t.strip() for t in labels.get("gx_capability_collections", "").split(",") if t.strip()]
        aliases = [t.strip() for t in labels.get("gx_catalog_aliases", "").split(",") if t.strip()]
        declared_kind = labels.get("gx_catalog_kind")
        provider = package.pin.id + "@" + package.pin.version
        for cap, ref in goal._methods(package).items():
            if cap == ref:
                if cap not in builtins or (package.contributions.workflows and cap not in package.contributions.goal_methods):
                    continue
                card = builtins[cap]
            else:
                try:
                    bundle = shipped._package_bundle(package, ref)
                    method = bundle["manifest"]
                    card = v62_catalog._public_card({
                        "capability_version": cap, "portable_sha256": shipped.portable_digest(bundle),
                        "method": method["method_card"], "inputs": manifest.input_roles(method), "outputs": method["outputs"],
                        "limitations": method.get("limitations"),
                        "cost_class": "gpu" if method["resources"].get("gpu") else "local_cpu",
                    }, metric_contract=True)
                except (GxError, KeyError, TypeError, ValueError, OSError):
                    unavailable.append(f"{provider} / {cap}")
                    continue
            rows.append(dict(card, provider=provider, provider_sha256=package.pin.digest,
                             aliases=aliases, collections=collections,
                             **({"catalog_kind": declared_kind} if declared_kind in ("research", "database") else {})))
            provided.add(cap)
    rows.extend(dict(card, provider=None, collections=[], aliases=[])
                for cap, card in builtins.items() if cap not in provided)
    return tuple(sorted(rows, key=lambda row: (row["capability_version"], row["provider"] or ""))), tuple(unavailable)


# Aliases describe concepts; they never select a method or confer an execution grant.
_EQUIVALENTS = (
    ("文獻", "文献", "論文", "论文", "literature", "publication", "paper", "bibliographic"),
    ("全文", "full text", "full-text"),
    ("資料庫", "数据库", "database"),
    ("序列", "sequence"), ("工作流", "workflow", "recipe"),
    ("溶劑可及", "溶剂可及", "sasa", "solvent accessible"),
)
_ALIASES = {name: group for group in _EQUIVALENTS for name in group}
# A JATS source can satisfy a full-text search; a generic full-text reader need not support JATS.
_ALIASES["全文"] += ("jats",)
_STOP = frozenset("a an and are as at by for from how in is of on or the to with what which".split())


def _fold(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _text(value):
    if isinstance(value, str):
        return _fold(value)
    if isinstance(value, dict):
        return " ".join(f"{_fold(k)} {_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    return ""


def _words(query):
    query = _fold(query)
    # Also recognize an embedded CJK concept in a natural question without spaces.
    terms = re.findall(r"[^\W_]+(?:[-_.@][^\W_]+)*", query, flags=re.UNICODE)
    terms.extend(key for key in _ALIASES if not key.isascii() and key in query)
    return list(dict.fromkeys(t for t in terms if t not in _STOP))


def _context_words(context):
    """Rank against the research question, materials and findings. Opaque object IDs do not describe methods."""
    values = [context.get("question"), context.get("pi_update"), context.get("required_outputs")]
    for item in context.get("available_inputs", ()):
        values.extend([item.get("name"), item.get("role")])
    for update in context.get("updates", ()):
        for message in update.get("researcher_messages", ()):
            values.extend([(message.get("finding") or {}).get("text"), message.get("gaps"), message.get("next_steps")])
    return _words(_text(values))


def _fields(row):
    return {key: _text(row[key]) for key in (
        "reference", "provider", "title", "category", "aliases", "purpose", "method", "inputs", "outputs",
        "limitations", "kind", "focuses", "description", "related_methods", "collections", "methods",
        "tools", "source", "cost_class", "units", "id_alignment", "missing_values", "version", "training_data_lineage"
    ) if row.get(key)}


def _match(fields, words):
    hits = {}
    for word in words:
        for key, value in fields.items():
            if any(candidate in value for candidate in _ALIASES.get(word, (word,))):
                hits[word] = key
                break
    return hits


def _method_rows(conn, run_id, brief, inputs, active_rows):
    scope = brief["scope"]
    survey = False
    allowed = None
    if scope != "run":
        from . import v62_modules
        record = v62_modules.require_active(conn, run_id, scope)
        grant = json.loads(record["grant_json"])
        if "gx_query_catalog" not in grant["tools"]:
            raise ScopeDenied("This module has no catalogue grant")
        survey = grant.get("catalog_access") == "public_methods"
        allowed = set(v62_catalog.effective_methods(conn, run_id, grant, methods=active_rows))
    def owner(row):
        provider = row.get("plugin", {})
        return provider["id"] + "@" + provider["version"] if provider else None

    active = {(row["capability_version"], row["portable_sha256"], owner(row)): row for row in active_rows}
    cards = {(row["capability_version"], row["portable_sha256"], row["provider"]): row for row in installed_methods()}
    installed = set(cards)
    for key, row in active.items():
        if key not in cards:
            cards[key] = v62_catalog._public_card(row, metric_contract=True)
    variants = Counter((cap, digest) for cap, digest, _ in cards)
    options = capability_views.goal_options(inputs)
    bans = intent.active_items(conn, run_id, intent.BAN_TYPES)
    for (cap, digest, provider), card in cards.items():
        if intent.forbidden_capability(*cap.rsplit("@", 1), bans):
            continue
        key = (cap, digest, provider)
        granted = key in active and (allowed is None or cap in allowed)
        present = key in installed and (not granted or not provider or
                                        active[key]["plugin"]["sha256"] == card.get("provider_sha256"))
        if allowed is not None and not (granted or survey):
            continue
        opt = options.get(cap, {})
        selectable = (scope == "run" and opt.get("digest") == digest and opt.get("provider", provider) == provider
                      and opt.get("provider_digest", card.get("provider_sha256")) == card.get("provider_sha256"))
        if granted:
            status = "available" if present else "pinned_deployment_missing"
        elif selectable and not opt.get("provider") and variants[(cap, digest)] > 1:
            status = "needs_exact_provider"
        else:
            status = "selectable" if selectable else "catalog_only"
        # The public preview and exact card never expose paths, schemas, or evaluation payloads to survey modules.
        detail = card
        if granted and scope == "run":
            detail = {k: v for k, v in active[key].items() if k not in ("content_artifact_ref", "provider_closure")}
        summary = v62_catalog._method_summary(card)
        row = {**card, "reference": cap, "kind": "method", "category": summary["category"], "purpose": summary["purpose"],
               "provider": provider, "digest": digest, "execution_granted": granted, "status": status,
               "installation_status": "installed" if present else "unavailable"}
        row["_kinds"] = {"method"} | catalog_resources._method_kinds(card)
        row["_spec"] = detail
        yield row


def _outputs(row):
    result = {}
    outputs = row.get("outputs", ())
    slots = outputs.items() if isinstance(outputs, dict) else (("", outputs),)
    for slot, declarations in slots:
        for output in declarations:
            if isinstance(output, str):
                name, aliases = output, {output}
            elif isinstance(output, dict) and isinstance(output.get("name"), str):
                name = output["name"]
                aliases = {name, output.get("kind", ""), *output.get("metric_specs", {})}
            else:
                continue
            result[f"{slot}.{name}" if slot else name] = aliases
    return result


def _input_roles(row):
    roles = row.get("inputs", ())
    if isinstance(roles, dict):
        return list(roles.get("required", ())) if "properties" in roles else list(roles)
    return list(roles)


def _input_status(row, materials):
    roles = _input_roles(row)
    available = {m["role"] for m in materials if isinstance(m, dict) and m.get("role")}
    missing = [role for role in roles if role not in available]
    if not materials and roles:
        status = "no_visible_materials"
    elif missing:
        status = "needs_mapping"
    elif roles:
        status = "role_names_match"
    else:
        status = "no_fixed_roles"
    return {"status": status, "unmatched_roles": missing}


def query(conn, run_id, brief, args, inputs):
    scope = brief.get("scope", "")
    if scope != "run" and not scope.startswith("module:"):
        raise ScopeDenied("This task has no discovery grant")
    kind = args.get("kind", "all")
    kinds = {"all", "module", "method", "research", "database", "skill", "recipe"}
    if kind not in kinds:
        raise SchemaInvalid("Unknown catalogue kind")
    required = args.get("required_outputs", [])
    if not isinstance(required, list) or any(not isinstance(x, str) or not x.strip() for x in required):
        raise SchemaInvalid("required_outputs must contain named outputs or output types")
    active = v62_catalog._methods(conn, run_id, inputs)
    rows = []
    if kind in ("all", "method", "research", "database"):
        rows.extend(row for row in _method_rows(conn, run_id, brief, inputs, active)
                    if kind == "all" or kind in row["_kinds"])
    for resource_kind in ("skill", "recipe", "research", "database"):
        if kind in ("all", resource_kind):
            records = catalog_resources.rows_for_kind(conn, run_id, brief, resource_kind,
                                                       active if resource_kind == "recipe" else (), (), current_reading=True)
            rows.extend(dict(r, kind=resource_kind) for r in records)
    if kind in ("all", "module"):
        modules = (inputs.get("research_catalog") or {}).get("modules", ())
        for mod in modules:
            if scope == "run" or (mod["module_id"], mod["version"]) == (brief.get("module_id"), brief.get("module_version")):
                rows.append({**v62_catalog._module_summary(mod), "reference": mod["module_id"] + "@" + mod["version"],
                             "kind": "module", "category": "module", "outputs": [mod["output"]], "status": "available",
                             "digest": mod["declaration_sha256"]})
    if args.get("provider"):
        rows = [r for r in rows if r.get("provider") == args["provider"]]
    context = brief.get("catalog_discovery") or {}
    materials = context.get("available_inputs", brief.get("materials", ()))
    visible_active = {r["reference"] for r in rows if r["kind"] == "method" and r["execution_granted"]}
    observations = (method_health.recent(conn, run_id, [m for m in active if m["capability_version"] in visible_active])
                    if scope == "run" else {})
    if reference := args.get("reference"):
        matches = [r for r in rows if r["reference"] == reference]
        if not matches:
            raise ScopeDenied("Catalogue reference is unavailable within this scope")
        if len(matches) > 1:
            raise SchemaInvalid("Multiple catalogue entries match; specify the kind and exact provider",
                                details={"candidates": [{"kind": r["kind"], "provider": r.get("provider"),
                                                         "digest": r["digest"]} for r in matches]})
        row = matches[0]
        if row["kind"] == "module":
            spec = v62_catalog.resolve(conn, run_id, row["module_id"], row["version"])
            if scope != "run":
                spec["capabilities"] = [c for c in spec["capabilities"] if c in brief.get("allowed_method_ids", [])]
                spec["tools"] = [t for t in spec["tools"] if t in brief.get("allowed_tools", [])]
        else:
            spec = row.get("_spec", row)
        return {"kind": row["kind"], "reference": reference, "spec": spec, "status": row["status"],
                "execution_granted": row.get("execution_granted", row["status"] == "available"), "provider": row.get("provider"),
                **({"installation_status": row["installation_status"], "input_coverage": _input_status(row, materials)}
                   if row["kind"] == "method" else {}),
                **({"execution_health": observations.get(row["reference"], {"status": "unobserved"})}
                   if row["kind"] == "method" and scope == "run" and row["execution_granted"] else {})}

    query_text = args.get("query", "")
    words = _words(query_text)
    context_words = _context_words(context)
    results = []
    for row in rows:
        if args.get("category") and args["category"] not in [row.get("category"), *row.get("focuses", ())]:
            continue
        fields = _fields(row)
        matches = _match(fields, words)
        context_matches = _match(fields, context_words)
        exact = bool(query_text) and _fold(query_text) == fields["reference"]
        if words and not matches and not exact:
            continue
        outputs = _outputs(row)
        supplied = {_fold(v) for name, aliases in outputs.items() for v in (name, *aliases) if isinstance(v, str)}
        missing = [v for v in required if _fold(v) not in supplied]
        summary = {key: row[key] for key in ("reference", "kind", "category", "status", "digest", "provider",
                    "capability_version", "cost_class", "module_id", "version") if key in row}
        summary.update(purpose=str(row.get("purpose", ""))[:210], input_roles=_input_roles(row),
                       output_names=list(outputs), execution_granted=row.get("execution_granted", row["status"] == "available"))
        summary["match_reason"] = ("exact reference" if exact else ", ".join(f"{term}: {field}" for term, field in matches.items())[:130]
                                   or ("context: " + ", ".join(context_matches)[:100] if context_matches else "catalogue listing"))
        if required:
            summary.update(required_outputs_missing=missing, output_fit="partial_or_missing" if missing else "declared")
        if row.get("limitations"):
            summary["limitations"] = str(row["limitations"])[:200]
        if row["kind"] == "method":
            summary.update(kinds=sorted(row["_kinds"]), installation_status=row["installation_status"],
                           input_coverage=_input_status(row, materials))
            if scope == "run" and row["execution_granted"]:
                summary["execution_health"] = observations.get(row["reference"], {"status": "unobserved"})
        scope_priority = {"available": 0, "selectable": 1, "catalog_only": 2}.get(row["status"], 3)
        rank = (not exact, len(missing), -len(matches), scope_priority if words else 0,
                -len(context_matches), scope_priority,
                row["reference"], row.get("provider") or "")
        results.append((rank, summary))
    results.sort(key=lambda item: item[0])
    start = args.get("offset", 0)
    end = start + args.get("limit", 15)
    response = {"kind": kind, "items": [item for _, item in results[start:end]], "total": len(results),
                "next_offset": end if end < len(results) else None}
    if scope == "run" and kind in ("all", "method", "research", "database"):
        unavailable = _snapshot()[1]
        if unavailable:
            response["unavailable_installed_metadata"] = list(unavailable)
    return response
