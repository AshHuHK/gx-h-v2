"""Publish one sourced, immutable report per module and release reports to its Lead by round."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..artifacts import ArtifactService, delivery
from ..domain import ids
from ..errors import ArtifactHashMismatch, IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from . import manifest as execution
from . import v6_decisions, v6_goal, v6_jobs

SCHEMA = "gx.v62.module_report_bundle.1"
_FIELDS = {"operation_key", "status", "findings", "artifact_refs", "gaps", "next_steps"}


def _strings(items):
    return isinstance(items, list) and all(isinstance(v, str) and v.strip() for v in items)


def _validate(payload, *, current=False):
    extra = {"gap_criteria", "metric_refs", "qualitative_notes"} if current else {"gap_criteria"}
    if not isinstance(payload, dict) or not _FIELDS <= set(payload) <= _FIELDS | extra:
        raise SchemaInvalid("Supply report content, status, artifact references and an operation key")
    if (not isinstance(payload["operation_key"], str) or not payload["operation_key"].strip()
            or payload["status"] not in ("COMPLETE", "PARTIAL") or not isinstance(payload["findings"], list)):
        raise SchemaInvalid("Invalid module report operation key, status or findings")
    if not all(_strings(payload[k]) for k in ("artifact_refs", "gaps", "next_steps")):
        raise SchemaInvalid("Report references, gaps and next steps must be lists of nonempty strings")
    selectors, notes = payload.get("metric_refs", []), payload.get("qualitative_notes", [])
    if not isinstance(notes, list) or not isinstance(selectors, list) or any(
            not isinstance(item, dict) or set(item) != {"artifact_ref", "metric_id"} or
            not _strings([item["artifact_ref"], item["metric_id"]]) for item in selectors):
        raise SchemaInvalid("Supply qualitative notes and pairs of named output reference and metric ID")
    for finding in [*payload["findings"], *notes]:
        if (not isinstance(finding, dict) or not {"text", "source_refs"} <= finding.keys()
                or finding.keys() - {"text", "source_refs", "kind"}
                or not isinstance(finding["text"], str) or not finding["text"].strip()
                or not _strings(finding["source_refs"]) or not finding["source_refs"]
                or finding.get("kind", "observation") not in ("observation", "counterevidence", "support")):
            raise SchemaInvalid("Every finding needs its text, cited sources and a supported kind")
    if payload["status"] == "PARTIAL" and not payload["gaps"]:
        raise SchemaInvalid("A partial report must name its unfinished work")
    if payload["status"] == "COMPLETE" and not (payload["findings"] or payload["gaps"] or notes or selectors):
        raise SchemaInvalid("A completed report needs a sourced finding or a documented gap")


def _task_module(conn, rid, tid):
    row = conn.execute(
        "SELECT m.* FROM v62_modules m JOIN v6_decision_tasks d USING(scope,run_id) "
        "WHERE d.run_id=? AND d.task_id=?", (rid, tid)).fetchone()
    if not row:
        raise ScopeDenied("This task does not own a module in this Run")
    return row


def _replay(conn, mid, key, sha, identity):
    row = conn.execute("SELECT * FROM v62_module_reports WHERE module_instance_id=?", (mid,)).fetchone()
    if not row:
        return None
    receipt = store.loads(row, "receipt_json")
    if key != row["operation_key"] or sha != row["request_sha256"]:
        raise IdempotencyConflict("This module has already delivered a different final report")
    if identity is not None and any(receipt["author"].get(k) != identity[k] for k in
                                    ("task_id", "lease_id", "thread_id", "turn_id")):
        raise ScopeDenied("Only the original native binding may replay its submitted report")
    return receipt


def _module_evidence(conn, module):
    """Index confirmed tool reads and Job outputs. A grant by itself is insufficient to cite a source."""
    from .v62_modules import job_outputs
    from .v62_native import operation_receipts
    rid = module["run_id"]
    proofs = {}
    for op in operation_receipts(conn, rid, module["module_instance_id"]):
        for ref in set(op.get("source_refs", ())) | set(op.get("artifact_refs", ())):
            proofs.setdefault(ref, []).append({"tool": op["tool"], "operation_key": op["operation_key"],
                                             "result_sha256": op["result_sha256"],
                                             "source_sha256": op.get("artifact_sha256", {}).get(ref, op.get("source_sha256"))})
    jobs = conn.execute(
        "SELECT n.node_id,n.result_ref,n.exec_key,n.job_id FROM v6_nodes n "
        "WHERE n.run_id=? AND n.branch_id=? AND n.status IN ('SUCCEEDED','REUSED') ORDER BY n.node_id",
        (rid, module["branch_id"]))
    for row in jobs:
        manifest = execution.read_manifest(conn, row["job_id"])
        end = manifest["completion"]
        if not end or end["status"] != "SUCCEEDED":
            continue
        identity = v6_jobs.confirmed_result_identity(conn, row["node_id"], manifest)
        for output in job_outputs(conn, rid, row, manifest):
            proofs.setdefault(output["artifact_ref"], []).append(dict(
                node_id=row["node_id"], job_id=row["job_id"], **identity,
                **{k: output[k] for k in ("output_name", "nature") if k in output},
                output_sha256=output["sha256"], admission_sha256=manifest["admission_hash"],
                manifest_sha256=manifest["manifest_hash"]))
    return proofs


def _sources(conn, module, refs, evidence):
    service = ArtifactService(conn)
    sources = []
    for ref in sorted(refs):
        if ref not in evidence:
            raise ScopeDenied("The module must read the cited artifact or complete its producing Job")
        meta = service.describe(ref)
        if meta["run_id"] != module["run_id"]:
            raise ScopeDenied("The cited record belongs to another Run")
        if (not service.verify(ref)["ok"] or
                any((p.get("output_sha256") or p.get("source_sha256") or meta["sha256"]) != meta["sha256"]
                    for p in evidence[ref])):
            raise ArtifactHashMismatch("The module source bytes differ from their registered digest")
        sources.append({"artifact_ref": ref, "sha256": meta["sha256"], "kind": meta["kind"],
                        "receipts": evidence[ref]})
    return sources


@dataclass(frozen=True)
class Report:
    module: dict
    task: dict
    identity: dict | None
    payload: dict
    request_sha: str
    bundle: dict
    manifest: delivery.PreparedManifest


def _stage(conn, module, task, identity, payload, evidence, *, attempt):
    binds = {"kind": "MODULE" if identity else "GX", "task_id": task["task_id"],
             "context_ref": task["context_ref"], **(identity or {})}
    sources = set(payload["artifact_refs"])
    for finding in [*payload["findings"], *payload.get("qualitative_notes", [])]:
        sources.update(finding["source_refs"])
    sources.update(selector["artifact_ref"] for selector in payload.get("metric_refs", []))
    checked = _sources(conn, module, sources, evidence)
    bundle = {**{k: module[k] for k in ("module_instance_id", "module_id", "module_version", "round",
                                         "run_id", "scope", "goal_version", "parent_context_ref")},
              "schema": SCHEMA, "version": 1, "author": binds, "base": json.loads(task["base_json"]),
              "status": payload["status"], "findings": payload["findings"], "sources": checked,
              "gaps": payload["gaps"], "artifact_refs": payload["artifact_refs"], "next_steps": payload["next_steps"]}
    from . import v621_metrics, v621_policy
    if v621_policy.enabled(conn, module["run_id"]):
        v621_metrics.render_report(conn, module["run_id"], bundle, current=v621_policy.current(conn, module["run_id"]),
                                   metric_refs=payload.get("metric_refs", ()),
                                   qualitative_notes=payload.get("qualitative_notes", ()))
    if "gap_criteria" in payload:
        from . import v621_gaps
        bundle.update(v621_gaps.register(conn, module, payload, checked))
    service = ArtifactService(conn)
    params = {"run_id": module["run_id"], "task_id": task["task_id"], "attempt": max(1, attempt)}
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=service.inbox_dir(module["run_id"])) as source:
        source.write(ids.canonical_json(bundle))
        source.flush()
        manifest = delivery.prepare_manifest(service, **params, provenance=binds,
            entries=[{"kind": "module_report_bundle", "media_type": "application/json",
                      "inbox_path": Path(source.name).name}])
        original = delivery.load_manifest(service, manifest, **params)
    return Report(dict(module), dict(task), identity, payload, ids.sha256_of_obj(payload), bundle, original)


def _report_authority(conn, identity, module):
    root, task, _, _ = v6_decisions.authority(conn, **identity)
    name = "gx_submit_module_report"
    if (name not in json.loads(root["tool_names_json"] or "[]")
            or name not in store.loads(module, "grant_json")["tools"]
            or name not in store.loads(task, "snapshot_json")["allowed_tools"]):
        raise ScopeDenied("Only an assigned module report tool may publish this product")
    return root, task


def prepare_report(conn, *, run_id, task_id, lease_id, thread_id, turn_id, call_id, payload):
    if conn.in_transaction:
        raise RuntimeError("Prepare report bytes and verify original evidence outside the write transaction")
    from . import v621_policy
    try:
        _validate(payload, current=v621_policy.current(conn, run_id))
    except SchemaInvalid as exc:
        exc.details["category"] = "module_report_structure"
        raise
    module = _task_module(conn, run_id, task_id)
    identity = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, thread_id=thread_id, turn_id=turn_id)
    saved = _replay(conn, module["module_instance_id"], payload["operation_key"], ids.sha256_of_obj(payload), identity)
    if saved:
        return saved
    root, task = _report_authority(conn, identity, module)
    return _stage(conn, module, task, dict(identity, call_id=call_id), payload, _module_evidence(conn, module),
                  attempt=root["attempt"])


def _released(conn, rid, round_number=None, through_cursor=None):
    query = ("SELECT e.payload_json FROM v62_module_wakes w JOIN events e USING(event_id) WHERE w.run_id=?")
    params = [rid]
    if round_number is not None:
        query += " AND w.round=?"
        params.append(round_number)
    if through_cursor is not None:
        query += " AND w.event_id<=?"
        params.append(through_cursor)
    ids_seen = set()
    for row in conn.execute(query, params):
        ids_seen.update(json.loads(row[0])["module_instance_ids"])
    return ids_seen


def _wake(conn, module, key, reason, rows):
    if not rows:
        return None
    rid, number = module["run_id"], module["round"]
    saved = conn.execute("SELECT event_id FROM v62_module_wakes WHERE run_id=? AND wake_key=?", (rid, key)).fetchone()
    if saved:
        return saved[0]
    from . import v621_policy
    origin = {"goal_version": module["goal_version"]} if v621_policy.enabled(conn, rid) else {}
    reports = [r["report_ref"] for r in rows]
    mids = [r["module_instance_id"] for r in rows]
    event = store.append_event(conn, run_id=rid, kind="v62.module.wake", actor="GX",
        subject_kind="run", subject_id=rid, evidence_level="EXECUTION_OBSERVED",
        payload={**origin, "scope": "run", "round": number, "wake_key": key, "reason": reason, "report_refs": reports,
                 "module_instance_ids": mids})
    conn.execute("INSERT INTO v62_module_wakes(run_id,wake_key,round,event_id) VALUES(?,?,?,?)",
                 (rid, key, number, event))
    return event


def release_parent_rounds(conn, run_id, task_id):
    """Accepting or retiring the opening Lead seals membership in its rounds."""
    rounds = conn.execute("SELECT DISTINCT run_id,goal_version,round FROM v62_modules WHERE run_id=? AND parent_task_id=?",
                          (run_id, task_id)).fetchall()
    for member in rounds:
        release_round(conn, member)


def reconcile_rounds(conn, run_id):
    """Recover unpublished reports when a delegating task terminated by another path."""
    active = ",".join("?" for _ in v6_goal.ACTIVE_TASKS)
    from . import v621_policy
    scoped = v621_policy.enabled(conn, run_id)
    rounds = conn.execute(f"""SELECT DISTINCT m.run_id,m.goal_version,m.round FROM v62_module_reports r
        JOIN v62_modules m USING(run_id,module_instance_id) JOIN tasks t ON t.task_id=m.parent_task_id
        WHERE m.run_id=? AND t.status NOT IN ({active})
        AND NOT EXISTS(SELECT 1 FROM v62_modules peer
                        WHERE peer.run_id=m.run_id AND peer.round=m.round AND peer.status='OPEN'
                          AND (NOT ? OR peer.goal_version=m.goal_version))
        AND NOT EXISTS(SELECT 1 FROM v62_module_wakes w WHERE w.run_id=m.run_id AND
                       (w.wake_key='early:'||m.module_instance_id OR w.wake_key=
                        CASE WHEN ? AND m.goal_version>1 THEN 'goal:'||m.goal_version||':round:'||m.round
                             ELSE 'round:'||m.round END))""", (run_id, *v6_goal.ACTIVE_TASKS, scoped, scoped)).fetchall()
    if not rounds:
        return False
    changed = False
    with store.transaction(conn, transaction_owner="ResearchModule", operation="release_sealed_round", run_id=run_id):
        for member in rounds:
            changed = bool(release_round(conn, member)) or changed
    return changed


def release_round(conn, module, *, publishing=None):
    """Called under the publication/cancellation transaction once a member changes status."""
    if not conn.in_transaction:
        raise RuntimeError("Release the round in the member's state-change transaction")
    from . import v621_policy
    rid, number = module["run_id"], module["round"]
    version = module["goal_version"] if v621_policy.enabled(conn, rid) else None
    # The Lead may be about to delegate another member of this round. Its durable task
    # yields membership with its decision even across native reconnects or GX restart.
    active = ",".join("?" for _ in v6_goal.ACTIVE_TASKS)
    if conn.execute(f"""SELECT 1 FROM v62_modules m JOIN tasks t ON t.task_id=m.parent_task_id
                    WHERE m.run_id=? AND m.round=? AND (? IS NULL OR m.goal_version=?)
                      AND (m.status='OPEN' OR t.status IN ({active})) LIMIT 1""",
                    (rid, number, version, version, *v6_goal.ACTIVE_TASKS)).fetchone():
        return None
    released = _released(conn, rid, number)
    rows = conn.execute("SELECT r.module_instance_id,r.report_ref FROM v62_module_reports r "
                        "JOIN v62_modules m USING(module_instance_id,run_id) "
                        "WHERE m.run_id=? AND m.round=? AND (? IS NULL OR m.goal_version=?) ORDER BY r.rowid",
                        (rid, number, version, version))
    undisclosed = [r for r in rows if r["module_instance_id"] not in released]
    if publishing:
        undisclosed.append(publishing)
    key = f"goal:{version}:round:{number}" if version and version > 1 else f"round:{number}"
    return _wake(conn, module, key, "round_complete", undisclosed)


def _early_reason(module, bundle):
    watches = set(json.loads(module["wake_json"]))
    if "counterevidence" in watches and any(f.get("kind") == "counterevidence" for f in bundle["findings"]):
        return "counterevidence"
    if "gap" in watches and bundle["gaps"]:
        return "gap"
    if "partial" in watches and bundle["status"] == "PARTIAL":
        return "partial"
    return None


def exhausted_report_repairs(conn, task):
    if not task or task["status"] != "BLOCKED":
        return None
    # Only the last, exhausted lease of this task can turn a blocked module into a handback.
    failed = conn.execute(
        """SELECT 1 FROM p0_roots r JOIN p0_root_terminals e USING(thread_id)
           JOIN p0_product_repairs p USING(thread_id,turn_id,call_id)
           WHERE r.task_id=? AND r.attempt=? AND e.reason='GX_REPAIR_LIMIT'
             AND p.category='module_report_structure' AND json_extract(p.response_json,'$.task_status')='BLOCKED'""",
        (task["task_id"], task["attempt"])).fetchone()
    return "module report structural repair and task attempts exhausted" if failed else None


def _partial_reason(conn, module):
    from . import v62_budget, v62_modules
    if reason := v62_budget.exhausted(conn, module) or exhausted_report_repairs(conn, v62_modules._task(conn, module)):
        return reason
    if conn.execute("SELECT 1 FROM budget_reservations WHERE budget_ref=? AND status='HELD' LIMIT 1",
                    (module["budget_ref"],)).fetchone():
        return None
    marks = ",".join("?" for _ in v6_goal.ACTIVE_TASKS)
    active = conn.execute("SELECT 1 FROM v6_decision_tasks d JOIN tasks t USING(task_id) WHERE d.run_id=? "
                          f"AND d.scope=? AND t.status IN ({marks}) LIMIT 1",
                          (module["run_id"], module["scope"], *v6_goal.ACTIVE_TASKS)).fetchone()
    pending = conn.execute("SELECT status FROM v6_nodes WHERE run_id=? AND branch_id=?",
                           (module["run_id"], module["branch_id"]))
    if (not active and all(row[0] in v6_jobs.NODE_TERMINAL for row in pending)
            and v62_budget.remaining(conn, module["budget_ref"]).get("worker_tasks", 0) < 1):
        return "module worker task allowance exhausted"
    return None


def commit_report(conn, plan):
    from . import v62_budget, v62_modules
    if isinstance(plan, dict):
        return plan
    module, payload, identity = plan.module, plan.payload, plan.identity
    rid, mid = module["run_id"], module["module_instance_id"]
    key = payload["operation_key"]
    with store.transaction(conn, transaction_owner="ResearchModule", operation="publish_report", run_id=rid):
        if not identity:
            # A worker may have published, or the Lead may have cancelled, after GX staged this handback.
            terminal = conn.execute("SELECT status FROM v62_modules WHERE run_id=? AND module_instance_id=?",
                                    (rid, mid)).fetchone()
            if terminal and terminal[0] != "OPEN":
                prior = conn.execute("SELECT receipt_json FROM v62_module_reports WHERE module_instance_id=?",
                                     (mid,)).fetchone()
                return json.loads(prior[0]) if prior else None
        saved = _replay(conn, mid, key, plan.request_sha, identity)
        if saved:
            return saved
        current = v62_modules.require_active(conn, rid, module["scope"])
        root = None
        if identity:
            native = {k: v for k, v in identity.items() if k != "call_id"}
            root, task = _report_authority(conn, native, current)
            if task["context_ref"] != plan.task["context_ref"]:
                raise StaleRevision("The module submitted a different frozen context")
        if (current["grant_json"], current["snapshot_json"]) != (module["grant_json"], module["snapshot_json"]):
            raise StaleRevision("The module's authorized premises changed before publication")
        if not identity and _partial_reason(conn, current) != payload["gaps"][0]:
            return None  # New work, evidence or funding arrived during GX's preparation.
        # Tool receipts and completion manifests are durable. Recheck source and output identities under the fence.
        evidence = _module_evidence(conn, current)
        if not identity and set(evidence) != {source["artifact_ref"] for source in plan.bundle["sources"]}:
            return None
        for source in plan.bundle["sources"]:
            if not all(item in evidence.get(source["artifact_ref"], []) for item in source["receipts"]):
                raise StaleRevision("A source receipt changed before publication")
        ref, = delivery.register_manifest(ArtifactService(conn), plan.manifest,
                                         actor=identity["thread_id"] if identity else "GX")
        conn.execute("UPDATE v62_modules SET status=? WHERE module_instance_id=?", (payload["status"], mid))
        if root:
            v6_decisions.accept_product(conn, root, plan.request_sha, ref)
        else:
            marks = ",".join("?" for _ in v6_goal.ACTIVE_TASKS)
            for row in conn.execute("SELECT t.task_id FROM v6_decision_tasks d JOIN tasks t USING(task_id) "
                                    f"WHERE d.run_id=? AND d.scope=? AND t.status IN ({marks})",
                                    (rid, module["scope"], *v6_goal.ACTIVE_TASKS)).fetchall():
                v6_goal.cancel_decision(conn, rid, row[0], reason="GX delivered a sourced partial report")
        receipt = {k: plan.bundle[k] for k in ("module_instance_id", "module_id", "module_version", "run_id",
                                              "scope", "round", "version", "goal_version", "status", "author", "base")}
        receipt.update(operation_key=key, report_ref=ref, artifact_ref=ref,
                       report_sha256=plan.manifest.entries[0]["sha256"], source_refs=[s["artifact_ref"] for s in plan.bundle["sources"]])
        receipt["event_id"] = store.append_event(conn, run_id=rid, kind="v62.module.reported", actor=(
            identity["thread_id"] if identity else "GX"), subject_kind="module", subject_id=mid,
            payload=receipt, evidence_level="EXECUTION_OBSERVED")
        # Completing the last member releases all remaining reports together, even if this member has a watch.
        wake = release_round(conn, module, publishing=receipt)
        reason = _early_reason(module, plan.bundle)
        if wake is None and not reason:
            from . import v62_rounds
            reason = v62_rounds.early_reason(conn, module, plan.bundle)
        if wake is None and reason and mid not in _released(conn, rid, module["round"]):
            wake = _wake(conn, module, "early:" + mid, reason, [receipt])
        receipt["wake_event_id"] = wake
        conn.execute("INSERT INTO v62_module_reports(module_instance_id,run_id,report_ref,operation_key,"
                     "request_sha256,receipt_json) VALUES(?,?,?,?,?,?)", (mid, rid, ref, key, plan.request_sha, store._j(receipt)))
        v62_budget.close_if_settled(conn, current["budget_ref"])
        return receipt


def submit_report(conn, **call):
    return commit_report(conn, prepare_report(conn, **call))


def partial(conn, module, reason):
    """Finish from durable evidence with no model reservation, call or synthetic worker lease."""
    from .v62_modules import require_active
    if conn.in_transaction:
        raise RuntimeError("Prepare a GX partial report outside the writer")
    mid, rid = module["module_instance_id"], module["run_id"]
    saved = conn.execute("SELECT receipt_json FROM v62_module_reports WHERE module_instance_id=?", (mid,)).fetchone()
    if saved:
        return json.loads(saved[0])
    require_active(conn, rid, module["scope"])
    task = conn.execute("SELECT d.*,t.attempt FROM v6_decision_tasks d JOIN tasks t USING(task_id) "
                        "WHERE d.run_id=? AND d.scope=? ORDER BY d.rowid DESC LIMIT 1", (rid, module["scope"])).fetchone()
    if not task:
        raise SchemaInvalid("GX partial must reference the module's frozen decision task")
    evidence = _module_evidence(conn, module)
    refs = sorted(evidence)
    payload = dict(operation_key="gx.partial:" + mid, status="PARTIAL", findings=[], artifact_refs=refs,
                   gaps=[reason], next_steps=[])
    return commit_report(conn, _stage(conn, module, task, None, payload, evidence, attempt=task["attempt"]))


def _size(value):
    return len(ids.canonical_json(value).encode("utf-8"))


def _fit_string(item, key, text, available):
    """Largest valid Unicode prefix that also fits after JSON string escaping."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        item[key] = text[:mid]
        if _size(item) <= available:
            lo = mid
        else:
            hi = mid - 1
    item[key] = text[:lo]
    return lo


def _base(bundle, ref):
    return {**{k: bundle[k] for k in ("module_instance_id", "module_id", "module_version", "round", "version", "goal_version", "status")},
            **({"applicability": bundle["applicability"]} if "applicability" in bundle else {}),
            "report_ref": ref}


def _excerpt(text, limit=240):
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit - 3].decode("utf-8", errors="ignore") + "…"


def _index_entry(bundle, ref):
    # All released counterevidence and its original citation links remain discoverable by
    # the Lead, even when the native packet cannot fit one summary per report.
    entry = {**_base(bundle, ref)}
    counters = [{"text": ("A named measurement was flagged for review." if finding.get("metric_refs")
                          else _excerpt(finding["text"])), "source_refs": finding["source_refs"],
                 **({"evidence_status": finding["evidence_status"]} if "evidence_status" in finding else {})}
                for finding in bundle["findings"] if finding.get("kind") == "counterevidence"]
    if counters:
        entry["counterevidence"] = counters
    return entry


def _compact(index):
    entry = {k: index[k] for k in ("module_instance_id", "status", "report_ref", "applicability") if k in index}
    if index.get("counterevidence"):
        first = index["counterevidence"][0]
        entry["counterevidence"] = [{**first, "source_refs": first["source_refs"][:1]}]
    entry["truncated"] = True
    return entry


def _preview(bundle, index, limit):
    base = _base(bundle, index["report_ref"])
    full = {**base, **{k: bundle[k] for k in ("findings", "gaps", "next_steps", "artifact_refs")},
            "truncated": False}
    proof_fields = ("verified_metrics", "source_quotations") if "verified_metrics" in bundle else ("metrics", "source_quotations")
    for field in (*proof_fields, "qualitative_notes"):
        if bundle.get(field):
            full[field] = bundle[field]
    if index.get("counterevidence"):
        full["counterevidence"] = index["counterevidence"]
    if _size(full) <= limit:
        return full
    preview = _compact(index)
    if _size(preview) > limit:
        if "counterevidence" not in preview:
            return None
        counter = preview["counterevidence"][0]
        text = counter["text"]
        room = limit - _size(preview) + _size(counter)
        if not _fit_string(counter, "text", text, room):
            return None
    for field in proof_fields:
        if bundle.get(field):
            preview[field] = []
            for proof in bundle[field]:
                preview[field].append(proof)
                if _size(preview) > limit:
                    preview[field].pop()
                    break
            if not preview[field]:
                del preview[field]
    for key, value in base.items():
        if key not in preview:
            preview[key] = value
            if _size(preview) > limit:
                del preview[key]
    for field in ("findings", "gaps", "next_steps", "artifact_refs"):
        if not bundle[field]:
            continue
        preview[field] = []
        if _size(preview) > limit:
            del preview[field]
            continue
        for value in bundle[field]:
            if field == "findings":
                if value.get("kind") == "counterevidence":
                    continue  # The priority cue already carries the report link and first citation.
                if value.get("metric_refs") and not set(value["metric_refs"]) <= {
                        m["metric_ref"] for m in preview.get("verified_metrics", ())}:
                    continue  # A clipped or missing proof cannot deliver a verified numerical reading.
                entry = {**value, "source_refs": value["source_refs"][:1], "text": ""}
                preview[field].append(entry)
                room = limit - _size(preview) + _size(entry)
                if not _fit_string(entry, "text", value["text"], room):
                    preview[field].pop()
                    break
            else:
                preview[field].append(value)
                if _size(preview) > limit:
                    preview[field].pop()
                    break
    notes = bundle.get("qualitative_notes")
    if notes:
        preview["qualitative_notes"] = notes
        if _size(preview) > limit:
            del preview["qualitative_notes"]
    return preview


def _released_rows(conn, rid, through_cursor):
    visible = _released(conn, rid, through_cursor=through_cursor)
    from . import v621_goal_scope
    version = v621_goal_scope.active(conn, rid)
    rows = conn.execute("""SELECT r.module_instance_id,r.report_ref,r.receipt_json FROM v62_module_reports r
        JOIN v62_modules m USING(module_instance_id,run_id) WHERE r.run_id=? AND (? IS NULL OR m.goal_version=?)
        ORDER BY r.rowid""", (rid, version, version))
    return [row for row in rows if row["module_instance_id"] in visible]


def released_report_refs(conn, run_id, through_cursor=None):
    return [row["report_ref"] for row in _released_rows(conn, run_id, through_cursor)]


def released_evidence_refs(conn, run_id, through_cursor=None):
    refs = []
    for row in _released_rows(conn, run_id, through_cursor):
        refs.append(row["report_ref"])
        refs.extend(store.loads(row, "receipt_json")["source_refs"])
    return list(dict.fromkeys(refs))


def report_views(conn, run_id, *, per_report_bytes=4096, total_bytes=16384, through_cursor=None):
    """A bounded projection plus a complete, source-linked index for the archived Lead context."""
    from .v62_modules import _assert_sources
    service = ArtifactService(conn)
    released = []
    for row in _released_rows(conn, run_id, through_cursor):
        ref = row["report_ref"]
        bundle = service.read_json(ref)
        module = conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND module_instance_id=?",
                              (run_id, row["module_instance_id"])).fetchone()
        try:
            _assert_sources(conn, run_id, module["goal_version"], store.loads(module, "grant_json"),
                            store.loads(module, "snapshot_json"), for_projection=True)
        except StaleRevision:
            bundle["applicability"] = "historical_revalidation_required"
        released.append((bundle, _index_entry(bundle, ref)))

    # Decide which entries can fit before adding ordinary prose. Older adverse
    # findings retain priority even when newer routine reports fill the same cursor.
    order = sorted(range(len(released)), key=lambda i: (not released[i][1].get("counterevidence"), -i))
    previews = {}
    remaining = total_bytes - 2
    for position, i in enumerate(order):
        bundle, index = released[i]
        available = remaining - bool(previews)
        share = available // (len(order) - position)
        limit = min(per_report_bytes, available, max(share, _size(_compact(index))))
        preview = _preview(bundle, index, limit)
        if preview is not None:
            previews[i] = preview
            remaining = available - _size(preview)
    return [previews[i] for i in sorted(previews)], [index for _, index in released]


def report_projection(conn, run_id, **limits):
    summaries, _ = report_views(conn, run_id, **limits)
    return summaries
