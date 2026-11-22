"""Bind a first Codex task claim to a native Host dispatch, without spawning anything.

Verified against local Codex 0.169.0 records. A parent's own rollout contains a
spawn_agent call, SubAgentActivity with the same call id and actual child UUID,
and a native tool result. The child's database/header independently name its parent.
followup_task can carry a new contract to that same native child.

A dispatch is not authentication of whoever repeats its principal. Claim admission
also reads the Host's direct subprocess identity, and requires that child's own,
currently unfinished exec call to contain exactly the CLI command being executed.
Historical calls, inherited fork history, current-process environment variables and
request-supplied agent references establish no caller identity. This is evidence in
one local trust domain, like the Claude transcript tie, not a filesystem sandbox.
No prompts, commands or non-whitelisted environment values are persisted.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .. import contracts
from ..context import Ctx, SCOPE_HOST_ADAPTER, SCOPE_OPERATOR
from ..domain import ids
from ..errors import PermissionDenied, SchemaInvalid, ScopeDenied
from ..persistence import store
from . import codex

OPEN = "<GX_CODEX_TASK_CONTRACT>"
CLOSE = "</GX_CODEX_TASK_CONTRACT>"
AUDIT_EVENT = "host.codex_dispatch_verified"
CLAIM_EVENT = "host.codex_claim_verified"
_AUTHZ_KEY = "host.codex_dispatch.current_claim"
_ENV = (b"CODEX_THREAD_ID", b"CODEX_SESSION_ID")
_EXEC = re.compile(r"\btools\.exec_command\s*\(\s*\{")
_CMD = re.compile(r'(?:"cmd"|\bcmd)\s*:\s*(")')


def _contract(conn: sqlite3.Connection, run_id: str, task_id: str, subject: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT t.run_id,t.attempt,r.contract_sha256 FROM tasks t JOIN runs r"
                       " ON r.run_id=t.run_id WHERE t.task_id=?", (task_id,)).fetchone()
    if row is None or row["run_id"] != run_id or not isinstance(subject, str) or not subject.strip():
        return None
    if row["contract_sha256"] != contracts.contract_digest()["__combined__"]:
        return None
    assignment = conn.execute("SELECT subject,assignment_generation FROM task_assignments WHERE task_id=?",
                              (task_id,)).fetchone()
    if assignment is not None and assignment["subject"] != subject:
        return None
    body = {"schema_version": 1, "run_id": run_id, "task_id": task_id,
            "expected_attempt": int(row["attempt"]), "principal_subject": subject,
            "contract_sha256": row["contract_sha256"],
            "assignment_generation": int(assignment["assignment_generation"]) if assignment else None}
    run = conn.execute("SELECT agent_policy_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    snapshot = json.loads(run["agent_policy_json"] or "{}") if run else {}
    if "policy_host" in snapshot or "policy_scope" in snapshot:
        from . import codex_worker_observation
        task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        _, _, scope = codex_worker_observation.requirement(conn, task)
        body.update(scope)
    return body


def make_task_contract(conn: sqlite3.Connection, *, run_id: str, task_id: str,
                       principal_subject: str) -> str:
    """The exact marker a coordinator puts in a real native spawn/followup message.

    All pins, attempt and assignment generation come from the backend, not a template.
    Ordinary instructions can precede/follow the single marker. This starts no Host work.
    """
    body = _contract(conn, run_id, task_id, principal_subject)
    if body is None:
        raise SchemaInvalid("the task, assignment and current run pin must agree before native dispatch")
    return OPEN + ids.canonical_json(body) + CLOSE


def _message_contract(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, str) or message.count(OPEN) != 1 or message.count(CLOSE) != 1:
        return None
    body = message.split(OPEN, 1)[1].split(CLOSE, 1)[0]
    if len(body) > 8192:
        return None
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class Invocation:
    """Internal /proc reading, never an accepted request body or persisted command."""
    native_agent_ref: str
    session_ref: str
    command: str
    cwd: str
    shell_pid: int
    shell_start: str
    host_pid: int
    host_start: str


def _stat(proc: Path, pid: int) -> tuple[int, str]:
    fields = (proc / str(pid) / "stat").read_text()[:8192].rsplit(")", 1)[1].split()
    return int(fields[1]), fields[19]


def _native_env(path: Path) -> dict[bytes, str]:
    # Skip every other variable's value without decoding, retaining or logging it.
    found: dict[bytes, str] = {}
    with path.open("rb") as stream:
        key, value, in_value, wanted = bytearray(), bytearray(), False, False
        while byte := stream.read(1):
            if byte == b"\0":
                if wanted:
                    found[bytes(key)] = value.decode("ascii", "strict")
                key, value, in_value, wanted = bytearray(), bytearray(), False, False
            elif not in_value:
                if byte == b"=":
                    in_value, wanted = True, bytes(key) in _ENV
                elif len(key) < 256:
                    key.extend(byte)
            elif wanted:
                if len(value) >= 128:
                    return {}
                value.extend(byte)
    return found


def current_invocation(*, proc_root: Path = Path("/proc"), pid: int | None = None) -> Invocation | None:
    """Read the direct child of the nearest real Codex ancestor, not this process's env.

    Native IDs are the two non-secret Host-injected fields. Missing process metadata,
    a persistent/shared MCP transport, or an unsupported shell fails closed.
    """
    current = os.getpid() if pid is None else pid
    seen: set[int] = set()
    try:
        for _ in range(16):
            if current in seen or current < 1:
                return None
            seen.add(current)
            parent, started = _stat(proc_root, current)
            parent_exe = (proc_root / str(parent) / "exe").resolve(strict=True)
            if parent_exe.name == "codex":
                _, host_start = _stat(proc_root, parent)
                base = proc_root / str(current)
                marker = _native_env(base / "environ")
                child, session = marker.get(_ENV[0]), marker.get(_ENV[1])
                if not codex.valid_thread_ref(child) or not codex.valid_thread_ref(session):
                    return None
                raw = (base / "cmdline").read_bytes()
                if len(raw) > codex.MAX_ROW_BYTES:
                    return None
                argv = [part.decode("utf-8", "strict") for part in raw.rstrip(b"\0").split(b"\0")]
                if len(argv) != 3 or Path(argv[0]).name not in ("bash", "sh") or argv[1] not in ("-lc", "-c"):
                    return None
                return Invocation(child, session, argv[2], str((base / "cwd").resolve(strict=True)),
                                  current, started, parent, host_start)
            current = parent
    except (OSError, ValueError, IndexError, UnicodeError):
        return None
    return None


def _public_call(command: str, subject: str, tool: str) -> dict[str, Any] | None:
    """Parse a literal public CLI invocation without executing shell or Python text."""
    try:
        words = shlex.split(command)
        if words and words[0] == "env":
            words.pop(0)
        while words and re.fullmatch(r"(?:GX_DB|GX_PROJECTS|PYTHONDONTWRITEBYTECODE)=.*", words[0]):
            words.pop(0)
        if not words:
            return None
        program = words.pop(0)
        if Path(program).name.startswith("python"):
            if words and words[0] == "-B":
                words.pop(0)
            if words[:2] != ["-m", "gx_core.cli"]:
                return None
            del words[:2]
        elif Path(program).resolve() != (contracts.HARNESS_ROOT / "bin" / "gx").resolve():
            return None
        subjects, payloads, remainder = [], [], []
        while words:
            token = words.pop(0)
            if token in ("--as", "--json"):
                if not words:
                    return None
                (subjects if token == "--as" else payloads).append(words.pop(0))
            else:
                remainder.append(token)
        if (remainder != ["call", tool] or len(payloads) != 1 or len(subjects) != 1
                or subjects[0] not in (subject, "worker:" + subject)):
            return None
        payload = json.loads(payloads[0])
        return payload if isinstance(payload, dict) else None
    except (ValueError, OSError):
        return None


def _claim_command(command: str, expected: dict[str, Any], role: str) -> bool:
    payload = _public_call(command, expected["principal_subject"], "gx_task_claim")
    return bool(payload is not None and payload.get("task_id") == expected["task_id"]
                and type(payload.get("expected_attempt")) is int
                and payload["expected_attempt"] == expected["expected_attempt"]
                and payload.get("worker_role") == role)


def lease_command(command: str, *, subject: str, task_id: str, lease_id: str, operation: str) -> bool:
    """A current worker call must consume the exact lease under its authenticated subject."""
    if operation not in ("bind", "submit"):
        return False
    payload = _public_call(command, subject, "gx_task_" + operation)
    return bool(payload is not None and payload.get("lease_ref") == lease_id
                and (operation != "submit" or payload.get("task_id") == task_id))


def _own_rows(path: Path, native_ref: str, *, prefix: Mapping[str, Any] | None = None,
              initial_context_line: int | None = None) -> Iterator[tuple[int, dict[str, Any]]]:
    """Only rows after this thread's first own settings boundary; never inherited history."""
    total, boundary = 0, False
    limit = prefix.get("rollout_prefix_bytes") if prefix is not None else None
    if prefix is not None and (type(limit) is not int or not 0 < limit <= codex.MAX_RECORD_BYTES):
        raise ValueError("no bounded native packet prefix")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for number, raw in enumerate(iter(lambda: stream.readline(codex.MAX_ROW_BYTES + 1), b""), 1):
            if limit is not None and total == limit:
                break
            total += len(raw)
            digest.update(raw)
            if (total > codex.MAX_RECORD_BYTES or len(raw) > codex.MAX_ROW_BYTES
                    or (limit is not None and total > limit)):
                raise ValueError("bounded Host reader limit")
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                raise ValueError("the native causal record has an unrecognized row shape")
            payload = row["payload"]
            if (row.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied"
                    and payload.get("thread_id") == native_ref):
                boundary = True
            elif boundary or (number == initial_context_line and row.get("type") == "turn_context"):
                yield number, row
    if prefix is not None and (total != limit or digest.hexdigest() != prefix.get("rollout_prefix_sha256")):
        raise ValueError("the Host packet prefix changed while its causal call was read")


def _exec_commands(payload: dict[str, Any]) -> list[str]:
    if payload.get("type") == "function_call" and payload.get("name") in ("exec_command", "functions.exec_command"):
        try:
            args = json.loads(payload.get("arguments", "{}"))
        except (ValueError, TypeError, RecursionError):
            return []
        return [args["cmd"]] if isinstance(args, dict) and isinstance(args.get("cmd"), str) else []
    if payload.get("type") != "custom_tool_call" or payload.get("name") not in ("exec", "functions.exec"):
        return []
    text = payload.get("input", "")
    if not isinstance(text, str):
        return []
    commands = []
    for start in _EXEC.finditer(text):
        # Only JSON string literals for cmd are accepted; no evaluation of JavaScript.
        match = _CMD.search(text, start.end(), min(len(text), start.end() + 2048))
        if match:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.start(1):])
            except (ValueError, TypeError):
                return []
            if isinstance(value, str):
                commands.append(value)
    return commands


def _pending_claim(path: Path, native_ref: str, invocation: Invocation,
                   expected: dict[str, Any], role: str) -> str | None:
    pending: dict[str, list[str]] = {}
    for _, row in _own_rows(path, native_ref):
        p = row.get("payload") or {}
        if row.get("type") != "response_item":
            continue
        call = p.get("call_id")
        if not isinstance(call, str):
            continue
        if p.get("type") in ("function_call_output", "custom_tool_call_output"):
            pending.pop(call, None)
        elif p.get("type") in ("function_call", "custom_tool_call"):
            pending[call] = _exec_commands(p)
    matches = [call for call, commands in pending.items() if invocation.command in commands
               and _claim_command(invocation.command, expected, role)]
    return matches[0] if len(matches) == 1 else None


def verified_pending_invocation(actual: Invocation, packet: codex.NativeObservation, *,
                                config_root: Path | None = None) -> dict[str, Any] | None:
    """The current Host exec call and the actual applied packet must name one turn.

    Called only with backend reader products. The command is matched in memory, never
    returned or persisted. A completed/history turn or unknown metadata fails closed.
    No policy is selected here and no Host operation is executed.
    """
    if not isinstance(actual, Invocation) or not isinstance(packet, codex.NativeObservation):
        return None
    turn = getattr(packet, "current_turn", None)
    if (not packet.readable or not getattr(packet, "current_turn_readable", False)
            or not isinstance(turn, dict) or turn.get("status") != "active"
            or turn.get("source") != "host_applied_turn_context"
            or turn.get("model_authority") != "codex_host_applied_metadata"
            or turn.get("upstream_tier_independently_verified") is not False
            or not turn.get("evidence_sha256") or not turn.get("turn_id")
            or packet.native_agent_ref != actual.native_agent_ref or packet.session_ref != actual.session_ref):
        return None
    root = (config_root if config_root is not None else Path.home() / ".codex").resolve()
    try:
        with sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=1) as host:
            found = host.execute("SELECT rollout_path FROM threads WHERE id=?", (actual.native_agent_ref,)).fetchone()
        if found is None:
            return None
        path = Path(found[0]).resolve()
        if not path.is_relative_to(root / "sessions"):
            return None
        pending = {}
        current_context = None
        for line, row in _own_rows(path, actual.native_agent_ref, prefix=turn,
                initial_context_line=turn["context_line"] if type(turn.get("source_boundary_line")) is int
                    and turn["context_line"] < turn["source_boundary_line"] else None):
            payload = row.get("payload") or {}
            if row.get("type") == "turn_context":
                current_context = (line, payload.get("turn_id"))
            if row.get("type") != "response_item":
                continue
            call = payload.get("call_id")
            if not isinstance(call, str):
                continue
            if payload.get("type") in ("function_call_output", "custom_tool_call_output"):
                pending.pop(call, None)
            elif payload.get("type") in ("function_call", "custom_tool_call"):
                metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                pending[call] = {"commands": _exec_commands(payload), "turn_id": metadata.get("turn_id"),
                                 "line": line}
        if current_context != (turn.get("context_line"), turn["turn_id"]):
            return None
        matches = [(call, item) for call, item in pending.items() if actual.command in item["commands"]
                   and item["turn_id"] == turn["turn_id"]
                   and type(turn.get("context_line")) is int and item["line"] > turn["context_line"]]
        if len(matches) != 1:
            return None
        call, item = matches[0]
        return {"call_id": call, "turn_id": item["turn_id"], "call_line": item["line"],
                "native_agent_ref": actual.native_agent_ref, "session_ref": actual.session_ref,
                "packet_evidence_sha256": turn["evidence_sha256"], "source": "codex_native_invocation"}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None


def _dispatch(path: Path, parent: str, child: str, agent_path: str,
              expected: dict[str, Any]) -> dict[str, Any] | None:
    calls, activities, results = {}, {}, {}
    for number, row in _own_rows(path, parent):
        p = row.get("payload") or {}
        if row.get("type") == "response_item":
            call = p.get("call_id")
            if p.get("type") == "function_call" and p.get("name") in ("spawn_agent", "followup_task"):
                args = json.loads(p.get("arguments", "{}"))
                if not isinstance(args, dict):
                    continue
                if ids.canonical_json(_message_contract(args.get("message"))) == ids.canonical_json(expected):
                    calls[call] = (number, p["name"], args)
            elif p.get("type") == "function_call_output":
                results[call] = (number, p.get("output"))
        elif row.get("type") == "event_msg" and p.get("type") == "item_completed" and p.get("thread_id") == parent:
            item = p.get("item") or {}
            if not isinstance(item, dict):
                continue
            if (item.get("type") == "SubAgentActivity" and item.get("agent_thread_id") == child
                    and item.get("agent_path") == agent_path):
                activities[item.get("id")] = (number, item.get("kind"))
    matches = []
    for call, (line, tool, args) in calls.items():
        activity, output = activities.get(call), results.get(call)
        if not activity or not output or not line < activity[0] < output[0]:
            continue
        if tool == "spawn_agent":
            if activity[1] != "started" or agent_path.rsplit("/", 1)[-1] != args.get("task_name"):
                continue
            result = json.loads(output[1])
            if not isinstance(result, dict) or result.get("task_name") != agent_path:
                continue
        else:
            if (activity[1] != "interacted" or output[1] != ""
                    or args.get("target") not in (agent_path, agent_path.rsplit("/", 1)[-1])):
                continue
        matches.append({"dispatch_call_id": call, "dispatch_tool": tool,
                        "dispatch_line": line, "native_result_line": output[0]})
    return matches[-1] if matches else None


def _root_session(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute("SELECT c.session_ref,c.host,c.released_at,r.host_json FROM coordinator_leases c"
                       " JOIN runs r ON r.run_id=c.run_id WHERE c.run_id=?", (run_id,)).fetchone()
    if row is None or row["host"] != "codex" or row["released_at"] is not None:
        return None
    declared = json.loads(row["host_json"] or "{}")
    if declared.get("kind") != "codex" or declared.get("session_ref", row["session_ref"]) != row["session_ref"]:
        return None
    return row["session_ref"] if codex.valid_thread_ref(row["session_ref"]) else None


def _read_binding(conn: sqlite3.Connection, *, run_id: str, task_id: str, subject: str,
                  native_ref: str, config_root: Path | None = None,
                  issued_attempt: int | None = None) -> dict[str, Any] | None:
    expected, session = _contract(conn, run_id, task_id, subject), _root_session(conn, run_id)
    if expected is None or session is None or not codex.valid_thread_ref(native_ref) or native_ref == session:
        return None
    if issued_attempt is not None:
        expected["expected_attempt"] = issued_attempt - 1
    root = (config_root if config_root is not None else Path.home() / ".codex").resolve()
    observed = codex.observe_native(host="codex", session_ref=session, native_agent_ref=native_ref, config_root=root)
    if not observed.readable or not observed.native_parent_ref or not observed.agent_path:
        return None
    creator = codex.observe_native(host="codex", session_ref=session,
                                   native_agent_ref=observed.native_parent_ref, config_root=root)
    if not creator.readable:
        return None
    with sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=1) as host:
        paths = dict(host.execute("SELECT id,rollout_path FROM threads WHERE id IN (?,?)",
                                 (observed.native_parent_ref, native_ref)))
    parent_path, child_path = Path(paths[observed.native_parent_ref]).resolve(), Path(paths[native_ref]).resolve()
    if not all(path.is_relative_to(root / "sessions") for path in (parent_path, child_path)):
        return None
    cause = _dispatch(parent_path, observed.native_parent_ref, native_ref, observed.agent_path, expected)
    if cause is None:
        from .codex_opaque_dispatch import read_authorized_binding
        cause = read_authorized_binding(conn, run_id=run_id, task_id=task_id, subject=subject,
            expected=expected, observed=observed, config_root=config_root, issued_attempt=issued_attempt)
        if cause is None:
            return None
    return {**expected, **cause, "host": "codex", "session_ref": session, "native_agent_ref": native_ref,
            "native_parent_ref": observed.native_parent_ref, "native_depth": observed.native_depth,
            "observed_worker_ref": f"codex:{session}:{native_ref}", "source": "host_transcript",
            "identity_sha256": observed.evidence_sha256, "child_path": str(child_path)}


def prepare_claim(ctx: Ctx, *, task_id: str, run_id: str | None = None,
                  principal_subject: str | None = None, config_root: Path | None = None,
                  proc_root: Path = Path("/proc"), pid: int | None = None) -> str | None:
    """Read the native caller, applied packet and pinned task policy before the write lock."""
    from . import codex_worker_observation as worker
    ctx.authz.pop(_AUTHZ_KEY, None)
    task = ctx.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        return None
    run_id = run_id or task["run_id"]
    subject = principal_subject or ctx.principal.subject
    from .native_context import is_native_run
    if is_native_run(ctx.conn, task["run_id"]):
        from .native_observation import prepare_claim as prepare_official
        return prepare_official(ctx, task_id=task_id, run_id=run_id, principal_subject=subject)
    if run_id != task["run_id"] or subject != ctx.principal.subject:
        return None
    if not worker.scoped_run(ctx.conn, run_id):
        from .observation import run_host_kind
        if run_host_kind(ctx.conn, run_id) == "codex":
            from ..errors import ContentHashMismatch
            raise ContentHashMismatch("the legacy run has no independently pinned Codex policy; do not re-pin it automatically")
        return None
    if ctx.conn.in_transaction:
        raise RuntimeError("prepare native Codex claims outside the SQLite write transaction")
    run = ctx.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or not ctx.principal.may_access_project(run["project_ref"]):
        raise ScopeDenied("the native claim task is outside the caller's project scope")
    document, required, scope = worker.requirement(ctx.conn, task, current=True)
    before = worker.snapshot(ctx.conn, task_id)
    invocation = current_invocation(proc_root=proc_root, pid=pid)
    if invocation is None or Path(invocation.cwd) != contracts.HARNESS_ROOT:
        return None
    try:
        binding = _read_binding(ctx.conn, run_id=run_id, task_id=task_id, subject=subject,
                                native_ref=invocation.native_agent_ref, config_root=config_root)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None
    if binding is None or invocation.session_ref != binding["session_ref"]:
        return None
    packet = codex.observe_native(host="codex", session_ref=invocation.session_ref,
                                  native_agent_ref=invocation.native_agent_ref, config_root=config_root)
    try:
        applied = worker.checked_packet(packet, required, document)
    except worker.ModelPolicyViolation as exc:
        raise ScopeDenied(exc.message, details=exc.details) from exc
    pending = verified_pending_invocation(invocation, packet, config_root=config_root)
    if pending is None or not _claim_command(invocation.command, binding, task["role"]):
        return None
    try:
        if (_stat(proc_root, invocation.shell_pid)[1] != invocation.shell_start
                or _stat(proc_root, invocation.host_pid)[1] != invocation.host_start):
            return None
    except (OSError, ValueError, IndexError):
        return None
    if worker.snapshot(ctx.conn, task_id) != before:
        from ..errors import StalePlan
        raise StalePlan("the task moved during native Codex claim preparation")
    proof = {key: value for key, value in binding.items() if key != "child_path"}
    proof.update(scope)
    proof.update(claim_call_id=pending["call_id"], claim_turn_id=pending["turn_id"],
                 claim_call_line=pending["call_line"], applied_packet=applied,
                 required=dict(required), request_id=ctx.request_id,
                 model_authority=worker.AUTHORITY, upstream_tier_independently_verified=False)
    ctx.authz[_AUTHZ_KEY] = _VerifiedClaim(ids.canonical_json(proof), ids.canonical_json(before))
    return binding["observed_worker_ref"]


def fence_claim(ctx: Ctx, *, task_id: str | None = None) -> None:
    """Fence the prepared read at write-transaction entry, before backend writes."""
    from . import native_context, native_observation
    task = ctx.conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone() if task_id else None
    if ctx.authz.get(native_observation.CLAIM_KEY) is not None or (task and native_context.is_native_run(ctx.conn, task["run_id"])):
        return native_observation.fence_claim(ctx, task_id=task_id)
    from . import codex_worker_observation as worker
    from ..errors import StalePlan
    product = ctx.authz.get(_AUTHZ_KEY)
    if not isinstance(product, _VerifiedClaim):
        if task_id is not None:
            task = ctx.conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is not None and worker.scoped_run(ctx.conn, task["run_id"]):
                raise ScopeDenied("no current native Codex invocation proves this task's caller and applied model/effort")
        return
    if not ctx.conn.in_transaction:
        raise RuntimeError("fence native Codex claims at transaction entry")
    expected = json.loads(product.snapshot_json)
    if worker.snapshot(ctx.conn, expected["task"]["task_id"]) != expected:
        raise StalePlan("the task/policy/assignment/event cursor moved after native claim preparation")
    if product.evidence.get("evidence_mode") == "backend_authorized_opaque_followup":
        from .codex_opaque_dispatch import fence_claim_receipt
        fence_claim_receipt(ctx, product.evidence)
    ctx.authz[_AUTHZ_KEY] = _VerifiedClaim(product.evidence_json, product.snapshot_json, True)


def verified_worker_for_claim(ctx: Ctx, *, run_id: str, task_id: str, principal_subject: str,
                              config_root: Path | None = None, proc_root: Path = Path("/proc"),
                              pid: int | None = None) -> str | None:
    """Consume only transaction-fenced internal evidence; never read Host records here."""
    from .native_context import is_native_run
    if is_native_run(ctx.conn, run_id):
        from .native_observation import worker_for_claim
        return worker_for_claim(ctx, run_id=run_id, task_id=task_id, principal_subject=principal_subject)
    if not ctx.conn.in_transaction:
        return prepare_claim(ctx, run_id=run_id, task_id=task_id, principal_subject=principal_subject,
                             config_root=config_root, proc_root=proc_root, pid=pid)
    product = ctx.authz.get(_AUTHZ_KEY)
    if not isinstance(product, _VerifiedClaim) or not product.fenced:
        return None
    proof = product.evidence
    current = _contract(ctx.conn, run_id, task_id, principal_subject)
    if (current is None or ctx.principal.subject != principal_subject or proof.get("request_id") != ctx.request_id
            or any(proof.get(key) != value for key, value in current.items())):
        return None
    return proof["observed_worker_ref"]


@dataclass(frozen=True)
class _VerifiedClaim:
    evidence_json: str
    snapshot_json: str
    fenced: bool = False

    @property
    def evidence(self) -> dict[str, Any]:
        return json.loads(self.evidence_json)


def record_claim_binding(ctx: Ctx, *, lease_id: str) -> int | None:
    """After lease issue and task transition, in that same transaction, preserve its tie.

    Consumes only the verifier's internal product in this request context. An adapter
    audit or a request body cannot supply it. No Host I/O or new observer is run here.
    """
    from . import native_context, native_observation
    owner = ctx.conn.execute("SELECT t.run_id FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?", (lease_id,)).fetchone()
    if owner and native_context.is_native_run(ctx.conn, owner["run_id"]):
        return native_observation.record_claim(ctx, lease_id=lease_id)
    product = ctx.authz.pop(_AUTHZ_KEY, None)
    if not isinstance(product, _VerifiedClaim):
        return None
    if not ctx.conn.in_transaction or not product.fenced:
        raise RuntimeError("a native claim binding must commit with its transaction-fenced new task lease")
    proof = product.evidence
    lease = ctx.conn.execute("SELECT l.*,t.lease_id AS current_lease,t.attempt AS current_attempt"
        " FROM leases l JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?", (lease_id,)).fetchone()
    if (lease is None or lease["current_lease"] != lease_id or lease["released_at"] is not None
            or lease["principal"] != ctx.principal.subject or lease["principal"] != proof["principal_subject"]
            or lease["task_id"] != proof["task_id"] or lease["current_attempt"] != lease["attempt"]
            or int(lease["attempt"]) != proof["expected_attempt"] + 1):
        raise SchemaInvalid("the verified native claim does not identify the newly issued lease")
    current = _contract(ctx.conn, proof["run_id"], proof["task_id"], proof["principal_subject"])
    if current is None or any(current[key] != proof[key] for key in current if key != "expected_attempt"):
        raise SchemaInvalid("the task assignment or pin moved after native claim verification")
    payload = {**proof, "lease_id": lease_id, "attempt": int(lease["attempt"])}
    payload["evidence_sha256"] = ids.sha256_of_obj(payload)
    return store.append_event(ctx.conn, kind=CLAIM_EVENT, actor=ctx.actor, run_id=proof["run_id"],
        subject_kind="task", subject_id=proof["task_id"], evidence_level="EXECUTION_OBSERVED", payload=payload,
        reason="current native Codex CLI claim verified before this lease was issued")


def verified_lease_binding(conn: sqlite3.Connection, *, lease_id: str,
                           native_agent_ref: str, config_root: Path | None = None) -> dict[str, Any] | None:
    """Read the backend's atomic claim receipt and re-read the Host dispatch behind it.

    For the pool's post-claim bind. Never promotes a dispatch audit into a lease tie.
    Call outside the pool write lock, then fence its lease and generation at commit.
    """
    if conn.in_transaction:
        raise RuntimeError("read Codex lease bindings outside SQLite write transactions")
    try:
        lease = conn.execute("SELECT l.*,t.run_id,t.lease_id AS current_lease FROM leases l"
                             " JOIN tasks t ON t.task_id=l.task_id WHERE l.lease_id=?", (lease_id,)).fetchone()
        if lease is None or lease["current_lease"] != lease_id or lease["released_at"] is not None:
            return None
        matches = []
        for event in conn.execute("SELECT event_id,payload_json FROM events WHERE kind=? AND subject_id=?"
                                   " ORDER BY event_id", (CLAIM_EVENT, lease["task_id"])):
            payload = json.loads(event["payload_json"])
            if payload.get("lease_id") == lease_id:
                matches.append((event["event_id"], payload))
        if len(matches) != 1:
            return None
        event_id, proof = matches[0]
        digest = proof.pop("evidence_sha256", None)
        if (digest != ids.sha256_of_obj(proof) or proof.get("native_agent_ref") != native_agent_ref
                or proof.get("principal_subject") != lease["principal"] or proof.get("run_id") != lease["run_id"]
                or proof.get("attempt") != int(lease["attempt"])):
            return None
        binding = _read_binding(conn, run_id=lease["run_id"], task_id=lease["task_id"], subject=lease["principal"],
            native_ref=native_agent_ref, config_root=config_root, issued_attempt=int(lease["attempt"]))
        if binding is None or any(binding[key] != proof.get(key) for key in binding if key not in (
                "expected_attempt", "identity_sha256", "child_path")):
            return None
        from . import codex_worker_observation as worker
        applied = {}
        if worker.scoped_run(conn, lease["run_id"]):
            audit = worker.binding_observation(conn, lease_id=lease_id, proof=proof,
                                               claim_event_id=event_id, config_root=config_root)
            if audit is None:
                return None
            applied = {"policy_verdict": "CONFORMS", "model_authority": worker.AUTHORITY,
                       "upstream_tier_independently_verified": False, "model_observation": audit}
        return {key: value for key, value in {**binding, "claim_event_id": event_id,
                "claim_call_id": proof["claim_call_id"], "lease_id": lease_id,
                **applied}.items() if key != "child_path"}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None


def record_verified_dispatch(ctx: Ctx, *, run_id: str, task_id: str, principal_subject: str,
                             native_agent_ref: str, config_root: Path | None = None) -> dict[str, Any]:
    """Optional adapter audit: a native id is a locator, never accepted as proof.

    A record is not a bearer credential and cannot replace current-claim verification.
    All Host I/O precedes the transaction; pins/attempt/assignment are fenced again inside.
    """
    if ctx.principal.scope not in (SCOPE_HOST_ADAPTER, SCOPE_OPERATOR):
        raise PermissionDenied("only the Host adapter or operator may record native dispatch observations")
    if ctx.conn.in_transaction:
        raise RuntimeError("read native dispatch evidence before taking a write transaction")
    run = ctx.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or not ctx.principal.may_access_project(run["project_ref"]):
        raise ScopeDenied("the dispatch run is outside the caller's project scope")
    try:
        binding = _read_binding(ctx.conn, run_id=run_id, task_id=task_id, subject=principal_subject,
                                native_ref=native_agent_ref, config_root=config_root)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        binding = None
    if binding is None:
        raise SchemaInvalid("the native Host records do not bear out this task dispatch")
    binding.pop("child_path")
    binding["evidence_sha256"] = ids.sha256_of_obj(binding)
    with store.transaction(ctx.conn) as txn:
        current = _contract(txn, run_id, task_id, principal_subject)
        if current is None or any(binding[key] != value for key, value in current.items()):
            raise SchemaInvalid("the task changed while the native dispatch was being verified")
        event = store.append_event(txn, kind=AUDIT_EVENT, actor=ctx.actor, run_id=run_id,
                                   subject_kind="task", subject_id=task_id,
                                   evidence_level="EXECUTION_OBSERVED", payload=binding,
                                   reason="native Codex dispatch call and actual Host result verified")
    return {**binding, "event_id": event}
