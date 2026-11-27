"""Cards for installed analytical databases, source readers, research skills and runnable recipes."""
from .. import contracts
from ..domain import ids
from ..errors import ScopeDenied
from . import v62_sources


def _method_kinds(row):
    card = row.get("method")
    family = row.get("family") or (card.get("family") if isinstance(card, dict) else "") or ""
    kind = set()
    if row.get("catalog_kind") in {"database", "research"}:
        kind.add(row["catalog_kind"])
    if ("archive" in family or "database" in family or any(isinstance(o, dict) and
            o.get("acquired_material") for o in row.get("outputs", []))):
        kind.add("database")
    if any(word in family for word in ("literature", "bibliographic", "source_reading", "community_source")):
        kind.add("research")
    return kind


def query(conn, run_id, brief, kind, args, methods):
    from . import v62_catalog, v6_recipes
    from ..plugins import development
    module = brief["scope"].startswith("module:")
    allowed_methods = set(brief.get("allowed_method_ids", [])) if module else None
    visible = [row for row in methods if allowed_methods is None or row["capability_version"] in allowed_methods]
    tools = set(brief.get("allowed_tools" if module else "source_tools", ()))
    rows = []
    if kind == "recipe":
        if not module:
            pins = {r["capability_version"]:r["portable_sha256"] for r in visible}
            for card in v6_recipes.cards(conn):
                row = dict(card, category="workflow", purpose=card["title"])
                row["status"] = ("available" if all(pins.get(ref) == digest
                                                       for ref, digest in card["method_pins"].items()) else "requires_method")
                rows.append(row)
    elif kind == "skill":
        if not module:
            for card in development.skills(conn, run_id):
                rows.append(dict(card, category="evidence", purpose=card["description"], status="available",
                                 digest=card["plugin"]["sha256"]))
        if set(v62_sources.TOOL_NAMES) <= tools:
            stages = list(v62_sources.TOOL_NAMES)
            detail = {"tools":stages, "purpose":"Read an original passage, extract its evidence and review its scope.",
                      "inputs":["visible source artifact"], "outputs":["locator-grounded evidence","review"],
                      "limitations":"Abstract-only readings do not substantiate method details or numerical results."}
            rows.append(dict(detail, kind=kind, category="evidence", reference="source-evidence-review@1",
                             digest=ids.sha256_of_obj([contracts.native_goal_tool_spec(n) for n in stages]),
                             status="available"))
    else:
        for method in visible:
            if kind not in _method_kinds(method):
                continue
            card = v62_catalog._public_card(method, metric_contract=True)
            rows.append(dict(card, reference=method["capability_version"], digest=method["portable_sha256"],
                             status="available", kind=kind, **v62_catalog._method_summary(method)))
        if kind == "research":
            for tool in sorted(tools.intersection(v62_sources.TOOL_NAMES)):
                spec = contracts.native_goal_tool_spec(tool)
                rows.append({"reference":tool+"@1", "kind":kind, "category":"source_reading", "status":"available",
                             "purpose":spec["description"], "handler":tool, "inputs":spec["inputSchema"],
                             "digest":ids.sha256_of_obj(spec), "outputs":["source or evidence receipt"]})
    reference = args.get("reference")
    if reference:
        for card in rows:
            if card["reference"] == reference:
                return {"reference":reference, "kind":kind, "spec":card}
        raise ScopeDenied("Catalogue reference is unavailable within this scope")
    search = args.get("query", "").casefold()
    matches = []
    for row in sorted(rows, key=lambda card:card["reference"]):
        if args.get("category") and args["category"] != row["category"]:
            continue
        if search and search not in ids.canonical_json(row).casefold():
            continue
        summary = {key:row[key] for key in ("reference","kind","category","status","purpose","digest")}
        summary["version"] = row["reference"].rsplit("@",1)[-1]
        matches.append(summary)
    start = args.get("offset", 0)
    end = start + args.get("limit", 15)
    return {"kind":kind, "items":matches[start:end], "total":len(matches),
            "next_offset":end if end < len(matches) else None}
