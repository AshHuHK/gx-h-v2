"""Drive a saved GX project. SQLite commits precede every native message delivery."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from ..context import Ctx, Principal
from ..domain import clock, ids, machines
from ..errors import ArtifactNotFound, BudgetExceeded, HoldActive, IdempotencyConflict, PermissionDenied, SchemaInvalid, StaleRevision
from ..jobs.service import JobService
from ..orchestration import reactor, service
from ..persistence import store
from . import intent, intent_budget, science_inputs, waits
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
    # status is read-only, including for projects not yet opened for an upgrade.
    columns = {field["name"] for field in conn.execute("PRAGMA table_info(p0_root_turn_usage)")}
    meter = ("u.measured_tokens, u.metering_reason" if "measured_tokens" in columns
             else "NULL AS measured_tokens, NULL AS metering_reason")
    unsettled = rows("SELECT d.event_id,d.reservation_ref,d.thread_id,d.turn_id,d.state,b.amounts_json,"
                     "u.total_tokens,u.terminal_status," + meter + " FROM p0_root_deliveries d "
                     "JOIN budget_reservations b ON b.reservation_ref=d.reservation_ref "
                     "LEFT JOIN p0_root_turn_usage u ON u.thread_id=d.thread_id AND u.turn_id=d.turn_id "
                     "WHERE d.run_id=? AND b.status='HELD'")
    for entry in unsettled:
        if not entry["metering_reason"]:
            entry["metering_reason"] = ("awaiting delivery receipt" if not entry["turn_id"] else
                                        "awaiting terminal turn" if not entry["terminal_status"] else
                                        "awaiting attributable usage")
    return {
        "project": run["project_ref"], "run_id": rid, "status": run["status"],
        "pause": store.loads(run, "paused_json"),
        "attempt_waits": waits.pending(conn, rid),
        "resource_limits": intent_budget.current_caps(conn, rid),
        "supplements": science_inputs.source_status(conn, rid),
        "reviews": science_inputs.review_history(conn, rid),
        "node": checkpoint["node"] if checkpoint else None, "round": state.get("round"),
        "report_ref": state.get("outputs", {}).get("report", {}).get("artifact_ref"),
        "rounds": state.get("scratch", {}).get("history", []),
        "intent": intent.active_items(conn, rid),
        "jobs": rows("SELECT job_id,status,scheduler_ref FROM jobs WHERE run_id=? ORDER BY rowid"),
        "tasks": rows("SELECT task_id,role,stage,status,attempt FROM tasks WHERE run_id=? ORDER BY rowid"),
        "unsettled_turns": unsettled,
        "pending_deliveries": rows("SELECT outbox_id,topic,attempts,last_error FROM outbox "
                                   "WHERE run_id=? AND topic LIKE 'p0.%' AND consumed_at IS NULL"),
        "replies": rows("SELECT m.message_id,r.turn_id,r.status,r.response FROM p0_main_replies r "
                        "JOIN p0_operator_messages m USING(message_id) WHERE m.run_id=? ORDER BY m.rowid"),
        "messages": rows("SELECT message_id,result_json,rejected FROM p0_operator_messages WHERE run_id=? ORDER BY rowid"),
        "pending_previews": intent.pending_previews(conn, rid),
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
        self._exhausted = {}

    def main_read(self, tool, args, envelope):
        if tool == "gx_propose_intent" and set(args) == {"candidate"}:
            return intent.process_text(self.conn, self.rid, envelope["thread_id"], envelope["turn_id"],
                                       candidate=args["candidate"])
        if tool != "gx_project_state" or args:
            raise SchemaInvalid("gx_project_state reads the bound project and takes no arguments")
        conn = self.conn
        run = project(conn)
        latest = conn.execute("SELECT round_index,actions_json,reason,stop_reason,decision_ref "
                              "FROM science_decisions WHERE run_id=? ORDER BY round_index DESC LIMIT 1",
                              (self.rid,)).fetchone()
        action = conn.execute("SELECT result_json FROM p0_human_inputs WHERE run_id=? AND receipt_turn_id=? "
                              "AND result_json IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                              (self.rid, envelope["turn_id"])).fetchone()
        supplements = science_inputs.source_status(conn, self.rid)
        decision = dict(latest) if latest else None
        if decision is not None:
            decision["pending_source_ids"] = [source["source_id"] for source in supplements
                                              if source["disposition"] == "pending"]
        return {"run_id":self.rid, "status":run["status"],
                "current_items":intent.active_items(conn, self.rid),
                "supplements":supplements,
                "input_result":json.loads(action[0]) if action else None,
                "latest_decision":decision}

    @property
    def native(self):
        if self._native is None:
            thread_dir = self.directory / "threads"
            thread_dir.mkdir(parents=True, exist_ok=True)
            run = project(self.conn)
            spec = store.loads(run, "inputs_json")["inputs"]["p0_spec"]
            main_tool = {"type":"function", "name":"gx_project_state",
                         "description":"Read GX's saved Intent, this input's outcome and the most recent scientific decision.",
                         "inputSchema":{"type":"object", "properties":{}, "additionalProperties":False}}
            proposal_tool = {"type":"function", "name":"gx_propose_intent",
                             "description":"Suggest exact edits expressed by this human input. GX checks each edit against its source.",
                             "inputSchema":{"type":"object","properties":{"candidate":{"type":"object"}},
                                            "required":["candidate"],"additionalProperties":False}}
            worker_tools, worker_call, worker_prepare = (), None, None
            if run["workflow_id"] == "tl1a_geometry":
                from workflows.tl1a_geometry import review_tools
                worker_tools = review_tools.WORKER_TOOLS
                def worker_prepare(row, tool, args, envelope):
                    return review_tools.recalculate_now(self.conn, row, args)
                def worker_call(row, tool, args, envelope, *, prepared):
                    return review_tools.worker_call(self.conn, tool, args, row, envelope, prepared=prepared)
            self._native = self._stack.enter_context(Roots(self.conn, run_id=self.rid, cwd=thread_dir,
                    main_tools=(main_tool,proposal_tool), main_call=self.main_read,
                    worker_tools=worker_tools, worker_call=worker_call, worker_prepare=worker_prepare,
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
            except BudgetExceeded as exc:
                self._exhausted["message:"+mid] = exc
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
        with store.transaction(db) as tx:
            intent.finish_text(tx, self.rid, mid)
        turns = self.native.delivery._pages("thread/turns/list", {"threadId":row["thread_id"],
                        "sortDirection":"desc", "itemsView":"full", "limit":8})
        turn = next((item for item in turns if item["id"] == row["turn_id"]), None)
        if turn is not None:
            reply = "\n".join(item["text"] for item in turn.get("items", [])
                               if item.get("type") == "agentMessage")
            with store.transaction(db) as tx:
                tx.execute("INSERT OR IGNORE INTO p0_main_replies VALUES(?,?,?,?)",
                           (mid, row["turn_id"], turn["status"], reply))

    def deliveries(self):
        rows = self.conn.execute("SELECT * FROM outbox WHERE run_id=? AND consumed_at IS NULL "
                                 "AND topic IN ('p0.intent.notification','p0.intent.reevaluate','p0.intent.cancel_job',"
                                 "'p0.supplement.review') ORDER BY outbox_id",
                                 (self.rid,)).fetchall()
        for row in rows:
            oid = row["outbox_id"]
            if time.monotonic() < self._retry_at.get(oid, 0):
                continue
            payload = json.loads(row["payload_json"])
            try:
                if row["topic"] == "p0.intent.reevaluate":
                    if project(self.conn)["workflow_id"] == "tl1a_geometry":
                        from workflows.tl1a_geometry.domain import reevaluate_intent
                    else:
                        from .science import reevaluate_intent
                    reevaluate_intent(self.conn, payload)
                elif row["topic"] == "p0.supplement.review":
                    if not science_inputs.reconcile_supplement(self.conn, self.rid, payload["source_id"]):
                        self._retry_at[oid] = time.monotonic() + 0.5
                        continue
                elif row["topic"] == "p0.intent.cancel_job":
                    JobService(self.conn).cancel(payload["job_id"],
                        reason="operator confirmed cancellation in "+payload["change_id"],
                        idempotency_key=payload["change_id"]+":"+payload["job_id"])
                else:
                    thread_id = self.native.thread_for_lease(payload["task_id"], payload.get("lease_id"))
                    if thread_id:
                        from .impact import notification_content
                        content = notification_content(self.conn, payload)
                        event_id = str(oid)
                        interrupt = payload.get("action") == "interrupt"
                        original = "GX updated this task. " + ids.canonical_json(payload)
                        prior = self.conn.execute("SELECT text_sha256 FROM p0_root_deliveries WHERE run_id=? "
                                                  "AND event_id=?", (self.rid,event_id)).fetchone()
                        if prior and payload != content and prior[0] == hashlib.sha256(original.encode()).hexdigest():
                            self.native.deliver(thread_id, original, event_id=event_id, interrupt=interrupt)
                            event_id += ":content"
                        if not (prior and interrupt and event_id.endswith(":content")):
                            self.native.deliver(thread_id, "GX updated this task. " + ids.canonical_json(content),
                                                event_id=event_id, interrupt=interrupt)
                    # A task with no Root will see current Intent in its first brief.
                with store.transaction(self.conn) as tx:
                    store.complete_outbox(tx, oid, "gx-p0")
            except (BudgetExceeded, HoldActive) as exc:
                if isinstance(exc, BudgetExceeded):
                    self._exhausted["outbox:"+str(oid)] = exc
            except Exception as exc:
                with store.transaction(self.conn) as tx:
                    store.fail_outbox(tx, oid, type(exc).__name__ + ": " + str(exc)[:240])
                self._retry_at[oid] = time.monotonic() + min(30, 2 ** min(row["attempts"], 5))

    def reconcile_costs(self):
        pending = self.conn.execute(
            "SELECT DISTINCT d.thread_id FROM p0_root_deliveries d JOIN budget_reservations b "
            "ON b.reservation_ref=d.reservation_ref WHERE d.run_id=? AND b.status='HELD'",
            (self.rid,)).fetchall()
        for row in pending:
            self.native.reconcile_meter(row["thread_id"])

    def drive(self, timeout):
        deadline = time.monotonic() + timeout
        idle_delay = 0.15
        self.reconcile_costs()
        sent = {}
        retry_delay = {}
        last_terminal_event = None
        while time.monotonic() < deadline:
            self._exhausted.clear()
            self.messages()
            # A terminal human message may still need its GX effect after billing has settled.
            # This also applies when the project is paused and there is no live native client.
            for row in self.conn.execute("SELECT m.message_id FROM p0_operator_messages m "
                    "LEFT JOIN p0_main_replies r USING(message_id) WHERE m.run_id=? AND m.result_json IS NOT NULL "
                    "AND r.message_id IS NULL ORDER BY m.rowid", (self.rid,)).fetchall():
                self.finish_main(row[0], wait=0)
            self.deliveries()
            run = project(self.conn)
            if run["status"] in machines.terminal_states("run"):
                # A scientific decision can commit while its author's turn is still streaming.
                # Keep its reservation until the official final usage has been observed.
                if self._native and self.native.active_turns:
                    self.native.poll(0.2)
                    continue
                self.reconcile_costs()
                result = snapshot(self.conn)
                if self._native and result["unsettled_turns"]:
                    # Parents may finish before the last child reports usage. Drain the live
                    # connection while it has traffic, then expose genuinely missing receipts.
                    if last_terminal_event is None:
                        last_terminal_event = time.monotonic()
                    if time.monotonic() - last_terminal_event < 3:
                        if self.native.poll(0.2):
                            last_terminal_event = time.monotonic()
                        continue
                    result["driver"] = "usage_pending"
                return result
            if run["status"] == "PAUSED":
                return snapshot(self.conn)
            for job in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=? AND status IN "
                                         "('CREATED','SUBMITTING','SUBMITTED','QUEUED','RUNNING','CANCEL_REQUESTED','OUTCOME_UNKNOWN')",
                                         (self.rid,)).fetchall():
                JobService(self.conn).status(job["job_id"])
            reactor.lazy(self.ctx, self.rid)
            reactor.drive_science(self.ctx, self.rid)
            tasks = self.conn.execute("SELECT t.task_id,t.status,t.attempt,j.status AS waiting_job "
                                      "FROM tasks t LEFT JOIN p0_roots r ON r.lease_id=t.lease_id "
                                      "LEFT JOIN jobs j ON j.job_id=r.wait_job_id WHERE t.run_id=? "
                                      "AND t.status IN ('READY','CLAIMED','RUNNING') ORDER BY t.rowid",
                                      (self.rid,)).fetchall()
            progressed = False
            for t in tasks:
                key = (t["task_id"], t["attempt"] + (t["status"] == "READY"))
                retry = False
                if turn_id := sent.get(key):
                    outcome = self.conn.execute("SELECT terminal_status FROM p0_root_turn_usage WHERE turn_id=?",
                                                (turn_id,)).fetchone()
                    if outcome and outcome[0] in ("interrupted", "failed"):
                        delay, at = retry_delay.setdefault(key, (0.5, time.monotonic() + 0.5))
                        if time.monotonic() >= at:
                            retry = True
                            retry_delay[key] = (min(30, delay * 2), time.monotonic() + min(30, delay * 2))
                if key not in sent or retry or t["waiting_job"] in machines.terminal_states("job"):
                    try:
                        receipt = self.native.dispatch(t["task_id"])
                    except HoldActive:
                        continue
                    except BudgetExceeded as exc:
                        self._exhausted[t["task_id"]] = exc
                        continue
                    if receipt.get("waiting_for_review"):
                        continue
                    sent[key] = receipt.get("turn_id")
                    progressed = True
            if self._native and self.native.poll(min(0.2, max(0.01, deadline - time.monotonic()))):
                # Drain a burst of native items before sleeping or re-reading every GX checkpoint.
                until = min(deadline, time.monotonic() + 0.15)
                while time.monotonic() < until and self.native.poll(0.005):
                    pass
                idle_delay = 0.1
                continue
            if self._exhausted and not progressed and not self.native.active_turns:
                active_job = self.conn.execute(
                    "SELECT 1 FROM jobs WHERE run_id=? AND status IN "
                    "('CREATED','SUBMITTING','SUBMITTED','QUEUED','RUNNING','CANCEL_REQUESTED','OUTCOME_UNKNOWN') LIMIT 1",
                    (self.rid,)).fetchone()
                if not active_job and not self.native.has_pending_children():
                    cause = next(iter(self._exhausted.values()))
                    service.run_control(self.ctx, {"run_id":self.rid, "action":"pause",
                                        "reason":"budget_exhausted: "+cause.message,
                                        "idempotency_key":str(uuid.uuid4())})
                    return snapshot(self.conn)
            idle_delay = 0.1 if progressed else min(1, idle_delay * 1.3)
            time.sleep(min(idle_delay, max(0, deadline - time.monotonic())))
        result = snapshot(self.conn)
        result["driver"] = "time_limit"
        return result
