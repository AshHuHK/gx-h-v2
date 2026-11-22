"""Codex Host-applied policy evidence, prepared outside SQLite write transactions.

This proves the configuration the local Host applied. It does not independently
attest the upstream provider's actual model tier. Requests supply no observation.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping

from ..domain import clock, ids
from ..persistence import store
from ..errors import ContentHashMismatch, IdempotencyConflict, LeaseExpired, ModelPolicyViolation, OutcomeUnknown, ScopeDenied, StalePlan
from . import codex, policy
from .record import Record, Turn, summarise

AUTHORITY = "codex_host_applied_metadata"


def scoped_run(conn: sqlite3.Connection, run_id: str) -> bool:
    from . import host_policy
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return run is not None and host_policy.scope_of_run(run) == "codex"


def requirement(conn: sqlite3.Connection, task: Mapping[str, Any], *, current: bool = False):
    """Read one backend-pinned Host policy; never select GPT from a declaration."""
    from . import codex_roles, host_policy, observation
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (task["run_id"],)).fetchone()
    if run is None or host_policy.scope_of_run(run) != "codex":
        raise ModelPolicyViolation("this run has no independently pinned Codex policy")
    document, digest = observation.policy_of_run(conn, task["run_id"])
    if current:
        installed, installed_sha = host_policy.current_for_run(run)
        if installed_sha != digest or ids.canonical_json(installed) != ids.canonical_json(document):
            raise ContentHashMismatch("the Codex run policy changed; no automatic re-pin is allowed")
    required = policy.requirement_of_task(dict(task), document)
    authorized = {"model": policy.class_model(required["task_class"], document),
                  "allowed_efforts": policy.allowed_efforts(required["task_class"], document)}
    try:
        stored_efforts = json.loads(task["required_effort_json"] or "[]")
    except (ValueError, TypeError) as exc:
        raise ContentHashMismatch("the task's stored Codex effort requirement is unreadable") from exc
    if (not policy.is_model(required["model"], authorized["model"])
            or not isinstance(stored_efforts, list)
            or any(level not in authorized["allowed_efforts"] for level in stored_efforts)
            or not required["allowed_efforts"]
            or any(level not in authorized["allowed_efforts"] for level in required["allowed_efforts"])):
        raise ContentHashMismatch("the task's stored model/effort is outside its pinned Codex role policy")
    selected = json.loads(run["host_json"])["policy_selection"]
    role_sha = selected.get("role_intent_pins", {}).get("role." + task["role"])
    if not isinstance(role_sha, str) or len(role_sha) != 64:
        raise ContentHashMismatch("the task's run has no pinned Codex role intent")
    scope = {"policy_sha256": digest, "required_model": required["model"],
             "default_effort": required["default_effort"], "allowed_efforts": required["allowed_efforts"],
             "role_intent_version": codex_roles.VERSION, "role_intent_sha256": role_sha,
             "required_sha256": ids.sha256_of_obj(required)}
    return document, required, scope


def checked_packet(packet: codex.NativeObservation, required: Mapping[str, Any], document: Mapping[str, Any]):
    turn = getattr(packet, "current_turn", None)
    valid = (isinstance(packet, codex.NativeObservation) and packet.readable
             and getattr(packet, "current_turn_readable", False) and isinstance(turn, dict)
             and turn.get("source") == "host_applied_turn_context" and turn.get("status") == "active"
             and turn.get("model_authority") == AUTHORITY
             and turn.get("upstream_tier_independently_verified") is False)
    if (not valid or not turn.get("turn_id") or not turn.get("evidence_sha256")
            or type(turn.get("context_line")) is not int or type(turn.get("settings_line")) is not int
            or not policy.is_model(turn.get("model"), required["model"])
            or turn.get("effort") not in required["allowed_efforts"]
            or turn.get("settings_model") != turn.get("model")
            or turn.get("settings_effort") != turn.get("effort")):
        raise ModelPolicyViolation("the current native Host-applied model/effort is absent or outside this task's policy",
                                   details={"required_model": required["model"],
                                            "allowed_efforts": list(required["allowed_efforts"]),
                                            "observed_model": (turn or {}).get("model"),
                                            "observed_effort": (turn or {}).get("effort"),
                                            "model_authority": AUTHORITY,
                                            "upstream_tier_independently_verified": False})
    return dict(turn)


def snapshot(conn: sqlite3.Connection, task_id: str, lease_id: str | None = None) -> dict[str, Any]:
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    assignment = conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
    run = conn.execute("SELECT agent_policy_json,agent_policy_sha256,host_json,contract_sha256 FROM runs"
                       " WHERE run_id=?", (task["run_id"],)).fetchone()
    lease = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone() if lease_id else None
    return {"task": dict(task), "assignment": dict(assignment) if assignment else None,
            "run": dict(run), "lease": dict(lease) if lease else None,
            "event_cursor": conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE run_id=?",
                                          (task["run_id"],)).fetchone()[0]}


@dataclass(frozen=True)
class Prepared:
    """A bounded immutable reader product, not a request payload."""
    json: str


@dataclass(frozen=True)
class Fenced:
    """Internal product issued at the beginning of the consuming transaction."""
    json: str


def fence(conn: sqlite3.Connection, prepared: Prepared | None) -> Fenced | None:
    from . import native_observation
    if isinstance(prepared, native_observation.Prepared):
        return native_observation.fence(conn, prepared)
    if prepared is None:
        return None
    if not conn.in_transaction or not isinstance(prepared, Prepared):
        raise RuntimeError("Codex observation fencing belongs at transaction entry")
    body = json.loads(prepared.json)
    expected = body["snapshot"]
    actual = snapshot(conn, expected["task"]["task_id"], (expected["lease"] or {}).get("lease_id"))
    if actual != expected:
        raise StalePlan("task, lease, assignment, policy, role intent or event cursor moved during native observation")
    return Fenced(prepared.json)


def _receipt(conn: sqlite3.Connection, task: Mapping[str, Any], lease: Mapping[str, Any]):
    from .codex_dispatch import CLAIM_EVENT
    found = []
    for row in conn.execute("SELECT payload_json,event_id FROM events WHERE kind=? AND subject_id=? ORDER BY event_id",
                             (CLAIM_EVENT, task["task_id"])):
        value = json.loads(row["payload_json"])
        if value.get("lease_id") == lease["lease_id"]:
            found.append((value, row["event_id"]))
    if len(found) != 1:
        raise ModelPolicyViolation("the Codex lease has no unique backend-verified claim receipt")
    proof, event_id = found[0]
    digest = proof.pop("evidence_sha256", None)
    if (digest != ids.sha256_of_obj(proof) or proof.get("principal_subject") != lease["principal"]
            or proof.get("attempt") != int(lease["attempt"]) or proof.get("run_id") != task["run_id"]
            or proof.get("task_id") != task["task_id"] or not isinstance(proof.get("applied_packet"), dict)):
        raise ModelPolicyViolation("the Codex claim receipt does not bind this lease and applied policy packet")
    return proof, event_id


def _record_dict(record: Record) -> dict[str, Any]:
    return {"host": record.host, "sha256": record.sha256, "size": record.size,
            "session_ref": record.session_ref, "agent_ref": record.agent_ref,
            "readable": record.readable, "reason": record.reason, "versions": record.versions,
            "meta": record.meta, "turns": [dict(at=t.at, model=t.model, effort=t.effort,
                                                 message_id=t.message_id) for t in record.turns]}


def _record(value: Mapping[str, Any]) -> Record:
    answer = Record(host=value["host"], path="", sha256=value["sha256"], size=value["size"],
                    session_ref=value["session_ref"], agent_ref=value["agent_ref"],
                    readable=value["readable"], reason=value["reason"], versions=value["versions"], meta=value["meta"])
    answer.turns = [Turn(**turn) for turn in value["turns"]]
    return answer


def _lease_record(record: Record, proof: Mapping[str, Any], until: str | None) -> Record:
    start = proof["applied_packet"]["context_line"]
    packets = record.meta.get("turn_packets")
    errors = record.meta.get("turn_metadata_errors")
    if not isinstance(packets, list) or not isinstance(errors, list):
        record.readable, record.reason = False, "the native reader supplied no complete applied-context audit"
        record.turns = []
        return record
    selected = [item for item in packets if type(item.get("context_line")) is int and item["context_line"] >= start
                and (until is None or clock.parse(item["context_at"]) <= clock.parse(until))]
    bad = [item for item in errors if (item.get("context_line") is None or item["context_line"] >= start)
           and (until is None or not item.get("context_at")
                or clock.parse(item["context_at"]) <= clock.parse(until))]
    if (bad or not selected or len(selected) > 4096 or selected[0]["context_line"] != start
            or selected[0]["turn_id"] != proof["applied_packet"]["turn_id"]):
        record.readable, record.reason = False, "a lease context is absent, unknown or contradictory in native applied metadata"
        record.turns = []
        return record
    record.turns = [Turn(at=item["context_at"], model=item["model"], effort=item["effort"],
                         message_id=item["turn_id"]) for item in selected]
    record.meta.update(model_authority=AUTHORITY, upstream_tier_independently_verified=False,
                       claim_context_line=start, lease_contexts=len(selected))
    return record


def preflight(ctx: Any, *, lease_id: str, operation: str, task_id: str | None = None):
    """Authorize a secondary lease from store rows before settlement or any Host I/O."""
    lease = ctx.conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
    if lease is None:
        return None
    task = ctx.conn.execute("SELECT * FROM tasks WHERE task_id=?", (lease["task_id"],)).fetchone()
    if task is None or not scoped_run(ctx.conn, task["run_id"]):
        return None
    # The public call can carry a primary task in one project and a secondary
    # lease in another. Authorize their relation before opening any Host record.
    if task_id is not None and lease["task_id"] != task_id:
        raise LeaseExpired("the requested lease belongs to another task")
    run = ctx.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (task["run_id"],)).fetchone()
    if run is None or not ctx.principal.may_access_project(run["project_ref"]):
        raise ScopeDenied("the requested lease is outside the caller's project scope")
    if operation in ("bind", "submit"):
        if lease["principal"] != ctx.principal.subject:
            raise LeaseExpired("the requested lease belongs to another principal")
        if lease["released_at"] is not None or task["lease_id"] != lease_id:
            raise LeaseExpired("the requested lease no longer covers its task")
        # Preserve the platform's job-held effective lease, rather than replacing
        # it with a naked TTL check. This helper only reads the existing rules.
        from ..teams import recovery
        effective = recovery.live_lease(ctx.conn, task["task_id"])
        if effective is None or effective["lease_id"] != lease_id:
            raise LeaseExpired("the requested lease has expired without an effective job hold")
    return task, lease


def prepare(ctx: Any, *, lease_id: str, operation: str, task_id: str | None = None,
            config_root=None, _entry=None) -> Prepared | None:
    """Consume parent admission before reading new Codex Host evidence."""
    if operation in ("bind", "submit"):
        from ..teams.service import _LeaseEntry, FRESH_AUTHORIZED
        if (not isinstance(_entry, _LeaseEntry) or _entry.disposition != FRESH_AUTHORIZED
                or _entry.principal_subject != ctx.principal.subject or _entry.request_id != ctx.request_id
                or _entry.lease_id != lease_id or _entry.task_id != task_id
                or _entry.tool != "gx_task_" + operation):
            raise ScopeDenied("native work observation requires this request's immutable parent admission")
        if _entry.policy_family != "codex":
            return None
    from .native_context import is_native_run
    native_task = ctx.conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone() if task_id else None
    if native_task and is_native_run(ctx.conn, native_task["run_id"]):
        from . import native_observation
        return native_observation.prepare(ctx, lease_id=lease_id, operation=operation, task_id=task_id, _entry=_entry)
    rows = preflight(ctx, lease_id=lease_id, operation=operation, task_id=task_id)
    if rows is None:
        return None
    if ctx.conn.in_transaction:
        raise RuntimeError("prepare Codex Host observations before opening a write transaction")
    task, lease = rows
    doc, required, scope = requirement(ctx.conn, task)
    before = snapshot(ctx.conn, task["task_id"], lease_id)
    reason, proof, event_id = None, None, None
    record = Record(host="codex", path="", sha256="", size=0, readable=False)
    try:
        proof, event_id = _receipt(ctx.conn, task, lease)
        if any(proof.get(key) != value for key, value in scope.items()):
            raise ModelPolicyViolation("the task policy or role intent differs from its immutable claim receipt")
        record = codex.read_worker(proof["session_ref"], proof["native_agent_ref"], config_root=config_root)
        from . import codex_dispatch
        # The immutable claim prefix must still be the prefix of this actual Host
        # record. Neither a changed file nor a copied good current turn can replace it.
        for _ in codex_dispatch._own_rows(Path(record.path), proof["native_agent_ref"], prefix=proof["applied_packet"]):
            pass
        if operation in ("bind", "submit"):
            from . import codex_dispatch
            actual = codex_dispatch.current_invocation()
            packet = codex.observe_native(host="codex", session_ref=proof["session_ref"],
                                          native_agent_ref=proof["native_agent_ref"], config_root=config_root)
            pending = codex_dispatch.verified_pending_invocation(actual, packet, config_root=config_root)
            if (actual is None or actual.native_agent_ref != proof["native_agent_ref"]
                    or lease["principal"] != ctx.principal.subject or pending is None
                    or not codex_dispatch.lease_command(actual.command, subject=lease["principal"],
                        task_id=task["task_id"], lease_id=lease_id, operation=operation)):
                raise ModelPolicyViolation("this call is not the current literal native invocation of this lease holder")
            checked_packet(packet, required, doc)
            # These two bounded reader products must describe the same own context.
            # A context change between reads needs a fresh request, not a chosen good value.
            previous = record.meta.get("current_turn") or {}
            if previous.get("evidence_sha256") != packet.current_turn["evidence_sha256"]:
                raise ModelPolicyViolation("the native applied context changed during lease observation")
        record = _lease_record(record, proof, lease["released_at"])
    except (OSError, ValueError, KeyError, TypeError, ModelPolicyViolation) as exc:
        reason = str(exc)
        record.readable, record.reason, record.turns = False, reason, []
    if snapshot(ctx.conn, task["task_id"], lease_id) != before:
        raise StalePlan("the task moved while its native metadata was being read")
    body = {"snapshot": before, "operation": operation, "required": required, "policy_sha256": scope["policy_sha256"],
            "record": _record_dict(record), "claim": proof, "claim_event_id": event_id, "reason": reason}
    return Prepared(ids.canonical_json(body))


def prepare_run(ctx: Any, run_id: str) -> Prepared | None:
    from .native_context import is_native_run
    if is_native_run(ctx.conn, run_id):
        # A completed turn is not a destroyed worker. Ordinary lease expiry stays
        # with GX; there is no private-record poll or synthetic native stop here.
        return None
    if not scoped_run(ctx.conn, run_id):
        return None
    if ctx.conn.in_transaction:
        raise RuntimeError("prepare Codex run observations outside the write transaction")
    before = ctx.conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
    observations = {}
    for row in ctx.conn.execute("SELECT l.lease_id FROM leases l JOIN tasks t ON t.lease_id=l.lease_id"
                                 " WHERE t.run_id=? AND l.released_at IS NULL", (run_id,)).fetchall():
        value = prepare(ctx, lease_id=row[0], operation="inspect")
        if value is not None:
            observations[row[0]] = value.json
    after = ctx.conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
    if before != after:
        raise StalePlan("the Codex run moved during its native observation")
    return Prepared(ids.canonical_json({"run_id": run_id, "event_cursor": before, "observations": observations}))


def fence_run(conn: sqlite3.Connection, prepared: Prepared | None) -> dict[str, Fenced]:
    if prepared is None:
        return {}
    if not conn.in_transaction or not isinstance(prepared, Prepared):
        raise RuntimeError("fence Codex run observations at write-transaction entry")
    body = json.loads(prepared.json)
    cursor = conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE run_id=?", (body["run_id"],)).fetchone()[0]
    if cursor != body["event_cursor"]:
        raise StalePlan("the Codex run event cursor moved before its observations were consumed")
    return {lease_id: fence(conn, Prepared(value)) for lease_id, value in body["observations"].items()}


def consume(conn: sqlite3.Connection, *, task: Mapping[str, Any], lease: Mapping[str, Any],
            prepared: Fenced | None, reported: Mapping[str, Any] | None,
            declared_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """No Host I/O: compare the immutable read to the task/lease and judge its period."""
    from . import observation
    doc, required, scope = requirement(conn, task)
    body = json.loads(prepared.json) if isinstance(prepared, Fenced) else None
    valid = body is not None and body["snapshot"]["task"]["task_id"] == task["task_id"]
    if valid:
        old = body["snapshot"]
        now = snapshot(conn, task["task_id"], lease["lease_id"])
        for key in ("task_id", "run_id", "attempt", "lease_id", "role", "task_class", "required_model", "required_effort_json"):
            valid = valid and old["task"].get(key) == now["task"].get(key)
        valid = valid and old["assignment"] == now["assignment"] and old["run"] == now["run"]
        valid = valid and old["lease"] == now["lease"] and body["policy_sha256"] == scope["policy_sha256"]
    record = _record(body["record"]) if valid else Record(host="codex", path="", sha256="", size=0,
        readable=False, reason="no transaction-fenced native Codex observation covers this task lease")
    summary = summarise(record, required_model=required["model"], allowed_efforts=required["allowed_efforts"])
    verdict = policy.judge(host="codex", required=required, reported=reported, observed=summary, policy=doc)
    identity = (body or {}).get("claim") or {}
    declared = declared_identity or {}
    disagreements = []
    for key, actual in (("host", "codex"), ("session_ref", identity.get("session_ref")),
                        ("agent_ref", identity.get("native_agent_ref"))):
        if declared.get(key) is not None and declared[key] != actual:
            disagreements.append("the declared " + key + " differs from the verified native lease holder")
    last = record.turns[-1] if record.turns and record.readable else None
    if last and reported:
        if reported.get("model") is not None and not policy.is_model(reported["model"], last.model):
            disagreements.append("the declared model contradicts the Host-applied model")
        if reported.get("effort") is not None and reported["effort"] != last.effort:
            disagreements.append("the declared effort contradicts the Host-applied effort")
    if disagreements:
        verdict.update(policy_verdict=policy.MISMATCH, basis="native_lease_identity",
                       failure_class=policy.FAILURE_POLICY, reasons=[*verdict["reasons"], *disagreements])
    if verdict["policy_verdict"] != policy.CONFORMS:
        verdict.update(bind_action="refuse", submit_action="hold", waits_for_user=True,
                       failure_class=verdict["failure_class"] or policy.FAILURE_POLICY)
    last = record.turns[-1] if record.turns and record.readable else None
    return {"verdict": verdict, "summary": summary, "record": record, "required": required,
            "judged_host": "codex", "policy_sha256": scope["policy_sha256"], "deviation": None,
            "usable": bool(record.readable and record.turns), "last_turn": last,
            "worker_ref": observation.observed_worker_ref(record) if record.readable and record.turns else None,
            "native_identity": {"session_ref": identity.get("session_ref"), "agent_ref": identity.get("native_agent_ref")},
            "model_authority": AUTHORITY, "upstream_tier_independently_verified": False,
            "claim_event_id": (body or {}).get("claim_event_id")}


def binding_observation(conn: sqlite3.Connection, *, lease_id: str, proof: Mapping[str, Any],
                        claim_event_id: int, config_root=None) -> dict[str, Any] | None:
    """The pool's own external read of this lease, never a client-supplied verdict.

    Call only after the backend claim receipt and Host dispatch have been verified.
    The short consuming transaction must call fence_binding_observation before use.
    """
    if conn.in_transaction:
        raise RuntimeError("read native pool policy evidence outside SQLite write transactions")
    lease = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
    if lease is None:
        return None
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (lease["task_id"],)).fetchone()
    if task is None or lease["released_at"] is not None or task["lease_id"] != lease_id:
        return None
    from ..teams import recovery
    effective = recovery.live_lease(conn, task["task_id"])
    if effective is None or effective["lease_id"] != lease_id:
        return None
    document, required, scope = requirement(conn, task)
    if any(proof.get(key) != value for key, value in scope.items()):
        return None
    before = snapshot(conn, task["task_id"], lease_id)
    record = codex.read_worker(proof["session_ref"], proof["native_agent_ref"], config_root=config_root)
    from . import codex_dispatch
    try:
        for _ in codex_dispatch._own_rows(Path(record.path), proof["native_agent_ref"], prefix=proof["applied_packet"]):
            pass
        record = _lease_record(record, proof, lease["released_at"])
        summary = summarise(record, required_model=required["model"], allowed_efforts=required["allowed_efforts"])
        verdict = policy.judge(host="codex", required=required, observed=summary, policy=document)
        # This is a lease-period audit. A completed turn remains evidence of its
        # applied configuration; it neither proves native termination nor grants
        # a future task. Actual claim/bind/submit calls require a fresh active turn.
        current = record.meta.get("current_turn") or {}
        if (verdict["policy_verdict"] != policy.CONFORMS or not record.meta.get("current_turn_readable")
                or current.get("status") not in ("active", "completed", "aborted")
                or snapshot(conn, task["task_id"], lease_id) != before):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    audit = {"policy_verdict": policy.CONFORMS, "model_authority": AUTHORITY,
        "upstream_tier_independently_verified": False, **scope,
        "lease_id": lease_id, "task_id": task["task_id"], "run_id": task["run_id"],
        "native_agent_ref": proof["native_agent_ref"], "session_ref": proof["session_ref"],
        "claim_event_id": claim_event_id, "database_snapshot_sha256": ids.sha256_of_obj(before),
        "rollout_prefix_sha256": record.sha256, "rollout_prefix_bytes": record.size,
        "current_packet_evidence_sha256": current.get("evidence_sha256"),
        "covered_since": record.turns[0].at, "covered_until": record.turns[-1].at,
        "claim_context_line": proof["applied_packet"]["context_line"], "lease_contexts": len(record.turns)}
    audit["observation_sha256"] = ids.sha256_of_obj(audit)
    return audit


def fence_binding_observation(conn: sqlite3.Connection, binding: Mapping[str, Any], *,
                              lease_id: str, native_agent_ref: str) -> bool:
    """Check the pool reader product under its short writer lock, without Host I/O."""
    if not conn.in_transaction:
        raise RuntimeError("fence pool native policy evidence in its consuming transaction")
    audit = binding.get("model_observation")
    if (not isinstance(audit, dict) or binding.get("policy_verdict") != policy.CONFORMS
            or audit.get("policy_verdict") != policy.CONFORMS or audit.get("model_authority") != AUTHORITY
            or audit.get("upstream_tier_independently_verified") is not False
            or audit.get("lease_id") != lease_id or audit.get("native_agent_ref") != native_agent_ref):
        return False
    unsigned = {key: value for key, value in audit.items() if key != "observation_sha256"}
    if ids.sha256_of_obj(unsigned) != audit.get("observation_sha256"):
        return False
    try:
        now = snapshot(conn, audit["task_id"], lease_id)
        if ids.sha256_of_obj(now) != audit.get("database_snapshot_sha256"):
            return False
        proof, event_id = _receipt(conn, now["task"], now["lease"])
        return (event_id == audit["claim_event_id"] and proof["native_agent_ref"] == native_agent_ref
                and proof["claim_call_id"] == binding.get("claim_call_id")
                and all(proof.get(key) == audit.get(key) for key in ("policy_sha256", "required_sha256", "role_intent_sha256")))
    except (ValueError, TypeError, KeyError, ModelPolicyViolation):
        return False
