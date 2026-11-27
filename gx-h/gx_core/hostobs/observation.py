"""Rows of ``worker_observations`` and ``host_events``, and the verdict work is taken on.

Everything the backend knows about who did a piece of work passes through here, and each
row says where its content came from (``labels.observation_source``):

``client_report``     the worker said so about itself. States ``reported_running`` and
                      ``reported_finished``; the ``reported_*`` columns.
``host_transcript``   read from the record the host wrote of that worker's turns. States
                      ``observed_started`` and ``observed_finished``; the ``observed_*`` columns.
``host_hook``         the host ran a hook and its payload says so (``events``).

The two are never merged: a row written from a report carries no observed model, and a row
written from a host record carries no reported one. For one lease the rows form a path of
the ``worker_observation`` machine - ``observed_started``, ``reported_running``,
``reported_finished``, ``observed_finished`` when the host could be read at both ends.

What period of a worker's record a lease is judged on
-----------------------------------------------------
From the last turn the worker took before the lease was issued (the turn in which it
claimed), to the moment of reading. A worker that lived before the task - a teammate, or the
main session doing the work itself - is judged on the turns of this task and on the model it
was on when it took the task, and a fallback long before is reported as context rather than
counted. A period with no turn in it never yields ``CONFORMS``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .. import contracts
from ..domain import clock, ids, machines
from ..errors import ArtifactNotFound, ContentHashMismatch, SchemaInvalid
from . import claude_code, policy
from .record import Record, deviation_for_report, summarise

MACHINE = "worker_observation"

SOURCE_REPORT = "client_report"
SOURCE_HOOK = "host_hook"
SOURCE_TRANSCRIPT = "host_transcript"
for _value in (SOURCE_REPORT, SOURCE_HOOK, SOURCE_TRANSCRIPT):
    contracts.require_label("observation_source", _value)
del _value

LEVEL_OBSERVED = "EXECUTION_OBSERVED"
LEVEL_REPORTED = "CLIENT_REPORTED"
#: What a client may say about the evidence behind its own report. It cannot say "observed".
CLIENT_LEVELS = ("CLIENT_REPORTED", "PROPOSED", "UNVERIFIED")
for _value in (LEVEL_OBSERVED, *CLIENT_LEVELS):
    contracts.require_label("evidence_level", _value)
del _value

STATE_REQUESTED = "requested"
STATE_OBSERVED_STARTED = "observed_started"
STATE_REPORTED_RUNNING = "reported_running"
STATE_REPORTED_FINISHED = "reported_finished"
STATE_OBSERVED_FINISHED = "observed_finished"
STATE_FAILED = "failed"
_OBSERVED_STATES = (STATE_OBSERVED_STARTED, STATE_OBSERVED_FINISHED)
for _value in (STATE_REQUESTED, STATE_OBSERVED_STARTED, STATE_REPORTED_RUNNING, STATE_REPORTED_FINISHED,
               STATE_OBSERVED_FINISHED, STATE_FAILED):
    if _value not in machines.all_states(MACHINE):
        raise RuntimeError(f"{_value!r} is not a state of the worker_observation machine")
del _value

#: Sources from which a state named ``observed_*`` may be written (contracts/states.json).
_OBSERVING_SOURCES = ("host_hook", "host_transcript", "backend_process", "host_protocol")

HOST_CLAUDE_CODE = claude_code.HOST
contracts.require_label("host_kind", HOST_CLAUDE_CODE)


def _json(value: Any) -> str:
    return ids.canonical_json(value)


def derived_event_id(*parts: Any) -> str:
    """The id of an event derived from a host record: the same finding is the same row."""
    digest = hashlib.sha256("\x1f".join("" if part is None else str(part) for part in parts).encode("utf-8"))
    return f"{ids.PREFIXES['host_event']}_d{digest.hexdigest()[:24]}"


# ------------------------------------------------------------------------------- hosts

def strictest_host(hosts: list[str | None], policy_doc: Mapping[str, Any] | None = None) -> str:
    """Of several host kinds, the one whose mode accepts the fewest verdicts.

    A run started under a strict host stays strict for a worker that names a lenient one.
    With no host on record at all the answer is the most lenient kind, whose mode records
    the verdict and holds nothing: a script or a person driving the backend has no model to
    observe.
    """
    policy_doc = policy_doc if policy_doc is not None else policy.agent_policy()[0]
    modes = policy_doc["observation"]["by_host"]
    known = [host for host in hosts if host in modes]
    if not known:
        return max(sorted(modes), key=lambda host: len(modes[host]["accepts"]))
    return min(known, key=lambda host: len(modes[host]["accepts"]))


def policy_of_run(conn: sqlite3.Connection, run_id: str | None) -> tuple[dict[str, Any], str]:
    """The agent policy a run pinned when it started, with its sha256.

    A worker is judged by the policy its run started under: editing the file afterwards
    neither tightens nor loosens a run in progress (resume compares the sha256 and refuses
    on a difference, which is where a changed policy is put in front of someone). A run
    that pinned none - one created below the tool surface - is judged by the file as it is.
    """
    if run_id:
        row = conn.execute(
            "SELECT agent_policy_json, agent_policy_sha256, host_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and (row["agent_policy_json"] not in (None, "", "{}") or row["agent_policy_sha256"]):
            if not row["agent_policy_json"] or not row["agent_policy_sha256"]:
                raise ContentHashMismatch("the run has an incomplete pinned agent policy snapshot")
            try:
                pinned = json.loads(row["agent_policy_json"])
            except ValueError as exc:
                raise ContentHashMismatch("the run's pinned agent policy snapshot is unreadable") from exc
            if not isinstance(pinned, dict) or not pinned or policy.problems_in(pinned):
                raise ContentHashMismatch("the run's pinned agent policy snapshot is invalid")
            if "policy_host" in pinned or "policy_scope" in pinned:
                from . import host_policy
                host_policy.scope_of_run(row)
            return pinned, str(row["agent_policy_sha256"])
    return policy.agent_policy()


def run_host_kind(conn: sqlite3.Connection, run_id: str | None) -> str | None:
    """The host kind recorded on the run when it started, or None."""
    if not run_id:
        return None
    row = conn.execute("SELECT host_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or not row["host_json"]:
        return None
    try:
        kind = json.loads(row["host_json"]).get("kind")
    except (ValueError, AttributeError):
        return None
    return kind if kind in contracts.label_values("host_kind") else None


def read_worker_record(
    host: str, session_ref: str | None, agent_ref: str | None,
    policy_doc: Mapping[str, Any] | None = None,
) -> Record | None:
    """The host's own record of a worker, or None when this host kind has no reader.

    For a host that has a reader the answer is always a record; when the worker cannot be
    found it is an unreadable one whose ``reason`` says what was looked for.
    """
    spec = policy.reader_spec(host, policy_doc)
    if host != HOST_CLAUDE_CODE or spec is None:
        return None
    placeholder = Record(host=host, path="", sha256="", size=0, session_ref=session_ref, agent_ref=agent_ref)
    if not claude_code.valid_session_ref(session_ref):
        placeholder.reason = "the worker named no host session, so there is no record to look for"
        return placeholder
    main = claude_code.locate_session(session_ref)
    if main is None:
        placeholder.reason = f"the host holds no record of session {session_ref}"
        return placeholder
    allowed = claude_code.Fields.from_spec(spec)
    if agent_ref is None:
        return claude_code.read_record(main, verified_versions=spec["verified_on_versions"],
                                       session_ref=session_ref, allowed=allowed)
    path = claude_code.agent_record_path(main, agent_ref)
    if path is None or not path.is_file():
        placeholder.reason = "the host holds no record of that agent in that session"
        return placeholder
    return claude_code.read_record(
        path, verified_versions=spec["verified_on_versions"], session_ref=session_ref, agent_ref=agent_ref,
        meta=claude_code.read_meta(main, agent_ref, allowed=allowed), allowed=allowed,
    )


def observed_worker_ref(record: Record | None) -> str | None:
    """A reference to a worker the host's record shows to exist, or None.

    Two leases whose observations carry two different references were held by two workers
    the host recorded separately. A reference a worker merely reported is never one of these.
    """
    if record is None or not record.readable or not record.turns or not record.session_ref:
        return None
    return f"{record.host}:{record.session_ref}:{record.agent_ref or 'main'}"


def period_start(record: Record, issued_at: str) -> str:
    """The turn in which the worker took the lease, or the lease's own time if it had none."""
    before = [turn.at for turn in record.turns if turn.at < issued_at]
    return max(before) if before else issued_at


def _last_turn(record: Record, since: str | None, until: str | None):
    chosen = None
    for turn in record.turns:
        if (since is None or turn.at >= since) and (until is None or turn.at <= until):
            chosen = turn
    return chosen


# -------------------------------------------------------------------------------- rows

def latest_state(conn: sqlite3.Connection, *, task_id: str, lease_id: str | None, agent_ref: str | None) -> str | None:
    """The state of the newest observation of one lease (or, with no lease, of one agent on a task)."""
    if lease_id is not None:
        row = conn.execute(
            "SELECT state FROM worker_observations WHERE task_id=? AND lease_id=? ORDER BY rowid DESC LIMIT 1",
            (task_id, lease_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT state FROM worker_observations WHERE task_id=? AND lease_id IS NULL"
            " AND agent_ref IS ? ORDER BY rowid DESC LIMIT 1",
            (task_id, agent_ref),
        ).fetchone()
    return row["state"] if row is not None else None


def can_follow(previous: str | None, state: str) -> bool:
    """Whether ``state`` may be written after ``previous`` for the same lease.

    The same state again is a newer observation of the same point, not a transition.
    """
    origin = previous if previous is not None else machines.initial_state(MACHINE)
    return state == origin or machines.can_transition(MACHINE, origin, state)


def write_observation(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    host: str,
    state: str,
    source: str,
    evidence_level: str,
    lease_id: str | None = None,
    host_session_ref: str | None = None,
    agent_ref: str | None = None,
    observed_ref: str | None = None,
    delegation_mode: str | None = None,
    reported_model: str | None = None,
    reported_effort: str | None = None,
    observed_model: str | None = None,
    observed_effort: str | None = None,
    turns_observed: int | None = None,
    turns_off_policy: int | None = None,
    transcript_sha256: str | None = None,
    host_version: str | None = None,
    policy_verdict: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Append one observation. Refuses a row that would say more than its source can."""
    contracts.require_label("host_kind", host)
    contracts.require_label("observation_source", source)
    contracts.require_label("evidence_level", evidence_level)
    if delegation_mode is not None:
        contracts.require_label("delegation_mode", delegation_mode)
    if policy_verdict is not None:
        contracts.require_label("model_policy_verdict", policy_verdict)
    if state not in machines.all_states(MACHINE):
        raise SchemaInvalid(f"{state!r} is not a worker observation state",
                            details={"allowed": machines.all_states(MACHINE)})
    observing = source in _OBSERVING_SOURCES
    if state in _OBSERVED_STATES and not observing:
        raise SchemaInvalid(
            f"{state} is written only from a record the host wrote or a process the backend "
            f"started; {source} is a report",
            details={"state": state, "source": source, "rule": machines.rule(MACHINE, "observed")},
        )
    if not observing and (observed_model is not None or observed_effort is not None or observed_ref is not None
                          or evidence_level == LEVEL_OBSERVED):
        raise SchemaInvalid(
            "a report cannot fill the observed columns or call itself observed: a model's "
            "statement about itself is a report, never an authentication",
            details={"source": source, "evidence_level": evidence_level},
        )
    if observing and (reported_model is not None or reported_effort is not None):
        raise SchemaInvalid("an observation carries no reported model or effort; they are separate rows",
                            details={"source": source})
    previous = latest_state(conn, task_id=task_id, lease_id=lease_id, agent_ref=agent_ref)
    if not can_follow(previous, state):
        machines.check_transition(MACHINE, previous or machines.initial_state(MACHINE), state, subject_id=lease_id or task_id)
    observation_id = ids.new_id("worker")
    conn.execute(
        "INSERT INTO worker_observations(worker_observation_id, task_id, lease_id, host, host_session_ref,"
        " observed_worker_ref, agent_ref, state, evidence_level, observation_source, delegation_mode,"
        " reported_model, reported_effort, observed_model, observed_effort, model_evidence_level,"
        " turns_observed, turns_off_policy, transcript_sha256, host_version, policy_verdict, observed_at,"
        " payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id, task_id, lease_id, host, host_session_ref, observed_ref, agent_ref, state,
            evidence_level, source, delegation_mode, reported_model, reported_effort, observed_model,
            observed_effort,
            LEVEL_OBSERVED if observed_model is not None else (LEVEL_REPORTED if reported_model is not None else None),
            turns_observed, turns_off_policy, transcript_sha256, host_version, policy_verdict,
            clock.now_iso(), _json(dict(payload or {})),
        ),
    )
    return observation_id


def record_event(
    conn: sqlite3.Connection,
    *,
    host: str,
    hook_event: str,
    session_ref: str,
    source: str,
    agent_ref: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
    matched_task_id: str | None = None,
    run_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> tuple[str, bool]:
    """Append one host event. Returns ``(event_id, written)``; a derived event is written once."""
    contracts.require_label("host_kind", host)
    contracts.require_label("observation_source", source)
    derived = event_id is not None
    event_id = event_id or ids.new_id("host_event")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO host_events(event_id, run_id, host, hook_event, session_ref, agent_ref, model,"
        " effort, source, decision, reason, matched_task_id, payload_json, received_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, run_id, host, hook_event, session_ref, agent_ref, model, effort, source, decision,
         reason, matched_task_id, _json(dict(payload or {})), clock.now_iso()),
    )
    written = cursor.rowcount == 1
    if not written and not derived:
        raise RuntimeError(f"host event id {event_id} collided")
    return event_id, written


# ----------------------------------------------------------------------------- lookups

def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return {key: row[key] for key in row.keys()} if row is not None else None


def _task_and_lease(conn: sqlite3.Connection, task_id: str, lease_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    task = _row(conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
    if task is None:
        raise ArtifactNotFound(f"no such task {task_id!r}", details={"task_id": task_id})
    lease = _row(conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone())
    if lease is None or lease["task_id"] != task_id:
        raise ArtifactNotFound(f"no lease {lease_id!r} on task {task_id!r}",
                               details={"task_id": task_id, "lease_id": lease_id})
    return task, lease


def binding_of(conn: sqlite3.Connection, *, task_id: str, lease_id: str) -> dict[str, Any] | None:
    """What the worker under a lease said about itself when it bound, or None if it never did."""
    return _row(conn.execute(
        "SELECT * FROM worker_observations WHERE task_id=? AND lease_id=? AND observation_source=?"
        " AND state=? ORDER BY rowid DESC LIMIT 1",
        (task_id, lease_id, SOURCE_REPORT, STATE_REPORTED_RUNNING),
    ).fetchone())


#: Derived events: what the ties between subjects and agents add up to.
EVENT_SEVERAL_SUBJECTS = "WorkerUnderSeveralSubjects"
EVENT_SHARED_SUBJECT = "SubjectUsedBySeveralAgents"
#: The hook event and tool on which a host hook reports a call of the backend's client.
_CLIENT_CALL_EVENT, _CLIENT_CALL_TOOL = "PreToolUse", "Bash"


def _client_call_rows(conn: sqlite3.Connection, host: str, where: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
    # In the order the rows were written. ``received_at`` has millisecond grain and a host
    # event id ends in random characters, so ordering by those two put two calls of one
    # millisecond in either order (seen on 2026-11-12: a test asking for "the order first
    # seen" passed or failed by chance).
    return conn.execute(
        "SELECT rowid AS row_no, session_ref, agent_ref, payload_json, received_at FROM host_events "
        f"WHERE host=? AND source=? AND hook_event=? AND decision=? AND {where} ORDER BY rowid",
        (host, SOURCE_HOOK, _CLIENT_CALL_EVENT, "allow", *args),
    ).fetchall()


def _subjects_in(row: sqlite3.Row) -> list[str]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        return []
    if not isinstance(payload, dict) or payload.get("tool_name") != _CLIENT_CALL_TOOL or not payload.get("gx_calls"):
        return []
    return [item for item in payload.get("gx_subjects") or [] if isinstance(item, str)]


def subjects_of_agent(conn: sqlite3.Connection, *, host: str, session_ref: str, agent_ref: str | None) -> list[str]:
    """Every subject a host hook saw this agent call the backend under, in the order first seen."""
    rows = _client_call_rows(conn, host, "session_ref=? AND agent_ref IS ?", (session_ref, agent_ref))
    return list(dict.fromkeys(subject for row in rows for subject in _subjects_in(row)))


def agents_of_subject(conn: sqlite3.Connection, *, host: str, subject: str) -> list[dict[str, Any]]:
    """Every agent a host hook said was calling the backend under this subject.

    ``[{"session_ref", "agent_ref", "calls", "first_at", "last_at"}]``; ``agent_ref`` is None
    for a session's own thread. Each row is what a hook delivered: where to look, not yet who
    it was. :func:`tie_of_subject` is the reading that asks the host's own record.
    """
    found: dict[tuple[str, str | None], dict[str, Any]] = {}
    # The subject sits inside the payload; the rows of one host are few enough to read.
    for row in _client_call_rows(conn, host, "1=1", ()):
        if subject not in _subjects_in(row):
            continue
        key = (row["session_ref"], row["agent_ref"])
        entry = found.setdefault(key, {"session_ref": key[0], "agent_ref": key[1], "calls": 0,
                                       "first_at": row["received_at"], "last_at": row["received_at"]})
        entry["calls"] += 1
        entry["last_at"] = row["received_at"]
    return list(found.values())


#: How far a turn of a session's own thread may lie from a hook's event for the event to be
#: about that thread's work (design r2.2a section 8.2: "a turn in the minute of the event").
TIE_WINDOW_SECONDS = 60


def _near(at: str, first: str, last: str, seconds: int = TIE_WINDOW_SECONDS) -> bool:
    try:
        moment = clock.parse(at)
        return (clock.parse(first) - moment).total_seconds() <= seconds and \
               (moment - clock.parse(last)).total_seconds() <= seconds
    except (TypeError, ValueError):
        return False


def _borne_out(host: str, subject: str, item: Mapping[str, Any], spec: Mapping[str, Any],
               policy_doc: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the host's own record says about a hook's claim that an agent works under a subject.

    None when the record does not bear the claim out. Otherwise ``{"borne_out_by", "record"}``:
    for an agent, its own meta names this very subject (the label its launcher gave it), and its
    record, where it already holds turns, has one within a minute of the hook's events; for a
    session's own thread, which has no meta, its record has a turn within that minute.
    """
    if host != HOST_CLAUDE_CODE or not claude_code.valid_session_ref(item.get("session_ref")):
        return None
    main = claude_code.locate_session(item["session_ref"])
    if main is None:
        return None
    agent_ref = item.get("agent_ref")
    if agent_ref is not None:
        allowed = claude_code.Fields.from_spec(spec)
        if claude_code.read_meta(main, agent_ref, allowed=allowed).get(allowed.meta_subject_field) != subject:
            return None
    record = read_worker_record(host, item["session_ref"], agent_ref, policy_doc)
    turns = record.turns if record is not None and record.readable else []
    near = any(_near(turn.at, item["first_at"], item["last_at"]) for turn in turns)
    if agent_ref is None:
        return {"borne_out_by": "a_turn_of_the_sessions_own_thread", "record": record} if near else None
    if turns and not near:
        return None             # the agent the meta names took no turn anywhere near the hook's event
    return {"borne_out_by": "the_agents_meta_names_the_subject", "record": record}


def tie_of_subject(conn: sqlite3.Connection, *, host: str, subject: str | None,
                   policy_doc: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The one agent a subject is tied to, or None when there is none or more than one.

    Design r2.2a section 8.2: a hook's event is a proposal and the host's own record is the
    observation. ``gx_host_event`` is guarded only by a scope any shell can name, so where the
    policy's reader has the rule (``meta_subject_field``) an agent a hook named counts only
    when the host's record bears the claim out (:func:`_borne_out`). The answer then carries
    ``borne_out_by`` and the ``record`` that was read. A forged event that names another agent
    for a subject finds that agent's meta naming another subject, or none, and ties nothing.

    Without the rule - a run that pinned a policy from before it, or a host with no reader -
    the hook's word is the tie, as it was.
    """
    if not subject:
        return None
    agents = agents_of_subject(conn, host=host, subject=subject)
    spec = policy.reader_spec(host, policy_doc) if host in contracts.label_values("host_kind") else None
    if spec is None or not spec.get("meta_subject_field"):
        return agents[0] if len(agents) == 1 else None
    borne = []
    for item in agents:
        found = _borne_out(host, subject, item, spec, policy_doc)
        if found is not None:
            borne.append({**item, **found})
    return borne[0] if len(borne) == 1 else None


def claim_independence(conn: sqlite3.Connection, *, task: Mapping[str, Any], subject: str | None) -> dict[str, Any] | None:
    """See :func:`gx_core.hostobs.policy.claim_independence`."""
    if not isinstance(task, Mapping):          # a sqlite3.Row of the tasks table, as the claim holds it
        task = {key: task[key] for key in task.keys()}
    author_task = task.get("independent_of_task_id")
    if not author_task:
        return None
    policy_doc, _sha = policy_of_run(conn, task.get("run_id"))
    run_host = run_host_kind(conn, task.get("run_id"))
    hosts = [run_host] if run_host else [name for name in contracts.label_values("host_kind")
                                         if needs_hook_tie(name, policy_doc)]
    claimant = None
    for host in hosts:
        tie = tie_of_subject(conn, host=host, subject=subject, policy_doc=policy_doc)
        if tie is not None:
            claimant = f"{host}:{tie['session_ref']}:{tie['agent_ref'] or 'main'}"
            break
    if claimant is None:
        strict = run_host is not None and policy.host_mode(run_host, policy_doc).get("mode") == "strict"
        if strict:
            spec = policy.reader_spec(run_host, policy_doc) or {}
            missing = (f"no hook tied subject {subject!r} to one agent whose own record names it"
                       if spec.get("meta_subject_field") else f"no hook tied subject {subject!r} to one agent")
            return {"basis": "no_hook_tie", "claimant": None, "authors": [], "reason": (
                f"task {task.get('task_id')} reviews task {author_task}; on a {run_host} run the reviewer is the agent "
                f"the host's hook saw make the claim, and {missing}")}
        return None
    authors: set[str] = set()
    for lease in conn.execute("SELECT lease_id FROM leases WHERE task_id=?", (author_task,)):
        found = identity_of(conn, task_id=author_task, lease_id=lease["lease_id"])
        if found["observed_worker_ref"]:
            authors.add(found["observed_worker_ref"])
        authors.update(found["agents_of_subject"])
    for row in conn.execute(
        "SELECT observed_worker_ref FROM worker_observations WHERE task_id=? AND observed_worker_ref IS NOT NULL",
        (author_task,),
    ):
        authors.add(row["observed_worker_ref"])
    if claimant not in authors:
        return None
    return {"basis": "observed_author", "claimant": claimant, "authors": sorted(authors), "reason": (
        f"task {task.get('task_id')} reviews task {author_task}, and the host recorded the agent making this claim "
        f"({claimant}) as a worker of that task; a review is made by another agent, launched under its own subject")}


def identity_of(conn: sqlite3.Connection, *, task_id: str, lease_id: str | None = None) -> dict[str, Any]:
    """Who did a task, as far as the host can say, for whoever must decide whether two tasks had two workers.

    ``principal`` is the subject the lease was issued to - a name the worker typed.
    ``observed_worker_ref`` is the agent the host recorded, set only from rows a host hook or
    a host record produced; ``source`` says which. ``subjects_of_agent`` lists every subject
    that agent was seen under and ``agents_of_subject`` every agent seen under the
    principal: more than one entry in either is a name that does not stand for one worker.
    """
    from .native_context import is_native_run
    owner = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if owner and is_native_run(conn, owner["run_id"]):
        from .native_observation import identity_of as protocol_identity
        return protocol_identity(conn, task_id=task_id, lease_id=lease_id)
    if lease_id is None:
        row = conn.execute(
            "SELECT * FROM leases WHERE task_id=? ORDER BY issued_at DESC, rowid DESC LIMIT 1", (task_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM leases WHERE lease_id=? AND task_id=?", (lease_id, task_id)).fetchone()
    out: dict[str, Any] = {
        "task_id": task_id, "lease_id": row["lease_id"] if row is not None else lease_id,
        "principal": row["principal"] if row is not None else None,
        "host": None, "host_session_ref": None, "agent_ref": None, "observed_worker_ref": None, "source": None,
        "subjects_of_agent": [], "agents_of_subject": [], "one_worker_one_name": None,
    }
    if row is None:
        return out
    observed = conn.execute(
        "SELECT host, host_session_ref, agent_ref, observed_worker_ref, observation_source FROM worker_observations "
        "WHERE task_id=? AND lease_id=? AND observed_worker_ref IS NOT NULL AND observation_source != ? "
        "ORDER BY observed_at DESC, rowid DESC LIMIT 1", (task_id, row["lease_id"], SOURCE_REPORT),
    ).fetchone()
    if observed is not None:
        out.update(host=observed["host"], host_session_ref=observed["host_session_ref"], agent_ref=observed["agent_ref"],
                   observed_worker_ref=observed["observed_worker_ref"], source=observed["observation_source"])
    hosts = [out["host"]] if out["host"] else [name for name in contracts.label_values("host_kind")]
    run = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    policy_doc, _sha = policy_of_run(conn, run["run_id"] if run is not None else None)
    for host in hosts:
        agents = agents_of_subject(conn, host=host, subject=row["principal"]) if row["principal"] else []
        if not agents:
            continue
        out["agents_of_subject"] = [f"{host}:{item['session_ref']}:{item['agent_ref'] or 'main'}" for item in agents]
        if out["observed_worker_ref"] is None:
            # No record of its turns was read for this lease yet. The agent is named only when
            # the tie holds: where the policy has the rule, on the host's own record of that
            # agent (design r2.2a section 8.2); before the rule, on the hook's word.
            tie = tie_of_subject(conn, host=host, subject=row["principal"], policy_doc=policy_doc)
            if tie is not None:
                out.update(host=host, host_session_ref=tie["session_ref"], agent_ref=tie["agent_ref"],
                           observed_worker_ref=f"{host}:{tie['session_ref']}:{tie['agent_ref'] or 'main'}",
                           source=SOURCE_HOOK)
        break
    if out["host"] and out["host_session_ref"]:
        out["subjects_of_agent"] = subjects_of_agent(conn, host=out["host"], session_ref=out["host_session_ref"],
                                                     agent_ref=out["agent_ref"])
    if out["observed_worker_ref"] is not None:
        out["one_worker_one_name"] = len(out["subjects_of_agent"]) <= 1 and len(out["agents_of_subject"]) <= 1
    return out


def hook_saw_agent(conn: sqlite3.Connection, *, host: str, session_ref: str | None, agent_ref: str | None) -> bool:
    """Whether a hook the host ran has named this agent in this session."""
    if not session_ref or not agent_ref:
        return False
    row = conn.execute(
        "SELECT 1 FROM host_events WHERE host=? AND session_ref=? AND agent_ref=? AND source=? LIMIT 1",
        (host, session_ref, agent_ref, SOURCE_HOOK),
    ).fetchone()
    return row is not None


# ----------------------------------------------------------------------------- judging

_UNREAD = object()


def untied_record(host: str, subject: str | None, *, session_ref: str | None, agent_ref: str | None) -> Record:
    """The record judged for a lease no hook of the host tied to an agent: none, and why.

    On a host whose workers are shown through its hooks (Claude Code), the host says which of
    its agents ran a call of the backend's client, under which subject. Without that tie, the
    agent a worker names is only what it says, and reading the record it names would let the
    worker choose what it is judged on (audit HOST-02). A worker that names no agent is not
    judged on its session's main thread either: that is the coordinator's record (audit
    host/tasks). So nothing is read, the report stays a report, and a strict host holds it.
    """
    record = Record(host=host, path="", sha256="", size=0, session_ref=session_ref, agent_ref=agent_ref)
    record.reason = (f"the host ties subject {subject!r} to none of its agents: no hook saw one call the backend "
                     "under it, or the record of the agent a hook named does not name it; so no record of the "
                     "host is read on the worker's word")
    return record


#: The one delegation mode whose bind may be judged on the session's main thread (design r2 §8.2).
SINGLE_AGENT = contracts.require_label("delegation_mode", "single_agent_sequential")


def main_thread_record(host: str, subject: str | None, *, session_ref: str | None) -> Record:
    """The record judged when the hook tied a subject to a session's main thread, for a bind
    that did not declare single_agent_sequential: none. The main thread is the coordinator's
    record; a delegated worker is an agent of its own, so nothing is read on its behalf."""
    record = Record(host=host, path="", sha256="", size=0, session_ref=session_ref, agent_ref=None)
    record.reason = (f"the host's hook tied subject {subject!r} to the main thread of session {session_ref}, and the "
                     "main thread is read only for a bind that declares single_agent_sequential")
    return record


def _record_for(tie: Mapping[str, Any] | None, untied: Record | None) -> dict[str, Any]:
    """The record to judge, when it is already settled: none for an untied worker, the one the tie was borne out on."""
    if untied is not None:
        return {"record": untied}
    if tie is not None and tie.get("record") is not None:
        return {"record": tie["record"]}
    return {}


def needs_hook_tie(host: str | None, policy_doc: Mapping[str, Any] | None = None) -> bool:
    """Whether a worker of this host is shown only through the host's hooks: its records are read by agent."""
    return host is not None and policy.reader_spec(host, policy_doc) is not None


def _observe(
    conn: sqlite3.Connection, *, task: Mapping[str, Any], lease: Mapping[str, Any], host: str,
    session_ref: str | None, agent_ref: str | None, reported: Mapping[str, Any] | None,
    until: str | None = None, record: Any = _UNREAD, untied: bool = False,
    _codex_prepared: Any = None,
) -> dict[str, Any]:
    """Read the host's record for a lease and judge it. Writes nothing.

    ``record`` is passed by a caller that has already read the worker's record and is
    judging several leases against it.
    """
    from . import codex_worker_observation
    if codex_worker_observation.scoped_run(conn, task["run_id"]):
        return codex_worker_observation.consume(conn, task=task, lease=lease,
                                                prepared=_codex_prepared, reported=reported,
            declared_identity={"host": host, "session_ref": session_ref, "agent_ref": agent_ref})
    policy_doc, policy_sha256 = policy_of_run(conn, task.get("run_id"))
    required = policy.requirement_of_task(task, policy_doc)
    judged_host = strictest_host([host, run_host_kind(conn, task.get("run_id"))], policy_doc)
    if record is _UNREAD:
        record = read_worker_record(host, session_ref, agent_ref, policy_doc)
    summary = None
    if record is not None:
        summary = summarise(
            record, required_model=required["model"], allowed_efforts=required["allowed_efforts"],
            since=period_start(record, lease["issued_at"]) if record.readable else lease["issued_at"],
            until=until,
        )
    verdict = policy.judge(host=judged_host, required=required, reported=reported, observed=summary,
                           policy=policy_doc, untied=untied)
    deviation = None
    if verdict["waits_for_user"]:
        deviation = deviation_for_report(summary, required_model=required["model"]) if summary else None
        if deviation is None:
            deviation = {
                "kind": "report_outside_policy", "when": clock.now_iso(), "from_model": required["model"],
                "to_model": (reported or {}).get("model"), "category": None, "request_id": None,
                "effort": (reported or {}).get("effort"), "source": SOURCE_REPORT,
            }
        deviation.update({"task_id": task["task_id"], "brief_sha256": task.get("brief_sha256"),
                          "lease_id": lease["lease_id"], "failure_class": verdict["failure_class"]})
    last = _last_turn(record, summary["since"], summary["until"]) if record is not None and record.readable else None
    usable = bool(summary and summary["readable"] and summary["turns"])
    return {
        "verdict": verdict, "summary": summary, "record": record, "required": required,
        "judged_host": judged_host, "policy_sha256": policy_sha256, "deviation": deviation,
        "usable": usable, "last_turn": last,
        "worker_ref": observed_worker_ref(record) if usable else None,
    }


def _observed_view(found: Mapping[str, Any], *, corroborated: bool) -> dict[str, Any]:
    """What the answer says was observed. Counts and names; no path and no content."""
    summary = found["summary"]
    if summary is None:
        return {"source": None, "readable": False, "reason": "this host kind has no record the backend can read"}
    last = found["last_turn"]
    view = {
        "source": SOURCE_TRANSCRIPT, "readable": summary["readable"], "reason": summary["reason"],
        "host_version": summary["host_version"], "record_sha256": summary["record_sha256"] or None,
        "since": summary["since"], "until": summary["until"],
        "model": last.model if last else None, "effort": last.effort if last else None,
        "turns": summary["turns"], "responses": summary["responses"],
        "turns_off_policy": summary["turns_off_policy"], "models": summary["models"],
        "efforts": summary["efforts"], "tokens": summary["tokens"],
        "deviations": summary["deviations"], "preceding_fallback": summary["preceding_fallback"],
        "stretches": summary["stretches"], "off_policy_ongoing": summary["off_policy_ongoing"],
        "observed_worker_ref": found["worker_ref"], "agent_named_by_a_host_hook": corroborated,
    }
    if found.get("model_authority"):
        view.update(model_authority=found["model_authority"], upstream_tier_independently_verified=False,
                    claim_event_id=found.get("claim_event_id"))
    return view


def _answer(found: Mapping[str, Any], *, observation_id: str, action: str, observed: Mapping[str, Any],
            reported: Mapping[str, Any] | None) -> dict[str, Any]:
    verdict = found["verdict"]
    required = found["required"]
    return {
        "worker_observation_id": observation_id,
        "policy_verdict": verdict["policy_verdict"],
        "action": action,
        "waits_for_user": verdict["waits_for_user"],
        "failure_class": verdict["failure_class"],
        "reasons": list(verdict["reasons"]),
        "basis": verdict["basis"],
        "host": found["judged_host"],
        "mode": verdict["mode"],
        "required": {"model": required["model"], "allowed_efforts": required["allowed_efforts"],
                     "task_class": required["task_class"]},
        "reported": {"model": (reported or {}).get("model"), "effort": (reported or {}).get("effort")},
        "report_disagrees": verdict["report_disagrees"],
        "observed": dict(observed),
        "deviation": found["deviation"],
        "policy_sha256": found["policy_sha256"],
    }


def _write_observed(conn: sqlite3.Connection, *, found: Mapping[str, Any], task_id: str, lease_id: str,
                    host: str, session_ref: str | None, agent_ref: str | None, state: str,
                    observed: Mapping[str, Any]) -> str:
    summary, last = found["summary"], found["last_turn"]
    return write_observation(
        conn, task_id=task_id, lease_id=lease_id, host=host, state=state, source=SOURCE_TRANSCRIPT,
        evidence_level=LEVEL_OBSERVED, host_session_ref=session_ref, agent_ref=agent_ref,
        observed_ref=found["worker_ref"], observed_model=last.model, observed_effort=last.effort,
        turns_observed=summary["turns"], turns_off_policy=summary["turns_off_policy"],
        transcript_sha256=summary["record_sha256"], host_version=summary["host_version"],
        policy_verdict=found["verdict"]["policy_verdict"],
        payload={"observed": dict(observed), "reasons": found["verdict"]["reasons"],
                 "judged_as": found["judged_host"], "policy_sha256": found["policy_sha256"]},
    )


def bind_worker(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    host: str,
    host_session_ref: str | None = None,
    agent_ref: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    delegation_mode: str | None = None,
    evidence_level: str = LEVEL_REPORTED,
    actor: str = "backend",
    _codex_prepared: Any = None,
) -> dict[str, Any]:
    """See :func:`gx_core.hostobs.policy.bind_worker`."""
    from .native_context import is_native_run
    owner = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if owner and is_native_run(conn, owner["run_id"]):
        from .native_observation import bind_worker as protocol_bind
        return protocol_bind(conn, task_id=task_id, lease_id=lease_id, host=host,
            host_session_ref=host_session_ref, agent_ref=agent_ref, model=model, effort=effort,
            delegation_mode=delegation_mode, evidence_level=evidence_level, actor=actor,
            _codex_prepared=_codex_prepared)
    if host not in contracts.label_values("host_kind"):
        raise SchemaInvalid(f"{host!r} is not a host kind", details={"allowed": contracts.label_values("host_kind")})
    if evidence_level not in CLIENT_LEVELS:
        raise SchemaInvalid(
            f"a bind cannot record {evidence_level!r}: what a worker says about itself is a report. "
            f"Allowed: {', '.join(CLIENT_LEVELS)}",
            details={"evidence_level": evidence_level, "allowed": list(CLIENT_LEVELS)},
        )
    if delegation_mode is not None and delegation_mode not in contracts.label_values("delegation_mode"):
        raise SchemaInvalid(f"{delegation_mode!r} is not a delegation mode",
                            details={"allowed": contracts.label_values("delegation_mode")})
    from . import codex
    valid_agent = codex.valid_thread_ref if host == "codex" else claude_code.valid_agent_ref
    if agent_ref is not None and not valid_agent(agent_ref):
        raise SchemaInvalid("agent_ref is not an agent identifier", details={"agent_ref": str(agent_ref)[:64]})
    task, lease = _task_and_lease(conn, task_id, lease_id)
    reported = {"model": model, "effort": effort}
    # Which agent's record is read: the one a hook of the host tied to the subject this lease
    # was issued to, when there is exactly one. What the worker says about which agent it is
    # stays a report; where the two differ the host's word is used and the difference kept.
    said = {"host_session_ref": host_session_ref, "agent_ref": agent_ref}
    run_policy, _run_sha = policy_of_run(conn, task.get("run_id"))
    from . import codex_worker_observation
    native_scope = codex_worker_observation.scoped_run(conn, task["run_id"])
    tie = None if native_scope else tie_of_subject(conn, host=host, subject=lease.get("principal"), policy_doc=run_policy)
    identity_differs = False
    untied = None
    if tie is not None:
        identity_differs = (host_session_ref is not None and host_session_ref != tie["session_ref"]) or \
                           (agent_ref is not None and agent_ref != tie["agent_ref"])
        host_session_ref, agent_ref = tie["session_ref"], tie["agent_ref"]
        if agent_ref is None and delegation_mode != SINGLE_AGENT and needs_hook_tie(host, run_policy):
            untied = main_thread_record(host, lease.get("principal"), session_ref=host_session_ref)
    elif not native_scope and needs_hook_tie(host, run_policy):
        untied = untied_record(host, lease.get("principal"), session_ref=host_session_ref, agent_ref=agent_ref)
    found = _observe(conn, task=task, lease=lease, host=host, session_ref=host_session_ref,
                     agent_ref=agent_ref, reported=reported, untied=untied is not None,
                     _codex_prepared=_codex_prepared, **_record_for(tie, untied))
    if found.get("native_identity"):
        host, host_session_ref, agent_ref = "codex", found["native_identity"]["session_ref"], found["native_identity"]["agent_ref"]
    corroborated = hook_saw_agent(conn, host=host, session_ref=host_session_ref, agent_ref=agent_ref)
    observed = _observed_view(found, corroborated=corroborated)

    observed_id = None
    previous = latest_state(conn, task_id=task_id, lease_id=lease_id, agent_ref=agent_ref)
    if found["usable"] and can_follow(previous, STATE_OBSERVED_STARTED):
        observed_id = _write_observed(
            conn, found=found, task_id=task_id, lease_id=lease_id, host=host, session_ref=host_session_ref,
            agent_ref=agent_ref, state=STATE_OBSERVED_STARTED, observed=observed,
        )
    report_id = write_observation(
        conn, task_id=task_id, lease_id=lease_id, host=host, state=STATE_REPORTED_RUNNING,
        source=SOURCE_REPORT, evidence_level=evidence_level, host_session_ref=host_session_ref,
        agent_ref=agent_ref, delegation_mode=delegation_mode, reported_model=model, reported_effort=effort,
        policy_verdict=found["verdict"]["policy_verdict"],
        payload={"actor": actor, "observation": observed_id, "reasons": found["verdict"]["reasons"],
                 "judged_as": found["judged_host"], "tied_by_host_hook": tie is not None,
                 "said_about_identity": said if identity_differs else None,
                 **({"tie_borne_out_by": tie["borne_out_by"]} if tie and tie.get("borne_out_by") else {})},
    )
    answer = _answer(found, observation_id=observed_id or report_id,
                     action=found["verdict"]["bind_action"], observed=observed, reported=reported)
    answer["identity"] = {"tied_by_host_hook": tie is not None, "host_session_ref": host_session_ref,
                          "agent_ref": agent_ref, "report_differs": identity_differs}
    if tie and tie.get("borne_out_by"):
        # Under the rule of r2.2a section 8.2: what in the host's own record the tie rests on.
        answer["identity"]["borne_out_by"] = tie["borne_out_by"]
    return answer


def verdict_for(conn: sqlite3.Connection, *, task_id: str, lease_id: str, _codex_prepared: Any = None) -> dict[str, Any]:
    """See :func:`gx_core.hostobs.policy.verdict_for`."""
    from .native_context import is_native_run
    owner = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if owner and is_native_run(conn, owner["run_id"]):
        from .native_observation import verdict_for as protocol_verdict
        return protocol_verdict(conn, task_id=task_id, lease_id=lease_id, _codex_prepared=_codex_prepared)
    task, lease = _task_and_lease(conn, task_id, lease_id)
    binding = binding_of(conn, task_id=task_id, lease_id=lease_id)
    reported = None
    host = session_ref = agent_ref = None
    if binding is not None:
        host, session_ref, agent_ref = binding["host"], binding["host_session_ref"], binding["agent_ref"]
        reported = {"model": binding["reported_model"], "effort": binding["reported_effort"]}
    record_host = host or strictest_host([run_host_kind(conn, task.get("run_id"))])
    # The record judged is the one the host ties to this lease's subject, as at bind; a worker
    # that never bound is judged on it too. With no tie, nothing is read.
    run_policy, _run_sha = policy_of_run(conn, task.get("run_id"))
    from . import codex_worker_observation
    native_scope = codex_worker_observation.scoped_run(conn, task["run_id"])
    tie = None if native_scope else tie_of_subject(conn, host=record_host, subject=lease.get("principal"), policy_doc=run_policy)
    untied = None
    if tie is not None:
        session_ref, agent_ref = tie["session_ref"], tie["agent_ref"]
        declared = binding["delegation_mode"] if binding is not None else None
        if agent_ref is None and declared != SINGLE_AGENT and needs_hook_tie(record_host, run_policy):
            untied = main_thread_record(record_host, lease.get("principal"), session_ref=session_ref)
    elif not native_scope and needs_hook_tie(record_host, run_policy):
        untied = untied_record(record_host, lease.get("principal"), session_ref=session_ref, agent_ref=agent_ref)
    found = _observe(conn, task=task, lease=lease, host=record_host, session_ref=session_ref,
                     agent_ref=agent_ref, reported=reported, until=lease.get("released_at"), untied=untied is not None,
                     _codex_prepared=_codex_prepared, **_record_for(tie, untied))
    if found.get("native_identity"):
        record_host, session_ref, agent_ref = "codex", found["native_identity"]["session_ref"], found["native_identity"]["agent_ref"]
    corroborated = hook_saw_agent(conn, host=record_host, session_ref=session_ref, agent_ref=agent_ref)
    observed = _observed_view(found, corroborated=corroborated)

    def report(state: str, event: str) -> str:
        return write_observation(
            conn, task_id=task_id, lease_id=lease_id, host=record_host, state=state,
            source=SOURCE_REPORT, evidence_level=LEVEL_REPORTED, host_session_ref=session_ref,
            agent_ref=agent_ref, policy_verdict=found["verdict"]["policy_verdict"],
            payload={"event": event, "bound": binding is not None,
                     "reasons": found["verdict"]["reasons"], "judged_as": found["judged_host"]},
        )

    # The rows of one lease are a path of the machine. A submission is the worker saying it
    # has finished; one that arrives with no bind before it is first recorded as the worker
    # saying it is there, because that is all the backend has been told.
    previous = latest_state(conn, task_id=task_id, lease_id=lease_id, agent_ref=agent_ref)
    observation_id = None
    if previous is None:
        report(STATE_REPORTED_RUNNING, "submitted without binding")
        previous = STATE_REPORTED_RUNNING
    if previous in (STATE_OBSERVED_STARTED, STATE_REPORTED_RUNNING) or (
        previous == STATE_REPORTED_FINISHED and not found["usable"]
    ):
        observation_id = report(STATE_REPORTED_FINISHED, "submitted")
        previous = STATE_REPORTED_FINISHED
    if found["usable"] and can_follow(previous, STATE_OBSERVED_FINISHED):
        observation_id = _write_observed(
            conn, found=found, task_id=task_id, lease_id=lease_id, host=record_host, session_ref=session_ref,
            agent_ref=agent_ref, state=STATE_OBSERVED_FINISHED, observed=observed,
        )
    if observation_id is None:
        # The lease already ended in a state nothing follows (the host showed the worker
        # finish or fail). The verdict is still answered; the row it points at is that one.
        observation_id = conn.execute(
            "SELECT worker_observation_id FROM worker_observations WHERE task_id=? AND lease_id=?"
            " ORDER BY rowid DESC LIMIT 1", (task_id, lease_id),
        ).fetchone()["worker_observation_id"]
    answer = _answer(found, observation_id=observation_id, action=found["verdict"]["submit_action"],
                     observed=observed, reported=reported)
    answer["bound"] = binding is not None
    return answer


# ------------------------------------------- what core asks about a lease (design r2.2 section 8.2)

#: The hook events in which the host says a worker stopped, and what each is called as a cause.
STOP_CAUSES = {"SubagentStop": "subagent_stop", "StopFailure": "stop_failure", "SessionEnd": "session_end"}


def _tie_of_lease(conn: sqlite3.Connection, task: Mapping[str, Any], lease: Mapping[str, Any]) -> tuple[
        str | None, dict[str, Any] | None, dict[str, Any]]:
    """``(host, tie, policy)`` for a lease: the agent the host ties its subject to, under the run's own policy."""
    policy_doc, _sha = policy_of_run(conn, task.get("run_id"))
    run_host = run_host_kind(conn, task.get("run_id"))
    hosts = [run_host] if run_host else [name for name in contracts.label_values("host_kind")
                                         if needs_hook_tie(name, policy_doc)]
    for host in hosts:
        tie = tie_of_subject(conn, host=host, subject=lease.get("principal"), policy_doc=policy_doc)
        if tie is not None:
            return host, tie, policy_doc
    return (hosts[0] if hosts else None), None, policy_doc


def _last_call_row(conn: sqlite3.Connection, host: str, subject: str, session_ref: str, agent_ref: str | None) -> int:
    """The row of the last call of the backend a hook reported for this agent under this subject (0: none)."""
    last = 0
    for row in _client_call_rows(conn, host, "session_ref=? AND agent_ref IS ?", (session_ref, agent_ref)):
        if subject in _subjects_in(row):
            last = row["row_no"]
    return last


def _stop_event(conn: sqlite3.Connection, host: str, session_ref: str, agent_ref: str | None, *,
                after_row: int = 0) -> dict[str, Any] | None:
    """The latest event in which a hook said this agent stopped: its own stop, or the end of its session.

    Only an event written after ``after_row`` counts: a stop reported before the holder's last
    call of the backend is not the stop of this holder (it called again; a session that ended
    was resumed). Rows are compared by the order they were written, never by their timestamps,
    which have millisecond grain.
    """
    marks = ",".join("?" * len(STOP_CAUSES))
    for row in conn.execute(
        f"SELECT event_id, hook_event, agent_ref, received_at FROM host_events WHERE host=? AND session_ref=?"
        f" AND source=? AND hook_event IN ({marks}) AND rowid>? ORDER BY rowid DESC",
        (host, session_ref, SOURCE_HOOK, *STOP_CAUSES, after_row),
    ):
        if row["hook_event"] == "SessionEnd" or row["agent_ref"] == agent_ref:
            return {"event_id": row["event_id"], "hook_event": row["hook_event"], "received_at": row["received_at"]}
    return None


def holder_stopped(conn: sqlite3.Connection, *, lease_id: str) -> dict[str, Any]:
    """See :func:`gx_core.hostobs.policy.holder_stopped`."""
    lease = _row(conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone())
    if lease is None:
        raise ArtifactNotFound(f"no such lease {lease_id!r}", details={"lease_id": lease_id})
    task = _row(conn.execute("SELECT * FROM tasks WHERE task_id=?", (lease["task_id"],)).fetchone()) or {}
    from .native_context import is_native_run
    if task.get("run_id") and is_native_run(conn, task["run_id"]):
        return {"stopped": False, "cause": None, "host_event_id": None, "agent": None,
                "reason": "official turn completion/idle is not worker termination; no private-record stop inference"}
    out: dict[str, Any] = {"stopped": False, "cause": None, "host_event_id": None, "agent": None, "reason": None}
    host, tie, policy_doc = _tie_of_lease(conn, task, lease)
    if tie is None:
        out["reason"] = (f"the host ties the lease's subject {lease.get('principal')!r} to none of its agents, so no "
                         "record of the host says its holder stopped")
        return out
    session_ref, agent_ref = tie["session_ref"], tie["agent_ref"]
    out["agent"] = f"{host}:{session_ref}:{agent_ref or 'main'}"
    event = _stop_event(conn, host, session_ref, agent_ref,
                        after_row=_last_call_row(conn, host, lease["principal"], session_ref, agent_ref))
    if event is None:
        out["reason"] = (f"no hook of the host has said that {out['agent']} stopped since it last called the backend "
                         "under the lease's subject")
        return out
    out.update(cause=STOP_CAUSES[event["hook_event"]], host_event_id=event["event_id"])
    # A stop event is a hook's proposal. What bears it out is the agent's own record at the
    # moment of asking: a worker that is still working contradicts it with its next turn.
    record = tie.get("record") or read_worker_record(host, session_ref, agent_ref, policy_doc)
    if record is None or not record.readable:
        out["reason"] = (f"the host's record of {out['agent']} cannot be read"
                         + (f": {record.reason}" if record is not None and record.reason else "")
                         + "; a stop is not taken on a hook's word")
        return out
    later = [turn.at for turn in record.turns if turn.at > event["received_at"]]
    if later:
        out["reason"] = (f"the record of {out['agent']} holds {len(later)} turn(s) after the stop a hook reported at "
                         f"{event['received_at']}, the last at {later[-1]}: it has not stopped")
        return out
    if agent_ref is not None and record.meta.get("toolUseId") and record.meta.get("finished") is not True:
        out["reason"] = (f"the host's record of {out['agent']}, an agent started with the Agent tool, does not say it "
                         "finished")
        return out
    out["stopped"] = True
    out["reason"] = (f"a hook reported {event['hook_event']} at {event['received_at']} and the record of "
                     f"{out['agent']} holds no turn after it")
    return out


def stopped_holders(conn: sqlite3.Connection, *, run_id: str) -> list[dict[str, Any]]:
    """See :func:`gx_core.hostobs.policy.stopped_holders`."""
    found: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT l.lease_id AS lease_id, l.task_id AS task_id FROM leases l JOIN tasks t ON t.task_id=l.task_id"
        " WHERE t.run_id=? AND l.released_at IS NULL ORDER BY l.issued_at, l.lease_id", (run_id,),
    ).fetchall():
        seen = holder_stopped(conn, lease_id=row["lease_id"])
        if seen["stopped"]:
            found.append({"lease_id": row["lease_id"], "task_id": row["task_id"], "agent": seen["agent"],
                          "cause": seen["cause"], "host_event_id": seen["host_event_id"]})
    return found


def record_verdict_for_lease(conn: sqlite3.Connection, *, task_id: str, lease_id: str, _codex_prepared: Any = None) -> dict[str, Any]:
    """See :func:`gx_core.hostobs.policy.record_verdict_for_lease`."""
    from .native_context import is_native_run
    owner = conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if owner and is_native_run(conn, owner["run_id"]):
        from .native_observation import verdict_for as protocol_verdict
        return protocol_verdict(conn, task_id=task_id, lease_id=lease_id, _codex_prepared=_codex_prepared, inspect=True)
    task, lease = _task_and_lease(conn, task_id, lease_id)
    binding = binding_of(conn, task_id=task_id, lease_id=lease_id)
    reported = {"model": binding["reported_model"], "effort": binding["reported_effort"]} if binding else None
    host = (binding["host"] if binding else None) or strictest_host([run_host_kind(conn, task.get("run_id"))])
    run_policy, _sha = policy_of_run(conn, task.get("run_id"))
    from . import codex_worker_observation
    native_scope = codex_worker_observation.scoped_run(conn, task["run_id"])
    tie = None if native_scope else tie_of_subject(conn, host=host, subject=lease.get("principal"), policy_doc=run_policy)
    session_ref = agent_ref = None
    untied = None
    if tie is not None:
        session_ref, agent_ref = tie["session_ref"], tie["agent_ref"]
        declared = binding["delegation_mode"] if binding else None
        if agent_ref is None and declared != SINGLE_AGENT and needs_hook_tie(host, run_policy):
            untied = main_thread_record(host, lease.get("principal"), session_ref=session_ref)
    elif not native_scope and needs_hook_tie(host, run_policy):
        untied = untied_record(host, lease.get("principal"), session_ref=None, agent_ref=None)
    found = _observe(conn, task=task, lease=lease, host=host, session_ref=session_ref, agent_ref=agent_ref,
                     reported=reported, until=lease.get("released_at"), untied=untied is not None,
                     _codex_prepared=_codex_prepared, **_record_for(tie, untied))
    if found.get("native_identity"):
        host, session_ref, agent_ref = "codex", found["native_identity"]["session_ref"], found["native_identity"]["agent_ref"]
    observation_id = None
    previous = latest_state(conn, task_id=task_id, lease_id=lease_id, agent_ref=agent_ref)
    if found["usable"] and can_follow(previous, STATE_OBSERVED_FINISHED):
        observation_id = _write_observed(
            conn, found=found, task_id=task_id, lease_id=lease_id, host=host, session_ref=session_ref,
            agent_ref=agent_ref, state=STATE_OBSERVED_FINISHED,
            observed=_observed_view(found, corroborated=hook_saw_agent(
                conn, host=host, session_ref=session_ref, agent_ref=agent_ref)),
        )
    verdict, summary = found["verdict"], found["summary"]
    return {
        "policy_verdict": verdict["policy_verdict"], "worker_observation_id": observation_id,
        "waits_for_user": verdict["waits_for_user"], "failure_class": verdict["failure_class"],
        "refusal": verdict["policy_verdict"] == policy.REFUSAL_OR_FALLBACK,
        "readable": bool(summary and summary["readable"]),
        "tied": bool(found.get("claim_event_id")) if native_scope else tie is not None and untied is None,
        "deviation": found["deviation"], "reasons": list(verdict["reasons"]),
    }


# ------------------------------------------------- a worker's whole record, when it is read

def observe_worker(
    conn: sqlite3.Connection, *, host: str, session_ref: str, agent_ref: str | None,
    record: Record | None, hook: str,
) -> dict[str, Any]:
    """Judge every lease and every spawned-for task of one worker against its record.

    Called when the host says a worker stopped and when a whole session is read. For each
    lease the worker bound it appends ``observed_finished`` with the verdict for that
    lease's period; for each task the host tied to the agent at spawn it appends
    ``observed_finished`` judged on the whole record. A worker with neither is still judged,
    on its whole record against the floor of the policy, because the user's rule covers
    every agent.
    """
    policy_doc, _sha = policy.agent_policy()
    floor = policy.requirement_for_spawn(policy=policy_doc)
    whole = None
    if record is not None:
        # The record view: each turn against the pin the user had set when it was served
        # (pinned_model.history), and the models the user allowed beside that pin from then on
        # (also_allowed). A change the history records is the user's, not drift; which task a
        # turn belonged to, and so which one model it had to run on, is the run view's question.
        whole = summarise(record, required_model=policy.model_at(policy_doc), allowed_efforts=floor["allowed_efforts"])
    judged: list[dict[str, Any]] = []
    written = 0

    leases = conn.execute(
        "SELECT DISTINCT task_id, lease_id FROM worker_observations WHERE host=? AND agent_ref IS ?"
        " AND host_session_ref=? AND lease_id IS NOT NULL ORDER BY rowid",
        (host, agent_ref, session_ref),
    ).fetchall()
    for row in leases:
        task, lease = _task_and_lease(conn, row["task_id"], row["lease_id"])
        binding = binding_of(conn, task_id=row["task_id"], lease_id=row["lease_id"])
        reported = {"model": binding["reported_model"], "effort": binding["reported_effort"]} if binding else None
        found = _observe(conn, task=task, lease=lease, host=host, session_ref=session_ref, agent_ref=agent_ref,
                         reported=reported, until=lease.get("released_at"), record=record)
        previous = latest_state(conn, task_id=row["task_id"], lease_id=row["lease_id"], agent_ref=agent_ref)
        observation_id = None
        if found["usable"] and can_follow(previous, STATE_OBSERVED_FINISHED):
            observation_id = _write_observed(
                conn, found=found, task_id=row["task_id"], lease_id=row["lease_id"], host=host,
                session_ref=session_ref, agent_ref=agent_ref, state=STATE_OBSERVED_FINISHED,
                observed=_observed_view(found, corroborated=hook_saw_agent(
                    conn, host=host, session_ref=session_ref, agent_ref=agent_ref)),
            )
            written += 1
        judged.append({"task_id": row["task_id"], "lease_id": row["lease_id"], "run_id": task.get("run_id"),
                       "policy_verdict": found["verdict"]["policy_verdict"],
                       "worker_observation_id": observation_id, "deviation": found["deviation"],
                       "required_model": found["required"]["model"]})

    spawned_for = conn.execute(
        "SELECT DISTINCT task_id FROM worker_observations WHERE host=? AND agent_ref IS ? AND host_session_ref=?"
        " AND lease_id IS NULL AND observation_source=? ORDER BY rowid",
        (host, agent_ref, session_ref, SOURCE_HOOK),
    ).fetchall() if agent_ref is not None else []
    for row in spawned_for:
        task = _row(conn.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone())
        required = policy.requirement_of_task(task, policy_doc)
        summary = None
        if record is not None:
            summary = summarise(record, required_model=required["model"], allowed_efforts=required["allowed_efforts"])
        verdict = policy.judge(host=strictest_host([host, run_host_kind(conn, task.get("run_id"))], policy_doc),
                               required=required, observed=summary, policy=policy_doc)
        previous = latest_state(conn, task_id=task["task_id"], lease_id=None, agent_ref=agent_ref)
        observation_id = None
        if summary and summary["readable"] and summary["turns"] and can_follow(previous, STATE_OBSERVED_FINISHED):
            last = record.turns[-1]
            observation_id = write_observation(
                conn, task_id=task["task_id"], host=host, state=STATE_OBSERVED_FINISHED,
                source=SOURCE_TRANSCRIPT, evidence_level=LEVEL_OBSERVED, host_session_ref=session_ref,
                agent_ref=agent_ref, observed_ref=observed_worker_ref(record), observed_model=last.model,
                observed_effort=last.effort, turns_observed=summary["turns"],
                turns_off_policy=summary["turns_off_policy"], transcript_sha256=summary["record_sha256"],
                host_version=summary["host_version"], policy_verdict=verdict["policy_verdict"],
                payload={"hook": hook, "models": summary["models"], "efforts": summary["efforts"],
                         "reasons": verdict["reasons"], "whole_record": True},
            )
            written += 1
        deviation = None
        if summary and verdict["waits_for_user"]:
            deviation = deviation_for_report(summary, required_model=required["model"])
        judged.append({"task_id": task["task_id"], "lease_id": None, "run_id": task.get("run_id"),
                       "policy_verdict": verdict["policy_verdict"], "worker_observation_id": observation_id,
                       "deviation": deviation, "required_model": required["model"]})

    overall = policy.judge(host=host, required={**floor, "task_class": None}, observed=whole, policy=policy_doc)
    return {"record": record, "whole": whole, "floor": floor, "judged": judged, "written": written,
            "whole_verdict": overall["policy_verdict"], "whole_reasons": overall["reasons"],
            "whole_failure_class": overall["failure_class"]}


def session_of_run(conn: sqlite3.Connection, run_id: str) -> tuple[str | None, str | None]:
    """``(host, session_ref)`` of the session that holds a run's coordinator lease."""
    row = conn.execute("SELECT host, session_ref FROM coordinator_leases WHERE run_id=?", (run_id,)).fetchone()
    return (row["host"], row["session_ref"]) if row is not None else (None, None)


def observe_session(
    conn: sqlite3.Connection, *, host: str, session_ref: str | None = None, run_id: str | None = None,
    actor: str = "host_adapter",
) -> dict[str, Any]:
    """Read everything the host recorded about a session's workers. The body of ``gx_host_observe``.

    Answers with one entry per worker and one violation per refusal, per fallback and per
    worker with turns off the policy, and writes what it found: a derived ``host_events`` row
    per worker and per deviation (the same finding is written once however often the session
    is read) and ``observed_finished`` rows for the leases those workers held.
    """
    from ..errors import ClientCapabilityMissing

    if host not in contracts.label_values("host_kind"):
        raise SchemaInvalid(f"{host!r} is not a host kind", details={"allowed": contracts.label_values("host_kind")})
    policy_doc, policy_sha256 = policy.agent_policy()
    spec = policy.reader_spec(host, policy_doc)
    if host != HOST_CLAUDE_CODE or spec is None:
        raise ClientCapabilityMissing(
            f"the backend has no reader for the records of a {host} host: none has been verified "
            "against a real session of it. Its workers are judged on their reports.",
            details={"host": host, "readers": [HOST_CLAUDE_CODE]},
        )
    if session_ref is None and run_id is not None:
        lease_host, session_ref = session_of_run(conn, run_id)
        if lease_host is not None and lease_host != host:
            session_ref = None
    if session_ref is None:
        raise SchemaInvalid(
            "name the session to read, or a run whose coordinator lease names one",
            details={"run_id": run_id},
        )
    if not claude_code.valid_session_ref(session_ref):
        raise SchemaInvalid("session_ref is not a session identifier", details={"session_ref": str(session_ref)[:64]})

    answer: dict[str, Any] = {"observations": 0, "workers": [], "violations": [], "host_version": None,
                              "readable": False, "session_ref": session_ref, "reason": None}
    main = claude_code.locate_session(session_ref)
    if main is None:
        answer["reason"] = f"the host holds no record of session {session_ref} under its records directory"
        return answer
    session = claude_code.read_session(main, verified_versions=spec["verified_on_versions"],
                                       allowed=claude_code.Fields.from_spec(spec))
    versions: set[str] = set()
    for record in session.records:
        versions.update(record.versions)
        name = record.worker_name or ("main session" if record.agent_ref is None else record.agent_ref)
        seen = observe_worker(conn, host=host, session_ref=session_ref, agent_ref=record.agent_ref,
                              record=record, hook="gx_host_observe")
        answer["observations"] += seen["written"]
        whole = seen["whole"]
        worker = {
            "worker": name, "agent_ref": record.agent_ref, "agent_type": record.meta.get("agentType"),
            "parent_agent_ref": record.meta.get("parentAgentId"), "requested_model": record.meta.get("model"),
            "readable": record.readable, "reason": record.reason, "host_version": record.host_version,
            "turns": whole["turns"], "responses": whole["responses"], "models": whole["models"],
            "efforts": whole["efforts"], "turns_off_policy": whole["turns_off_policy"],
            "turns_off_model": whole["turns_off_model"],
            "first_turn_at": whole["first_turn_at"], "last_turn_at": whole["last_turn_at"],
            "off_policy_ongoing": whole["off_policy_ongoing"],
            "policy_verdict": seen["whole_verdict"],
            "tasks": [{key: item[key] for key in ("task_id", "lease_id", "policy_verdict")} for item in seen["judged"]],
        }
        answer["workers"].append(worker)
        run_ref = run_id or next((item["run_id"] for item in seen["judged"] if item["run_id"]), None)
        last = record.turns[-1] if record.readable and record.turns else None
        _id, written = record_event(
            conn, host=host, hook_event="TranscriptObserved", session_ref=session_ref, agent_ref=record.agent_ref,
            source=SOURCE_TRANSCRIPT, model=last.model if last else None, effort=last.effort if last else None,
            matched_task_id=next((item["task_id"] for item in seen["judged"]), None), run_id=run_ref,
            payload={key: worker[key] for key in ("worker", "agent_type", "parent_agent_ref", "requested_model",
                                                    "readable", "reason", "host_version", "turns", "responses",
                                                    "models", "efforts", "turns_off_policy", "first_turn_at",
                                                    "last_turn_at", "policy_verdict")}
            | {"record_sha256": record.sha256, "policy_sha256": policy_sha256, "actor": actor},
            event_id=derived_event_id(host, session_ref, record.agent_ref, "TranscriptObserved", record.sha256, policy_sha256),
        )
        answer["observations"] += int(written)
        # An unreadable record has no deviation and no turn in its summary: nothing is
        # concluded from it in either direction.
        for deviation in whole["deviations"]:
            answer["violations"].append({
                "kind": deviation["kind"], "worker": name, "agent_ref": record.agent_ref,
                "when": deviation["at"], "from_model": deviation["from_model"] or policy.pin_at(deviation["at"], policy_doc)["model"],
                "to_model": deviation["to_model"], "category": deviation["category"],
                "request_id": deviation["request_id"],
            })
            _id, written = record_event(
                conn, host=host, hook_event="ModelFallback" if deviation["kind"] == "fallback" else "ModelRefusal",
                session_ref=session_ref, agent_ref=record.agent_ref, source=SOURCE_TRANSCRIPT,
                model=deviation["to_model"], run_id=run_ref,
                reason=f"{deviation['kind']} at {deviation['at']}: "
                       f"{deviation['from_model'] or policy.pin_at(deviation['at'], policy_doc)['model']} -> "
                       f"{deviation['to_model'] or 'no other model'}, category {deviation['category']}, "
                       f"request {deviation['request_id']}",
                payload={**deviation, "worker": name, "actor": actor},
                event_id=derived_event_id(host, session_ref, record.agent_ref, deviation["kind"],
                                          deviation["at"], deviation["request_id"]),
            )
            answer["observations"] += int(written)
        # One entry per stretch: when it began, its last off-policy turn, and whether a later
        # turn ended it. A session that fell back and returned is not still falling back.
        for stretch in whole["stretches"]:
            began = stretch["fallback"] or {}
            answer["violations"].append({
                "kind": "off_policy_turns", "worker": name, "agent_ref": record.agent_ref,
                "when": stretch["first_at"], "until": stretch["last_at"],
                "ended": stretch["ended"],
                "back_on_policy_at": stretch["ended_at"] if stretch["back_on_policy"] else None,
                "cause": stretch["cause"], "cause_at": began.get("at") or stretch["announced_at"],
                "from_model": stretch["required_model"],
                "to_model": stretch["model"] if stretch["model"] != stretch["required_model"] else None,
                "turns": stretch["turns"], "turns_off_model": stretch["turns_off_model"],
                "turns_below_effort": stretch["turns_below_effort"], "of_turns": whole["turns"],
                "efforts": stretch["efforts"], "category": began.get("category"),
                "request_id": began.get("request_id") or stretch["request_id"],
            })
    answer["host_version"] = next(iter(versions)) if len(versions) == 1 else None
    answer["versions"] = sorted(versions)
    answer["readable"] = all(record.readable for record in session.records)
    if not answer["readable"]:
        unreadable = [record.worker_name or record.agent_ref or "main session"
                      for record in session.records if not record.readable]
        answer["reason"] = f"{len(unreadable)} of {len(session.records)} record(s) are not readable: " + ", ".join(unreadable[:8])
    return answer
