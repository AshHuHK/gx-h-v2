"""Deliberate, recorded workflow-pin transitions through the existing decision owner.

Routine maintenance uses current Native Root authority and standing policy. Historical
person decisions remain readable for their original proposal. Budgets never change here.
"""
from __future__ import annotations
from typing import Any

from .. import contracts
from ..context import Ctx, SCOPE_OPERATOR
from ..domain import clock, ids, machines
from ..errors import ApprovalRequired, ContentHashMismatch, DependencyUnmet, GxError, ScopeDenied
from ..governance import approvals
from ..persistence import store
from ..registry import Subject


def _generation(conn: Any, run_id: str) -> int:
    row = conn.execute("SELECT MAX(event_id) FROM events WHERE run_id=? AND kind='run.repinned'", (run_id,)).fetchone()
    return int(row[0] or 0)


def require_native_root(ctx: Ctx, run: Any) -> None:
    """Use existing callback and coordinator owners for a current-run operation."""
    from ..hostobs import native_context as native

    call = native.call_for_ctx(ctx)
    host = store.loads(run, "host_json", {})
    if (ctx.transport != native.TRANSPORT or ctx.principal.scope != SCOPE_OPERATOR
            or native.source_of_run(run) != native.SOURCE
            or call.thread_id != call.root_thread_id or call.tool_name != ctx.tool
            or call.session_id != host.get("session_ref")
            or call.root_thread_id != host.get("policy_selection", {}).get("native_agent_ref")
            or call._issuer.recovery_only or not ctx.principal.may_access_project(run["project_ref"])
            or ctx.authz.get("run_id") != run["run_id"]
            or ctx.authz.get("project_ref") != run["project_ref"]):
        raise ScopeDenied("this operation needs the current run's actual Native Root")
    lease = store.coordinator_lease(ctx.conn, run["run_id"])
    if not store.coordinator_lease_live(lease) or lease["owner"] != ctx.principal.subject:
        raise ScopeDenied("the actual Native Root must hold its own current coordinator lease")


def require_quiescent(conn: Any, run: Any) -> None:
    """Leave active or uncertain work to its original owner; preserve settled history."""
    from .engine import _advance_live

    run_id = run["run_id"]
    if run["status"] in machines.terminal_states("run"):
        raise DependencyUnmet("a terminal run cannot change its workflow pins")
    checkpoint = conn.execute("SELECT state_json FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    if checkpoint is not None and _advance_live(store.loads(checkpoint, "state_json", {})):
        raise DependencyUnmet("another call is advancing the run")
    checks = (
        "SELECT 1 FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE t.run_id=? AND l.released_at IS NULL LIMIT 1",
        "SELECT 1 FROM tasks WHERE run_id=? AND status IN ('CLAIMED','RUNNING','SUBMITTED','VALIDATING') LIMIT 1",
        "SELECT 1 FROM jobs WHERE run_id=? AND (status NOT IN ('SUCCEEDED','FAILED','CANCELLED') OR side_effect_status='UNKNOWN') LIMIT 1",
        "SELECT 1 FROM plugin_invocations WHERE run_id=? AND status IN ('ADMITTED','OUTCOME_UNKNOWN') LIMIT 1",
        "SELECT 1 FROM budget_reservations r JOIN budgets b ON b.budget_ref=r.budget_ref WHERE b.run_id=? AND r.status='HELD' LIMIT 1",
    )
    if any(conn.execute(sql, (run_id,)).fetchone() for sql in checks):
        raise DependencyUnmet("workflow pins change only after task leases, jobs, plugin effects and budget holds settle")


@approvals.condition("run_repin")
def compatible_transition(ctx: Ctx, *, subject: Subject, request: Any, rule: Any) -> approvals.ConditionResult:
    """Generic standing source transition, bounded by installed compatibility facts."""
    from . import service

    no = lambda reason: approvals.ConditionResult(False, reasons=[reason])
    grant = rule.get("grant_when", {})
    if grant.get("workflow_source_only") is not True or not isinstance(request, dict):
        return no("the rule does not allow compatible workflow source maintenance")
    if (subject.kind, subject.id, subject.sha256, subject.version) != ("run", request.get("run_id"), ids.sha256_of_obj(request), None):
        return no("the subject does not bind the complete old/new proposal")
    old, new = request.get("old", {}), request.get("new", {})
    keys = {"contract_version", "contract_sha256", "workflow_sha256", "agent_policy_sha256"}
    if (not isinstance(old, dict) or not isinstance(new, dict)
            or set(old) != keys or set(new) != keys or old == new
            or old["contract_version"] != new["contract_version"]
            or old["agent_policy_sha256"] != new["agent_policy_sha256"]):
        return no("routine source maintenance preserves the contract version and agent policy")
    if old["contract_sha256"] != new["contract_sha256"]:
        meta, digest = store.read_meta(ctx.conn), contracts.contract_digest()
        if (grant.get("compatible_contract_update") is not True
                or new["contract_sha256"] != digest["__combined__"]
                or str(new["contract_version"]) != meta.get(store.META_CONTRACT_VERSION)
                or meta.get(store.SCHEMA_META_KEY) != digest["db_schema.sql"]):
            return no("contract maintenance needs the same declared version and unchanged owned-store schema")
    run = ctx.conn.execute("SELECT * FROM runs WHERE run_id=?", (subject.id,)).fetchone()
    if run is None or ctx.tool != "gx_run_control":
        return no("a deliberate current-run workflow_repin operation is required")
    try:
        require_native_root(ctx, run)
        require_quiescent(ctx.conn, run)
        if (service.run_pins(run) != old or _generation(ctx.conn, subject.id) != request.get("after_repin_event_id")
                or (run["workflow_id"], run["workflow_version"]) != (request.get("workflow_id"), request.get("workflow_version"))
                or service.installed_pins(service._pinned_workflow(ctx.conn, run), run=run) != new):
            return no("the run generation or exact installed workflow changed")
    except GxError as error:
        return no(error.message)
    return approvals.ConditionResult(True, facts={"run_id": subject.id,
        "subject_sha256": subject.sha256, "after_repin_event_id": request["after_repin_event_id"],
        "old": old, "new": new})


def _standing_grant(ctx: Ctx, row: Any, subject: Subject, proposal: Any) -> None:
    """Recheck the original policy decision against current owners before applying it."""
    document, _sha = approvals.policy()
    # The original approval retains its policy digest. Unrelated policy edits do
    # not invalidate it: the same active rule must still pass its current owner.
    policy_ref, separator, original_sha = str(row["policy_ref"] or "").rpartition("@")
    matching = [rule for rule in document.get("rules", [])
                if rule.get("action_kind") == "run_repin"
                and policy_ref == f"config/{approvals.POLICY_FILE}#{rule.get('id')}"]
    if ("run_repin" in document.get("never_standing", {}) or len(matching) != 1
            or not separator or row["decided_by"] != f"policy:{original_sha}"
            or not compatible_transition(ctx, subject=subject, request=proposal, rule=matching[0]).ok):
        raise ApprovalRequired("the compatible workflow transition's standing decision no longer applies")


def ensure(ctx: Ctx, run: Any, workflow: Any, *, reason: str | None = None) -> Any:
    from . import service, seams

    old, new = service.run_pins(run), service.installed_pins(workflow, run=run)
    if old == new:
        service.require_pins(run, workflow)
        return run
    require_quiescent(ctx.conn, run)
    generation = _generation(ctx.conn, run["run_id"])
    proposal = {"after_repin_event_id": generation, "run_id": run["run_id"], "workflow_id": run["workflow_id"],
                "workflow_version": run["workflow_version"], "old": old, "new": new}
    subject = Subject(kind="run", id=run["run_id"], sha256=ids.sha256_of_obj(proposal))
    approval = approvals.request(ctx, action_kind="run_repin", subject=subject,
                                 run_id=run["run_id"], request=proposal)
    if approval["status"] != "GRANTED":
        raise ApprovalRequired("the installed run pins changed; the exact old/new transition is not granted",
                               details={"approval_id": approval["approval_id"], "subject_sha256": subject.sha256,
                                        "old": old, "new": new})
    with store.transaction(ctx.conn) as tx:
        current = tx.execute("SELECT * FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
        now = service.installed_pins(workflow, run=run)
        if service.run_pins(current) == new:
            return current
        if service.run_pins(current) != old or now != new or _generation(tx, run["run_id"]) != generation:
            raise ContentHashMismatch("the run or installation changed after the re-pin proposal; a new review is needed")
        require_quiescent(tx, current)
        row = tx.execute("SELECT * FROM approvals WHERE approval_id=?", (approval["approval_id"],)).fetchone()
        if (row["status"] != "GRANTED" or row["subject_sha256"] != subject.sha256
                or row["action_kind"] != "run_repin" or row["subject_kind"] != "run" or row["subject_id"] != subject.id
                or row["run_id"] != subject.id or store.loads(row, "request_json", {}) != proposal
                or row["expires_at"] and clock.is_past(row["expires_at"])):
            raise ApprovalRequired("the exact one-time run re-pin decision no longer applies")
        if row["policy_ref"]:
            _standing_grant(ctx.child(conn=tx), row, subject, proposal)
        elif not str(row["decided_by"] or "").startswith("person:"):
            raise ApprovalRequired("only the original person or standing-policy decision may be applied")
        policy, policy_sha = seams.agent_policy_for_run(run)
        if policy_sha != new["agent_policy_sha256"]:
            raise ContentHashMismatch("the agent policy changed while the re-pin was being applied")
        tx.execute("UPDATE runs SET contract_version=?,contract_sha256=?,workflow_sha256=?,"
                   "agent_policy_sha256=?,agent_policy_json=? WHERE run_id=?",
                   (new["contract_version"], new["contract_sha256"], new["workflow_sha256"],
                    new["agent_policy_sha256"], store._j(policy), run["run_id"]))
        store.bump_revision(tx, run["run_id"])
        store.append_event(tx, kind="run.repinned", actor=ctx.actor, run_id=run["run_id"],
                           subject_kind="run", subject_id=run["run_id"], reason=reason, evidence_level="EXECUTION_OBSERVED",
                           payload={"approval_id": approval["approval_id"], "subject_sha256": subject.sha256,
                                    "old": old, "new": new, "after_repin_event_id": generation})
        current = tx.execute("SELECT * FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
        return current
