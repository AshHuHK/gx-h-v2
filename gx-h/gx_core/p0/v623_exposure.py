"""Successful delivery of endpoint values to one Codex actor. All evidence survives a restart."""
import hashlib
import json

from ..domain import ids
from ..persistence import store
from . import v621_policy, v623_endpoints as endpoints

EVENT = "v623.endpoints.received"


def _delivered(entry, ref):
    if not ref:
        return []
    proofs = [{"metric_ref": m["metric_ref"]} for m in entry.get("verified_metrics", ())
              if isinstance(m, dict) and m.get("schema") == "gx.verified_metric/1"]
    compact = [{"row": row} for row in entry.get("endpoint_rows", ())]
    return [dict(source_ref=ref, **proof) for proof in [*proofs, *compact]]


def _packet(conn, packet):
    from . import v623_chronology
    external = [{"source_ref": m["artifact_ref"], "external_endpoint": m}
                for m in v623_chronology.delivered_external(conn, packet)]
    return external + [p for key, source in (("module_reports", "report_ref"), ("observations", "artifact_ref"))
            for entry in packet.get(key, ()) for p in _delivered(entry, entry.get(source))]


def _save(conn, root, turn, key, kind, proofs):
    if not proofs:
        return
    subject = ids.sha256_of_obj([root["thread_id"], turn, key, kind])
    if conn.execute("SELECT 1 FROM events WHERE run_id=? AND kind=? AND subject_id=?",
                    (root["run_id"], EVENT, subject)).fetchone():
        return
    store.append_event(conn, run_id=root["run_id"], kind=EVENT, actor=root["thread_id"],
                       subject_kind="endpoint_delivery", subject_id=subject,
                       payload=dict(task_id=root["task_id"], lease_id=root["lease_id"], thread_id=root["thread_id"],
                                    turn_id=turn, delivery_kind=kind, receipt=key, endpoints=proofs))


def brief_ack(conn, root, delivery):
    from . import v6_native
    if delivery["method"] != "turn/start" or not root or "brief" not in root.keys():
        return
    if not (root["brief"] or "").startswith(v6_native.PREFIX):
        return
    if hashlib.sha256(root["brief"].encode()).hexdigest() != delivery["text_sha256"]:
        return
    packet = v6_native.brief(root)
    if packet.get("evidence_policy") == endpoints.POLICY:
        _save(conn, root, delivery["turn_id"], delivery["event_id"], "brief", _packet(conn, packet))


def pending_reads(conn, run_id, thread_id):
    """Only unresolved analytical reads need official item-history reconciliation."""
    if not v621_policy.live_science(conn, run_id):
        return set()
    rows = conn.execute("""SELECT c.turn_id FROM p0_v6_native_calls n JOIN p0_root_calls c
        USING(thread_id,turn_id,call_id) JOIN artifacts a ON a.artifact_ref=json_extract(n.request_json,'$.arguments.ref')
        WHERE n.thread_id=? AND a.run_id=? AND json_extract(n.request_json,'$.tool')='gx_read_artifact'
        AND length(CAST(c.result_json AS BLOB))<=? AND (
          (json_extract(n.request_json,'$.arguments.endpoint_offset') IS NOT NULL
            AND json_array_length(c.result_json,'$.endpoint_rows')>0) OR
          (a.kind='module_report_bundle' AND json_array_length(c.result_json,'$.verified_metrics')>0) OR
          (a.kind='research_brief' AND EXISTS (SELECT 1 FROM json_tree(c.result_json) WHERE
              key IN ('endpoint_rows','verified_metrics') AND type='array' AND json_array_length(value)>0)))
        AND NOT EXISTS(SELECT 1 FROM events e WHERE e.run_id=a.run_id AND e.actor=n.thread_id
          AND e.kind=? AND json_extract(e.payload_json,'$.receipt')=n.call_id)""",
        (thread_id, run_id, endpoints.PAGE_BYTES*2, EVENT))
    return {r[0] for r in rows}


def read_completed(conn, root, turn, item):
    """Only the successful exact content echoed by the official completed item may grant exposure."""
    if not root or item.get("tool") != "gx_read_artifact" or item.get("type") != "dynamicToolCall":
        return
    if item.get("status") != "completed" or item.get("success") is not True:
        return
    if not v621_policy.live_science(conn, root["run_id"]):
        return
    call = conn.execute("""SELECT c.result_json,n.request_json FROM p0_root_calls c JOIN p0_v6_native_calls n
        USING(thread_id,turn_id,call_id) WHERE c.thread_id=? AND c.turn_id=? AND c.call_id=?""",
        (root["thread_id"], turn, item.get("id"))).fetchone()
    if not call:
        return
    request = json.loads(call["request_json"])
    args = request["arguments"]
    if item.get("arguments") != args or request["tool"] != "gx_read_artifact":
        return
    if len(call["result_json"].encode()) > endpoints.PAGE_BYTES*2:
        return  # A large native tool result may be clipped again before model inference.
    if item.get("contentItems") != [{"type": "inputText", "text": call["result_json"]}]:
        return
    # An original JSON document can contain arbitrary researcher-defined keys. Only GX-owned
    # document structures or a GX endpoint page can confer a measurement receipt.
    ref = args.get("ref")
    owned = conn.execute("SELECT kind FROM artifacts WHERE run_id=? AND artifact_ref=?", (root["run_id"], ref)).fetchone()
    if not owned:
        return
    body = json.loads(call["result_json"])
    if not isinstance(body, dict):
        return
    if "endpoint_offset" in args and body.get("schema") == endpoints.PAGE and body.get("source_ref") == ref:
        proofs = _delivered(body, ref)
    elif "offset" not in args and owned["kind"] == "module_report_bundle" and body.get("schema") == endpoints.REPORT:
        if not conn.execute("SELECT 1 FROM v62_module_reports WHERE run_id=? AND report_ref=?", (root["run_id"], ref)).fetchone():
            return
        proofs = _delivered(body, ref)
    elif "offset" not in args and owned["kind"] == "research_brief":
        if not conn.execute("""SELECT 1 FROM v6_decision_tasks t JOIN research_contexts c USING(context_ref)
             WHERE t.run_id=? AND json_extract(c.snapshot_json,'$.full_brief_ref')=? LIMIT 1""",
             (root["run_id"],ref)).fetchone():
            return
        proofs = _packet(conn, body)
    else:
        return
    _save(conn, root, turn, item["id"], "artifact_read", proofs)


def received(conn, *, run_id, task_id, lease_id, thread_id, turn_id, aliases=None):
    """Resolve only trusted values on this actor's acknowledged native turns up to the requested turn."""
    result, cache = {}, {}
    query = """SELECT e.event_id,e.payload_json FROM events e WHERE e.run_id=? AND e.kind=? AND e.actor=?
        AND json_extract(e.payload_json,'$.task_id')=? AND json_extract(e.payload_json,'$.lease_id')=?
        AND EXISTS (SELECT 1 FROM p0_root_deliveries d WHERE d.thread_id=e.actor AND d.run_id=e.run_id
          AND d.turn_id=json_extract(e.payload_json,'$.turn_id') AND d.method='turn/start' AND d.state='acknowledged'
          AND d.rowid<=(SELECT MAX(rowid) FROM p0_root_deliveries WHERE run_id=e.run_id AND thread_id=e.actor
                       AND turn_id=? AND state='acknowledged' AND method='turn/start')) ORDER BY e.event_id"""
    for event in conn.execute(query, (run_id, EVENT, thread_id, task_id, lease_id, turn_id)):
        receipt = json.loads(event["payload_json"])
        for proof in receipt.pop("endpoints"):
            source = proof["source_ref"]
            if external := proof.get("external_endpoint"):
                result[external["metric_ref"]] = {**receipt, "source_ref":source, "event_id":event["event_id"],
                                                "external_endpoint":external}
                continue
            if aliases is not None:
                # The decision owner resolved its immutable, visible source set before the writer.
                ref = proof.get("metric_ref") or aliases.get(source + "#" + proof["row"]["key"])
                if ref:
                    result[ref] = {**receipt, "source_ref":source, "event_id":event["event_id"]}
                continue
            if source not in cache:
                metrics = endpoints.registered(conn, run_id, source)
                cache[source] = {m["metric_ref"]:row for m, row in zip(metrics, endpoints.compact_rows(metrics))}
            for ref, row in cache[source].items():
                if ref == proof.get("metric_ref") or row == proof.get("row"):
                    result[ref] = {**receipt, "source_ref":source, "event_id":event["event_id"]}
    return result
