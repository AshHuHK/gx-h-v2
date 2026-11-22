"""Reader of the record Claude Code keeps of every turn.

Claude Code writes one JSON line per event of a session under
``<config>/projects/<project>/<session>.jsonl`` and, for every agent the session spawned,
``<session>/subagents/agent-<id>.jsonl`` with an ``agent-<id>.meta.json`` beside it. Each
line of a model's response names the model that served it, the model that was asked for and
the effort the request was sent at; a refusal that moved the session to another model is a
``system`` line of its own.

Three rules shape this module.

**It keeps identity and settings fields only.** :func:`project_row` is the one place a
field leaves a raw line, and it copies short scalars from fixed lists of keys. Message
content, tool input, tool output, prompts, titles and descriptions are never copied, so they
cannot reach a row, an event, an artifact or a log. The rest of the reader consumes only
what :func:`project_row` returned, which is also why it gives the same answer on a stored
projection as on the original file.

**It is tied to the host versions it was verified on.** The field names are internal to
Claude Code. They were observed on 2.1.322 (5,999 response lines of 18 transcripts, each
joined to the request the host logged for it: the model and the effort on the line equalled
the model and the effort that were sent, without exception). On a line of any other version
the record is ``readable = False`` and nothing is concluded from it.

**It accepts a path only inside the host's own records directory.** A hook hands the
backend the path of a worker's transcript; a path that resolves anywhere else is not read.

The files belong to the same user as the backend, so this is evidence the host recorded in
one trust domain: stronger than a worker's report, and not tamper-proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from ..domain import clock
from .record import DEVIATION_FALLBACK, DEVIATION_REFUSAL, Announcement, Deviation, Record, Turn

HOST = "claude_code"

@dataclass(frozen=True)
class Fields:
    """The keys that may leave a line of a host record, and the longest value one may hold.

    They are not written in this module. ``config/role_policy.json`` lists them under
    ``observation.transcript_reader.claude_code`` - the policy says what may be read about
    a worker, and the reader copies exactly those keys.
    """

    row: tuple[str, ...]
    message: tuple[str, ...]
    stop_detail: tuple[str, ...]
    usage: tuple[str, ...]
    request: tuple[str, ...]
    request_param: tuple[str, ...]
    meta: tuple[str, ...]
    max_value_chars: int
    #: Of a line on which the host announces the session's model (``type: attachment`` whose
    #: attachment is of type ``model``): the keys of the attachment and of its ``identity``.
    #: Empty unless the policy lists them, and then no such line is read at all.
    attachment: tuple[str, ...] = ()
    attachment_identity: tuple[str, ...] = ()
    #: The one key of an agent's meta that is looked at for a worker subject, and the pattern a
    #: whole value must match to be kept (the policy's ``meta_subject_field`` and
    #: ``meta_subject_pattern``). Empty unless the policy has the rule, and then the key is
    #: never looked at. The host writes the name a launcher gave an agent under this key
    #: (probe E, 2.1.322); for any other agent it holds free text, which never passes.
    meta_subject_field: str = ""
    meta_subject_pattern: str = ""
    #: The keys of the host's record of a Workflow run (``workflow_run_fields``).
    workflow_run: tuple[str, ...] = ()

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "Fields":
        return cls(
            row=tuple(spec["row_fields"]), message=tuple(spec["message_fields"]),
            stop_detail=tuple(spec["stop_detail_fields"]), usage=tuple(spec["usage_fields"]),
            request=tuple(spec["request_fields"]), request_param=tuple(spec["request_param_fields"]),
            meta=tuple(spec["meta_fields"]), max_value_chars=int(spec["max_value_chars"]),
            attachment=tuple(spec.get("attachment_fields") or ()),
            attachment_identity=tuple(spec.get("attachment_identity_fields") or ()),
            meta_subject_field=str(spec.get("meta_subject_field") or ""),
            meta_subject_pattern=str(spec.get("meta_subject_pattern") or ""),
            workflow_run=tuple(spec.get("workflow_run_fields") or ()),
        )

    @property
    def reads_subject(self) -> bool:
        return bool(self.meta_subject_field and self.meta_subject_pattern)

    @property
    def meta_keys(self) -> tuple[str, ...]:
        """Every key a projected meta may hold: the listed fields, and the subject's key where the rule exists."""
        return (*self.meta, self.meta_subject_field) if self.reads_subject else self.meta

    @property
    def reads_announcements(self) -> bool:
        return "type" in self.attachment and "modelId" in self.attachment_identity

    def paths(self) -> frozenset[str]:
        """Every key path a projected line may hold."""
        return frozenset(
            list(self.row) + list(self.request)
            + [f"message.{key}" for key in self.message]
            + [f"message.stop_details.{key}" for key in self.stop_detail]
            + [f"message.usage.{key}" for key in self.usage]
            + [f"params.{key}" for key in self.request_param]
            + [f"attachment.{key}" for key in self.attachment]
            + [f"attachment.identity.{key}" for key in self.attachment_identity]
        )


def fields() -> Fields:
    """The field lists of the agent policy as it is on disk now."""
    from . import policy

    spec = policy.reader_spec(HOST)
    if spec is None:
        raise RuntimeError("the agent policy describes no transcript reader for claude_code")
    return Fields.from_spec(spec)


#: The four token counts a turn carries, in the order ``Turn.tokens`` holds them.
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
)

ROW_RESPONSE = "assistant"
ROW_REQUEST = "api-request"
ROW_SYSTEM = "system"
ROW_ATTACHMENT = "attachment"
#: The attachment on which the host tells a session which model it now runs on. It is written
#: at a prompt boundary: at the start, after a compaction, and after any change of model.
ATTACHMENT_MODEL = "model"
SUBTYPE_FALLBACK = "model_refusal_fallback"
STOP_REFUSAL = "refusal"
#: A response line the host itself wrote in place of a model's, for instance when a request
#: could not be served (observed on 2.1.322: two in each builder's record at the usage-limit
#: stop of 2026-11-12). Skipped rather than counted as a turn of a model called "<synthetic>".
SYNTHETIC_MODEL = "<synthetic>"

#: Key under which a projected line lists the *names* of listed fields that were left out
#: because their value was not a short scalar. Never observed on a verified version.
DROPPED = "_dropped"

_SESSION_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


# --------------------------------------------------------------------------- projection

def _scalars(source: Any, keys: Iterable[str], limit: int, dropped: list[str] | None = None,
             prefix: str = "") -> dict[str, Any]:
    """The listed keys of ``source`` whose values are short scalars. Nothing else.

    A listed key that holds anything else - a structure, or a string too long to be a name -
    is left out and its *name* is appended to ``dropped``, so the reader can tell a field
    that is absent from one that arrived in a shape it was not verified on.
    """
    if not isinstance(source, Mapping):
        return {}
    kept: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if value is None or isinstance(value, (bool, int)):
            kept[key] = value
        elif isinstance(value, str) and len(value) <= limit:
            kept[key] = value
        elif dropped is not None:
            dropped.append(prefix + key)
    return kept


def _dotted(source: Any, path: str) -> tuple[Any, str]:
    """``(the mapping that holds the last key of a dotted path, that key)``."""
    *parents, leaf = path.split(".")
    for key in parents:
        source = source.get(key) if isinstance(source, Mapping) else None
    return source, leaf


def project_row(row: Any, *, requests: bool = False, allowed: Fields | None = None) -> dict[str, Any] | None:
    """The identity and settings fields of one transcript line, or None if it has none.

    Four kinds of line are kept: a response (``type: assistant``), a line on which the host
    noted a refusal or a fallback, a line on which it announced the session's model (only
    when the policy lists the two keys that takes), and - only with ``requests=True`` - the
    host's log of a request it sent. Every other line, which is where prompts, tool results
    and every other attachment live, yields None without being looked into.
    """
    if not isinstance(row, Mapping):
        return None
    allowed = allowed if allowed is not None else fields()
    limit = allowed.max_value_chars
    kind = row.get("type")
    dropped: list[str] = []
    if kind == ROW_RESPONSE:
        projected = _scalars(row, allowed.row, limit, dropped)
        message = row.get("message")
        if isinstance(message, Mapping):
            kept = _scalars(message, allowed.message, limit, dropped, "message.")
            details = _scalars(message.get("stop_details"), allowed.stop_detail, limit, dropped,
                               "message.stop_details.")
            if details:
                kept["stop_details"] = details
            usage = {
                key: value for key, value in _scalars(message.get("usage"), allowed.usage, limit).items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            if usage:
                kept["usage"] = usage
            projected["message"] = kept
    elif kind == ROW_REQUEST:
        if not requests:
            return None
        projected = _scalars(row, allowed.request, limit, dropped)
        params = row.get("params")
        if isinstance(params, Mapping):
            kept: dict[str, Any] = {}
            for path in allowed.request_param:
                holder, leaf = _dotted(params, path)
                found = _scalars(holder, (leaf,), limit, dropped, f"params.{path[:-len(leaf)]}")
                if leaf in found:
                    target = kept
                    for key in path.split(".")[:-1]:
                        target = target.setdefault(key, {})
                    target[leaf] = found[leaf]
            projected["params"] = kept
    elif row.get("fallbackModel") is not None or row.get("apiRefusalCategory") is not None or (
        kind == ROW_SYSTEM and row.get("subtype") == SUBTYPE_FALLBACK
    ):
        projected = _scalars(row, allowed.row, limit, dropped)
    elif kind == ROW_ATTACHMENT:
        attachment = row.get("attachment")
        if not allowed.reads_announcements or not isinstance(attachment, Mapping) \
                or attachment.get("type") != ATTACHMENT_MODEL:
            return None
        projected = _scalars(row, allowed.row, limit, dropped)
        kept = _scalars(attachment, allowed.attachment, limit, dropped, "attachment.")
        identity = _scalars(attachment.get("identity"), allowed.attachment_identity, limit, dropped,
                            "attachment.identity.")
        if identity:
            kept["identity"] = identity
        projected["attachment"] = kept
    else:
        return None
    marked = row.get(DROPPED)
    if isinstance(marked, list):  # a line that was projected before keeps what it recorded
        paths = allowed.paths()
        dropped.extend(name for name in marked if isinstance(name, str) and name in paths)
    if dropped:
        projected[DROPPED] = sorted(set(dropped))
    return projected


def project_meta(meta: Any, *, allowed: Fields | None = None) -> dict[str, Any]:
    """The identity and settings fields of an agent's ``meta.json``, and the worker subject it names, if it names one."""
    allowed = allowed if allowed is not None else fields()
    projected = _scalars(meta, allowed.meta, allowed.max_value_chars)
    subject = meta_subject(meta, allowed)
    if subject is not None:
        projected[allowed.meta_subject_field] = subject
    return projected


def meta_subject(meta: Any, allowed: Fields) -> str | None:
    """The worker subject an agent's meta names, or None.

    Design r2.2a section 8.2: the host's own record says which hand-out an agent works under,
    because the launcher gave the agent its subject as its label (a Workflow agent) or as its
    description (an Agent-tool agent), and the host writes that under one meta key. The key
    can hold free text for any other agent, so it is looked at for one thing only: a whole
    value that is a subject (a full match of the policy's pattern) is kept. Any other value is
    dropped at this comparison: it is not returned, stored, logged or put in an error.
    """
    if not allowed.reads_subject or not isinstance(meta, Mapping):
        return None
    value = meta.get(allowed.meta_subject_field)
    if not isinstance(value, str) or len(value) > allowed.max_value_chars:
        return None
    try:
        # fullmatch, never match: with match a value that ends in a newline passes "^...$".
        return value if re.fullmatch(allowed.meta_subject_pattern, value) else None
    except re.error:
        return None


def iter_projected(raw: bytes, *, requests: bool = False,
                   allowed: Fields | None = None) -> Iterator[tuple[int, dict[str, Any] | None, bool]]:
    """``(line number, projected row or None, parsed)`` for every non-empty line of a file."""
    allowed = allowed if allowed is not None else fields()
    for number, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            yield number, None, False
            continue
        yield number, project_row(row, requests=requests, allowed=allowed), True


# ------------------------------------------------------------------------------ reading

def _canonical_time(value: Any) -> str | None:
    """A host timestamp in the fixed-width UTC form of the backend's clock, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = clock.parse(value)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass
class _Reading:
    turns: list[Turn] = field(default_factory=list)
    deviations: list[Deviation] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)
    versions: dict[str, int] = field(default_factory=dict)
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_refs: list[tuple[str | None, str, str | None]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rows_read: int = 0
    rows_unusable: int = 0
    session_refs: set[str] = field(default_factory=set)
    agent_refs: set[str] = field(default_factory=set)

    def problem(self, text: str) -> None:
        self.rows_unusable += 1
        if text not in self.problems:
            self.problems.append(text)


def _interpret(raw: bytes, *, requests: bool, allowed: Fields | None = None) -> _Reading:
    reading = _Reading()
    refused_responses: set[str] = set()
    pending = list(iter_projected(raw, requests=requests, allowed=allowed))
    last_number = pending[-1][0] if pending else 0
    for number, row, parsed in pending:
        if not parsed:
            # The host appends while the backend reads, so the last line may be half written.
            if number != last_number:
                reading.problem(f"line {number} is not JSON")
            continue
        if row is None:
            continue
        reading.rows_read += 1
        if row.get(DROPPED):
            reading.problem(
                "a line carries " + ", ".join(row[DROPPED]) + " in a shape this reader was not verified on"
            )
            continue
        version = _text(row.get("version"))
        if version is None:
            reading.problem("a line this reader interprets carries no host version")
        else:
            reading.versions[version] = reading.versions.get(version, 0) + 1
        at = _canonical_time(row.get("timestamp"))
        if at is None:
            reading.problem("a line this reader interprets carries no usable timestamp")
            continue
        if _text(row.get("sessionId")):
            reading.session_refs.add(row["sessionId"])
        if _text(row.get("agentId")):
            reading.agent_refs.add(row["agentId"])
        kind = row.get("type")

        if kind == ROW_REQUEST:
            params = row.get("params") or {}
            if _text(row.get("id")):
                reading.requests[row["id"]] = {
                    "model": _text(params.get("model")),
                    "effort": _text((params.get("output_config") or {}).get("effort")),
                    "source": _text(row.get("querySource")),
                    "at": at,
                }
            continue

        if kind == ROW_RESPONSE:
            message = row.get("message")
            model = _text(message.get("model")) if isinstance(message, Mapping) else None
            if model is None:
                reading.problem("a response line names no model")
                continue
            if model == SYNTHETIC_MODEL:
                continue
            effort = row.get("effort")
            per_turn = row.get("perTurnEffort")
            if not all(value is None or _text(value) for value in (effort, per_turn, row.get("requestedModel"))):
                reading.problem("a response line carries an effort or a requested model that is not a name")
                continue
            usage = message.get("usage") if isinstance(message.get("usage"), Mapping) else None
            tokens = tuple(int(usage.get(key, 0)) for key in TOKEN_FIELDS) if usage else None
            turn = Turn(
                at=at, model=model, requested=_text(row.get("requestedModel")),
                effort=_text(effort), per_turn_effort=_text(per_turn),
                message_id=_text(message.get("id")), request_id=_text(row.get("requestId")),
                stop_reason=_text(message.get("stop_reason")), tokens=tokens,
            )
            reading.turns.append(turn)
            reading.request_refs.append((_text(row.get("requestRef")), model, turn.effort))
            if turn.stop_reason == STOP_REFUSAL:
                key = turn.message_id or f"line:{number}"
                if key not in refused_responses:
                    refused_responses.add(key)
                    details = message.get("stop_details") or {}
                    reading.deviations.append(Deviation(
                        kind=DEVIATION_REFUSAL, at=at, from_model=model, to_model=None,
                        category=_text(details.get("category")), request_id=turn.request_id,
                    ))
            continue

        if kind == ROW_ATTACHMENT:
            identity = (row.get("attachment") or {}).get("identity") or {}
            model = _text(identity.get("modelId"))
            if model is None:
                reading.problem("a line announcing the session's model names none")
                continue
            reading.announcements.append(Announcement(at=at, model=model))
            continue

        # A line on which the host noted a refusal or a fallback.
        fallback = _text(row.get("fallbackModel"))
        reading.deviations.append(Deviation(
            kind=DEVIATION_FALLBACK if fallback else DEVIATION_REFUSAL, at=at,
            from_model=_text(row.get("originalModel")), to_model=fallback,
            category=_text(row.get("apiRefusalCategory")), request_id=_text(row.get("requestId")),
        ))
    reading.deviations.sort(key=lambda item: item.at)
    reading.announcements.sort(key=lambda item: item.at)
    return reading


def read_record(
    path: str | Path,
    *,
    verified_versions: Iterable[str],
    session_ref: str | None = None,
    agent_ref: str | None = None,
    meta: Mapping[str, Any] | None = None,
    allowed: Fields | None = None,
) -> Record:
    """Read one transcript into a :class:`Record`.

    The record is ``readable`` only if the file could be read, every line the reader
    interprets was written by a verified host version, and each of them has the shape that
    was verified. Otherwise ``reason`` says what was found, the turns are withheld, and the
    caller treats the worker as unobservable; a reader that guessed would report a model
    with confidence it does not have.
    """
    target = Path(path)
    record = Record(host=HOST, path=str(target), sha256="", size=0,
                    session_ref=session_ref, agent_ref=agent_ref, meta=dict(meta or {}))
    try:
        raw = target.read_bytes()
    except OSError as exc:
        record.reason = f"the host record cannot be read: {type(exc).__name__}"
        return record
    record.sha256 = hashlib.sha256(raw).hexdigest()
    record.size = len(raw)

    reading = _interpret(raw, requests=False, allowed=allowed)
    record.versions = dict(sorted(reading.versions.items()))
    record.rows_read = reading.rows_read
    record.rows_unusable = reading.rows_unusable
    record.deviations = reading.deviations
    if record.session_ref is None and len(reading.session_refs) == 1:
        record.session_ref = next(iter(reading.session_refs))

    verified = {str(version) for version in verified_versions}
    unverified = sorted(set(reading.versions) - verified)
    problems = list(reading.problems)
    if unverified:
        problems.insert(0, (
            f"written by Claude Code {', '.join(unverified)}; this reader was verified on "
            f"{', '.join(sorted(verified)) or 'no version'} only"
        ))
    if len(reading.session_refs) > 1:
        problems.append("lines of more than one session in one record")
    if session_ref is not None and reading.session_refs and reading.session_refs != {session_ref}:
        problems.append("the record belongs to another session than the one asked for")
    if agent_ref is not None and reading.agent_refs and reading.agent_refs != {agent_ref}:
        problems.append("the record belongs to another agent than the one asked for")
    if problems:
        record.reason = "; ".join(problems)
        record.deviations = []
        return record
    record.readable = True
    record.turns = reading.turns
    record.announcements = reading.announcements
    return record


# ----------------------------------------------------------------------------- locating

def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where Claude Code keeps its records: ``CLAUDE_CONFIG_DIR`` when set, else ``~/.claude``."""
    env = os.environ if env is None else env
    raw = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(raw) if raw else Path.home() / ".claude"


def valid_session_ref(session_ref: Any) -> bool:
    return isinstance(session_ref, str) and bool(_SESSION_RE.match(session_ref))


def valid_agent_ref(agent_ref: Any) -> bool:
    return isinstance(agent_ref, str) and bool(_AGENT_RE.match(agent_ref))


def locate_session(session_ref: str, *, config: Path | None = None) -> Path | None:
    """The main transcript of a session, found by its id under the host's records directory."""
    if not valid_session_ref(session_ref):
        return None
    projects = (config or config_dir()) / "projects"
    if not projects.is_dir():
        return None
    found = sorted(projects.glob(f"*/{session_ref}.jsonl"))
    return found[0] if found else None


#: Where the host keeps an agent's transcript, relative to ``<session>/``. An agent started
#: with the Agent tool (or as a teammate) is in ``subagents/``; one started by a Workflow
#: script is one level down, in the directory of that workflow run (observed on 2.1.322).
SUBAGENTS_DIR = "subagents"
WORKFLOWS_DIR = "workflows"
_WORKFLOW_RUN_RE = re.compile(r"^wf_[A-Za-z0-9-]{4,64}$")


def _agent_files(main_path: Path) -> dict[str, Path]:
    """Every agent transcript of a session, by agent id: the plain ones, then those of workflow runs."""
    directory = main_path.with_suffix("") / SUBAGENTS_DIR
    if not directory.is_dir():
        return {}
    candidates = sorted(directory.glob("agent-*.jsonl"))
    workflows = directory / WORKFLOWS_DIR
    if workflows.is_dir():
        for run_dir in sorted(workflows.iterdir()):
            if run_dir.is_dir() and _WORKFLOW_RUN_RE.match(run_dir.name):
                candidates.extend(sorted(run_dir.glob("agent-*.jsonl")))
    found: dict[str, Path] = {}
    for path in candidates:
        ref = path.name[len("agent-"):-len(".jsonl")]
        if valid_agent_ref(ref) and ref not in found:
            found[ref] = path
    return found


def agent_record_path(main_path: Path, agent_ref: str) -> Path | None:
    """Where the host keeps the transcript of one agent of a session.

    The place a plain agent's transcript would be, when the host holds none for this id
    anywhere: a caller that reads it then finds no file, which is the honest answer.
    """
    if not valid_agent_ref(agent_ref):
        return None
    found = _agent_files(main_path).get(agent_ref)
    return found if found is not None else main_path.with_suffix("") / SUBAGENTS_DIR / f"agent-{agent_ref}.jsonl"


def agent_refs_of(main_path: Path) -> list[str]:
    """The agents the host has a transcript for, by the id in the file name."""
    return sorted(_agent_files(main_path))


def workflow_run_of(path: Path) -> str | None:
    """The workflow run an agent transcript belongs to, from where the host put it; None for a plain agent."""
    parent = path.parent
    if parent.parent.name == WORKFLOWS_DIR and parent.parent.parent.name == SUBAGENTS_DIR and _WORKFLOW_RUN_RE.match(parent.name):
        return parent.name
    return None


def read_meta(main_path: Path, agent_ref: str, *, allowed: Fields | None = None) -> dict[str, Any]:
    path = agent_record_path(main_path, agent_ref)
    if path is None:
        return {}
    meta_path = path.with_name(path.name[:-len(".jsonl")] + ".meta.json")
    try:
        return project_meta(json.loads(meta_path.read_text(encoding="utf-8")), allowed=allowed)
    except (OSError, ValueError):
        return {}


def workflow_run(main_path: Path, run_id: Any, *, allowed: Fields | None = None) -> dict[str, Any] | None:
    """The host's own record of a Workflow run of a session, or None when it holds none.

    The host writes ``<session>/workflows/<run id>.json`` when a workflow run has ended (probe
    E, 2.1.322: the file is created at completion), so its presence is the host saying the
    run is over. Only the run fields of the policy are copied (``workflow_run_fields``):
    the script, the logs, the result and the per-agent previews are never read into the answer.
    """
    if not isinstance(run_id, str) or not _WORKFLOW_RUN_RE.match(run_id):
        return None
    allowed = allowed if allowed is not None else fields()
    path = main_path.with_suffix("") / WORKFLOWS_DIR / f"{run_id}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _scalars(document, allowed.workflow_run, allowed.max_value_chars)


def within_records(candidate: str | os.PathLike[str], *, config: Path | None = None) -> Path | None:
    """``candidate`` as a path, if it is a transcript inside the host's records directory.

    A hook passes the path of a worker's transcript. The hook is run by the host, but its
    payload reaches the backend through a command any shell can run, so a path is honoured
    only if it resolves under ``<config>/projects`` and is named the way the host names a
    session or an agent transcript.
    """
    try:
        path = Path(os.path.expanduser(str(candidate))).resolve()
        root = ((config or config_dir()) / "projects").resolve()
        relative = path.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    parts = relative.parts
    if len(parts) == 2 and parts[1].endswith(".jsonl") and valid_session_ref(parts[1][:-len(".jsonl")]):
        return path
    def names_an_agent(name: str) -> bool:
        return (name.startswith("agent-") and name.endswith(".jsonl")
                and valid_agent_ref(name[len("agent-"):-len(".jsonl")]))

    if len(parts) == 4 and valid_session_ref(parts[1]) and parts[2] == SUBAGENTS_DIR and names_an_agent(parts[3]):
        return path
    if (len(parts) == 6 and valid_session_ref(parts[1]) and parts[2] == SUBAGENTS_DIR and parts[3] == WORKFLOWS_DIR
            and _WORKFLOW_RUN_RE.match(parts[4]) and names_an_agent(parts[5])):
        return path
    return None


def refs_of_path(path: Path) -> tuple[str | None, str | None]:
    """``(session_ref, agent_ref)`` encoded in the place the host put a transcript."""
    agent_ref = path.name[len("agent-"):-len(".jsonl")]
    if path.parent.name == SUBAGENTS_DIR:
        return path.parent.parent.name, agent_ref
    if workflow_run_of(path) is not None:
        return path.parent.parent.parent.parent.name, agent_ref
    return path.name[:-len(".jsonl")], None


@dataclass
class Session:
    """The record of a session: the main conversation and every agent it spawned."""

    session_ref: str
    main_path: Path
    main: Record
    agents: list[Record]

    @property
    def records(self) -> list[Record]:
        return [self.main, *self.agents]


def read_session(main_path: str | Path, *, verified_versions: Iterable[str],
                 allowed: Fields | None = None) -> Session:
    main = Path(main_path)
    session_ref = main.name[:-len(".jsonl")]
    versions = list(verified_versions)
    allowed = allowed if allowed is not None else fields()
    agents = []
    for agent_ref in agent_refs_of(main):
        agents.append(read_record(
            agent_record_path(main, agent_ref), verified_versions=versions, session_ref=session_ref,
            agent_ref=agent_ref, meta=read_meta(main, agent_ref, allowed=allowed), allowed=allowed,
        ))
    return Session(
        session_ref=session_ref, main_path=main,
        main=read_record(main, verified_versions=versions, session_ref=session_ref, allowed=allowed),
        agents=agents,
    )


# ------------------------------------------------------------- verifying a host version

def verify_format(path: str | Path) -> dict[str, Any]:
    """What a transcript of *any* version looks like to this reader, with nothing concluded.

    This is the procedure for a host version that is not yet in
    ``role_policy.observation.transcript_reader.claude_code.verified_on_versions``. It reports
    the versions seen, the shape problems, and how the settings on each response line compare
    with the request the host logged for it. A version is added to the verified list by the
    policy's owner when ``agrees`` is true on real transcripts of that version.
    """
    raw = Path(path).read_bytes()
    reading = _interpret(raw, requests=True)
    joined = model_agrees = effort_agrees = 0
    for ref, model, effort in reading.request_refs:
        request = reading.requests.get(ref) if ref else None
        if request is None:
            continue
        joined += 1
        model_agrees += request["model"] == model
        effort_agrees += request["effort"] == effort
    responses = len(reading.request_refs)
    return {
        "path": str(path),
        "versions": dict(sorted(reading.versions.items())),
        "problems": list(reading.problems),
        "response_lines": responses,
        "request_lines": len(reading.requests),
        "joined_to_a_request": joined,
        "model_equals_model_sent": model_agrees,
        "effort_equals_effort_sent": effort_agrees,
        "deviations": [item.to_dict() for item in reading.deviations],
        "agrees": bool(responses) and not reading.problems
        and joined == responses == model_agrees == effort_agrees,
    }
