"""Lead visibility and delivery size for a Goal with research modules."""
from ..artifacts import ArtifactService
from ..domain import ids
from ..persistence import store


def internal_event(row, branches):
    """Module observations become Lead evidence when the owner releases their report."""
    p = store.loads(row, "payload_json")
    if row["kind"] == "v6.decision.superseded":
        return str(p.get("cause_scope", "")).startswith("module:")
    if row["kind"] not in {"v6.node.settled", "v6.observation.added", "v6.claim.adopted"}:
        return False
    return p.get("branch_id") in branches or p.get("scope") in branches or str(p.get("scope", "")).startswith("module:")


def branch_ids(conn, run_id):
    return {row[0] for row in conn.execute("SELECT branch_id FROM v62_modules WHERE run_id=?", (run_id,))}


def attach_reports(conn, brief, cursor, policy):
    from . import v62_reports
    reports, report_index = v62_reports.report_views(conn, brief["run_id"], through_cursor=cursor,
                                                     per_report_bytes=policy["report_bytes"], total_bytes=policy["brief_bytes"])
    brief["module_reports"] = reports
    if report_index:
        brief["module_report_index"] = report_index
    released = v62_reports.released_evidence_refs(conn, brief["run_id"], through_cursor=cursor)
    brief["evidence_refs"] = list(dict.fromkeys([*brief["evidence_refs"], *released]))
    return released


def _size(obj):
    return len(ids.canonical_json(obj).encode("utf-8"))


def _excerpt(value, limit):
    raw = value.encode("utf-8")
    return value if len(raw) <= limit else raw[:limit - 3].decode("utf-8", errors="ignore") + "…"


def describe_refresh(conn, brief, events):
    """Identify what changed since the replaced Lead brief in this frozen event view."""
    from . import v621_policy
    if brief.get("scope") != "run" or brief.get("evidence_policy") != v621_policy.VERSION:
        return
    marker = next((row for row in reversed(events) if row["kind"] == "v6.decision.superseded"), None)
    if not marker:
        return
    cause = store.loads(marker, "payload_json")
    if (cause.get("reason") not in {"close_context_changed", "convergence_context_changed", "comparison_context_changed"}
            or cause.get("scope") != "run" or cause.get("goal_version", brief["goal_version"]) != brief["goal_version"]):
        return
    # The previous accepted decision can be older than several consecutive stale attempts.
    # Use the replaced task's cursor and do not re-announce a previously delivered refresh.
    task = conn.execute("SELECT event_cursor FROM v6_decision_tasks JOIN research_contexts USING(context_ref) "
                        "WHERE task_id=? AND v6_decision_tasks.run_id=? AND scope='run' AND goal_version=?",
                        (marker["subject_id"], brief["run_id"], brief["goal_version"])).fetchone()
    through = brief["trigger"]["through_cursor"]
    if not task or conn.execute("SELECT 1 FROM v6_decision_tasks t JOIN research_contexts c USING(context_ref) "
            "WHERE t.run_id=? AND scope='run' AND goal_version=? AND c.event_cursor>=? AND c.event_cursor<? LIMIT 1",
            (brief["run_id"], brief["goal_version"], marker["event_id"], through)).fetchone():
        return
    after = task["event_cursor"]
    changes = {c["event_id"]: c for c in brief["trigger"]["changes"] if after < c["event_id"] <= through}
    visible = set(brief["evidence_refs"])
    current = {claim["hypothesis_id"]: claim for claim in brief["claims"]}
    for row in events:
        if row["kind"] != "v62.claim.reassessed" or row["event_id"] not in changes:
            continue
        data = store.loads(row, "payload_json")
        claim = current.get(row["subject_id"])
        if not claim or data.get("scope") != "run":
            continue
        item = changes[row["event_id"]]
        item.update({k: data[k] for k in ("before", "after", "direction", "previous_claim_ref") if k in data})
        revision = data.get("claim_revision_id")
        if revision in visible:
            item["claim_revision_id"] = revision
            if revision == claim["claim_revision_id"]:
                item["statement"] = _excerpt(claim["statement"], 192)
    # Use immutable event-time readings and verdicts; the compatibility evidence.status is mutable.
    sources = conn.execute("""SELECT x.event_id,x.kind,x.subject_id,
          json_extract(x.payload_json,'$.target_claim_ref') target, a.subject_id extraction_ref,
          s.blob_artifact_ref source_ref, json_extract(e.source_locator_json,'$.segment_id') segment_id,
          e.anchor_status,r.verdict
        FROM events x JOIN evidence e ON e.evidence_id=json_extract(x.payload_json,'$.evidence_id') AND e.run_id=x.run_id
        JOIN sources s ON s.source_id=e.source_id
        LEFT JOIN evidence_reviews r ON r.review_id=json_extract(x.payload_json,'$.review_id') AND r.evidence_id=e.evidence_id
        LEFT JOIN events a ON a.run_id=x.run_id AND a.kind='v62.source.extracted' AND a.event_id<=x.event_id
             AND json_extract(a.payload_json,'$.evidence_id')=e.evidence_id
        WHERE x.run_id=? AND x.event_id>? AND x.event_id<=? AND json_extract(x.payload_json,'$.scope')='run'
          AND x.kind IN ('v62.source.reviewed','v62.source.extracted')""", (brief["run_id"], after, through))
    for row in sources:
        if row["event_id"] not in changes or row["subject_id"] not in visible:
            continue
        item = changes[row["event_id"]]
        if row["kind"] == "v62.source.reviewed":
            item.update(review_ref=row["subject_id"], verdict=row["verdict"])
        if row["extraction_ref"] in visible:
            item.update(extraction_ref=row["extraction_ref"], anchor_status=row["anchor_status"])
        if row["source_ref"] in visible:
            item.update(source_ref=row["source_ref"], segment_id=row["segment_id"])
        if row["target"]:
            item["target_claim_ref"] = row["target"]
    late_kind = next((r["kind"] for r in events if r["event_id"] == cause.get("event_id")), None)
    if late_kind in ("research.pi.feedback", "research.pi.answered"):
        read_with = "Read the newly routed PI input in pi_dialogue when reconsidering this decision."
    else:
        read_with = ("Read review_ref via gx_read_artifact and its source with gx_read_source. "
                     "Use claim_revision_id for the resulting claim version.")
    brief["trigger"]["refresh"] = {
        "reason": cause["reason"], "after_cursor": after, "superseded_event": marker["event_id"],
        "first_late_event": cause.get("event_id"),
        "read_with": read_with,
    }


def deliver(conn, brief, limit):
    """Keep the authoritative scope intact when a large frozen packet needs pagination.

    This is called after the WAL read snapshot. The referenced artifact contains exactly
    the authorized packet, including all of its released results. Its content digest
    makes concurrent preparation/retry converge on the same bytes.
    """
    from .v6_native import PREFIX, has_open_branch, has_recipe_source
    room = limit - len(PREFIX.encode("utf-8"))
    if _size(brief) <= room:
        return brief, brief
    raw = ids.canonical_json(brief)
    sha = ids.sha256_of_obj(brief)
    artifact = ArtifactService(conn).publish_bytes(
        namespace=brief["run_id"] + "/briefs", rel_path=sha + ".json", kind="research_brief",
        data=raw, expected_sha256=sha, media_type="application/json", run_id=brief["run_id"],
        provenance={"goal_version": brief["goal_version"], "scope": "run", "through_cursor": brief["trigger"]["through_cursor"]},
    )
    ref = artifact.artifact_ref
    full = {**brief, "full_brief_ref": ref, "evidence_refs": list(dict.fromkeys([*brief["evidence_refs"], ref]))}
    packet = {k: brief[k] for k in ("run_id", "scope", "goal_version", "head", "research_effort")}
    if survey := brief.get("catalog_discovery"):
        packet["catalog_discovery"] = {k: survey[k] for k in ("state_key", "through_event", "survey_event", "opening_survey_event") if k in survey}
    if brief.get("ad_hoc_delegation"):
        packet["ad_hoc_delegation"] = True
    if "pi_method_scope" in brief:
        scope = brief["pi_method_scope"]
        packet["pi_method_scope"] = {k:scope[k] for k in ("status","collections") if k in scope}
    if "capability_view" in brief:
        packet["capability_view"] = brief["capability_view"]
    if brief.get("branch_delegation") is False:
        packet["branch_delegation"] = False
    packet.update(goal=_excerpt(brief["goal"], 512), full_brief_ref=ref, read_with="gx_read_artifact",
                  module_catalog_available=bool(brief.get("module_catalog_available") or brief.get("module_catalog")),
                  **({"source_tools": brief["source_tools"]} if brief.get("source_tools") else {}),
                  budget=brief["budget"], previous_decision=None, evidence_refs=[ref],
                  trigger={k: brief["trigger"][k] for k in ("since_cursor", "through_cursor")})
    # Stage the fields used by the public decision tools even in the smallest supported packet.
    packet.update(materials=[], capabilities=[], branches=[], nodes=[], claims=[], observations=[],
                  intent={}, module_reports=[], module_catalog=[])
    if "research_evidence_version" in brief:
        packet["research_evidence_version"] = brief["research_evidence_version"]
    if "prior_research" in brief:
        prior = brief["prior_research"]
        packet["prior_research"] = {k: prior[k] for k in (
            "parent_run_id", "parent_decision_ref", "parent_goal_version", "evidence_status")}
    if brief.get("core_materials"):
        packet["core_material_count"] = len(brief["core_materials"])
    if brief.get("research_topic_findings"):
        packet["research_topic_findings_available"] = len(brief["research_topic_findings"])
    if brief.get("research_joins"):
        packet["research_joins_available"] = len(brief["research_joins"])
    if brief.get("recipe_decisions"):
        packet["recipe_decisions_available"] = len(brief["recipe_decisions"])
    if "prospective_opening" in brief:
        packet["prospective_opening"] = brief["prospective_opening"]
    if "report_presentation" in brief:
        packet["report_presentation"] = brief["report_presentation"]
    if "harness_policy" in brief:
        packet["harness_policy"] = brief["harness_policy"]
        from . import harness_policy
        if harness_policy.enabled(brief):
            if has_recipe_source(brief, True):
                packet["recipe_source_available"] = True
            if brief.get("branch_delegation", True) and has_open_branch(brief, True):
                packet["open_branch_available"] = True
    if "study_class" in brief:
        packet["study_class"] = brief["study_class"]
    if "evidence_policy" in brief:
        packet["evidence_policy"] = brief["evidence_policy"]
    progress = brief.get("research_progress")
    if progress:
        packet["research_progress"] = {k: progress[k] for k in (
            "phase", "no_progress_rounds", "consecutive_no_progress", "decision_options")}
        for key in ("guidance", "last_round_repeated_measurements"):
            if key in progress:
                packet["research_progress"][key] = progress[key]
        packet["research_progress"].update(open_gaps=[], open_gap_count=len(progress["open_gaps"]),
                                          waiting_for=[], waiting_count=len(progress["waiting_for"]))
    refresh = brief["trigger"].get("refresh")
    if refresh:
        trigger = packet["trigger"]
        rows = [c for c in brief["trigger"]["changes"] if c["event_id"] > refresh["after_cursor"]
                and c["event"] != "v6.decision.superseded"]
        trigger.update(refresh=refresh, changes=[], refresh_change_count=len(rows))
    # Reserve headers before any variable-length content is fitted. Opening a
    # new empty section on a full packet can itself exceed the native limit.
    for section in ("core_materials", "research_joins", "research_topic_findings", "recipe_decisions", "research_intents",
                    "pi_dialogue", "comparisons"):
        if brief.get(section):
            packet[section] = []
    if brief.get("research_intents"):
        packet["research_intent_count"] = len(brief["research_intents"])
    report_index = brief.get("module_report_index", [])
    adverse_count = sum(bool(entry.get("counterevidence")) for entry in report_index)
    if adverse_count:
        packet["reports_with_counterevidence"] = adverse_count
    if _size(packet) > room:
        packet["budget"] = {"initial_job_limit": brief["budget"]["initial_job_limit"]} if "initial_job_limit" in brief["budget"] else {}
    # JSON escaping can expand the goal beyond its UTF-8 excerpt size.
    if _size(packet) > room:
        packet["goal"] = _excerpt(brief["goal"], 96)

    def append(key, value, *, owner=packet):
        owner[key].append(value)
        if _size(packet) <= room:
            return True
        owner[key].pop()
        return False

    if survey := brief.get("catalog_discovery"):
        preview = packet["catalog_discovery"]
        # Keep the current PI condition and a representative input/output on the first page.
        # The native query always receives the full frozen context.
        samples = {
            "pi_update": survey.get("pi_update") or "",
            "latest_input": (survey["available_inputs"] or [{}])[-1].get("artifact_ref", ""),
            "needed_output": (survey["required_outputs"] or [""])[0],
        }
        for key, value in samples.items():
            if value:
                preview[key] = _excerpt(value, 144)
                if _size(packet) > room:
                    del preview[key]
    if refresh:
        trigger = packet["trigger"]
        # Leave room for independent measurements and reports. The full brief holds every row.
        start = _size(packet)
        for item in reversed(rows):
            change = {k: v for k, v in item.items() if k not in {"matched_watches", "branch_watches"}}
            if change.get("scope") == "run":
                change.pop("scope")
            if _size(packet) - start + _size(change) > room // 3:
                change = {k: change[k] for k in ("event_id", "event", "subject_ref")}
            if _size(packet) - start + _size(change) > room // 3 or not append("changes", change, owner=trigger):
                break
        trigger["changes"].reverse()

    if brief.get("pi_dialogue"):
        for item in reversed(brief["pi_dialogue"]):
            if not append("pi_dialogue", item):
                break
    # Reserve space for adverse results and their durable citations ahead of routine inventory.
    recent_failures = {item.get("node_id") for item in brief.get("observations", ())
                       if item.get("event") == "v6.node.settled" and item.get("status") == "FAILED"}
    shown_failures = set()
    for node in reversed(brief.get("nodes", ())):
        if node.get("failure") and node["node_id"] in recent_failures:
            if append("nodes", {k: node[k] for k in ("node_id", "job_id", "branch_id", "status", "failure")}):
                shown_failures.add(node["node_id"])
    previews = {entry["report_ref"]: entry for entry in brief.get("module_reports", [])}
    adverse = [{**previews.get(entry["report_ref"], {}), **entry,
                **({"pending_endpoints": previews[entry["report_ref"]].get("pending_endpoints", True)}
                   if "endpoint_total" in entry and entry["report_ref"] in previews else {})}
               for entry in reversed(report_index) if entry.get("counterevidence")]
    adverse_refs = {entry["report_ref"] for entry in adverse}
    ordinary = [entry for entry in reversed(brief.get("module_reports", [])) if entry["report_ref"] not in adverse_refs]
    from . import v621_policy, v623_endpoints as endpoints
    active = brief.get("evidence_policy") in v621_policy.LIVE_SCIENCE
    placed = {}
    if active:
        if brief.get("comparisons"):
            for item in reversed(brief["comparisons"]):
                if not append("comparisons", {k:item[k] for k in ("comparison_id", "branch") if k in item}):
                    break
        # Allocate compact verified values before report prose, catalogs or large proofs.
        # Every value is bound to its own parent reference, including colliding metric names.
        items = [("module_reports", item, "report_ref") for item in [*adverse, *ordinary]]
        items.extend(("observations", item, "artifact_ref") for item in reversed(brief.get("observations", ())))
        preferred = {row["report_ref"]: set(row.get("comparison_endpoint_keys", ())) for row in report_index}
        comparisons = brief.get("comparisons", ())
        for obs in brief.get("observations", ()):
            if comparisons and obs.get("verified_metrics"):
                preferred[obs["artifact_ref"]] = endpoints.comparison_keys(conn, obs["verified_metrics"], comparisons)
        items.sort(key=lambda item: not any(row["key"] in preferred.get(item[1][item[2]], ())
                                            for row in item[1].get("endpoint_rows", ())))
        for group, original, key in items:
            if not original.get("endpoint_total"):
                continue
            ref = original[key]
            summary = {key:ref, "endpoint_total":original["endpoint_total"], "pending_endpoints":True}
            if not append(group, summary):
                packet["endpoints_available_in_full_brief"] = True
                if _size(packet) > room:
                    packet.pop("endpoints_available_in_full_brief")
                continue
            rows = sorted(original.get("endpoint_rows", ()), key=lambda r: r["key"] not in preferred.get(ref, ()))
            endpoints.fit(summary, rows, original["endpoint_total"], lambda: _size(packet) <= room)
            placed[ref] = summary
        for group, original, key in items:
            if (summary := placed.get(original.get(key))) is None:
                continue
            append("evidence_refs", original[key])
            for name in ("status","module_instance_id","event_id","node_id","source_job_id","applicability","counterevidence"):
                if name in original and name not in summary:
                    summary[name] = original[name]
                    if _size(packet) > room:
                        del summary[name]
    for report in [*adverse, *ordinary]:
        if report["report_ref"] in placed:
            continue
        summary = {k: report[k] for k in ("module_instance_id", "status", "report_ref") if k in report}
        if report.get("applicability"):
            summary["applicability"] = report["applicability"]
        contrary = report.get("counterevidence", [])
        if contrary:
            first = contrary[0]
            summary["counterevidence"] = [{**first, "text": _excerpt(first["text"], 256), "source_refs": first["source_refs"][:1]}]
        if append("module_reports", summary):
            append("evidence_refs", report["report_ref"])
            if "verified_metrics" in report:
                proof_fields = ("verified_metrics", "source_quotations")
            else:
                proof_fields = ("metrics", "source_quotations")
            for field in proof_fields:
                if report.get(field):
                    summary[field] = []
                    for proof in report[field]:
                        if not append(field, proof, owner=summary):
                            break
                    if not summary[field]:
                        del summary[field]
    # Keep verbatim words and bound authorship next to the report, after named endpoint allocation.
    indexed_reports = {entry["report_ref"]: entry for entry in report_index}
    for row in packet["module_reports"]:
        original = indexed_reports.get(row["report_ref"], {})
        message = original.get("researcher_message")
        if message:
            row["researcher_message"] = message
            if _size(packet) > room:
                row["researcher_message"] = {"author_kind": message["author_kind"], "read_full_report": True}
                if _size(packet) > room:
                    del row["researcher_message"]
            if original.get("gx_facts"):
                row["gx_facts"] = original["gx_facts"]
                if _size(packet) > room:
                    del row["gx_facts"]
    if progress:
        projected = packet["research_progress"]
        for key in ("waiting_for", "open_gaps"):
            allowance = room // 8
            for row in progress[key]:
                item = {"gap_ref": row["gap_ref"]} if key == "open_gaps" else row
                allowance -= _size(item) + 1
                if allowance < 0 or not append(key, item, owner=projected):
                    break
    if brief.get("research_joins"):
        for row in reversed(brief["research_joins"]):
            summary = {k: row[k] for k in ("key", "state", "question", "outcome_event", "question_id") if k in row}
            if row.get("outcome"):
                summary.update({k: row["outcome"][k] for k in ("needed", "satisfied_count", "still_possible_count")})
            if not append("research_joins", summary):
                break
    for item in brief.get("core_materials", []):
        facts = {k:item[k] for k in ("question", "question_id", "goal_version", "ref", "state") if k in item}
        # Preserve the ref and the observed incomplete status under a tight
        # packet budget. The full provider windows remain on the named artifact.
        if item.get("query_record") and append("core_materials", {**facts, "query_record":item["query_record"]}):
            continue
        if not append("core_materials", facts):
            break
    if brief.get("recipe_decisions"):
        for point in reversed(brief["recipe_decisions"]):
            if not append("recipe_decisions", point):
                break

    if findings := brief.get("research_topic_findings"):
        for finding in reversed(findings):
            if not append("research_topic_findings", finding):
                break
    if brief.get("research_intents"):
        allowance = room // 6
        for route in brief["research_intents"]:
            summary = {key: route[key] for key in ("intent", "revision", "phase", "binding_state")}
            summary["question"] = _excerpt(route["question"], 100)
            allowance -= _size(summary)
            if allowance < 0 or not append("research_intents", summary):
                break

    for key in ("module_catalog", "materials", "capabilities", "branches", "nodes", "claims", "observations"):
        for item in brief.get(key, []):
            if key == "nodes" and item.get("node_id") in shown_failures:
                continue
            if key == "observations" and item.get("artifact_ref") in placed:
                continue
            if not append(key, item):
                break
    for key in ("previous_decision", "catalog_view", "module_catalog_view", "research_progress", "intent", "trigger"):
        if key not in brief:
            continue
        old = packet.get(key)
        packet[key] = brief[key]
        if _size(packet) > room:
            if old is None and key not in ("previous_decision",):
                packet.pop(key)
            else:
                packet[key] = old
    return full, packet
