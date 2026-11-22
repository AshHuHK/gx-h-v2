"""Restricted opaque Host followup dispatch, not plaintext authentication.

The native Root authorizes an exact backend task/assignment/OWNED slot tuple.
Only a later, independently read native followup to that already-owned worker can
confirm it. Neither ciphertext shape nor a returned ticket is a bearer credential.
The existing claim verifier still proves the actual child process, pending literal
CLI call, policy and transactional lease issue. No keys or message contents are read.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .. import config, contracts
from ..context import Ctx, SCOPE_HOST_ADAPTER
from ..domain import clock, ids, machines
from ..errors import GxError, IdempotencyConflict, PermissionDenied, SchemaInvalid, ScopeDenied, StalePlan
from ..persistence import store
from ..registry import ToolResult
from . import codex, codex_dispatch as legacy, codex_worker_observation as worker, policy

AUTHORIZE = "CodexDispatchAuthorize"
CONFIRM = "CodexDispatchConfirm"
AUTHORIZED = "host.codex_opaque_dispatch_authorized"
CONFIRMED = "host.codex_opaque_dispatch_confirmed"
CONSUMED = "host.codex_opaque_dispatch_consumed"
MODE = "backend_authorized_opaque_followup"
TTL_SECONDS = 600
MAX_OUTPUT_BYTES = 262144


def handles(args: Mapping[str, Any]) -> bool:
    return args.get("host") == "codex" and args.get("hook_event") in (AUTHORIZE, CONFIRM)


def _deny(message: str, cause: str = "SCOPE_DENIED") -> None:
    raise ScopeDenied(message, details={"dispatch_mode": MODE, "cause": cause})


def _shape(value: Any, required: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(required):
        raise SchemaInvalid("opaque dispatch payload requires exactly its documented identifier fields")
    for key, item in value.items():
        if key.startswith("expected_"):
            if type(item) is not int or item < 0:
                raise SchemaInvalid(key + " must be a nonnegative integer")
        elif not isinstance(item, str) or not item or len(item) > 256:
            raise SchemaInvalid(key + " must be a bounded identifier")
    return value


def _root_path(config_root: Path | None) -> Path:
    return (config_root if config_root is not None else Path.home() / ".codex").resolve()


def _path(config_root: Path | None, native: str) -> Path:
    root = _root_path(config_root)
    with sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
        row = conn.execute("SELECT rollout_path FROM threads WHERE id=?", (native,)).fetchone()
    if row is None:
        _deny("native thread has no Host-owned record")
    path = Path(row[0]).resolve(strict=True)
    if not path.is_relative_to(root / "sessions"):
        _deny("native thread record is outside the Host session root")
    return path


def _literal_event(ctx: Ctx, command: str, args: dict[str, Any]) -> bool:
    """A literal CLI with its real store and exact full arguments, no evaluation."""
    try:
        words = shlex.split(command)
        if words and words[0] == "env":
            words.pop(0)
        env: dict[str, str] = {}
        while words and "=" in words[0] and not words[0].startswith("-"):
            key, value = words.pop(0).split("=", 1)
            if key not in ("GX_DB", "GX_PROJECTS", "PYTHONDONTWRITEBYTECODE") or key in env:
                return False
            env[key] = value
        db = next((row[2] for row in ctx.conn.execute("PRAGMA database_list") if row[1] == "main"), None)
        if not db or Path(env.get("GX_DB", str(config.db_path()))).resolve() != Path(db).resolve():
            return False
        program = words.pop(0)
        if Path(program).name.startswith("python"):
            if words and words[0] == "-B":
                words.pop(0)
            if words[:2] != ["-m", "gx_core.cli"]:
                return False
            del words[:2]
        elif Path(program).resolve() != (contracts.HARNESS_ROOT / "bin/gx").resolve():
            return False
        subjects, bodies, remainder = [], [], []
        while words:
            token = words.pop(0)
            if token in ("--as", "--json"):
                (subjects if token == "--as" else bodies).append(words.pop(0))
            else:
                remainder.append(token)
        return (subjects == ["host_adapter:" + ctx.principal.subject] and len(bodies) == 1
                and remainder == ["call", "gx_host_event"]
                and ids.canonical_json(json.loads(bodies[0])) == ids.canonical_json(args))
    except (ValueError, OSError, IndexError, TypeError):
        return False


def _prefix(path: Path, native: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    last = 0
    for line, _ in legacy._own_rows(path, native, prefix=packet,
            initial_context_line=packet["context_line"] if packet.get("source_boundary_line")
                and packet["context_line"] < packet["source_boundary_line"] else None):
        last = line
    return {"rollout_prefix_bytes": packet["rollout_prefix_bytes"],
            "rollout_prefix_sha256": packet["rollout_prefix_sha256"], "last_line": last}


def _unchanged_prefix(path: Path, saved: Mapping[str, Any]) -> None:
    remaining = saved["rollout_prefix_bytes"]
    if type(remaining) is not int or not 0 < remaining <= codex.MAX_RECORD_BYTES:
        _deny("authorization has no bounded parent prefix")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while remaining:
            block = stream.read(min(65536, remaining))
            if not block:
                _deny("Host record no longer contains the authorized prefix")
            remaining -= len(block)
            digest.update(block)
    if digest.hexdigest() != saved["rollout_prefix_sha256"]:
        _deny("Host record changed its authorized prefix")


def _root_call(ctx: Ctx, args: dict[str, Any], run: Mapping[str, Any], *,
               config_root: Path | None, proc_root: Path, pid: int | None) -> dict[str, Any]:
    if ctx.conn.in_transaction or ctx.transport != "cli" or ctx.principal.scope != SCOPE_HOST_ADAPTER:
        _deny("opaque dispatch authorizations require the native Root's current public CLI")
    actual = legacy.current_invocation(proc_root=proc_root, pid=pid)
    session = legacy._root_session(ctx.conn, run["run_id"])
    if (actual is None or not session or actual.native_agent_ref != session or actual.session_ref != session
            or Path(actual.cwd) != contracts.HARNESS_ROOT or args.get("session_ref") != session
            or args.get("agent_ref") != session or not _literal_event(ctx, actual.command, args)):
        _deny("the real native Root invocation does not name this exact dispatch request")
    packet = codex.observe_native(host="codex", session_ref=session, native_agent_ref=session, config_root=config_root)
    from . import host_policy
    document, _ = host_policy.current_for_run(run)
    pending = legacy.verified_pending_invocation(actual, packet, config_root=config_root)
    if (pending is None or not packet.readable or packet.native_parent_ref is not None or packet.native_depth != 0
            or not policy.is_pinned_model(packet.model, document)
            or packet.effort not in document["native_coordinator_observation"]["accepted_efforts"]):
        _deny("native Root pending call or applied coordinator model/effort is not verified")
    path = _path(config_root, session)
    call_at = None
    for line, row in legacy._own_rows(path, session, prefix=packet.current_turn):
        if line == pending["call_line"]:
            if legacy._exec_commands(row["payload"]) != [actual.command]:
                _deny("authorization must be the only literal CLI in its native exec call")
            call_at = row.get("timestamp")
    if not isinstance(call_at, str):
        _deny("native authorization call has no Host timestamp")
    if clock.parse(call_at) > clock.now():
        _deny("native Root call timestamp is in the future")
    if (legacy._stat(proc_root, actual.shell_pid)[1] != actual.shell_start
            or legacy._stat(proc_root, actual.host_pid)[1] != actual.host_start):
        _deny("native Root process changed while it authorized dispatch")
    return {**pending, "call_at": call_at, "prefix": _prefix(path, session, packet.current_turn),
            "identity_sha256": packet.evidence_sha256,
            "applied_packet_sha256": packet.current_turn["evidence_sha256"]}


def _run_gate(ctx: Ctx, run_id: str, lease_ref: str, revision: int) -> dict[str, Any]:
    from ..orchestration import service
    from ..teams import manager

    config.check_holds("planning")
    run = manager._run(ctx, run_id)
    manager._require_pins(ctx, run)
    store.check_revision(ctx.conn, run_id, revision)
    lease = service.require_coordinator(ctx, ctx.conn, run_id, lease_ref, renew=False)
    if lease is None or lease["owner"] != ctx.principal.subject or lease["host"] != "codex":
        _deny("dispatch requires the genuine coordinator owner, without operator impersonation")
    if not worker.scoped_run(ctx.conn, run_id):
        _deny("opaque dispatch cannot select policy for a legacy run")
    return dict(run)


def _state(conn: sqlite3.Connection, *, run_id: str, task_id: str, subject: str,
           slot_id: str, expected_slot_generation: int, expected_pool_generation: int,
           issued: bool = False) -> dict[str, Any]:
    task = conn.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone()
    assignment = conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
    slot = conn.execute("SELECT * FROM host_pool_slots WHERE slot_id=?", (slot_id,)).fetchone()
    if task is None or assignment is None or slot is None:
        _deny("dispatch needs a real backend task, assignment and owned slot")
    pool = conn.execute("SELECT * FROM host_pools WHERE pool_id=?", (slot["pool_id"],)).fetchone()
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    session = legacy._root_session(conn, run_id)
    if (pool is None or session is None or pool["host"] != "codex" or pool["session_ref"] != session
            or (slot["current_run_id"] or slot["run_id"]) != run_id or slot["state"] not in (("ACTIVE", "CLOSING") if issued else ("ACTIVE",))
            or slot["pool_generation"] != pool["session_generation"]
            or pool["session_generation"] != expected_pool_generation
            or (not issued and slot["generation"] != expected_slot_generation)
            or not codex.valid_thread_ref(slot["native_agent_ref"]) or slot["native_agent_ref"] == session
            or slot["observed_source"] != "host_transcript"):
        _deny("dispatch slot/root/current-run/generations are not the protected current binding")
    if not issued and (task["status"] != "READY" or task["lease_id"] is not None):
        _deny("dispatch authorization admits only a READY task without a lease")
    if slot["task_id"] != task_id:
        old = conn.execute("SELECT * FROM leases WHERE lease_id=? AND task_id=?",
                           (slot["lease_id"], slot["task_id"])).fetchone()
        prior = conn.execute("SELECT * FROM tasks WHERE task_id=?", (slot["task_id"],)).fetchone()
        if (old is None or prior is None or old["released_at"] is None
                or not machines.is_terminal("task", prior["status"]) or old["principal"] != subject):
            _deny("the owned worker's prior task/lease/principal has not settled")
        previous_assignment = conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (slot["task_id"],)).fetchone()
        if previous_assignment is not None and (previous_assignment["status"] != "ASSIGNED"
                or previous_assignment["pending_json"] is not None or previous_assignment["subject"] != subject):
            _deny("the previous native assignment has an unsettled transfer")
        for job in conn.execute("SELECT * FROM jobs WHERE task_id=? OR lease_ref=?", (slot["task_id"], slot["lease_id"])):
            if not machines.is_terminal("job", job["status"]) or job["side_effect_status"] == "UNKNOWN":
                _deny("a previous native task still owns an active or unknown job")
    if (assignment["subject"] != subject or assignment["status"] != "ASSIGNED"
            or assignment["pending_json"] is not None):
        _deny("the target task has no settled assignment to this exact principal")
    from ..teams import manager
    team = manager._team(conn, run_id, assignment["team_id"])
    if not issued:
        manager._accepts_work(conn, run_id, team)
    member = conn.execute("SELECT * FROM team_members WHERE team_id=? AND subject=?",
                          (team["team_id"], subject)).fetchone()
    observed = f"codex:{session}:{slot['native_agent_ref']}"
    if not issued and (member is None or member["status"] != "ACTIVE" or any(value not in (None, observed)
            for value in (member["observed_worker_ref"], assignment["observed_worker_ref"]))):
        _deny("native target differs from the active team member or assigned worker")
    manager._context(conn, run_id, assignment["context_ref"], current=not issued)
    manager.assignment_intent(conn, assignment)
    document, required, scope = worker.requirement(conn, task, current=True)
    from ..orchestration import service as orchestration
    run_pins = {"workflow_id": run["workflow_id"], "workflow_version": run["workflow_version"],
                **orchestration.run_pins(run)}
    if not issued and run["workflow_id"]:
        workflow = orchestration._pinned_workflow(conn, run)
        if workflow is None:
            _deny("the exact authorized workflow is not installed")
        orchestration.require_pins(run, workflow)
    contract = legacy._contract(conn, run_id, task_id, subject)
    if contract is None or (not issued and (run["status"] == "PAUSED" or machines.is_terminal("run", run["status"]))):
        _deny("the target run is not currently pinned and admitting work")
    body = {"contract": contract, "run_pins": run_pins, "role": task["role"], "project_ref": run["project_ref"],
            "slot_id": slot_id, "slot_generation": expected_slot_generation,
            "pool_id": pool["pool_id"], "pool_generation": pool["session_generation"],
            "native_agent_ref": slot["native_agent_ref"], "native_parent_ref": slot["native_parent_ref"],
            "native_depth": slot["native_depth"], "session_ref": session,
            "assignment_sha256": ids.sha256_of_obj(dict(assignment)),
            "member_sha256": ids.sha256_of_obj(dict(member)) if member is not None else None}
    return {"tuple": body, "snapshot": {"task": dict(task), "assignment": dict(assignment), "team": dict(team),
            "member": dict(member) if member is not None else None, "slot": dict(slot), "pool": dict(pool), "run": dict(run),
            "coordinator": dict(store.coordinator_lease(conn, run_id)),
            "event_cursor": conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]},
            "document": document, "required": required}


def _event(conn: sqlite3.Connection, event_id: str | int, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute("SELECT * FROM events WHERE event_id=? AND kind=?", (event_id, kind)).fetchone()
    if row is None or row["evidence_level"] != "EXECUTION_OBSERVED":
        _deny("dispatch receipt is not a protected backend event")
    value = json.loads(row["payload_json"])
    digest = value.pop("evidence_sha256", None)
    if digest != ids.sha256_of_obj(value) or value.get("message_content_observed") is not False or value.get("evidence_mode") != MODE:
        _deny("dispatch receipt identity or evidence qualification is invalid")
    return dict(row), value


def _latest(conn: sqlite3.Connection, native: str) -> int | None:
    row = conn.execute("SELECT event_id FROM events WHERE kind=? AND subject_kind='native_dispatch'"
                       " AND subject_id=? ORDER BY event_id DESC LIMIT 1", (AUTHORIZED, native)).fetchone()
    return row[0] if row else None


def _valid_authorization(conn: sqlite3.Connection, event: Mapping[str, Any], auth: Mapping[str, Any], *,
                         issued: bool = False) -> dict[str, Any]:
    value = auth["binding"]
    current = _state(conn, run_id=event["run_id"], task_id=value["contract"]["task_id"],
        subject=value["contract"]["principal_subject"], slot_id=value["slot_id"],
        expected_slot_generation=value["slot_generation"], expected_pool_generation=value["pool_generation"], issued=issued)
    expected = json.loads(ids.canonical_json(value))
    if issued:
        expected["contract"]["expected_attempt"] += 1
        # Membership/active-context were admission conditions, not revocation of an
        # already-issued lease. Its immutable assignment and receipt remain exact.
        expected["member_sha256"] = current["tuple"]["member_sha256"]
    if current["tuple"] != expected:
        _deny("task, assignment, context, policy, principal or native target moved after authorization")
    if not issued:
        if _latest(conn, value["native_agent_ref"]) != event["event_id"] or clock.is_past(auth["expires_at"]):
            _deny("dispatch authorization expired or was superseded")
        lease = store.coordinator_lease(conn, event["run_id"])
        if (lease is None or lease["lease_id"] != auth["coordinator_lease_ref"] or not store.coordinator_lease_live(lease)
                or lease["owner"] != auth["coordinator_subject"]):
            _deny("dispatch authorization's actual coordinator no longer holds its live lease")
    return current


def _receipt_in_output(output: Any, event_id: int, request_id: str) -> bool:
    """Only exact backend success envelopes in the native exec result prove completion."""
    stack, visited, total = [output], 0, 0
    while stack and visited < 64:
        value = stack.pop(); visited += 1
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
            if total > MAX_OUTPUT_BYTES:
                return False
            try:
                stack.append(json.loads(value))
            except ValueError:
                pass
        elif isinstance(value, list):
            stack.extend(value[:32])
        elif isinstance(value, dict):
            if (value.get("ok") is True and value.get("request_id") == request_id
                    and isinstance(value.get("data"), dict) and value["data"].get("event_id") == str(event_id)):
                return True
            for key in ("text", "output", "content"):
                if key in value:
                    stack.append(value[key])
    return False


def _envelope(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, str) or not 80 <= len(message) <= 65536 or legacy.OPEN in message:
        return None
    try:
        raw = message.encode("ascii")
        decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
        if (base64.urlsafe_b64encode(decoded) != raw or decoded[0] != 0x80
                or len(decoded) < 73 or (len(decoded) - 57) % 16):
            return None
    except (UnicodeError, ValueError, IndexError):
        return None
    return {"envelope_sha256": hashlib.sha256(raw).hexdigest(), "envelope_bytes": len(raw),
            "opaque_transport_shape": "fernet_v0x80_like_not_authenticated"}


def _action(event: Mapping[str, Any], auth: Mapping[str, Any], *, config_root: Path | None,
            before: Mapping[str, Any] | None = None, confirmed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    binding, root_proof = auth["binding"], auth["root_invocation"]
    session, lineage_parent, native = binding["session_ref"], binding["native_parent_ref"], binding["native_agent_ref"]
    # The authorized sender is Root, even for an owned depth-2/3/4 descendant.
    parent = session
    observations = {ref: codex.observe_native(host="codex", session_ref=session, native_agent_ref=ref,
                                              config_root=config_root) for ref in {session, lineage_parent, native}}
    child, creator, root = observations[native], observations[session], observations[session]
    if (not all(item.readable and item.current_turn_readable for item in observations.values())
            or child.native_parent_ref != lineage_parent or child.native_depth != binding["native_depth"]
            or observations[lineage_parent].native_depth != child.native_depth - 1 or child.agent_path != auth["agent_path"]):
        _deny("native Host lineage or current applied metadata differs from its authorization")
    root_path, parent_path = _path(config_root, session), _path(config_root, parent)
    _unchanged_prefix(root_path, root_proof["prefix"])
    _unchanged_prefix(parent_path, auth["sender_prefix"])
    completions = []
    for number, row in legacy._own_rows(root_path, session, prefix=root.current_turn):
        payload = row["payload"]
        if (row["type"] == "response_item" and payload.get("call_id") == root_proof["call_id"]
                and payload.get("type") in ("function_call_output", "custom_tool_call_output")
                and number > root_proof["call_line"]
                and _receipt_in_output(payload.get("output"), event["event_id"], auth["request_id"])):
            completions.append((number, row.get("timestamp")))
    if len(completions) != 1 or clock.parse(completions[0][1]) < clock.parse(event["ts"]):
        _deny("the actual Root has not received this committed backend authorization receipt")
    calls, activities, results = {}, {}, {}
    for number, row in legacy._own_rows(parent_path, parent, prefix=creator.current_turn):
        payload = row["payload"]
        if row["type"] == "response_item":
            call_id = payload.get("call_id")
            if payload.get("type") == "function_call" and payload.get("name") == "followup_task" and payload.get("namespace") == "collaboration":
                args = json.loads(payload.get("arguments", "{}"))
                opaque = _envelope(args.get("message")) if isinstance(args, dict) else None
                if (opaque and args.get("target") in (auth["agent_path"], auth["agent_path"].rsplit("/", 1)[-1])
                        and number > auth["sender_prefix"]["last_line"]
                        and clock.parse(row["timestamp"]) > clock.parse(completions[0][1])
                        and (parent != session or number > completions[0][0])):
                    calls[call_id] = (number, row["timestamp"], opaque)
            elif payload.get("type") == "function_call_output":
                results[call_id] = (number, row.get("timestamp"), payload.get("output"))
        elif row["type"] == "event_msg" and payload.get("type") == "item_completed" and payload.get("thread_id") == parent:
            item = payload.get("item") or {}
            if (item.get("type") == "SubAgentActivity" and item.get("agent_thread_id") == native
                    and item.get("agent_path") == auth["agent_path"]):
                activities[item.get("id")] = (number, row.get("timestamp"), item.get("kind"))
    matches = []
    for call_id, (line, at, opaque) in calls.items():
        activity, result = activities.get(call_id), results.get(call_id)
        if (not activity or not result or not line < activity[0] < result[0]
                or activity[2] != "interacted" or result[2] != ""
                or not clock.parse(at) <= clock.parse(activity[1]) <= clock.parse(result[1])):
            continue
        if before is not None and (clock.parse(result[1]) >= clock.parse(before["call_at"])
                or (parent == session and result[0] >= before["call_line"])):
            continue
        matches.append({"dispatch_call_id": call_id, "dispatch_tool": "followup_task", "dispatch_line": line,
                        "native_result_line": result[0], "dispatch_at": at, "native_result_at": result[1], "dispatch_sender_native_ref": session, **opaque})
    if confirmed is not None:
        matches = [item for item in matches if item == confirmed]
    if len(matches) != 1:
        _deny("opaque dispatch needs exactly one later real parent action, target, activity and successful outcome")
    return matches[0]


def _append(ctx: Ctx, kind: str, payload: dict[str, Any], subject_kind: str, subject_id: str,
            run_id: str, revision: int) -> tuple[int, int]:
    payload.update(evidence_mode=MODE, authorization_source="backend_native_root_dispatch_intent",
                   message_content_observed=False)
    payload["evidence_sha256"] = ids.sha256_of_obj(payload)
    prev, new = store.bump_revision(ctx.conn, run_id, revision)
    event = store.append_event(ctx.conn, kind=kind, actor=ctx.actor, run_id=run_id,
        subject_kind=subject_kind, subject_id=subject_id, evidence_level="EXECUTION_OBSERVED",
        prev_revision=prev, new_revision=new, payload=payload,
        reason="backend-owned opaque dispatch intent; ciphertext plaintext and HMAC are not observed")
    return event, new


def _result(event_id: int, phase: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return {"event_id": str(event_id), "matched": {"phase": phase, **dict(value)},
            "policy_verdict": None, "decision": "allow", "effect": "applied",
            "reason": "native Root authorization and Host metadata, without observing message content"}


def fence_claim_receipt(ctx: Ctx, proof: Mapping[str, Any]) -> None:
    """Atomically consume only this protocol's prepared authority, before lease issue.

    Runs after every historical Native5 snapshot check, in the very transaction
    issuing the lease. No Host/process/rollout reader is called here. A later task
    admission failure rolls this event back with the entire claim transaction.
    The backend clock at this consumption point, rather than the earlier Host read
    or later claim-receipt timestamp, determines whether admission met the deadline.
    """
    if not ctx.conn.in_transaction or proof.get("evidence_mode") != MODE:
        raise RuntimeError("consume opaque dispatch only inside its task-claim transaction")
    event, auth = _event(ctx.conn, proof.get("opaque_authorization_event_id"), AUTHORIZED)
    confirmation_event, confirmation = _event(ctx.conn, proof.get("opaque_confirmation_event_id"), CONFIRMED)
    state = _valid_authorization(ctx.conn, event, auth)
    binding = state["tuple"]
    if (ctx.principal.subject != binding["contract"]["principal_subject"]
            or proof.get("request_id") != ctx.request_id or proof.get("source") != "host_transcript"
            or proof.get("message_content_observed") is not False
            or proof.get("authorization_source") != "backend_native_root_dispatch_intent"
            or proof.get("native_agent_ref") != binding["native_agent_ref"]
            or proof.get("native_parent_ref") != binding["native_parent_ref"]
            or proof.get("session_ref") != binding["session_ref"]
            or any(proof.get(key) != value for key, value in binding["contract"].items())
            or confirmation_event["run_id"] != event["run_id"]
            or confirmation.get("authorization_event_id") != event["event_id"]
            or confirmation.get("binding_sha256") != ids.sha256_of_obj(binding)
            or any(proof.get(key) != value for key, value in confirmation["action"].items())):
        _deny("the internal opaque claim proof is not this exact current authorization and confirmation")
    confirmations = ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND subject_id=?",
                                     (CONFIRMED, str(event["event_id"]))).fetchone()[0]
    if confirmations != 1 or ctx.conn.execute("SELECT 1 FROM events WHERE kind=? AND subject_id=? LIMIT 1",
            (CONSUMED, str(event["event_id"]))).fetchone():
        _deny("the opaque dispatch confirmation is ambiguous or its authorization has already been consumed")
    coordinator = store.coordinator_lease(ctx.conn, event["run_id"])
    admitted_at = clock.now_iso()
    if (clock.parse(admitted_at) >= clock.parse(auth["expires_at"])
            or clock.parse(admitted_at) >= clock.parse(coordinator["expires_at"])):
        _deny("the opaque dispatch authorization or genuine coordinator expired before atomic consumption")
    value = {"authorization_event_id": event["event_id"], "confirmation_event_id": confirmation_event["event_id"],
        "binding_sha256": ids.sha256_of_obj(binding), "run_id": event["run_id"],
        "task_id": binding["contract"]["task_id"], "expected_attempt": binding["contract"]["expected_attempt"],
        "principal_subject": ctx.principal.subject, "native_agent_ref": binding["native_agent_ref"],
        "claim_call_id": proof["claim_call_id"], "claim_turn_id": proof["claim_turn_id"],
        "request_id": ctx.request_id, "coordinator_lease_ref": auth["coordinator_lease_ref"],
        "admitted_at": admitted_at, "evidence_mode": MODE, "source": "host_transcript",
        "authorization_source": "backend_native_root_dispatch_intent", "message_content_observed": False}
    value["evidence_sha256"] = ids.sha256_of_obj(value)
    store.append_event(ctx.conn, kind=CONSUMED, actor=ctx.actor, run_id=event["run_id"],
        subject_kind="native_dispatch_authorization", subject_id=str(event["event_id"]),
        evidence_level="EXECUTION_OBSERVED", payload=value,
        reason="exact opaque authorization consumed at native claim admission; this transaction also issues its lease")


def handle(ctx: Ctx, args: dict[str, Any], *, config_root: Path | None = None,
           proc_root: Path = Path("/proc"), pid: int | None = None) -> ToolResult:
    if not handles(args) or ctx.principal.scope != SCOPE_HOST_ADAPTER:
        raise PermissionDenied("this handler accepts only the two native Codex dispatch lifecycle events")
    payload = _shape(args.get("payload"), ("run_id", "task_id", "principal_subject", "slot_id",
        "coordinator_lease_ref", "expected_revision", "expected_slot_generation", "expected_pool_generation")
        if args["hook_event"] == AUTHORIZE else ("run_id", "authorization_event_id", "coordinator_lease_ref", "expected_revision"))
    if not isinstance(args.get("idempotency_key"), str) or not args["idempotency_key"].strip():
        raise SchemaInvalid("a dispatch lifecycle event needs an idempotency_key")
    if ctx.conn.in_transaction:
        raise RuntimeError("native dispatch Host reads belong outside SQLite write transactions")
    run = ctx.conn.execute("SELECT * FROM runs WHERE run_id=?", (payload["run_id"],)).fetchone()
    if run is None or not ctx.principal.may_access_project(run["project_ref"]):
        _deny("the dispatch run is outside the caller's project scope")
    request = {**args, "backend_principal": ctx.principal.subject}
    key = store.tool_key("gx_host_event", args["idempotency_key"])
    # A successful replay reveals its old receipt but never renews authority or TTL.
    prior = ctx.conn.execute("SELECT * FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
    if prior is not None:
        _root_call(ctx, args, run, config_root=config_root, proc_root=proc_root, pid=pid)
        if prior["request_sha"] != ids.sha256_of_obj(request):
            raise IdempotencyConflict("this dispatch event key belongs to a different exact request/principal")
        if prior["completed_at"] and prior["side_effect_status"] == "APPLIED":
            return ToolResult(data=json.loads(prior["response_json"]), run_id=payload["run_id"])
        _deny("an unfinished dispatch authorization cannot be repeated as a new intent")
    run = _run_gate(ctx, payload["run_id"], payload["coordinator_lease_ref"], payload["expected_revision"])
    root = _root_call(ctx, args, run, config_root=config_root, proc_root=proc_root, pid=pid)
    if args["hook_event"] == AUTHORIZE:
        state = _state(ctx.conn, **{key: payload[key] for key in ("run_id", "task_id", "slot_id",
            "expected_slot_generation", "expected_pool_generation")}, subject=payload["principal_subject"])
        value = state["tuple"]
        child = codex.observe_native(host="codex", session_ref=value["session_ref"],
            native_agent_ref=value["native_agent_ref"], config_root=config_root)
        parent = codex.observe_native(host="codex", session_ref=value["session_ref"],
            native_agent_ref=value["native_parent_ref"], config_root=config_root)
        if (not child.readable or not child.current_turn_readable or not parent.readable or not parent.current_turn_readable
                or child.native_parent_ref != value["native_parent_ref"] or child.native_depth != value["native_depth"]
                or parent.native_depth != child.native_depth - 1 or not child.agent_path):
            _deny("the owned native target's actual lineage/model metadata is not readable")
        sender_prefix = root["prefix"]
        prepared = {"binding": value, "root_invocation": root, "sender_prefix": sender_prefix,
            "agent_path": child.agent_path, "coordinator_lease_ref": payload["coordinator_lease_ref"],
            "coordinator_subject": ctx.principal.subject, "request_id": ctx.request_id}
    else:
        event, auth = _event(ctx.conn, payload["authorization_event_id"], AUTHORIZED)
        if event["run_id"] != payload["run_id"] or auth["coordinator_subject"] != ctx.principal.subject:
            _deny("a dispatch confirmation cannot change its authorization run or principal")
        state = _valid_authorization(ctx.conn, event, auth)
        child = codex.observe_native(host="codex", session_ref=auth["binding"]["session_ref"],
            native_agent_ref=auth["binding"]["native_agent_ref"], config_root=config_root)
        worker.checked_packet(child, state["required"], state["document"])
        prepared = {"authorization_event_id": event["event_id"], "binding_sha256": ids.sha256_of_obj(auth["binding"]),
                    "action": _action(event, auth, config_root=config_root, before=root), "root_invocation": root}
    with store.transaction(ctx.conn):
        record = store.begin_idempotent(ctx.conn, key, "gx_host_event", request)
        if record.replay:
            return ToolResult(data=record.response, run_id=payload["run_id"])
        if not record.fresh:
            _deny("dispatch lifecycle key is already in progress")
        _run_gate(ctx, payload["run_id"], payload["coordinator_lease_ref"], payload["expected_revision"])
        current = _state(ctx.conn, run_id=payload["run_id"], task_id=state["tuple"]["contract"]["task_id"],
            subject=state["tuple"]["contract"]["principal_subject"], slot_id=state["tuple"]["slot_id"],
            expected_slot_generation=state["tuple"]["slot_generation"], expected_pool_generation=state["tuple"]["pool_generation"])
        if current["snapshot"] != state["snapshot"]:
            raise StalePlan("dispatch task, coordinator, assignment, slot or event cursor moved during Host reads")
        if args["hook_event"] == AUTHORIZE:
            latest = _latest(ctx.conn, value["native_agent_ref"])
            if latest is not None:
                _, old = _event(ctx.conn, latest, AUTHORIZED)
                consumed = ctx.conn.execute("SELECT 1 FROM events WHERE kind=? AND json_extract(payload_json,'$.opaque_authorization_event_id')=? LIMIT 1",
                    (legacy.CLAIM_EVENT, latest)).fetchone()
                if not clock.is_past(old["expires_at"]) and consumed is None:
                    _deny("an unexpired native authorization already exists; reuse it or wait for expiry")
            coordinator = store.coordinator_lease(ctx.conn, payload["run_id"])
            prepared["expires_at"] = min(clock.iso_after(TTL_SECONDS), coordinator["expires_at"])
            eid, revision = _append(ctx, AUTHORIZED, prepared, "native_dispatch", value["native_agent_ref"], payload["run_id"], payload["expected_revision"])
            result = _result(eid, "AUTHORIZED", {"authorization_event_id": str(eid), "expires_at": prepared["expires_at"],
                "task_contract": legacy.OPEN + ids.canonical_json(value["contract"]) + legacy.CLOSE,
                "native_agent_ref": value["native_agent_ref"], "parent_native_ref": value["native_parent_ref"],
                "agent_path": child.agent_path, "message_content_observed": False})
        else:
            _valid_authorization(ctx.conn, event, auth)
            if ctx.conn.execute("SELECT 1 FROM events WHERE kind=? AND subject_id=? LIMIT 1", (CONFIRMED, str(event["event_id"]))).fetchone():
                _deny("this authorization already has a confirmation; replay its original key")
            eid, revision = _append(ctx, CONFIRMED, prepared, "native_dispatch_authorization", str(event["event_id"]), payload["run_id"], payload["expected_revision"])
            result = _result(eid, "CONFIRMED", {"authorization_event_id": str(event["event_id"]),
                "dispatch_call_id": prepared["action"]["dispatch_call_id"], "message_content_observed": False})
        store.complete_idempotent(ctx.conn, key, result)
    return ToolResult(data=result, run_id=payload["run_id"], revision=revision)


def _claim_after(conn: sqlite3.Connection, event: Mapping[str, Any], auth: Mapping[str, Any], action: Mapping[str, Any],
                 expected: dict[str, Any], child: codex.NativeObservation, config_root: Path | None,
                 issued_attempt: int | None, confirmed_at: str) -> None:
    actual = legacy.current_invocation() if issued_attempt is None else None
    if issued_attempt is None and (actual is None or actual.native_agent_ref != child.native_agent_ref
            or actual.session_ref != child.session_ref or Path(actual.cwd) != contracts.HARNESS_ROOT
            or not legacy._claim_command(actual.command, expected, auth["binding"]["role"])):
        _deny("opaque dispatch has no actual current child CLI invocation")
    receipt = None
    if issued_attempt is not None:
        receipts = list(conn.execute("SELECT payload_json,ts,event_id FROM events WHERE kind=? AND subject_id=?"
            " AND json_extract(payload_json,'$.opaque_authorization_event_id')=?", (legacy.CLAIM_EVENT, expected["task_id"], event["event_id"])))
        if len(receipts) != 1:
            _deny("issued opaque dispatch has no unique atomic claim receipt")
        receipt = json.loads(receipts[0]["payload_json"])
        consumptions = list(conn.execute("SELECT event_id FROM events WHERE kind=? AND subject_id=? LIMIT 2",
                                         (CONSUMED, str(event["event_id"]))))
        if len(consumptions) != 1:
            _deny("the issued opaque lease has no unique atomic authorization consumption")
        consumed_event, consumed = _event(conn, consumptions[0][0], CONSUMED)
        expected_consumption = {"authorization_event_id": event["event_id"],
            "confirmation_event_id": receipt.get("opaque_confirmation_event_id"),
            "binding_sha256": ids.sha256_of_obj(auth["binding"]), "run_id": receipt.get("run_id"),
            "task_id": expected["task_id"], "expected_attempt": expected["expected_attempt"],
            "principal_subject": receipt.get("principal_subject"), "native_agent_ref": child.native_agent_ref,
            "claim_call_id": receipt.get("claim_call_id"), "claim_turn_id": receipt.get("claim_turn_id"),
            "request_id": receipt.get("request_id"), "coordinator_lease_ref": auth["coordinator_lease_ref"]}
        if (receipt.get("attempt") != issued_attempt or receipt.get("native_agent_ref") != child.native_agent_ref
                or any(consumed.get(key) != value for key, value in expected_consumption.items())
                or consumed_event["run_id"] != event["run_id"]
                or consumed_event["event_id"] >= receipts[0]["event_id"]
                or clock.parse(consumed["admitted_at"]) >= clock.parse(auth["expires_at"])
                or clock.parse(consumed["admitted_at"]) > clock.parse(receipts[0]["ts"])):
            _deny("the issued lease is not the authorization's unique timely atomic native consumption")
    # A live claim uses the reader's current packet; a historical issued claim
    # uses the unique protected claim receipt already checked above. A later own
    # turn must not erase the original pre-settings claim context.
    context_packet = child.current_turn if receipt is None else receipt.get("applied_packet")
    turn_id = child.current_turn["turn_id"] if receipt is None else receipt.get("claim_turn_id")
    if (not isinstance(context_packet, Mapping) or context_packet.get("turn_id") != turn_id
            or type(context_packet.get("context_line")) is not int or context_packet["context_line"] < 1
            or type(context_packet.get("source_boundary_line")) is not int or context_packet["source_boundary_line"] < 1
            or context_packet["source_boundary_line"] != child.current_turn.get("source_boundary_line")):
        _deny("the native claim has no matching trusted own-context boundary")
    pending, calls, context = {}, {}, None
    for line, row in legacy._own_rows(_path(config_root, child.native_agent_ref), child.native_agent_ref,
            prefix=child.current_turn,
            initial_context_line=context_packet["context_line"]
                if context_packet["context_line"] < context_packet["source_boundary_line"] else None):
        p = row["payload"]
        if row["type"] == "turn_context":
            context = (line, p.get("turn_id"))
        if row["type"] != "response_item" or not isinstance(p.get("call_id"), str):
            continue
        call = p["call_id"]
        if p.get("type") in ("custom_tool_call", "function_call"):
            metadata = p.get("internal_chat_message_metadata_passthrough") or {}
            commands = legacy._exec_commands(p)
            turn = child.current_turn["turn_id"] if receipt is None else receipt.get("claim_turn_id")
            if (context is not None and metadata.get("turn_id") == context[1] == turn and line > context[0]
                    and clock.parse(row["timestamp"]) > max(clock.parse(action["native_result_at"]), clock.parse(confirmed_at))
                    and any(legacy._claim_command(command, expected, auth["binding"]["role"])
                            and (actual is None or command == actual.command) for command in commands)):
                calls[call] = line
                pending[call] = line
        elif p.get("type") in ("custom_tool_call_output", "function_call_output"):
            pending.pop(call, None)
    if receipt is None:
        if len(pending) != 1:
            _deny("the actual child literal invocation does not uniquely follow the confirmed Host action")
        if conn.execute("SELECT 1 FROM events WHERE kind=? AND subject_id=? LIMIT 1",
                (CONSUMED, str(event["event_id"]))).fetchone():
            _deny("this dispatch authorization was already atomically consumed by a task claim")
    elif receipt.get("claim_call_id") not in calls:
        _deny("the historical atomic claim is absent from the child's own bounded Host record")


def read_authorized_binding(conn: sqlite3.Connection, *, run_id: str, task_id: str, subject: str,
        expected: dict[str, Any], observed: codex.NativeObservation, config_root: Path | None = None,
        issued_attempt: int | None = None) -> dict[str, Any] | None:
    """Called only after legacy identity checks pass and no plaintext dispatch matches."""
    if conn.in_transaction:
        raise RuntimeError("opaque dispatch verification performs Host reads outside the writer")
    try:
        authorization_id = _latest(conn, observed.native_agent_ref)
        if authorization_id is None:
            return None
        event, auth = _event(conn, authorization_id, AUTHORIZED)
        if event["run_id"] != run_id or auth["binding"]["contract"] != expected:
            return None
        state = _valid_authorization(conn, event, auth, issued=issued_attempt is not None)
        confirmations = list(conn.execute("SELECT event_id FROM events WHERE kind=? AND subject_id=? ORDER BY event_id LIMIT 2", (CONFIRMED, str(authorization_id))))
        if len(confirmations) != 1:
            return None
        confirmation_event, confirmation = _event(conn, confirmations[0][0], CONFIRMED)
        if (confirmation_event["run_id"] != run_id or confirmation["binding_sha256"] != ids.sha256_of_obj(auth["binding"])
                or clock.parse(confirmation_event["ts"]) >= clock.parse(auth["expires_at"])):
            return None
        _unchanged_prefix(_path(config_root, auth["binding"]["session_ref"]), confirmation["root_invocation"]["prefix"])
        action = _action(event, auth, config_root=config_root, before=confirmation["root_invocation"], confirmed=confirmation["action"])
        if issued_attempt is None:
            worker.checked_packet(observed, state["required"], state["document"])
        _claim_after(conn, event, auth, action, expected, observed, config_root, issued_attempt, confirmation_event["ts"])
        return {**action, "opaque_authorization_event_id": authorization_id,
                "opaque_confirmation_event_id": confirmation_event["event_id"],
                "evidence_mode": MODE, "authorization_source": "backend_native_root_dispatch_intent",
                "message_content_observed": False}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, GxError):
        return None
