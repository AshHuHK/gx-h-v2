"""Codex turn delivery, durable receipts and GX budget settlement."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
import time
import uuid

from .. import budgets
from ..domain import clock, machines
from ..errors import (BudgetExceeded, HoldActive, IdempotencyConflict, LeaseExpired,
                      OutcomeUnknown, SchemaInvalid, ScopeDenied)
from ..hostobs.app_server_client import AppServerResponseError
from ..persistence import store
from . import activity, meter_limits, metering, response_attribution, team_admission, waits
from .profiles import CODEX_VERSION

ACTOR = "p0-root"
TERMINAL = frozenset({"completed", "failed", "interrupted"})


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _idle_refusal(error, method):
    return (error.message == f"no active turn to {method}"
            or (error.message.startswith("expected active turn id ") and " but found " in error.message))


def _token_count(value):
    return value if type(value) is int and value >= 0 else None


@dataclass
class _LiveReceipts:
    responses: dict = field(default_factory=dict)
    responses_at_snapshot: int = 0
    unpaired_snapshot: bool = False
    latest_total: int | None = None
    repeated_total: bool = False
    cancelled_submit_after: int | None = None

    def snapshot(self, total):
        # The pinned host can emit an unchanged count after receiving rate limits
        # for an interrupted request whose final provider usage never arrived.
        if self.responses_at_snapshot == len(self.responses):
            self.unpaired_snapshot = True
        self.responses_at_snapshot = len(self.responses)
        if self.latest_total is not None and total <= self.latest_total:
            self.repeated_total = True
        self.latest_total = total


def _queued_message(item):
    return (item.get("type") == "function_call" and item.get("namespace") == "collaboration"
            and item.get("name") == "send_message" and item.get("call_id"))


def _stored_child_session(thread, meter):
    # In 0.169.0 a stored, unloaded Thread summary uses its own id as the
    # sessionId. The session_meta of that exact official path retains the
    # original root session and spawn parent.
    if (thread.get("sessionId") != meter["child_id"] or (thread.get("status") or {}).get("type") != "notLoaded"
            or thread.get("cliVersion") != CODEX_VERSION or not thread.get("path")):
        return False
    try:
        with Path(thread["path"]).open("rb") as stream:
            header = json.loads(stream.readline())
        metadata = header["payload"]
        spawn = metadata["source"]["subagent"]["thread_spawn"]
        return (header["type"] == "session_meta" and metadata["cli_version"] == CODEX_VERSION
                and metadata["id"] == meter["child_id"] and metadata["session_id"] == meter["root_id"]
                and metadata["parent_thread_id"] == spawn["parent_thread_id"] == meter["parent_id"]
                and metadata.get("forked_from_id") == thread.get("forkedFromId"))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _cancelled_history_call(payload, thread_id, turn_id, call_id):
    item = payload.get("item") or {}
    return ((payload.get("thread_id"), payload.get("turn_id")) == (thread_id, turn_id)
            and item.get("type") == "DynamicToolCall" and item.get("tool") == "gx_submit_product"
            and item.get("namespace") == "gx" and item.get("id") == call_id
            and item.get("status") == "failed" and item.get("success") is False and item.get("content_items") == []
            and item.get("error") == "dynamic tool call was cancelled before receiving a response")


def _terminal_history_marker(entry_type, kind, payload, thread_id, turn_id, call_id):
    # An interrupted direct submit can end between the scoped TokenUsageRecord and
    # its later TokenCount notification. Only these host bookkeeping records may
    # sit between that final response and the exact abort when the mirror is absent.
    if kind == "thread_goal_updated":
        return payload.get("threadId") == thread_id and (payload.get("goal") or {}).get("threadId") == thread_id
    if kind == "item_completed":
        return _cancelled_history_call(payload, thread_id, turn_id, call_id)
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    return (entry_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user"
            and metadata.get("turn_id") == turn_id and metadata.get("content_item_kinds") == ["generic.turn_aborted"])


def _recorded_usage(path, thread_id, turn_id, session_id, owner_turn, start, *, terminal_call=None, prior=None):
    """Corroborate a cold 0.169.0 snapshot with its exact official response records.

    A completed response emits a TokenCount even if its provider omitted usage.
    In that case Codex repeats the last cumulative value without a new
    TokenUsageRecord. Never turn this intermediate snapshot into a final bill.
    """
    with path.open("rb") as stream:
        header = json.loads(stream.readline())
        if header.get("type") != "session_meta" or header["payload"].get("id") != thread_id:
            return None
        strict = terminal_call is not None
        if strict and (header["payload"].get("session_id") != session_id
                       or header["payload"].get("cli_version") != CODEX_VERSION or prior is None):
            return None
        if start:
            stream.seek(start)
        capture = False
        receipts = {}
        final_submit = False
        final_response_id = None
        unmirrored_submit = False
        cancelled = False
        reported_thread_total = None
        last_tokens = None
        cost = 0
        cumulative = None
        awaiting_count = False
        messages = set()
        for line in stream:
            entry = json.loads(line)
            payload = entry.get("payload") or {}
            entry_type = entry.get("type")
            kind = payload.get("type") if entry_type == "event_msg" else None
            if unmirrored_submit and entry_type != "token_usage_record" and kind not in ("turn_aborted", "token_count"):
                unmirrored_submit = final_response_id is not None and _terminal_history_marker(
                    entry_type, kind, payload, thread_id, turn_id, terminal_call)
            if kind in ("task_started", "turn_started"):
                if capture:
                    return None
                capture = payload.get("turn_id") == turn_id
                if capture and payload.get("root_turn_id") != owner_turn:
                    return None
            elif not capture:
                continue
            elif entry.get("type") == "response_item":
                if (strict and thread_id == session_id and payload.get("type") == "function_call"
                        and payload.get("namespace") == "gx" and payload.get("name") == "gx_submit_product"
                        and payload.get("call_id") == terminal_call):
                    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                    unmirrored_submit = not final_submit and metadata.get("turn_id") == turn_id
                    final_submit = True
                if call_id := _queued_message(payload):
                    messages.add(call_id)
            elif kind == "item_completed" and strict and thread_id == session_id:
                if final_submit and final_response_id and _cancelled_history_call(payload, thread_id, turn_id, terminal_call):
                    cancelled = True
            elif entry.get("type") == "token_usage_record":
                response_id = payload.get("response_id")
                tokens = _token_count((payload.get("usage") or {}).get("total_tokens"))
                if (awaiting_count or not response_id or response_id in receipts or tokens is None or
                        (payload.get("thread_id"), payload.get("turn_id"), payload.get("session_id"),
                         payload.get("root_turn_id")) != (thread_id, turn_id, session_id, owner_turn)):
                    return None
                if not isinstance(response_id, str) or (strict and final_response_id is not None):
                    return None
                receipts[response_id] = last_tokens = tokens
                if final_submit:
                    final_response_id = response_id
                cost += tokens
                if strict:
                    reported_thread_total = _token_count((payload.get("thread_token_usage") or {}).get("total_tokens"))
                    if reported_thread_total != prior + cost:
                        return None
                if (payload.get("turn_token_usage") or {}).get("total_tokens") != cost:
                    return None
                awaiting_count = True
            elif kind == "token_count":
                info = payload.get("info") or {}
                cumulative = _token_count((info.get("total_token_usage") or {}).get("total_tokens"))
                if not awaiting_count or cumulative is None:
                    return None
                if strict and (cumulative != reported_thread_total or
                        _token_count((info.get("last_token_usage") or {}).get("total_tokens")) != last_tokens):
                    return None
                awaiting_count = False
            elif kind in ("task_complete", "turn_complete", "turn_aborted"):
                # The cold root stopped while its committed direct call was awaiting a
                # reply. A completed child instead supplies its own terminal boundary.
                if strict and (payload.get("root_turn_id") != owner_turn or
                        (thread_id == session_id and (not cancelled or kind != "turn_aborted")) or
                        (thread_id != session_id and kind == "turn_aborted")):
                    return None
                if payload.get("turn_id") == turn_id and receipts:
                    if not awaiting_count and cumulative is not None:
                        return cost, cumulative, stream.tell(), messages, receipts
                    if unmirrored_submit and cancelled and kind == "turn_aborted" and payload.get("reason") == "interrupted":
                        return cost, reported_thread_total, stream.tell(), messages, receipts
                return None
    return None


class RootDelivery:
    def __init__(self, roots):
        self.roots = roots
        self.conn = roots.conn
        self.run_id = roots.run_id
        self.attribution = response_attribution.Attribution(self.conn, self.run_id)
        self._verify_after = {}
        self._live = {}
        self._paths = {}
        self._read_at = {}
        self._checked = {}
        self._queued_messages = set()
        self._cancelled_products = {}

    def _queued(self, sender, turn_id, owner_turn, call_id):
        self._queued_messages.add((sender, turn_id, call_id))
        self.conn.execute("UPDATE p0_native_child_meters SET awaiting_turn=0 WHERE run_id=? "
                          "AND parent_id=? AND owner_turn=? AND awaiting_call_id=?",
                          (self.run_id, sender, owner_turn, call_id))

    def _evidence(self, method, params, thread_id, turn_id):
        key = (thread_id, turn_id)
        if method == "turn/started":
            metering.observe_legacy_host_start(self.conn, self.run_id, thread_id, turn_id)
            self.attribution.start(thread_id, turn_id)
            self._live.setdefault(key, _LiveReceipts())
        elif method == "rawResponseItem/completed":
            self.attribution.item(thread_id, turn_id, params.get("item") or {})
            if call_id := _queued_message(params.get("item") or {}):
                owner = self.conn.execute("SELECT owner_turn FROM p0_native_child_usage WHERE child_id=? AND turn_id=?",
                                          key).fetchone()
                self._queued(thread_id, turn_id, owner["owner_turn"] if owner else turn_id, call_id)
        elif method == "rawResponse/completed":
            rid = params.get("responseId")
            tokens = _token_count((params.get("usage") or {}).get("totalTokens"))
            with store.transaction(
                self.conn, transaction_owner="Usage", operation="record_response", run_id=self.run_id,
            ):
                changed = metering.response(self.conn,thread_id,turn_id,rid,tokens)
                owner = self.conn.execute("SELECT root_id,metadata_verified FROM p0_native_child_meters WHERE child_id=?",
                                          (thread_id,)).fetchone()
                self.attribution.completed(owner["root_id"] if owner else thread_id, thread_id, turn_id, params)
                if changed and tokens is not None and metering.late_response(self.conn, thread_id, turn_id, child=bool(owner)):
                    if owner:
                        metering.recover_late_response_cost(self.conn, thread_id, turn_id, child=True)
                        scope = self.conn.execute("SELECT owner_turn FROM p0_native_child_usage WHERE child_id=? AND turn_id=?",
                                                  (thread_id, turn_id)).fetchone()
                        self._settle(owner["root_id"], scope[0])
                    else:
                        self._settle(thread_id, turn_id)
                if not owner or owner["metadata_verified"]:
                    meter_limits.check(self.conn,self.run_id,owner["root_id"] if owner else thread_id)
            if key not in self._live:
                return
            receipts = self._live[key].responses
            if not rid:
                receipts[rid] = None
            elif rid not in receipts or receipts[rid] is None:
                receipts[rid] = tokens
            elif tokens is not None and receipts[rid] != tokens:
                receipts[rid] = None

    def _live_end(self, thread_id, turn):
        self.attribution.end(thread_id, turn["id"])
        live = self._live.pop((thread_id, turn["id"]), None)
        if live is None:
            return None, 0
        if turn.get("status") != "completed" and live.unpaired_snapshot:
            return None, 1
        if live.responses and all(value is not None for value in live.responses.values()):
            return sum(live.responses.values()), 1
        return None, 1

    def attached(self, thread_id, thread):
        """Use the verified official resume response before consuming its queued snapshot."""
        if thread.get("id") != thread_id:
            raise OutcomeUnknown("Codex returned another thread for metering")
        if thread.get("path"):
            self._paths[thread_id] = Path(thread["path"])
            metering.remember_host_history(self.conn, self.run_id, thread_id, thread_id, thread)
        pending = self.conn.execute(
            "SELECT d.turn_id,d.state FROM p0_root_deliveries d LEFT JOIN budget_reservations r "
            "USING(reservation_ref) LEFT JOIN p0_meter_turns mt ON mt.reservation_ref=d.reservation_ref "
            "WHERE thread_id=? AND (r.status='HELD' OR d.state='sending' OR (r.status='RELEASED' AND EXISTS "
            "(SELECT 1 FROM p0_usage_releases x WHERE x.reservation_ref=r.reservation_ref AND x.confirmation_event_id IS NULL)) OR (mt.confidence='ESTIMATED' AND EXISTS "
            "(SELECT 1 FROM p0_root_terminals e WHERE e.thread_id=d.thread_id AND e.turn_id=d.turn_id)))",
            (thread_id,)).fetchall()
        targets = {row["turn_id"] for row in pending}
        with store.transaction(
            self.conn, transaction_owner="RootDelivery", operation="attach_thread", run_id=self.run_id,
        ):
            for turn in thread.get("turns", []):
                if turn["id"] in targets or None in targets:
                    self._history(thread_id, turn)

    def _terminal_call(self, root, turn):
        row = self.conn.execute("SELECT call_id FROM p0_root_terminals WHERE thread_id=? AND turn_id=?",
                                (root, turn)).fetchone()
        return row[0] if row else None

    def _recorded(self, thread_id, turn_id, session_id, owner_turn, prior):
        path = self._paths.get(thread_id)
        if not path or not metering.rollout_diagnostics(self.conn,self.run_id):
            return None
        terminal_call = self._terminal_call(session_id, owner_turn)
        if terminal_call and prior is None:
            return None
        key = thread_id, turn_id
        try:
            stat = path.stat()
            stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size, terminal_call, prior)
            checked = self._checked.get(key)
            if checked and checked[0] == stamp:
                result = checked[1]
            else:
                offset = self._read_at.get(thread_id, 0)
                result = _recorded_usage(path, thread_id, turn_id, session_id, owner_turn, offset,
                                         terminal_call=terminal_call, prior=prior)
                if result is None and offset:
                    result = _recorded_usage(path, thread_id, turn_id, session_id, owner_turn, 0,
                                             terminal_call=terminal_call, prior=prior)
                self._checked[key] = stamp, result
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Remote, compressed, incomplete, rotated or older histories may
            # not expose the required records. Their original hold stays open.
            return None
        if result and terminal_call:
            observed = self.conn.execute("SELECT turn_id,response_id,tokens,conflicting FROM p0_meter_responses "
                "WHERE thread_id=? AND (turn_id=? OR response_id IN (SELECT value FROM json_each(?)))",
                (thread_id, turn_id, json.dumps(list(result[4]))))
            for receipt in observed:
                if (receipt["turn_id"] != turn_id or receipt["conflicting"] or receipt["response_id"] not in result[4] or
                        (receipt["tokens"] is not None and result[4][receipt["response_id"]] != receipt["tokens"])):
                    return None
        if result and (prior is None or result[1] - prior == result[0]):
            for call_id in result[3]:
                self._queued(thread_id, turn_id, owner_turn, call_id)
            self._read_at[thread_id] = result[2]
            result = result[:2]
        else:
            result = None
        return result

    def _history(self, thread_id, turn):
        tid = turn["id"]
        if turn.get("itemsView") == "full":
            for item in turn.get("items", []):
                self.observe_descendant("item/completed", {"threadId": thread_id, "turnId": tid, "item": item})
                self._arrived(thread_id, tid, item)
            known = True
        else:
            known = False
        if turn.get("status") in TERMINAL:
            self._terminal(thread_id, turn, children_known=int(known))

    def _get(self, event_id):
        return self.conn.execute("SELECT * FROM p0_root_deliveries WHERE run_id=? AND event_id=?",
                                 (self.run_id, event_id)).fetchone()

    def active(self, thread_id):
        try:
            thread = self.roots._request("thread/read", {"threadId": thread_id})["thread"]
        except AppServerResponseError as exc:
            # A live, freshly created root can have a zero-byte rollout until it receives
            # its first input. GX knows whether any input was sent on this connection.
            known_empty = (exc.code == -32603 and exc.message.startswith("failed to read thread:")
                           and exc.message.endswith(" is empty") and ": rollout at " in exc.message
                           and thread_id in exc.message and thread_id in self.roots.loaded
                           and self.roots._binding(thread_id) and thread_id not in self.roots.active_turns)
            if known_empty and not self.conn.execute(
                    "SELECT 1 FROM p0_root_deliveries WHERE thread_id=? AND state<>'ready' UNION ALL "
                    "SELECT 1 FROM p0_root_turn_usage WHERE thread_id=? LIMIT 1", (thread_id,thread_id)).fetchone():
                return None
            raise
        if thread["id"] != thread_id:
            raise OutcomeUnknown("Codex returned a different thread")
        if thread.get("path"):
            self._paths[thread_id] = Path(thread["path"])
            metering.remember_host_history(self.conn, self.run_id, thread_id, thread_id, thread)
        status = thread["status"]["type"]
        if status == "idle":
            return None
        if status not in ("active", "systemError"):
            raise HoldActive(f"Codex thread is {status}, resume before delivery")
        page = self.roots._request("thread/turns/list", {"threadId": thread_id,
                                   "sortDirection": "desc", "itemsView": "notLoaded", "limit": 2})
        if status == "systemError":
            if activity.retryable_failed_thread(self.conn, self.roots._binding(thread_id), page["data"]):
                return None
            raise HoldActive("Codex thread is systemError, resume before delivery")
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
        task = self.conn.execute("SELECT t.lease_id,t.attempt,t.status,r.status AS run_status FROM tasks t "
                                 "JOIN runs r ON r.run_id=t.run_id WHERE t.task_id=? AND t.run_id=?",
                                 (task_id, self.run_id)).fetchone()
        lease = self.conn.execute("SELECT released_at FROM leases WHERE lease_id=? AND task_id=?",
                                  (binding["lease_id"], task_id)).fetchone()
        current = (task and lease and lease["released_at"] is None and task["lease_id"] == binding["lease_id"]
                   and task["attempt"] == binding["attempt"] and task["status"] in ("CLAIMED", "RUNNING"))
        if interrupt:
            if current:
                raise ScopeDenied("commit the GX attempt fence before interrupting Codex")
        elif current:
            lease = store.validate_lease(self.conn, binding["lease_id"], task_id=task_id)
            if (lease["principal"] != thread_id or lease["attempt"] != binding["attempt"] or
                    task["run_status"] in ("PAUSED", *machines.terminal_states("run"))):
                raise ScopeDenied("GX no longer permits this root to execute its task")
        else:
            raise LeaseExpired("the root no longer holds the task attempt")
        return binding

    def _reserve(self, thread_id, event_id, binding):
        # A retry gets a new thread as soon as its predecessor submits. That
        # predecessor can still be spending inside the already admitted turn.
        pending = self.conn.execute(
            "SELECT d.thread_id,d.event_id,d.turn_id,u.metering_reason FROM p0_root_deliveries d "
            "JOIN budget_reservations r USING(reservation_ref) LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) "
            "WHERE d.run_id=? AND d.method='turn/start' AND r.status='HELD' AND d.thread_id IN "
            "(SELECT ? UNION ALL SELECT thread_id FROM p0_roots WHERE run_id=? AND task_id=? AND attempt<?) "
            "ORDER BY r.rowid LIMIT 1",
            (self.run_id, thread_id, self.run_id, binding.get("task_id"), binding.get("attempt"))).fetchone()
        if pending:
            raise HoldActive("waiting for the preceding Codex turn's usage and descendants",
                             details={**dict(pending), "reason": "native_usage_pending"})
        previous = self.conn.execute("SELECT * FROM p0_meter_native_turns WHERE thread_id=? "
            "AND (budget_status='SETTLED' OR (budget_status='RELEASED' AND attribution='TURN')) "
            "ORDER BY sequence DESC LIMIT 1",(thread_id,)).fetchone()
        if previous and previous["estimated_at"] is None and (
                previous["terminal_status"] is None or previous["total_tokens"] is None):
            raise HoldActive("the preceding charged turn lacks its cumulative baseline", details={
                "reason": "native_usage_pending", "thread_id": thread_id, "event_id": previous["event_id"],
                "turn_id": previous["turn_id"]})
        prior = previous["total_tokens"] if previous else 0
        if prior is None and previous["estimated_at"]:
            observed = self.conn.execute("SELECT observed_total FROM p0_meter_threads WHERE thread_id=?",
                                         (thread_id,)).fetchone()
            prior = (observed[0] if observed else None) or 0  # A provisional baseline; never an actual charge.
        # A completed host-created Turn can lie between two paid inputs. Its scoped
        # terminal high-water mark is the new baseline; its receipts stay unreserved.
        extra = metering.unreserved_root_turns(self.conn, thread_id)
        boundary = max((r["total_tokens"] for r in extra if r["terminal_status"] and r["total_tokens"] is not None),
                       default=None)
        if boundary is not None and (prior is None or boundary > prior):
            prior = boundary
        run_ref = budgets.budget_for(self.conn, run_id=self.run_id)
        ref = team_admission.team_funding(self.conn,self.run_id,binding.get("task_id"),run_ref)
        risk_available = meter_limits.allowance(self.conn,self.run_id,ref,binding)
        available = int(budgets.state(self.conn, ref)["available"].get("tokens", 0))
        if risk_available is not None:
            available = min(available,risk_available)
        ceiling = self.roots.token_budget
        if ceiling is not None and (type(ceiling) is not int or ceiling <= 0):
            raise SchemaInvalid("the per-turn token allocation must be a positive integer")
        # Let the budget owner report *all* limiting lines, including API calls
        # and project-wide limits, even when no run tokens are currently free.
        tokens = max(1, available if ceiling is None else min(ceiling, available))
        while True:
            try:
                hold = budgets.reserve(self.conn, budget_ref=ref, subject_kind="native_turn", subject_id=event_id,
                                       amounts={"tokens": tokens, "api_calls": 1}, actor=ACTOR)
                if risk_available is not None and risk_available < 1:
                    # A funded leaf can have local units while sibling work ties up
                    # all of the workspace's real headroom. The admission savepoint rolls back this hold.
                    raise HoldActive("the shared native allowance is reserved by in-flight work",
                                     details={"reason":"budget_reserved","available":risk_available,"key":"tokens"})
                self.conn.execute("INSERT INTO p0_meter_turns(reservation_ref) VALUES(?)",(hold["reservation_ref"],))
                return hold["reservation_ref"], tokens, prior
            except BudgetExceeded as exc:
                # Use only an explicit positive smaller token allowance returned by the
                # budget owner, including a tighter project phase or Intent item.
                limits = exc.details.get("shortfalls") or ([exc.details] if "key" in exc.details else [])
                smaller = [int(line["available"]) for line in limits if line.get("key") == "tokens"
                           and 0 < int(line.get("available", 0)) < tokens]
                if smaller:
                    tokens = min(smaller)
                    continue
                if not limits:
                    raise
                details = {**exc.details, "shortfalls": limits}
                # Native admission can accept a smaller positive token allowance.
                # One token and one GX call must be possible on every limiting line.
                exhausted = next((line for line in limits if line.get("reserved", 0) <= 0
                                  or line.get("limit") is None or line["limit"] - line.get("spent", 0) < 1), None)
                local_team = self.conn.execute("SELECT team_id FROM p0_teams WHERE run_id=? AND budget_ref=?",
                                               (self.run_id,ref)).fetchone()
                if local_team and all("project_ref" not in line for line in limits):
                    raise HoldActive("the team's native budget has no available grant",details={
                        **details,"reason":"budget_reserved","team_id":local_team[0]}) from exc
                if exhausted is None:
                    raise HoldActive("native turn capacity is reserved by work in progress",
                                     details={**details, "reason": "budget_reserved"}) from exc
                message = (exc.message if len(limits) == 1 else
                           f"no admissible native turn remains under the {exhausted['key']} limit")
                raise BudgetExceeded(message, details={**details, "reason": "budget_exhausted",
                                                       "key": exhausted["key"]}) from exc

    def _prepare(self, thread_id, text, event_id, human, interrupt, client_id, active):
        with store.transaction(
            self.conn, transaction_owner="RootDelivery", operation="prepare_delivery", run_id=self.run_id,
        ):
            try:
                return self._try_prepare(thread_id, text, event_id, human, interrupt, client_id, active)
            except (BudgetExceeded, HoldActive) as exc:
                reason = exc.details.get("reason")
                reasons = {"budget_reserved":"budget","budget_exhausted":"budget","soft_budget":"budget",
                           "native_usage_pending":"metering","provider_backoff":"provider_backoff",
                           "team_capacity":"team_capacity","team_paused":"team_paused"}
                if reason not in reasons:
                    raise
                waits.begin_delivery(self.conn, thread_id, event_id,reasons[reason])
                denied = exc
        raise denied

    def _try_prepare(self, thread_id, text, event_id, human, interrupt, client_id, active):
        with store.transaction(
            self.conn, transaction_owner="RootDelivery", operation="reserve_delivery", run_id=self.run_id,
        ):
            row = self._get(event_id)
            if row:
                if (row["thread_id"], row["text_sha256"], row["client_id"], row["human"], row["interrupt"]) != (
                        thread_id, _digest(text), client_id, human, interrupt):
                    raise IdempotencyConflict("this event ID names another Codex delivery")
                if row["state"] != "ready":
                    return row, False
            if not interrupt:
                waits.prepare_delivery(self.conn, thread_id, event_id)
            binding = self._binding(thread_id, interrupt=interrupt, human=human)
            if not interrupt and binding.get("wait_job_id") and event_id != (
                    f"wake:{binding['lease_id']}:{binding['wait_job_id']}"):
                raise HoldActive("the worker is waiting for its original Job",details={"reason":"job_wait"})
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
            if not interrupt:
                budgets.require_native_metering(self.conn, self.run_id)
            if method == "turn/start" or (method=="turn/steer" and event_id.startswith("wake:")):
                team_admission.require_capacity(self.conn,binding)
            ref, tokens, prior = self._reserve(thread_id, event_id, binding) if method == "turn/start" else (None, None, None)
            if method == "turn/start":
                activity.require_provider_capacity(self.conn,binding,event_id)
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
        metering.reconcile_turn(self.conn, thread_id, turn_id)

    def _terminal_cost(self, thread, turn):
        call = self._cancelled_products.get((thread, turn))
        if not call or self._terminal_call(thread, turn) != call:
            return None
        return metering.terminal_response_cost(self.conn, thread, turn, call)

    def needs_terminal_snapshot(self, thread, turn):
        row = self.conn.execute("SELECT prior_tokens,total_tokens FROM p0_meter_native_turns WHERE thread_id=? "
            "AND turn_id=? AND terminal_status='interrupted' AND (budget_status IN ('HELD','RELEASED') OR confidence='ESTIMATED')",
            (thread, turn)).fetchone()
        if not row or row['prior_tokens'] is None:
            return False
        exact = self._terminal_cost(thread, turn)
        return exact is not None and (row['total_tokens'] is None or row['total_tokens'] < row['prior_tokens'] + exact)

    def _terminal(self, thread_id, turn, *, measured=None, children_known=0):
        meter_limits.ended(self.conn,thread_id,turn["id"],turn["status"])
        # A lost terminal notification can be recovered on the original connection.
        # Pair the native cancellation of the pending submit with the last raw
        # completion and its cumulative snapshot. Earlier/intermediate counts alone
        # cannot attest an interrupted turn. Keep live coverage until the pair arrives.
        live = self._live.get((thread_id, turn["id"]))
        if (measured is None and turn["status"] == "interrupted" and live and live.responses
                and live.cancelled_submit_after == live.responses_at_snapshot == len(live.responses)
                and not live.unpaired_snapshot and all(v is not None for v in live.responses.values())):
            measured, known = self._live_end(thread_id, turn)
            children_known = max(children_known, known)
        recovered_total = None
        if measured is None:
            held = self.conn.execute(
                "SELECT prior_tokens,total_tokens FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=? "
                "AND (budget_status IN ('HELD','RELEASED') OR confidence='ESTIMATED')", (thread_id, turn["id"])).fetchone()
            if held and turn["status"] == "interrupted":
                public_cost = self._terminal_cost(thread_id, turn["id"])
                if (public_cost is not None and held["prior_tokens"] is not None
                        and held["total_tokens"] == held["prior_tokens"] + public_cost):
                    measured = public_cost
            if held and measured is None:
                proof = self._recorded(thread_id, turn["id"], thread_id, turn["id"], held["prior_tokens"])
                if proof and (held["total_tokens"] is None or held["total_tokens"] <= proof[1]):
                    measured = proof[0]
                    if self._terminal_call(thread_id, turn["id"]):
                        recovered_total = proof[1]
                        metering.observe(self.conn, self.run_id, thread_id, thread_id, turn["id"], recovered_total,
                                         source="official terminal response history")
        self.conn.execute(
            "INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,measured_tokens,children_known,total_tokens) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(thread_id,turn_id) DO UPDATE SET terminal_status=excluded.terminal_status,"
            "total_tokens=coalesce(excluded.total_tokens,total_tokens),"
            "measured_tokens=coalesce(excluded.measured_tokens,measured_tokens),"
            "children_known=max(children_known,excluded.children_known)",
            (thread_id, turn["id"], turn["status"], measured, children_known, recovered_total))
        self.conn.execute("UPDATE p0_root_deliveries SET state='acknowledged',turn_id=target_turn "
                          "WHERE run_id=? AND thread_id=? AND method='turn/interrupt' AND target_turn=? "
                          "AND state='sending'", (self.run_id, thread_id, turn["id"]))
        self._settle(thread_id, turn["id"])
        metering.late_cumulative(self.conn,thread_id,turn["id"])
        meter_limits.check(self.conn,self.run_id,thread_id)

    def maintain(self):
        metering.maintain(self.conn,self.run_id)

    def flush_interrupts(self):
        """Deliver a committed hard-budget decision outside callbacks and SQLite writes."""
        for row in self.conn.execute("SELECT * FROM p0_meter_interrupts WHERE run_id=? "
                "AND sent_at IS NULL AND ended_at IS NULL ORDER BY requested_at", (self.run_id,)).fetchall():
            thread,tid = row["thread_id"],row["turn_id"]
            try:
                if self.roots.active_turns.get(thread) != tid and self.roots._child_turns.get(thread) != tid:
                    turns = self._pages("thread/turns/list", {"threadId":thread,"sortDirection":"desc",
                                                             "itemsView":"notLoaded","limit":16})
                    original = next((turn for turn in turns if turn["id"]==tid), None)
                    if not original:
                        continue
                    if original["status"] in TERMINAL:
                        with store.transaction(
                            self.conn, transaction_owner="RootDelivery", operation="reconcile_interrupt", run_id=self.run_id,
                        ):
                            meter_limits.ended(self.conn,thread,tid,original["status"])
                        continue
                    if original["status"] != "inProgress":
                        continue
                self.roots._request("turn/interrupt",{"threadId":thread,"turnId":tid})
            except AppServerResponseError as exc:
                if exc.code not in (-32600,-32602):
                    raise
                continue  # A racing completion is reconciled by its actual turn ID.
            with store.transaction(
                self.conn, transaction_owner="RootDelivery", operation="acknowledge_interrupt", run_id=self.run_id,
            ):
                self.conn.execute("UPDATE p0_meter_interrupts SET sent_at=coalesce(sent_at,?) "
                                  "WHERE thread_id=? AND turn_id=?", (clock.now_iso(),thread,tid))

    @staticmethod
    def _receipt(row):
        if row["human"] and row["method"] != "turn/start":
            raise ScopeDenied("the operator input was merged into an existing turn; use a distinct human turn")
        return {key: row[key] for key in ("event_id", "thread_id", "turn_id", "method", "state", "client_id",
                                           "reservation_ref", "token_budget")}

    def _arrived(self, thread_id, turn_id, item):
        # The official history omits the native error string but retains the precise
        # canceled item and its failed/empty outcome. Match the GX commit separately.
        if (item.get('type') == 'dynamicToolCall' and item.get('namespace') == 'gx'
                and item.get('tool') == 'gx_submit_product' and item.get('status') == 'failed'
                and item.get('success') is False and item.get('contentItems') == []):
            self._cancelled_products[thread_id, turn_id] = item.get('id')
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
        if method not in ("turn/started", "rawResponse/completed", "rawResponseItem/completed", "item/started",
                          "item/completed", "thread/tokenUsage/updated", "turn/completed"):
            return
        if method == "rawResponseItem/completed" and not response_attribution.is_tool_item(params.get("item") or {}):
            return
        thread_id = params.get("threadId")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if not thread_id or not turn_id or not self.roots._binding(thread_id):
            return
        if method in ("turn/started", "rawResponse/completed", "rawResponseItem/completed"):
            self._evidence(method, params, thread_id, turn_id)
            return
        with store.transaction(
            self.conn, transaction_owner="RootDelivery", operation="observe_root", run_id=self.run_id,
        ):
            if method in ("item/started", "item/completed"):
                item = params.get("item") or {}
                if (method == "item/completed" and item.get("type") == "dynamicToolCall"
                        and item.get("tool") == "gx_submit_product" and item.get("status") == "failed"
                        and item.get("success") is False and item.get("contentItems") == []
                        and (live := self._live.get((thread_id, turn_id)))):
                    live.cancelled_submit_after = len(live.responses)
                self._arrived(thread_id, turn_id, item)
            elif method == "thread/tokenUsage/updated":
                total = (params.get("tokenUsage") or {}).get("total", {}).get("totalTokens")
                earlier = self.conn.execute("SELECT total_tokens,terminal_status FROM p0_root_turn_usage "
                                            "WHERE thread_id=? AND turn_id=?",(thread_id,turn_id)).fetchone()
                metering.observe(self.conn,self.run_id,thread_id,thread_id,turn_id,total)
                if _token_count(total) is not None and (live := self._live.get((thread_id, turn_id))):
                    live.snapshot(total)
                if _token_count(total) is not None and not self.conn.execute(
                        "SELECT 1 FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=? "
                        "AND budget_status='SETTLED' AND (estimated_at IS NULL OR attribution='TURN')",
                        (thread_id,turn_id)).fetchone():
                    self.conn.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,total_tokens) VALUES(?,?,?) "
                                      "ON CONFLICT(thread_id,turn_id) DO UPDATE SET total_tokens="
                                      "max(coalesce(total_tokens,0),excluded.total_tokens)", (thread_id, turn_id, total))
                    if (earlier and earlier["terminal_status"] == "interrupted" and
                            ((live and live.cancelled_submit_after is not None) or (thread_id, turn_id) in self._cancelled_products)):
                        self._terminal(thread_id, {"id": turn_id, "status": "interrupted"})
                    else:
                        self._settle(thread_id, turn_id)
                    if (earlier and earlier["terminal_status"]=="completed" and earlier["total_tokens"] is not None
                            and total>earlier["total_tokens"]):
                        metering.settle_cumulative(self.conn,thread_id,turn_id)
                    metering.late_cumulative(self.conn,thread_id,turn_id)
                    meter_limits.check(self.conn,self.run_id,thread_id)
            elif method == "turn/completed" and params["turn"].get("status") in TERMINAL:
                live = self._live.get((thread_id,turn_id))
                aggregate = (params["turn"]["status"]=="completed" and live and not live.responses
                             and live.latest_total is not None and not live.repeated_total)
                measured, known = self._live_end(thread_id, params["turn"])
                self._terminal(thread_id, params["turn"], measured=measured, children_known=known)
                if aggregate:
                    metering.settle_cumulative(self.conn,thread_id,turn_id)

    def _child_start(self, child, turn):
        owner_turn = turn.get("rootTurnId")
        if not owner_turn:
            return None
        if not self.conn.execute(
                "SELECT 1 FROM p0_root_deliveries WHERE thread_id=? AND turn_id=? AND method='turn/start' "
                "UNION ALL SELECT 1 FROM p0_native_child_meters WHERE child_id=? AND owner_turn=?",
                (child["root_id"], owner_turn, child["child_id"], owner_turn)).fetchone():
            return None
        current = self.conn.execute("SELECT * FROM p0_native_child_usage WHERE child_id=? AND turn_id=?",
                                    (child["child_id"], turn["id"])).fetchone()
        if not current:
            previous = self.conn.execute("SELECT terminal,total_tokens FROM p0_native_child_usage "
                                         "WHERE child_id=? ORDER BY rowid DESC LIMIT 1",
                                         (child["child_id"],)).fetchone()
            prior = (previous["total_tokens"] if previous["terminal"] else None) if previous else 0
            self.conn.execute("INSERT INTO p0_native_child_usage(child_id,turn_id,owner_turn,prior_tokens) "
                              "VALUES(?,?,?,?)", (child["child_id"], turn["id"], owner_turn, prior))
            self.conn.execute("UPDATE p0_native_child_meters SET awaiting_turn=0 WHERE child_id=? AND owner_turn=?",
                              (child["child_id"], owner_turn))
        elif current["owner_turn"] != owner_turn:
            return None
        return owner_turn

    def observe_descendant(self, method, params):
        """The parent activity identifies the child. The child's causal root turn identifies its charge."""
        sender = params.get("threadId")
        turn = params.get("turn") or {}
        tid = params.get("turnId") or turn.get("id")
        if not sender or not tid:
            return
        activity = method in ("item/started", "item/completed")
        item = params.get("item") or {}
        if method == "item/completed" and item.get("type") in ("collabAgentToolCall", "dynamicToolCall", "mcpToolCall"):
            if isinstance(item.get("id"), str) and isinstance(item.get("status"), str):
                self.attribution.model_outcome(sender, tid, item["id"], item["status"])
        if activity:
            if item.get("type") != "subAgentActivity" or item.get("kind") not in ("started", "interacted"):
                return
        elif method not in ("turn/started", "turn/completed", "thread/tokenUsage/updated",
                            "rawResponse/completed", "rawResponseItem/completed"):
            return
        child = self.conn.execute("SELECT * FROM p0_native_child_meters WHERE child_id=? AND run_id=?",
                                  (sender, self.run_id)).fetchone()
        if activity:
            target = item.get("agentThreadId")
            if not isinstance(target, str) or not target or target == sender:
                return
            if self.roots._binding(sender):
                root_id, owner_turn = sender, tid
            elif child:
                parent = self.conn.execute("SELECT owner_turn FROM p0_native_child_usage WHERE child_id=? AND turn_id=?",
                                           (sender, tid)).fetchone()
                if not parent:
                    return
                root_id, owner_turn = child["root_id"], parent["owner_turn"]
            else:
                return
            if item["kind"] == "started":
                self.conn.execute("INSERT OR IGNORE INTO p0_native_child_meters "
                                  "(child_id,run_id,root_id,parent_id,owner_turn) VALUES(?,?,?,?,?)",
                                  (target, self.run_id, root_id, sender, owner_turn))
            elif (sender, tid, item.get("id")) not in self._queued_messages:
                # The same child can be awakened in a later paid turn. Do not
                # clear an earlier outstanding spawn, or relabel old usage.
                self.conn.execute("UPDATE p0_native_child_meters SET awaiting_turn=NOT EXISTS "
                                  "(SELECT 1 FROM p0_native_child_usage WHERE child_id=? AND owner_turn=? AND terminal=0),"
                                  "owner_turn=?,awaiting_call_id=? WHERE child_id=? AND root_id=? AND parent_id=? "
                                  "AND awaiting_turn=0 AND awaiting_call_id IS NOT ?",
                                  (target, owner_turn, owner_turn, item.get("id"), target, root_id, sender, item.get("id")))
            return
        if not child:
            return
        with store.transaction(
            self.conn, transaction_owner="RootDelivery", operation="observe_descendant", run_id=self.run_id,
        ):
            if method == "turn/started":
                if self._child_start(child, turn):
                    self._evidence(method, params, sender, tid)
                return
            if method in ("rawResponse/completed", "rawResponseItem/completed"):
                self._evidence(method, params, sender, tid)
                return
            meter = self.conn.execute("SELECT * FROM p0_native_child_usage WHERE child_id=? AND turn_id=?",
                                      (sender, tid)).fetchone()
            if not meter:
                return
            if method == "thread/tokenUsage/updated":
                total = _token_count((params.get("tokenUsage") or {}).get("total", {}).get("totalTokens"))
                if total is not None:
                    if live := self._live.get((sender, tid)):
                        live.snapshot(total)
                    if child["metadata_verified"]:
                        metering.observe(self.conn,self.run_id,child["root_id"],sender,tid,total)
                    self.conn.execute("UPDATE p0_native_child_usage SET total_tokens=max(coalesce(total_tokens,0),?) "
                                      "WHERE child_id=? AND turn_id=?", (total, sender, tid))
                    metering.recover_late_response_cost(self.conn, sender, tid, child=True)
            elif turn.get("status") in TERMINAL:
                measured, known = self._live_end(sender, turn)
                meter_limits.ended(self.conn,sender,tid,turn["status"])
                self.conn.execute("UPDATE p0_native_child_usage SET terminal=1,"
                                  "measured_tokens=coalesce(?,measured_tokens),children_known=max(children_known,?) "
                                  "WHERE child_id=? AND turn_id=?", (measured, known, sender, tid))
            self._settle(child["root_id"], meter["owner_turn"])
            metering.late_cumulative(self.conn,child["root_id"],meter["owner_turn"])
            meter_limits.check(self.conn,self.run_id,child["root_id"])

    def verify_descendants(self, requested=()):
        """Verify accounting-only ancestry and recover terminal children without starting them."""
        meters = self.conn.execute(
            "SELECT m.* FROM p0_native_child_meters m WHERE run_id=? AND "
            "(metadata_verified=0 OR EXISTS (SELECT 1 FROM p0_root_deliveries d "
            "JOIN budget_reservations r USING(reservation_ref) LEFT JOIN p0_meter_turns mt USING(reservation_ref) "
            "WHERE d.thread_id=m.root_id AND (r.status='HELD' OR mt.confidence='ESTIMATED') "
            "AND (d.turn_id=m.owner_turn OR EXISTS (SELECT 1 FROM p0_native_child_usage u WHERE "
            "u.child_id=m.child_id AND u.owner_turn=d.turn_id)))) ORDER BY m.rowid", (self.run_id,)).fetchall()
        for meter in meters:
            child_id = meter["child_id"]
            now = time.monotonic()
            if child_id not in requested and self._verify_after.get(child_id, 0) > now:
                continue
            self._verify_after[child_id] = now + 1
            thread = requested.get(child_id) if isinstance(requested, dict) else None
            if not thread:
                try:
                    thread = self.roots._request("thread/read", {"threadId": child_id, "includeTurns": True})["thread"]
                except AppServerResponseError:
                    continue
            source = thread.get("source")
            parent = source.get("subAgent") if isinstance(source, dict) else None
            facts = parent.get("thread_spawn") if isinstance(parent, dict) else None
            expected = meter["parent_id"]
            if (thread.get("id") != child_id or thread.get("parentThreadId") != expected
                    or thread.get("forkedFromId") not in (None, expected)
                    or not isinstance(facts, dict) or facts.get("parent_thread_id") != expected):
                continue
            if thread.get("sessionId") != meter["root_id"]:
                # 0.169 reports the unloaded child's own ID as sessionId. Its
                # completed Turn still names the original GX-funded root Turn.
                if (thread.get("sessionId") != child_id or (thread.get("status") or {}).get("type") != "notLoaded"
                        or thread.get("cliVersion") != CODEX_VERSION or not self.conn.execute(
                            "SELECT 1 FROM p0_root_deliveries WHERE run_id=? AND thread_id=? AND turn_id=? "
                            "AND method='turn/start' AND state='acknowledged'",
                            (self.run_id, meter["root_id"], meter["owner_turn"])).fetchone()):
                    continue
                if not meter["metadata_verified"]:
                    closed_child = expected == meter["root_id"] and any(
                        turn.get("id") and turn.get("rootTurnId") == meter["owner_turn"]
                        and turn.get("status") in TERMINAL and type(turn.get("completedAt")) is int
                        for turn in thread.get("turns", []))
                    if not closed_child and not (metering.rollout_diagnostics(self.conn, self.run_id)
                                                 and _stored_child_session(thread, meter)):
                        continue
            with store.transaction(
                self.conn, transaction_owner="RootDelivery", operation="verify_descendant", run_id=self.run_id,
            ):
                self.conn.execute("UPDATE p0_native_child_meters SET metadata_verified=1 WHERE child_id=?", (child_id,))
                if thread.get("path"):
                    self._paths[child_id] = Path(thread["path"])
                    metering.remember_host_history(self.conn, self.run_id, meter["root_id"], child_id, thread)
                for turn in thread.get("turns", []):
                    owner = self._child_start(meter, turn)
                    if not owner or turn.get("status") not in TERMINAL:
                        continue
                    known = turn.get("itemsView") == "full"
                    if known:
                        for item in turn.get("items", []):
                            self.observe_descendant("item/completed", {"threadId": child_id,
                                                    "turnId": turn["id"], "item": item})
                    row = self.conn.execute("SELECT * FROM p0_native_child_usage WHERE child_id=? AND turn_id=?",
                                            (child_id, turn["id"])).fetchone()
                    proof = self._recorded(child_id, turn["id"], meter["root_id"], owner, row["prior_tokens"])
                    # Codex 0.169.0 can project a waking turn as interrupted before
                    # its live status arrives. That projection has no completion time.
                    if (turn["status"] == "interrupted" and turn.get("completedAt") is None
                            and not row["terminal"] and proof is None):
                        continue
                    measured, cumulative = proof or (None, None)
                    total = cumulative if cumulative is not None else row["total_tokens"]
                    metering.observe(self.conn,self.run_id,meter["root_id"],child_id,turn["id"],total,
                                     source="official child history")
                    meter_limits.ended(self.conn,child_id,turn["id"],turn["status"])
                    self.conn.execute("UPDATE p0_native_child_usage SET terminal=1,"
                                      "measured_tokens=coalesce(?,measured_tokens),total_tokens=CASE WHEN ? IS NULL "
                                      "THEN total_tokens ELSE max(coalesce(total_tokens,0),?) END,"
                                      "children_known=max(children_known,?) WHERE child_id=? AND turn_id=?",
                                      (measured, cumulative, cumulative, int(known), child_id, turn["id"]))
                for owner in self.conn.execute("SELECT DISTINCT owner_turn FROM p0_native_child_usage WHERE child_id=?",
                                               (child_id,)):
                    self._settle(meter["root_id"], owner["owner_turn"])
                    metering.late_cumulative(self.conn,meter["root_id"],owner["owner_turn"])

    def _pages(self, method, params):
        seen = set()
        while True:
            page = self.roots._request(method, params)
            yield from page["data"]
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise OutcomeUnknown("Codex returned an inconsistent history cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    def reconcile(self, thread_id):
        """Recover exact input receipts and terminal turns from the official history."""
        rows = self.conn.execute(
            "SELECT d.* FROM p0_root_deliveries d LEFT JOIN budget_reservations r ON r.reservation_ref=d.reservation_ref "
            "LEFT JOIN p0_meter_turns mt ON mt.reservation_ref=d.reservation_ref WHERE d.run_id=? AND d.thread_id=? "
            "AND (d.state='sending' OR r.status='HELD' OR (mt.confidence='ESTIMATED' AND EXISTS "
            "(SELECT 1 FROM p0_root_terminals e WHERE e.thread_id=d.thread_id AND e.turn_id=d.turn_id)))",
            (self.run_id, thread_id)).fetchall()
        if not rows:
            return
        pending = {row["event_id"]: row["target_turn"] for row in rows if row["state"] == "sending"
                   and not row["interrupt"]}
        targets = {row["turn_id"] or row["target_turn"] for row in rows} - {None}
        turns = self._pages("thread/turns/list", {"threadId": thread_id, "sortDirection": "desc",
                                                 "itemsView": "notLoaded", "limit": 64})
        try:
            for turn in turns:
                tid = turn["id"]
                terminal = turn["status"] in TERMINAL
                inspect_items = tid in pending.values() or None in pending.values()
                if terminal and tid in targets:
                    inventory = self.conn.execute("SELECT children_known FROM p0_root_turn_usage "
                                                  "WHERE thread_id=? AND turn_id=?", (thread_id, tid)).fetchone()
                    inspect_items |= not inventory or not inventory["children_known"]
                if inspect_items:
                    items = self._pages("thread/items/list", {"threadId": thread_id, "turnId": tid,
                                        "sortDirection": "asc", "limit": 64,
                                        "deferOutput": {"commandExecution": True, "mcpToolCall": True}})
                    for entry in items:
                        if entry.get("turnId") != tid:
                            raise OutcomeUnknown("Codex returned an item from a different turn")
                        item = entry["item"]
                        with store.transaction(
                            self.conn, transaction_owner="RootDelivery", operation="reconcile_item", run_id=self.run_id,
                        ):
                            self.observe_descendant("item/completed", {"threadId": thread_id, "turnId": tid, "item": item})
                            self._arrived(thread_id, tid, item)
                    pending = {eid: target for eid, target in pending.items() if self._get(eid)["state"] == "sending"}
                if terminal:
                    with store.transaction(
                        self.conn, transaction_owner="RootDelivery", operation="reconcile_turn", run_id=self.run_id,
                    ):
                        self._terminal(thread_id, turn, children_known=int(inspect_items))
                targets.discard(tid)
                if not pending and not targets:
                    break
        except AppServerResponseError as exc:
            if "not materialized yet" not in exc.message or "before first user message" not in exc.message:
                raise

    def _refused(self, event_id):
        with store.transaction(
            self.conn, transaction_owner="RootDelivery", operation="refuse_delivery", run_id=self.run_id,
        ):
            row = self._get(event_id)
            if row["state"] == "sending":
                if row["reservation_ref"]:
                    budgets.release(self.conn, row["reservation_ref"], actor=ACTOR, reason="Codex refused the input")
                self.conn.execute("UPDATE p0_root_deliveries SET state='ready',reservation_ref=NULL,prior_tokens=NULL,"
                                  "token_budget=NULL WHERE run_id=? AND event_id=?", (self.run_id, event_id))

    def deliver(self, thread_id, text, *, event_id, interrupt=False, human=False, message_id=None):
        if self.conn.in_transaction:
            raise RuntimeError("commit the GX change before Codex delivery")
        if not isinstance(text, str) or not isinstance(event_id, str) or not event_id:
            raise SchemaInvalid("a Codex delivery requires a text body and stable event ID")
        if not interrupt:
            self.maintain()  # Commit due settlements independently of the next attempted grant.
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
                if method == "turn/start" and activity.rate_refusal(exc.data):
                    with store.transaction(
                        self.conn, transaction_owner="RootDelivery", operation="record_rate_refusal", run_id=self.run_id,
                    ):
                        latest = self._get(event_id)
                        if latest["state"] == "acknowledged":
                            return self._receipt(latest)
                        self._refused(event_id)
                        denied = activity.rejected_start(self.conn, self.roots._binding(thread_id), event_id)
                    raise denied from exc
                if method == "turn/steer" and _idle_refusal(exc, "steer"):
                    # The protocol explicitly refused this input. The next iteration samples the live turn again.
                    self._refused(event_id)
                    if self.active(thread_id) == active:
                        raise HoldActive("Codex has not yet published the new active-turn state") from exc
                    continue
                if method == "turn/interrupt" and _idle_refusal(exc, "interrupt") and self.active(thread_id) != active:
                    response = {}
                else:
                    if exc.code in (-32600, -32601, -32602):
                        self._refused(event_id)
                    raise
            turn_id = (response.get("turn") or {}).get("id") if method == "turn/start" else response.get("turnId", active)
            if not turn_id or (active and turn_id != active):
                raise OutcomeUnknown("Codex did not return the expected turn receipt")
            with store.transaction(
                self.conn, transaction_owner="RootDelivery", operation="acknowledge_delivery", run_id=self.run_id,
            ):
                self._ack(event_id, turn_id)
            receipt = self._receipt(self._get(event_id))
            if receipt["method"] == "turn/start" and not human and self.roots._binding(thread_id).get("task_id"):
                self.roots._goal_for_budget(thread_id, receipt["token_budget"])
            return receipt
