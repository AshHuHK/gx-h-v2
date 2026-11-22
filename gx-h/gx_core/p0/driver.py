"""Drive a saved GX project. SQLite commits precede every native message delivery."""

from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import time
import uuid

from ..context import Ctx, Principal
from ..domain import clock, ids, machines
from ..errors import ArtifactNotFound, HoldActive, IdempotencyConflict, PermissionDenied, SchemaInvalid, StaleRevision
from ..jobs.service import JobService
from ..orchestration import reactor, service
from ..persistence import store
from . import intent
from .roots import Roots


def context(conn):
    return Ctx(conn=conn, principal=Principal(subject="gx-p0-operator", scope="operator"),
               tool="gx_p0_operator")


def project(conn):
    rows = conn.execute("SELECT * FROM runs WHERE json_extract(inputs_json,'$.inputs.p0')=1"
                        " ORDER BY rowid LIMIT 2").fetchall()
    if len(rows) > 1:
        raise ValueError("Use a separate --state directory for each P0 project")
    return rows[0] if rows else None


def snapshot(conn):
    run = project(conn)
    if not run:
        return {"status": "not_started"}
    rid = run["run_id"]
    checkpoint = store.latest_checkpoint(conn, rid)
    state = store.loads(checkpoint, "state_json", {}) if checkpoint else {}
    def rows(sql):
        return [dict(row) for row in conn.execute(sql, (rid,))]
    return {
        "project": run["project_ref"], "run_id": rid, "status": run["status"],
        "node": checkpoint["node"] if checkpoint else None, "round": state.get("round"),
        "report_ref": state.get("outputs", {}).get("report", {}).get("artifact_ref"),
        "rounds": state.get("scratch", {}).get("history", []),
        "intent": intent.active_items(conn, rid),
        "jobs": rows("SELECT job_id,status,scheduler_ref FROM jobs WHERE run_id=? ORDER BY rowid"),
        "tasks": rows("SELECT task_id,role,stage,status,attempt FROM tasks WHERE run_id=? ORDER BY rowid"),
        "pending_deliveries": rows("SELECT outbox_id,topic,attempts,last_error FROM outbox "
                                   "WHERE run_id=? AND topic LIKE 'p0.%' AND consumed_at IS NULL"),
        "replies": rows("SELECT m.message_id,r.turn_id,r.status,r.response FROM p0_main_replies r "
                        "JOIN p0_operator_messages m USING(message_id) WHERE m.run_id=? ORDER BY m.rowid"),
        "messages": rows("SELECT message_id,result_json,rejected FROM p0_operator_messages WHERE run_id=? ORDER BY rowid"),
    }


def enqueue(conn, rid, action, text, message_id=None):
    mid = message_id or str(uuid.uuid4())
    encoded = ids.canonical_json(action)
    with store.transaction(conn) as tx:
        prev = tx.execute("SELECT * FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
        if prev:
            if (prev["run_id"], prev["action_json"], prev["message_text"]) != (rid, encoded, text):
                raise ValueError("This message ID already belongs to a different input")
        else:
            tx.execute("INSERT INTO p0_operator_messages(message_id,run_id,action_json,message_text,created_at)"
                       " VALUES(?,?,?,?,?)", (mid, rid, encoded, text, clock.now_iso()))
    return mid


class Driver:
    def __init__(self, conn, directory, rid):
        self.conn, self.directory, self.rid = conn, Path(directory), rid
        self.ctx = context(conn)
        self._stack = ExitStack()
        self._native = None
        self._retry_at = {}

    @property
    def native(self):
        if self._native is None:
            thread_dir = self.directory / "threads"
            thread_dir.mkdir(parents=True, exist_ok=True)
            run = project(self.conn)
            spec = store.loads(run, "inputs_json")["inputs"]["p0_spec"]
            self._native = self._stack.enter_context(Roots(self.conn, run_id=self.rid, cwd=thread_dir,
                    token_budget=spec.get("turn_token_budget", 65536)))
        return self._native

    def close(self):
        self._stack.close()

    def messages(self):
        rows = self.conn.execute("SELECT * FROM p0_operator_messages WHERE run_id=? AND result_json IS NULL"
                                 " AND rejected IS NULL ORDER BY rowid", (self.rid,)).fetchall()
        for row in rows:
            mid = row["message_id"]
            action = json.loads(row["action_json"])
            thread = self.native.ensure_main()
            intent.bind_main(self.conn, self.rid, thread)
            def stage(thread_id, message_id):
                if (thread_id, message_id) != (thread, mid):
                    raise ValueError("Human message binding changed")
                intent.stage_input(self.conn, self.rid, mid, action)
            def accepted(thread_id, turn_id, message_id):
                if (thread_id, message_id) != (thread, mid):
                    raise ValueError("Human turn acknowledgment does not match the submitted input")
                with store.transaction(self.conn) as tx:
                    result = intent.accept_input(tx, self.rid, thread_id, turn_id, message_id)
                    tx.execute("UPDATE p0_operator_messages SET result_json=? WHERE message_id=?",
                               (ids.canonical_json(result), mid))
            try:
                self.native.human_turn(row["message_text"], before_start=stage, on_started=accepted,
                                       message_id=mid)
            except HoldActive:
                # In particular, a confirmation must receive its own later main turn.
                break
            except (ArtifactNotFound, IdempotencyConflict, PermissionDenied, SchemaInvalid, StaleRevision) as exc:
                # Transport and uncertain native outcomes remain durable and can be reconciled.
                with store.transaction(self.conn) as tx:
                    tx.execute("UPDATE p0_operator_messages SET rejected=? WHERE message_id=? AND result_json IS NULL",
                               (str(exc)[:500], mid))
                continue

    def finish_main(self, mid, *, wait=30):
        """Read the reply from the actual persisted main turn. Authorization was already committed."""
        db = self.conn
        if db.execute("SELECT 1 FROM p0_main_replies WHERE message_id=?", (mid,)).fetchone():
            return
        row = db.execute("SELECT thread_id,turn_id FROM p0_root_deliveries WHERE run_id=? AND event_id=?",
                         (self.rid, "human:"+mid)).fetchone()
        if not row or not row["turn_id"]:
            return
        deadline = time.monotonic() + wait
        while True:
            terminal = db.execute("SELECT terminal_status FROM p0_root_turn_usage WHERE thread_id=? AND turn_id=?",
                                  (row["thread_id"], row["turn_id"])).fetchone()
            if terminal and terminal[0]:
                break
            if time.monotonic() >= deadline:
                return
            self.native.poll(max(0.01, min(0.25, deadline-time.monotonic())))
        page = self.native.client.request("thread/turns/list", {"threadId":row["thread_id"],
                        "sortDirection":"desc", "itemsView":"full", "limit":8})
        turn = next((item for item in page["data"] if item["id"] == row["turn_id"]), None)
        if turn is not None:
            reply = "\n".join(item["text"] for item in turn.get("items", [])
                               if item.get("type") == "agentMessage")
            with store.transaction(db) as tx:
                tx.execute("INSERT OR IGNORE INTO p0_main_replies VALUES(?,?,?,?)",
                           (mid, row["turn_id"], turn["status"], reply))

    def deliveries(self):
        rows = self.conn.execute("SELECT * FROM outbox WHERE run_id=? AND consumed_at IS NULL "
                                 "AND topic IN ('p0.intent.notification','p0.intent.reevaluate') ORDER BY outbox_id",
                                 (self.rid,)).fetchall()
        for row in rows:
            oid = row["outbox_id"]
            if time.monotonic() < self._retry_at.get(oid, 0):
                continue
            payload = json.loads(row["payload_json"])
            try:
                if row["topic"] == "p0.intent.reevaluate":
                    from .science import reevaluate_intent
                    reevaluate_intent(self.conn, payload)
                else:
                    thread_id = self.native.thread_for_lease(payload["task_id"], payload.get("lease_id"))
                    if thread_id:
                        self.native.deliver(thread_id, "GX updated this task. " + ids.canonical_json(payload),
                                            event_id=str(oid), interrupt=payload.get("action") == "interrupt")
                    # A task with no Root will see current Intent in its first brief.
                with store.transaction(self.conn) as tx:
                    store.complete_outbox(tx, oid, "gx-p0")
            except Exception as exc:
                with store.transaction(self.conn) as tx:
                    store.fail_outbox(tx, oid, type(exc).__name__ + ": " + str(exc)[:240])
                self._retry_at[oid] = time.monotonic() + min(30, 2 ** min(row["attempts"], 5))

    def drive(self, timeout):
        deadline = time.monotonic() + timeout
        idle_delay = 0.15
        sent = set()
        while time.monotonic() < deadline:
            self.messages()
            if self._native:
                for row in self.conn.execute("SELECT message_id FROM p0_operator_messages WHERE run_id=? "
                             "AND result_json IS NOT NULL AND message_id NOT IN (SELECT message_id FROM p0_main_replies)",
                             (self.rid,)).fetchall():
                    self.finish_main(row[0], wait=0)
            self.deliveries()
            run = project(self.conn)
            if run["status"] in machines.terminal_states("run") or run["status"] == "PAUSED":
                return snapshot(self.conn)
            for job in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=? AND status IN "
                                         "('CREATED','SUBMITTING','SUBMITTED','QUEUED','RUNNING','CANCEL_REQUESTED')",
                                         (self.rid,)).fetchall():
                JobService(self.conn).status(job["job_id"])
            reactor.lazy(self.ctx, self.rid)
            reactor.drive_science(self.ctx, self.rid)
            tasks = self.conn.execute("SELECT t.task_id,t.status,t.attempt,j.status AS waiting_job "
                                      "FROM tasks t LEFT JOIN p0_roots r ON r.lease_id=t.lease_id "
                                      "LEFT JOIN jobs j ON j.job_id=r.wait_job_id WHERE t.run_id=? "
                                      "AND t.status IN ('READY','CLAIMED','RUNNING') ORDER BY t.rowid LIMIT 10",
                                      (self.rid,)).fetchall()
            progressed = False
            for t in tasks:
                key = (t["task_id"], t["attempt"] + (t["status"] == "READY"))
                if key not in sent or t["waiting_job"] in machines.terminal_states("job"):
                    self.native.dispatch(t["task_id"])
                    sent.add(key)
                    progressed = True
            if self._native:
                self.native.poll(min(0.2, max(0.01, deadline - time.monotonic())))
            idle_delay = 0.1 if progressed else min(1, idle_delay * 1.3)
            time.sleep(min(idle_delay, max(0, deadline - time.monotonic())))
        result = snapshot(self.conn)
        result["driver"] = "time_limit"
        return result
