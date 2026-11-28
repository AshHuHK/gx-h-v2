"""Settle Goal research rounds from published work and record the Lead's convergence decision."""

import json
from collections import defaultdict
from typing import NamedTuple

from ..artifacts import ArtifactService
from ..domain import clock, ids
from ..errors import GxError, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from . import v621_goal_scope

VERSION = "gx.research.v6.2b.1"
OWNER = "v62.rounds"


class _RoundPolicy(NamedTuple):
    threshold: int
    advisory: bool


def _policy(conn, rid):
    row = conn.execute(
        """SELECT json_extract(inputs_json,'$.inputs.research_evidence_version'),
                  json_extract(inputs_json,'$.inputs.research_catalog.policy.effort'),
                  json_extract(inputs_json,'$.inputs.research_catalog.policy.no_progress_rounds'),
                  json_extract(inputs_json,'$.inputs.research_catalog.policy.stagnation_policy')
           FROM runs WHERE run_id=?""", (rid,)
    ).fetchone()
    if row and row[0] == VERSION and row[1] in ("medium", "deep"):
        return _RoundPolicy(row[2] or 2, row[3] == "advisory")
    return None


def _normalize(text):
    return " ".join(text.casefold().split())


def _key(kind, basis):
    return kind + ":" + ids.sha256_of_obj(basis)


def _waiting(conn, rid):
    version = v621_goal_scope.active(conn, rid)
    # A private module is represented by its delegated identity, even when it has internal Jobs.
    items = [dict(kind="module", module_instance_id=m[0], round=m[1]) for m in conn.execute(
        "SELECT module_instance_id,round FROM v62_modules WHERE run_id=? AND status='OPEN' "
        "AND (? IS NULL OR goal_version=?) ORDER BY rowid", (rid, version, version)
    )]
    from . import v62_reports
    released = v62_reports._released(conn, rid)
    items.extend(dict(kind="report_release", module_instance_id=m[0], round=m[1]) for m in conn.execute(
        """SELECT m.module_instance_id,m.round FROM v62_modules m JOIN v62_module_reports r
           USING(module_instance_id,run_id) WHERE m.run_id=? AND (? IS NULL OR m.goal_version=?)
           ORDER BY m.rowid""", (rid, version, version)
    ) if m[0] not in released)
    items.extend(dict(kind="node", node_id=n[0], status=n[1]) for n in conn.execute(
        """SELECT n.node_id,n.status FROM v6_nodes n WHERE n.run_id=?
           AND n.status IN ('PENDING','RUNNING','CANCEL_REQUESTED')
           AND NOT EXISTS (SELECT 1 FROM v62_modules m WHERE m.branch_id=n.branch_id)
           AND (? IS NULL OR EXISTS(SELECT 1 FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id)
               WHERE d.decision_ref=n.decision_ref AND t.goal_version=?))
           ORDER BY n.rowid""", (rid, version, version)
    ))
    items.extend(dict(kind="operator", approval_id=a[0]) for a in conn.execute(
        """SELECT a.approval_id FROM approvals a WHERE a.run_id=? AND a.status='PENDING'
           AND NOT EXISTS(SELECT 1 FROM submissions s JOIN v6_decision_tasks t USING(task_id)
               WHERE a.subject_kind='submission' AND a.subject_id=s.submission_id AND t.goal_version!=?)
           ORDER BY a.rowid""", (rid, version)
    ))
    return items


def _members(conn, rid):
    version = v621_goal_scope.active(conn, rid)
    rows = conn.execute(
        """SELECT m.round,m.module_instance_id,m.module_id,m.module_version,m.focus,m.expected_delivery,
                  m.parent_context_ref,m.status,m.snapshot_json,m.grant_json,r.report_ref,t.status AS parent_status
           FROM v62_modules m JOIN tasks t ON t.task_id=m.parent_task_id
           LEFT JOIN v62_module_reports r ON r.run_id=m.run_id AND r.module_instance_id=m.module_instance_id
           WHERE m.run_id=? AND (? IS NULL OR m.goal_version=?) AND NOT EXISTS
           (SELECT 1 FROM research_rounds rr WHERE rr.run_id=m.run_id AND rr.created_by=?
            AND json_extract(rr.sealed_json,'$.round')=m.round AND
            (? IS NULL OR json_extract(rr.sealed_json,'$.goal_version')=m.goal_version))
           ORDER BY m.round,m.rowid""", (rid, version, version, OWNER, version)
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["round"]].append(row)
    return grouped


def _history(conn, rid, cursor=None, *, goal_version=None):
    version = v621_goal_scope.active(conn, rid) if goal_version is None else goal_version
    return [json.loads(row[0]) for row in conn.execute(
        """SELECT r.sealed_json FROM research_rounds r JOIN events e
           ON e.run_id=r.run_id AND e.kind='v62.round.settled' AND e.subject_id=r.round_id
           WHERE r.run_id=? AND r.created_by=? AND (? IS NULL OR e.event_id<=?)
           AND (? IS NULL OR json_extract(r.sealed_json,'$.goal_version')=?) ORDER BY r.round_index""",
        (rid, OWNER, cursor, cursor, version, version),
    )]


def _cursor(conn, rid):
    return conn.execute("SELECT coalesce(max(event_id),0) FROM events WHERE run_id=?", (rid,)).fetchone()[0]


def _reference(conn, rid, ref, scope, cursor):
    """Only evidence and output owners determine the scientific nature of a source."""
    from . import v62_outputs, v62_sources

    row = conn.execute("SELECT kind,sha256,origin,job_id FROM artifacts WHERE run_id=? AND artifact_ref=? "
                       "AND published=1", (rid, ref)).fetchone()
    if not row:
        return None
    if row["kind"] in ("v62_evidence_review", "v62_source_extraction"):
        fact = v62_sources.claim_ground(conn, rid, ref, scope, cursor)
        if (fact and fact.get("source_class") == "reviewed_source_evidence"
                and fact.get("record_nature") == "formal_evidence" and fact.get("verdict") == "SUPPORTED"):
            kind = {"supports": "reviewed_evidence", "opposes": "counterevidence",
                    "unresolved": "reviewed_uncertainty"}.get(fact["direction"])
            if kind:
                basis = [fact.get(field) for field in ("source_sha256", "segment_id", "quote_sha256",
                                                       "applicability", "target_claim_ref")]
                return kind, _key(kind, basis)
        return None
    if not row["job_id"] and (row["origin"] != "INGEST" or not conn.execute(
        """SELECT 1 FROM events WHERE run_id=? AND kind='v6.observation.added' AND event_id<=?
           AND json_extract(payload_json,'$.artifact_ref')=? AND json_type(payload_json,'$.acquisition')='object'
           LIMIT 1""", (rid, cursor, ref)
    ).fetchone()):
        return None  # A raw material or module suggestion has no acquired-measurement record.
    fact = v62_outputs.classify(conn, rid, ref, scope=scope, through_event=cursor)
    nature = fact.get("nature")
    if fact.get("record_kind") in ("job_output", "supplied_observation"):
        if "model_prediction" in fact.get("upstream_natures", []):
            nature = "model_prediction"
        if nature == "observed" or (nature == "analysis" and fact.get("original_observations")):
            lineage = fact["dependencies"]
            basis = [row["sha256"], sorted(p["sha256"] for p in fact["data_refs"]),
                     lineage["acquisition"], lineage["source"]]
            return nature, _key(nature, basis)
        if nature == "model_prediction":
            return nature, _key(nature, row["sha256"])
    return None


def early_reason(conn, module, bundle):
    """Contribute an optional early reason to A's single, idempotent release owner."""
    rid = module["run_id"]
    if not _policy(conn, rid):
        return None
    version = v621_goal_scope.active(conn, rid)
    subjects = {"run", rid, module["scope"], module["module_instance_id"], module["branch_id"]}
    prior = conn.execute(
        """SELECT d.receipt_json FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id)
           WHERE d.run_id=? AND t.scope='run' AND (? IS NULL OR t.goal_version=?)
           ORDER BY d.rowid DESC LIMIT 1""", (rid, version, version)
    ).fetchone()
    watches = json.loads(prior[0])["reconsider_when"] if prior else []
    if any(w["event"] == "module_report" and w["subject_ref"] in subjects for w in watches):
        return "reconsider_when"
    # The finding can flag an adverse result for inspection. GX's evidence owners decide
    # whether an original qualifies as scientific counterevidence for round/claim assessment.
    for finding in bundle["findings"]:
        if finding.get("kind") != "counterevidence":
            continue
        for ref in finding["source_refs"]:
            fact = _reference(conn, rid, ref, module["scope"], _cursor(conn, rid))
            if fact and fact[0] in ("observed", "analysis", "counterevidence"):
                return "critical_counterevidence"
    return None


def _source_versions(conn, rid, sources, cursor):
    """Use original pins even when a later inquiry cites a new wrapper for the same material."""
    from . import v62_sources

    queue, visited, versions = list(sources), set(), set()
    while queue:
        ref = queue.pop()
        if ref in visited:
            continue
        visited.add(ref)
        meta = conn.execute("SELECT kind,sha256 FROM artifacts WHERE run_id=? AND artifact_ref=?",
                            (rid, ref)).fetchone()
        if not meta:
            continue
        if meta["kind"] == "module_report_bundle":
            receipt = conn.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? AND report_ref=?",
                                   (rid, ref)).fetchone()
            if receipt:
                queue.extend(json.loads(receipt[0])["source_refs"])
            continue
        if meta["kind"] in ("v62_evidence_review", "v62_source_extraction"):
            doc = v62_sources.claim_ground(conn, rid, ref, "run", cursor)
            if doc:
                versions.add(_key("span", [doc["source_sha256"], doc["segment_id"], doc["quote_sha256"]]))
            continue
        versions.add(meta["sha256"])
    return sorted(versions)


def _report(conn, rid, member, cursor):
    ref = member["report_ref"]
    bundle = ArtifactService(conn).read_json(ref)
    # The report owner has checked each referenced artifact and its read or Job receipt.
    sources = {s["artifact_ref"]: s for s in bundle["sources"] if s["receipts"]}
    source_hashes = _source_versions(conn, rid, sources, cursor)
    facts = {}
    for source in sources:
        value = _reference(conn, rid, source, "run", cursor)
        if value:
            kind, key = value
            facts[key] = dict(kind=kind, reference=source)
    gaps = {}
    if bundle["author"]["kind"] == "MODULE" and source_hashes and (bundle["status"] == "COMPLETE" or bundle["findings"]):
        from . import v621_gaps, v621_policy
        current = v621_policy.enabled(conn, rid)
        report_event = None
        if current and bundle["gaps"]:
            report_event = conn.execute("SELECT json_extract(receipt_json,'$.event_id') FROM v62_module_reports "
                                        "WHERE run_id=? AND report_ref=?", (rid, ref)).fetchone()[0]
        for question in bundle["gaps"]:
            if current:
                gap = v621_gaps.declaration(bundle, question, source_hashes, ref, report_event)
                key = gap["gap_ref"]
            else:
                key = _key("gap", [rid, _normalize(question), source_hashes])
                gap = dict(gap_ref=key, question=question, report_ref=ref)
            gaps[key] = {**gap, "source_refs": sorted(sources)}
            facts[key] = dict(kind="new_gap", reference=key, **gaps[key])
    # A new, completed inquiry can confirm an existing answer. The inquiry and its
    # underlying source versions are stable across new module IDs, prose and report refs.
    snap, grant = (json.loads(member[k]) for k in ("snapshot_json", "grant_json"))
    inquiry = [member["module_id"], member["module_version"], _normalize(member["focus"]),
               _normalize(member["expected_delivery"]), _source_versions(conn, rid,
                   [m["artifact_ref"] for m in snap["materials"]], cursor),
               sorted(grant["capabilities"]), source_hashes]
    ready = bool(gaps or any(f["source_refs"] and all(s in sources for s in f["source_refs"])
                            for f in bundle["findings"]))
    return facts, gaps, _key("inquiry", inquiry) if ready else None


def _event_facts(conn, rid, after, through):
    from . import v62_lead

    facts, failures = {}, []
    private = v62_lead.branch_ids(conn, rid)
    version = v621_goal_scope.active(conn, rid)
    after = max(after, v621_goal_scope.start_cursor(conn, rid, version) - 1)
    for row in conn.execute(
        """SELECT event_id,kind,subject_id,reason,payload_json FROM events WHERE run_id=? AND event_id>? AND event_id<=?
           AND kind IN ('v6.node.settled','v6.observation.added','v6.claim.adopted','v62.claim.reassessed',
                        'v6.branch.stop_requested','v62.gap.resolved','v62.source.reviewed') ORDER BY event_id""", (rid, after, through)
    ):
        p = store.loads(row, "payload_json")
        if not v621_goal_scope.applies(conn, rid, row, p, version):
            continue
        if v62_lead.internal_event(row, private) or str(p.get("scope", "")).startswith("module:"):
            continue
        kind, ref = row["kind"], None
        value = None
        if kind == "v6.node.settled":
            condition = p.get("condition", {})
            if condition.get("reason") == "evidence_condition_false":
                gate = condition["gate"]
                key = _key("candidate_excluded", [condition["source_sha256"], condition["result_sha256"],
                                                  gate["pointer"], gate["equals"]])
                facts[key] = dict(kind="candidate_excluded", reference=condition["result_ref"],
                                  node_id=row["subject_id"], event_id=row["event_id"])
                continue
            if p.get("dependency", {}).get("reason") == "evidence_route_blocked":
                continue
            if p.get("status") in ("FAILED", "BLOCKED", "CANCELLED"):
                failures.append(dict(node_id=row["subject_id"], event_id=row["event_id"], status=p["status"],
                                     job_id=p.get("job_id"), reason=row["reason"]))
            elif p.get("job_id") and p["status"] in ("SUCCEEDED", "REUSED"):
                from . import v62_outputs
                for output in v62_outputs.published_outputs(conn, rid, p["job_id"]):
                    ref = output["artifact_ref"]
                    result = _reference(conn, rid, ref, "run", through)
                    if result:
                        label, key = result
                        facts[key] = dict(kind=label, reference=ref, event_id=row["event_id"])
            continue
        if kind == "v6.observation.added" or (kind == "v62.source.reviewed" and p.get("scope") == "run"):
            ref = p["artifact_ref"]
            value = _reference(conn, rid, ref, "run", through)
        elif kind == "v62.claim.reassessed":
            ref = p["claim_ref"]
            value = "claim_changed", _key("claim", [row["subject_id"], p["before"], p["after"],
                                                    p["direction"], p["evidence_refs"]])
        elif kind == "v6.claim.adopted":
            from . import v6_claims
            ref = p["claim_revision_id"]
            claim = v6_claims.read_revision(conn, rid, ref)
            if claim and not str(claim["decision_scope"]).startswith("module:"):
                maturity = claim.get("maturity", {})
                value = "claim_changed", _key("claim", [claim["statement"], claim["scope"], claim["level"],
                                                       maturity.get("stage"), maturity.get("direction")])
        elif kind == "v62.gap.resolved":
            ref = p["gap_ref"]
            value = "resolved_gap", _key("resolved_gap", ref)
        elif kind == "v6.branch.stop_requested" and row["subject_id"] not in private:
            ref = p["decision_id"]
            decision = conn.execute("SELECT request_json FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                                    (rid, ref)).fetchone()
            if decision and json.loads(decision[0])["decision"]["evidence_refs"]:
                value = "candidate_excluded", _key("candidate_excluded", row["subject_id"])
        if value:
            name, key = value
            facts[key] = dict(kind=name, reference=ref, event_id=row["event_id"])
    return facts, failures


def _present(member):
    return {k: member[k] for k in ("module_instance_id", "module_id", "focus", "expected_delivery")}


def _calculate(conn, rid, rounds, policy, cursor):
    version = v621_goal_scope.active(conn, rid)
    history = _history(conn, rid)
    seen = {key for entry in history for key in (*entry["new_facts"], *entry.get("initial_known_facts", []))}
    inquiries = {k for entry in history for k in entry["inquiries"]}
    streak = history[-1]["consecutive_no_progress"] if history else 0
    indexes = list(rounds)
    initial = {}
    if history:
        after = history[-1]["through_cursor"]
    else:
        first_context = rounds[indexes[0]][0]["parent_context_ref"]
        after = conn.execute("SELECT event_cursor FROM research_contexts WHERE context_ref=?", (first_context,)).fetchone()[0]
        initial, _ = _event_facts(conn, rid, 0, after)
        seen.update(initial)
    plans = []
    for index, number in enumerate(indexes):
        members = rounds[number]
        # If several rounds drained between ticks, attribute public events through each
        # round's own release; the last includes any observation we were waiting for.
        through = cursor
        if index + 1 < len(indexes):
            released = conn.execute("""SELECT max(w.event_id) FROM v62_module_wakes w JOIN events e USING(event_id)
                WHERE w.run_id=? AND w.round=? AND (? IS NULL OR json_extract(e.payload_json,'$.goal_version')=?)""",
                (rid, number, version, version)).fetchone()[0]
            through = max(after, released or after)
        facts, failures = _event_facts(conn, rid, after, through)
        gaps, asked, deliveries = {}, set(), []
        for m in members:
            result = dict(module_instance_id=m["module_instance_id"], status=m["status"], report_ref=m["report_ref"])
            if m["report_ref"]:
                added, missing, inquiry = _report(conn, rid, m, through)
                facts.update(added)
                gaps.update(missing)
                if inquiry:
                    asked.add(inquiry)
            deliveries.append(result)
        novel = {k: v for k, v in facts.items() if k not in seen}
        fresh = sorted(asked - inquiries)
        outcome = "progress" if novel else "no_progress" if fresh else "uncounted"
        previous = streak
        if novel:
            streak = 0
        elif fresh:
            streak += 1
        result = dict(round=number, through_cursor=through, outcomes=deliveries, outcome=outcome,
                      new_facts=novel, inquiries=fresh, execution_failures=failures, consecutive_no_progress=streak,
                      no_progress_rounds=policy.threshold, reconsider=previous < policy.threshold <= streak)
        if policy.advisory:
            result["repeated_measurement_refs"] = sorted({fact["reference"] for key, fact in facts.items()
                if key in seen and fact["kind"] in ("analysis", "observed")})
        if version is not None:
            result["goal_version"] = version
        if index == 0 and initial:
            result["initial_known_facts"] = sorted(initial)
        plans.append((members[0]["parent_context_ref"], [_present(m) for m in members], result, gaps))
        seen.update(novel)
        inquiries.update(fresh)
        after = through
    return plans


def reconcile(conn, rid):
    """Seal once the expected work has drained. Prepare original reads outside the writer."""
    policy = _policy(conn, rid)
    if not policy:
        return
    rounds = _members(conn, rid)
    if not rounds or _waiting(conn, rid):
        return
    conn.execute("SAVEPOINT v62_round_capture")
    try:
        cutoff = _cursor(conn, rid)
        rounds = _members(conn, rid)
        if not rounds or _waiting(conn, rid):
            return
        from . import v6_goal
        # A permits the opening Lead to add another member to its current round, even
        # after an early release or withdrawal of all the existing members.
        if any(m["parent_status"] in v6_goal.ACTIVE_TASKS for group in rounds.values() for m in group):
            return
        plans = _calculate(conn, rid, rounds, policy, cutoff)
    finally:
        conn.execute("RELEASE v62_round_capture")
    with store.transaction(conn, transaction_owner=OWNER, operation="settle", run_id=rid):
        if _cursor(conn, rid) != cutoff or _waiting(conn, rid):
            return
        for context, expected, result, gaps in plans:
            number = result["round"]
            physical = number
            identity = [rid, number]
            if "goal_version" in result:
                identity = [rid, result["goal_version"], number]
                # The shared history uses a Run-wide sequence; its signed content
                # records the per-objective number shown to the researcher.
                physical = conn.execute("SELECT coalesce(max(round_index),0)+1 FROM research_rounds WHERE run_id=?",
                                        (rid,)).fetchone()[0]
            key = _key("round", identity)
            now = clock.now_iso()
            conn.execute(
                """INSERT INTO research_rounds(round_id,run_id,round_index,context_ref,expected_json,
                   expected_sha256,status,sealed_json,sealed_sha256,created_by,created_at,sealed_at,closed_at)
                   VALUES(?,?,?,?,?,?,'COMMITTED',?,?,?,?,?,?)""",
                (key, rid, physical, context, store._j(expected), ids.sha256_of_obj(expected), store._j(result),
                 ids.sha256_of_obj(result), OWNER, now, now, now),
            )
            for gap in gaps.values():
                conn.execute(
                    """INSERT INTO gaps(gap_id,run_id,namespace,status,gap_kind,question,missing,origin,
                       created_at,updated_at) VALUES(?,?,?,'OPEN','missing_information',?,?,?,?,?)
                       ON CONFLICT(gap_id) DO NOTHING""",
                    (gap["gap_ref"], rid, rid, gap["question"], gap["question"], OWNER, now, now),
                )
            store.append_event(conn, kind="v62.round.settled", run_id=rid, actor=OWNER,
                               subject_kind="round", subject_id=key, payload={
                                   k: result[k] for k in ("round", "through_cursor", "outcome",
                                                          "consecutive_no_progress", "reconsider", "goal_version") if k in result})


def _convergence(conn, rid, cursor=None):
    version = v621_goal_scope.active(conn, rid)
    row = conn.execute(
        """SELECT payload_json FROM events WHERE run_id=? AND kind='v62.convergence.entered'
           AND (? IS NULL OR event_id<=?) AND (? IS NULL OR json_extract(payload_json,'$.goal_version')=?)
           ORDER BY event_id LIMIT 1""", (rid, cursor, cursor, version, version)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _streak(conn, rid, threshold):
    version = v621_goal_scope.active(conn, rid)
    entry = conn.execute(
        """SELECT json_extract(sealed_json,'$.consecutive_no_progress') FROM research_rounds
           WHERE run_id=? AND created_by=? AND (? IS NULL OR json_extract(sealed_json,'$.goal_version')=?)
           ORDER BY round_index DESC LIMIT 1""", (rid, OWNER, version, version)
    ).fetchone()
    return bool(entry and entry[0] >= threshold)


def require_exploration(conn, rid, *, route_intent=None):
    """The ordinary module admission owner calls this after its duplicate-operation lookup."""
    if route_intent is not None:
        from . import harness_policy, v6_routes
        if not harness_policy.active(conn, rid):
            raise ScopeDenied("This Goal does not support route-scoped delegation")
        v6_routes.require_open(conn, rid, route_intent)
        return
    policy = _policy(conn, rid)
    if policy and (_convergence(conn, rid) or (not policy.advisory and _streak(conn, rid, policy.threshold))):
        raise ScopeDenied("Exploration is closed to new modules; use the Lead's convergence or closing decision")


def brief(conn, rid, cursor):
    policy = _policy(conn, rid)
    if not policy:
        return {}
    history = _history(conn, rid, cursor)
    streak = history[-1]["consecutive_no_progress"] if history else 0
    entered, waiting = _convergence(conn, rid, cursor), _waiting(conn, rid)
    if entered:
        phase, options = "converging", ["assess_candidates", "close_run"]
        from . import harness_policy
        if harness_policy.active(conn, rid):
            options.append("reopen_intent")
    elif streak >= policy.threshold and policy.advisory:
        phase, options = "reconsider", ["explore", "wait"]
        if not waiting:
            options.extend(["enter_convergence", "close_run"])
    elif streak >= policy.threshold:
        phase, options = "convergence_due", ["wait"] if waiting else ["enter_convergence", "close_run"]
    else:
        phase, options = "exploring", ["explore", "enter_convergence", "close_run"]
    resolution_events = conn.execute(
        """SELECT subject_id,event_id,payload_json FROM events WHERE run_id=? AND kind='v62.gap.resolved'
           AND (? IS NULL OR event_id<=?) ORDER BY event_id""", (rid, cursor, cursor))
    resolved = {r["subject_id"]: {**json.loads(r["payload_json"]), "event_id": r["event_id"]} for r in resolution_events}
    active_goal = v621_goal_scope.active(conn, rid)
    closed = [r for r in resolved.values() if active_goal is not None and r.get("goal_version") == active_goal]
    gaps = [{k: fact[k] for k in ("gap_ref", "question", "report_ref", "goal_version", "criterion",
                                   "registered_event_id", "closure") if k in fact}
            for entry in history for fact in entry["new_facts"].values()
            if fact["kind"] == "new_gap" and fact["gap_ref"] not in resolved]
    rounds = [{k: entry[k] for k in ("round", "outcome", "outcomes", "execution_failures",
                                     "consecutive_no_progress", "through_cursor", "repeated_measurement_refs") if k in entry}
              for entry in history]
    # Unreleased work contributes only its assignment to the Lead's brief.
    from . import v62_reports
    released = set(v62_reports.released_report_refs(conn, rid, through_cursor=cursor))
    pending = []
    for n, members in _members(conn, rid).items():
        for m in members:
            item = dict(_present(m), round=n)
            if m["report_ref"] in released:
                item.update(status=m["status"], report_ref=m["report_ref"])
            elif m["status"] in ("STALE", "CANCELLED"):
                item["status"] = m["status"]
            else:
                item["status"] = "AWAITING_REPORT"
            pending.append(item)
    advice = {}
    if phase == "reconsider":
        advice["last_round_repeated_measurements"] = len(rounds[-1].get("repeated_measurement_refs", []))
        advice["guidance"] = ("Use known evidence, repeated measurements, outstanding work and remaining resources "
                              "to decide whether to wait, converge, or explore. For another measurement, explain "
                              "which competing explanations it separates and what result would change your action.")
    return {"research_progress": dict(phase=phase, no_progress_rounds=policy.threshold, consecutive_no_progress=streak,
                                     decision_options=options, rounds=rounds, expected=pending, waiting_for=waiting,
                                     open_gaps=gaps, convergence=entered, **advice,
                                     **({"resolved_gaps": closed} if closed else {}))}


def late_evidence(conn, rid, cursor):
    """The normal Lead event view excludes historical Goals, private workers and accounting."""
    from . import harness_policy, research_dialogue, v6_goal, v62_lead, v621_policy
    private = v62_lead.branch_ids(conn, rid)
    for row in v6_goal._events(conn, rid, cursor):
        kind = row["kind"]
        if kind in ("v62.module.wake", "v6.node.settled", "v6.observation.added", "v62.claim.reassessed",
                    "v62.source.reviewed", "v6.claim.adopted", "v6.intent.changed"):
            if not v62_lead.internal_event(row, private):
                return row["event_id"]
        if kind == "v621.material.assigned" and v621_policy.current(conn, rid):
            return row["event_id"]
        if kind in (research_dialogue.FEEDBACK, research_dialogue.ANSWER) and harness_policy.current(conn, rid):
            pi = store.loads(row, "payload_json")
            # Main's receipt is independent of the deciding Lead's acknowledged
            # brief. Only an input routed to this recipient can refresh it.
            if pi["scope"] == "run" and (kind == research_dialogue.ANSWER or not conn.execute(
                    "SELECT 1 FROM events WHERE run_id=? AND subject_id=? AND kind=? LIMIT 1",
                    (rid, row["subject_id"], research_dialogue.DISPOSE)).fetchone()):
                return row["event_id"]
    return None


def adopt(conn, request, decision_ref, task):
    """Validate and persist the choice in the ordinary decision owner's write transaction."""
    decision, rid = request["decision"], request["run_id"]
    entered, resolved = decision.get("convergence"), decision.get("resolved_gaps", [])
    policy = _policy(conn, rid)
    if not policy or task["scope"] != "run":
        if entered is not None or resolved:
            raise ScopeDenied("Research convergence and gap decisions require this version's Run Lead")
        return None
    if not conn.in_transaction:
        raise RuntimeError("Record research choices in the adopting decision's transaction")
    converged = _convergence(conn, rid)
    if policy.advisory and converged and any(op["op"] == "open_branch" for op in decision["ops"]):
        from . import harness_policy, v6_routes
        if not harness_policy.active(conn, rid) or not v6_routes.locally_bound_openings(conn, rid, decision["ops"]):
            raise ScopeDenied("This Run has entered convergence; assess its candidates or reopen an affected route")
    due = not converged and _streak(conn, rid, policy.threshold)
    closes = any(op["op"] == "close_run" for op in decision["ops"])
    if not (due or entered is not None or resolved):
        return None
    frozen = json.loads(task["snapshot_json"])
    # A task already underway can finish a plain wait if the threshold has just been
    # crossed. The successor will actually receive the evidence and the available choices.
    due = due and frozen.get("research_progress", {}).get("phase") in ("convergence_due", "reconsider")
    waiting = _waiting(conn, rid)
    if entered is not None:
        if converged:
            raise ScopeDenied("This Run already has its immutable convergence decision")
        if decision["ops"] and not closes:
            raise SchemaInvalid("Entering convergence requires resolve or a closing decision")
        if not decision["evidence_refs"]:
            raise SchemaInvalid("Convergence requires cited evidence from the frozen brief")
        if waiting:
            raise ScopeDenied("Wait for the outstanding commissioned work before entering convergence",
                              details={"waiting_for": waiting})
    if due and not policy.advisory and entered is None and not closes:
        if not waiting or decision["ops"]:
            raise ScopeDenied("The no-progress limit calls for convergence or closure",
                              details={"waiting_for": waiting})
    if due and closes and waiting:
        raise ScopeDenied("The closing decision must await outstanding work", details={"waiting_for": waiting})
    from . import v621_policy
    if (entered is not None or closes or resolved) and not (closes and v621_policy.current(conn, rid)):
        late = late_evidence(conn, rid, task["event_cursor"])
        if late:
            raise StaleRevision("New information arrived after the frozen brief; consider it before settling",
                                details={"reason": "convergence_context_changed", "event_id": late})
    if resolved and "gap_resolutions" in request:
        for result in request["gap_resolutions"]:
            key = result["gap_ref"]
            changed = conn.execute("UPDATE gaps SET status='RESOLVED',updated_at=? WHERE run_id=? AND gap_id=? "
                                   "AND status='OPEN'", (clock.now_iso(), rid, key)).rowcount
            if changed != 1:
                raise StaleRevision("The gap has already been resolved")
            store.append_event(conn, run_id=rid, kind="v62.gap.resolved", actor=OWNER,
                               subject_kind="gap", subject_id=key,
                               payload={**result, "scope": "run", "decision_ref": decision_ref,
                                        "evidence_refs": [m["artifact_ref"] for m in result["matches"]],
                                        "rationale": decision["rationale"]})
    elif resolved:
        visible = {g["gap_ref"]: g for g in frozen.get("research_progress", {}).get("open_gaps", [])}
        if not set(resolved) <= set(visible):
            raise ScopeDenied("A resolved gap must be open in this frozen Lead brief")
        grounds = {}
        for ref in decision["evidence_refs"]:
            value = _reference(conn, rid, ref, "run", task["event_cursor"])
            if value and value[0] not in ("reviewed_uncertainty", "model_prediction"):
                grounds[ref] = value
        if not grounds:
            raise SchemaInvalid("Resolving a gap requires a cited, admitted observation, analysis or reviewed source")
        for gap in resolved:
            existing = conn.execute("SELECT 1 FROM gaps WHERE run_id=? AND gap_id=? AND status='OPEN'",
                                    (rid, gap)).fetchone()
            if not existing:
                raise StaleRevision("The gap has already been resolved")
            original = conn.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? AND report_ref=?",
                                    (rid, visible[gap]["report_ref"])).fetchone()
            prior_keys = {_reference(conn, rid, ref, "run", task["event_cursor"])
                          for ref in json.loads(original[0])["source_refs"]} if original else set()
            if not original or set(grounds.values()) <= prior_keys:
                raise SchemaInvalid("Gap resolution must add grounds beyond the sources that established the gap")
            conn.execute("UPDATE gaps SET status='RESOLVED',updated_at=? WHERE gap_id=?", (clock.now_iso(), gap))
            store.append_event(conn, run_id=rid, kind="v62.gap.resolved", actor=OWNER,
                               subject_kind="gap", subject_id=gap, payload=dict(
                                   scope="run", gap_ref=gap, decision_ref=decision_ref,
                                   evidence_refs=sorted(grounds), rationale=decision["rationale"]))
    if entered is None:
        return None
    record = dict(scope="run", candidates=entered["candidates"], spec_version=entered["spec_version"],
                  rationale=decision["rationale"], evidence_refs=decision["evidence_refs"],
                  goal_version=request["goal_version"], context_ref=request["context_ref"],
                  through_cursor=task["event_cursor"], decision_ref=decision_ref)
    store.append_event(conn, run_id=rid, kind="v62.convergence.entered", actor=OWNER,
                       subject_kind="run", subject_id=rid, payload=record)
    return record


def refresh(conn, identity, error):
    """Release a stale terminal choice so the normal Lead scheduler can freeze the new information."""
    from . import v6_decisions, v6_goal

    rid = identity["run_id"]
    with store.transaction(conn, transaction_owner=OWNER, operation=error.details["reason"], run_id=rid):
        try:
            native = {k: identity[k] for k in ("run_id", "thread_id", "turn_id", "task_id", "lease_id")}
            _, task, _, _ = v6_decisions.authority(conn, **native)
        except GxError:
            return
        if task["scope"] != "run" or not late_evidence(conn, rid, task["event_cursor"]):
            return
        v6_goal.cancel_decision(conn, rid, task["task_id"], reason=error.message)
        store.append_event(conn, run_id=rid, kind="v6.decision.superseded", actor=OWNER,
                           subject_kind="task", subject_id=task["task_id"],
                           payload={"scope": "run", **error.details})
