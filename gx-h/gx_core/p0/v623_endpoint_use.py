"""Bind a Lead decision's stated use of concrete endpoints to what reached that actor."""
from ..errors import SchemaInvalid, ScopeDenied


FIELDS = ("comparison_plan", "comparison_id", "disposition", "metric_refs", "affected_scope", "stop_reason")
INCOMPLETE_STOPS = {"UNREAD_EVIDENCE", "INSUFFICIENT_DATA", "BUDGET", "TIME_LIMIT", "OPERATOR"}


def prepare(conn, task):
    """Read immutable published outputs outside the writer, within the frozen task's scope."""
    from . import v623_endpoints

    rid, visible = task["run_id"], task["visible_refs_json"]
    rows = conn.execute("""
        WITH visible(ref) AS (SELECT value FROM json_each(?))
        SELECT m.report_ref AS ref FROM visible
          JOIN v62_module_reports m ON m.report_ref=visible.ref
          JOIN v62_modules owner USING(module_instance_id)
          WHERE m.run_id=? AND owner.goal_version=?
        UNION ALL
        SELECT a.artifact_ref FROM visible JOIN artifacts a ON a.artifact_ref=visible.ref
          WHERE a.run_id=? AND a.origin='JOB_OUTPUT' AND a.published=1
        ORDER BY ref""", (visible, rid, task["goal_version"], rid)).fetchall()
    by_source, aliases = {}, {}
    for row in rows:
        source = row["ref"]
        metrics = v623_endpoints.registered(conn, rid, source)
        if not metrics:
            continue
        by_source[source] = {metric["metric_ref"] for metric in metrics}
        compact = v623_endpoints.compact_rows(metrics)
        for metric, short in zip(metrics, compact, strict=True):
            aliases[source + "#" + short["key"]] = metric["metric_ref"]
    return by_source, aliases


def verify(conn, task, root, turn, decision, inventory, *, closing):
    """Fence actor receipts and resolve explicit citations inside the adoption transaction."""
    from . import v623_exposure

    by_source, aliases = inventory
    all_refs = set().union(*by_source.values())
    exposures = v623_exposure.received(conn, run_id=root["run_id"], task_id=task["task_id"],
                                       lease_id=root["lease_id"], thread_id=root["thread_id"], turn_id=turn,
                                       aliases=aliases)
    received = {ref for ref, proof in exposures.items() if ref in by_source.get(proof["source_ref"], ())}
    used = set()
    for citation in decision.get("metric_refs", []):
        ref = aliases.get(citation, citation)
        if ref not in all_refs:
            raise ScopeDenied("The decision cites an endpoint outside this Goal's frozen Lead scope")
        if ref not in received:
            raise ScopeDenied("The cited endpoint must reach this Lead in a brief or successful endpoint read",
                              details={"metric_ref": ref, "reason": "endpoint_not_received"})
        if ref in used:
            raise SchemaInvalid("Multiple citations resolve to the same concrete endpoint")
        used.add(ref)
    stop_reason = decision.get("stop_reason")
    if stop_reason is not None and not closing:
        raise SchemaInvalid("A stop reason belongs to a decision that closes the Run")
    unread = all_refs - received
    cited_sources = by_source.keys() & set(decision["evidence_refs"])
    missing = sorted(ref for ref in cited_sources if not used.intersection(by_source[ref]))
    if closing and missing and stop_reason not in INCOMPLETE_STOPS:
        raise SchemaInvalid("A cited quantitative source requires a concrete received endpoint, "
                            "or an explicit reason for stopping without using it",
                            details={"reason": "endpoint_use_unspecified", "source_refs": missing})
    if closing and stop_reason == "UNREAD_EVIDENCE" and not unread:
        raise SchemaInvalid("An unread-evidence stop requires a published endpoint this Lead has not received")
    return {"received_metric_refs": sorted(received), "used_metric_refs": sorted(used),
            "unread_metric_refs": sorted(unread),
            "receipts": {ref: exposures[ref] for ref in sorted(received)},
            **({"stop_reason": stop_reason} if stop_reason else {})}
