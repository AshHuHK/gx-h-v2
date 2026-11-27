"""Cards for installed analytical databases, source readers, research skills and runnable recipes."""
from functools import lru_cache
import json

from .. import config, contracts
from ..domain import ids
from ..errors import ScopeDenied
from . import v62_sources


def _method_kinds(row):
    card = row.get("method")
    family = row.get("family") or (card.get("family") if isinstance(card, dict) else "") or ""
    kind = set()
    if row.get("catalog_kind") in {"database", "research"}:
        kind.add(row["catalog_kind"])
    if ("archive" in family or "database" in family or family == "yeast_enzyme_evidence" or any(isinstance(o, dict) and
            o.get("acquired_material") for o in row.get("outputs", []))):
        kind.add("database")
    if any(word in family for word in ("literature", "bibliographic", "source_reading", "community_source")):
        kind.add("research")
    return kind


@lru_cache(maxsize=4)
def _reference_index(path, modified_ns):
    return json.loads(path.read_bytes())["cards"]


def _references(kind, occupied):
    path = config.CONFIG_DIR / "research_asset_cards.json"
    if not path.is_file():
        return []
    result = []
    for card in _reference_index(path, path.stat().st_mtime_ns):
        if card["kind"] != kind or card["reference"] in occupied:
            continue
        # These are source locations and descriptions. Their presence cannot
        # choose a Job provider or turn an old workflow description into a recipe.
        result.append({**card, "status":"reference_only", "execution_granted":False})
        occupied.add(card["reference"])
    return result


def rows_for_kind(conn, run_id, brief, kind, methods, choices, *, current_reading=False):
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
            providers = {r["capability_version"]: v6_recipes.provider_pin(r) for r in visible}
            for card in v6_recipes.cards(conn):
                row = dict(card, category="workflow", purpose=card["title"])
                matches = (all(pins.get(ref) == value for ref, value in card["method_pins"].items())
                           and all(providers.get(ref) == value for ref, value in card.get("provider_pins", {}).items()))
                row["status"] = "available" if matches else "requires_method"
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
            if current_reading:
                guide = {
                    "title": "GX2Understand — source-grounded scientific reading",
                    "aliases": ["gx2understand", "論文精讀", "论文精读", "interpret research evidence"],
                    "purpose": "Resolve a disputed interpretation of original methods, measurements or figures.",
                    "inputs": ["visible original or scientific source"],
                    "outputs": ["locator-grounded evidence", "reading coverage", "interpretation and limitations"],
                    "tools": stages,
                    "adapted_from": {"reference": "gx-understand@legacy",
                        "sha256": "cfa6161ac65eb8fa77b1a07c8069cefb55aac51d992c91afe0e598e81763f295"},
                    "reading_guide": [
                        "Triage relevance to the current decision. Record why a source can be set aside. "
                        "For a central claim read the original methods or figure, at the exact locator.",
                        "Extract the endpoint, sample, denominator, units, controls and conditions used. "
                        "Keep the authors' findings, your inference, unseen pages and contrary evidence distinct.",
                        "Multiple interfaces can point to one publication. Compare the underlying samples "
                        "before treating reports as independent. Unknown independence is a valid outcome.",
                        "Explain which comparison or action this evidence changes or supports keeping. "
                        "If the disputed interpretation matters to that action, request a fresh reading "
                        "of the original by an independent researcher."
                    ],
                    "limitations": "Record abstracts, partial originals and unavailable appendices as partial coverage.",
                }
                rows.append(dict(guide, kind=kind, category="evidence", reference="gx2understand@1",
                                 status="available", digest=ids.sha256_of_obj([guide, rows[-1]["digest"]])))
    else:
        available = {r["capability_version"] for r in visible}
        for method in [*visible, *(r for r in choices if r["capability_version"] not in available)]:
            if kind not in _method_kinds(method):
                continue
            card = v62_catalog._public_card(method, metric_contract=True)
            rows.append({**v62_catalog._method_summary(method), **card, "reference":method["capability_version"],
                         "digest":method["portable_sha256"], "kind":kind,
                         "status":"available" if method["capability_version"] in available else "selectable"})
        if kind == "research":
            for tool in sorted(tools.intersection(v62_sources.TOOL_NAMES)):
                spec = contracts.native_goal_tool_spec(tool)
                rows.append({"reference":tool+"@1", "kind":kind, "category":"source_reading", "status":"available",
                             "purpose":spec["description"], "handler":tool, "inputs":spec["inputSchema"],
                             "digest":ids.sha256_of_obj(spec), "outputs":["source or evidence receipt"]})
    if not module:
        rows.extend(_references(kind, {row["reference"] for row in rows}))
    return rows


def query(conn, run_id, brief, kind, args, methods, choices):
    rows = rows_for_kind(conn, run_id, brief, kind, methods, choices)
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
        if row["status"] == "reference_only":
            summary["execution_granted"] = False
        if isinstance(row.get("inputs"), list):
            summary["input_roles"] = row["inputs"]
        if isinstance(row.get("outputs"), list):
            summary["output_names"] = [o if isinstance(o,str) else o["name"] for o in row["outputs"]
                                       if isinstance(o,str) or isinstance(o,dict) and "name" in o]
        summary["version"] = row["reference"].rsplit("@",1)[-1]
        matches.append(summary)
    start = args.get("offset", 0)
    end = start + args.get("limit", 15)
    return {"kind":kind, "items":matches[start:end], "total":len(matches),
            "next_offset":end if end < len(matches) else None}
