"""Attach the project browser to the ordinary P0 research conversation and event log."""
from contextlib import closing
from pathlib import Path
import heapq
import json
import logging
import queue
import sqlite3
import threading
import time

from ..domain import machines
from ..errors import GxError
from ..hostobs.app_server_client import AppServerDisconnected, AppServerError
from ..jobs import wakeup
from ..native_release import native_state, ReleaseStateError
from ..p0 import cli, operator, v621_assignment
from . import projects


def selected_run(state, project, requested):
    if not (state / "gx.db").is_file():
        if requested:
            raise ValueError("Create the P0 research Run before attaching with --run-id")
        return None
    if not requested and (state / "project-driver.json").is_file():
        return None
    with closing(projects.read_db(state)) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v6_goals'").fetchone():
            rows = conn.execute("""SELECT r.run_id FROM runs r JOIN v6_goals g USING(run_id)
                WHERE r.project_ref=? ORDER BY (g.status='OPEN') DESC,r.rowid DESC""", (project,)).fetchall()
            if requested and not any(row[0] == requested for row in rows):
                raise ValueError("This research Run does not belong to the selected project")
            if rows:
                operator.authorize_project(operator.local_context(conn), project)
                return requested or rows[0][0]
    if requested:
        raise ValueError("This state does not contain the selected P0 research Run")
    return None


def _goal(conn, project, run_id):
    row = conn.execute("""SELECT r.run_id,r.status AS run_status,g.status AS goal_status,g.goal_version,
                                 g.goal,g.materials_json FROM runs r JOIN v6_goals g USING(run_id)
                          WHERE r.project_ref=? AND r.run_id=?""", (project, run_id)).fetchone()
    if not row:
        raise projects.Conflict("Select a research Run belonging to this project")
    return row


def snapshot(state, project, run_id):
    from ..p0 import conversation

    with closing(cli.existing_db(state, readonly=True)) as conn:
        conn.execute("BEGIN")
        goal = _goal(conn, project, run_id)
        view = conversation.timeline(conn, run_id)
        version, objective, cursor = goal["goal_version"], goal["goal"], view["cursor"]
        if goal["goal_status"] == "CLOSED":
            from ..p0 import intent, research_report
            close = research_report.completed_close(conn, run_id)
            version, cursor = close["goal_version"], close["closed_event"]
            objective = intent.read_item(conn, run_id, "objective", version)["value"]
        branches = {r["branch_id"]: dict(r) for r in conn.execute("""SELECT branch_id,status,
            COALESCE(json_extract(spec_json,'$.hypothesis'),json_extract(spec_json,'$.question')) AS question
            FROM v6_branches WHERE run_id=? ORDER BY rowid""", (run_id,))}
        branches.update((r["branch_id"], dict(r)) for r in conn.execute("""SELECT branch_id,status,focus AS question,
            expected_delivery FROM v62_modules WHERE run_id=? AND goal_version=? ORDER BY rowid""", (run_id, version)))
        options = {}
        files = conn.execute("SELECT f.value FROM runs r JOIN json_each(r.inputs_json,'$.files') f WHERE r.run_id=?",
                             (run_id,))
        for (raw,) in files:
            item = json.loads(raw)
            options[item["artifact_ref"]] = {"artifact_ref": item["artifact_ref"], "name": Path(item["path"]).name}
        options.update((item["artifact_ref"], item) for item in v621_assignment.for_lead(conn, run_id, version, cursor))
    return {**view, "project": project, "backend": "p0", "status": goal["run_status"],
            "goal_status": goal["goal_status"], "goal_version": version, "goal": objective,
            "branches": list(branches.values()), "material_options": list(options.values())}


def basis(state, project, run_id, query):
    from ..p0 import conversation_science, research_dialogue

    with closing(cli.existing_db(state, readonly=True)) as conn:
        conn.execute("BEGIN")
        _goal(conn, project, run_id)
        feedback_id = query.get("feedback_id", [None])[0]
        if feedback_id:
            rows = research_dialogue.feedback_status(conn, run_id, query.get("message_id", [None])[0])
            feedback = next((f for f in rows if f["feedback_id"] == feedback_id), None)
            if feedback is None:
                raise ValueError("Select feedback recorded in this Run and message")
            result = {"run_id": run_id, "feedback": feedback}
            effect = feedback["disposition"]
            if effect and effect.get("decision_ref") and effect["scope"] == "run":
                result["published_decision"] = conversation_science.project(
                    conn, run_id, goal_version=effect["goal_version"], decision_ref=effect["decision_ref"],
                    before_event=effect["product_event_id"])["lead_decisions"][0]
            if effect and effect.get("report_ref"):
                published = conversation_science.project(conn, run_id, goal_version=effect["goal_version"],
                                                          branch_id=feedback["branch_id"])["released_reports"]
                result["released_report"] = next((r for r in published if r["report_ref"] == effect["report_ref"]), None)
            return result
        filters = {key: query[key][0] for key in ("branch_id", "decision_ref") if key in query}
        filters.update((key, int(query[key][0])) for key in ("goal_version", "before_event") if key in query)
        return conversation_science.project(conn, run_id, **filters)


def events(state, project, run_id, after):
    with closing(projects.read_db(state)) as conn:
        _goal(conn, project, run_id)
        rows = conn.execute("""SELECT event_id AS id,ts AS at,run_id,subject_kind AS entity,
            subject_id AS subject,new_state AS status,kind FROM events
            WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT 200""", (run_id, after))
        return [dict(row) for row in rows]


def send(state, project, body):
    from ..p0 import conversation

    rid = body.get("run_id")
    message_id = body.get("client_message_id") or body.get("message_id")
    if not isinstance(rid, str) or not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("Send run_id and a stable client_message_id with the message")
    if "message_id" in body and body["message_id"] != message_id:
        raise ValueError("The supplied message identifiers disagree")
    allowed = ("reply_to", "branch_id", "question_id", "answer_option", "conversation_id", "goal_version", "attachments", "successor")
    options = {key: body[key] for key in allowed if body.get(key) is not None}
    follow = options.get("successor")
    if follow is not None:
        if not isinstance(follow, dict) or set(follow) - {"parent_material_refs", "research_effort", "include_message_attachments"}:
            raise ValueError("The browser can select published parent materials for a follow-up")
    with closing(cli.existing_db(state, readonly=False)) as conn:
        _goal(conn, project, rid)
        operator.authorized(operator.local_context(conn), rid)
        return conversation.send(conn, rid, body.get("text"), message_id, **options)


class Delivery:
    """Wake the shared Main delivery owner after a POST or process restart."""

    def __init__(self, state, project):
        self.state = state
        self.project = project
        self.queue = queue.Queue()
        self.queued = set()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.successors = threading.Event()
        self.driver = threading.Thread(target=self._drive_successors, name="gx-web-workspace", daemon=True)
        self.thread = threading.Thread(target=self._work, name="gx-web-main-delivery", daemon=True)

    def start(self):
        from ..p0 import conversation

        with closing(cli.existing_db(self.state, readonly=False)) as conn:
            runs = conn.execute("SELECT run_id FROM runs JOIN v6_goals USING(run_id) WHERE project_ref=?",
                                (self.project,))
            for (rid,) in runs:
                for ident in conversation.pending(conn, rid):
                    self.wake(rid, ident)
            if self._has_successor(conn):
                self.successors.set()
        self.thread.start()
        self.driver.start()

    def wake(self, rid, ident):
        with self.lock:
            if (rid, ident) not in self.queued:
                self.queued.add((rid, ident))
                self.queue.put((rid, ident))

    def close(self):
        self.stopping.set()
        self.queue.put(None)
        self.successors.set()
        wakeup.notify(self.state)
        for thread in (self.driver, self.thread):
            if thread.is_alive():
                thread.join(timeout=5)

    def _has_successor(self, conn):
        rows = conn.execute("""SELECT r.status FROM runs r JOIN v6_goals g USING(run_id)
            WHERE r.project_ref=? AND g.status='OPEN' AND r.status!='PAUSED'
            AND json_extract(r.inputs_json,'$.inputs.p0_spec.predecessor.request_message_id') IS NOT NULL""",
            (self.project,))
        return any(not machines.is_terminal("run", row[0]) for row in rows)

    def _drive_successors(self):
        from ..p0 import coordinator
        while self.successors.wait():
            self.successors.clear()
            if self.stopping.is_set():
                return
            try:
                with closing(cli.existing_db(self.state, readonly=False)) as conn:
                    if not self._has_successor(conn):
                        continue
                    with native_state(self.state):
                        coordinator.drive_workspace(conn, self.state, float("inf"), stop=self.stopping)
            except ReleaseStateError as exc:
                if "another native client" not in str(exc):
                    logging.getLogger(__name__).exception("Cannot resume the web follow-up")
                    continue
            except (OSError, sqlite3.OperationalError):
                logging.getLogger(__name__).warning("Workspace temporarily unavailable", exc_info=True)
            except (GxError, AppServerError, RuntimeError, ValueError, sqlite3.Error):
                logging.getLogger(__name__).exception("Workspace could not continue the admitted follow-up")
                continue
            else:
                continue
            if not self.stopping.wait(1):
                self.successors.set()

    def _work(self):
        from ..p0 import conversation

        delayed = []
        while not self.stopping.is_set():
            now = time.monotonic()
            if delayed and delayed[0][0] <= now:
                _, item, failures = heapq.heappop(delayed)
            else:
                try:
                    item = self.queue.get(timeout=delayed[0][0] - now if delayed else None)
                except queue.Empty:
                    continue
                if item is None:
                    return
                failures = 0
            rid, ident = item
            retry = False
            try:
                with closing(cli.existing_db(self.state, readonly=False)) as conn:
                    reply = conversation.process(conn, self.state, rid, ident)
                    status = reply.get("status")
                    if status == "successor_started":
                        self.successors.set()
                    elif status == "queued_for_active_driver":
                        retry = ident in conversation.pending(conn, rid)
                        if not retry:
                            self.successors.set()  # Another owner may have admitted a follow-up.
                    else:
                        retry = any(w.get("next_retry_at") and w["owner"] in ("message", "reply", "connection")
                                    for w in reply.get("waits", ()))
            except (OSError, AppServerDisconnected, sqlite3.OperationalError) as exc:
                # BUSY, LOCKED, IOERR and protocol transport errors can clear without another POST.
                code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff
                retry = isinstance(exc, (OSError, AppServerDisconnected)) or code in (
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN)
                logging.getLogger(__name__).warning("Main delivery for %s: %s", ident, type(exc).__name__)
            except (GxError, AppServerError, RuntimeError, ValueError, sqlite3.Error) as exc:
                logging.getLogger(__name__).warning("Main delivery for %s remains in GX: %s", ident, type(exc).__name__)
            if retry:
                delay = min(30, .25 * 2 ** min(failures, 7))
                heapq.heappush(delayed, (time.monotonic() + delay, item, failures + 1))
            else:
                with self.lock:
                    self.queued.discard(item)
