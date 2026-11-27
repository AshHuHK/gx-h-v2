"""Drive a saved GX project. SQLite commits precede every native message delivery."""

from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .. import metrics
from ..context import Ctx, Principal
from ..domain import clock, ids, machines
from ..errors import ArtifactNotFound, BudgetExceeded, HoldActive, IdempotencyConflict, IllegalTransition, LeaseExpired, OutcomeUnknown, PermissionDenied, SchemaInvalid, ScopeDenied, StaleRevision
from ..hostobs.app_server_client import AppServerDisconnected, AppServerResponseError, AppServerTimeout
from ..jobs.service import JobService
from ..orchestration import branches, native_worker, reactor, service
from ..orchestration.engine import StoreCheckpointer
from ..persistence import store
from ..research import records
from . import idle, intent, intent_budget, research_window, waits
from .roots import Roots, thread_for_lease


NATIVE_WAIT = "p0.native.wait"
OFFLINE = "GX_CODEX_OFFLINE"
_DEFERRED = object()
_LOCAL_TOPICS = ("p0.intent.reevaluate", "p0.intent.cancel_job", "p0.supplement.review")
_NATIVE_TOPICS = ("p0.intent.notification", "p0.compute.notice")


def native_waits(conn, rid):
    return [{"outbox_id": row["outbox_id"], **store.loads(row, "payload_json")} for row in conn.execute(
        "SELECT outbox_id,payload_json FROM outbox WHERE run_id=? AND topic=? AND consumed_at IS NULL "
        "ORDER BY outbox_id", (rid, NATIVE_WAIT))]


def context(conn):
    return Ctx(conn=conn, principal=Principal(subject="gx-p0-operator", scope="operator"),
               tool="gx_p0_operator")


def project(conn, run_id=None):
    from .workspace import find_run
    return find_run(conn, run_id=run_id)


def snapshot(conn, run_id=None):
    run = project(conn, run_id)
    if not run:
        return {"status": "not_started"}
    rid = run["run_id"]
    from . import metering, operator_budget
    checkpoint = StoreCheckpointer().latest(conn, rid)
    state = checkpoint.state if checkpoint else {}
    fields = ("status", "owner", "reason", "since", "interrupt_ref", "references", "next_retry_at")
    node_waits = {node: {field: wait.get(field) for field in fields}
                  for node, wait in branches.waits(checkpoint).items()}
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
    workflow_projection = native_worker.for_run(run).snapshot(conn, rid)
    binding = store.loads(run, "inputs_json").get("plugin_binding") if '"plugin_binding"' in (run["inputs_json"] or "") else None
    from ..orchestration import run_mode
    meter = metering.inspect(conn, rid)
    return {
        **run_mode.for_run(run).snapshot(conn, run, meter),
        "project": run["project_ref"], "run_id": rid, "run_key": run["run_key"], "status": run["status"],
        "run_objective": run["objective"],
        "resource_profile": (store.loads(run, "host_json") or {}).get("resource_profile"),
        "workflow": {"id": run["workflow_id"], "version": run["workflow_version"]},
        **({"plugin_binding": binding} if binding is not None else {}),
        "pause": store.loads(run, "paused_json"),
        "attempt_waits": waits.pending(conn, rid),
        "native_waits": native_waits(conn, rid),
        "resource_limits": intent_budget.current_caps(conn, rid),
        "metering": meter,
        "supplements": records.source_status(conn, rid),
        "reviews": records.review_history(conn, rid),
        **workflow_projection,
        "node": checkpoint.node if checkpoint else None, "round": state.get("round"),
        "completed_nodes": state.get("completed", []),
        "node_waits": node_waits, "failed_nodes": state.get("failed_nodes", {}),
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
        "pending_previews": intent.pending_previews(conn, rid) + operator_budget.pending(conn, rid),
    }


def enqueue(conn, rid, action, text, message_id=None):
    mid = message_id or str(uuid.uuid4())
    encoded = ids.canonical_json(action)
    with store.transaction(conn, transaction_owner="Driver", operation="enqueue_message", run_id=rid) as tx:
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
        self.research_window = research_window.for_run(conn, rid)
        self._stack = ExitStack()
        self._native = None
        self._retry_at = {}
        self._exhausted = {}
        self.offline = os.environ.get(OFFLINE) == "1"
        self._waits = {(w["owner"], w["reference"]): w for w in native_waits(conn, rid)}
        self._backoff = max((w["next_retry_at"] for w in self._waits.values() if w.get("disconnected")
                             and w["next_retry_at"]), default="")
        self._checked_costs = set()
        self._sent = {}
        self.idle_seconds = idle.MIN_SECONDS
        self.wake_at = 0.
        self.made_progress = False
        self._dispatch_deadlines = ()

    def main_read(self, tool, args, envelope):
        if tool == "gx_propose_intent" and set(args) == {"candidate"}:
            return intent.process_text(self.conn, self.rid, envelope["thread_id"], envelope["turn_id"],
                                       candidate=args["candidate"])
        conn = self.conn
        goal = conn.execute("SELECT 1 FROM v6_goals WHERE run_id=?", (self.rid,)).fetchone()
        if goal and tool == "gx_route_pi_message" and set(args) == {"clauses"}:
            from . import pi_clauses
            receipt = pi_clauses.process(conn, self.rid, envelope["thread_id"], envelope["turn_id"], args["clauses"])
            if any(part.get("feedback_id") for part in receipt.get("clauses", ())):
                return {**receipt, "main_action": "reply_now"}
            return receipt
        if tool != "gx_project_state" or set(args) - {"goal_version","branch_id","decision_ref","before_event"}:
            raise SchemaInvalid("Unknown project state request")
        run = project(conn, self.rid)
        action = conn.execute("SELECT message_id,result_json FROM p0_human_inputs WHERE run_id=? AND receipt_turn_id=? "
                              "ORDER BY rowid DESC LIMIT 1", (self.rid, envelope["turn_id"])).fetchone()
        supplements = records.source_status(conn, self.rid)
        result = {"run_id":self.rid, "status":run["status"], "current_items":intent.active_items(conn, self.rid),
                  "supplements":supplements, "input_result":json.loads(action["result_json"])
                  if action and action["result_json"] else None}
        if not goal:
            from ..orchestration import run_mode
            return {**result, "latest_decision":run_mode.for_run(run).latest_decision(conn, self.rid, supplements)}
        from . import conversation, conversation_science, pi_clauses, research_dialogue
        mid = action["message_id"] if action else None
        history = conversation._entries(conn, run, through=mid, limit=8)
        # Conversation history belongs to the PI/Main. Workers receive only their frozen scope.
        result["recent_conversation"] = [{"message_id":m["message_id"], "text":m["text"][:1800],
            "main_reply":m["reply"]["text"][:2000] if m["reply"] else None, "branch_id":m["branch_id"],
            "goal_version":m["goal_version"], "reply_to":m["reply_to"]} for m in history]
        scope = dict(args)
        if history and mid == history[-1]["message_id"]:
            if history[-1]["goal_version"] is not None:
                scope.setdefault("goal_version", history[-1]["goal_version"])
            if ref := history[-1]["reply_to"]:
                anchor = conversation.reply_anchor(conn, run, ref)
                result["reply_anchor"] = anchor
                if anchor["kind"] != "message":
                    scope.update({k:anchor[k] for k in ("goal_version","decision_ref","branch_id")
                                  if k in anchor and k not in args})
        result["science"] = conversation_science.project(conn, self.rid, **scope)
        result["questions"] = research_dialogue.questions(conn, self.rid)
        result["feedback"] = research_dialogue.feedback_status(conn, self.rid)
        if mid:
            result["pi_message"] = pi_clauses.main_context(conn, self.rid, mid)
        return result

    @property
    def native(self):
        if self.offline:
            raise AppServerDisconnected("Codex disabled by " + OFFLINE)
        if self._native is None:
            thread_dir = self.directory / "threads"
            thread_dir.mkdir(parents=True, exist_ok=True)
            run = project(self.conn, self.rid)
            spec = store.loads(run, "inputs_json")["inputs"]["p0_spec"]
            main_tool = {"type":"function", "name":"gx_project_state",
                         "description":"Read GX's saved Intent, this input's outcome and the most recent scientific decision.",
                         "inputSchema":{"type":"object", "properties":{}, "additionalProperties":False}}
            proposal_tool = {"type":"function", "name":"gx_propose_intent",
                             "description":"Suggest exact edits expressed by this human input. GX checks each edit against its source.",
                             "inputSchema":{"type":"object","properties":{"candidate":{"type":"object"}},
                                            "required":["candidate"],"additionalProperties":False}}
            tools = service.hooks_for_run(self.conn, self.rid).build_worker_tools(self.conn, self.rid)
            main_tools = [main_tool, proposal_tool]
            if self.conn.execute("SELECT 1 FROM v6_goals WHERE run_id=?", (self.rid,)).fetchone():
                from . import pi_clauses
                main_tool["description"] = ("Read this PI conversation, published research decisions, branches, "
                                            "questions and actual feedback dispositions. Use optional scope for history.")
                main_tool["inputSchema"]["properties"] = {"goal_version":{"type":"integer"},
                    "branch_id":{"type":"string"}, "decision_ref":{"type":"string"}, "before_event":{"type":"integer"}}
                main_tools.append(pi_clauses.TOOL)
            roots = Roots(self.conn, run_id=self.rid, cwd=thread_dir, main_tools=main_tools,
                          main_call=self.main_read, worker_tools=tools.specs, worker_call=tools.call,
                          worker_prepare=tools.prepare, token_budget=spec.get("turn_token_budget", 65536))
            try:
                self._native = self._stack.enter_context(roots)
            except OSError as exc:
                # Only transport startup: a missing executable or unreachable proxy must not stop GX.
                raise AppServerDisconnected("Codex startup failed: " + type(exc).__name__) from exc
        return self._native

    def close(self):
        self._stack.close()
        self._native = None

    def _scoped_waits(self, key):
        return self.conn.execute("SELECT outbox_id,payload_json FROM outbox WHERE run_id=? AND topic=? "
            "AND consumed_at IS NULL AND json_extract(payload_json,'$.owner')=? "
            "AND json_extract(payload_json,'$.reference')=? ORDER BY outbox_id", (self.rid, NATIVE_WAIT, *key))

    def _scope_pending(self, key):
        owner, ref = key
        if owner == "task":
            row = self.conn.execute("SELECT r.status FROM tasks t JOIN runs r USING(run_id) "
                "WHERE t.run_id=? AND t.task_id=? AND t.status IN ('READY','CLAIMED','RUNNING')",
                (self.rid, ref)).fetchone()
            return bool(row and not machines.is_terminal("run", row["status"]))
        if owner == "message":
            query = ("SELECT 1 FROM p0_operator_messages WHERE run_id=? AND message_id=? "
                     "AND result_json IS NULL AND rejected IS NULL")
        elif owner == "reply":
            query = ("SELECT 1 FROM p0_operator_messages m LEFT JOIN p0_main_replies r USING(message_id) "
                     "WHERE m.run_id=? AND m.message_id=? AND r.message_id IS NULL AND m.rejected IS NULL")
        elif owner == "outbox":
            query = "SELECT 1 FROM outbox WHERE run_id=? AND outbox_id=? AND consumed_at IS NULL"
        else:
            return True
        return self.conn.execute(query, (self.rid, ref)).fetchone() is not None

    def _defer_native(self, key, reason, retry_at, *, disconnected, attempted=False):
        prior = self._waits.get(key)
        if not attempted and prior and (prior["reason"], prior["next_retry_at"]) == (reason, retry_at):
            return retry_at
        owner, ref = key
        with store.transaction(self.conn, transaction_owner="Driver", operation="defer_native", run_id=self.rid):
            # Bootstrap recovery and a cold Driver can both have observed the same missing wait.
            saved = self._scoped_waits(key).fetchone()
            prior = store.loads(saved, "payload_json") if saved else None
            attempts = prior["attempts"] if prior else 0
            if attempted:
                retry_at = clock.iso_after(min(30, 0.5 * 2 ** min(attempts, 6)))
                attempts += 1
            # Another Driver may have acknowledged this input since our inbox or dispatch list was read.
            if not self._scope_pending(key):
                self._end_native_wait(key)
                return retry_at
            data = {"run_id": self.rid, "owner": owner, "reference": ref, "reason": reason,
                    "since": prior["since"] if prior else clock.now_iso(), "next_retry_at": retry_at,
                    "disconnected": disconnected, "attempts": attempts}
            if owner == "task":
                root = self.conn.execute("SELECT r.root_key,r.lease_id,r.attempt FROM p0_roots r JOIN tasks t ON t.lease_id=r.lease_id "
                    "WHERE t.task_id=? AND r.run_id=? AND t.status IN ('CLAIMED','RUNNING')", (ref,self.rid)).fetchone()
                if root:
                    data.update(dict(root))
                    waits.begin(self.conn, root["root_key"], "provider_backoff", "transport:" + root["root_key"])
            if saved:
                oid = saved["outbox_id"]
            else:
                oid = store.enqueue_outbox(self.conn, topic=NATIVE_WAIT, payload=data)
            self.conn.execute("UPDATE outbox SET payload_json=?,attempts=?,last_error=? "
                              "WHERE outbox_id=? AND consumed_at IS NULL", (ids.canonical_json(data), attempts, reason, oid))
        self._waits[key] = {**data, "outbox_id": oid}
        return retry_at

    def _end_native_wait(self, key):
        if key in self._waits or self._scoped_waits(key).fetchone():
            with store.transaction(
                self.conn, transaction_owner="Driver", operation="end_native_wait", run_id=self.rid,
            ):
                for row in self._scoped_waits(key).fetchall():
                    wait = store.loads(row, "payload_json")
                    if root := wait.get("root_key"):
                        waits.finish(self.conn, root, "provider_backoff", "transport:" + root)
                    store.complete_outbox(self.conn, row["outbox_id"], "gx-p0")
            self._waits.pop(key, None)

    def _retire_resolved_waits(self):
        # Another transaction may have cancelled the task, acknowledged the input or consumed its notice.
        for wait in native_waits(self.conn, self.rid):
            key = (wait["owner"], wait["reference"])
            if not self._scope_pending(key):
                self._end_native_wait(key)

    def _native_call(self, owner, reference, operation):
        """Defer only this native operation. Its owning task, turn, delivery or hold stays durable."""
        key = (owner, str(reference))
        row = self._scoped_waits(key).fetchone()
        prior = store.loads(row, "payload_json") if row else None
        if prior:
            self._waits[key] = {**prior, "outbox_id": row["outbox_id"]}
        else:
            self._waits.pop(key, None)
        if self.offline:
            self._defer_native(key, "Codex disabled by " + OFFLINE, None, disconnected=True)
            return _DEFERRED
        now = clock.now_iso()
        if prior and (prior["next_retry_at"] or "") > now:
            return _DEFERRED
        if self._backoff > now:
            self._defer_native(key, "Codex transport is waiting to reconnect", self._backoff,
                               disconnected=True)
            return _DEFERRED
        try:
            native = self.native
            if owner != "connection" and self._waits.get(("connection", self.rid), {}).get("disconnected"):
                self._end_native_wait(("connection", self.rid))
            if prior and (root_key := prior.get("root_key")):
                # Re-open only the retained attempt, under the existing capacity check, before a native write.
                from .team_admission import require_capacity
                with store.transaction(
                    self.conn, transaction_owner="Driver", operation="reopen_native", run_id=self.rid,
                ):
                    root = self.conn.execute("SELECT r.* FROM p0_roots r JOIN tasks t ON r.lease_id=t.lease_id "
                        "WHERE r.root_key=? AND t.status IN ('CLAIMED','RUNNING')", (root_key,)).fetchone()
                    if root:
                        require_capacity(self.conn, dict(root))
                        waits.finish(self.conn, root_key, "provider_backoff", "transport:" + root_key)
            result = operation(native)
        except HoldActive as exc:
            if exc.details.get("account_bucket"):
                from . import activity
                activity.refresh_blocked_account(native, exc)
            raise  # The normal admission path retries the original task with the refreshed facts.
        except (AppServerDisconnected, AppServerTimeout, OutcomeUnknown, AppServerResponseError) as exc:
            if self.conn.in_transaction or isinstance(exc, OutcomeUnknown) and "rollback_error" in exc.details:
                raise
            disconnected = isinstance(exc, (AppServerDisconnected, AppServerTimeout))
            reason = type(exc).__name__
            if isinstance(exc, AppServerResponseError):
                reason += " " + str(exc.code)
            if isinstance(exc.__cause__, OSError):
                reason += " during startup (" + type(exc.__cause__).__name__ + ")"
            retry_at = self._defer_native(key, reason, None, disconnected=disconnected, attempted=True)
            if disconnected:
                self._backoff = retry_at
                # Preserve any already-dispatched, still-owned attempts on this connection across a long outage.
                for task_id, attempt_id in self._sent:
                    current = self.conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND run_id=? AND attempt=? "
                        "AND status IN ('CLAIMED','RUNNING')", (task_id,self.rid,attempt_id)).fetchone()
                    if current and key != ("task", task_id):
                        self._defer_native(("task", task_id), reason, retry_at, disconnected=True)
                self._sent.clear()
                self._checked_costs.clear()
                self.close()
            return _DEFERRED
        self._end_native_wait(key)
        return result

    def messages(self, *, local_only=False, pending=None, through_message=None):
        from . import operator
        rows = pending
        if rows is None:
            query = "SELECT * FROM p0_operator_messages WHERE run_id=? AND result_json IS NULL AND rejected IS NULL "
            args = [self.rid]
            if through_message:
                query += "AND rowid<=(SELECT rowid FROM p0_operator_messages WHERE run_id=? AND message_id=?) "
                args.extend([self.rid, through_message])
            rows = self.conn.execute(query + "ORDER BY rowid", args).fetchall()
        native_rows = []
        native_blocked = False
        for row in rows:
            mid = row["message_id"]
            try:
                successor = self.conn.execute("SELECT successor_json FROM p0_conversation_messages WHERE message_id=? "
                                              "AND successor_json IS NOT NULL", (mid,)).fetchone()
                if successor:
                    from .conversation_delivery import start_successor
                    start_successor(self.conn, self.directory, self.rid, mid, row["message_text"],
                                    json.loads(successor[0]), native_owner=True)
                    continue
                if pending is None and operator.consume_queued(self.ctx, self.rid, row):
                    self._end_native_wait(("message", mid))
                    continue
                if local_only:
                    native_rows.append(row)
                    continue
                if native_blocked:
                    continue
                outcome = self._native_call("message", mid, lambda native: self._human_turn(native, row))
                native_blocked = outcome is _DEFERRED
            except HoldActive:
                # In particular, a confirmation must receive its own later main turn.
                native_blocked = True
            except BudgetExceeded as exc:
                self._exhausted["message:"+mid] = exc
                native_blocked = True
            except (ArtifactNotFound, IdempotencyConflict, PermissionDenied, SchemaInvalid, StaleRevision, ValueError) as exc:
                # Transport and uncertain native outcomes remain durable and can be reconciled.
                with store.transaction(
                    self.conn, transaction_owner="Driver", operation="reject_message", run_id=self.rid,
                ) as tx:
                    tx.execute("UPDATE p0_operator_messages SET rejected=? WHERE message_id=? AND result_json IS NULL",
                               (str(exc)[:500], mid))
                self._end_native_wait(("message", mid))
        return native_rows

    def _human_turn(self, native, row):
        mid = row["message_id"]
        def stage(thread_id, message_id):
            if (thread_id, message_id) != (thread, mid):
                raise ValueError("Human message binding changed")
            intent.stage_input(self.conn, self.rid, mid, store.loads(row, "action_json"))
        def accepted(thread_id, turn_id, message_id):
            if (thread_id, message_id) != (thread, mid):
                raise ValueError("Human turn acknowledgment does not match the submitted input")
            with store.transaction(
                self.conn, transaction_owner="Driver", operation="accept_message", run_id=self.rid,
            ) as tx:
                result = intent.accept_input(tx, self.rid, thread_id, turn_id, message_id)
                tx.execute("UPDATE p0_operator_messages SET result_json=? WHERE message_id=?",
                           (ids.canonical_json(result), mid))
        # A crash can separate the official acknowledgement from local Intent acceptance.
        # Consume the original receipt before looking for a new physical Main.
        prior = self.conn.execute("SELECT thread_id,turn_id FROM p0_root_deliveries WHERE run_id=? AND event_id=? "
                                  "AND human=1 AND state='acknowledged' AND turn_id IS NOT NULL",
                                  (self.rid, "human:"+mid)).fetchone()
        if prior:
            thread = prior["thread_id"]
            accepted(thread, prior["turn_id"], mid)
            return prior["turn_id"]
        thread = native.ensure_main()
        intent.bind_main(self.conn, self.rid, thread)
        return native.human_turn(row["message_text"], before_start=stage, on_started=accepted, message_id=mid)

    def finish_main(self, mid, *, wait=30, local_only=False):
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
            if local_only or time.monotonic() >= deadline:
                return
            delay = max(0.01, min(0.25, deadline-time.monotonic()))
            if self._native_call("reply", mid, lambda native: native.poll(delay)) is _DEFERRED:
                return
        # The Intent owner rechecks the receipt in its own write transaction only when it has work.
        intent.finish_text(db, self.rid, mid)
        if local_only:
            return
        def read_turn(native):
            turns = native.delivery._pages("thread/turns/list", {"threadId":row["thread_id"],
                    "sortDirection":"desc", "itemsView":"full", "limit":8})
            return next((item for item in turns if item["id"] == row["turn_id"]), None)
        turn = self._native_call("reply", mid, read_turn)
        if turn is not None and turn is not _DEFERRED and turn["status"] in ("completed","failed","interrupted"):
            messages = [item for item in turn.get("items", []) if item.get("type") == "agentMessage"]
            final = [item for item in messages if item.get("phase") == "final_answer"]
            if not final and turn["status"] == "completed":
                final = [item for item in messages if item.get("phase") is None]
            reply = "\n".join(item["text"] for item in final)
            with store.transaction(db, transaction_owner="Driver", operation="save_reply", run_id=self.rid) as tx:
                inserted = tx.execute("INSERT OR IGNORE INTO p0_main_replies VALUES(?,?,?,?)",
                           (mid, row["turn_id"], turn["status"], reply))
                if inserted.rowcount:
                    store.append_event(tx, kind="p0.conversation.replied", run_id=self.rid, actor="codex-main",
                        subject_kind="message", subject_id=mid, payload={"message_id":mid,"turn_id":row["turn_id"],
                                                                       "has_text":bool(reply),"status":turn["status"]})

    def deliveries(self, topics=_LOCAL_TOPICS + _NATIVE_TOPICS):
        marks = ",".join("?" for _ in topics)
        rows = self.conn.execute(f"SELECT * FROM outbox WHERE run_id=? AND consumed_at IS NULL "
                                 f"AND topic IN ({marks}) ORDER BY outbox_id", (self.rid, *topics)).fetchall()
        consumed = False
        for row in rows:
            oid = row["outbox_id"]
            if time.monotonic() < self._retry_at.get(oid, 0):
                continue
            payload = json.loads(row["payload_json"])
            try:
                if row["topic"] == "p0.compute.notice":
                    from . import compute
                    if self._native_call("outbox", oid, lambda native: compute.deliver_notice(native, payload)) is _DEFERRED:
                        continue
                elif row["topic"] == "p0.intent.reevaluate":
                    service.hooks_for_run(self.conn, self.rid).reevaluate(self.conn, payload)
                elif row["topic"] == "p0.supplement.review":
                    if not service.hooks_for_run(self.conn, self.rid).reconcile_supplement(self.conn, self.rid, payload["source_id"]):
                        self._retry_at[oid] = time.monotonic() + 0.5
                        continue
                elif row["topic"] == "p0.intent.cancel_job":
                    JobService(self.conn).cancel(payload["job_id"],
                        reason="operator confirmed cancellation in "+payload["change_id"],
                        idempotency_key=payload["change_id"]+":"+payload["job_id"])
                else:
                    thread = thread_for_lease(self.conn, self.rid, payload["task_id"], payload.get("lease_id"))
                    if thread and self._native_call("outbox", oid, lambda native:
                            self._notify(native, thread, payload, oid)) is _DEFERRED:
                        continue
                    # A task with no Root will see current Intent in its first brief.
                with store.transaction(
                    self.conn, transaction_owner="Outbox", operation="complete_delivery", run_id=self.rid,
                ) as tx:
                    consumed |= store.complete_outbox(tx, oid, "gx-p0")
                self._end_native_wait(("outbox", str(oid)))
            except (BudgetExceeded, HoldActive) as exc:
                if isinstance(exc, BudgetExceeded):
                    self._exhausted["outbox:"+str(oid)] = exc
            except Exception as exc:
                with store.transaction(
                    self.conn, transaction_owner="Outbox", operation="fail_delivery", run_id=self.rid,
                ) as tx:
                    store.fail_outbox(tx, oid, type(exc).__name__ + ": " + str(exc)[:240])
                self._retry_at[oid] = time.monotonic() + min(30, 2 ** min(row["attempts"], 5))
        return consumed

    def _notify(self, native, thread_id, payload, oid):
        from .impact import notification_content
        content = notification_content(self.conn, payload)
        event_id = str(oid)
        interrupt = payload.get("action") == "interrupt"
        original = "GX updated this task. " + ids.canonical_json(payload)
        prior = self.conn.execute("SELECT text_sha256 FROM p0_root_deliveries WHERE run_id=? "
                                  "AND event_id=?", (self.rid,event_id)).fetchone()
        if prior and payload != content and prior[0] == hashlib.sha256(original.encode()).hexdigest():
            native.deliver(thread_id, original, event_id=event_id, interrupt=interrupt)
            event_id += ":content"
        if not (prior and interrupt and event_id.endswith(":content")):
            native.deliver(thread_id, "GX updated this task. " + ids.canonical_json(content),
                           event_id=event_id, interrupt=interrupt)

    def reconcile_costs(self, *, refresh=True):
        from . import terminal_products, v62_budget
        pending = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT thread_id FROM p0_meter_native_turns WHERE run_id=? AND "
            "(budget_status='HELD' OR (confidence='ESTIMATED' AND EXISTS (SELECT 1 FROM p0_root_terminals e "
            "WHERE e.thread_id=p0_meter_native_turns.thread_id AND e.turn_id=p0_meter_native_turns.turn_id)))",
            (self.rid,))}
        closing_threads = {row["thread_id"] for row in terminal_products.pending(self.conn, self.rid)}
        pending.update(closing_threads)
        if self.research_window:
            # A host can accept the original request before GX persists its turn ID.
            # Its stable input ID can still be reconciled after admission has closed.
            pending.update(row[0] for row in self.conn.execute(
                "SELECT DISTINCT thread_id FROM p0_root_deliveries WHERE run_id=? AND state='sending'",
                (self.rid,)))
        # A passive read during Goal closure cannot collect the cumulative snapshot returned by
        # a guarded attach after closure. Revisit that thread when the terminal owner completes.
        reads = {(thread, thread in closing_threads) for thread in pending}
        self._checked_costs.intersection_update(reads)
        for read in sorted(reads if refresh else reads - self._checked_costs):
            thread, _ = read
            if not refresh and thread in getattr(self._native, "loaded", ()):
                # New live turns deliver their own observations; reconnects begin with an empty loaded set.
                self._checked_costs.add(read)
                continue
            if self._native_call("usage", thread, lambda native: native.reconcile_meter(thread)) is not _DEFERRED:
                self._checked_costs.add(read)
        if closing_threads and self._native is not None:
            self._native_call("connection", self.rid, lambda native: native.terminal_products.flush())
        for owner, thread in tuple(self._waits):
            if owner == "usage" and thread not in pending:
                self._end_native_wait((owner, thread))
        # A report can close a module before its native turn delivers the final
        # usage receipt. The scientific Run may already be terminal on recovery.
        v62_budget.release_finished(self.conn, self.rid)

    @property
    def read_fds(self):
        client = getattr(self._native, "client", None)
        return getattr(getattr(client, "transport", None), "read_fds", ())

    def wake(self):
        self.idle_seconds = idle.MIN_SECONDS

    def _event_id(self):
        return self.conn.execute("SELECT MAX(event_id) FROM events WHERE run_id=?", (self.rid,)).fetchone()[0]

    def _next_delay(self, checked_at, started, deadline):
        from . import metering
        now = time.monotonic()
        due = [deadline, now + self.idle_seconds]
        if self.research_window:
            wall = clock.now().timestamp()
            due.extend(now + self.research_window[key] - wall for key in ("start", "new_work_until", "drain_until")
                       if self.research_window[key] > wall)
        due.extend(t for t in (*self._dispatch_deadlines, *self._retry_at.values()) if t > started)
        heartbeat = getattr(self._native, "heartbeats", None)
        connection_wait = self._waits.get(("connection", self.rid), {}).get("next_retry_at")
        if not connection_wait or connection_wait <= checked_at:
            if heartbeat and self._native.active_turns:
                due.append(heartbeat.next_probe)
            if products := getattr(self._native, "terminal_products", None):
                due.extend(products.due.values())
        lease = self.conn.execute("SELECT min(l.expires_at) FROM leases l JOIN tasks t ON t.task_id=l.task_id "
                                  "JOIN runs r ON r.run_id=t.run_id WHERE t.run_id=? AND l.released_at IS NULL "
                                  "AND l.expires_at>? AND r.status<>'PAUSED'", (self.rid, checked_at)).fetchone()[0]
        for at in (lease, *(w.get("next_retry_at") for w in self._waits.values())):
            if at and at > checked_at:
                due.append(time.monotonic() - clock.age_seconds(at))
        from . import operator_usage
        for owner, args in ((JobService.next_poll_seconds, (self.conn, self.rid, checked_at)),
                            (metering.next_deadline_seconds, (self.conn, self.rid)),
                            (operator_usage.next_alert_seconds, (self.conn, self.rid))):
            sampled_at = time.monotonic()
            seconds = owner(*args)
            if seconds is not None:
                due.append(sampled_at + seconds)
        self.idle_seconds = min(idle.MAX_SECONDS, 2 * self.idle_seconds)
        self.wake_at = min(due)
        return max(0, self.wake_at - time.monotonic())

    def drive(self, timeout, *, waiter=None):
        passes = self.passes(time.monotonic() + timeout, waiter=waiter)
        while True:
            try:
                next(passes)
            except StopIteration as end:
                return end.value

    def passes(self, deadline, *, shared=False, waiter=None):
        """Yield between passes so other runs can progress on the same SQLite connection."""
        context = nullcontext(waiter) if shared or waiter is not None else idle.WorkspaceWait(self.conn)
        with context as listener:
            return (yield from self._passes(deadline, shared, listener))

    def _passes(self, deadline, shared, listener):
        cycles = self._cycles(deadline, shared)
        delay = 0
        last_event = self._event_id()
        while time.monotonic() < deadline:
            yield min(delay, max(0, self.wake_at - time.monotonic()))
            if listener and delay:
                ready = listener.wait(self.wake_at - time.monotonic(), (self,))
                if ready is None or ready:
                    self.wake()
            if time.monotonic() >= deadline:
                break
            cycle_start = time.monotonic()
            started = time.monotonic_ns()
            checked_at = clock.now_iso()
            try:
                activity = next(cycles)
                event = self._event_id()
                self.made_progress = bool(activity or event != last_event)
                last_event = event
                if self.made_progress:
                    self.wake()
                    self.wake_at = 0.
                    delay = 0
                else:
                    delay = self._next_delay(checked_at, cycle_start, deadline)
            except StopIteration as end:
                return end.value
            finally:
                metrics.record("p0_driver_cycle", started=started, run_id=self.rid)
        return {**snapshot(self.conn, self.rid), "driver": "time_limit"}

    def _poll(self, delay):
        if self._native is None:
            return False
        return self._native_call("connection", self.rid, lambda native: native.poll(delay)) is True

    def _cycles(self, deadline, shared):
        from . import compute, metering
        run = project(self.conn, self.rid)
        if run and '"plugin_binding"' in (run["inputs_json"] or ""):
            from ..errors import VersionImmutable
            try:
                service.hooks_for_run(self.conn, self.rid)
            except VersionImmutable as exc:
                return {**snapshot(self.conn, self.rid), "driver": "plugin_unavailable", "reason": exc.message}
        sent = self._sent
        retry_delay = {}
        admission_retries = {}
        def defer_admission(key, watch=None):
            previous = admission_retries.get(key)
            delay = min(2.0, 2 * previous[0]) if previous else 0.5
            admission_retries[key] = (delay, time.monotonic() + delay, watch)
        last_terminal_event = None
        while time.monotonic() < deadline:
            if research_window.phase(self.research_window) in ("RECONCILE", "ENDED"):
                # Hand control back to the operator's bounded stop and receipt-only reconciliation.
                return {**snapshot(self.conn, self.rid), "driver": "research_drain_ended"}
            metering.maintain(self.conn, self.rid)
            self._exhausted.clear()
            pending_messages = self.messages(local_only=True)
            progressed = self.deliveries(_LOCAL_TOPICS)
            progressed = compute.drain(self.conn, self.rid) or progressed
            replies = self.conn.execute("SELECT m.message_id FROM p0_operator_messages m "
                    "JOIN p0_root_deliveries d ON d.run_id=m.run_id AND d.event_id=('human:'||m.message_id) "
                    "LEFT JOIN p0_main_replies r USING(message_id) WHERE m.run_id=? AND m.result_json IS NOT NULL "
                    "AND d.turn_id IS NOT NULL AND r.message_id IS NULL ORDER BY m.rowid", (self.rid,)).fetchall()
            for row in replies:
                self.finish_main(row[0], wait=0, local_only=True)
            stopped = {"PAUSED", *machines.terminal_states("run")}
            run = project(self.conn, self.rid)
            if run["status"] in stopped:
                # A stopped Run may still own admitted Jobs. Observe them without expanding a stopped workflow.
                service.seams.reconcile_jobs(self.conn, run_id=self.rid, actor=self.ctx.actor)
            else:
                # lazy owns Job reconciliation, team settlement and reaction to the committed triggers.
                reactor.lazy(self.ctx, self.rid)
                reactor.drive_science(self.ctx, self.rid)
            # Every operation which may open or use the App Server follows the local work above.
            run = project(self.conn, self.rid)
            self.reconcile_costs(refresh=run["status"] in stopped and last_terminal_event is None)
            self.messages(pending=pending_messages)
            for row in replies:
                self.finish_main(row[0], wait=0)
            progressed = self.deliveries(_NATIVE_TOPICS) or progressed
            run = project(self.conn, self.rid)
            self._retire_resolved_waits()
            if run["status"] in stopped:
                # A pause or final scientific decision can commit while an admitted
                # native turn is still streaming. Drain its receipt before closing.
                if self._native and (self._native.active_turns or getattr(self._native, "_child_turns", {})):
                    yield self._poll(0.001) or progressed
                    continue
                if last_terminal_event is None:
                    last_terminal_event = time.monotonic()
                held = self.conn.execute("SELECT confidence FROM p0_meter_native_turns WHERE run_id=? "
                                         "AND budget_status='HELD'", (self.rid,)).fetchall()
                if held:
                    # Parents may finish before the last child reports usage. Drain the live
                    # connection while it has traffic, then expose genuinely missing receipts.
                    estimable = metering.next_deadline_seconds(self.conn, self.rid) is not None
                    unresolved = all(row["confidence"] == "UNRESOLVED" for row in held)
                    if estimable or (self._native is None and not unresolved) or (
                            self._native and time.monotonic() - last_terminal_event < 3):
                        activity = self._poll(0.001)
                        if activity:
                            last_terminal_event = time.monotonic()
                        self._dispatch_deadlines = (last_terminal_event + 3,)
                        yield activity or progressed
                        continue
                result = snapshot(self.conn, self.rid)
                outstanding_delivery = any(w["owner"] in ("message", "outbox", "reply") for w in result["native_waits"])
                if (outstanding_delivery or result.get("pending_terminal_turns") or
                        any(not machines.is_terminal("job", j["status"]) for j in result["jobs"])):
                    yield self._poll(0.001) or progressed
                    continue
                if self._native and getattr(self._native, "terminal_products", None) and self._native.terminal_products.due:
                    self._native_call("connection", self.rid, lambda n: n.terminal_products.flush())
                if ("connection", self.rid) in self._waits and not held:
                    self._end_native_wait(("connection", self.rid))
                    result = snapshot(self.conn, self.rid)
                if held:
                    result["driver"] = "usage_pending"
                return result
            last_terminal_event = None
            tasks = self.conn.execute("SELECT t.task_id,t.status,t.attempt,r.root_key,j.status AS waiting_job, "
                                      "EXISTS (SELECT 1 FROM p0_job_proposals p WHERE p.root_key=r.root_key "
                                      "AND p.wait_turn IS NOT NULL AND p.wait_ended IS NULL) AS waiting_proposal "
                                      "FROM tasks t LEFT JOIN p0_roots r ON r.lease_id=t.lease_id "
                                      "LEFT JOIN jobs j ON j.job_id=r.wait_job_id WHERE t.run_id=? "
                                      "AND t.status IN ('READY','CLAIMED','RUNNING') ORDER BY t.rowid",
                                      (self.rid,)).fetchall()
            opened = {}
            if admission_retries:
                ready_attempts = {(t["task_id"], t["attempt"] + 1) for t in tasks if t["status"] == "READY"}
                for ended in admission_retries.keys() - ready_attempts:
                    del admission_retries[ended]
            for t in tasks:
                if research_window.phase(self.research_window) != "OPEN":
                    break
                if t["waiting_proposal"] or (t["waiting_job"] and not machines.is_terminal("job", t["waiting_job"])):
                    continue
                key = (t["task_id"], t["attempt"] + (t["status"] == "READY"))
                if key in admission_retries:
                    _, retry_at, watch = admission_retries[key]
                    if time.monotonic() < retry_at:
                        if watch is None:
                            continue
                        if watch not in opened:
                            opened[watch] = watch.opened(self.conn)
                        if not opened[watch]:
                            continue
                retry = False
                if sent.get(key):
                    latest, resumable = waits.worker_turn(self.conn, t["root_key"], cached=True)
                    if latest:
                        sent[key] = latest
                    if resumable:
                        delay, at = retry_delay.setdefault(key, (0.5, time.monotonic() + 0.5))
                        if time.monotonic() >= at:
                            retry = True
                            retry_delay[key] = (min(30, delay * 2), time.monotonic() + min(30, delay * 2))
                if key not in sent or retry or t["waiting_job"] in machines.terminal_states("job"):
                    try:
                        started = time.monotonic_ns()
                        with metrics.identity(run_id=self.rid, task_id=t["task_id"]):
                            try:
                                receipt = self._native_call("task", t["task_id"], lambda n: n.dispatch(t["task_id"]))
                            finally:
                                metrics.record("p0_dispatch", started=started)
                    except (IllegalTransition, LeaseExpired, ScopeDenied):
                        # A stop or Goal edit can commit while Codex is reporting the thread state.
                        fresh = self.conn.execute("SELECT t.status,t.attempt,r.status AS run_status FROM tasks t "
                                                  "JOIN runs r USING(run_id) WHERE t.run_id=? AND t.task_id=?",
                                                  (self.rid, t["task_id"])).fetchone()
                        if fresh and fresh["run_status"] in stopped:
                            break
                        if fresh and (machines.is_terminal("task", fresh["status"]) or fresh["attempt"] > key[1]):
                            continue
                        raise
                    except HoldActive as exc:
                        if t["status"] == "READY":
                            defer_admission(key, getattr(exc, "slot_watch", None))
                        continue
                    except BudgetExceeded as exc:
                        self._exhausted[t["task_id"]] = exc
                        continue
                    if receipt is _DEFERRED:
                        continue
                    if receipt.get("waiting_for_review"):
                        if t["status"] == "READY":
                            defer_admission(key)
                        continue
                    admission_retries.pop(key, None)
                    sent[key] = receipt.get("turn_id")
                    progressed = True
            self._dispatch_deadlines = tuple(item[1] for item in (*retry_delay.values(), *admission_retries.values()))
            if self._poll(0.001):
                # Drain a burst of native items before sleeping or re-reading every GX checkpoint.
                until = min(deadline, time.monotonic() + (0.025 if shared else 0.15))
                while time.monotonic() < until and self._poll(0.005):
                    pass
                yield True
                continue
            if self._exhausted and not progressed and self._native and not self._native.active_turns:
                active_job = self.conn.execute(
                    "SELECT 1 FROM jobs WHERE run_id=? AND status IN "
                    "('CREATED','SUBMITTING','SUBMITTED','QUEUED','RUNNING','CANCEL_REQUESTED','OUTCOME_UNKNOWN') LIMIT 1",
                    (self.rid,)).fetchone()
                if not active_job and not self._native.has_pending_children():
                    cause = next(iter(self._exhausted.values()))
                    service.run_control(self.ctx, {"run_id":self.rid, "action":"pause",
                                        "reason":"budget_exhausted: "+cause.message,
                                        "idempotency_key":str(uuid.uuid4())})
                    return snapshot(self.conn, self.rid)
            yield progressed
        result = snapshot(self.conn, self.rid)
        result["driver"] = "time_limit"
        return result
