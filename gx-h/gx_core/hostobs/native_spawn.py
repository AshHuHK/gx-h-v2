"""Correlate observed Codex spawn operations with children across adapter restarts.

Codex owns its internal spawn. Its first observable event may follow creation.
Neither absence from thread history nor a failed item proves no child exists.
This adapter only queries and records. GX task ownership is granted by acquire.
"""
from collections.abc import Mapping
import json

from ..domain import ids
from ..errors import IdempotencyConflict, SchemaInvalid
from ..persistence import store


def _namespace(project, session):
    # The connection is already scoped to the persistent GX native store.
    return "codex_spawn/" + ids.sha256_of_obj([project, session])


def _text(value):
    return isinstance(value, str) and bool(value)


def thread_path(thread):
    """Read the path from Codex's typed Thread.source.subAgent.thread_spawn."""
    source = thread.get("source")
    sub = source.get("subAgent") if isinstance(source, Mapping) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, Mapping) else None
    if isinstance(spawn, Mapping) and spawn.get("parent_thread_id") == thread.get("parentThreadId"):
        return _agent_path(spawn.get("agent_path"))


def _agent_path(value):
    return value if isinstance(value, str) and value.startswith("/root/") and len(value) <= 1000 else None


def remember_path(conn, authority, thread_id, parent, path):
    """Persist a path from admitted thread metadata so a cold Root can enroll its worker."""
    if not path:
        return
    scope = _namespace(authority.project_ref, authority.session_id)
    key = "path:" + ids.sha256_of_obj([thread_id, path])
    with store.transaction(conn):
        if conn.execute("SELECT 1 FROM events WHERE subject_kind=? AND subject_id=? LIMIT 1", (scope, key)).fetchone():
            return
        store.append_event(conn, kind="native.worker_path", actor="codex_app_server",
            subject_kind=scope, subject_id=key, payload={"parent_thread_id": parent,
            "child_thread_id": thread_id, "agent_path": path})


def worker_directory(conn, authority):
    """Project/session scoped addressing evidence. It does not grant a lease or enroll a member."""
    directory = {}
    def add(child, parent, path):
        if not _text(child):
            return
        worker = directory.setdefault(child, {"thread_id": child, "parent_thread_id": parent,
            "backend_subject": authority.root_subject if child == authority.root_thread_id else "native:" + child,
            "agent_paths": []})
        if path and path not in worker["agent_paths"]:
            worker["agent_paths"].append(path)
    for thread_id, facts in authority.threads.items():
        add(thread_id, facts["parent_thread_id"], "/root" if thread_id == authority.root_thread_id else None)
    scope = _namespace(authority.project_ref, authority.session_id)
    for row in conn.execute("SELECT payload_json FROM events WHERE subject_kind=? "
            "AND (new_state='runtime_created' OR kind='native.worker_path') ORDER BY event_id", (scope,)):
        body = json.loads(row["payload_json"])
        add(body["child_thread_id"], body["parent_thread_id"], body.get("agent_path"))
    return list(directory.values())


def visible_workers(directory, call):
    if call.thread_id == call.root_thread_id:
        return directory
    return [w for w in directory if call.thread_id in (w["thread_id"], w["parent_thread_id"])]


def recover_legacy_paths(conn, authority, read_thread):
    """Read original thread metadata only for a project with a surviving old path assignment."""
    from .app_server_client import AppServerResponseError
    aliases = {r[0] for r in conn.execute(
        "SELECT DISTINCT a.subject FROM task_assignments a JOIN tasks t ON t.task_id=a.task_id "
        "JOIN runs r ON r.run_id=t.run_id WHERE r.project_ref=? AND json_extract(r.host_json,'$.session_ref')=? "
        "AND a.subject LIKE '/root/%' AND t.status NOT IN ('ACCEPTED','CANCELLED')",
        (authority.project_ref, authority.session_id))}
    if not aliases:
        return
    workers = worker_directory(conn, authority)
    for worker in workers:
        if worker["agent_paths"] or worker["parent_thread_id"] is None:
            continue
        try:
            response = read_thread(worker["thread_id"])
        except AppServerResponseError:
            continue  # The durable thread ID still suffices for an explicit handoff.
        thread = response.get("thread") or {}
        if (thread.get("id") == worker["thread_id"] and thread.get("parentThreadId") == worker["parent_thread_id"]
                and thread.get("sessionId") == authority.session_id and thread.get("cliVersion") == authority.host_version):
            remember_path(conn, authority, thread["id"], thread["parentThreadId"], thread_path(thread))


def resolve_subject(conn, call, subject):
    """Map an observed thread, agent path or principal to the one GX subject it can call as."""
    directory = worker_directory(conn, call._issuer)
    matches = {w["backend_subject"] for w in directory
               if subject == w["backend_subject"] or subject == w["thread_id"] or subject in w["agent_paths"]}
    if len(matches) != 1:
        raise SchemaInvalid("Use an observed backend_subject from gx_team_status for this worker. "
            "Enrollment and handoff use this same subject.", details={"subject":subject,
            "reason":"ambiguous_agent_path" if matches else "unobserved_native_subject",
            "registered_workers":visible_workers(directory, call)[:32]})
    return matches.pop()


def observe(conn, *, project_ref, session_id, parent_thread_id, turn_id, item, phase):
    """Accept only official item frames or items read from their exact saved turn."""
    if not isinstance(item, Mapping):
        return None
    collab = item.get("type") == "collabAgentToolCall" and item.get("tool") == "spawnAgent"
    activity = item.get("type") == "subAgentActivity" and item.get("kind") == "started"
    if not (collab or activity):
        return None
    item_id = item.get("id")
    if not all(map(_text, (project_ref, session_id, parent_thread_id, turn_id, item_id))):
        return None
    if collab and item.get("senderThreadId") != parent_thread_id:
        return None

    scope = _namespace(project_ref, session_id)
    op = "spawn:" + ids.sha256_of_obj([scope, parent_thread_id, turn_id, item_id])
    locator = {"parent_thread_id": parent_thread_id, "turn_id": turn_id, "item_id": item_id}
    requested = {k: ids.sha256_of_obj(item[k]) for k in ("prompt", "model", "reasoningEffort")
                 if collab and item.get(k) is not None}
    child = None
    if phase == "completed":
        if activity:
            child = item.get("agentThreadId")
        elif item.get("status") == "completed":
            receivers = item.get("receiverThreadIds")
            if isinstance(receivers, list) and len(receivers) == 1:
                child = receivers[0]
    child = child if _text(child) and child not in (session_id, parent_thread_id) else None
    path = _agent_path(item.get("agentPath")) if activity and child else None
    state = "runtime_created" if child else "creation_pending" if phase == "started" else "outcome_unknown"

    with store.transaction(conn):
        row = conn.execute("SELECT new_state,payload_json FROM events WHERE subject_kind=? AND subject_id=? "
                           "ORDER BY event_id DESC LIMIT 1", (scope, op)).fetchone()
        if row:
            old = json.loads(row["payload_json"])
            facts = old["request_fields"]
            if any(k in facts and facts[k] != value for k, value in requested.items()):
                raise IdempotencyConflict("the native spawn item changed its request")
            if old["child_thread_id"] and child and old["child_thread_id"] != child:
                raise IdempotencyConflict("the native spawn item named a different child")
            if old.get("agent_path") and path and old["agent_path"] != path:
                raise IdempotencyConflict("the native spawn item named a different agent path")
            if row["new_state"] == "runtime_created" or state == "creation_pending" or row["new_state"] == state:
                if not (row["new_state"] == state == "runtime_created" and path and not old.get("agent_path")):
                    return old
            requested = {**facts, **requested}
        result = {**locator, "operation_id": op, "session_id": session_id, "child_thread_id": child,
                  "request_fields": requested, "status": state}
        if path:
            result["agent_path"] = path
        store.append_event(conn, kind="native.spawn_observed", actor="codex_app_server",
                           subject_kind=scope, subject_id=op, prev_state=row["new_state"] if row else None,
                           new_state=state, payload=result)
    return result


def dispatch_for_worker(conn, *, project_ref, session_id, parent_thread_id, thread_id):
    """Only unambiguous observed creation ties the child to a spawn operation."""
    rows = conn.execute("SELECT DISTINCT subject_id FROM events WHERE subject_kind=? "
                        "AND new_state='runtime_created' AND json_extract(payload_json,'$.parent_thread_id')=? "
                        "AND json_extract(payload_json,'$.child_thread_id')=? LIMIT 2",
                        (_namespace(project_ref, session_id), parent_thread_id, thread_id)).fetchall()
    return rows[0]["subject_id"] if len(rows) == 1 else None


def _pending(conn, scope):
    return [json.loads(row["payload_json"]) for row in conn.execute(
        "SELECT e.payload_json FROM events e WHERE e.subject_kind=? AND e.new_state<>'runtime_created' "
        "AND NOT EXISTS (SELECT 1 FROM events later WHERE later.subject_kind=e.subject_kind "
        "AND later.subject_id=e.subject_id AND later.event_id>e.event_id)", (scope,))]


def reconcile(conn, *, project_ref, session_id, list_items):
    """Look up unresolved operations in their parent turns. Never issue a write RPC."""
    scope = _namespace(project_ref, session_id)
    pending = _pending(conn, scope)
    if not pending:
        return []
    outcomes = {}
    by_turn = {}
    for intent in pending:
        by_turn.setdefault((intent["parent_thread_id"], intent["turn_id"]), set()).add(intent["item_id"])
    from .app_server_client import AppServerResponseError
    for (parent, turn), items in by_turn.items():
        cursor = None
        seen = set()
        while items:
            params = {"threadId": parent, "turnId": turn, "limit": 32,
                      "sortDirection": "desc", "deferOutput": {"commandExecution": True, "mcpToolCall": True}}
            if cursor:
                params["cursor"] = cursor
            try:
                page = list_items(params)
            except AppServerResponseError:
                break  # e.g. unloaded parent or unsupported history store
            for entry in page.get("data") or []:
                item = entry.get("item") or {}
                if entry.get("turnId") != turn or item.get("id") not in items:
                    continue
                saved = observe(conn, project_ref=project_ref, session_id=session_id, parent_thread_id=parent,
                                turn_id=turn, item=item, phase="completed")
                if saved and saved["status"] == "runtime_created":
                    outcomes[saved["operation_id"]] = saved
                    items.discard(item["id"])
            cursor = page.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
    with store.transaction(conn):
        unresolved = _pending(conn, scope)
        for intent in unresolved:
            if intent["status"] == "creation_pending":
                intent["status"] = "outcome_unknown"
                store.append_event(conn, kind="native.spawn_reconciliation", actor="codex_app_server",
                                   subject_kind=scope, subject_id=intent["operation_id"],
                                   prev_state="creation_pending", new_state="outcome_unknown", payload=intent)
            outcomes[intent["operation_id"]] = intent
    return list(outcomes.values())
