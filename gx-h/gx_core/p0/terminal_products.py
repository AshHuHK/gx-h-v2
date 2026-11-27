"""Repair and close a native product callback on its original Root, Turn and lease."""
from __future__ import annotations

import json
import sqlite3
import time

from ..domain import clock, ids
from ..errors import SchemaInvalid, ScopeDenied
from ..hostobs.app_server_client import AppServerResponseError, AppServerTimeout
from ..persistence import store
from ..teams.service import TeamService
from . import metering
from .response_attribution import FINAL_TOOLS
from .root_delivery import TERMINAL

NAMESPACE = "gx"


def direct_tool_config(effective):
    """Keep the terminal tool outside yieldable code-mode cells on the pinned host."""
    mode = (effective.get("features") or {}).get("code_mode")
    existing = mode.get("direct_only_tool_namespaces", []) if isinstance(mode, dict) else []
    config = {"features.code_mode.direct_only_tool_namespaces": list(dict.fromkeys([*existing, NAMESPACE])),
              "features.code_mode_only_strict_3p_tools": False}
    if type(mode) is bool:
        # A dotted option promotes a TOML boolean into a table. Keep its original switch.
        config["features.code_mode.enabled"] = mode
    return config


def repair_limit(spec):
    value = spec.get("max_product_structural_failures", 2)
    if type(value) is not int or value < 1:
        raise SchemaInvalid("max_product_structural_failures must be a positive integer")
    return value


def pending(conn, run_id):
    return conn.execute(
        "SELECT e.thread_id,e.turn_id FROM p0_roots r JOIN p0_root_terminals e USING(thread_id) "
        "LEFT JOIN p0_root_turn_usage u ON u.thread_id=e.thread_id AND u.turn_id=e.turn_id "
        "WHERE r.run_id=? AND (e.goal_checked_at IS NULL OR u.terminal_status IS NULL)", (run_id,)
    ).fetchall()


class _AwaitingRpc(Exception):
    """A timed-out request already occupies this bounded recovery slot."""


class TerminalProducts:
    # After this interval read official state before retrying an exact-turn interrupt.
    # A callback cannot perform a nested App Server read.
    retry_seconds = 2
    rpc_timeout = 2
    per_pass = 2
    pending_per_method = 8
    response_grace_seconds = 1.0

    def __init__(self, roots):
        self.roots = roots
        self.conn = roots.conn
        spec = store.loads(roots.run, "inputs_json", {}).get("inputs", {}).get("p0_spec", {})
        self.limit = repair_limit(spec)
        self.due = {(row["thread_id"], row["turn_id"]): 0.0 for row in pending(self.conn, roots.run_id)}
        self.fresh = {}
        self.late_rpcs = {}
        self.late_results = {}
        self.retire_after = {}
        self.cursors = {}

    def record(self, thread, turn, call, reason):
        self.conn.execute(
            "INSERT INTO p0_root_terminals(thread_id,turn_id,call_id,reason,recorded_at) VALUES(?,?,?,?,?)",
            (thread, turn, call, reason, clock.now_iso()))
        self.conn.execute("UPDATE p0_roots SET goal_desired='complete' WHERE thread_id=?", (thread,))

        def committed():
            self.due[thread, turn] = 0.0
            self.fresh[thread, turn] = None

        store.on_transaction_end(self.conn, on_commit=committed)

    def await_live_response(self, params, *, accepted):
        thread, turn, call = (params[name] for name in ("threadId", "turnId", "callId"))
        key = (thread, turn)
        if (key in self.fresh and self.fresh[key] is None
                and self.roots.delivery.attribution.pending_direct_product(thread, turn, call)
                and self.is_terminal(thread, turn, call)):
            grace = self.response_grace_seconds
            if accepted:
                # The unanswered call holds the host on this response while its usage arrives.
                grace = min(metering.policy(self.conn, self.roots.run_id)["grace_seconds"],
                            self.roots.client.default_timeout)
            self.fresh[key] = (call, time.monotonic() + grace)

    def for_thread(self, thread):
        return self.conn.execute("SELECT e.turn_id,e.call_id FROM p0_root_terminals e "
                                 "JOIN p0_roots r USING(thread_id) WHERE e.thread_id=? AND r.run_id=?",
                                 (thread, self.roots.run_id)).fetchone()

    def is_terminal(self, thread, turn, call=None):
        row = self.for_thread(thread)
        return bool(row and row["turn_id"] == turn and (call is None or row["call_id"] == call))

    def replayed_failure(self, thread, turn, call, request_sha):
        row = self.conn.execute(
            "SELECT turn_id,request_sha,response_json FROM p0_product_repairs WHERE thread_id=? AND call_id=?",
            (thread, call)).fetchone()
        if row is None:
            return None
        if row["turn_id"] != turn or row["request_sha"] != request_sha:
            raise ScopeDenied("conflicting root callback identifier")
        return json.loads(row["response_json"])

    def failed_call(self, params, error):
        """Account once after the failing scientific transaction (including publication) rolled back."""
        from .roots import require_binding
        if self.conn.in_transaction:
            raise RuntimeError("roll back the product transaction before recording its structural failure")
        thread, turn, call = (params[key] for key in ("threadId", "turnId", "callId"))
        digest = ids.sha256_of_obj([params["tool"], params["arguments"]])
        category = error.details["category"]
        with store.transaction(self.conn, transaction_owner="TerminalProducts", operation="record_repair",
                               run_id=self.roots.run_id):
            prior = self.replayed_failure(thread, turn, call, digest)
            if prior is not None:
                return prior
            binding = require_binding(self.conn, self.roots.run_id, thread)
            if self.roots.active_turns.get(thread) != turn or self.conn.execute(
                    "SELECT 1 FROM p0_root_calls WHERE thread_id=? AND call_id=?", (thread, call)).fetchone():
                raise ScopeDenied("the failed callback no longer belongs to this active Root Turn")
            if category == "module_report_structure":
                from . import v6_decisions
                v6_decisions.module_authority(self.conn, run_id=self.roots.run_id, task_id=binding["task_id"],
                                              lease_id=binding["lease_id"], thread_id=thread, turn_id=turn)
            count = 1 + self.conn.execute("SELECT count(*) FROM p0_product_repairs WHERE thread_id=? AND category=?",
                                          (thread, category)).fetchone()[0]
            response = {"error": error.message[:1000], "code": error.code, "category": category,
                        "contract_digest": error.details["contract_digest"],
                        "pointer": str(error.details.get("pointer", ""))[:400],
                        "failure_count": count, "max_failures": self.limit, "terminal": count >= self.limit}
            if response["terminal"]:
                product = "module report" if category == "module_report_structure" else "product"
                reason = f"GX {product} contract reached {self.limit} distinct structural failures in this attempt"
                response["task_status"] = TeamService(self.conn).recover_failed_attempt(
                    task_id=binding["task_id"], lease_ref=binding["lease_id"], reason=reason)
                self.record(thread, turn, call, "GX_REPAIR_LIMIT")
            encoded = ids.canonical_json(response)
            if category == "module_report_structure":
                # All native Goal tools share this immutable call-ID registry. A refused report
                # cannot be replayed as a decision or a source operation with the same ID.
                self.conn.execute("INSERT INTO p0_root_calls VALUES(?,?,?,?,?)", (thread, call, turn, digest, encoded))
            self.conn.execute(
                "INSERT INTO p0_product_repairs(thread_id,call_id,turn_id,request_sha,category,contract_digest,"
                "response_json,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (thread, call, turn, digest, response["category"], response["contract_digest"],
                 encoded, clock.now_iso()))
            return response

    def _rpc(self, method, params):
        thread = params["threadId"]
        key = (method, thread, params.get("turnId"), params.get("cursor"))
        if key in self.late_results:
            return self.late_results.pop(key)
        for request_id, op in tuple(self.late_rpcs.items()):
            if op == key and time.monotonic() >= self.retire_after[request_id]:
                # Reads and setting an existing Goal complete are idempotent. Reaching this
                # point for a repeat interrupt requires fresh inProgress proof for this Turn.
                self._retire(request_id)
        unresolved = tuple(self.late_rpcs.values())
        # Reserve a lane for interrupt even when all Goal reads are unavailable.
        # A single lost interrupt may be retried once after fresh active-turn proof.
        if (unresolved.count(key) >= (2 if method == "turn/interrupt" else 1)
                or sum(other[0] == method for other in unresolved) >= self.pending_per_method):
            raise _AwaitingRpc
        if method == "turn/interrupt":
            self.conn.execute("UPDATE p0_root_terminals SET interrupt_attempts=interrupt_attempts+1 "
                              "WHERE thread_id=? AND turn_id=?", (thread, params["turnId"]))
        try:
            return self.roots._request(method, params, timeout=self.rpc_timeout)
        except AppServerTimeout as exc:
            if exc.request_id is not None:
                self.late_rpcs[exc.request_id] = key
                self.retire_after[exc.request_id] = time.monotonic() + 10
            raise

    def _retire(self, request_id):
        self.roots.client.retire(request_id)
        self.late_rpcs.pop(request_id)
        self.retire_after.pop(request_id)

    def _turn_closed(self, thread, turn):
        for request_id, (method, owner, target, _) in tuple(self.late_rpcs.items()):
            if owner == thread and ((method == "turn/interrupt" and target == turn) or method == "thread/turns/list"):
                self._retire(request_id)

    def _turn(self, thread, turn):
        params = {"threadId": thread, "sortDirection": "desc", "itemsView": "notLoaded", "limit": 32}
        key = (thread, turn)
        if key in self.cursors:
            params["cursor"] = self.cursors[key]
        page = self._rpc("thread/turns/list", params)
        self.cursors.pop(key, None)
        for item in page["data"]:
            if item["id"] == turn:
                return item
        if page.get("nextCursor") and page["nextCursor"] != params.get("cursor"):
            self.cursors[key] = page["nextCursor"]
        return None

    def _complete_goal(self, thread):
        # A failed Goal call must not leave a terminal scientific callback running.
        goal = self._rpc("thread/goal/get", {"threadId": thread})["goal"]
        if goal and goal["status"] not in ("complete", "budgetLimited"):
            result = self._rpc("thread/goal/set", {"threadId": thread, "origin": "automatic", "status": "complete"})
            if result["goal"]["status"] != "complete":
                return False
        self.conn.execute("UPDATE p0_root_terminals SET goal_checked_at=?,goal_last_error=NULL WHERE thread_id=?",
                          (clock.now_iso(), thread))
        return True

    @staticmethod
    def _error(exc):
        return str(exc.code) if isinstance(exc, AppServerResponseError) else "timeout"

    def _deliver(self, thread, turn, live, stream):
        state = self.conn.execute(
            "SELECT e.goal_checked_at,u.terminal_status,r.status AS run_status FROM p0_root_terminals e "
            "LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) JOIN runs r ON r.run_id=? "
            "WHERE e.thread_id=? AND e.turn_id=?", (self.roots.run_id, thread, turn)).fetchone()
        goal_done = bool(state["goal_checked_at"])
        if not goal_done:
            try:
                goal_done = self._complete_goal(thread)
            except _AwaitingRpc:
                pass
            except (AppServerResponseError, AppServerTimeout) as exc:
                self.conn.execute("UPDATE p0_root_terminals SET goal_last_error=? WHERE thread_id=?",
                                  (self._error(exc), thread))
        if state["terminal_status"]:
            self._turn_closed(thread, turn)
            return goal_done
        if stream and state["run_status"] not in ("PAUSED", "CANCELLED") and self.roots.active_turns.get(thread) == turn:
            call, deadline = stream
            now = time.monotonic()
            if now < deadline:
                try:
                    complete = metering.terminal_response_observed(self.conn, thread, turn, call)
                except sqlite3.Error:
                    complete = False
                if not complete:
                    # This exact unanswered direct call blocks the next inference. Keep pumping
                    # the existing stream outside the callback until completion or the deadline.
                    self.fresh[thread, turn] = stream
                    self.due[thread, turn] = min(deadline, now + 0.025)
                    return False
        if not live or self.roots.active_turns.get(thread) != turn:
            original = self._turn(thread, turn)
            if original and original["status"] in TERMINAL:
                if self.roots.active_turns.get(thread) == turn:
                    self.roots.active_turns.pop(thread)
                self.roots.delivery.observe("turn/completed", {"threadId": thread, "turn": original})
                self.roots.delivery.reconcile(thread)
                self._turn_closed(thread, turn)
                return goal_done
            if not original or original["status"] != "inProgress":
                return False
        # Authenticated callback or fresh official history proved this specific target active.
        # A successful interrupt RPC still needs turn/completed or history to prove termination.
        self._rpc("turn/interrupt", {"threadId": thread, "turnId": turn})
        self.conn.execute("UPDATE p0_root_terminals SET interrupt_ack_at=coalesce(interrupt_ack_at,?),"
                          "interrupt_last_error=NULL "
                          "WHERE thread_id=?", (clock.now_iso(), thread))
        return False

    def flush(self):
        for request_id, key in tuple(self.late_rpcs.items()):
            if self.roots.client.response_received(request_id) is not None:
                del self.late_rpcs[request_id]
                del self.retire_after[request_id]
                try:
                    result = self.roots.client.wait(request_id, timeout=self.rpc_timeout)
                except AppServerResponseError:
                    continue
                method, thread, turn, _ = key
                if method == "turn/interrupt":
                    self.conn.execute("UPDATE p0_root_terminals SET interrupt_ack_at=coalesce(interrupt_ack_at,?),"
                                      "interrupt_last_error=NULL WHERE thread_id=? AND turn_id=?",
                                      (clock.now_iso(), thread, turn))
                elif any(target[0] == thread for target in self.due):
                    self.late_results[key] = result
        now = time.monotonic()
        due = sorted((at, key) for key, at in self.due.items() if at <= now)
        for _, key in due[:self.per_pass]:
            self.due[key] = now + self.retry_seconds
            live = key in self.fresh
            stream = self.fresh.pop(key, None)
            try:
                if self._deliver(*key, live, stream):
                    self.due.pop(key)
                    self.cursors.pop(key, None)
                    self.late_results = {op: result for op, result in self.late_results.items() if op[1] != key[0]}
                    # This Root's only product Turn and Goal are both independently reconciled.
                    # Lost acknowledgements on this thread cannot consume another Root's capacity.
                    for request_id, op in tuple(self.late_rpcs.items()):
                        if op[1] == key[0]:
                            self._retire(request_id)
            except _AwaitingRpc:
                continue
            except (AppServerResponseError, AppServerTimeout) as exc:
                # There is no atomic proof that the original server request is still pending.
                # Re-read this Turn and retry only its interrupt. Never write a fallback response.
                self.conn.execute("UPDATE p0_root_terminals SET interrupt_last_error=? WHERE thread_id=?",
                                  (self._error(exc), key[0]))
