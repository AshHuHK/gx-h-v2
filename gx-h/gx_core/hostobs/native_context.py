"""Trusted, in-process facts from one owned official App Server connection.

The wire tool arguments never construct this object. The router constructs it
from an outstanding ``item/tool/call`` and Host metadata on its own connection.
This is a procedural boundary in one trust domain, not same-UID isolation or an
attestation of the upstream model service. No private Host files are read here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
import uuid

from ..context import Principal, SCOPE_OPERATOR, SCOPE_WORKER
from ..domain import ids
from ..errors import ContentHashMismatch, OutcomeUnknown, ScopeDenied, SchemaInvalid

SOURCE = "codex_native_events_v1"
OBSERVATION_SOURCE = "host_protocol"
TRANSPORT = "native_app_server"
MODEL_AUTHORITY = "codex_host_applied_configuration"
SUPPORTED_HOST_VERSIONS = frozenset({"0.169.0"})


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise SchemaInvalid(f"official native {name} is missing or invalid")
    return value


def source_of_run(run: Mapping[str, Any]) -> str | None:
    try:
        raw = run["host_json"]
        host = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ContentHashMismatch("the stored Host binding is unreadable") from exc
    if not isinstance(host, Mapping):
        raise ContentHashMismatch("the stored Host binding is not an object")
    selected = host.get("policy_selection") or {}
    if not isinstance(selected, Mapping):
        raise ContentHashMismatch("the stored Host policy selection is invalid")
    return selected.get("source")


def is_native_run(conn: sqlite3.Connection, run_id: str) -> bool:
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return run is not None and source_of_run(run) == SOURCE


@dataclass(frozen=True)
class NativeCall:
    adapter_instance_id: str
    root_thread_id: str
    session_id: str
    thread_id: str
    parent_thread_id: str | None
    forked_from_id: str | None
    fresh_context: bool | None
    turn_id: str
    call_id: str
    tool_name: str
    arguments_sha256: str
    host_version: str
    model: str
    effort: str
    model_authority: str
    event_seq: int
    evidence_sha256: str
    _issuer: "NativeAuthority" = field(repr=False, compare=False)

    @property
    def principal(self) -> Principal:
        return self._issuer.principal_for(self.thread_id)

    @property
    def worker_ref(self) -> str:
        return f"codex:{self.session_id}:{self.thread_id}"

    def as_evidence(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__ if key != "_issuer"}


class NativeAuthority:
    """Connection-local metadata and pending callbacks; no task scheduler/TTL.

    A root is registered only from its official response. Descendants retain
    their real parent and need the same official sessionId. A fork does not
    receive root privileges. Subjects are generated here, never model arguments.
    """
    def __init__(self, *, project_ref: str, root_subject: str = "native-coordinator",
                 host_version: str = "0.169.0"):
        self.project_ref = _text(project_ref, "project")
        if self.project_ref == "*":
            raise SchemaInvalid("the native adapter requires one explicit project")
        self.root_subject = _text(root_subject, "root subject")
        if host_version not in SUPPORTED_HOST_VERSIONS:
            raise ScopeDenied("this official Host version has not been admitted")
        self.host_version = host_version
        self.instance_id = str(uuid.uuid4())
        self.root_thread_id: str | None = None
        self.session_id: str | None = None
        self.threads: dict[str, dict[str, Any]] = {}
        self.active_turns: dict[str, str] = {}
        self.epochs: dict[str, list[dict[str, Any]]] = {}
        self.sequence = 0
        self.live = True
        self.tool_profile = "native_core"
        self.artifact_workspace: Path | None = None
        self.recovery_only = False
        self._pending: dict[tuple[str, str, str], NativeCall] = {}
        self._seen: set[tuple[str, str, str]] = set()

    def advance(self, sequence: int) -> None:
        if type(sequence) is not int or sequence < self.sequence or sequence < 1:
            raise ScopeDenied("the native connection sequence regressed or is invalid")
        if not self.live:
            raise ScopeDenied("the native connection is no longer live")
        self.sequence = sequence

    def register_thread(self, thread: Mapping[str, Any], *, sequence: int,
                        root: bool = False) -> None:
        self.advance(sequence)
        native = _text(thread.get("id"), "thread id")
        session = _text(thread.get("sessionId"), "session id")
        version = _text(thread.get("cliVersion"), "version")
        if version != self.host_version:
            raise ScopeDenied("the official thread came from another Host version")
        parent = thread.get("parentThreadId")
        fork_known = "forkedFromId" in thread
        fork_from = thread.get("forkedFromId")
        if fork_known and fork_from is not None:
            _text(fork_from, "forked-from id")
        if root:
            if self.root_thread_id not in (None, native) or parent is not None or thread.get("forkedFromId") is not None or session != native:
                raise ScopeDenied("the adapter root must be the actual unforked session root")
            self.root_thread_id, self.session_id = native, session
        elif native != self.root_thread_id:
            _text(parent, "parent thread id")
            if parent == native or session != self.session_id:
                raise ScopeDenied("the official descendant has no valid parent/root session")
        elif session != self.session_id or parent is not None:
            raise ScopeDenied("the official root identity changed")
        old = self.threads.get(native)
        if old and (old["session_id"] != session or old["parent_thread_id"] != parent):
            raise ScopeDenied("the official native identity cannot be rebound")
        if old and old.get("fork_origin_known"):
            if fork_known and old["forked_from_id"] != fork_from:
                raise ScopeDenied("the official fork origin cannot change for one identity")
            fork_known, fork_from = True, old["forked_from_id"]
        self.threads[native] = {"session_id": session, "parent_thread_id": parent,
                                "fork_origin_known": fork_known, "forked_from_id": fork_from,
                                "model": thread.get("model"), "effort": thread.get("reasoningEffort"),
                                "sequence": sequence}
        self.epochs.setdefault(native, [])
        if native in self.active_turns and old != self.threads[native]:
            self._epoch(native, sequence, "metadata")

    def _epoch(self, native: str, sequence: int, kind: str) -> None:
        history = self.epochs[native]
        if len(history) >= 512:
            self.close()
            raise ScopeDenied("the bounded native settings history is exhausted; no tail truncation")
        facts = self.threads[native]
        history.append({"event_seq": sequence, "turn_id": self.active_turns.get(native),
                        "model": facts.get("model"), "effort": facts.get("effort"), "kind": kind})

    def apply_settings(self, thread_id: str, settings: Mapping[str, Any], *, sequence: int) -> None:
        self.advance(sequence)
        if thread_id not in self.threads:
            raise ScopeDenied("settings arrived for an unregistered native thread")
        self.threads[thread_id].update(model=settings.get("model"), effort=settings.get("effort"), sequence=sequence)
        if thread_id in self.active_turns:
            self._epoch(thread_id, sequence, "settings")

    def start_turn(self, thread_id: str, turn_id: str, *, sequence: int) -> None:
        self.advance(sequence)
        if thread_id not in self.threads:
            raise ScopeDenied("a turn arrived before its official thread metadata")
        self.active_turns[thread_id] = _text(turn_id, "turn id")
        self._epoch(thread_id, sequence, "turn_started")

    def admit_existing_turn(self, thread_id: str, turn_id: str, *, metadata_seq: int,
                            original_wire_start_seq: int) -> None:
        """Admit current live metadata without inventing a historical Host epoch.

        The original observed start predates a later official metadata response.
        This local epoch covers only the response and subsequent callbacks; an
        old callback/lease never gains historical coverage through this method.
        """
        if (type(original_wire_start_seq) is not int or type(metadata_seq) is not int
                or not 0 < original_wire_start_seq < metadata_seq
                or thread_id not in self.threads or thread_id in self.active_turns
                or self.epochs.get(thread_id)
                or self.threads[thread_id]["sequence"] != metadata_seq):
            raise ScopeDenied("current native turn admission has no unambiguous observed start and metadata boundary")
        self.advance(metadata_seq)
        self.active_turns[thread_id] = _text(turn_id, "existing current turn id")
        self._epoch(thread_id, metadata_seq, "metadata_resolved_current_turn")
        self.epochs[thread_id][-1].update(metadata_seq=metadata_seq,
            original_wire_start_seq=original_wire_start_seq, history_before_metadata_observed=False)

    def model_rerouted(self, thread_id: str, turn_id: str, to_model: str, *, sequence: int) -> None:
        self.advance(sequence)
        if self.active_turns.get(thread_id) != turn_id:
            raise ScopeDenied("a reroute has no corresponding observed native turn")
        self.threads[thread_id].update(model=_text(to_model, "rerouted model"), sequence=sequence)
        self._epoch(thread_id, sequence, "reroute")

    def finish_turn(self, thread_id: str, turn_id: str, *, sequence: int) -> None:
        self.advance(sequence)
        if self.active_turns.get(thread_id) != turn_id:
            raise ScopeDenied("the completed native turn differs from the active turn")
        self.active_turns.pop(thread_id)

    def principal_for(self, thread_id: str) -> Principal:
        if thread_id not in self.threads:
            raise ScopeDenied("the native thread has no backend principal")
        root = thread_id == self.root_thread_id
        return Principal(subject=self.root_subject if root else "native:" + thread_id,
                         scope=SCOPE_OPERATOR if root else SCOPE_WORKER,
                         project_refs=(self.project_ref,), source=OBSERVATION_SOURCE)

    def make_call(self, params: Mapping[str, Any], *, sequence: int) -> NativeCall:
        self.advance(sequence)
        native = _text(params.get("threadId"), "callback thread")
        turn = _text(params.get("turnId"), "callback turn")
        call_id = _text(params.get("callId"), "callback call id")
        tool = _text(params.get("tool"), "callback tool")
        if params.get("namespace") is not None:
            raise ScopeDenied("this adapter registers only direct existing GX tools")
        args = params.get("arguments")
        if not isinstance(args, Mapping):
            raise SchemaInvalid("GX native tool arguments must be an object")
        if native not in self.threads or self.active_turns.get(native) != turn:
            raise ScopeDenied("the callback has no observed active thread/turn")
        key = (native, turn, call_id)
        if key in self._seen:
            raise OutcomeUnknown("this native callback was already delivered; do not redispatch")
        metadata = self.threads[native]
        model, effort = _text(metadata.get("model"), "configured model"), _text(metadata.get("effort"), "configured effort")
        facts = dict(adapter_instance_id=self.instance_id, root_thread_id=self.root_thread_id,
                     session_id=self.session_id, thread_id=native, parent_thread_id=metadata["parent_thread_id"],
                     forked_from_id=metadata["forked_from_id"],
                     fresh_context=(metadata["forked_from_id"] is None) if metadata["fork_origin_known"] else None,
                     turn_id=turn, call_id=call_id, tool_name=tool, arguments_sha256=ids.sha256_of_obj(dict(args)),
                     host_version=self.host_version, model=model, effort=effort,
                     model_authority=MODEL_AUTHORITY, event_seq=sequence)
        call = NativeCall(**facts, evidence_sha256=ids.sha256_of_obj(facts), _issuer=self)
        self._pending[key] = call
        self._seen.add(key)
        return call

    def valid(self, call: NativeCall) -> bool:
        metadata = self.threads.get(call.thread_id) or {}
        return (self.live and self._pending.get((call.thread_id, call.turn_id, call.call_id)) is call
                and self.active_turns.get(call.thread_id) == call.turn_id
                and metadata.get("sequence", call.event_seq + 1) <= call.event_seq
                and metadata.get("model") == call.model and metadata.get("effort") == call.effort)

    def complete(self, call: NativeCall) -> None:
        self._pending.pop((call.thread_id, call.turn_id, call.call_id), None)

    def close(self) -> None:
        self.live = False
        self._pending.clear()


def qualification_onboarding(conn, call, capability_id, version, digest, *, before=None):
    """Resolve the onboarding run that reviewed the exact probe subject in this project."""
    row = conn.execute(
        "SELECT r.* FROM reviews v JOIN tasks t ON t.task_id=v.task_id JOIN runs r ON r.run_id=t.run_id "
        "WHERE v.subject_kind='asset' AND v.subject_id=? AND v.subject_version=? AND v.subject_sha256=? "
        "AND v.verdict='APPROVE' AND r.workflow_id='onboarding' AND r.project_ref=? "
        "AND (? IS NULL OR v.created_at<=?) ORDER BY v.created_at DESC,v.rowid DESC LIMIT 1",
        (capability_id, version, digest, call._issuer.project_ref, before, before)).fetchone()
    if row is None or source_of_run(row) != SOURCE:
        return None
    host = json.loads(row["host_json"])
    if (host.get("session_ref") == call.session_id
            and host["policy_selection"].get("native_agent_ref") == call.root_thread_id):
        return row["run_id"]
    return None


def _qualification_read(conn, call, run, tool, args):
    """A standalone ladder budget may carry the original Jobs of a native onboarding."""
    if (run["workflow_id"] is not None or run["objective_id"] is not None
            or not run["budget_ref"] or run["project_ref"] != call._issuer.project_ref
            or json.loads(run["host_json"] or "{}") or tool not in ("gx_job_status", "gx_job_wait", "gx_artifact_get")):
        return False
    artifact_ref = args.get("artifact_ref")
    job_id = args.get("job_id")
    if artifact_ref:
        from ..mcp.envelope import job_response_owner
        job_id = job_response_owner(conn, run["run_id"], artifact_ref)
    from ..context import Ctx
    root = call._issuer.principal_for(call.root_thread_id)
    actor = Ctx(conn=conn, principal=root, transport=TRANSPORT).actor
    query = (
        "SELECT j.* FROM jobs j JOIN events e ON e.run_id=j.run_id AND e.subject_id=j.job_id "
        "WHERE j.run_id=? AND e.kind='job.created' AND e.actor=? "
        "AND EXISTS (SELECT 1 FROM events c WHERE c.run_id=j.run_id AND c.kind='run.created' AND c.actor=e.actor) ")
    if job_id:
        query += "AND j.job_id=?"
    else:
        query += ("AND EXISTS (SELECT 1 FROM artifacts a WHERE a.run_id=j.run_id AND a.artifact_ref=? AND a.published=1 "
                  "AND (a.job_id=j.job_id OR a.artifact_ref=j.input_manifest_ref OR a.artifact_ref IN "
                  "(SELECT value FROM json_each(j.submit_intent_json,'$.input_roles'))))")
    job = conn.execute(query, (run["run_id"], actor, job_id or artifact_ref)).fetchone()
    if job is None:
        return False
    from ..jobs.service import PURPOSE_PROBE
    intent = json.loads(job["submit_intent_json"])
    version = f"{job['capability_id']}@{job['capability_version']}"
    digest = intent.get("manifest_sha256")
    if (intent.get("purpose") != PURPOSE_PROBE or intent.get("capability_version") != version or not digest
            or run["objective"] != f"probe ladder of {version} at sha256 {digest}"):
        return False
    owner = qualification_onboarding(conn, call, job["capability_id"], job["capability_version"],
                                     digest, before=run["created_at"])
    # Old carriers had empty inputs. New carriers name their actual onboarding explicitly.
    inputs = json.loads(run["inputs_json"] or "{}")
    return owner is not None and (not inputs or inputs.get("qualification_onboarding_run_id") == owner)


def authorize_call(conn: sqlite3.Connection, principal: Principal, tool: str,
                   clean: Mapping[str, Any], native_call: Any, *,
                   raw_arguments: Mapping[str, Any] | None = None) -> NativeCall | None:
    """The single API seam, after ordinary scope/schema/project authorization."""
    from ..authz import resolve_run_id
    run_id, _ = resolve_run_id(conn, clean)
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone() if run_id else None
    native_run = run is not None and source_of_run(run) == SOURCE
    if native_call is None:
        if native_run:
            raise ScopeDenied("this run requires its official native callback context")
        return None
    if type(native_call) is not NativeCall or not isinstance(native_call._issuer, NativeAuthority) or not native_call._issuer.valid(native_call):
        raise ScopeDenied("native authority must be an outstanding internal callback object")
    actual = native_call.principal
    if (principal.subject, principal.scope, principal.project_refs, principal.source) != (
            actual.subject, actual.scope, actual.project_refs, actual.source):
        raise ScopeDenied("the backend principal differs from the native connection mapping")
    if tool != native_call.tool_name or native_call.arguments_sha256 != ids.sha256_of_obj(dict(raw_arguments if raw_arguments is not None else clean)):
        raise ScopeDenied("the official callback tool/body differs from this dispatch")
    if native_call.model_authority != MODEL_AUTHORITY:
        raise ScopeDenied("the native model evidence has an unknown authority")
    if native_call._issuer.recovery_only:
        from .. import contracts
        if contracts.any_tool_spec(tool).get("mutating"):
            raise ScopeDenied("the resumed native session has unresolved outcomes; only read/reconcile is permitted")
    if tool.startswith(("gx_host_", "gx_codex_dispatch_")):
        raise ScopeDenied("legacy pool/opaque protocols are not native business tools")
    if run is not None:
        if not native_run:
            if _qualification_read(conn, native_call, run, tool, clean):
                return native_call
            raise ScopeDenied("a native client cannot adopt or mutate a legacy run")
        host = json.loads(run["host_json"])
        selected = host["policy_selection"]
        if not actual.may_access_project(run["project_ref"]):
            raise ScopeDenied("the callback root/session/project differs from the run")
        if host.get("session_ref") != native_call.session_id or selected.get("native_agent_ref") != native_call.root_thread_id:
            raise ScopeDenied("the callback root/session/project differs from the run")
        if native_call.thread_id == native_call.root_thread_id:
            from .. import contracts
            if contracts.any_tool_spec(tool).get("mutating"):
                from . import observation, policy
                document, _ = observation.policy_of_run(conn, run["run_id"])
                if (not policy.is_pinned_model(native_call.model, document)
                        or native_call.effort not in document["native_coordinator_observation"]["accepted_efforts"]):
                    raise ScopeDenied("the current official root configuration is outside this run's pinned coordinator policy")
    if tool == "gx_run_start" and native_call.thread_id != native_call.root_thread_id:
        raise ScopeDenied("only the registered native root starts a new run")
    return native_call


def call_for_ctx(ctx: Any) -> NativeCall:
    call = getattr(ctx, "native_call", None)
    if type(call) is not NativeCall or not call._issuer.valid(call):
        raise ScopeDenied("an outstanding official native callback is required")
    actual = call.principal
    if (ctx.principal.subject, ctx.principal.scope, ctx.principal.project_refs, ctx.principal.source) != (
            actual.subject, actual.scope, actual.project_refs, actual.source):
        raise ScopeDenied("the native callback does not belong to this backend principal")
    return call


def policy_for_new_run(ctx: Any, record: Mapping[str, Any]):
    """Pin only Host-applied configuration from the registered root callback."""
    from . import host_policy, policy
    from ..errors import CapabilityNotApproved
    call = call_for_ctx(ctx)
    if call.tool_name != "gx_run_start" or call.thread_id != call.root_thread_id:
        raise ScopeDenied("new native runs require the actual root run_start callback")
    declared = record.get("declared") or {}
    if declared.get("kind") not in (None, "codex") or record.get("session_ref") not in (None, call.session_id):
        raise SchemaInvalid("the declared Host differs from the official native root")
    document, digest = host_policy.native_codex_policy()
    pins = host_policy.native_role_pins(document)
    if (not policy.is_pinned_model(call.model, document)
            or call.effort not in document["native_coordinator_observation"]["accepted_efforts"]):
        raise CapabilityNotApproved("the official native root configuration is outside its installed policy")
    selected = {**dict(record), "kind": "codex", "kind_basis": "official_native_callback",
        "session_ref": call.session_id, "policy_sha256": digest,
        "observation_mode": "strict", "accepts": ["CONFORMS"], "understated": False,
        "policy_selection": {"source": SOURCE, "host": "codex", "session_ref": call.session_id,
            "native_agent_ref": call.root_thread_id, "adapter_instance_id": call.adapter_instance_id,
            "identity_sha256": call.evidence_sha256, "host_version": call.host_version,
            "invocation_call_id": call.call_id, "turn_id": call.turn_id,
            "tool_profile": call._issuer.tool_profile,
            "role_intent_pins": pins, "policy_sha256": digest,
            "policy_snapshot_sha256": ids.sha256_of_obj(document),
            "observed_model": call.model, "observed_effort": call.effort,
            "model_authority": MODEL_AUTHORITY, "upstream_tier_independently_verified": False}}
    return document, digest, selected
