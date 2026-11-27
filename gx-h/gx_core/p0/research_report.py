"""Export the current Goal conclusion and the evidence the deciding Lead actually received."""
import json
import re

def completed_close(db, rid):
    """Read the decision that actually closed this Run, including older misaligned Goal headers."""
    row = db.execute("""
        SELECT t.goal_version, d.decision_ref, g.stop_op_digest AS op_digest,
               op.value AS resolved_op, requested.event_id AS requested_event,
               committed.event_id AS committed_event, closed.event_id AS closed_event,
               closed.ts AS closed_utc
        FROM v6_goals g
        JOIN v6_decisions d ON d.run_id=g.run_id AND d.decision_ref=g.stop_decision_ref
        JOIN v6_decision_tasks t ON t.task_id=d.task_id AND t.run_id=g.run_id
            AND t.scope='run' AND t.goal_version<=g.goal_version
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
            AND json_extract(d.receipt_json,'$.goal_version')=t.goal_version
            AND json_extract(d.receipt_json,'$.outcome')='adopted'
            AND requested.event_id < committed.event_id AND committed.event_id < closed.event_id
        ORDER BY closed.event_id DESC LIMIT 1
        """,(rid,)).fetchone()
    if row is None:
        raise ValueError("Missing enacted close for the sealed Goal")
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
    return {**({"report_presentation":adopted["report_presentation"]} if "report_presentation" in adopted else {}),
            "decision_ref":d["decision_ref"], "task_id":d["task_id"], "thread_id":d["thread_id"],
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


def _gap_questions(conn, rid, decision_ref, texts):
    refs = set(re.findall(r"gap:[0-9a-f]{64}", "\n".join(texts)))
    if not refs:
        return {}
    questions = {r["gap_id"]: r["question"] for r in conn.execute(
        "SELECT gap_id,question FROM gaps WHERE run_id=? AND namespace=?", (rid, rid)) if r["gap_id"] in refs}
    for r in conn.execute("""SELECT json_extract(g.value,'$.gap_ref'),json_extract(g.value,'$.question')
            FROM v6_decisions d JOIN research_contexts c USING(run_id,context_ref),
            json_each(c.snapshot_json,'$.research_progress.open_gaps') g
            WHERE d.run_id=? AND d.decision_ref=?""", (rid, decision_ref)):
        if r[0] in refs and r[1]:
            questions[r[0]] = r[1]
    return questions


def render_close(conn, rid, final, questions):
    ref = final["decision_ref"]
    cache = {ref: final["used_endpoints"]}
    fields = [final["reason"], final["rationale"], *final["unresolved"]]
    questions.update(_gap_questions(conn, rid, ref, fields))
    def display(text):
        rendered = render_decision(conn, rid, cache, ref, text)
        for key, question in questions.items():
            rendered = rendered.replace(key, question)
        return rendered
    return {"reason": display(final["reason"]), "rationale": display(final["rationale"]),
            "unresolved": [display(item) for item in final["unresolved"]]}


def _decisions_after_reading(conn, rid, close, work, cache):
    reports = {}
    for handoff in work["handoffs"]:
        if handoff.get("decision_ref") and handoff.get("received_content"):
            reports.setdefault(handoff["decision_ref"], {})[handoff["published_event"]] = handoff["status"]
    actions = []
    for row in conn.execute("""SELECT d.*,t.scope FROM v6_decisions d
            JOIN v6_decision_tasks t USING(run_id,task_id) WHERE d.run_id=? AND t.goal_version=?
            ORDER BY json_extract(d.receipt_json,'$.event_id')""", (rid, close["goal_version"])):
        ref = row["decision_ref"]
        receipt = json.loads(row["receipt_json"])
        if receipt["event_id"] >= close["committed_event"]:
            continue
        metrics = (receipt.get("endpoint_use") or {}).get("used_metric_refs", [])
        if not metrics and ref not in reports:
            continue
        proposal = json.loads(row["request_json"])["decision"]
        nodes = [dict(n) for n in conn.execute("""SELECT n.node_id,n.job_id,j.capability_id,j.status
                 FROM v6_nodes n LEFT JOIN jobs j ON n.job_id=j.job_id WHERE n.run_id=? AND n.decision_ref=?
                 ORDER BY n.rowid""", (rid, ref))]
        operations = [op["spec"] for op in receipt.get("resolved_ops", [])]
        launched = {n["node_id"] for n in nodes if n["job_id"]}
        objectives = [op.get("question") for op in operations if op.get("node") in launched]
        if not any(objectives):
            objectives = [op.get("question") or op.get("reason") for op in operations]
        actions.append({"decision_ref":ref, "event_id":receipt["event_id"], "scope":row["scope"],
                        "outcome":receipt.get("outcome"), "metric_refs":metrics, "report_events":sorted(reports.get(ref, ())),
                        "report_statuses":reports.get(ref, {}),
                        "operations":operations, "jobs":nodes,
                        "objectives":[render_decision(conn, rid, cache, ref, s) for s in objectives if s],
                        "claim_revisions":receipt.get("claim_revisions", []), "rationale":proposal["rationale"],
                        "rendered_rationale":render_decision(conn, rid, cache, ref, proposal["rationale"])})
    return actions


def _rejections(conn, rid, closed_event):
    corrected, rejected = {}, []
    for row in conn.execute("""SELECT event_id,kind,subject_id,payload_json FROM events WHERE run_id=?
            AND kind IN ('v624.decision.rejected','v624.decision.corrected') AND event_id<=?
            ORDER BY event_id DESC""", (rid, closed_event)):
        body = json.loads(row["payload_json"])
        if row["kind"] == "v624.decision.corrected":
            corrected[row["subject_id"]] = body.get("decision_event")
        else:
            rejected.append({**body, "event_id":row["event_id"],
                             "corrected_decision_event":corrected.get(row["subject_id"])})
    return list(reversed(rejected))


def export(conn, rid, directory, invocation):
    from ..native_release import write_native_bytes
    from .intent import read_item
    from .research_evidence import evidence
    from .research_page import page
    from .research_routes import routes
    conn.execute("SAVEPOINT research_report")
    try:
        close = completed_close(conn, rid)
        final = closing_decision(conn, rid, close)
        goal = read_item(conn, rid, "objective", close["goal_version"])["value"]
        science = evidence(conn, rid, through_event=close["closed_event"])
        work = routes(conn, rid, directory, goal_version=close["goal_version"])
        cache = {final["decision_ref"]: final["used_endpoints"]}
        actions = _decisions_after_reading(conn, rid, close, work, cache)
        texts = [text for item in actions for text in (item["rendered_rationale"], *item["objectives"])]
        questions = _gap_questions(conn, rid, final["decision_ref"], texts)
        final["rendered"] = render_close(conn, rid, final, questions)
        final["gap_questions"] = questions
        rejected = _rejections(conn, rid, close["closed_event"])
    finally:
        conn.execute("RELEASE research_report")
    result = {"schema":"gx.research-result/1", "run_id":rid, "goal":goal, "goal_status":"CLOSED",
              "goal_version":close["goal_version"], "closure":close, "final_decision":final,
              **science, "routes":work, "post_read_actions":actions, "rejected_decisions":rejected, "invocation":invocation}
    if preference := final.get("report_presentation"):
        result["presentation"] = preference
    predecessor = conn.execute("SELECT json_extract(inputs_json,'$.inputs.p0_spec.predecessor') "
                               "FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
    if predecessor:
        prior = json.loads(predecessor)
        result["prior_study"] = {k:prior[k] for k in (
            "parent_run_id", "parent_decision_ref", "parent_goal_version", "request_message_id", "selected_materials")}
    out = directory/"research"/rid
    out.mkdir(parents=True, exist_ok=True)
    files = {"REPORT.md": page(result), "result.json": json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n"}
    readers = []
    for row in conn.execute("""SELECT c.value FROM events e, json_each(e.payload_json,'$.clauses') c
            WHERE e.run_id=? AND e.event_id>? AND e.kind='pi.message.routed'
            AND json_extract(c.value,'$.decision_ref')=? AND json_type(c.value,'$.reader')='object'
            ORDER BY e.event_id,c.key""", (rid, close["closed_event"], final["decision_ref"])):
        clause = json.loads(row[0])
        name = "READER." + clause["report_language"] + "." + clause["clause_id"] + ".md"
        translation = clause["reader"]
        zh = clause["report_language"] == "zh"
        lines = ["# 结题决定译读本" if zh else "# Translated closing decision", "",
                 translation["reason"], "", translation["rationale"], "",
                 *("- " + item for item in translation["unresolved"]), "",
                 f"{final['decision_ref']} · " + ("[封存原报告](REPORT.md) · [原件与决定](result.json)" if zh else
                                                   "[Sealed report](REPORT.md) · [Evidence and decision](result.json)")]
        if final["evidence_refs"]:
            lines.extend(["", "原件与产物引用" if zh else "Evidence references", "",
                          *("- " + ref for ref in final["evidence_refs"])])
        files[name] = "\n".join(lines) + "\n"
        readers.append({"file": str(out/name), "source_message_id": clause["source_message_id"],
                        "language": clause["report_language"], "decision_ref": final["decision_ref"]})
    for name, data in files.items():
        target = out/name
        encoded = data.encode()
        if not target.is_file() or target.read_bytes() != encoded:
            write_native_bytes(target, encoded)
    return {**({"readers": readers} if readers else {}), "report":str(out/"REPORT.md"), "result":str(out/"result.json"), "invocation":invocation}
