"""Read the original late-source route and the context cursor used by every accepted decision."""

from datetime import datetime
import json
from support import P, read
from trial import sql


def contents(value):
    if isinstance(value, str):
        yield value
        text = value.removeprefix("GX research brief").strip()
        if text.startswith(("{", "[")):
            try:
                nested = json.loads(text)
            except ValueError:
                return
            yield from contents(nested)
    elif isinstance(value, list):
        for child in value:
            yield from contents(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from contents(child)


def receipt(folder, arm):
    intake = folder / "late-delivery.json"
    if not intake.exists():
        return {"intake": None, "official_items": [], "accepted_decisions": []}
    received = read(intake)
    source = (P / "sites.rdb").read_text().rstrip("\n")
    refs = read(P / "reference.json")
    # The exact original station row through the area cell survives trailing-column removal.
    prefixes = {}
    for row in source.splitlines():
        if not row.startswith("USGS\t"):
            continue
        site = row.split("\t")[1]
        area = refs["area_mi2"][site]
        prefixes[site] = row.partition("\t" + area + "\t")[0] + "\t" + area
    if arm == "stock":
        leads = {read(folder / "thread.json")["id"]}
        decisions = []
        feedback = []
    else:
        leads = {
            r["thread_id"]
            for r in sql(
                folder,
                "SELECT r.thread_id FROM p0_roots r JOIN v6_decision_tasks t ON r.task_id=t.task_id WHERE t.scope='run'",
            )
        }
        decisions = sql(
            folder,
            """SELECT e.event_id,e.ts,c.event_cursor,c.created_at AS context_utc,
                    d.decision_ref,d.thread_id,d.turn_id,d.call_id
                    FROM v6_decisions d JOIN research_contexts c ON c.context_ref=d.context_ref
                    JOIN events e ON e.subject_id=d.decision_ref AND e.kind='v6.decision.committed'
                    WHERE json_extract(d.request_json,'$.scope')='run' ORDER BY e.event_id""",
        )
        feedback = sql(
            folder,
            """SELECT event_id,kind,ts,json_extract(payload_json,'$.feedback_id') AS feedback_id
                    FROM events WHERE kind IN ('research.pi.feedback','pi.message.routed','research.pi.question','v6.run.closed')
                    ORDER BY event_id""",
        )
    items = []
    for path in (folder / "sessions").rglob("*.jsonl"):
        for line_no, line in enumerate(path.open(), 1):
            if not any(v in line for v in ["11560.0", "11,560", "11560", "49.4"]):
                continue
            try:
                r = json.loads(line)
                m = r["payload"]
                it = m.get("item", {})
            except (KeyError, ValueError):
                continue
            if r.get("type") != "event_msg" or m.get("type") != "item_completed":
                continue
            if (
                datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
                < received["at"] - 1
            ):
                continue
            if it.get("type") == "UserMessage":
                body = it.get("content", [])
            elif it.get("type") == "DynamicToolCall" and it.get("success") is True:
                body = it.get("content_items", [])
            else:
                continue
            strings = list(contents(body))
            exact = any(source in s for s in strings)
            stations = [sid for sid, prefix in prefixes.items() if any(prefix in s for s in strings)]
            normalized = [s.lower().replace(",", "") for s in strings]
            site_values = [
                sid
                for sid in prefixes
                if any(sid in t and refs["area_mi2"][sid].removesuffix(".0") in t for t in normalized)
            ]
            if not (exact or stations or site_values):
                continue
            desc = any(
                "drain_area_va" in t
                or ("area" in t and ("square mile" in t or "mi²" in t or "mi2" in t))
                for t in normalized
            )
            items.append(
                {
                    "utc": r["timestamp"],
                    "thread": m.get("thread_id"),
                    "turn": m.get("turn_id"),
                    "item": it.get("id"),
                    "kind": it["type"],
                    "tool": it.get("tool"),
                    "line": line_no,
                    "session": str(path.relative_to(folder)),
                    "scientific_lead": m.get("thread_id") in leads,
                    "entire_original": exact,
                    "original_rows_through_area": stations,
                    "cooccurring_site_and_value": site_values,
                    "area_field_or_units_in_body": desc,
                }
            )
    lead_items = [x for x in items if x["scientific_lead"]]
    return {
        "intake": received["at"],
        "intake_source_sha256": received["source_sha256"],
        "scientific_lead_threads": sorted(leads),
        "effective_feedback_and_closure_events": feedback,
        "accepted_decisions_with_input_cursors": decisions,
        "official_items": sorted(items, key=lambda x: x["utc"]),
        "lead_received_entire_inventory": any(x["entire_original"] for x in lead_items),
        "lead_received_both_original_rows": set().union(
            *(set(x["original_rows_through_area"]) for x in lead_items)
        )
        == set(prefixes),
        "lead_saw_domain_and_both_rows": any(x["area_field_or_units_in_body"] for x in lead_items)
        and set().union(*(set(x["original_rows_through_area"]) for x in lead_items)) == set(prefixes),
    }
