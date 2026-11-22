"""Admission from official Codex quota windows and renewal of evidenced, live work."""
from __future__ import annotations

import json
import time

from .. import config
from ..domain import clock, machines
from ..errors import HoldActive
from ..hostobs import host_policy
from ..hostobs.app_server_client import AppServerResponseError
from ..jobs import leases
from ..persistence import store
from . import waits
from .team_admission import SlotWatch


# Exact v0.169.0 ServerNotification names. Completed items, dynamic tool requests,
# raw receipts, token usage, account updates, and lifecycle notifications remain enabled.
OPT_OUT = ("item/agentMessage/delta", "item/plan/delta", "item/reasoning/textDelta",
           "item/reasoning/summaryTextDelta", "item/commandExecution/outputDelta", "item/fileChange/outputDelta")
PROGRESS = frozenset(("turn/started", "item/started", "item/completed", "thread/tokenUsage/updated",
                      "rawResponse/completed", "item/tool/call"))


def _provider(conn, binding):
    if binding.get("provider"):
        return binding["provider"]
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (binding["run_id"],)).fetchone()
    return host_policy.p0_provider_for_run(run)


def _policy():
    return config.limits().get("native_provider", {})


def _retry_seconds():
    return max(1, int(_policy().get("retry_seconds", 60)))


def _save(conn, provider, bucket, facts, source):
    conn.execute("INSERT INTO p0_provider_limits(provider,bucket,model,primary_json,secondary_json,"
                 "blocked_until,source,observed_at) VALUES(?,?,?,?,?,?,?,?)"
                 " ON CONFLICT(provider,bucket) DO UPDATE SET model=excluded.model,primary_json=excluded.primary_json,"
                 "secondary_json=excluded.secondary_json,blocked_until=excluded.blocked_until,"
                 "source=excluded.source,observed_at=excluded.observed_at",
                 (provider, bucket, facts.get("model"), facts.get("primary_json"), facts.get("secondary_json"),
                  facts.get("blocked_until"), source, clock.now_iso()))


def account_update(conn, provider, snapshot):
    """Merge independently supplied account windows. Only retain fields used for admission."""
    if not isinstance(snapshot, dict):
        return
    bucket = snapshot.get("limitId") or "codex"
    if not isinstance(bucket, str):
        return
    with store.transaction(conn):
        prior = conn.execute("SELECT * FROM p0_provider_limits WHERE provider=? AND bucket=?",
                             (provider, bucket)).fetchone()
        facts = dict(prior) if prior else {}
        if isinstance(snapshot.get("normalModelSlug"), str):
            facts["model"] = snapshot["normalModelSlug"]
        for name in ("primary", "secondary"):
            window = snapshot.get(name)
            if not isinstance(window, dict) or type(window.get("usedPercent")) not in (int, float):
                continue
            reset = window.get("resetsAt")
            if type(reset) not in (int, float):
                reset = None
                if window["usedPercent"] >= 100:
                    old = json.loads(facts.get(name + "_json") or "{}")
                    previous = old.get("resetsAt") or 0
                    reset = previous if old.get("usedPercent", 0) >= 100 and previous > clock.now().timestamp() else (
                        clock.now().timestamp() + _retry_seconds())
            facts[name + "_json"] = json.dumps({"usedPercent":window["usedPercent"], "resetsAt":reset})
        if snapshot.get("spendControlReached") is True or snapshot.get("rateLimitReachedType"):
            facts["blocked_until"] = clock.now().timestamp() + _retry_seconds()
        elif snapshot.get("spendControlReached") is False:
            facts["blocked_until"] = None
        _save(conn, provider, bucket, facts, "account/rateLimits/updated")


def rate_refusal(error):
    info = (error or {}).get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info in ("rateLimitExceeded", "usageLimitExceeded")
    return isinstance(info, dict) and any(isinstance(detail, dict) and detail.get("httpStatusCode") == 429
                                          for detail in info.values())


def terminal_backoff(conn, binding, turn):
    """The official terminal error scopes the provider and explains a product-less turn."""
    if turn.get("status") != "failed":
        return False
    provider = _provider(conn, binding)
    bucket = "turn:" + turn["id"]
    with store.transaction(conn):
        prior = conn.execute("SELECT 1 FROM p0_provider_limits WHERE provider=? AND bucket=?", (provider,bucket)).fetchone()
        if not prior and not rate_refusal(turn.get("error")):
            return False
        delivery = conn.execute(
            "SELECT 1 FROM p0_root_deliveries WHERE thread_id=? AND method='turn/start'"
            " AND ((state='acknowledged' AND turn_id=?) OR (state='sending' AND turn_id IS NULL))",
            (binding["thread_id"],turn["id"])).fetchone()
        if not delivery:
            return False
        # An official terminal may arrive while the original start RPC is awaiting
        # its ACK. A recovered receipt/replayed notification cannot slide the deadline.
        if not prior:
            _save(conn, provider, bucket, {"blocked_until":clock.now().timestamp() + _retry_seconds()}, "turn/completed")
            if binding.get("task_id"):
                waits.begin_delivery(conn, binding["thread_id"], "resume:" + turn["id"], "provider_backoff")
    return True


def rejected_start(conn, binding, event_id):
    """Persist an explicit provider refusal in the transaction releasing the unsent turn."""
    provider = _provider(conn, binding)
    retry_at = clock.now().timestamp() + _retry_seconds()
    _save(conn, provider, "start:" + binding["run_id"] + ":" + event_id, {"blocked_until":retry_at}, "turn/start")
    if binding.get("task_id"):
        waits.begin_delivery(conn, binding["thread_id"], event_id, "provider_backoff")
    return HoldActive("Codex refused the new turn until provider capacity is available", details={
        "reason":"provider_backoff", "provider":provider, "resets_at":retry_at, "event_id":event_id})


def retryable_failed_thread(conn, binding, recent_turns):
    """Codex keeps a thread in systemError after a failed turn, including an ordinary 429."""
    if (not binding or not recent_turns or recent_turns[0].get("status") != "failed"
            or any(t.get("status") == "inProgress" for t in recent_turns)):
        return False
    turn_id = recent_turns[0].get("id")
    return bool(conn.execute(
        "SELECT 1 FROM p0_root_deliveries d JOIN p0_root_turn_usage u USING(thread_id,turn_id)"
        " JOIN p0_provider_limits p ON p.provider=? AND p.bucket=('turn:'||d.turn_id)"
        " AND p.source='turn/completed' WHERE d.run_id=? AND d.thread_id=? AND d.turn_id=?"
        " AND d.method='turn/start' AND d.state='acknowledged' AND u.terminal_status='failed'"
        " AND NOT EXISTS(SELECT 1 FROM p0_root_deliveries outstanding WHERE outstanding.thread_id=d.thread_id"
        " AND (outstanding.state='sending' OR (outstanding.method='turn/start' AND outstanding.rowid>d.rowid)))",
        (_provider(conn, binding), binding["run_id"], binding["thread_id"], turn_id)).fetchone())


def _active_starts(conn, provider=None, *, receipts=None):
    selection, argument = ("r.provider=?", provider) if receipts is None else (
        "d.rowid IN (SELECT value FROM json_each(?))", json.dumps(receipts))
    return conn.execute(
        "SELECT d.rowid FROM p0_root_deliveries d JOIN p0_roots r USING(thread_id)"
        " LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) WHERE d.method='turn/start'"
        f" AND d.state IN ('sending','acknowledged') AND u.terminal_status IS NULL AND {selection}",
        (argument,)).fetchall()


def _count_held_starts(conn, receipts):
    return len(_active_starts(conn, receipts=receipts))


def require_provider_capacity(conn, binding, event_id):
    """Within the claim or paid-start transaction; replays, steers and interrupts bypass admission."""
    provider = _provider(conn, binding)
    now = clock.now().timestamp()
    rows = conn.execute("SELECT * FROM p0_provider_limits WHERE provider=? AND (model IS NULL OR model=?)"
                        " AND (source='account/rateLimits/updated' OR blocked_until>?)",
                        (provider, binding.get("model"), now)).fetchall()
    known = False
    for row in rows:
        known |= bool(row["primary_json"] or row["secondary_json"])
        resets = [row["blocked_until"] or 0]
        for field in ("primary_json", "secondary_json"):
            window = json.loads(row[field]) if row[field] else {}
            if window.get("usedPercent", 0) >= 100:
                resets.append(window["resetsAt"])
        if max(resets) > now:
            raise HoldActive("Codex provider capacity is awaiting its reset", details={
                "reason":"provider_backoff", "provider":provider, "resets_at":max(resets), "event_id":event_id})
    policy = _policy()
    cap = policy.get("parallel_turns", {}).get(provider)
    if cap is None and provider != "openai" and not known:
        row = conn.execute("SELECT agent_policy_json,inputs_json FROM runs WHERE run_id=?", (binding["run_id"],)).fetchone()
        pinned = store.loads(row, "agent_policy_json", {}) or {}
        # Pre-v2.1 runs retain their previously authorized concurrency. Explicit
        # provider feedback still applies immediately to every run on the workspace.
        if "p0_unknown_provider_parallel_turns" in pinned:
            spec = (store.loads(row, "inputs_json", {}).get("inputs") or {}).get("p0_spec") or {}
            cap = spec.get("max_parallel_roots", pinned["p0_unknown_provider_parallel_turns"])
    if cap is not None:
        # Sending without a receipt is still exposure. A terminal turn can free
        # provider capacity while its GX token adjustment is being reconciled.
        active = _active_starts(conn, provider)
        if len(active) >= cap:
            receipts = tuple(sorted(row[0] for row in active))
            raise SlotWatch(_count_held_starts, receipts, cap).hold("The provider's concurrent turn capacity is in use",
                {"reason":"provider_backoff", "provider":provider, "event_id":event_id, "active":len(active)})


def _due(conn, run_id, thread_id, turn_id, horizon):
    # Intent changes requiring new work revoke the lease in impact._release_attempt.
    # A pause additionally excludes late notifications that belong to its previous control state.
    row = conn.execute(
        "SELECT r.lease_id,r.task_id,l.expires_at,rn.status AS run_status FROM p0_roots r"
        " JOIN tasks t ON t.task_id=r.task_id AND t.run_id=r.run_id AND t.attempt=r.attempt"
        " AND t.lease_id=r.lease_id AND t.status IN ('CLAIMED','RUNNING')"
        " JOIN leases l ON l.lease_id=t.lease_id AND l.task_id=t.task_id AND l.attempt=t.attempt"
        " AND l.released_at IS NULL AND l.principal=r.thread_id JOIN runs rn ON rn.run_id=r.run_id"
        " JOIN p0_root_deliveries d ON d.thread_id=r.thread_id AND d.run_id=r.run_id"
        " LEFT JOIN p0_root_turn_usage u ON u.thread_id=d.thread_id AND u.turn_id=d.turn_id"
        " WHERE r.run_id=? AND r.thread_id=? AND r.wait_job_id IS NULL AND d.method='turn/start'"
        " AND d.state='acknowledged' AND d.turn_id=? AND u.terminal_status IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM p0_attempt_waits w WHERE w.root_key=r.root_key"
        " AND w.turn_id=d.turn_id AND (w.ended_at IS NULL OR w.reason='operator_pause'))",
        (run_id, thread_id, turn_id)).fetchone()
    if row is None or row["run_status"] == "PAUSED" or machines.is_terminal("run", row["run_status"]):
        return None
    remaining = (clock.parse(row["expires_at"]) - clock.now()).total_seconds()
    return row if 0 < remaining <= horizon else None


class Heartbeats:
    """One timer per Roots client. Quiet turns are checked against fresh official history outside callbacks."""
    def __init__(self, roots):
        self.roots = roots
        self.next_probe = 0.0
        self.check_after = {}

    def _horizon(self):
        return config.limits()["task_budget"]["lease_seconds"] / 2

    def observed(self, thread_id, turn_id):
        if not turn_id or self.roots.active_turns.get(thread_id) != turn_id:
            return
        at = clock.now().timestamp()
        if self.check_after.get(thread_id, (None, 0))[0] == turn_id and self.check_after[thread_id][1] > at:
            return
        conn, run_id = self.roots.conn, self.roots.run_id
        horizon = self._horizon()
        self.check_after[thread_id] = (turn_id, at + min(5, horizon / 4))
        if _due(conn, run_id, thread_id, turn_id, horizon) is None:
            return
        with store.transaction(conn):
            row = _due(conn, run_id, thread_id, turn_id, horizon)
            if row:
                leases.renew(conn, row, actor="codex:" + thread_id)

    def poll(self):
        if not self.roots.active_turns or time.monotonic() < self.next_probe:
            return
        horizon = self._horizon()
        self.next_probe = time.monotonic() + min(30, max(0.1, horizon / 3))
        for thread, turn in list(self.roots.active_turns.items()):
            if _due(self.roots.conn, self.roots.run_id, thread, turn, horizon) is None:
                continue
            try:
                page = self.roots._request("thread/turns/list", {"threadId":thread, "sortDirection":"desc",
                                           "itemsView":"notLoaded", "limit":1})
            except AppServerResponseError:
                continue
            if any(item.get("id") == turn and item.get("status") == "inProgress" for item in page.get("data", [])):
                self.observed(thread, turn)
