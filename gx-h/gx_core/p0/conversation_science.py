"""Read the scientific record available to the PI, independent of any physical Main thread."""
import json

from ..artifacts import ArtifactService
from ..errors import SchemaInvalid
from . import research_report


def latest_receipt(conn, run_id):
    row = conn.execute("""SELECT d.receipt_json FROM v6_decisions d
        JOIN v6_decision_tasks t USING(run_id,task_id) JOIN v6_goals g ON g.run_id=d.run_id
        WHERE d.run_id=? AND t.scope='run'
        AND ((g.status='CLOSED' AND g.stop_decision_ref=d.decision_ref)
          OR (g.status!='CLOSED' AND g.goal_version=t.goal_version)) ORDER BY d.rowid DESC LIMIT 1""", (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _routes(conn, rid, version, cursor, *, current):
    result = {}
    for row in conn.execute("""SELECT m.branch_id,m.scope,m.module_instance_id,m.focus,m.expected_delivery,
               m.round,m.status,e.event_id,e.actor FROM v62_modules m JOIN events e ON e.run_id=m.run_id
               AND e.kind='v62.module.opened' AND e.subject_id=m.module_instance_id
               WHERE m.run_id=? AND m.goal_version=? AND e.event_id<=? ORDER BY e.event_id""",
                            (rid, version, cursor)):
        item = dict(row)
        if not current:
            item.pop("status")
        result[row["branch_id"]] = item
    for row in conn.execute("""SELECT b.branch_id,b.spec_json,b.decision_ref,b.status,
                json_extract(d.receipt_json,'$.event_id') AS event_id FROM v6_branches b
                JOIN v6_decisions d ON b.decision_ref=d.decision_ref AND b.run_id=d.run_id
                JOIN v6_decision_tasks t USING(run_id,task_id)
                WHERE b.run_id=? AND t.scope='run' AND t.goal_version=?
                AND json_extract(d.receipt_json,'$.event_id')<=? ORDER BY d.rowid""", (rid, version, cursor)):
        spec = json.loads(row["spec_json"])
        result[row["branch_id"]] = {"branch_id": row["branch_id"], "scope": "run", "event_id": row["event_id"],
                                    "decision_ref": row["decision_ref"], "hypothesis": spec.get("hypothesis"),
                                    "alternative": spec.get("alternative"), "method_family": spec.get("method_family"),
                                    **({"status":row["status"]} if current else {})}
    return result


def _reports(conn, run, version, cursor, branch):
    # A submitted bundle becomes shared at its release event, possibly later than publication.
    rows = conn.execute("""SELECT m.branch_id,m.scope,r.report_ref,r.receipt_json,min(w.event_id) AS release_event
        FROM v62_module_wakes w JOIN events e ON e.event_id=w.event_id
        JOIN json_each(e.payload_json,'$.module_instance_ids') published
        JOIN v62_modules m ON m.run_id=w.run_id AND m.module_instance_id=published.value
        JOIN v62_module_reports r USING(module_instance_id,run_id)
        WHERE w.run_id=? AND m.goal_version=? AND w.event_id<=? AND (? IS NULL OR m.branch_id=?)
        GROUP BY r.report_ref ORDER BY release_event DESC""", (run, version, cursor, branch, branch))
    results = []
    artifacts = ArtifactService(conn)
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        result = {"report_ref": row["report_ref"], "published_event": receipt["event_id"],
                  "release_event": row["release_event"], "branch_id": row["branch_id"],
                  "author_scope": row["scope"], "status": receipt["status"]}
        if len(results) < 3:
            bundle = artifacts.read_json(row["report_ref"])
            result["findings"] = [{k: f[k] for k in ("kind","text","source_refs","metric_refs") if k in f}
                                  for f in bundle.get("findings", ())[:3]]
        results.append(result)
    return results


def _decision(conn, row, branch, branch_reports, cache, detailed):
    receipt = json.loads(row["receipt_json"])
    proposal = json.loads(row["request_json"])["decision"]
    ops = [item["spec"] for item in receipt.get("resolved_ops", ())]
    sources = proposal.get("evidence_refs", [])
    related = [op for op in ops if branch in (op.get("branch"), op.get("branch_id"))] if branch else ops
    if branch and not (related or branch in sources or branch_reports.intersection(sources) or
                       branch == proposal.get("affected_scope")):
        return None
    ref = row["decision_ref"]
    result = {"decision_ref": ref, "event_id": receipt["event_id"], "author_scope":"run",
              "author_role":"Scientific Lead", "task_id":row["task_id"], "goal_version":row["goal_version"],
              "rationale":research_report.render_decision(conn, row["run_id"], cache, ref, proposal["rationale"]),
              "evidence_refs":sources, "actions":[{k:op[k] for k in
                  ("op","branch","branch_id","node","capability","question","reason") if k in op} for op in related]}
    if branch:
        result["route_evidence"] = {"operations_on_route": bool(related),
                                    "cited_reports": [r for r in sources if r in branch_reports]}
    if detailed:
        if ref not in cache:
            cache[ref] = research_report.decision_readings(conn, row["run_id"], row, receipt)
        result["used_readings"] = cache[ref]
    return result


def project(conn, rid, *, goal_version=None, branch_id=None, decision_ref=None, before_event=None):
    """Return published decisions and routes through the inclusive event cursor.

    The default view contains the newest Lead decisions. A branch, decision reference or older
    cursor retrieves prior decisions without treating a module's private reasoning as the Lead's.
    """
    goal = conn.execute("SELECT goal,goal_version,status,stop_decision_ref FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
    if goal is None:
        return {}
    close = research_report.completed_close(conn, rid) if goal["status"] == "CLOSED" else None
    effective_version = close["goal_version"] if close else goal["goal_version"]
    version = goal_version if goal_version is not None else effective_version
    if type(version) is not int or not 1 <= version <= goal["goal_version"]:
        raise SchemaInvalid("Choose a Goal version belonging to this Run")
    if before_event is not None and (type(before_event) is not int or before_event < 0):
        raise SchemaInvalid("Supply a nonnegative event cursor")
    tail = conn.execute("SELECT coalesce(max(event_id),0) FROM events WHERE run_id=?", (rid,)).fetchone()[0]
    cursor = tail if before_event is None else min(before_event, tail)
    current = version == effective_version and cursor == tail
    routes = _routes(conn, rid, version, cursor, current=current)
    if branch_id:
        branch_id = next((key for key, row in routes.items() if branch_id in
                          (key, row.get("module_instance_id"), row.get("scope")) and branch_id != "run"), None)
        if branch_id is None:
            raise SchemaInvalid("The selected route does not belong to this Run, Goal version and event cursor")
    reports = _reports(conn, rid, version, cursor, branch_id)
    refs = {row["report_ref"] for row in reports}
    decisions = []
    cache = {}
    rows = conn.execute("""SELECT d.*,t.goal_version FROM v6_decisions d JOIN v6_decision_tasks t USING(run_id,task_id)
        WHERE d.run_id=? AND t.goal_version=? AND t.scope='run' AND json_extract(d.receipt_json,'$.event_id')<=?
        AND (? IS NULL OR d.decision_ref=?) ORDER BY d.rowid DESC""", (rid,version,cursor,decision_ref,decision_ref))
    for row in rows:
        view = _decision(conn, row, branch_id, refs, cache, bool(decision_ref))
        if view:
            decisions.append(view)
        if len(decisions) == 3:
            break
    if decision_ref is not None and not decisions:
        raise SchemaInvalid("The selected Lead decision is not published in this Run, version and route")
    sealed = close and version == close["goal_version"] and cursor >= close["closed_event"]
    if sealed:
        from .intent import read_item
        objective = read_item(conn, rid, "objective", version)["value"]
    elif current:
        objective = goal["goal"]
    else:
        row = conn.execute("""SELECT json_extract(c.snapshot_json,'$.goal') FROM v6_decision_tasks t
            JOIN research_contexts c USING(run_id,context_ref) WHERE t.run_id=? AND t.scope='run'
            AND t.goal_version=? AND c.event_cursor<=? ORDER BY c.event_cursor DESC LIMIT 1""",
                           (rid, version, cursor)).fetchone()
        objective = row[0] if row else None
    return {"run_id":rid, "goal_version":version, "goal":objective, "through_event":cursor,
            **({"goal_status":goal["status"], "stop_decision_ref":goal["stop_decision_ref"]} if current or sealed else {}),
            **({"older_before_event":decisions[-1]["event_id"] - 1} if len(decisions) == 3 else {}),
            "branches":[routes[branch_id]] if branch_id else list(routes.values()), "lead_decisions":decisions,
            "released_reports":reports}
