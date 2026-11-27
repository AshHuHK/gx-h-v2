"""Read identity and settings that Codex wrote, without retaining message content.

Verified locally on Codex 0.169.0. A forked rollout contains its ancestors' metadata,
turns and tool results. Only the matching first session header and turns after that
thread's ``thread_settings_applied`` boundary belong to the requested worker.

Neither a completed turn nor an open/absent spawn edge proves that the native thread
has been destroyed. This reader deliberately supplies no native termination proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from ..domain import clock, ids
from .record import Record, Turn

HOST = "codex"
VERIFIED_VERSIONS = ("0.169.0",)
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_CONTROL_ROWS = 4096
MAX_METADATA_CONTEXTS = 512
_THREAD_REF = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_MODEL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
# Raw Host metadata vocabulary. Policy decides which of these a role may use;
# this does not add ultra to GX's request/effort contract.
_HOST_EFFORTS = frozenset(("low", "medium", "high", "xhigh", "max", "ultra"))
MODEL_AUTHORITY = "codex_host_applied_metadata"


@dataclass(frozen=True)
class NativeObservation:
    """A reader's product, never accepted as a client request body.

    Pool settlement obtains this by calling its own observer outside the write lock.
    ``terminal_confirmed`` is separate from readability and from a native status word.
    """

    host: str
    session_ref: str
    native_agent_ref: str
    observed_at: str
    readable: bool = False
    native_parent_ref: str | None = None
    native_depth: int | None = None
    agent_path: str | None = None
    host_version: str | None = None
    model: str | None = None
    effort: str | None = None
    source: str = "host_transcript"
    evidence_sha256: str = ""
    terminal_confirmed: bool = False
    terminal_reason: str | None = None
    reason: str | None = None
    current_turn_readable: bool = False
    current_turn: dict[str, Any] | None = None
    metadata_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def valid_thread_ref(value: Any) -> bool:
    return isinstance(value, str) and _THREAD_REF.fullmatch(value) is not None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _source(value: Any) -> Any:
    # Root rows use the bare source name; subagent rows use a JSON object.
    if value in ("cli", "vscode", "exec", "mcp"):
        return value
    return json.loads(value)


def _text(value: Any, limit: int = 512) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _identity(meta: dict[str, Any]) -> dict[str, Any] | None:
    thread_ref = meta.get("id")
    if not valid_thread_ref(thread_ref):
        return None
    spawn = _object(_object(_object(meta.get("source")).get("subagent")).get("thread_spawn"))
    parent = spawn.get("parent_thread_id")
    header_parent = meta.get("parent_thread_id") or meta.get("forked_from_id")
    if parent is not None:
        if not valid_thread_ref(parent) or (header_parent is not None and parent != header_parent):
            return None
        depth = spawn.get("depth")
        if type(depth) is not int or not 1 <= depth <= 64:
            return None
    else:
        # A header advertising a subagent without a readable spawn link is unknown.
        if meta.get("thread_source") == "subagent" or header_parent is not None:
            return None
        depth = 0
    return {"native_agent_ref": thread_ref, "native_parent_ref": parent,
            "native_depth": depth, "agent_path": _text(spawn.get("agent_path") or meta.get("agent_path")),
            "host_version": _text(meta.get("cli_version"), 64)}


def _time(value: Any) -> str | None:
    value = _text(value, 64)
    if value is None:
        return None
    try:
        return clock.parse(value).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError):
        return None


def _control(row: dict[str, Any], line: int) -> dict[str, Any] | None:
    """Project only identity/settings/lifecycle fields, never tool/message bodies."""
    payload = _object(row.get("payload"))
    kind = row.get("type")
    if kind == "turn_context":
        return {"kind": kind, "line": line, "at": _time(row.get("timestamp")),
                "turn_id": _text(payload.get("turn_id"), 64),
                "model": _text(payload.get("model"), 200), "effort": _text(payload.get("effort"), 32)}
    if kind != "event_msg":
        return None
    kind = payload.get("type")
    if kind == "thread_settings_applied":
        settings = _object(payload.get("thread_settings"))
        return {"kind": kind, "line": line, "at": _time(row.get("timestamp")),
                "thread_id": _text(payload.get("thread_id"), 64),
                "model": _text(settings.get("model"), 200),
                "effort": _text(settings.get("reasoning_effort"), 32)}
    if kind in ("task_started", "task_complete", "turn_aborted"):
        return {"kind": kind, "line": line, "at": _time(row.get("timestamp")),
                "turn_id": _text(payload.get("turn_id"), 64)}
    return None


def _control_error(row: dict[str, Any], target: str | None) -> str | None:
    """Own-boundary controls with missing identities cannot be ignored as foreign."""
    if row.get("metadata_error"):
        return row["metadata_error"]
    if row["kind"] == "turn_context":
        return None  # Each context has its own complete packet/error below.
    if row["kind"] == "thread_settings_applied":
        if not valid_thread_ref(row.get("thread_id")) or row["thread_id"] != target:
            return "own-boundary settings have no matching native thread identity"
        if (row.get("model") is None or _MODEL_REF.fullmatch(row["model"]) is None
                or row.get("effort") not in _HOST_EFFORTS):
            return "own applied settings have missing or unsupported model/effort metadata"
    elif not valid_thread_ref(row.get("turn_id")):
        return "own-boundary lifecycle metadata has no valid turn identity"
    if row.get("at") is None:
        return "own-boundary control metadata has no readable Host timestamp"
    return None


def _current_metadata(record: Record, controls: list[dict[str, Any]], *, tainted: bool) -> None:
    """Correlate the latest own context with own applied settings and lifecycle.

    Applied settings may persist across turns; a current context must explicitly
    agree with the latest applicable own setting. Missing new context/settings
    never borrows values from SQLite, a caller, an ancestor or an earlier context.
    """
    record.meta.update(current_turn=None, current_turn_readable=False)
    def unknown(reason: str) -> None:
        record.meta["metadata_reason"] = reason
    if record.host_version not in VERIFIED_VERSIONS:
        unknown("the current-turn field schema is not verified on this Host version")
        return
    if tainted:
        unknown("unreadable or inherited control data follows the own-thread boundary")
        return
    contexts = [row for row in controls if row["kind"] == "turn_context"]
    settings = [row for row in controls if row["kind"] == "thread_settings_applied"]
    starts = [row for row in controls if row["kind"] == "task_started"]
    if not contexts or not settings or not starts:
        unknown("own context, applied settings and task-start metadata are all required")
        return
    context = contexts[-1]
    turn_id = context["turn_id"]
    if not valid_thread_ref(turn_id):
        unknown("the own current context has no valid turn id")
        return
    if starts[-1]["turn_id"] != turn_id:
        unknown("a newer own task start has no matching current context")
        return
    own_starts = [row for row in starts if row["turn_id"] == turn_id]
    if len(own_starts) != 1 or own_starts[0]["line"] >= context["line"]:
        unknown("the current context has no unambiguous preceding own task start")
        return
    start = own_starts[0]
    ends = [row for row in controls if row["kind"] in ("task_complete", "turn_aborted")
            and row["turn_id"] == turn_id]
    if len(ends) > 1 or (ends and ends[0]["line"] <= context["line"]):
        unknown("the current context and its terminal metadata disagree")
        return
    end = ends[0] if ends else None
    for row in controls:
        if row["line"] >= start["line"] and (end is None or row["line"] <= end["line"]):
            problem = _control_error(row, record.agent_ref or record.session_ref)
            if problem is not None:
                unknown(problem)
                return
    applicable = [row for row in settings if end is None or row["line"] < end["line"]]
    if not applicable:
        unknown("the current turn has no own applied settings before its terminal record")
        return
    applied = applicable[-1]
    problem = _control_error(applied, record.agent_ref or record.session_ref)
    if problem is not None:
        unknown(problem)
        return
    if (context["model"] is None or _MODEL_REF.fullmatch(context["model"]) is None
            or context["effort"] not in _HOST_EFFORTS
            or applied["model"] != context["model"] or applied["effort"] != context["effort"]):
        unknown("own applied settings and current context model/effort are missing or contradictory")
        return
    if any(row["at"] is None for row in (start, context, applied) + ((end,) if end else ())):
        unknown("the current turn's Host timestamps are not readable")
        return
    if clock.parse(start["at"]) > clock.parse(context["at"]):
        unknown("the current context predates its task start")
        return
    if end and (clock.parse(end["at"]) < max(clock.parse(context["at"]), clock.parse(applied["at"]))):
        unknown("the current turn's terminal timestamp predates its context/settings")
        return
    packet = {"source": "host_applied_turn_context", "turn_id": turn_id,
              "model": context["model"], "effort": context["effort"],
              "status": "completed" if end and end["kind"] == "task_complete" else "aborted" if end else "active",
              "started_at": start["at"], "context_at": context["at"],
              "completed_at": end["at"] if end else None,
              "context_line": context["line"], "settings_line": applied["line"],
              "settings_at": applied["at"], "settings_model": applied["model"],
              "settings_effort": applied["effort"], "task_started_line": start["line"],
              "task_terminal_line": end["line"] if end else None,
              "source_boundary_line": record.meta.get("first_settings_boundary_line"),
              "rollout_prefix_sha256": record.sha256, "rollout_prefix_bytes": record.size,
              "model_authority": MODEL_AUTHORITY, "upstream_tier_independently_verified": False}
    packet["evidence_sha256"] = ids.sha256_of_obj({"host": HOST, "native_agent_ref": record.agent_ref or record.session_ref,
                                                  "host_version": record.host_version, "current_turn": packet})
    record.meta.update(current_turn=packet, current_turn_readable=True, metadata_reason=None,
                       model_authority=MODEL_AUTHORITY, upstream_tier_independently_verified=False)


def _all_metadata(record: Record, controls: list[dict[str, Any]], *, tainted: bool) -> None:
    """Retain every own context's corroboration or explicit failure for lease audits."""
    contexts = [row for row in controls if row["kind"] == "turn_context"]
    if len(controls) > MAX_CONTROL_ROWS or len(contexts) > MAX_METADATA_CONTEXTS:
        reason = "own metadata history exceeds the bounded packet audit limit"
        record.meta.update(current_turn=None, current_turn_readable=False, metadata_reason=reason,
                           turn_packets=[], turn_context_map=[],
                           turn_metadata_errors=[{"context_line": None, "context_at": None,
                                                  "turn_id": None, "reason": reason}])
        return
    packets: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    last_start: dict[str, Any] | None = None
    for row in controls:
        if row["kind"] == "task_started":
            last_start = row
        elif row["kind"] in ("task_complete", "turn_aborted") and (
            last_start is None or row.get("turn_id") != last_start.get("turn_id")
        ):
            row["metadata_error"] = "own terminal metadata has no matching preceding task-start identity"
    for index, context in enumerate(contexts):
        # A new task's start belongs to its new context, not to the prior epoch.
        boundaries = [row for row in controls if row["line"] > context["line"]
                      and ((row["kind"] == "turn_context")
                           or (row["kind"] == "task_started" and row["turn_id"] != context["turn_id"]))]
        boundary = boundaries[0] if boundaries else None
        epoch = [row for row in controls if boundary is None or row["line"] < boundary["line"]]
        _current_metadata(record, epoch, tainted=tainted)
        mapping.append({"context_line": context["line"], "context_at": context["at"], "turn_id": context["turn_id"],
                        "turn_index": context.get("turn_index")})
        if record.meta["current_turn_readable"]:
            packet = dict(record.meta["current_turn"])
            packet.update(turn_index=context.get("turn_index"), context_end_line=boundary["line"] if boundary else None,
                          context_end_at=boundary["at"] if boundary else None)
            packet.pop("evidence_sha256", None)
            packet["evidence_sha256"] = ids.sha256_of_obj(packet)
            packets.append(packet)
        else:
            errors.append({"context_line": context["line"], "context_at": context["at"], "turn_id": context["turn_id"],
                           "turn_index": context.get("turn_index"), "reason": record.meta["metadata_reason"]})
    for row in controls:
        problem = _control_error(row, record.agent_ref or record.session_ref)
        if problem is not None:
            # For a malformed non-context control, context_line/context_at identify
            # that exact metadata source row. The explicit kind prevents treating
            # it as a real turn_context or fabricating a context-to-Turn mapping.
            errors.append({"context_line": row["line"], "context_at": row["at"],
                           "turn_id": row.get("turn_id"), "turn_index": None,
                           "control_kind": row["kind"], "reason": problem})
    errors.sort(key=lambda row: row["context_line"])
    _current_metadata(record, controls, tainted=tainted)
    record.meta.update(turn_packets=packets, turn_metadata_errors=errors, turn_context_map=mapping)


def read_record(
    path: str | Path, *, verified_versions: Iterable[str] = VERIFIED_VERSIONS,
    session_ref: str | None = None, agent_ref: str | None = None,
    expected_project: str | Path | None = None,
) -> Record:
    """Read a bounded rollout and discard all text, prompts and arbitrary metadata.

    No fallback to the most recent metadata: a mismatching first header is refused.
    History ordinals are recorded for diagnosis, never interpreted as physical lines.
    """
    record = Record(host=HOST, path=str(path), sha256="", size=0,
                    session_ref=session_ref, agent_ref=agent_ref)
    target = agent_ref or session_ref
    if not valid_thread_ref(target):
        record.reason = "no valid native Codex thread id was supplied"
        return record
    digest = hashlib.sha256()
    own_boundary = False
    inherited_metadata = False
    initial_turns: list[Turn] = []
    controls: list[dict[str, Any]] = []
    initial_controls: list[dict[str, Any]] = []
    metadata_tainted = False
    identity: dict[str, Any] | None = None
    try:
        with Path(path).open("rb") as stream:
            line_number = 0
            while raw := stream.readline(MAX_ROW_BYTES + 1):
                line_number += 1
                record.size += len(raw)
                if len(raw) > MAX_ROW_BYTES or record.size > MAX_RECORD_BYTES:
                    record.reason = "the rollout exceeds the bounded reader's size limit"
                    return record
                digest.update(raw)
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    record.rows_unusable += 1
                    metadata_tainted = metadata_tainted or own_boundary
                    continue
                if not isinstance(row, dict):
                    record.rows_unusable += 1
                    metadata_tainted = metadata_tainted or own_boundary
                    continue
                payload = _object(row.get("payload"))
                kind = row.get("type")
                if identity is None:
                    # Host writes its own metadata first; ancestor headers follow it.
                    if line_number != 1 or kind != "session_meta" or payload.get("id") != target:
                        record.reason = "the first rollout header does not identify the requested native thread"
                        return record
                    identity = _identity(payload)
                    if identity is None:
                        record.reason = "the native parent/depth fields disagree or are not readable"
                        return record
                    version = identity["host_version"]
                    if version not in set(verified_versions):
                        record.reason = "this Codex record version has not been verified by the reader"
                        return record
                    record.versions[version] = 1
                    project = _text(payload.get("cwd"), 4096)
                    if expected_project is not None and (project is None or not Path(project).resolve().is_relative_to(Path(expected_project).resolve())):
                        record.reason = "the own rollout header is outside the requested Host project"
                        return record
                    record.meta = {**identity, "header_line": line_number,
                                   "history_start_ordinal": payload.get("subagent_history_start_ordinal"),
                                   "current_turn": None, "current_turn_readable": False,
                                   "header_project_sha256": ids.sha256_of_obj(project)}
                    record.rows_read += 1
                    continue
                if kind == "session_meta":
                    # Inherited metadata must never replace this worker's identity.
                    inherited_metadata = True
                    initial_turns.clear()
                    initial_controls.clear()
                    metadata_tainted = metadata_tainted or own_boundary
                    continue
                if (kind == "event_msg" and payload.get("type") == "thread_settings_applied"
                        and payload.get("thread_id") == target):
                    own_boundary = True
                    record.meta["settings_boundary_line"] = line_number
                    record.meta.setdefault("first_settings_boundary_line", line_number)
                    # Fresh threads without fork history can write their first own
                    # context immediately before settings_applied. The matching own
                    # boundary corroborates it; a fork marker/ancestor header forbids
                    # this path even if a model name happens to match.
                    if not inherited_metadata and record.meta.get("history_start_ordinal") is None:
                        offset = len(record.turns)
                        for control in initial_controls:
                            if control.get("turn_index") is not None:
                                control["turn_index"] += offset
                        record.turns.extend(initial_turns)
                        controls.extend(initial_controls)
                        record.rows_read += len(initial_turns)
                        if initial_turns:
                            record.meta["initial_context_corroborated_by_own_settings"] = True
                    initial_turns.clear()
                    initial_controls.clear()
                    controls.append(_control(row, line_number))
                    record.rows_read += 1
                    continue
                control = _control(row, line_number)
                if control is not None and kind != "turn_context":
                    if control["kind"] == "thread_settings_applied":
                        if own_boundary:
                            # After the authenticated own boundary, an unowned or
                            # malformed identity is an error, not ignorable history.
                            controls.append(control)
                        continue
                    if own_boundary:
                        controls.append(control)
                    elif not inherited_metadata and record.meta.get("history_start_ordinal") is None:
                        initial_controls.append(control)
                    continue
                if kind != "turn_context":
                    continue
                control["turn_index"] = None
                if own_boundary:
                    controls.append(control)
                elif not inherited_metadata and record.meta.get("history_start_ordinal") is None:
                    initial_controls.append(control)
                at, model = _text(row.get("timestamp"), 64), _text(payload.get("model"), 200)
                effort = _text(payload.get("effort"), 32)
                if at is None or model is None:
                    record.rows_unusable += 1
                    continue
                try:
                    clock.parse(at)
                except (TypeError, ValueError):
                    record.rows_unusable += 1
                    continue
                turn = Turn(at=at, model=model, effort=effort, message_id=_text(payload.get("turn_id"), 64))
                if own_boundary:
                    control["turn_index"] = len(record.turns)
                    record.turns.append(turn)
                    record.rows_read += 1
                elif not inherited_metadata and record.meta.get("history_start_ordinal") is None:
                    control["turn_index"] = len(initial_turns)
                    initial_turns.append(turn)
    except (OSError, ValueError) as exc:
        record.reason = f"the Codex rollout could not be read ({type(exc).__name__})"
        return record
    record.sha256 = digest.hexdigest()
    record.readable = identity is not None
    if not record.readable:
        record.reason = "the rollout contains no own session header"
    elif not own_boundary:
        record.meta["settings_boundary_missing"] = True
    if record.readable:
        _all_metadata(record, controls, tainted=metadata_tainted)
    return record


def _state_path(config_root: Path) -> Path:
    return config_root / "state_5.sqlite"


def read_worker(
    session_ref: str, agent_ref: str | None = None, *, config_root: str | Path | None = None,
) -> Record:
    """Safely read one native worker of the specified Host tree.

    Identifiers are internal locators, never authentication. The claim adapter must
    still bind its actual process and unfinished call to this packet's turn/context.
    SQLite is read-only with its WAL visible; no caller-supplied record path, model,
    effort, credential or fallback setting is consumed.
    """
    target = agent_ref or session_ref
    placeholder = Record(host=HOST, path="", sha256="", size=0, session_ref=session_ref, agent_ref=target)
    placeholder.meta.update(current_turn=None, current_turn_readable=False)
    def refuse(reason: str) -> Record:
        placeholder.reason = reason
        placeholder.meta["metadata_reason"] = reason
        return placeholder
    if not valid_thread_ref(session_ref) or not valid_thread_ref(target):
        return refuse("no valid native Codex session/worker ids were supplied")
    root = (Path(config_root) if config_root is not None else Path.home() / ".codex").resolve()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_state_path(root).as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(threads)")}
        cwd_column = "cwd" if "cwd" in columns else "NULL AS cwd"
        query = "SELECT id,rollout_path,source,cli_version,agent_path," + cwd_column + " FROM threads WHERE id=?"
        row = connection.execute(query, (target,)).fetchone()
        root_row = connection.execute(query, (session_ref,)).fetchone()
        if row is None or root_row is None:
            return refuse("the Host holds no matching root/worker record")
        row, root_row = dict(row), dict(root_row)
        if row["cli_version"] not in VERIFIED_VERSIONS or root_row["cli_version"] not in VERIFIED_VERSIONS:
            return refuse("this Codex Host database version is not verified by the reader")
        project, worker_project = _text(root_row["cwd"], 4096), _text(row["cwd"], 4096)
        project_verified = project is not None and worker_project is not None
        if not project_verified:
            return refuse("the Host database has no readable project attribution; no rollout was opened")
        if not Path(worker_project).resolve().is_relative_to(Path(project).resolve()):
            # Refuse before opening a worker record of a different project.
            return refuse("the worker's Host project is outside this native root's project")
        lineage: set[str] = set()
        cursor = target
        while cursor != session_ref:
            if cursor in lineage or len(lineage) >= 64:
                return refuse("the native parent chain is cyclic or too deep")
            lineage.add(cursor)
            entry = connection.execute("SELECT source FROM threads WHERE id=?", (cursor,)).fetchone()
            source = _source(entry["source"]) if entry else None
            cursor = _object(_object(_object(source).get("subagent")).get("thread_spawn")).get("parent_thread_id")
            if not valid_thread_ref(cursor):
                return refuse("the worker does not belong to this native session tree")
        edge = connection.execute("SELECT parent_thread_id,status FROM thread_spawn_edges WHERE child_thread_id=?", (target,)).fetchone()
        connection.execute("COMMIT")
        path = Path(row["rollout_path"]).resolve()
        if not path.is_relative_to(root / "sessions"):
            return refuse("the Host record points outside its sessions directory")
        record = read_record(path, session_ref=session_ref, agent_ref=target, expected_project=project)
        if not record.readable:
            return record
        if record.meta["header_project_sha256"] != ids.sha256_of_obj(worker_project):
            return refuse("the Host database and own header project attribution disagree")
        state_meta = _identity({"id": row["id"], "source": _source(row["source"]),
                                "cli_version": row["cli_version"], "agent_path": row["agent_path"]})
        if state_meta is None or any(state_meta[key] != record.meta[key] for key in (
            "native_agent_ref", "native_parent_ref", "native_depth", "host_version")):
            return refuse("the Host database and own rollout identity disagree")
        if edge is not None and edge["parent_thread_id"] != record.meta["native_parent_ref"]:
            return refuse("the Host spawn edge and own rollout parent disagree")
        identity = {"identity": state_meta, "rollout_sha256": record.sha256,
                    "settings_boundary_line": record.meta.get("settings_boundary_line"),
                    "edge_status": edge["status"] if edge else None,
                    "project_scope_sha256": ids.sha256_of_obj({"root": project, "worker": worker_project})}
        record.meta.update(native_identity_verified=True, native_identity_sha256=ids.sha256_of_obj(identity),
                           project_scope_verified=project_verified)
        packets = [*record.meta.get("turn_packets", [])]
        if record.meta.get("current_turn") is not None:
            packets.append(record.meta["current_turn"])
        for packet in packets:
            packet["native_identity_sha256"] = record.meta["native_identity_sha256"]
            packet.pop("evidence_sha256", None)
            packet["evidence_sha256"] = ids.sha256_of_obj(packet)
        return record
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return refuse(f"the Host record could not be verified ({type(exc).__name__})")
    finally:
        if connection is not None:
            connection.close()


def observe_native(
    *, host: str, session_ref: str, native_agent_ref: str, config_root: str | Path | None = None,
) -> NativeObservation:
    """Host identity plus its current applied metadata, never an upstream-tier attestation."""
    base = {"host": host, "session_ref": session_ref, "native_agent_ref": native_agent_ref,
            "observed_at": clock.now_iso()}
    if host != HOST:
        return NativeObservation(**base, reason="no verified native reader for this host")
    record = read_worker(session_ref, native_agent_ref, config_root=config_root)
    if not record.readable:
        return NativeObservation(**base, reason=record.reason, metadata_reason=record.meta.get("metadata_reason", record.reason))
    packet = record.meta.get("current_turn") if record.meta.get("current_turn_readable") else None
    return NativeObservation(
        **base, readable=True, native_parent_ref=record.meta["native_parent_ref"],
        native_depth=record.meta["native_depth"], agent_path=record.meta["agent_path"], host_version=record.host_version,
        model=packet["model"] if packet else None, effort=packet["effort"] if packet else None,
        evidence_sha256=record.meta["native_identity_sha256"], current_turn_readable=packet is not None,
        current_turn=packet, metadata_reason=record.meta.get("metadata_reason"), terminal_confirmed=False,
        reason="Host identity/applied metadata only; upstream tier and native termination are not independently attested",
    )
