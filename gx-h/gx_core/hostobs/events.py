"""Events the host itself raised, as its hooks delivered them, and what the backend answers.

A hook is a command the host runs at a point of its own lifecycle. The host adapter's hook
client reduces the hook's input to identifiers and settings and calls ``gx_host_event``; this
module is what that tool does with it.

It keeps nothing but identifiers and settings
---------------------------------------------
:func:`project_payload` copies, per event, a fixed list of named fields with a fixed type
each. A spawn's prompt, a subagent's last message, an error's text are not on any list, so a
caller that sends them - a hook client with a bug, or a shell calling the tool directly -
still leaves nothing of them in ``host_events``. The answer says how many fields were left
out.

It answers the events the host lets a hook decide
--------------------------------------------------
``PreToolUse`` of the ``Agent`` tool   deny a spawn that does not name the model its work
                                       requires - its task's, its role's class's, or for a
                                       spawn tied to neither one the policy allows now - at an
                                       allowed effort (``policy.judge_spawn``).
``PreToolUse`` of the shell tool       when the command runs this backend's client: record
                                       which agent the host says is running it under which
                                       subject (the tie governance reads), and deny a call
                                       that claims the host adapter's own scope, or - from a
                                       subagent - any scope but a worker's.
``PreToolUse`` of the ``Workflow`` tool  a Workflow script starts its agents without an Agent
                                       tool call, so no spawn gate sees them (observed on
                                       Claude Code 2.1.322). GX roles are started only by the
                                       saved ``gx-dispatch`` this tree ships: its bytes must be
                                       the shipped ones and every row it is handed must name
                                       its own task's model and an allowed effort; rows of
                                       one launch may name different models. Any other
                                       workflow that would start a GX role is denied. An
                                       admitted launch is a hand-out (:mod:`.dispatch`): its
                                       rows are offered to nobody else while it runs, and a
                                       launch that repeats them is denied.
``PreModelSwitch``                     ask the person at the keyboard before the session leaves
                                       the pin: the pin is the model of the session itself,
                                       whatever model a class of work runs on.
``Stop``                               deny ending the turn while a run this session
                                       coordinates has READY tasks that no running dispatch
                                       holds (design r2 §8.6). A running gx-dispatch of the
                                       session, an open hand-out, or a pending model approval
                                       of the run lets it stop; a Monitor or any other
                                       background task does not.

``allow`` means the policy has no objection. It is never a grant: the hook client maps it to
"no decision", so the host's own permission flow still applies.

A hook event is a proposal
--------------------------
``gx_host_event`` is guarded by a scope any shell on the machine can name, so nothing a hook
sends proves who sent it (design r2.2a section 8.2). Under a policy whose reader has the rule
for reading a subject from an agent's meta, an event therefore moves state only where the
host's own record agrees with it:

a tie            the agent's own meta names the subject (asked again, with the agent's turns,
                 where the tie is used: ``observation.tie_of_subject``);
a spawn          the observation that an agent was started for a task is written when the
                 agent's meta shows the same agent type and tool-use id;
a hand-out close when the host's record of the session holds the ended workflow run;
a stop           decided when it is asked for (``policy.holder_stopped``), never here.

What the record does not bear out is kept in ``host_events`` with ``effect`` ``reported_only``
and each withheld effect named; it changes nothing. Decisions (a gate's allow or deny) and
plain records are ``applied``. Before the rule - a policy from before it - the hook's word is
taken as it was.

For the rest it records, and where the event names a worker the backend knows, it observes:
``PostToolUse`` of ``Agent`` ties a task to the agent the host started for it and the model
the host resolved; ``SubagentStop`` reads the worker's own record and judges every lease it
held; ``PostModelSwitch`` is how a fallback that the host made on its own becomes known.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from .. import contracts
from ..domain import clock, machines
from ..errors import SchemaInvalid
from ..persistence import store
from . import claude_code, dispatch, observation, policy
from .record import deviation_for_report

EVENT_PRE_TOOL = "PreToolUse"
EVENT_POST_TOOL = "PostToolUse"
EVENT_SUBAGENT_START = "SubagentStart"
EVENT_SUBAGENT_STOP = "SubagentStop"
EVENT_STOP = "Stop"
EVENT_PRE_SWITCH = "PreModelSwitch"
EVENT_POST_SWITCH = "PostModelSwitch"
EVENT_POST_TOOL_FAILURE = "PostToolUseFailure"
EVENT_SESSION_START = "SessionStart"
EVENT_SESSION_END = "SessionEnd"
TOOL_SPAWN = "Agent"
TOOL_SHELL = "Bash"
TOOL_WORKFLOW = "Workflow"
SCOPE_HOST_ADAPTER = "host_adapter"
#: The saved Workflow script that dispatches a round of a GX run, by the name in its ``meta``
#: and the file this tree ships. The installer copies the file unchanged into a project's
#: ``.claude/workflows/``; the hook hashes the copy the host is about to run.
DISPATCH_WORKFLOW = "gx-dispatch"
DISPATCH_SCRIPT = contracts.HARNESS_ROOT / "adapters" / "hosts" / "claude_code" / "workflows" / "gx-dispatch.js"
#: The scopes a subagent may call the backend under: a worker's, or one that only reads.
SUBAGENT_SCOPES = ("worker", "readonly")

DECISION_ALLOW, DECISION_DENY, DECISION_ASK = policy.SPAWN_ALLOW, policy.SPAWN_DENY, policy.SPAWN_ASK

#: What an event did (contract r5, label axis host_event_effect): everything it means for
#: state was applied, or some of it was kept as the adapter's report because the host's own
#: record does not bear it out.
EFFECT_APPLIED, EFFECT_REPORTED = "applied", "reported_only"
if "host_event_effect" in contracts.labels():
    for _value in (EFFECT_APPLIED, EFFECT_REPORTED):
        contracts.require_label("host_event_effect", _value)
    del _value
#: The outbox topics this module writes (design r2.2 section 2.4.1), and why a worker stopped.
TOPIC_WORKER_STOPPED, TOPIC_WORKER_VERDICT = "worker.stopped", "worker.verdict"
CAUSE_SUBAGENT_STOP, CAUSE_STOP_FAILURE, CAUSE_SESSION_END = "subagent_stop", "stop_failure", "session_end"
EVENT_STOP_FAILURE = "StopFailure"

TASK_READY = "READY"
if TASK_READY not in machines.all_states("task"):
    raise RuntimeError("READY is not a task state in contracts/states.json")
_RUN_ENDED = set(machines.terminal_states("run"))
#: A run in one of these states hands out nothing; the coordinator ending its turn is the
#: right thing to happen. A run that waits for one approval still hands out what does not
#: depend on it (SCHEDULING_DESIGN §2: dispatch continues in every waiting state but PAUSED),
#: so WAITING_APPROVAL with READY tasks keeps the coordinator at its post.
_RUN_NOT_DRIVEN = _RUN_ENDED | {"PAUSED"}
for _state in ("PAUSED", "WAITING_APPROVAL"):
    if _state not in machines.all_states("run"):
        raise RuntimeError(f"{_state} is not a run state in contracts/states.json")
del _state
#: A run that waits on this kind of approval may let its coordinator stop (§8.6): what it
#: waits for is the user, and no row it hands out would be admitted until they decide.
MODEL_APPROVAL_KIND = contracts.require_label("approval_action_kind", "model_switch")
APPROVAL_PENDING = "PENDING"
if APPROVAL_PENDING not in machines.all_states("approval"):
    raise RuntimeError("PENDING is not an approval state in contracts/states.json")

_NAME = "name"          # a short identifier or setting value
_PATH = "path"          # a path, kept only if it lies in the host's own records directory
_FLAG = "flag"
_COUNT = "count"
_NAMES = "names"        # a short list of names
_TASKS = "tasks"        # a list of task ids
_ROWS = "rows"          # the rows of a dispatch plan a workflow is handed: ids and settings only
_FILES = "files"        # GX files a session started with: scope, kind, file name, sha256

MAX_NAME_CHARS = 128
MAX_PATH_CHARS = 1024
MAX_LIST = 64
MAX_ROWS = 256
#: What is kept of one row of a dispatch plan, with the kind of each value.
_ROW_FIELDS = {"task_id": _TASKS, "role": _NAME, "model": _NAME, "effort": _NAME, "attempt": _COUNT}

_COMMON = {"agent_type": _NAME, "permission_mode": _NAME, "effort_level": _NAME, "transcript_path": _PATH}
_SPAWN_REQUEST = {
    "tool_name": _NAME, "tool_use_id": _NAME, "subagent_type": _NAME, "spawn_model": _NAME,
    "spawn_effort": _NAME, "spawn_name": _NAME, "run_in_background": _FLAG, "isolation": _NAME,
    "gx_task_ids": _TASKS, "definition_model": _NAME, "definition_effort": _NAME,
    "subagent_model_env": _NAME, "subagent_model_force": _FLAG, "effort_env": _NAME,
}
#: A call of the backend's client seen by a hook on the shell tool: who it ran as and what it
#: named. The command line itself is never sent and has no field here.
_CLIENT_CALL = {
    "tool_name": _NAME, "tool_use_id": _NAME, "gx_calls": _COUNT, "gx_subjects": _NAMES, "gx_scopes": _NAMES,
    "gx_commands": _NAMES, "gx_tools": _NAMES, "gx_task_ids": _TASKS, "gx_lease_refs": _NAMES,
}
_SPAWN_RESULT = {
    "tool_name": _NAME, "tool_use_id": _NAME, "subagent_type": _NAME, "gx_task_ids": _TASKS,
    "status": _NAME, "spawned_agent_id": _NAME, "resolved_model": _NAME, "models_used": _NAMES,
    "total_tokens": _COUNT, "total_duration_ms": _COUNT, "total_tool_use_count": _COUNT,
}
_SWITCH = {"from_model": _NAME, "to_model": _NAME, "requested_model": _NAME, "switch_source": _NAME,
           "context_tokens": _COUNT}
#: A call of the Workflow tool that could start GX roles, as the hook reduced it: which saved
#: workflow (or that the script was inline or given by path), the sha256 of the script the host
#: will run, the GX agent types the script names, and the rows of the plan it was handed.
_WORKFLOW_CALL = {
    "tool_name": _NAME, "tool_use_id": _NAME, "workflow_name": _NAME, "workflow_inline": _FLAG,
    "workflow_by_path": _FLAG, "workflow_sha256": _NAME, "workflow_source": _NAME, "gx_agent_types": _NAMES,
    "dispatch_rows": _ROWS, "gx_task_ids": _TASKS, "dispatch_run_id": _NAME, "dispatch_session": _NAME,
    "dispatch_nonce": _NAME, "follow_depth": _COUNT,
}
#: What the host answered when it launched such a workflow: the run it made and the sha256 of
#: the copy of the script it keeps in its own records.
_WORKFLOW_LAUNCH = {"workflow_run_id": _NAME, "workflow_task_id": _NAME, "workflow_status": _NAME,
                    "script_copy_sha256": _NAME}
PAYLOAD_FIELDS: dict[str, dict[str, str]] = {
    EVENT_PRE_TOOL: {**_SPAWN_REQUEST, **_CLIENT_CALL, **_WORKFLOW_CALL},
    EVENT_POST_TOOL: {**_SPAWN_RESULT, **_WORKFLOW_LAUNCH, "workflow_name": _NAME, "workflow_sha256": _NAME},
    EVENT_POST_TOOL_FAILURE: {"tool_name": _NAME, "tool_use_id": _NAME, "subagent_type": _NAME, "gx_task_ids": _TASKS,
                              "workflow_name": _NAME},
    EVENT_SUBAGENT_START: {},
    EVENT_SUBAGENT_STOP: {"agent_transcript_path": _PATH, "stop_hook_active": _FLAG},
    EVENT_STOP: {"stop_hook_active": _FLAG, "background_tasks": _COUNT, "background_subagents": _COUNT,
                 "background_workflows": _NAMES, "background_workflow_ids": _NAMES},
    "StopFailure": {"error": _NAME},
    EVENT_SESSION_START: {"start_source": _NAME, "session_model": _NAME, "gx_files": _FILES,
                          "agents_dir_present": _FLAG, "workflows_dir_present": _FLAG},
    EVENT_SESSION_END: {"end_reason": _NAME},
    EVENT_PRE_SWITCH: _SWITCH,
    EVENT_POST_SWITCH: _SWITCH,
}

_EVENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TASK_RE = re.compile(r"^gxt_[0-9a-z]{18}$")

#: Worst first. What an event reports when it bears on several leases.
_SEVERITY = (policy.REFUSAL_OR_FALLBACK, policy.MISMATCH, policy.UNOBSERVABLE, policy.REPORTED_ONLY, policy.CONFORMS)


# ------------------------------------------------------------------------- projection

def project_payload(hook_event: str, payload: Any) -> tuple[dict[str, Any], int]:
    """``(what is kept, how many fields were left out)`` of a hook payload."""
    if not isinstance(payload, Mapping):
        return {}, 0
    fields = {**_COMMON, **PAYLOAD_FIELDS.get(hook_event, {})}
    kept: dict[str, Any] = {}
    for key, kind in fields.items():
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if kind == _NAME and isinstance(value, str) and 0 < len(value) <= MAX_NAME_CHARS:
            kept[key] = value
        elif kind == _FLAG and isinstance(value, bool):
            kept[key] = value
        elif kind == _COUNT and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            kept[key] = value
        elif kind == _NAMES and isinstance(value, list):
            names = [item for item in value[:MAX_LIST] if isinstance(item, str) and 0 < len(item) <= MAX_NAME_CHARS]
            if names:
                kept[key] = names
        elif kind == _TASKS and isinstance(value, list):
            tasks = [item for item in value[:MAX_LIST] if isinstance(item, str) and _TASK_RE.match(item)]
            if tasks:
                kept[key] = list(dict.fromkeys(tasks))
        elif kind == _PATH and isinstance(value, str) and 0 < len(value) <= MAX_PATH_CHARS:
            inside = claude_code.within_records(value)
            if inside is not None:
                kept[key] = str(inside)
        elif kind == _ROWS and isinstance(value, list):
            rows = [_project_row(item) for item in value[:MAX_ROWS]]
            rows = [row for row in rows if row is not None]
            if rows:
                kept[key] = rows
        elif kind == _FILES and isinstance(value, list):
            files = [item for item in (_project_file(entry) for entry in value[:MAX_LIST]) if item is not None]
            if files:
                kept[key] = files
    dropped = sum(1 for key, value in payload.items() if value is not None and key not in kept)
    return kept, dropped


_GX_FILE_RE = re.compile(r"^gx[-_][a-z0-9_-]{1,60}\.(?:md|js)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _project_file(entry: Any) -> dict[str, str] | None:
    """One GX file a session started with: where, what, its name and sha256; anything else is dropped."""
    if not isinstance(entry, Mapping):
        return None
    scope, kind, name, sha = (entry.get(key) for key in ("scope", "kind", "name", "sha256"))
    if scope not in ("project", "user") or kind not in ("role", "workflow") or not isinstance(name, str) \
            or not _GX_FILE_RE.match(name) or not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return None
    return {"scope": scope, "kind": kind, "name": name, "sha256": sha}


def _project_row(row: Any) -> dict[str, Any] | None:
    """One row of a dispatch plan: its task id and settings, or None if it has no task id."""
    if not isinstance(row, Mapping) or not isinstance(row.get("task_id"), str) or not _TASK_RE.match(row["task_id"]):
        return None
    kept: dict[str, Any] = {"task_id": row["task_id"]}
    for key, kind in _ROW_FIELDS.items():
        value = row.get(key)
        if key == "task_id" or value is None:
            continue
        if kind == _NAME and isinstance(value, str) and 0 < len(value) <= MAX_NAME_CHARS:
            kept[key] = value
        elif kind == _COUNT and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            kept[key] = value
    return kept


# ----------------------------------------------------------------------------- lookups

def _tasks(conn: sqlite3.Connection, task_ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for task_id in task_ids:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is not None:
            rows.append({key: row[key] for key in row.keys()})
    return rows


def _role_of(subagent_type: Any, policy_doc: Mapping[str, Any]) -> str | None:
    """The role a generated role file names: ``gx-skill-author`` is ``skill_author``."""
    if not isinstance(subagent_type, str):
        return None
    text = subagent_type.strip().lower()
    for prefix in ("gx-", "gx_"):
        if text.startswith(prefix):
            role = text[len(prefix):].replace("-", "_")
            return role if role in policy_doc["roles"] else None
    return None


def _earlier_task_ids(conn: sqlite3.Connection, host: str, session_ref: str, tool_use_id: str | None) -> list[str]:
    """The tasks named when the spawn this result belongs to was requested."""
    if not tool_use_id:
        return []
    row = conn.execute(
        "SELECT payload_json FROM host_events WHERE host=? AND session_ref=? AND hook_event=?"
        " AND json_extract(payload_json, '$.tool_use_id')=? ORDER BY rowid DESC LIMIT 1",
        (host, session_ref, EVENT_PRE_TOOL, tool_use_id),
    ).fetchone()
    if row is None:
        return []
    try:
        found = json.loads(row["payload_json"]).get("gx_task_ids") or []
    except ValueError:
        return []
    return [item for item in found if isinstance(item, str) and _TASK_RE.match(item)]


def _coordinated_runs(conn: sqlite3.Connection, host: str, session_ref: str, *,
                      live_only: bool = False) -> list[dict[str, Any]]:
    """The runs whose coordinator lease names this session; with ``live_only``, not expired either."""
    rows = conn.execute(
        "SELECT r.run_id AS run_id, r.status AS status, c.expires_at AS expires_at FROM coordinator_leases c"
        " JOIN runs r ON r.run_id=c.run_id WHERE c.session_ref=? AND c.host=? AND c.released_at IS NULL"
        " ORDER BY r.run_id",
        (session_ref, host),
    ).fetchall()
    return [{"run_id": row["run_id"], "status": row["status"]} for row in rows
            if not live_only or not clock.is_past(row["expires_at"])]


def _worst(verdicts: list[str]) -> str | None:
    return next((verdict for verdict in _SEVERITY if verdict in verdicts), None)


def _proposals(host: str, policy_doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The reader's spec when hook events are proposals under this policy (design r2.2a section 8.2), else None."""
    spec = policy.reader_spec(host, policy_doc)
    return spec if spec is not None and spec.get("meta_subject_field") else None


def _withheld(what: str, why: str) -> dict[str, Any]:
    return {"what": what, "applied": False, "why": why}


def _main_record_path(session_ref: str):
    return claude_code.locate_session(session_ref) if claude_code.valid_session_ref(session_ref) else None


def _ended_by_the_hosts_record(host: str, session_ref: str, handouts: list[dict[str, Any]],
                               policy_doc: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(hand-outs to close, effects withheld)`` for hand-outs a hook says have stopped running.

    A hand-out whose launch the host reported is closed when the host's record of the session
    holds that workflow run, which the host writes when the run has ended. One whose launch
    was never reported has no run to look for: nothing bears the hook out, and it stays open
    until its time.
    """
    spec = _proposals(host, policy_doc)
    if spec is None:
        return list(handouts), []
    main = _main_record_path(session_ref)
    allowed = claude_code.Fields.from_spec(spec)
    ended, effects = [], []
    for item in handouts:
        run_id = item.get("workflow_run_id")
        if main is not None and run_id and claude_code.workflow_run(main, run_id, allowed=allowed) is not None:
            ended.append(item)
        else:
            effects.append(_withheld(
                f"the close of hand-out {item['handout_id']}",
                "the host's record of the session holds no ended workflow run for it; it stays open until "
                f"{item.get('expires_at')}"))
    return ended, effects


def _leases_of_agent(conn: sqlite3.Connection, host: str, session_ref: str, agent_ref: str | None) -> list[dict[str, Any]]:
    """The live leases, with no submission, of the subjects a hook saw this agent call the backend under."""
    subjects = observation.subjects_of_agent(conn, host=host, session_ref=session_ref, agent_ref=agent_ref)
    found: list[dict[str, Any]] = []
    for subject in subjects:
        for row in conn.execute(
            "SELECT l.lease_id AS lease_id, l.task_id AS task_id, t.run_id AS run_id FROM leases l"
            " JOIN tasks t ON t.task_id=l.task_id WHERE l.principal=? AND l.released_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.lease_id=l.lease_id) ORDER BY l.issued_at, l.lease_id",
            (subject,),
        ):
            found.append({"lease_id": row["lease_id"], "task_id": row["task_id"], "run_id": row["run_id"]})
    return found


def _stopped_rows(conn: sqlite3.Connection, host: str, session_ref: str, agent_ref: str | None, cause: str) -> list[dict[str, Any]]:
    """One ``worker.stopped`` trigger per lease the stopped agent held with no submission.

    A trigger says "look": core's reaction asks ``policy.holder_stopped`` whether the host's
    record bears the stop out before it releases anything, so a row written for a stop that
    did not happen moves nothing.
    """
    return [{"topic": TOPIC_WORKER_STOPPED, "key": f"{TOPIC_WORKER_STOPPED}:{item['lease_id']}",
             "payload": {"run_id": item["run_id"], "subject": item["lease_id"], "event_id": None,
                         "task_id": item["task_id"], "cause": cause, "agent_ref": agent_ref}}
            for item in _leases_of_agent(conn, host, session_ref, agent_ref)]


# ----------------------------------------------------------------------------- handlers

def _spawn_gate(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    tasks = _tasks(conn, fields.get("gx_task_ids", []))
    role = _role_of(fields.get("subagent_type"), policy_doc)
    required = policy.requirement_for_spawn(tasks, role, policy_doc)
    judged = policy.judge_spawn(
        required=required, model=fields.get("spawn_model"), effort=fields.get("spawn_effort"),
        definition_model=fields.get("definition_model"), definition_effort=fields.get("definition_effort"),
        env_model=fields.get("subagent_model_env"), env_force=bool(fields.get("subagent_model_force")),
        policy=policy_doc,
    )
    reason = None
    failed: list[dict[str, Any]] = []
    if judged["decision"] == DECISION_DENY:
        reason = (
            "The agent policy (config/role_policy.json) refuses this spawn: " + judged["reason"]
            + ". Nothing was started. Do not work around it by another route; if the model the work "
            "requires cannot do it, stop and tell the user."
        )
        # The spawn's model or effort is the row's own: a dispatch failure of each task it named (§4.8).
        failed = [{"task_id": task["task_id"], "run_id": task["run_id"], "source": dispatch.SOURCE_SPAWN_GATE,
                   "reason": judged["reason"]} for task in tasks]
    return {
        "dispatch_failed": failed,
        "decision": judged["decision"], "reason": reason,
        "model": judged["named_model"], "effort": judged["named_effort"],
        "matched": {"task_ids": [task["task_id"] for task in tasks], "role": role, "by": "task_marker"}
        if tasks or role else None,
        "matched_task_id": tasks[0]["task_id"] if tasks else None,
        "run_id": tasks[0]["run_id"] if tasks else None,
        "extra": {"judged": {key: judged[key] for key in ("model_basis", "effort_basis", "required")},
                  "unknown_task_ids": [item for item in fields.get("gx_task_ids", [])
                                       if item not in {task["task_id"] for task in tasks}]},
    }


def _client_call(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """A hook on the shell tool saw this backend's client about to run.

    The host says which agent is running the command (``agent_ref``; none for the session's
    own thread) and the command line said which subject the call runs under. Recording the
    two together is the tie between a name a worker typed and an agent the host started.

    Two things are refused here, where the caller is known to the host rather than declared
    by itself: a call that claims the host adapter's scope (the adapter's own client is not
    run through the shell tool, so such a call is an attempt to write host events by hand),
    and a call from a subagent under any scope but a worker's.
    """
    subjects = list(fields.get("gx_subjects") or [])
    scopes = list(fields.get("gx_scopes") or [])
    extra: dict[str, Any] = {}
    if SCOPE_HOST_ADAPTER in scopes:
        return {"decision": DECISION_DENY, "extra": extra, "reason": (
            "GX host adapter: this command calls the backend under the host adapter's scope. That scope "
            "is the adapter's own, used by the hooks the host runs; it is not available from the shell tool."
        )}
    if agent_ref is not None and any(scope not in SUBAGENT_SCOPES for scope in scopes):
        return {"decision": DECISION_DENY, "extra": extra, "reason": (
            f"GX host adapter: a worker calls the backend as a worker. This command names the scope "
            f"{', '.join(scope for scope in scopes if scope not in SUBAGENT_SCOPES)}, which a subagent is not given."
        )}
    known = observation.subjects_of_agent(conn, host=host, session_ref=session_ref, agent_ref=agent_ref)
    several = sorted(set(known) | set(subjects))
    if len(several) > 1 and set(subjects) - set(known):
        # One agent the host started, more than one name: what the names claim is not two workers.
        extra["subjects_of_this_agent"] = several
        observation.record_event(
            conn, host=host, hook_event=observation.EVENT_SEVERAL_SUBJECTS, session_ref=session_ref,
            agent_ref=agent_ref, source=observation.SOURCE_HOOK,
            reason=f"the host's agent {agent_ref or 'main'} of session {session_ref} has called the backend "
                   f"under {len(several)} subjects: {', '.join(several)}",
            payload={"subjects": several},
            event_id=observation.derived_event_id(host, session_ref, agent_ref, observation.EVENT_SEVERAL_SUBJECTS,
                                                  *several),
        )
    for subject in subjects:
        others = [item for item in observation.agents_of_subject(conn, host=host, subject=subject)
                  if (item["session_ref"], item["agent_ref"]) != (session_ref, agent_ref)]
        if others:
            # One name, more than one agent of the host: a subject passed from worker to worker.
            everyone = sorted({f"{item['session_ref']}:{item['agent_ref'] or 'main'}" for item in others}
                              | {f"{session_ref}:{agent_ref or 'main'}"})
            extra.setdefault("subjects_used_by_several_agents", {})[subject] = everyone
            observation.record_event(
                conn, host=host, hook_event=observation.EVENT_SHARED_SUBJECT, session_ref=session_ref,
                agent_ref=agent_ref, source=observation.SOURCE_HOOK,
                reason=f"subject {subject} has been used by {len(everyone)} agents of the host: {', '.join(everyone)}",
                payload={"subject": subject, "agents": everyone},
                event_id=observation.derived_event_id(host, observation.EVENT_SHARED_SUBJECT, subject, *everyone),
            )
    task_ids = list(fields.get("gx_task_ids") or [])
    # The tie this event proposes holds only on the host's own record of the agent (r2.2a
    # section 8.2). Here the meta is asked, which is cheap; where the tie is used the agent's
    # turns are asked too. A session's own thread has no meta: its tie is settled where used.
    effects: list[dict[str, Any]] = []
    spec = _proposals(host, policy_doc)
    if spec is not None and agent_ref is not None and subjects:
        main = _main_record_path(session_ref)
        allowed = claude_code.Fields.from_spec(spec)
        named = (claude_code.read_meta(main, agent_ref, allowed=allowed).get(allowed.meta_subject_field)
                 if main is not None else None)
        effects = [_withheld(f"the tie of subject {subject} to agent {agent_ref}",
                             "the host's record of that agent does not name the subject")
                   for subject in subjects if named != subject]
    return {"decision": DECISION_ALLOW, "reason": None, "extra": extra, "effects": effects,
            "matched_task_id": task_ids[0] if len(task_ids) == 1 else None}


def shipped_dispatch_sha256() -> str | None:
    """The sha256 of the dispatch workflow as this tree ships it, or None if it cannot be read."""
    try:
        return hashlib.sha256(DISPATCH_SCRIPT.read_bytes()).hexdigest()
    except OSError:
        return None


def _names_required_model(name: Any, required: Mapping[str, Any], policy_doc: Mapping[str, Any]) -> bool:
    """Whether a row names the model its own task requires: that model's id or one of its short names."""
    return policy.names_model(name, required.get("model"), policy_doc)


def _workflow_gate(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """A call of the Workflow tool that could start GX roles (the hook sends no other).

    Such a script's agents start without an Agent tool call, so the spawn gate never sees
    them; the host's hooks give no model or effort for them before they run. What can be
    judged here is the script and the plan it is handed: the saved ``gx-dispatch`` whose
    bytes are the ones this tree ships, and rows each naming its own task's model and an
    effort the task allows. Rows of one launch may name different models (a model per class of
    work, design r2.2 §8.11); the script starts every agent at its own row's model and effort.
    """
    name = fields.get("workflow_name")
    types = list(fields.get("gx_agent_types") or [])
    rows = list(fields.get("dispatch_rows") or [])
    extra: dict[str, Any] = {"gx_agent_types": types, "rows": len(rows)}
    if name != DISPATCH_WORKFLOW or fields.get("workflow_inline") or fields.get("workflow_by_path"):
        how = (f"the saved workflow {name}" if name and not fields.get("workflow_inline")
               else "a script given inline" if fields.get("workflow_inline") else "a script given by path")
        if not types:
            return {"decision": DECISION_ALLOW, "reason": None, "extra": extra}
        return {"decision": DECISION_DENY, "extra": extra, "reason": (
            f"GX host adapter: {how} would start GX roles ({', '.join(types)}) itself. A GX role is started "
            f"only by the saved {DISPATCH_WORKFLOW} workflow, which takes each task's model and effort from the "
            "dispatch plan the backend returned. Nothing was started; dispatch the plan with "
            f"Workflow({{name: \"{DISPATCH_WORKFLOW}\", args: ...}})."
        )}
    shipped = shipped_dispatch_sha256()
    found = fields.get("workflow_sha256")
    extra.update(workflow_sha256=found, shipped_sha256=shipped, workflow_source=fields.get("workflow_source"))
    if shipped is None or found != shipped:
        return {"decision": DECISION_DENY, "extra": extra, "reason": (
            f"GX host adapter: the {DISPATCH_WORKFLOW} workflow this session would run "
            + ("could not be read" if found is None else f"is not the one this tree ships (sha256 {found[:12]}…, "
               f"shipped {str(shipped)[:12]}…)")
            + ". Nothing was started. Install the adapter again (adapters/hosts/install.py), and start a new "
            "session after installing: a session does not pick up role files added after it started."
        )}
    tasks = {task["task_id"]: task for task in _tasks(conn, [row["task_id"] for row in rows])}
    # A row is judged for its own task: what is wrong here is a dispatch failure of that task
    # (§4.8). What is wrong with the launch as a whole counts against no task (ruling 19:52Z).
    problems: list[str] = []
    failed: list[dict[str, Any]] = []
    for row in rows:
        task = tasks.get(row["task_id"])
        if task is None:
            problems.append(f"{row['task_id']} is not a task this backend holds")
            continue
        required = policy.requirement_of_task(task, policy_doc)
        model, effort = row.get("model"), row.get("effort")
        found: list[str] = []
        if not model:
            found.append(f"{row['task_id']} names no model, so its agent would run on whatever the session runs on")
        elif not _names_required_model(model, required, policy_doc):
            found.append(f"{row['task_id']} names the model {model}; the task requires {required['model']}")
        if effort is not None and effort not in required["allowed_efforts"]:
            found.append(f"{row['task_id']} names the effort {effort!r}; the task allows {required['allowed_efforts']}")
        problems.extend(found)
        failed.extend({"task_id": task["task_id"], "run_id": task["run_id"], "source": dispatch.SOURCE_WORKFLOW_GATE,
                       "reason": item} for item in found)
    matched_ids = [row["task_id"] for row in rows if row["task_id"] in tasks]
    first = tasks.get(matched_ids[0]) if matched_ids else None
    outcome: dict[str, Any] = {
        "matched": {"task_ids": matched_ids, "by": "dispatch_plan"} if matched_ids else None,
        "matched_task_id": matched_ids[0] if len(matched_ids) == 1 else None,
        "run_id": first["run_id"] if first else None, "extra": extra, "dispatch_failed": failed,
    }
    launch = _launch_problems(conn, session_ref, fields, rows, tasks)
    problems = launch + problems
    if problems:
        extra["problems"] = problems[:MAX_LIST]
        shown = "; ".join(problems[:5]) + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
        outcome.update(decision=DECISION_DENY, reason=(
            f"GX host adapter: this dispatch is refused: {shown}. Nothing was started. "
            "Dispatch the rows as `gx next` returned them, under a nonce of your own for this launch; "
            "if the plan itself is wrong, stop and tell the user."
        ))
        return outcome
    run_id = first["run_id"]
    outcome.update(decision=DECISION_ALLOW, reason=None, handout={
        "run_id": run_id, "nonce": fields["dispatch_nonce"], "tool_use_id": fields.get("tool_use_id"),
        "follow_depth": int(fields.get("follow_depth", dispatch.DEFAULT_FOLLOW_DEPTH)),
        # The attempt is the store's, not the one the coordinator typed: an offered row is one
        # whose task is still READY at this attempt.
        "rows": [{"task_id": row["task_id"], "attempt": int(tasks[row["task_id"]]["attempt"] or 0),
                  "role": tasks[row["task_id"]]["role"], "model": row.get("model"), "effort": row.get("effort")}
                 for row in rows],
    })
    extra["handout_id"] = dispatch.handout_id(session_ref, fields["dispatch_nonce"])
    return outcome


def _launch_problems(conn, session_ref, fields, rows, tasks) -> list[str]:
    """What is wrong with a gx-dispatch launch as a whole: the coordinator's, never a task's."""
    found: list[str] = []
    if not rows:
        found.append("it is handed no rows (args.tasks): a dispatch starts the rows of a plan")
    session8, nonce = fields.get("dispatch_session"), fields.get("dispatch_nonce")
    if session8 != dispatch.session_prefix(session_ref):
        found.append(f"args.session8 is {session8!r}, not this session's {dispatch.session_prefix(session_ref)!r}")
    if not isinstance(nonce, str) or not dispatch.NONCE_RE.match(nonce):
        found.append("args.nonce is missing or not 1-32 lower-case letters, digits, '_' or '-'")
    elif dispatch.nonce_used(conn, session_ref=session_ref, nonce=nonce):
        found.append(f"the nonce {nonce!r} was used by an earlier dispatch of this session; make a new one for each launch")
    depth = fields.get("follow_depth")
    if depth is not None and depth > dispatch.MAX_FOLLOW_DEPTH:
        found.append(f"args.follow_depth is {depth}; at most {dispatch.MAX_FOLLOW_DEPTH}")
    runs = sorted({task["run_id"] for task in tasks.values()})
    if len(runs) > 1:
        found.append(f"its rows belong to {len(runs)} runs ({', '.join(runs)}); one dispatch hands out the rows of one run")
    declared = fields.get("dispatch_run_id")
    if runs and declared and declared not in runs:
        found.append(f"args.run_id is {declared}, but its rows belong to {runs[0]}")
    if len(runs) == 1:
        offered = {(item["task_id"], item["attempt"]): item["handout_id"]
                   for item in dispatch.offered_rows(conn, runs[0])}
        again = [f"{row['task_id']} (handed out by {offered[(row['task_id'], int(tasks[row['task_id']]['attempt'] or 0))]})"
                 for row in rows if row["task_id"] in tasks
                 and (row["task_id"], int(tasks[row["task_id"]]["attempt"] or 0)) in offered]
        if again:
            found.append("rows already handed to a dispatch that is still running: " + ", ".join(again[:5])
                         + (f" and {len(again) - 5} more" if len(again) > 5 else "")
                         + "; they are under `offered` in `gx next` until that dispatch ends")
    return found


def _workflow_launched(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """The host launched a GX workflow: keep its run id and the sha256 of the copy it kept of the script.

    The copy is the host's own record of what ran; equal to the shipped sha256, it shows that
    the agents of that run were started by the shipped dispatch.
    """
    shipped = shipped_dispatch_sha256() if fields.get("workflow_name") == DISPATCH_WORKFLOW else None
    copy = fields.get("script_copy_sha256")
    extra = {"shipped_sha256": shipped, "copy_is_shipped": bool(copy and shipped and copy == shipped)}
    return {"decision": None, "reason": None, "extra": extra, "launched": {
        "tool_use_id": fields.get("tool_use_id"), "workflow_run_id": fields.get("workflow_run_id"),
        "workflow_task_id": fields.get("workflow_task_id"), "status": fields.get("workflow_status")}}


def _workflow_failed(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """PostToolUseFailure of the Workflow tool: a hand-out it made did not launch."""
    return {"decision": None, "reason": None, "launched": {
        "tool_use_id": fields.get("tool_use_id"), "workflow_run_id": None, "workflow_task_id": None,
        "status": "failed"}}


def session_start_facts(fields: Mapping[str, Any], policy_doc: Mapping[str, Any]) -> dict[str, Any]:
    """What a SessionStart record says this session can do for GX (design r2 §8.1).

    ``roles``: the GX roles whose role file the session started with (project files first);
    ``missing_roles``: the policy's roles it has no file for; ``dispatch``: ``shipped`` when the
    project's gx-dispatch was there and is this tree's, ``other`` when another one was,
    ``absent`` otherwise. A session can coordinate when it has the coordinator, at least one
    worker role, and the shipped dispatch in a workflows directory that existed at start.
    """
    files = list(fields.get("gx_files") or [])
    roles = sorted({_role_of(item["name"][:-3], policy_doc) for item in files
                    if item["kind"] == "role"} - {None})
    dispatches = [item for item in files if item["kind"] == "workflow" and item["name"] == f"{DISPATCH_WORKFLOW}.js"]
    project = [item for item in dispatches if item["scope"] == "project"] or dispatches
    shipped = shipped_dispatch_sha256()
    dispatch_state = ("absent" if not project else
                      "shipped" if shipped is not None and project[0]["sha256"] == shipped else "other")
    missing = sorted(set(policy_doc["roles"]) - set(roles))
    can = ("coordinator" in roles and any(role != "coordinator" for role in roles) and dispatch_state == "shipped"
           and bool(fields.get("workflows_dir_present")))
    return {"roles": roles, "missing_roles": missing, "dispatch": dispatch_state,
            "workflows_dir_present": bool(fields.get("workflows_dir_present")), "can_coordinate": can}


#: SessionStart sources on which the host reads role files and saved workflows afresh: a new
#: process. After "compact" or "clear" the same process goes on with what it loaded at start.
_FRESH_START_SOURCES = ("startup", "resume")


def _session_started(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """SessionStart: record what GX files the session began with, and say it in the session.

    On "compact" or "clear" the files in the project now are not what the session loaded, so
    the facts are those of the session's last fresh start; with none on record, it cannot be
    shown that the session has the GX roles, and it is told so.
    """
    facts = session_start_facts(fields, policy_doc)
    if fields.get("start_source") not in _FRESH_START_SOURCES:
        earlier = conn.execute(
            "SELECT payload_json FROM host_events WHERE host=? AND session_ref=? AND hook_event=?"
            " AND json_extract(payload_json, '$.start_source') IN (?, ?) ORDER BY rowid DESC LIMIT 1",
            (host, session_ref, EVENT_SESSION_START, *_FRESH_START_SOURCES),
        ).fetchone()
        try:
            loaded = json.loads(earlier["payload_json"]).get("session_start") if earlier else None
        except ValueError:
            loaded = None
        facts = dict(loaded) if isinstance(loaded, dict) else {**facts, "can_coordinate": False, "roles": [],
                                                                 "dispatch": "absent"}
        facts["from_fresh_start"] = loaded is not None
    if facts["can_coordinate"]:
        said = (f"GX host adapter facts (session start): this session started with the GX roles "
                f"{', '.join(facts['roles'])} and the shipped gx-dispatch; it can coordinate a GX run.")
    else:
        why = []
        if "coordinator" not in facts["roles"] or len(facts["roles"]) < 2:
            why.append("without the GX role files" if not facts["roles"] else
                       f"with only the GX roles {', '.join(facts['roles'])}")
        if facts["dispatch"] != "shipped":
            why.append({"absent": "without gx-dispatch", "other": "with a gx-dispatch that is not this tree's"}
                       [facts["dispatch"]])
        if not facts["workflows_dir_present"]:
            why.append("without a .claude/workflows directory, so no saved workflow is picked up")
        said = ("GX host adapter facts (session start): this session started " + "; ".join(why)
                + ". It cannot coordinate a GX run: install the adapter (adapters/hosts/install.py) and start a "
                  "new session; a session does not pick up role files or workflows added after it started.")
    return {"decision": None, "reason": said, "extra": {"session_start": facts}}


def session_capability(conn: sqlite3.Connection, *, host: str, session_ref: str | None) -> dict[str, Any]:
    """Whether the host's hook recorded, when this session started, that it can coordinate a GX run.

    For core's ``gx_run_start``, which answers CLIENT_CAPABILITY_MISSING from it (contract r5:
    probe D showed that a session never sees role files or a saved workflow installed after it
    started, so only what it began with counts). Reads the session's last SessionStart event from
    the host's hook (design r2 §8.1: after "compact" or "clear" that event already carries the
    facts of the last fresh start); writes nothing.

    ``{"recorded", "can_coordinate", "reason", "event_id", "start_source", "roles",
    "missing_roles", "dispatch", "workflows_dir_present"}``. ``recorded`` is False when the store
    holds no SessionStart of that session from the host's hook: its hooks never ran, or it began
    before the adapter was installed. ``can_coordinate`` is then False and ``reason`` says why.
    """
    empty = {"recorded": False, "can_coordinate": False, "event_id": None, "start_source": None, "roles": [],
             "missing_roles": [], "dispatch": None, "workflows_dir_present": None}
    if host not in contracts.label_values("host_kind") or not isinstance(session_ref, str) or not _REF_RE.match(session_ref):
        return {**empty, "reason": "no session of a host was named"}
    row = conn.execute(
        "SELECT event_id, payload_json FROM host_events WHERE host=? AND session_ref=? AND hook_event=? AND source=?"
        " ORDER BY rowid DESC LIMIT 1", (host, session_ref, EVENT_SESSION_START, observation.SOURCE_HOOK),
    ).fetchone()
    if row is None:
        return {**empty, "reason": (f"the store holds no SessionStart of session {session_ref} from the host's hook: "
                                    "its hooks never ran, or it began before the adapter was installed; install the "
                                    "adapter and start a new session")}
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        payload = {}
    facts = payload.get("session_start") if isinstance(payload, dict) else None
    if not isinstance(facts, dict):
        return {**empty, "recorded": True, "event_id": row["event_id"],
                "reason": "the session's SessionStart record holds no facts about its GX files"}
    can = bool(facts.get("can_coordinate"))
    return {"recorded": True, "can_coordinate": can, "event_id": row["event_id"],
            "start_source": payload.get("start_source"), "roles": list(facts.get("roles") or []),
            "missing_roles": list(facts.get("missing_roles") or []), "dispatch": facts.get("dispatch"),
            "workflows_dir_present": facts.get("workflows_dir_present"),
            "reason": None if can else ("the session started without what a coordinator needs: "
                                        f"roles {facts.get('roles') or []}, gx-dispatch {facts.get('dispatch')}, "
                                        f"workflows directory {'present' if facts.get('workflows_dir_present') else 'absent'}"
                                        "; install the adapter and start a new session")}


def _session_ended(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """SessionEnd: the session's dispatches end with it, and its own thread stops holding what it held.

    A hand-out is closed where the host's record bears it out (its workflow run is on record
    as ended) or where it was never launched; one whose run the record does not show ended
    stays open until its time. The leases of the session's own thread get a ``worker.stopped``
    trigger; whether they are released is settled by ``policy.holder_stopped`` when core asks.
    """
    open_now = dispatch.open_handouts(conn, session_ref=session_ref)
    launched = [item for item in open_now if item.get("workflow_run_id")]
    ended, effects = _ended_by_the_hosts_record(host, session_ref, launched, policy_doc)
    closing = [item for item in open_now if not item.get("workflow_run_id")] + ended
    return {"decision": None, "reason": None, "effects": effects,
            "close_handouts": [(item, dispatch.CLOSED_SESSION_ENDED) for item in closing],
            "triggers": _stopped_rows(conn, host, session_ref, None, CAUSE_SESSION_END)}


def _stop_failed(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """StopFailure: a turn of the session's own thread ended in an error of the host or of the API.

    The thread that failed is the session's own (or the agent the event names); a background
    workflow of the session goes on, so no hand-out closes here. Leases that thread holds with
    no submission get a ``worker.stopped`` trigger, cause ``stop_failure``.
    """
    return {"decision": None, "reason": None,
            "triggers": _stopped_rows(conn, host, session_ref, agent_ref, CAUSE_STOP_FAILURE)}


def _spawned(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    task_ids = fields.get("gx_task_ids") or _earlier_task_ids(conn, host, session_ref, fields.get("tool_use_id"))
    tasks = _tasks(conn, task_ids)
    spawned = fields.get("spawned_agent_id")
    resolved = fields.get("resolved_model")
    models = [resolved, *fields.get("models_used", [])] if resolved else list(fields.get("models_used", []))

    def off(wanted: list[str]) -> list[str]:
        # What the host says it resolved is a record: only the id of a model counts.
        return [name for name in models if name and not any(policy.is_model(name, item) for item in wanted)]

    # Each task is held to its own model; a worker tied to no task, to its role's or to what
    # the policy allows now (the same requirement the spawn gate judged it by).
    untied = policy.requirement_for_spawn(None, _role_of(fields.get("subagent_type"), policy_doc), policy_doc)
    wanted_by_task = {task["task_id"]: [str(policy.requirement_of_task(task, policy_doc)["model"])] for task in tasks}
    off_by_task = {task_id: off(wanted) for task_id, wanted in wanted_by_task.items()}
    off_model = (list(dict.fromkeys(name for found in off_by_task.values() for name in found)) if tasks
                 else off(policy.models_of(untied)))
    verdict = policy.MISMATCH if off_model else None
    written = []
    # The observation "this agent was started for these tasks" is written when the host's own
    # record of the agent shows this spawn: the same agent type and tool-use id in its meta
    # (r2.2a section 8.2). Until then the event is the adapter's report and writes no row.
    effects: list[dict[str, Any]] = []
    borne_out = True
    spec = _proposals(host, policy_doc)
    if spec is not None and tasks and spawned and claude_code.valid_agent_ref(spawned):
        main = _main_record_path(session_ref)
        meta = claude_code.read_meta(main, spawned, allowed=claude_code.Fields.from_spec(spec)) if main is not None else {}
        # The tool call that started the agent must be the one the event names; the agent type too,
        # where the event names one (a spawn of no type in particular names none).
        agent_type, call = fields.get("subagent_type"), fields.get("tool_use_id")
        borne_out = bool(call and meta.get("toolUseId") == call
                         and (not agent_type or meta.get("agentType") == agent_type))
        if not borne_out:
            effects.append(_withheld(
                f"the observation that agent {spawned} was started for {', '.join(task['task_id'] for task in tasks)}",
                "the host's record of that agent does not show this spawn (its agent type and tool-use id)"))
    if spawned and claude_code.valid_agent_ref(spawned) and borne_out:
        for task in tasks:
            previous = observation.latest_state(conn, task_id=task["task_id"], lease_id=None, agent_ref=spawned)
            if not observation.can_follow(previous, observation.STATE_OBSERVED_STARTED):
                continue
            written.append(observation.write_observation(
                conn, task_id=task["task_id"], host=host, state=observation.STATE_OBSERVED_STARTED,
                source=observation.SOURCE_HOOK, evidence_level=observation.LEVEL_OBSERVED,
                host_session_ref=session_ref, agent_ref=spawned,
                observed_ref=f"{host}:{session_ref}:{spawned}", observed_model=resolved,
                policy_verdict=policy.MISMATCH if off_by_task[task["task_id"]] else None,
                payload={"hook": EVENT_POST_TOOL, "status": fields.get("status"),
                         "models_used": fields.get("models_used", []), "spawned_by": agent_ref or "main"},
            ))
    reason = None
    if off_model:
        wanted = list(dict.fromkeys(name for names in wanted_by_task.values() for name in names))
        required = (f"its task requires {' and '.join(wanted)}" if tasks else
                    f"the policy pins {untied['model']}" if len(policy.models_of(untied)) == 1 else
                    f"the policy allows {' or '.join(policy.models_of(untied))}")
        reason = (
            f"The host started this worker on {', '.join(off_model)}; {required}. "
            "Its work cannot be accepted without the user's approval."
        )
    return {
        "decision": None, "reason": reason, "model": resolved, "effort": None, "policy_verdict": verdict,
        "matched": {"task_ids": [task["task_id"] for task in tasks], "agent_ref": spawned, "by": "spawn"}
        if tasks or spawned else None,
        "matched_task_id": tasks[0]["task_id"] if tasks else None,
        "run_id": tasks[0]["run_id"] if tasks else None,
        "extra": {"worker_observation_ids": written}, "effects": effects,
        "event_agent_ref": spawned if spawned and claude_code.valid_agent_ref(spawned) else None,
    }


def _worker_stopped(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    if not agent_ref:
        return {"decision": None, "reason": None}
    record = observation.read_worker_record(host, session_ref, agent_ref, policy_doc)
    seen = observation.observe_worker(conn, host=host, session_ref=session_ref, agent_ref=agent_ref,
                                      record=record, hook=EVENT_SUBAGENT_STOP)
    judged, whole, floor = seen["judged"], seen["whole"], seen["floor"]
    # What is concluded here is read from the agent's own record, so the event only says when
    # to look. Two kinds of trigger go to the outbox (design r2.2 section 2.4.1): a verdict
    # recorded for a lease, and a worker that stopped holding a lease with no submission.
    triggers = [{"topic": TOPIC_WORKER_VERDICT, "key": f"{TOPIC_WORKER_VERDICT}:{item['worker_observation_id']}",
                 "payload": {"run_id": item["run_id"], "subject": item["lease_id"], "event_id": None,
                             "task_id": item["task_id"], "worker_observation_id": item["worker_observation_id"],
                             "policy_verdict": item["policy_verdict"]}}
                for item in judged if item["lease_id"] and item["worker_observation_id"]]
    triggers += _stopped_rows(conn, host, session_ref, agent_ref, CAUSE_SUBAGENT_STOP)
    effects = [] if record is None or record.readable else [_withheld(
        f"what agent {agent_ref} ran on", f"the host's record of that agent is not readable: {record.reason}")]
    # A worker the backend holds no task for is still a worker under the user's rule.
    verdict = _worst([item["policy_verdict"] for item in judged] or [seen["whole_verdict"]])
    last = record.turns[-1] if record is not None and record.readable and record.turns else None
    task_ids = list(dict.fromkeys(item["task_id"] for item in judged))
    reason = None
    if verdict in policy_doc["observation"]["always_waits_for_the_user"]:
        deviating = next((item for item in judged if item["deviation"]), None)
        told = deviating["deviation"] if deviating else (
            deviation_for_report(whole, required_model=floor["model"]) if whole else None)
        # A lease is judged against its own task's model; a worker with no task, against the pin.
        reason = _deviation_sentence(told, deviating["required_model"] if deviating else floor["model"],
                                     of_task=deviating is not None)
    return {
        "decision": None, "reason": reason, "policy_verdict": verdict,
        "model": last.model if last else None, "effort": last.effort if last else None,
        "matched": {"task_ids": task_ids, "lease_ids": [item["lease_id"] for item in judged if item["lease_id"]],
                    "by": "binding"} if judged else None,
        "matched_task_id": task_ids[0] if task_ids else None,
        "run_id": next((item["run_id"] for item in judged if item["run_id"]), None),
        "triggers": triggers, "effects": effects,
        "extra": {
            "record": {key: whole[key] for key in ("readable", "reason", "host_version", "record_sha256", "turns",
                                                    "responses", "turns_off_policy", "models", "efforts",
                                                    "first_turn_at", "last_turn_at", "deviations")} if whole else None,
            "judged": [{key: item[key] for key in ("task_id", "lease_id", "policy_verdict", "worker_observation_id")}
                       for item in judged],
        },
    }


def _deviation_sentence(told: Mapping[str, Any] | None, required: str, *, of_task: bool = False) -> str:
    """What the session is told about a deviation: ``required`` is the task's model, or the pin for a worker with no task."""
    rule = f"The task requires {required}" if of_task else f"The policy pins {required}"
    if not told:
        return (f"A worker ran outside the agent policy. {rule}. Its work waits for the user.")
    parts = [f"at {told.get('when')}", f"from {told.get('from_model') or required} to {told.get('to_model') or 'no other model'}"]
    if told.get("category"):
        parts.append(f"category {told['category']}")
    if told.get("request_id"):
        parts.append(f"request {told['request_id']}")
    return (
        f"The host recorded a {str(told.get('kind') or 'deviation').replace('_', ' ')} "
        + ", ".join(parts)
        + f". {rule}; work done on another model waits for the user's approval "
        "(action kind model_switch) and nothing switches or retries on its own."
    )


def _wait_is_running(conn: sqlite3.Connection, run_id: str, session_ref: str) -> bool:
    """Whether a ``gx wait`` of this run is running for this session (design r2.2 section 8.6).

    Core's reader lists a wait while its process is alive and its time has not passed, from
    the records ``gx wait`` keeps beside the store; a wait that died is not listed. A record
    that names another session is another session's wait; one that names none counts.
    """
    try:
        from ..orchestration import wait as run_wait      # core's module; imported here, not at load
    except ImportError:
        return False
    try:
        found = run_wait.live_waits(conn, run_id)
    except Exception:  # noqa: BLE001 - a reader of files beside the store; a failure is "no wait"
        return False
    return any(item.get("session_ref") in (None, session_ref) for item in found)


def _model_approval_pending(conn: sqlite3.Connection, run_id: str) -> bool:
    """Whether the run waits on a model approval the user has not decided."""
    return conn.execute(
        "SELECT 1 FROM approvals WHERE run_id=? AND action_kind=? AND status=? LIMIT 1",
        (run_id, MODEL_APPROVAL_KIND, APPROVAL_PENDING),
    ).fetchone() is not None


def _stop_gate(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    """Design r2 §8.6: a coordinator may end its turn only when nothing it should hand out waits.

    It may stop while a gx-dispatch of this session runs, while a hand-out of the session is
    open, or, for a run, while that run waits on a pending model approval. Otherwise it is
    refused when a run it coordinates has READY rows; rows whose last dispatch failed are
    left out (they need a decision, not another launch). A Monitor, a log watcher or any
    other background task is not a reason to stop: none of them hands a row to a worker.

    First, the hand-outs this payload shows have stopped running are closed, so a dispatch
    that ended no longer holds its rows back from the next plan.
    """
    workflows = list(fields.get("background_workflows") or [])
    dispatching = DISPATCH_WORKFLOW in workflows
    ids = fields.get("background_workflow_ids")
    said_finished = dispatch.no_longer_running(conn, session_ref=session_ref,
                                               running_task_ids=list(ids) if ids is not None else (
                                                   [] if not workflows else None),
                                               dispatch_running=dispatching)
    # What the payload says has stopped is closed where the host's own record agrees (r2.2a
    # section 8.2); the rest stays open, so its rows are not offered to a second dispatch.
    finished, effects = _ended_by_the_hosts_record(host, session_ref, said_finished, policy_doc)
    closing = {item["handout_id"] for item in finished}
    still_open = [item["handout_id"] for item in dispatch.open_handouts(conn, session_ref=session_ref)
                  if item["handout_id"] not in closing]
    outcome: dict[str, Any] = {"close_handouts": [(item, dispatch.CLOSED_NOT_RUNNING) for item in finished],
                               "effects": effects}
    extra: dict[str, Any] = {"handouts_closed": sorted(closing), "handouts_open": still_open}
    waiting: list[dict[str, Any]] = []
    held: list[str] = []
    waited: list[str] = []
    # A lease that expired is no longer this session's to keep: whoever claims the run next
    # holds it, and a coordinator that wants it back claims it again.
    for run in _coordinated_runs(conn, host, session_ref, live_only=True):
        if run["status"] in _RUN_NOT_DRIVEN:
            continue
        if _model_approval_pending(conn, run["run_id"]):
            held.append(run["run_id"])
            continue
        if _wait_is_running(conn, run["run_id"], session_ref):
            # A `gx wait` of this run is running: its exit wakes the session (design r2.2 section 8.3).
            waited.append(run["run_id"])
            continue
        ready = conn.execute(
            "SELECT task_id, kind, role FROM tasks WHERE run_id=? AND status=? ORDER BY task_id",
            (run["run_id"], TASK_READY),
        ).fetchall()
        failed = [row["task_id"] for row in ready if dispatch.last_dispatch_failed(conn, row["task_id"])]
        ready = [row for row in ready if row["task_id"] not in failed]
        if failed:
            extra.setdefault("ready_last_dispatch_failed", []).extend(failed)
        if ready:
            waiting.append({"run_id": run["run_id"], "status": run["status"],
                            "ready": [row["task_id"] for row in ready],
                            "kinds": sorted({row["kind"] for row in ready})})
    if held:
        extra["waiting_on_model_approval"] = held
    if waited:
        extra["waiting_in_gx_wait"] = waited
    ready_now = sorted(task_id for item in waiting for task_id in item["ready"])
    extra["ready"] = ready_now
    outcome["extra"] = extra
    if not waiting:
        return {**outcome, "decision": DECISION_ALLOW, "reason": None}
    matched = {"run_ids": [item["run_id"] for item in waiting], "ready": len(ready_now), "by": "coordinator_lease"}
    if dispatching or still_open:
        # The Workflow tool returns as soon as it has launched (observed on 2.1.322), so right
        # after a dispatch its tasks are still READY. The running dispatch wakes the session
        # with a task notification when it ends; ending the turn now is how a coordinator waits.
        return {**outcome, "decision": DECISION_ALLOW, "matched": matched, "run_id": waiting[0]["run_id"],
                "reason": ("a gx-dispatch of this session is running" if dispatching else
                           f"{len(still_open)} hand-out(s) of this session are open ({', '.join(still_open[:3])})")
                + "; the session is woken when it ends"}
    if fields.get("stop_hook_active"):
        # The host is already continuing because of an earlier refusal to stop. If the same
        # tasks are still READY, the coordinator could not hand them out; refusing again
        # would only spend turns.
        earlier = conn.execute(
            "SELECT payload_json FROM host_events WHERE host=? AND session_ref=? AND hook_event=? AND decision=?"
            " ORDER BY rowid DESC LIMIT 1",
            (host, session_ref, EVENT_STOP, DECISION_DENY),
        ).fetchone()
        try:
            before = sorted(json.loads(earlier["payload_json"]).get("ready") or []) if earlier else None
        except ValueError:
            before = None
        if before == ready_now:
            extra["no_progress"] = True
            return {**outcome, "decision": DECISION_ALLOW, "matched": matched, "run_id": waiting[0]["run_id"],
                    "reason": "the same tasks were READY when stopping was last refused; refusing again would not hand them out"}
    first = waiting[0]
    gx = str(contracts.HARNESS_ROOT / "bin" / "gx")
    reason = (
        f"Run {first['run_id']} ({first['status']}) has {len(first['ready'])} READY task(s) of kind "
        f"{', '.join(first['kinds'])} that no running dispatch holds, and no gx-dispatch of this session is "
        f"running. A Monitor or another background task does not hand rows to workers. Read the dispatch plan "
        f"with `{gx} next --as <your subject> {first['run_id']}` and launch gx-dispatch with it under a new "
        f"nonce; if you cannot, pause the run with "
        f"`{gx} call --as <your subject> gx_run_control --json '{{\"run_id\": \"{first['run_id']}\", "
        f"\"action\": \"pause\", \"reason\": \"<why>\", \"coordinator_lease_ref\": \"<your lease>\", "
        f"\"expected_revision\": <the run's revision>, \"idempotency_key\": \"<a new key>\"}}'` and say why."
    )
    if len(waiting) > 1:
        reason += f" ({len(waiting) - 1} more run(s) of this session are in the same state.)"
    return {**outcome, "decision": DECISION_DENY, "reason": reason, "matched": matched, "run_id": first["run_id"]}


def _switch_gate(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    target = fields.get("to_model")
    if target is None or policy.is_pinned_model(target, policy_doc):
        return {"decision": DECISION_ALLOW, "reason": None, "model": target}
    runs = _coordinated_runs(conn, host, session_ref)
    return {
        "decision": DECISION_ASK, "model": target,
        "reason": (
            f"This project pins {policy_doc['pinned_model']['id']} (config/role_policy.json); leaving it "
            f"for {target} is the user's decision. "
            + (f"{len(runs)} GX run(s) this session coordinates will wait for an approval of kind model_switch."
               if runs else "Work done on the other model will be held for approval.")
        ),
        "matched": {"run_ids": [run["run_id"] for run in runs], "by": "coordinator_lease"} if runs else None,
        "run_id": runs[0]["run_id"] if runs else None,
    }


def _switched(conn, host, session_ref, agent_ref, fields, policy_doc) -> dict[str, Any]:
    target = fields.get("to_model")
    if target is None or policy.is_pinned_model(target, policy_doc):
        return {"decision": None, "reason": None, "model": target}
    automatic = fields.get("switch_source") == "auto"
    runs = _coordinated_runs(conn, host, session_ref)
    return {
        "decision": None, "model": target,
        "policy_verdict": policy.REFUSAL_OR_FALLBACK if automatic else policy.MISMATCH,
        "reason": (
            f"The session's model changed from {fields.get('from_model') or 'the pinned model'} to {target}"
            + (" on the host's own initiative (a fallback)" if automatic else "")
            + f". This project pins {policy_doc['pinned_model']['id']}. Stop work, tell the user when it "
            "happened, from which model to which, the category and the request id, and wait for approval."
        ),
        "matched": {"run_ids": [run["run_id"] for run in runs], "by": "coordinator_lease"} if runs else None,
        "run_id": runs[0]["run_id"] if runs else None,
    }


# ------------------------------------------------------------------------------ handle

def _write_dispatch_records(conn: sqlite3.Connection, *, host: str, session_ref: str, actor: str,
                            outcome: Mapping[str, Any], event_id: str) -> None:
    """What a gate's answer means for the hand-out record and the dispatch failures, written
    after the host event so each record names the event it came from."""
    for failure in outcome.get("dispatch_failed") or []:
        dispatch.record_dispatch_failed(conn, task_id=failure["task_id"], run_id=failure.get("run_id"),
                                        source=failure["source"], reason=failure["reason"], actor=actor,
                                        host_event_id=event_id)
    handout = outcome.get("handout")
    if handout and outcome.get("decision") == DECISION_ALLOW:
        dispatch.record_handout(conn, run_id=handout["run_id"], host=host, session_ref=session_ref,
                                nonce=handout["nonce"], tool_use_id=handout.get("tool_use_id"), rows=handout["rows"],
                                follow_depth=handout["follow_depth"], actor=actor, host_event_id=event_id)
    launched = outcome.get("launched")
    if launched:
        dispatch.record_launched(conn, session_ref=session_ref, tool_use_id=launched.get("tool_use_id"),
                                 workflow_run_id=launched.get("workflow_run_id"),
                                 workflow_task_id=launched.get("workflow_task_id"), status=launched.get("status"),
                                 actor=actor)
    for item, reason in outcome.get("close_handouts") or []:
        dispatch.close(conn, item, reason=reason, actor=actor)
    # Outbox triggers, in this event's own transaction: each names the host event it came from,
    # and a stop is one occurrence per event (a stop the record does not bear out today can be
    # followed by the real one, which must make a row of its own).
    for trigger in outcome.get("triggers") or []:
        key = trigger["key"] + (f":{event_id}" if trigger["topic"] == TOPIC_WORKER_STOPPED else "")
        store.enqueue_outbox(conn, topic=trigger["topic"], idempotency_key=key,
                             payload={**trigger["payload"], "host_event_id": event_id})


def handle(
    conn: sqlite3.Connection,
    *,
    host: str,
    hook_event: str,
    session_ref: str,
    agent_ref: str | None = None,
    payload: Mapping[str, Any] | None = None,
    actor: str = "host_adapter",
    caller: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one host event and answer it. The body of ``gx_host_event``.

    ``caller`` is what the backend itself saw of the process that made the call
    (``tools.caller_facts``): it is stored with the event, names only.
    """
    if host not in contracts.label_values("host_kind"):
        raise SchemaInvalid(f"{host!r} is not a host kind", details={"allowed": contracts.label_values("host_kind")})
    if not isinstance(hook_event, str) or not _EVENT_RE.match(hook_event):
        raise SchemaInvalid("hook_event is not the name of a hook event", details={"hook_event": str(hook_event)[:64]})
    if not isinstance(session_ref, str) or not _REF_RE.match(session_ref):
        raise SchemaInvalid("session_ref is not a session identifier", details={"session_ref": str(session_ref)[:64]})
    if agent_ref is not None and (not isinstance(agent_ref, str) or not _REF_RE.match(agent_ref)):
        raise SchemaInvalid("agent_ref is not an agent identifier", details={"agent_ref": str(agent_ref)[:64]})
    if payload is not None and not isinstance(payload, Mapping):
        raise SchemaInvalid("payload is not an object")

    policy_doc, policy_sha256 = policy.agent_policy()
    fields, dropped = project_payload(hook_event, payload)
    spawn_tool = fields.get("tool_name") == TOOL_SPAWN
    workflow_tool = fields.get("tool_name") == TOOL_WORKFLOW
    if hook_event == EVENT_PRE_TOOL and spawn_tool:
        outcome = _spawn_gate(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_PRE_TOOL and fields.get("tool_name") == TOOL_SHELL and fields.get("gx_calls"):
        outcome = _client_call(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_PRE_TOOL and workflow_tool:
        outcome = _workflow_gate(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_POST_TOOL and spawn_tool:
        outcome = _spawned(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_POST_TOOL and workflow_tool:
        outcome = _workflow_launched(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_POST_TOOL_FAILURE and workflow_tool:
        outcome = _workflow_failed(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_SESSION_END:
        outcome = _session_ended(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_STOP_FAILURE:
        outcome = _stop_failed(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_SESSION_START:
        outcome = _session_started(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_SUBAGENT_STOP:
        outcome = _worker_stopped(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_STOP:
        outcome = _stop_gate(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_PRE_SWITCH:
        outcome = _switch_gate(conn, host, session_ref, agent_ref, fields, policy_doc)
    elif hook_event == EVENT_POST_SWITCH:
        outcome = _switched(conn, host, session_ref, agent_ref, fields, policy_doc)
    else:
        outcome = {"decision": None, "reason": None}

    withheld = [item for item in outcome.get("effects") or [] if not item.get("applied")]
    effect = EFFECT_REPORTED if withheld else EFFECT_APPLIED
    stored = dict(fields)
    stored.update(outcome.get("extra") or {})
    stored["fields_left_out"] = dropped
    stored["policy_sha256"] = policy_sha256
    stored["actor"] = actor
    stored["effect"] = effect
    if withheld:
        stored["withheld"] = [{"what": item["what"], "why": item["why"]} for item in withheld]
    if caller is not None:
        stored["caller"] = dict(caller)
    event_id, _written = observation.record_event(
        conn, host=host, hook_event=hook_event, session_ref=session_ref,
        agent_ref=outcome.get("event_agent_ref") or agent_ref, source=observation.SOURCE_HOOK,
        model=outcome.get("model") or fields.get("session_model"), effort=outcome.get("effort") or fields.get("effort_level"),
        decision=outcome.get("decision"), reason=outcome.get("reason"),
        matched_task_id=outcome.get("matched_task_id"), run_id=outcome.get("run_id"), payload=stored,
    )
    _write_dispatch_records(conn, host=host, session_ref=session_ref, actor=actor, outcome=outcome, event_id=event_id)
    return {
        "event_id": event_id,
        "matched": outcome.get("matched"),
        "policy_verdict": outcome.get("policy_verdict"),
        "decision": outcome.get("decision"),
        "reason": outcome.get("reason"),
        "fields_left_out": dropped,
        "run_id": outcome.get("run_id"),
        "effect": effect,
        "withheld": [f"{item['what']}: {item['why']}" for item in withheld],
    }
