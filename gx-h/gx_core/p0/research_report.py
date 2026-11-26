"""Export the current Goal conclusion and the evidence the deciding Lead actually received."""
from functools import partial
import json

def completed_close(db, rid):
    """Resolve the Goal owner's closing operation to its commit and completion events."""
    row = db.execute("""
        SELECT g.goal_version, d.decision_ref, g.stop_op_digest AS op_digest,
               op.value AS resolved_op, requested.event_id AS requested_event,
               committed.event_id AS committed_event, closed.event_id AS closed_event,
               closed.ts AS closed_utc
        FROM v6_goals g
        JOIN v6_decisions d ON d.run_id=g.run_id AND d.decision_ref=g.stop_decision_ref
        JOIN v6_decision_tasks t ON t.task_id=d.task_id AND t.run_id=g.run_id
            AND t.scope='run' AND t.goal_version=g.goal_version
        JOIN json_each(d.receipt_json,'$.resolved_ops') op
            ON json_extract(op.value,'$.spec.op')='close_run'
            AND json_extract(op.value,'$.op_digest')=g.stop_op_digest
        JOIN events requested ON requested.run_id=g.run_id AND requested.subject_id=g.run_id
            AND requested.kind='v6.run.close_requested'
            AND json_extract(requested.payload_json,'$.decision_id')=d.decision_ref
            AND json_extract(requested.payload_json,'$.op_digest')=g.stop_op_digest
        JOIN events committed ON committed.run_id=g.run_id AND committed.subject_id=d.decision_ref
            AND committed.kind='v6.decision.committed'
            AND committed.event_id=json_extract(d.receipt_json,'$.event_id')
        JOIN events closed ON closed.run_id=g.run_id AND closed.subject_id=g.run_id
            AND closed.kind='v6.run.closed'
            AND json_extract(closed.payload_json,'$.decision_id')=d.decision_ref
            AND json_extract(closed.payload_json,'$.op_digest')=g.stop_op_digest
        WHERE g.run_id=? AND g.status='CLOSED'
            AND json_extract(d.receipt_json,'$.goal_version')=g.goal_version
            AND json_extract(d.receipt_json,'$.outcome')='adopted'
            AND requested.event_id < committed.event_id AND committed.event_id < closed.event_id
        ORDER BY closed.event_id DESC LIMIT 1
        """,(rid,)).fetchone()
    if row is None:
        raise ValueError("Missing enacted close for the current Goal version")
    closure = dict(row)
    closure["operation"] = json.loads(closure.pop("resolved_op"))["spec"]
    return closure


def decision_readings(conn, rid, d, adopted):
    from . import v623_endpoints
    used = adopted.get("endpoint_use", {})
    readings = []
    registered = {}
    for metric_ref in used.get("used_metric_refs", []):
        proof = used["receipts"].get(metric_ref, {})
        source = proof.get("source_ref")
        row = conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND event_id=?
              AND kind='v623.endpoints.received' AND actor=? AND event_id<?""",
              (rid, proof.get("event_id"), d["thread_id"], adopted["event_id"])).fetchone()
        delivery = json.loads(row[0]) if row else {}
        if (delivery.get("task_id") != d["task_id"] or delivery.get("lease_id") != d["lease_id"]
                or delivery.get("turn_id") != proof.get("turn_id") or delivery.get("receipt") != proof.get("receipt")):
            raise ValueError("Decision is missing its recorded endpoint delivery for "+metric_ref)
        if source not in registered:
            metrics = v623_endpoints.registered(conn, rid, source)
            registered[source] = {m["metric_ref"]: (m, r) for m,r in zip(metrics,v623_endpoints.compact_rows(metrics))}
        metric, compact = registered[source].get(metric_ref, ({}, None))
        found = None
        for entry in delivery.get("endpoints", []):
            if entry.get("source_ref") != source:
                continue
            if entry.get("external_endpoint", {}).get("metric_ref") == metric_ref:
                found = entry["external_endpoint"]
                break
            if metric and (entry.get("metric_ref") == metric_ref or compact == entry.get("row")):
                found = metric
                break
        if found is None:
            raise ValueError("Decision lacks its cited delivered endpoint "+metric_ref)
        readings.append({"metric_ref":metric_ref, "source_ref":source, "event_id":proof["event_id"], "reading":found})
    return readings


def closing_decision(conn, rid, close):
    d = conn.execute("SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                     (rid, close["decision_ref"])).fetchone()
    adopted = json.loads(d["receipt_json"])
    proposal = json.loads(d["request_json"])["decision"]
    readings = decision_readings(conn, rid, d, adopted)
    return {"decision_ref":d["decision_ref"], "task_id":d["task_id"], "thread_id":d["thread_id"],
            "turn_id":d["turn_id"], "rationale":proposal["rationale"], "reason":close["operation"]["reason"],
            "unresolved":close["operation"].get("unresolved", []), "claim_refs":close["operation"].get("claim_refs", []),
            "evidence_refs":adopted.get("evidence_refs", proposal.get("evidence_refs", [])),
            "stop_reason":adopted.get("stop_reason"), "used_endpoints":readings}


def render_decision(conn, rid, cache, ref, text):
    """Use only immutable measurement rows acknowledged and selected by this decision."""
    from . import v621_metrics
    if "{{metric:" not in text:
        return text
    if ref not in cache:
        row = conn.execute("SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?", (rid, ref)).fetchone()
        if row is None:
            raise ValueError("Missing adopted decision for the report: "+ref)
        cache[ref] = decision_readings(conn, rid, row, json.loads(row["receipt_json"]))
    measured = [entry["reading"] for entry in cache[ref] if entry["reading"].get("schema") == v621_metrics.SCHEMA]
    return v621_metrics.render_accepted_text(text, measured)


def export(conn, rid, directory, invocation):
    from ..native_release import write_native_bytes
    from .research_evidence import evidence, evidence_lines
    from .research_routes import routes, route_lines
    conn.execute("SAVEPOINT research_report")
    try:
        close = completed_close(conn, rid)
        final = closing_decision(conn, rid, close)
        goal = conn.execute("SELECT goal FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0]
        science = evidence(conn, rid)
        work = routes(conn, rid, directory)
        display = partial(render_decision, conn, rid, {final["decision_ref"]: final["used_endpoints"]})
        final_text = {key:display(final["decision_ref"], final[key]) for key in ("rationale","reason")}
        final_text["unresolved"] = [display(final["decision_ref"], item) for item in final["unresolved"]]
        final["rendered"] = final_text
        route_text = route_lines(work, display)
    finally:
        conn.execute("RELEASE research_report")
    result = {"schema":"gx.research-result/1", "run_id":rid, "goal":goal, "goal_status":"CLOSED",
              "goal_version":close["goal_version"], "closure":close, "final_decision":final,
              **science, "routes":work, "invocation":invocation}
    lines = ["# Research report", "", final_text["rationale"], ""]
    if final_text["reason"] != final_text["rationale"]:
        lines.extend([final_text["reason"], ""])
    if final_text["unresolved"]:
        lines.extend(["## Scope and limitations", "", *("- "+item for item in final_text["unresolved"]), ""])
    lines.extend(["## Evidence used at closure", ""])
    for item in final["used_endpoints"]:
        reading = item["reading"]
        value = reading.get("display") or str(reading.get("value"))
        lines.append(f"- {reading.get('metric_id', item['metric_ref'])} · {value} · {item['source_ref']} "
                     f"(delivery {item['event_id']}, {item['metric_ref']})")
    if not final["used_endpoints"]:
        lines.append("No quantitative endpoint was cited in the accepted closing decision.")
    lines.extend(["", *evidence_lines(science), "", *route_text, "", "## Run and recovery", "",
                  f"Run {rid}, Goal version {close['goal_version']}. Decision {close['decision_ref']} "
                  f"committed at event {close['committed_event']}; closed at event {close['closed_event']}.",
                  f"Invocation {invocation['action']} added {invocation['added']['host_turns']} acknowledged host turns "
                  f"and {invocation['added']['jobs']} Jobs.", "", "```sh", invocation["command"], "```"])
    out = directory/"research"/rid
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("REPORT.md", "\n".join(lines)+"\n"),
                       ("result.json", json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n")):
        target = out/name
        encoded = data.encode()
        if not target.is_file() or target.read_bytes() != encoded:
            write_native_bytes(target, encoded)
    return {"report":str(out/"REPORT.md"), "result":str(out/"result.json"), "invocation":invocation}
