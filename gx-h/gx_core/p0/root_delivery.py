"""Codex turn delivery, durable receipts and GX budget settlement."""
from __future__ import annotations

import hashlib
import uuid

from .. import budgets
from ..errors import (BudgetExceeded, HoldActive, IdempotencyConflict, LeaseExpired,
                      OutcomeUnknown, SchemaInvalid, ScopeDenied)
from ..hostobs.app_server_client import AppServerResponseError
from ..persistence import store

ACTOR = "p0-root"
TERMINAL = frozenset({"completed", "failed", "interrupted"})


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _idle_refusal(error, method):
    return (error.message == f"no active turn to {method}"
            or (error.message.startswith("expected active turn id ") and " but found " in error.message))


class RootDelivery:
    def __init__(self, roots):
        self.roots = roots
        self.conn = roots.conn
        self.run_id = roots.run_id

    def _get(self, event_id):
        return self.conn.execute("SELECT * FROM p0_root_deliveries WHERE run_id=? AND event_id=?",
                                 (self.run_id, event_id)).fetchone()

    def active(self, thread_id):
        thread = self.roots._request("thread/read", {"threadId": thread_id})["thread"]
        if thread["id"] != thread_id:
            raise OutcomeUnknown("Codex returned a different thread")
        status = thread["status"]["type"]
        if status == "idle":
            return None
        if status != "active":
            raise HoldActive(f"Codex thread is {status}, resume before delivery")
        page = self.roots._request("thread/turns/list", {"threadId": thread_id,
                                   "sortDirection": "desc", "itemsView": "notLoaded", "limit": 2})
        turn = next((t for t in page["data"] if t["status"] == "inProgress"), None)
        if turn:
            return turn["id"]
        check = self.roots._request("thread/read", {"threadId": thread_id})["thread"]
        if check["id"] == thread_id and check["status"]["type"] == "idle":
            return None
        raise HoldActive("the active thread has not published a steerable turn ID")

    def _binding(self, thread_id, *, interrupt, human):
        binding = self.roots._binding(thread_id)
        if not binding or binding["run_id"] != self.run_id:
            raise ScopeDenied("only a bound root in this run may receive GX input")
        task_id = binding.get("task_id")
        if not task_id:
            if interrupt:
                raise ScopeDenied("a GX lease interruption requires its task root")
            return binding
        if human:
            raise ScopeDenied("human input must be delivered to the registered main thread")
        task = self.conn.execute("SELECT lease_id, attempt, status FROM tasks WHERE task_id=? AND run_id=?",
                                 (task_id, self.run_id)).fetchone()
        lease = self.conn.execute("SELECT released_at FROM leases WHERE lease_id=? AND task_id=?",
                                  (binding["lease_id"], task_id)).fetchone()
        current = (task and lease and lease["released_at"] is None and task["lease_id"] == binding["lease_id"]
                   and task["attempt"] == binding["attempt"] and task["status"] in ("CLAIMED", "RUNNING"))
        if interrupt:
            if current:
                raise ScopeDenied("commit the GX attempt fence before interrupting Codex")
        elif current:
            store.validate_lease(self.conn, binding["lease_id"], task_id=task_id)
        else:
            raise LeaseExpired("the root no longer holds the task attempt")
        return binding

    def _reserve(self, thread_id, event_id):
        ref = budgets.budget_for(self.conn, run_id=self.run_id)
        available = int(budgets.state(self.conn, ref)["available"].get("tokens", 0))
        ceiling = self.roots.token_budget
        if ceiling is not None and (type(ceiling) is not int or ceiling <= 0):
            raise SchemaInvalid("the per-turn token allocation must be a positive integer")
        tokens = available if ceiling is None else min(ceiling, available)
        if tokens <= 0:
            raise BudgetExceeded("the run has no remaining tokens for this turn")
        # The upstream snapshot is cumulative over the thread. An earlier turn with unknown
        # usage precludes assigning a shared delta to its successor.
        previous = self.conn.execute(
            "SELECT d.turn_id,u.total_tokens,u.terminal_status FROM p0_root_deliveries d "
            "LEFT JOIN p0_root_turn_usage u ON u.thread_id=d.thread_id AND u.turn_id=d.turn_id "
            "WHERE d.thread_id=? AND d.method='turn/start' AND d.reservation_ref IS NOT NULL "
            "ORDER BY d.rowid DESC LIMIT 1", (thread_id,)).fetchone()
        if previous and previous["terminal_status"] is None:
            raise HoldActive("the previous turn still needs its terminal receipt")
        prior = previous["total_tokens"] if previous else 0
        while True:
            try:
                hold = budgets.reserve(self.conn, budget_ref=ref, subject_kind="native_turn", subject_id=event_id,
                                       amounts={"tokens": tokens, "api_calls": 1}, actor=ACTOR)
                return hold["reservation_ref"], tokens, prior
            except BudgetExceeded as exc:
                # Use only an explicit positive smaller token allowance returned by the
                # budget owner, including a tighter project phase or Intent item.
                limits = [exc.details, *exc.details.get("shortfalls", [])]
                smaller = [int(line["available"]) for line in limits if line.get("key") == "tokens"
                           and 0 < int(line.get("available", 0)) < tokens]
                if not smaller:
                    raise
                tokens = min(smaller)

    def _prepare(self, thread_id, text, event_id, human, interrupt, client_id, active):
        with store.transaction(self.conn):
            row = self._get(event_id)
            if row:
                if (row["thread_id"], row["text_sha256"], row["client_id"], row["human"], row["interrupt"]) != (
                        thread_id, _digest(text), client_id, human, interrupt):
                    raise IdempotencyConflict("this event ID names another Codex delivery")
                if row["state"] != "ready":
                    return row, False
            self._binding(thread_id, interrupt=interrupt, human=human)
            if self.conn.execute("SELECT 1 FROM p0_root_deliveries WHERE thread_id=? AND state='sending' "
                                 "AND event_id<>?", (thread_id, event_id)).fetchone() and not interrupt:
                raise HoldActive("another input to this thread still needs a delivery receipt")
            if human and active:
                raise HoldActive("a new operator message needs a distinct idle main-thread turn")
            if not row:
                self.conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,"
                                  "human,interrupt,state) VALUES(?,?,?,?,?,?,?,'ready')",
                                  (self.run_id, event_id, thread_id, client_id, _digest(text), human, interrupt))
            method = "turn/interrupt" if interrupt else "turn/steer" if active else "turn/start"
            ref, tokens, prior = self._reserve(thread_id, event_id) if method == "turn/start" else (None, None, None)
            state = "acknowledged" if interrupt and not active else "sending"
            self.conn.execute("UPDATE p0_root_deliveries SET method=?,target_turn=?,reservation_ref=?,token_budget=?,"
                              "prior_tokens=?,state=? WHERE run_id=? AND event_id=?",
                              (method, active, ref, tokens, prior, state, self.run_id, event_id))
            return self._get(event_id), True

    def _ack(self, event_id, turn_id):
        incoming = self._get(event_id)
        if incoming["state"] == "sending" and incoming["method"] == "turn/start":
            prior = self.conn.execute(
                "SELECT 1 FROM p0_root_deliveries WHERE thread_id=? AND turn_id=? AND method='turn/start' "
                "AND state='acknowledged' AND event_id<>?", (incoming["thread_id"], turn_id, event_id)).fetchone()
            if prior:
                # turn/start may steer when an independent client wins the idle race.
                budgets.release(self.conn, incoming["reservation_ref"], actor=ACTOR,
                                reason="Codex attached this input to an existing turn")
                self.conn.execute("UPDATE p0_root_deliveries SET method='turn/steer',reservation_ref=NULL,"
                                  "token_budget=NULL,prior_tokens=NULL WHERE run_id=? AND event_id=?",
                                  (self.run_id, event_id))
        self.conn.execute("UPDATE p0_root_deliveries SET state='acknowledged',turn_id=? "
                          "WHERE run_id=? AND event_id=? AND state='sending'", (turn_id, self.run_id, event_id))
        row = self._get(event_id)
        if row["turn_id"] != turn_id:
            raise OutcomeUnknown("Codex returned inconsistent turn receipts")
        self._settle(row["thread_id"], turn_id)

    def _settle(self, thread_id, turn_id):
        row = self.conn.execute(
            "SELECT d.reservation_ref,d.prior_tokens,u.total_tokens FROM p0_root_deliveries d "
            "JOIN p0_root_turn_usage u USING(thread_id,turn_id) JOIN budget_reservations r "
            "ON r.reservation_ref=d.reservation_ref WHERE d.thread_id=? AND d.turn_id=? "
            "AND d.state='acknowledged' AND u.terminal_status IS NOT NULL AND r.status='HELD'",
            (thread_id, turn_id)).fetchone()
        if row and row["total_tokens"] is not None and row["prior_tokens"] is not None and row["total_tokens"] >= row["prior_tokens"]:
            budgets.settle(self.conn, row["reservation_ref"],
                           actual={"tokens": row["total_tokens"] - row["prior_tokens"], "api_calls": 1},
                           actor=ACTOR)

    @staticmethod
    def _receipt(row):
        if row["human"] and row["method"] != "turn/start":
            raise ScopeDenied("the operator input was merged into an existing turn; use a distinct human turn")
        return {key: row[key] for key in ("event_id", "thread_id", "turn_id", "method", "state", "client_id",
                                           "reservation_ref", "token_budget")}

    def _arrived(self, thread_id, turn_id, item):
        if item.get("type") != "userMessage" or not isinstance(item.get("clientId"), str):
            return
        content = item.get("content")
        if not isinstance(content, list) or len(content) != 1:
            return
        part = content[0]
        if part.get("type") != "text" or not isinstance(part.get("text"), str) or part.get("text_elements"):
            return
        row = self.conn.execute("SELECT * FROM p0_root_deliveries WHERE thread_id=? AND client_id=? "
                                "AND state='sending' AND interrupt=0 AND run_id=?",
                                (thread_id, item["clientId"], self.run_id)).fetchone()
        if row and _digest(part["text"]) == row["text_sha256"] and row["target_turn"] in (None, turn_id):
            self._ack(row["event_id"], turn_id)

    def observe(self, method, params):
        """Called on the owning connection. Never initiate an RPC inside a callback."""
        thread_id = params.get("threadId")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if not thread_id or not turn_id or not self.roots._binding(thread_id):
            return
        with store.transaction(self.conn):
            if method in ("item/started", "item/completed"):
                self._arrived(thread_id, turn_id, params.get("item") or {})
            elif method == "thread/tokenUsage/updated":
                total = (params.get("tokenUsage") or {}).get("total", {}).get("totalTokens")
                if type(total) is int and total >= 0:
                    self.conn.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,total_tokens) VALUES(?,?,?) "
                                      "ON CONFLICT(thread_id,turn_id) DO UPDATE SET total_tokens="
                                      "max(coalesce(total_tokens,0),excluded.total_tokens)", (thread_id, turn_id, total))
                    self._settle(thread_id, turn_id)
            elif method == "turn/completed" and params["turn"].get("status") in TERMINAL:
                self.conn.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status) VALUES(?,?,?) "
                                  "ON CONFLICT(thread_id,turn_id) DO UPDATE SET terminal_status=excluded.terminal_status",
                                  (thread_id, turn_id, params["turn"]["status"]))
                self.conn.execute("UPDATE p0_root_deliveries SET state='acknowledged',turn_id=target_turn "
                                  "WHERE run_id=? AND thread_id=? AND method='turn/interrupt' AND target_turn=? "
                                  "AND state='sending'", (self.run_id, thread_id, turn_id))
                self._settle(thread_id, turn_id)

    def reconcile(self, thread_id):
        """Search the official durable history, including older pages, for uncertain inputs."""
        rows = self.conn.execute(
            "SELECT d.* FROM p0_root_deliveries d LEFT JOIN budget_reservations r ON r.reservation_ref=d.reservation_ref "
            "WHERE d.run_id=? AND d.thread_id=? AND (d.state='sending' OR r.status='HELD')",
            (self.run_id, thread_id)).fetchall()
        if not rows:
            return
        pending_input = any(row["state"] == "sending" and not row["interrupt"] for row in rows)
        cursor = None
        seen = set()
        while True:
            params = {"threadId": thread_id, "sortDirection": "desc", "itemsView": "full" if pending_input else
                      "notLoaded", "limit": 8}
            if cursor:
                params["cursor"] = cursor
            try:
                page = self.roots._request("thread/turns/list", params)
            except AppServerResponseError as exc:
                if "not materialized yet" in exc.message and "before first user message" in exc.message:
                    return
                raise
            for turn in page["data"]:
                for item in turn.get("items") or []:
                    with store.transaction(self.conn):
                        self._arrived(thread_id, turn["id"], item)
                if turn["status"] in TERMINAL:
                    self.observe("turn/completed", {"threadId": thread_id, "turn": turn})
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise OutcomeUnknown("Codex returned an inconsistent history cursor")
            seen.add(cursor)

    def deliver(self, thread_id, text, *, event_id, interrupt=False, human=False, message_id=None):
        if self.conn.in_transaction:
            raise RuntimeError("commit the GX change before Codex delivery")
        if not isinstance(text, str) or not isinstance(event_id, str) or not event_id:
            raise SchemaInvalid("a Codex delivery requires a text body and stable event ID")
        client_id = message_id or str(uuid.uuid5(uuid.NAMESPACE_URL, self.run_id + "/" + event_id))
        while True:
            row = self._get(event_id)
            active = None if row and row["state"] != "ready" else self.active(thread_id)
            row, fresh = self._prepare(thread_id, text, event_id, human, interrupt, client_id, active)
            if row["state"] == "acknowledged":
                return self._receipt(row)
            if not fresh:
                self.reconcile(thread_id)
                row = self._get(event_id)
                if row["state"] == "acknowledged":
                    return self._receipt(row)
                raise OutcomeUnknown("the Codex delivery is awaiting its original receipt", details={"event_id": event_id})
            method = row["method"]
            if interrupt:
                params = {"threadId": thread_id, "turnId": active}
            else:
                params = {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}],
                          "clientUserMessageId": client_id}
                if active:
                    params["expectedTurnId"] = active
            try:
                response = self.roots._request(method, params)
            except AppServerResponseError as exc:
                if method == "turn/steer" and _idle_refusal(exc, "steer"):
                    # The protocol explicitly refused this input. The next iteration samples the live turn again.
                    self.conn.execute("UPDATE p0_root_deliveries SET state='ready' WHERE run_id=? "
                                      "AND event_id=? AND state='sending'", (self.run_id, event_id))
                    if self.active(thread_id) == active:
                        raise HoldActive("Codex has not yet published the new active-turn state") from exc
                    continue
                if method == "turn/interrupt" and _idle_refusal(exc, "interrupt") and self.active(thread_id) != active:
                    response = {}
                else:
                    raise
            turn_id = (response.get("turn") or {}).get("id") if method == "turn/start" else response.get("turnId", active)
            if not turn_id or (active and turn_id != active):
                raise OutcomeUnknown("Codex did not return the expected turn receipt")
            with store.transaction(self.conn):
                self._ack(event_id, turn_id)
            receipt = self._receipt(self._get(event_id))
            if receipt["method"] == "turn/start" and not human and self.roots._binding(thread_id).get("task_id"):
                self.roots._goal_for_budget(thread_id, receipt["token_budget"])
            return receipt
