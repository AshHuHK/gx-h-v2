"""Deliver native researchers' words alongside GX evidence, and address short hints to peers."""
from __future__ import annotations

from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from ..persistence import store

HINT = "gx_send_research_hint"
HINT_SPEC = {
    "type": "function", "name": HINT,
    "description": "Send one short scientific hint to a named open peer in this Run. Cite only jointly readable originals. Check gx_task_context for peers and received hints.",
    "inputSchema": {"type": "object", "required": ["recipient_scope", "text", "source_refs", "operation_key"],
                    "additionalProperties": False, "properties": {
                        "recipient_scope": {"type": "string"}, "text": {"type": "string", "minLength": 1, "maxLength": 600},
                        "source_refs": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string"}},
                        "operation_key": {"type": "string", "minLength": 1}}}}



def hint_spec(brief):
    from . import harness_policy
    if not harness_policy.enabled(brief):
        return HINT_SPEC
    original = HINT_SPEC["inputSchema"]
    properties = {**original["properties"],
                  "action":{"type":"string","enum":["subscribe","publish","forward","correct","retract","history"]},
                  "topic":{"type":"string","minLength":1,"maxLength":128},
                  "question":{"type":"string","minLength":1,"maxLength":600},
                  "interpretation":{"type":"string","minLength":1,"maxLength":600},
                  "parent_hint_ids":{"type":"array","maxItems":8,"uniqueItems":True,"items":{"type":"string"}},
                  "source_refs":{"type":"array","maxItems":8,"uniqueItems":True,"items":{"type":"string"}}}
    return {**HINT_SPEC, "description":"Share a direct peer hint, or exchange research on a topic within this Goal. "
            "Subscribe to see past and new findings. Publish a question, interpretation and/or original source refs. "
            "For synthesis cite all parent_hint_ids. Forward, correct, retract or read revision history by parent_hint_ids. "
            "gx_task_context returns available topics and unread changes. Interpretations require independent source checks.",
            "inputSchema":{**original,"required":["operation_key"],"properties":properties}}


def active(conn, run_id):
    return bool(conn.execute("SELECT 1 FROM runs WHERE run_id=? AND "
        "json_extract(inputs_json,'$.inputs.discovery_policy')='gx.catalog/1'", (run_id,)).fetchone())


def author_statement(payload):
    """Verbatim submissions stay separate from GX's own reported numbers and interpretations."""
    return {key: list(payload.get(key, ())) for key in ("findings", "qualitative_notes", "gaps", "next_steps")}


def message_preview(bundle):
    statement = bundle.get("researcher_statement")
    if statement is None:
        return None
    author = bundle["author"]
    summary = {"author_kind": author["kind"]}
    if author["kind"] == "GX":
        return summary
    summary.update(task_id=author["task_id"], evidence_status="AUTHOR_INTERPRETATION")
    def passage(text):
        raw = text.encode("utf-8")
        if len(raw) <= 320:
            return text
        summary["read_full_report"] = True
        return raw[:320].decode("utf-8", errors="ignore")
    claims = [*statement["findings"], *statement["qualitative_notes"]]
    if any(len(items) > 1 for items in (claims, statement["gaps"], statement["next_steps"])):
        summary["read_full_report"] = True
    first = next((item for item in claims if item.get("kind") == "counterevidence"), next(iter(claims), None))
    if first:
        summary["finding"] = {"text": passage(first["text"]), "source_refs": first["source_refs"][:2]}
        if "kind" in first:
            summary["finding"]["kind"] = first["kind"]
        if len(first["source_refs"]) > 2:
            summary["read_full_report"] = True
    for name in ("gaps", "next_steps"):
        if statement[name]:
            summary[name] = [passage(statement[name][0])]
    return summary


def execution_facts(bundle):
    """Point to the checked original or named Job output separately from the scientific author's text."""
    facts = []
    for source in bundle["sources"]:
        proof = next((p for p in source["receipts"] if p.get("output_name") and p.get("job_id")),
                     next(iter(source["receipts"]), None))
        if proof:
            facts.append({"source_ref":source["artifact_ref"],
                          **{k:proof[k] for k in ("job_id", "output_name", "tool") if k in proof}})
    facts.sort(key=lambda item: "job_id" not in item)
    return {"source_count": len(bundle["sources"]), "job_count": len({f["job_id"] for f in facts if "job_id" in f}),
            "examples": facts[:2]}


def review_materials(conn, run_id, materials):
    """Bind a source-only reviewer's quote to an already granted original and extraction."""
    from . import v62_sources
    owned = {m["artifact_ref"] for m in materials}
    for material in materials:
        if material.get("role") != "source_evidence":
            continue
        ref = material["artifact_ref"]
        recorded = v62_sources._owned(conn, run_id, ref)
        if not recorded or recorded["kind"] != v62_sources.EXTRACTION:
            continue
        extraction = v62_sources._document(conn, ref, recorded)
        if extraction["source_ref"] not in owned or extraction["status"] != "LOCATOR_VERIFIED":
            continue
        # The enclosing material already pins the full immutable extraction. The original
        # read plus the character interval gives the entire quotation even for long text.
        target = {k: extraction[k] for k in (
            "source_ref", "segment_id", "char_start", "char_end", "quote_sha256")}
        target.update(claim_preview=extraction["claim"][:200], quote_preview=extraction["quote"][:200])
        target["preview_truncated"] = len(extraction["claim"]) > 200 or len(extraction["quote"]) > 200
        material["review_target"] = target


def peers(conn, run_id, scope):
    from . import harness_policy
    modern = harness_policy.active(conn,run_id)
    shared = " AND coalesce(json_extract(grant_json,'$.communication'),'shared')!='isolated'" if modern else ""
    rows = conn.execute("SELECT scope,module_id FROM v62_modules WHERE run_id=? AND status='OPEN' "
                        "AND scope!=? AND goal_version=(SELECT goal_version FROM v62_modules WHERE run_id=? AND scope=?) "
                        + shared + " ORDER BY module_instance_id", (run_id, scope, run_id, scope))
    return [dict(row) for row in rows]


def inbox(conn, run_id, scope):
    rows = [store.loads(row, "payload_json") for row in conn.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND kind='v62.module.peer_hint' AND subject_id=? "
        "ORDER BY event_id", (run_id, scope))]
    from . import harness_policy, research_topics
    if not rows or not harness_policy.active(conn,run_id):
        return rows
    member = conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND scope=?",(run_id,scope)).fetchone()
    if not member or research_topics.isolated(member):
        return []
    visible = research_topics.visible_refs(conn,member)
    return [{k:v for k,v in message.items() if k != "visibility_refs"} for message in rows
            if message["goal_version"] == member["goal_version"]
            and set(message.get("visibility_refs",message["source_refs"])) <= visible]


def send(conn, actor, args, native):
    from . import harness_policy, pi_materials, research_topics, v62_modules
    modern = harness_policy.active(conn, actor["run_id"])
    if modern and "action" in args:
        return research_topics.send(conn,actor,args,native)
    if modern and research_topics.isolated(actor):
        raise ScopeDenied("An isolated research assignment cannot send peer hints")
    rid, src = actor["run_id"], actor["scope"]
    target = args.get("recipient_scope")
    text, refs = args.get("text"), args.get("source_refs")
    if (set(args) != {"recipient_scope", "text", "source_refs"} or not isinstance(target, str)
        or not isinstance(text, str) or not text.strip() or len(text) > 600
        or not isinstance(refs, list) or not 1 <= len(refs) <= 4 or not all(isinstance(ref, str) for ref in refs)):
        raise SchemaInvalid("A peer hint needs a recipient scope, short text and one to four shared originals")
    recipient = conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND scope=? "
                             "AND status='OPEN' AND goal_version=?", (rid, target, actor["goal_version"])).fetchone()
    if not recipient or target == src or (modern and research_topics.isolated(recipient)):
        raise ScopeDenied("Select another open research module of this Run and Goal version")
    src_refs = set(v62_modules.visible_refs(conn, actor))
    dst_refs = set(v62_modules.visible_refs(conn, recipient))
    if not set(refs) <= src_refs & dst_refs or (pi_materials.private_originals(conn, rid) & src_refs) - dst_refs:
        raise ScopeDenied("Peer hints require shared originals within the recipient's assignment")
    message = {"sender_scope": src, "recipient_scope": target, "text": text, "source_refs": refs,
               "author": {k: native[k] for k in ("threadId", "turnId", "callId")}, "goal_version": actor["goal_version"]}
    if modern:
        message["visibility_refs"] = sorted(set(refs) | (src_refs & pi_materials.private_originals(conn,rid)))
    eid = store.append_event(conn, run_id=rid, kind="v62.module.peer_hint", actor=native["threadId"],
                             subject_kind="module", subject_id=target, payload=message,
                             evidence_level="EXECUTION_OBSERVED")
    return {"recipient_scope": target, "event_id": eid, "message_sha256": ids.sha256_of_obj(message)}
