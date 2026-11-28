"""Evidence at a commissioned researcher's final callback and the Lead's factual claim check."""
import json

from ..artifacts import ArtifactService
from ..errors import SchemaInvalid
from ..persistence import store


ADVISORY = "research.close.module_advisory"


def module_advisory(conn, task, module, report_ref):
    """Called once in the transaction publishing the stable assignment's single signed product."""
    from . import research_closeout
    visible = set(json.loads(task["visible_refs_json"]))
    outstanding = {}
    for item in research_closeout.owned_materials(conn, task, visible):
        result = research_closeout._state(conn, task, item, visible)
        if result:
            state, ref = result
            key = (item["question_id"], ref, item.get("segment_id"), item.get("candidate_id"))
            outstanding[key] = {"question_id":item["question_id"], "ref":ref, "state":state,
                                **{k:item[k] for k in ("segment_id","candidate_id","fetch_job","query_record") if k in item}}
    if not outstanding:
        return None
    # Keep the scoped contents in the original author's callback receipt. Other
    # event consumers need only the stable assignment and the published report.
    mid = module["module_instance_id"]
    eid = store.append_event(conn, run_id=module["run_id"], kind=ADVISORY, actor="GX",
        subject_kind="module", subject_id=mid, payload={"scope":module["scope"], "report_ref":report_ref,
            "goal_version":module["goal_version"]})
    return {"event_id":eid, "module_instance_id":mid, "report_ref":report_ref,
            "materials":list(outstanding.values())[:6], "material_count":len(outstanding),
            "outcome":"report_delivered_with_outstanding_evidence"}


def _commission_ports(conn, rid, mid):
    """Current required ports explicitly tied to this assignment or one of its own nodes."""
    from . import research_joins
    nodes = {row[0] for row in conn.execute("""SELECT n.node_id FROM v6_nodes n JOIN v62_modules m
        USING(run_id,branch_id) WHERE n.run_id=? AND m.module_instance_id=?""", (rid,mid))}
    ports = []
    for join in research_joins.view(conn, rid):
        spec = join["spec"]
        for port in spec["ports"]:
            if not port["required"] and spec["mode"] != "all":
                continue
            own = [alt for alt in port["alternatives"]
                   if alt.get("module_instance_id") == mid or alt.get("node_id") in nodes]
            if own:
                ports.append((join["key"],join["revision"],port["slot"],own))
    return ports


def completed_delivery(conn, task, check, *, refs):
    """Identify a released report from exactly this assignment, including optional actual named output."""
    from . import v62_reports
    rid, mid, ref = task["run_id"], check["module_instance_id"], check["source_ref"]
    row = conn.execute("""SELECT m.status,r.receipt_json,a.sha256 FROM v62_modules m
        JOIN v62_module_reports r USING(run_id,module_instance_id)
        JOIN artifacts a ON a.run_id=r.run_id AND a.artifact_ref=r.report_ref AND a.published=1
        WHERE m.run_id=? AND m.goal_version=? AND m.module_instance_id=? AND r.report_ref=?""",
                       (rid,task["goal_version"],mid,ref)).fetchone()
    if not row or mid not in v62_reports._released(conn, rid, through_cursor=task["event_cursor"]):
        raise SchemaInvalid("Delivery grade requires the released report of this exact commission")
    receipt = json.loads(row["receipt_json"])
    if (row["status"] != "COMPLETE" or receipt["author"]["kind"] != "MODULE"
            or receipt["report_sha256"] != row["sha256"]):
        raise SchemaInvalid("Only a complete researcher-authored delivery can receive this grade")
    bundle = json.loads(ArtifactService(conn).read_bytes(ref, expected_sha256=row["sha256"]))
    if not bundle["sources"] or not (bundle["findings"] or bundle.get("verified_metrics") or
                                      bundle.get("researcher_statement", {}).get("qualitative_notes")):
        raise SchemaInvalid("A complete delivery grade requires a sourced finding or named measurement")
    proof = {**check, "report_sha256":row["sha256"], "author":receipt["author"], "status":"COMPLETE",
             "scope":"researcher_report", "recorded_gaps":bundle["gaps"]}
    # Scope the signed grade to the exact product, never to the author's prose.
    # An input read, or a generic COMPLETE envelope, cannot stand in for a Job.
    named = {}
    for source in bundle["sources"]:
        source_ref = source["artifact_ref"]
        if source_ref not in refs:
            continue
        for job in source["receipts"]:
            if job.get("job_id") and job.get("output_name"):
                named[source_ref] = {"output_ref":source_ref, "output_sha256":source["sha256"],
                    "output_name":job["output_name"], "job_id":job["job_id"], "node_id":job["node_id"]}
    output = check.get("output_ref")
    if output and output not in named:
        raise SchemaInvalid("The requested product must be a named Job output delivered by this commission")
    if output:
        proof.update(named[output], scope="named_job_output")
    # Reuse the Join owner's distinct-delivery rule for alternatives and required
    # ports. Each independent Join has its own ports; different Joins may reuse a product.
    from . import research_joins
    groups = {}
    for key, revision, slot, alternatives in _commission_ports(conn,rid,mid):
        matches = set()
        for alt in alternatives:
            if alt["output_name"] == "report" and "module_instance_id" in alt:
                matches.add(ref)
            matches.update(art for art,job in named.items() if job["output_name"] == alt["output_name"]
                           and ("node_id" not in alt or alt["node_id"] == job["node_id"]))
        groups.setdefault((key,revision),[]).append({"slot":slot,"required":True,
            "evidence":[{"artifact_ref":art} for art in sorted(matches)]})
    satisfied = []
    for (key,revision), ports in groups.items():
        assigned = research_joins._assign(ports, allow_pending=False)
        if len(assigned) != len(ports):
            raise SchemaInvalid("The commission's required named delivery lacks a cited output of its producing Job",
                                details={"join":key, "missing_slots":[p["slot"] for i,p in enumerate(ports) if i not in assigned]})
        for i, art in assigned.items():
            satisfied.append({"join":key,"revision":revision,"slot":ports[i]["slot"],"artifact_ref":art,
                              **(named[art] if art in named else {})})
    checked_outputs = {p["artifact_ref"] for p in satisfied if p["artifact_ref"] in named}
    if output:
        checked_outputs.add(output)
    if checked_outputs:
        from . import manifest
        assets = ArtifactService(conn)
        for art in checked_outputs:
            manifest.verify_artifact_pin(assets, {"artifact_ref":art,"sha256":named[art]["output_sha256"]})
    if satisfied:
        proof.update(scope="required_ports", satisfied_ports=satisfied)
    elif not output:
        proof["grade"] = "report_received"
    return proof


def query_record_for_ref(conn, run_id, ref):
    """Called outside the writer on a visible artifact nominated or released by its owner."""
    from adapters.scientific.publication_coverage import query_record
    from ..errors import GxError
    from . import manifest
    row = conn.execute("""SELECT a.job_id,a.sha256 FROM artifacts a JOIN jobs j USING(job_id,run_id)
        WHERE a.run_id=? AND a.artifact_ref=? AND a.kind='scholarly_candidates'
        AND a.published=1 AND a.origin='JOB_OUTPUT' AND j.status='SUCCEEDED'""", (run_id,ref)).fetchone()
    if not row:
        return None
    job, digest = row
    result = {"source_ref":ref, "job_id":job, "sha256":digest}
    try:
        record = manifest.read_manifest(conn, job)
        completion = record["completion"]
        if (record["admission"]["run_id"] != run_id or not completion or completion["status"] != "SUCCEEDED"
                or not any(out.get("output_name") and out["artifact_ref"] == ref and out["sha256"] == digest
                           for out in completion["outputs"])):
            raise ValueError("Unsupported Job output")
        document = json.loads(ArtifactService(conn).read_bytes(ref, expected_sha256=digest))
        return {**result, **query_record(document)}
    except (GxError, OSError, ValueError, TypeError, KeyError):
        # A different publisher may use this file kind. Preserve its artifact and
        # distinguish unreadable/unrecognized facts from a genuine zero-row query.
        return {**result, "status":"unavailable", "reason":"unverifiable_query_window"}


def query_records(conn, run_id, checked):
    records = []
    for source in checked:
        if source["kind"] == "scholarly_candidates":
            if record := query_record_for_ref(conn, run_id, source["artifact_ref"]):
                records.append(record)
    return records
