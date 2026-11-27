"""Bind a Lead decision's stated use of concrete endpoints to what reached that actor."""
from ..errors import SchemaInvalid, ScopeDenied


FIELDS = ("comparison_plan", "comparison_id", "disposition", "metric_refs", "affected_scope", "stop_reason")
INCOMPLETE_STOPS = {"UNREAD_EVIDENCE", "INSUFFICIENT_DATA", "BUDGET", "TIME_LIMIT", "OPERATOR"}
MAX_MISSING_FIELDS = 16


def prepare(conn, task, *, metric_details=None):
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
        if metric_details is not None:
            metric_details.update((m["metric_ref"], m) for m in metrics)
        compact = v623_endpoints.compact_rows(metrics)
        for metric, short in zip(metrics, compact, strict=True):
            aliases[source + "#" + short["key"]] = metric["metric_ref"]
    return by_source, aliases


def resolve_delivered(conn, task, root, turn, decision, inventory, *, received_metrics=None):
    """Resolve each selected wire key against its own officially delivered source."""
    from . import v623_exposure

    by_source, aliases = inventory
    proofs = v623_exposure.received(conn, run_id=root["run_id"], task_id=task["task_id"],
                                    lease_id=root["lease_id"], thread_id=root["thread_id"], turn_id=turn,
                                    aliases=aliases, by_source=True)
    delivered = {key: proof for key, proof in proofs.items() if key[1] in by_source.get(key[0], ())}
    if received_metrics is not None:
        received_metrics.update((ref, proof) for (_, ref), proof in delivered.items())
    sources = set(decision.get("evidence_refs", ()))
    selected = {}
    for citation in decision.get("metric_refs", ()):
        matches = [(source, ref) for source, ref in delivered
                   if citation == ref or aliases.get(source + "#" + citation) == ref or
                   (citation.startswith(source + "#") and aliases.get(citation) == ref)]
        if len(matches) > 1 and sources:
            scoped = [key for key in matches if key[0] in sources]
            if scoped:
                matches = scoped
        if not matches:
            raise ScopeDenied("Select a row delivered to this Lead, or read the source's endpoint page first",
                              details={"reason": "endpoint_not_received", "field": "metric_refs",
                                       "citation": citation})
        if len(matches) > 1:
            choices = {key for key, ref in aliases.items() if (key.rsplit("#", 1)[0], ref) in matches}
            raise SchemaInvalid("Choose the intended delivered source and row",
                                details={"reason": "endpoint_ambiguous", "field": "metric_refs",
                                         "options": sorted(choices)})
        key, = matches
        source, ref = key
        if ref in selected:
            raise SchemaInvalid("Select each concrete endpoint once",
                                details={"reason": "duplicate_endpoint", "field": "metric_refs"})
        selected[ref] = delivered[key]
    if "metric_refs" in decision:
        decision["metric_refs"] = list(selected)
        decision["evidence_refs"] = list(dict.fromkeys([*decision["evidence_refs"],
                                                       *(p["source_ref"] for p in selected.values())]))
    return selected


def verify(conn, task, root, turn, decision, inventory, *, closing, selected=None):
    """Fence actor receipts and resolve explicit citations inside the adoption transaction."""
    from . import v623_exposure

    by_source, aliases = inventory
    all_refs = set().union(*by_source.values())
    deliveries = v623_exposure.received(conn, run_id=root["run_id"], task_id=task["task_id"],
                                        lease_id=root["lease_id"], thread_id=root["thread_id"], turn_id=turn,
                                        aliases=aliases, by_source=selected is not None)
    if selected is None:
        exposures = deliveries
    else:
        exposures = {ref: proof for (source, ref), proof in deliveries.items() if ref in by_source.get(source, ())}
        # Keep the exact source/official delivery selected in prepare. A second delivery
        # of the same immutable endpoint via another report cannot substitute for it.
        for ref, proof in selected.items():
            current = deliveries.get((proof["source_ref"], ref))
            if current is None or current["event_id"] < proof["event_id"]:
                raise ScopeDenied("The selected endpoint delivery is no longer in the deciding Lead's scope",
                                  details={"reason": "endpoint_not_received", "field": "metric_refs"})
        exposures.update(selected)
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
    missing = {ref for ref in cited_sources if not used.intersection(by_source[ref])}
    if closing and missing and stop_reason not in INCOMPLETE_STOPS:
        positions = [i for i, ref in enumerate(decision["evidence_refs"]) if ref in missing]
        fields = [f"evidence_refs[{i}]" for i in positions[:MAX_MISSING_FIELDS]]
        details = {"reason": "endpoint_use_unspecified", "source_refs": sorted(missing),
                   "field": fields[0], "missing_fields": fields}
        if len(positions) > len(fields):
            details["more_missing_fields"] = len(positions) - len(fields)
        raise SchemaInvalid("For each listed source you used, keep it in evidence_refs and add a received "
                            "endpoint (source#row) from it to metric_refs. Drop unused sources from evidence_refs. "
                            "For an incomplete close, give the applicable stop_reason.", details=details)
    if closing and stop_reason == "UNREAD_EVIDENCE" and not unread:
        raise SchemaInvalid("An unread-evidence stop requires a published endpoint this Lead has not received")
    return {"received_metric_refs": sorted(received), "used_metric_refs": sorted(used),
            "unread_metric_refs": sorted(unread),
            "receipts": {ref: exposures[ref] for ref in sorted(received)},
            **({"stop_reason": stop_reason} if stop_reason else {})}
