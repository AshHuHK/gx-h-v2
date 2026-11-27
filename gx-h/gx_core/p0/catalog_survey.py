"""Link the Run Lead's lightweight catalog survey to the scientific state it considered."""
from functools import lru_cache
import hashlib
import json

from .. import config
from ..domain import ids
from ..errors import SchemaInvalid
from ..persistence import store
from .capability_views import DISCOVERY_POLICY as POLICY

INVENTORY = "research.catalog.updated"
UPDATES = frozenset({
    INVENTORY, "v6.intent.changed", "v6.observation.added", "v6.node.settled",
    "v621.material.assigned", "research.pi.feedback", "research.pi.answered",
    "v62.module.wake", "v62.module.invalidated", "v62.round.settled", "v62.source.extracted",
    "v62.source.reviewed", "v62.claim.reassessed", "research.capabilities.changed",
})
INSTRUCTION = ("Before deciding, survey this research state with gx_query_catalog(kind=all), unless catalog_discovery already includes a survey_event. "
               "Use the current scientific question, available inputs and outputs you need. "
               "You may investigate background in the same turn and search again whenever useful. "
               "An empty survey permits any authorized next action, including investigating a gap.")


@lru_cache(maxsize=16)
def _digest(path, stat_key):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(conn):
    pins = []
    for name in ("workflow_packages.json", "shipped.json", "research_asset_cards.json"):
        path = config.CONFIG_DIR / name
        if name == "research_asset_cards.json" and not path.is_file():
            pins.append(None)
            continue
        stat = path.stat()
        pins.append(_digest(path, (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)))
    # Recipes are immutable and append-only; new publications change the available choices.
    recipe = conn.execute("SELECT rowid,digest FROM v6_saved_recipes ORDER BY rowid DESC LIMIT 1").fetchone()
    return ids.sha256_of_obj([*pins, list(recipe) if recipe else None])


def _last_inventory(conn, rid):
    row = conn.execute("SELECT json_extract(payload_json,'$.digest') FROM events "
                       "WHERE subject_kind='research_catalog_inventory' AND subject_id=? "
                       "ORDER BY event_id DESC LIMIT 1", (rid,)).fetchone()
    return row[0] if row else None


def inventory_changed(conn, rid):
    return inventory(conn) != _last_inventory(conn, rid)


def sync_inventory(conn, rid):
    current = inventory(conn)
    previous = _last_inventory(conn, rid)
    if current == previous:
        return
    with store.transaction(conn, transaction_owner="ScientificLead", operation="catalog_update", run_id=rid):
        if _last_inventory(conn, rid) == previous:
            store.append_event(conn, kind=INVENTORY, actor="GX", run_id=rid,
                               subject_kind="research_catalog_inventory", subject_id=rid,
                               payload={"digest": current, "previous_digest": previous})


def receipt(conn, state_key):
    row = conn.execute("SELECT event_id,payload_json FROM events WHERE subject_kind='catalog_survey' "
                       "AND subject_id=? ORDER BY event_id LIMIT 1", (state_key,)).fetchone()
    return {**json.loads(row[1]), "event_id": row[0]} if row else None


def _native_answer(conn, pointer):
    row = conn.execute("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                       (pointer["thread_id"], pointer["call_id"])).fetchone()
    return json.loads(row[0]) if row else None


def _candidates(answer):
    return [{key: item[key] for key in ("reference", "kind", "provider", "status") if key in item}
            for item in answer.get("items", ())[:5]]


def describe(conn, brief, prior, observed_inputs, *, catalog_sources=None):
    changed = [row for row in brief["trigger"]["changes"] if row["event"] in UPDATES]
    cursor = changed[-1]["event_id"] if changed else (prior["survey_cursor"] if prior else 0)
    key = ids.sha256_of_obj([POLICY, brief["run_id"], brief["goal_version"], cursor])
    question = brief["goal"]
    feedback = [item for item in brief.get("pi_dialogue", ()) if item["kind"] in ("feedback", "answer")]
    available = {}
    for material in brief["materials"]:
        if ref := material.get("artifact_ref"):
            available[ref] = {k: material[k] for k in ("name", "role", "artifact_ref") if k in material}
    for node in brief["nodes"]:
        if ref := node.get("result_ref"):
            available[ref] = {"artifact_ref": ref, "node_id": node["node_id"]}
            for port in node.get("outputs", ()):
                ref = port["artifact_ref"]
                available[ref] = {"artifact_ref": ref, "node_id": node["node_id"], "name": port["output_name"]}
    for ref, source in observed_inputs.items():
        available.setdefault(ref, {"artifact_ref": ref, "role": "observation", "source_id": source})
    for obs in brief["observations"]:
        if ref := obs.get("artifact_ref"):
            available.setdefault(ref, {"artifact_ref": ref, "role": obs["event"]})
    for ref, role in (catalog_sources or {}).items():
        available.setdefault(ref, {"artifact_ref": ref, "role": role})
    messages = {}
    for report in brief.get("module_report_index", ()):
        if ref := report.get("report_ref"):
            available[ref] = {"artifact_ref": ref, "role": "released_module_report"}
            message = report.get("researcher_message")
            if message and message["author_kind"] == "MODULE":
                messages[ref] = {"report_ref": ref, **message}
    gaps = brief.get("research_progress", {}).get("open_gaps", ())
    requirements = list(dict.fromkeys(value for gap in gaps
                        for value in ((gap.get("criterion") or {}).get("output_name"), gap["question"])
                        if value))
    if not requirements:
        requirements = [question]
    result = {"state_key": key, "through_event": cursor, "trigger_events": [r["event_id"] for r in changed],
              "question": question, "available_inputs": list(available.values()), "required_outputs": requirements}
    states = {o["event_id"]: o for o in brief.get("observations", ()) if "event_id" in o}
    result["updates"] = [{k: value for k, value in {
        "event": row["event"], "subject_ref": row["subject_ref"],
        "status": states.get(row["event_id"], {}).get("status"),
        "report_refs": row.get("report_refs"),
        "researcher_messages": [messages[ref] for ref in row.get("report_refs", ()) if ref in messages],
    }.items() if value} for row in changed]
    if feedback:
        latest = max(feedback, key=lambda i: i.get("answer_event_id") or i.get("event_id", 0))
        result["pi_update"] = latest.get("answer") or latest.get("text")
    existing = receipt(conn, key)
    if existing:
        result.update(survey_event=existing["event_id"], surveyed_query=existing["arguments"],
                      surveyed_total=existing["total"])
        if "first_candidates" in existing:
            result["surveyed_candidates"] = existing["first_candidates"]
        else:
            # Early survey receipts pointed to the native answer without embedding a card preview.
            original = _native_answer(conn, existing)
            if original is None:
                result["surveyed_details_unavailable"] = True
            else:
                result["surveyed_candidates"] = _candidates(original)
    return result


def required(conn, brief):
    survey = brief.get("catalog_discovery")
    if survey and not receipt(conn, survey["state_key"]):
        raise SchemaInvalid(INSTRUCTION, details={"reason": "CATALOG_SURVEY_REQUIRED",
                                                 "field": "catalog_discovery",
                                                 "state_key": survey["state_key"]})


def lead_query(conn, packet, arguments, identity, query):
    survey = packet.get("catalog_discovery")
    if not survey or arguments.get("reference"):
        return query(arguments)
    # A method selected during this turn immediately changes the choice shown to the same caller.
    revision, = conn.execute("SELECT max(event_id) FROM events WHERE run_id=? AND kind='research.capabilities.changed'",
                             (identity["run_id"],)).fetchone()
    request = {"kind": "all", "offset": 0, "limit": 15, **arguments}
    cache_key = "catalog.query:" + ids.sha256_of_obj([survey["state_key"], inventory(conn), revision, request])
    record = store.begin_idempotent(conn, cache_key, "catalog.query", request)
    if record.replay and (original := _native_answer(conn, record.response)) is not None:
        return original
    overview = request["kind"] == "all" and request["offset"] == 0
    saved = receipt(conn, survey["state_key"]) if overview else None
    if saved and "semantic_key" not in saved and saved["event_id"] >= (revision or 0):
        latest_change, = conn.execute("SELECT max(event_id) FROM events WHERE "
                                      "subject_kind='research_catalog_inventory' AND subject_id=?",
                                      (identity["run_id"],)).fetchone()
        previous = {"kind": "all", "offset": 0, "limit": 15, **saved["arguments"]}
        if previous == request and saved["event_id"] >= (latest_change or 0):
            original = _native_answer(conn, saved)
            if original is not None:
                store.complete_idempotent(conn, cache_key, {k: saved[k] for k in ("thread_id", "call_id")})
                return original

    result = query(arguments)
    if overview and not saved:
        # The context event, semantic query key and exact native response share the same GX transaction.
        context_ref, = conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?",
                                    (identity["task_id"],)).fetchone()
        event = store.append_event(conn, kind="research.catalog.surveyed", actor=identity["thread_id"],
                                   run_id=identity["run_id"], subject_kind="catalog_survey", subject_id=survey["state_key"],
                                   payload={"context_ref": context_ref, "through_event": survey["through_event"],
                                            "trigger_events": survey["trigger_events"], "arguments": arguments,
                                            "thread_id": identity["thread_id"], "turn_id": identity["turn_id"],
                                            "call_id": identity["call_id"], "total": result.get("total", 0),
                                            "first_candidates": _candidates(result), "semantic_key": cache_key})
        result = {**result, "survey_event": event}
    store.complete_idempotent(conn, cache_key, {"thread_id": identity["thread_id"], "call_id": identity["call_id"]})
    return result
