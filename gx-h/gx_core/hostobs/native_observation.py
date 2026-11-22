"""Task/lease policy observations from official callback facts, not private logs.

Only ordinary GX task state is persisted. Host thread/turn execution stays with
Codex. A settings gap or changed connection cannot be repaired by a caller's
reported UUID/model, and completed turns never release native ownership.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from types import SimpleNamespace
from typing import Any, Mapping

from ..domain import clock, ids
from ..errors import ContentHashMismatch, IndependenceRequired, LeaseExpired, ModelPolicyViolation, ScopeDenied, StalePlan
from ..persistence import store
from . import native_context as native, policy

CLAIM_EVENT = "host.native_claim_verified"
CLAIM_KEY = "host.native.current_claim"


def snapshot(conn, task_id: str, lease_id: str | None = None):
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise ScopeDenied("the native task does not exist")
    run = conn.execute(
        "SELECT project_ref,status,phase,control_epoch,workflow_sha256,contract_sha256,"
        "agent_policy_sha256,agent_policy_json,host_json FROM runs WHERE run_id=?",
        (task["run_id"],)).fetchone()
    assignment = conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
    lease = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone() if lease_id else None
    teams, member, grant = [], None, None
    if assignment:
        from ..teams.manager import _ancestors
        teams = [{"team_id": row["team_id"], "status": row["status"],
                  "leader_generation": row["leader_generation"]}
                 for row in _ancestors(conn, task["run_id"], assignment["team_id"])]
        member = conn.execute("SELECT status,observed_worker_ref FROM team_members "
            "WHERE team_id=? AND subject=?", (assignment["team_id"], assignment["subject"])).fetchone()
        if lease_id is None and assignment["delegated_by"]:
            # Changes to other scopes or an unrelated task list entry do not
            # invalidate this worker. Revocation of this task's delegation does.
            grant = bool(conn.execute(
                "SELECT 1 FROM team_grants g JOIN teams t ON t.team_id=g.team_id "
                "WHERE t.team_id=? AND t.leader_subject=g.subject AND g.subject=? "
                "AND t.leader_generation=g.generation AND g.generation=? AND t.leader_expires_at>? "
                "AND g.revoked_at IS NULL AND 'assign' IN (SELECT value FROM json_each(g.scopes_json)) "
                "AND ? IN (SELECT value FROM json_each(g.task_ids_json))",
                (assignment["team_id"], assignment["delegated_by"], assignment["offered_leader_generation"],
                 clock.now_iso(), task_id)).fetchone())
    return {"task": dict(task), "run": dict(run), "assignment": dict(assignment) if assignment else None,
            "teams": teams, "member": dict(member) if member else None, "grant": grant,
            "lease": dict(lease) if lease else None}



def ownership_facts(snapshot):
    assignment = snapshot["assignment"] or {}
    return {"control_epoch": snapshot["run"]["control_epoch"],
            "assignment_generation": assignment.get("assignment_generation"),
            "assignment_status": assignment.get("status"),
            "teams": snapshot["teams"], "member": snapshot["member"]}


def lease_ownership_matches(proof, snapshot):
    # The leader's grant admits a new claim. Once issued, the worker owns its
    # lease independently until its task, assignment, team or project fences it.
    # Early V2 receipts also recorded grant admission inside their ownership map.
    issued = proof.get("ownership")
    return issued is None or {key: value for key, value in issued.items()
                              if key != "delegated"} == ownership_facts(snapshot)


def requirement(conn, task):
    # This existing helper only validates backend task/policy/role pins; it performs
    # no Host I/O and supplies no private-reader product.
    from .codex_worker_observation import requirement as pinned_requirement
    return pinned_requirement(conn, task, current=True)


def _check_current(call, required):
    if not policy.is_model(call.model, required["model"]) or call.effort not in required["allowed_efforts"]:
        raise ModelPolicyViolation("the official native configuration is outside this task's pinned policy",
            details={"required_model": required["model"], "allowed_efforts": required["allowed_efforts"],
                     "observed_model": call.model, "observed_effort": call.effort,
                     "model_authority": native.MODEL_AUTHORITY, "upstream_tier_independently_verified": False})


@dataclass(frozen=True)
class Claim:
    call: native.NativeCall
    snapshot_json: str
    scope_json: str
    fenced: bool = False


def prepare_claim(ctx, *, task_id, run_id=None, principal_subject=None):
    call = native.call_for_ctx(ctx)
    if call.tool_name not in ("gx_task_claim", "gx_task_acquire") or (principal_subject is not None and principal_subject != ctx.principal.subject):
        raise ScopeDenied("the native claim callback belongs to another operation/principal")
    if ctx.conn.in_transaction:
        raise RuntimeError("prepare native claims before the writer transaction")
    before = snapshot(ctx.conn, task_id)
    if run_id is not None and run_id != before["task"]["run_id"]:
        raise ScopeDenied("the native claim task belongs to another run")
    if not native.is_native_run(ctx.conn, before["task"]["run_id"]):
        raise ScopeDenied("this task did not pin the official native source")
    assignment = before["assignment"]
    if (not assignment or assignment["status"] != "ASSIGNED"
            or assignment["subject"] != ctx.principal.subject):
        raise ScopeDenied("the official native worker needs its actual settled task assignment")
    if before["grant"] is False:
        raise StalePlan("the leader no longer holds the delegation for this task")
    if call.tool_name == "gx_task_acquire" and (
            assignment["offered_control_epoch"] != before["run"]["control_epoch"]
            or assignment["offered_leader_generation"] != before["teams"][0]["leader_generation"]):
        raise StalePlan("the handoff predates the current project or leader generation")
    _, required, scope = requirement(ctx.conn, before["task"])
    task = before["task"]
    reviews = bool(task.get("independent_of_task_id")) or str(task.get("kind", "")).startswith("review") or task.get("role") in (
        "verifier", "evidence_reviewer", "research_adapter_reviewer")
    if reviews and call.fresh_context is not True:
        raise IndependenceRequired("this native reviewer needs official explicit fresh context; fork/unknown is not independent",
            details={"task_id": task_id, "forked_from_id": call.forked_from_id,
                     "fresh_context": call.fresh_context, "source": native.SOURCE})
    # Native parenthood alone is not context inheritance. This MVP deliberately
    # supports only explicit no-fork reviewers, including for public-root forks.
    # It never infers inherited context from a parent relationship or missing key.
    _check_current(call, required)
    ctx.authz[CLAIM_KEY] = Claim(call, ids.canonical_json(before), ids.canonical_json(scope))
    return call.worker_ref


def fence_claim(ctx, *, task_id=None):
    prepared = ctx.authz.get(CLAIM_KEY)
    if not isinstance(prepared, Claim) or not ctx.conn.in_transaction:
        raise ScopeDenied("the native claim has no prepared transaction admission")
    before = json.loads(prepared.snapshot_json)
    if task_id not in (None, before["task"]["task_id"]) or snapshot(ctx.conn, before["task"]["task_id"]) != before:
        raise StalePlan("the native task/pins/assignment changed before claim commit")
    if native.call_for_ctx(ctx) is not prepared.call:
        raise ScopeDenied("the official native callback changed during claim")
    ctx.authz[CLAIM_KEY] = Claim(prepared.call, prepared.snapshot_json, prepared.scope_json, True)


def worker_for_claim(ctx, *, run_id, task_id, principal_subject):
    prepared = ctx.authz.get(CLAIM_KEY)
    if not ctx.conn.in_transaction:
        return prepare_claim(ctx, run_id=run_id, task_id=task_id, principal_subject=principal_subject)
    if not isinstance(prepared, Claim) or not prepared.fenced:
        return None
    before = json.loads(prepared.snapshot_json)
    if (before["task"]["run_id"] != run_id or before["task"]["task_id"] != task_id
            or principal_subject != ctx.principal.subject or native.call_for_ctx(ctx) is not prepared.call):
        return None
    return prepared.call.worker_ref


def record_claim(ctx, *, lease_id):
    prepared = ctx.authz.get(CLAIM_KEY)
    if not isinstance(prepared, Claim) or not prepared.fenced or not ctx.conn.in_transaction:
        raise ScopeDenied("no fenced official native claim may issue this lease receipt")
    call = native.call_for_ctx(ctx)
    lease = ctx.conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
    before = json.loads(prepared.snapshot_json)
    if not lease or lease["principal"] != call.principal.subject or lease["task_id"] != before["task"]["task_id"]:
        raise ScopeDenied("the issued lease differs from the native claim")
    proof = {"source": native.SOURCE, "lease_id": lease_id, "task_id": lease["task_id"],
             "run_id": before["task"]["run_id"], "attempt": lease["attempt"],
             "principal_subject": lease["principal"], "observed_worker_ref": call.worker_ref,
             "native_call": call.as_evidence(), "scope": json.loads(prepared.scope_json),
             "ownership": ownership_facts(before)}
    proof["evidence_sha256"] = ids.sha256_of_obj(proof)
    event = store.append_event(ctx.conn, kind=CLAIM_EVENT, actor=ctx.actor, run_id=proof["run_id"],
        subject_kind="task", subject_id=lease["task_id"], evidence_level="EXECUTION_OBSERVED",
        reason="official native callback bound this task lease", payload=proof)
    if call.tool_name == "gx_task_acquire":
        # The admitted worker is executing its first real callback. Record the
        # start in the existing observation machine as part of the lease issue.
        _write(ctx.conn, lease["task_id"], lease_id, call,
               {"policy_verdict": policy.CONFORMS, "reasons": []},
               {"claim_event_id": event, "continuous": True, "turns": 1,
                "model": call.model, "effort": call.effort, "readable": True},
               finished=False)
    return event


def claim_receipt(conn, task_id, lease_id):
    matches = []
    for row in conn.execute("SELECT event_id,payload_json FROM events WHERE kind=? AND subject_id=? ORDER BY event_id", (CLAIM_EVENT, task_id)):
        proof = json.loads(row["payload_json"])
        if proof.get("lease_id") == lease_id:
            digest = proof.pop("evidence_sha256", None)
            if digest != ids.sha256_of_obj(proof):
                raise ContentHashMismatch("the native claim receipt hash changed")
            matches.append((proof, row["event_id"]))
    if len(matches) != 1:
        raise ScopeDenied("this task lease has no unique official native claim receipt")
    return matches[0]


def previously_observed_fork(conn, project, session, thread, parent, version):
    """Only the immutable fork source survives a reconnect; current turn/config come from the Host."""
    row = conn.execute(
        "SELECT e.subject_id,json_extract(e.payload_json,'$.lease_id') FROM events e "
        "JOIN runs r ON r.run_id=e.run_id WHERE e.kind=? AND r.project_ref=? "
        "AND json_extract(e.payload_json,'$.native_call.session_id')=? "
        "AND json_extract(e.payload_json,'$.native_call.thread_id')=? "
        "AND json_type(e.payload_json,'$.native_call.fresh_context') IN ('true','false') "
        "ORDER BY e.rowid DESC LIMIT 1", (CLAIM_EVENT, project, session, thread)).fetchone()
    if row is None:
        return {}
    proof, _ = claim_receipt(conn, row[0], row[1])
    call = proof["native_call"]
    if (proof["source"] != native.SOURCE or call["root_thread_id"] != session or call["host_version"] != version
            or call["parent_thread_id"] != parent):
        raise ScopeDenied("current worker identity differs from its original native claim")
    return {"forkedFromId": call["forked_from_id"]}


@dataclass(frozen=True)
class Prepared:
    call: native.NativeCall
    json: str


@dataclass(frozen=True)
class Fenced:
    call: native.NativeCall
    json: str


def prepare(ctx, *, lease_id, operation, task_id=None, _entry=None):
    from ..teams.service import _LeaseEntry, FRESH_AUTHORIZED
    if (not isinstance(_entry, _LeaseEntry) or _entry.disposition != FRESH_AUTHORIZED
            or _entry.principal_subject != ctx.principal.subject or _entry.request_id != ctx.request_id
            or _entry.lease_id != lease_id or _entry.task_id != task_id or _entry.tool != "gx_task_" + operation):
        raise ScopeDenied("native observation requires this request's actual lease admission")
    call = native.call_for_ctx(ctx)
    if call.tool_name != "gx_task_" + operation:
        raise ScopeDenied("the native callback does not match the lease operation")
    before = snapshot(ctx.conn, task_id, lease_id)
    lease = before["lease"]
    if (not lease or lease["released_at"] is not None or lease["principal"] != ctx.principal.subject
            or before["task"]["lease_id"] != lease_id):
        raise LeaseExpired("the actual native caller does not own this current lease")
    proof, event_id = claim_receipt(ctx.conn, task_id, lease_id)
    if not lease_ownership_matches(proof, before):
        raise StalePlan("the assignment or project control changed after this lease was issued")
    if (proof.get("principal_subject") != lease["principal"] or proof.get("attempt") != lease["attempt"]
            or proof.get("observed_worker_ref") != call.worker_ref):
        raise ScopeDenied("the native callback differs from this lease's original worker")
    document, required, scope = requirement(ctx.conn, before["task"])
    if scope != proof.get("scope"):
        raise ContentHashMismatch("the native lease policy/role pins changed after claim")
    origin = proof["native_call"]
    continuous = origin.get("adapter_instance_id") == call.adapter_instance_id
    epochs = [{"adapter_instance_id": origin["adapter_instance_id"], "event_seq": origin["event_seq"],
               "turn_id": origin["turn_id"], "model": origin["model"], "effort": origin["effort"], "kind": "claim"}]
    # Sequence numbers belong to one connection. A replacement's lower numbers
    # must not hide its actual epochs behind the original claim's sequence.
    epochs += [dict(item, adapter_instance_id=call.adapter_instance_id)
               for item in call._issuer.epochs[call.thread_id]
               if item["event_seq"] <= call.event_seq and (not continuous or origin["event_seq"] < item["event_seq"])]
    epochs.append({"adapter_instance_id": call.adapter_instance_id, "event_seq": call.event_seq,
                   "turn_id": call.turn_id, "model": call.model, "effort": call.effort, "kind": "callback"})
    body = {"snapshot": before, "operation": operation, "claim": proof, "claim_event_id": event_id,
            "required": required, "scope": scope, "document": document,
            "continuous": continuous, "epochs": epochs}
    return Prepared(call, ids.canonical_json(body))


def fence(conn, prepared):
    if prepared is None:
        return None
    if not isinstance(prepared, Prepared) or not conn.in_transaction:
        raise ScopeDenied("official native observation fencing requires a writer transaction")
    body = json.loads(prepared.json)
    old = body["snapshot"]
    if snapshot(conn, old["task"]["task_id"], old["lease"]["lease_id"]) != old or not prepared.call._issuer.valid(prepared.call):
        raise StalePlan("native task/lease/pins/callback moved before commit")
    return Fenced(prepared.call, prepared.json)


def _recorded(conn, task_id, lease_id, call, body):
    """Retain actually judged facts of this original claim, never missing history."""
    epochs, adverse = list(body["epochs"]), []
    continuous = body["continuous"]
    for row in conn.execute(
            "SELECT * FROM worker_observations WHERE task_id=? AND lease_id=? AND host='codex'"
            " AND observation_source=? AND evidence_level='EXECUTION_OBSERVED'"
            " AND observed_worker_ref=? AND host_session_ref=? AND agent_ref=? ORDER BY rowid",
            (task_id, lease_id, native.OBSERVATION_SOURCE, call.worker_ref, call.session_id, call.thread_id)):
        payload = json.loads(row["payload_json"])
        observed = payload.get("observed") or {}
        if observed.get("claim_event_id") not in (None, body["claim_event_id"]):
            raise ContentHashMismatch("a native lease observation names another original claim")
        # Pre-correction protocol rows only persisted epochs from the original
        # adapter (even when a connection gap made that observation unreadable).
        for epoch in observed.get("epochs", []):
            epochs.append(dict(epoch, adapter_instance_id=epoch.get("adapter_instance_id")
                               or body["claim"]["native_call"]["adapter_instance_id"]))
        if observed:
            continuous = continuous and observed.get("continuous", observed.get("readable") is True)
        if row["policy_verdict"] in (policy.MISMATCH, policy.REFUSAL_OR_FALLBACK):
            adverse.append({"worker_observation_id": row["worker_observation_id"],
                            "policy_verdict": row["policy_verdict"]})
    # A repeated bind or another connection must not multiply the same facts.
    unique = {ids.canonical_json(epoch): epoch for epoch in epochs}
    return list(unique.values()), adverse, continuous


def _summary(required, epochs, continuous):
    bad_model = {e["turn_id"] for e in epochs if not policy.is_model(e.get("model"), required["model"])}
    bad_effort = {e["turn_id"] for e in epochs if e.get("effort") and e["effort"] not in required["allowed_efforts"]}
    unknown = {e["turn_id"] for e in epochs if not e.get("model") or not e.get("effort")}
    deviations = [{"kind": "fallback", "turn_id": e["turn_id"], "event_seq": e["event_seq"],
                   "adapter_instance_id": e["adapter_instance_id"]} for e in epochs if e["kind"] == "reroute"]
    return {"readable": True, "reason": None if continuous else "the official connection changed; the lease interval has an unobserved gap",
        "turns": len({e["turn_id"] for e in epochs}), "turns_off_model": len(bad_model),
        "turns_below_effort": len(bad_effort), "turns_effort_unknown": len(unknown), "deviations": deviations,
        "models": {m: sum(e.get("model") == m for e in epochs) for m in {e.get("model") for e in epochs} if m},
        "efforts": {m: sum(e.get("effort") == m for e in epochs) for m in {e.get("effort") for e in epochs} if m}}


def record_adverse_epoch(conn, authority, thread_id, *, sequence):
    """Carry one selected official policy failure before the connection stops.

    This changes no task, lease or attempt. The existing business owner handles
    any hold; unobserved history and unknown metadata are never reconstructed.
    """
    from ..teams import recovery
    from . import observation as obs
    epochs = authority.epochs.get(thread_id, []) if isinstance(authority, native.NativeAuthority) else []
    if (not isinstance(authority, native.NativeAuthority) or not authority.live
            or not epochs or epochs[-1]["event_seq"] != sequence
            or epochs[-1]["kind"] not in ("settings", "reroute")
            or epochs[-1]["turn_id"] != authority.active_turns.get(thread_id)):
        raise ScopeDenied("the policy failure has no current official settings epoch")
    epoch = dict(epochs[-1], adapter_instance_id=authority.instance_id)
    principal = authority.principal_for(thread_id)
    worker_ref = f"codex:{authority.session_id}:{thread_id}"
    with store.transaction(conn):
        leases = conn.execute("SELECT l.* FROM leases l JOIN tasks t ON t.lease_id=l.lease_id"
            " JOIN runs r ON r.run_id=t.run_id WHERE l.principal=? AND l.released_at IS NULL"
            " AND t.status IN ('CLAIMED','RUNNING') AND r.project_ref=?",
            (principal.subject, authority.project_ref)).fetchall()
        for lease in leases:
            before = snapshot(conn, lease["task_id"], lease["lease_id"])
            task, assignment = before["task"], before["assignment"]
            if not native.is_native_run(conn, task["run_id"]):
                continue
            effective = recovery.live_lease(conn, task["task_id"])
            if not effective or effective["lease_id"] != lease["lease_id"]:
                continue
            proof, claim_event_id = claim_receipt(conn, task["task_id"], lease["lease_id"])
            origin = proof["native_call"]
            # Pending handoff preserves this original holder's current lease.
            if (not assignment or assignment["status"] not in ("ASSIGNED", "PENDING_TRANSFER")
                    or assignment["subject"] != principal.subject
                    or proof.get("task_id") != task["task_id"] or proof.get("run_id") != task["run_id"]
                    or proof.get("attempt") != lease["attempt"] or task["attempt"] != lease["attempt"]
                    or proof.get("principal_subject") != principal.subject or proof.get("observed_worker_ref") != worker_ref
                    or origin.get("root_thread_id") != authority.root_thread_id or origin.get("session_id") != authority.session_id
                    or origin.get("thread_id") != thread_id or origin.get("host_version") != authority.host_version
                    or origin.get("adapter_instance_id") == authority.instance_id and sequence <= origin["event_seq"]):
                continue
            document, required, scope = requirement(conn, task)
            if scope != proof.get("scope"):
                raise ContentHashMismatch("the native failure's lease policy/role pins changed")
            # Only explicit adverse facts are durable failures. Missing metadata
            # stays unknown and never becomes an invented wrong-model finding.
            if (epoch["kind"] != "reroute"
                    and not (epoch.get("model") and not policy.is_model(epoch["model"], required["model"]))
                    and not (epoch.get("effort") and epoch["effort"] not in required["allowed_efforts"])):
                continue
            previous_rows = conn.execute("SELECT payload_json FROM worker_observations WHERE task_id=? AND lease_id=?"
                " AND observation_source=? AND observed_worker_ref=?",
                (task["task_id"], lease["lease_id"], native.OBSERVATION_SOURCE, worker_ref))
            if any(epoch in (json.loads(row["payload_json"]).get("observed") or {}).get("epochs", [])
                   for row in previous_rows):
                continue
            continuous = origin.get("adapter_instance_id") == authority.instance_id
            summary = _summary(required, [epoch], continuous)
            verdict = policy.judge(host="codex", required=required, observed=summary, policy=document)
            evidence = {"source": native.OBSERVATION_SOURCE, "model_authority": native.MODEL_AUTHORITY,
                "upstream_tier_independently_verified": False, "readable": True, "reason": summary["reason"],
                "continuous": continuous, "coverage": "selected_adverse_official_epoch",
                "adapter_instance_id": authority.instance_id, "model": epoch.get("model"), "effort": epoch.get("effort"),
                "turns": 1, "epochs": [epoch], "observed_worker_ref": worker_ref, "claim_event_id": claim_event_id}
            previous = obs.latest_state(conn, task_id=task["task_id"], lease_id=lease["lease_id"], agent_ref=thread_id)
            obs.write_observation(conn, task_id=task["task_id"], lease_id=lease["lease_id"], host="codex",
                state=previous or obs.STATE_OBSERVED_STARTED, source=native.OBSERVATION_SOURCE,
                evidence_level="EXECUTION_OBSERVED", host_session_ref=authority.session_id, agent_ref=thread_id,
                observed_ref=worker_ref, observed_model=epoch.get("model"), observed_effort=epoch.get("effort"),
                turns_observed=1, turns_off_policy=1, host_version=authority.host_version,
                policy_verdict=verdict["policy_verdict"], payload={"observed": evidence, "reasons": verdict["reasons"]})


def _judge(conn, task_id, lease_id, prepared, reported=None, declared=None):
    if not isinstance(prepared, Fenced) or not conn.in_transaction or not prepared.call._issuer.valid(prepared.call):
        raise ScopeDenied("no transaction-fenced official callback covers this lease")
    body = json.loads(prepared.json)
    old, call = body["snapshot"], prepared.call
    now = snapshot(conn, task_id, lease_id)
    keys = ("task_id", "run_id", "attempt", "lease_id", "role", "task_class", "required_model", "required_effort_json")
    if (old["task"]["task_id"] != task_id or old["lease"]["lease_id"] != lease_id
            or any(old["task"].get(k) != now["task"].get(k) for k in keys)
            or old["run"] != now["run"] or old["assignment"] != now["assignment"] or old["lease"] != now["lease"]):
        raise StalePlan("the native task/lease/assignment changed after fencing")
    required = body["required"]
    epochs, adverse, continuous = _recorded(conn, task_id, lease_id, call, body)
    summary = _summary(required, epochs, continuous)
    verdict = policy.judge(host="codex", required=required, reported=reported, observed=summary, policy=body["document"])
    prior_fallback = any(item["policy_verdict"] == policy.REFUSAL_OR_FALLBACK for item in adverse)
    if adverse:
        # A good current configuration cannot erase a durable adverse judgment,
        # including one whose reason was an identity contradiction rather than
        # an epoch. Existing user holds stay with their original business owner.
        verdict["policy_verdict"] = (policy.REFUSAL_OR_FALLBACK if prior_fallback
            or verdict["policy_verdict"] == policy.REFUSAL_OR_FALLBACK else policy.MISMATCH)
        verdict["reasons"].append("earlier official observations of this lease retain an adverse policy judgment")
    mismatches = [k for k, actual in (("host", "codex"), ("session_ref", call.session_id), ("agent_ref", call.thread_id))
                  if (declared or {}).get(k) not in (None, actual)]
    if mismatches:
        verdict.update(policy_verdict=policy.MISMATCH, reasons=[*verdict["reasons"], "reported identity contradicts the official callback: " + ",".join(mismatches)])
    if verdict["policy_verdict"] != policy.CONFORMS:
        verdict.update(bind_action="refuse", submit_action="hold", waits_for_user=True, failure_class=policy.FAILURE_POLICY)
    evidence = {"source": native.OBSERVATION_SOURCE, "model_authority": native.MODEL_AUTHORITY,
        "upstream_tier_independently_verified": False, "readable": summary["readable"], "reason": summary["reason"],
        "continuous": continuous, "coverage": "current_callback_and_recorded_epochs",
        "adapter_instance_id": call.adapter_instance_id, "prior_adverse_observations": adverse,
        "model": call.model, "effort": call.effort, "turns": summary["turns"], "epochs": epochs,
        "observed_worker_ref": call.worker_ref, "claim_event_id": body["claim_event_id"],
        "host_version": call.host_version, "protocol_evidence_sha256": call.evidence_sha256}
    return call, body, summary, verdict, evidence


def _write(conn, task_id, lease_id, call, verdict, evidence, *, finished):
    from . import observation as obs
    previous = obs.latest_state(conn, task_id=task_id, lease_id=lease_id, agent_ref=call.thread_id)
    target = obs.STATE_OBSERVED_FINISHED if finished else obs.STATE_OBSERVED_STARTED
    if not obs.can_follow(previous, target):
        raise ScopeDenied("the worker observation cannot advance from its settled state")
    return obs.write_observation(conn, task_id=task_id, lease_id=lease_id, host="codex", state=target,
        source=native.OBSERVATION_SOURCE, evidence_level="EXECUTION_OBSERVED", host_session_ref=call.session_id,
        agent_ref=call.thread_id, observed_ref=call.worker_ref, observed_model=call.model, observed_effort=call.effort,
        turns_observed=evidence["turns"], turns_off_policy=0 if verdict["policy_verdict"] == policy.CONFORMS else evidence["turns"],
        host_version=call.host_version, policy_verdict=verdict["policy_verdict"],
        payload={"observed": evidence, "reasons": verdict["reasons"]})


def _answer(body, verdict, evidence, observation_id, *, operation, reported):
    required = body["required"]
    return {"worker_observation_id": observation_id, "policy_verdict": verdict["policy_verdict"],
        "action": verdict["bind_action" if operation == "bind" else "submit_action"],
        "waits_for_user": verdict["waits_for_user"], "failure_class": verdict["failure_class"],
        "reasons": verdict["reasons"], "basis": verdict["basis"], "host": "codex", "mode": verdict["mode"],
        "required": {"model": required["model"], "allowed_efforts": required["allowed_efforts"], "task_class": required["task_class"]},
        "reported": dict(reported or {}), "report_disagrees": verdict["report_disagrees"],
        "observed": evidence, "deviation": None, "policy_sha256": body["scope"]["policy_sha256"]}


def bind_worker(conn, *, task_id, lease_id, host, host_session_ref=None, agent_ref=None,
                model=None, effort=None, evidence_level="CLIENT_REPORTED", _codex_prepared=None, **kwargs):
    from . import observation as obs
    if evidence_level not in obs.CLIENT_LEVELS:
        raise ScopeDenied("the worker may only report its requested evidence level")
    reported = {"model": model, "effort": effort}
    call, body, _, verdict, evidence = _judge(conn, task_id, lease_id, _codex_prepared, reported,
        {"host": host, "session_ref": host_session_ref, "agent_ref": agent_ref})
    observation_id = _write(conn, task_id, lease_id, call, verdict, evidence, finished=False)
    obs.write_observation(conn, task_id=task_id, lease_id=lease_id, host="codex", state=obs.STATE_REPORTED_RUNNING,
        source=obs.SOURCE_REPORT, evidence_level=evidence_level, host_session_ref=call.session_id,
        agent_ref=call.thread_id, reported_model=model, reported_effort=effort, policy_verdict=verdict["policy_verdict"],
        payload={"official_observation_id": observation_id})
    answer = _answer(body, verdict, evidence, observation_id, operation="bind", reported=reported)
    answer["identity"] = {"tied_by_host_hook": False, "host_session_ref": call.session_id, "agent_ref": call.thread_id,
                          "report_differs": any((v is not None and v != actual) for v, actual in ((host_session_ref, call.session_id), (agent_ref, call.thread_id)))}
    return answer


def verdict_for(conn, *, task_id, lease_id, _codex_prepared=None, inspect=False):
    if inspect and _codex_prepared is None:
        return {"policy_verdict": policy.UNOBSERVABLE, "worker_observation_id": None,
                "waits_for_user": True, "failure_class": policy.FAILURE_POLICY, "refusal": False,
                "readable": False, "tied": False, "deviation": None,
                "reasons": ["no active official callback covers this inspection; no private reader is used"]}
    call, body, _, verdict, evidence = _judge(conn, task_id, lease_id, _codex_prepared)
    observation_id = _write(conn, task_id, lease_id, call, verdict, evidence, finished=True)
    if inspect:
        return {"policy_verdict": verdict["policy_verdict"], "worker_observation_id": observation_id,
            "waits_for_user": verdict["waits_for_user"], "failure_class": verdict["failure_class"],
            "refusal": verdict["policy_verdict"] == policy.REFUSAL_OR_FALLBACK,
            "readable": evidence["readable"], "tied": True, "deviation": None, "reasons": verdict["reasons"]}
    answer = _answer(body, verdict, evidence, observation_id, operation="submit", reported=None)
    answer["bound"] = conn.execute("SELECT 1 FROM worker_observations WHERE lease_id=? AND observation_source='client_report'", (lease_id,)).fetchone() is not None
    return answer


def identity_of(conn, *, task_id, lease_id=None):
    if lease_id is None:
        row = conn.execute("SELECT lease_id FROM leases WHERE task_id=? ORDER BY issued_at DESC,rowid DESC LIMIT 1", (task_id,)).fetchone()
        lease_id = row[0] if row else None
    lease = conn.execute("SELECT * FROM leases WHERE lease_id=? AND task_id=?", (lease_id, task_id)).fetchone() if lease_id else None
    out = {"task_id": task_id, "lease_id": lease_id, "principal": lease["principal"] if lease else None,
           "host": None, "host_session_ref": None, "agent_ref": None, "observed_worker_ref": None,
           "source": None, "subjects_of_agent": [], "agents_of_subject": [], "one_worker_one_name": None}
    if not lease:
        return out
    proof, _ = claim_receipt(conn, task_id, lease_id)
    call = proof["native_call"]
    out.update(host="codex", host_session_ref=call["session_id"], agent_ref=call["thread_id"],
               observed_worker_ref=proof["observed_worker_ref"], source=native.OBSERVATION_SOURCE,
               native_source=native.SOURCE, fresh_context=call.get("fresh_context"),
               forked_from_id=call.get("forked_from_id"),
               subjects_of_agent=[proof["principal_subject"]], agents_of_subject=[proof["observed_worker_ref"]], one_worker_one_name=True)
    return out
