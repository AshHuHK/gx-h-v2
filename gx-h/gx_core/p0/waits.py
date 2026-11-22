"""Retain a Root's attempt over GX-owned waits and hand it back under a bounded lease."""
from ..domain import clock, machines
from ..errors import HoldActive
from ..persistence import store


# Every operation resolves the binding from the current task. A historical Root remains
# readable when an Intent change revokes it, even if its original Job or meter arrives later.
_CURRENT = (
    " FROM p0_roots r JOIN tasks t ON t.run_id=r.run_id AND t.task_id=r.task_id"
    " AND t.attempt=r.attempt AND t.lease_id=r.lease_id AND t.status IN ('CLAIMED','RUNNING')"
    " JOIN leases l ON l.lease_id=t.lease_id AND l.task_id=t.task_id AND l.attempt=t.attempt"
    " AND l.released_at IS NULL AND l.principal IS r.thread_id"
    " JOIN runs rn ON rn.run_id=t.run_id "
)
_FIELDS = "r.*,l.issued_at,l.expires_at,rn.status AS run_status"
_JOB_OWNER = (
    "j.run_id=r.run_id AND j.task_id=r.task_id AND j.lease_ref=r.lease_id"
    " AND COALESCE((SELECT json_extract(e.payload_json,'$.lease_ref') FROM events e"
    " WHERE e.subject_kind='job' AND e.subject_id=j.job_id AND e.kind='job.adopted'"
    " ORDER BY e.event_id DESC LIMIT 1),j.lease_ref)=r.lease_id"
)


def _installed(conn, table="p0_attempt_waits"):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def _current(conn, value, *, by="r.root_key"):
    row = conn.execute("SELECT " + _FIELDS + _CURRENT + " WHERE " + by + "=?", (value,)).fetchone()
    return row if row and not machines.is_terminal("run", row["run_status"]) else None


def job_waiter(conn, run_id, task_id, job_id):
    """Current owner for a Job handoff or a receipt replay. Callers match the parked Job ID."""
    if not _installed(conn, "p0_roots"):
        return None
    row = conn.execute("SELECT " + _FIELDS + ",j.status AS job_status" + _CURRENT +
                       " JOIN jobs j ON " + _JOB_OWNER + " WHERE r.run_id=? AND r.task_id=? AND j.job_id=?",
                       (run_id, task_id, job_id)).fetchone()
    return row if row and not machines.is_terminal("run", row["run_status"]) else None


def _waiting(conn, by, value):
    installed = _installed(conn)
    if not installed and not _installed(conn, "p0_roots"):
        return []
    # wait_job_id is the original committed park, including on older stores. Budget,
    # metering and operator waits can overlap it or each other and end independently.
    saved = (" OR EXISTS (SELECT 1 FROM p0_attempt_waits w WHERE w.root_key=r.root_key"
             " AND w.ended_at IS NULL AND w.reason<>'job')") if installed else ""
    return [row for row in conn.execute(
        "SELECT r.task_id,r.lease_id,j.job_id,rn.status AS run_status" + _CURRENT +
        " LEFT JOIN jobs j ON j.job_id=r.wait_job_id AND " + _JOB_OWNER +
        " WHERE " + by + "=? AND (j.job_id IS NOT NULL" + saved +
        " OR (rn.status='PAUSED' AND (SELECT julianday(e.ts)>=julianday(l.issued_at)"
        " AND julianday(e.ts)<julianday(l.expires_at) FROM events e WHERE e.run_id=r.run_id"
        " AND e.kind='run.transition' AND e.new_state='PAUSED' ORDER BY e.event_id DESC LIMIT 1)))", (value,))
        if not machines.is_terminal("run", row["run_status"])]


def is_waiting(conn, root_key):
    return bool(_waiting(conn, "r.root_key", root_key))


def held_for_run(conn, run_id):
    """Attempts kept on the scheduler's board. This read grants no worker write rights."""
    return _waiting(conn, "r.run_id", run_id)


def pending(conn, run_id):
    installed = _installed(conn)
    if not installed and not _installed(conn, "p0_roots"):
        return []
    rows = [dict(row) for row in conn.execute(
        "SELECT r.root_key,r.task_id,r.attempt,r.lease_id,r.thread_id,w.reason,w.wait_key,"
        "w.started_at,w.turn_id,w.ready_at,w.delivery_started_at" + _CURRENT +
        " JOIN p0_attempt_waits w ON w.root_key=r.root_key WHERE r.run_id=? AND w.ended_at IS NULL"
        " ORDER BY w.wait_id", (run_id,))] if installed else []
    # A pre-upgrade park has the original Job reference but no recorded wait timestamps.
    missing = (" AND NOT EXISTS(SELECT 1 FROM p0_attempt_waits w WHERE w.root_key=r.root_key"
               " AND w.reason='job' AND w.wait_key=j.job_id AND w.ended_at IS NULL)") if installed else ""
    rows.extend(dict(row) for row in conn.execute(
        "SELECT r.root_key,r.task_id,r.attempt,r.lease_id,r.thread_id,'job' AS reason,j.job_id AS wait_key,"
        "NULL AS started_at,NULL AS turn_id,NULL AS ready_at,NULL AS delivery_started_at" + _CURRENT +
        " JOIN jobs j ON j.job_id=r.wait_job_id AND " + _JOB_OWNER + " WHERE r.run_id=?" + missing, (run_id,)))
    return rows


def _active_turn(conn, thread_id):
    row = conn.execute(
        "SELECT d.turn_id FROM p0_root_deliveries d LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id)"
        " WHERE d.thread_id=? AND d.method='turn/start' AND d.turn_id IS NOT NULL"
        " AND u.terminal_status IS NULL ORDER BY d.rowid DESC LIMIT 1", (thread_id,)).fetchone()
    return row[0] if row else None


def _open_wait(conn, root_key, reason, key):
    return conn.execute("SELECT * FROM p0_attempt_waits WHERE root_key=? AND reason=? AND wait_key=?"
                        " AND ended_at IS NULL", (root_key, reason, key)).fetchone()


def _insert(conn, root, reason, key, *, started_at=None, turn_id=None):
    conn.execute("INSERT OR IGNORE INTO p0_attempt_waits(root_key,reason,wait_key,started_at,turn_id)"
                 " VALUES(?,?,?,?,?)", (root["root_key"], reason, key, started_at or clock.now_iso(),
                                        turn_id or _active_turn(conn, root["thread_id"])))


def begin(conn, root_key, reason, key, *, started_at=None, turn_id=None):
    """Record the specific GX operation that prevented the current owner from working."""
    root = _current(conn, root_key)
    if not root or (started_at and clock.parse(root["issued_at"]) > clock.parse(started_at)):
        return
    if reason == "job":
        if root["wait_job_id"] != key or not job_waiter(conn, root["run_id"], root["task_id"], key):
            return
    elif clock.parse(root["expires_at"]) <= clock.parse(started_at or clock.now_iso()):
        if not is_waiting(conn, root_key):
            return  # An unrelated late denial/pause cannot reclassify a prior silent lapse.
    _insert(conn, root, reason, key, started_at=started_at, turn_id=turn_id)


def _handoff(conn, root, wait, phase):
    """Grant one normal work interval at a committed wait transition. Reads never call this."""
    from ..teams.service import default_lease_seconds
    previous = root["expires_at"]
    deadline = max(previous, clock.iso_after(default_lease_seconds()), key=clock.parse)
    conn.execute("UPDATE leases SET expires_at=? WHERE lease_id=? AND released_at IS NULL",
                 (deadline, root["lease_id"]))
    facts = {"phase": phase, "reason": wait["reason"], "wait_key": wait["wait_key"],
             "wait_id": wait["wait_id"], "started_at": wait["started_at"], "turn_id": wait["turn_id"],
             "root_key": root["root_key"], "thread_id": root["thread_id"], "lease_id": root["lease_id"],
             "attempt": root["attempt"], "previous_expires_at": previous, "expires_at": deadline}
    if wait["reason"] == "job":
        facts["job_id"] = wait["wait_key"]
        facts["wake_event_id"] = f"wake:{root['lease_id']}:{wait['wait_key']}"
    store.append_event(conn, kind="task.wait_handoff", actor="backend", run_id=root["run_id"],
                       subject_kind="task", subject_id=root["task_id"], evidence_level="EXECUTION_OBSERVED",
                       reason=f"{wait['reason']} wait on the original task attempt: {phase}", payload=facts)


def finish(conn, root_key, reason, key):
    wait = _open_wait(conn, root_key, reason, key)
    if not wait and reason == "job":
        # Old GX could crash after acknowledging the wake but before clearing the
        # parked root. Its real receipt is sufficient to finish the same handoff.
        root = _current(conn, root_key)
        if root and root["wait_job_id"] == key:
            terminal_job(conn, {"run_id":root["run_id"], "task_id":root["task_id"], "job_id":key})
            wait = _open_wait(conn, root_key, reason, key)
    if not wait:
        return
    root = _current(conn, root_key)
    if not root or (reason == "job" and root["wait_job_id"] != key):
        return
    conn.execute("UPDATE p0_attempt_waits SET ended_at=? WHERE wait_id=?", (clock.now_iso(), wait["wait_id"]))
    if reason != "job" or clock.is_past(root["expires_at"]):
        _handoff(conn, root, wait, "wait_finished")


def terminal_job(conn, job):
    """With the terminal transition, arrange exactly one wake interval for the parked owner."""
    if not job["task_id"] or not _installed(conn):
        return
    root = job_waiter(conn, job["run_id"], job["task_id"], job["job_id"])
    if not root or root["wait_job_id"] != job["job_id"] or not machines.is_terminal("job", root["job_status"]):
        return
    _insert(conn, root, "job", job["job_id"])
    wait = _open_wait(conn, root["root_key"], "job", job["job_id"])
    if not wait["ready_at"]:
        conn.execute("UPDATE p0_attempt_waits SET ready_at=? WHERE wait_id=?", (clock.now_iso(), wait["wait_id"]))
        _handoff(conn, root, wait, "job_terminal")


def _worker(conn, thread_id):
    return _current(conn, thread_id, by="r.thread_id")


def begin_delivery(conn, thread_id, event_id, reason):
    if reason not in ("budget", "metering"):
        raise ValueError("delivery waits require a budget or metering decision")
    root = _worker(conn, thread_id)
    if root:
        begin(conn, root["root_key"], reason, event_id)
        # A new admission decision supersedes the older reason for this same
        # undelivered event. The task is still waiting, so no lease handoff is due.
        previous = "metering" if reason == "budget" else "budget"
        conn.execute("UPDATE p0_attempt_waits SET ended_at=? WHERE root_key=? AND wait_key=?"
                     " AND reason=? AND ended_at IS NULL AND EXISTS (SELECT 1 FROM p0_attempt_waits w"
                     " WHERE w.root_key=? AND w.wait_key=? AND w.reason=? AND w.ended_at IS NULL)",
                     (clock.now_iso(), root["root_key"], event_id, previous, root["root_key"], event_id, reason))


def prepare_delivery(conn, thread_id, event_id):
    """Inside paid admission's savepoint. A refusal rolls the entire handoff back."""
    root = _worker(conn, thread_id)
    if not root or root["run_status"] == "PAUSED":
        return
    key = root["root_key"]
    blocked = conn.execute("SELECT reason FROM p0_attempt_waits WHERE root_key=? AND wait_key=?"
                           " AND reason IN ('budget','metering') AND ended_at IS NULL", (key, event_id)).fetchall()
    for row in blocked:
        finish(conn, key, row["reason"], event_id)
    if not blocked and clock.is_past(root["expires_at"]):
        # A newly arriving event need not have tried to reserve before the outage.
        # Its predecessor's exact terminal reservation already explains the wait.
        pending_meter = conn.execute(
            "SELECT w.turn_id,w.wait_key FROM p0_attempt_waits w JOIN budget_reservations b"
            " ON b.reservation_ref=w.wait_key WHERE w.root_key=? AND w.reason='metering'"
            " AND w.ended_at IS NULL AND b.status='HELD' ORDER BY w.wait_id LIMIT 1", (key,)).fetchone()
        if pending_meter:
            raise HoldActive("the preceding terminal turn is still awaiting its GX charge", details={
                "reason": "native_usage_pending", "thread_id": thread_id, "turn_id": pending_meter["turn_id"],
                "reservation_ref": pending_meter["wait_key"], "event_id": event_id})
    job_id = root["wait_job_id"]
    if not job_id or event_id != f"wake:{root['lease_id']}:{job_id}":
        return
    owner = job_waiter(conn, root["run_id"], root["task_id"], job_id)
    if not owner or not machines.is_terminal("job", owner["job_status"]):
        return
    wait = _open_wait(conn, key, "job", job_id)
    if not wait or not wait["ready_at"]:
        # Preserve a terminal parked Job from before this migration (no prospective
        # handoff row). The stored owner and external result suffice for its first wake.
        terminal_job(conn, {"run_id": root["run_id"], "task_id": root["task_id"], "job_id": job_id})
        wait = _open_wait(conn, key, "job", job_id)
        owner = _current(conn, key)
    if not wait["delivery_started_at"]:
        if clock.is_past(owner["expires_at"]):
            _handoff(conn, owner, wait, "wake_after_downtime")
        conn.execute("UPDATE p0_attempt_waits SET delivery_started_at=? WHERE wait_id=?",
                     (clock.now_iso(), wait["wait_id"]))


def meter_pending(conn, thread_id, turn_id):
    root = _worker(conn, thread_id)
    if not root:
        return
    held = conn.execute(
        "SELECT d.reservation_ref FROM p0_root_deliveries d JOIN budget_reservations b USING(reservation_ref)"
        " JOIN p0_root_turn_usage u USING(thread_id,turn_id) WHERE d.run_id=? AND d.thread_id=? AND d.turn_id=?"
        " AND d.method='turn/start' AND d.state='acknowledged' AND u.terminal_status IS NOT NULL"
        " AND b.status='HELD'", (root["run_id"], thread_id, turn_id)).fetchone()
    if held:
        _insert(conn, root, "metering", held[0], turn_id=turn_id)


def meter_settled(conn, thread_id, turn_id):
    root = _worker(conn, thread_id)
    if not root:
        return
    for row in conn.execute(
            "SELECT d.reservation_ref FROM p0_root_deliveries d JOIN budget_reservations b USING(reservation_ref)"
            " WHERE d.run_id=? AND d.thread_id=? AND d.turn_id=? AND d.method='turn/start' AND b.status='SETTLED'",
            (root["run_id"], thread_id, turn_id)):
        finish(conn, root["root_key"], "metering", row[0])


def pause_run(conn, run_id, key, *, at=None):
    roots = conn.execute(
        "SELECT r.root_key,(SELECT d.event_id FROM p0_root_deliveries d WHERE d.thread_id=r.thread_id"
        " AND d.method='turn/start' AND d.state='sending' AND d.turn_id IS NULL LIMIT 1) AS sending"
        + _CURRENT + " WHERE r.run_id=? AND NOT EXISTS (SELECT 1 FROM p0_attempt_waits w"
        " WHERE w.root_key=r.root_key AND w.reason='operator_pause' AND w.ended_at IS NULL)",
        (run_id,)).fetchall()
    for root in roots:
        # The request can already be at Codex when the operator stops GX. Its committed
        # delivery key will identify the turn once the original acknowledgment is recovered.
        begin(conn, root["root_key"], "operator_pause", root["sending"] or key, started_at=at)


def resume_run(conn, run_id):
    for row in conn.execute(
            "SELECT r.root_key,w.wait_key" + _CURRENT + " JOIN p0_attempt_waits w ON w.root_key=r.root_key"
            " WHERE r.run_id=? AND w.reason='operator_pause' AND w.ended_at IS NULL", (run_id,)).fetchall():
        finish(conn, row["root_key"], "operator_pause", row["wait_key"])


def is_p0_run(row):
    inputs = store.loads(row, "inputs_json", {}) if "inputs_json" in row.keys() else {}
    parameters = inputs.get("inputs") if isinstance(inputs, dict) else None
    return isinstance(parameters, dict) and parameters.get("p0") == 1


def on_run_transition(conn, run, to_state, event_id):
    """All P0 pauses suspend held attempts, including stops raised inside a workflow node."""
    if not is_p0_run(run) or machines.is_terminal("run", to_state) or not _installed(conn):
        return
    run_id = run["run_id"]
    paused = conn.execute("SELECT event_id,ts FROM events WHERE run_id=? AND event_id<=?"
                          " AND kind='run.transition' AND new_state='PAUSED' ORDER BY event_id DESC LIMIT 1",
                          (run_id, event_id)).fetchone()
    if paused:
        # Recover the original pause edge when the project was stopped by an older
        # release. Only leases already owned at that instant can be carried forward.
        pause_run(conn, run_id, "pause:" + str(paused["event_id"]), at=paused["ts"])
    if to_state != "PAUSED":
        resume_run(conn, run_id)


def deferred_turn(conn, root_key, turn_id):
    """An observed park/stop for this very turn explains its ending without a worker product."""
    return conn.execute(
        "SELECT 1 FROM p0_attempt_waits w JOIN p0_roots r USING(root_key)"
        " LEFT JOIN p0_root_deliveries stopped ON w.reason='operator_pause' AND stopped.run_id=r.run_id"
        " AND stopped.thread_id=r.thread_id AND stopped.event_id=w.wait_key"
        " WHERE w.root_key=? AND ((w.reason='operator_pause' AND (w.turn_id=? OR stopped.turn_id=?))"
        " OR (w.reason='job' AND w.turn_id=?"
        " AND NOT EXISTS(SELECT 1 FROM p0_root_deliveries d WHERE d.run_id=r.run_id AND d.thread_id=r.thread_id"
        " AND d.turn_id=w.turn_id AND d.event_id=('wake:'||r.lease_id||':'||w.wait_key)"
        " AND d.state='acknowledged'))) LIMIT 1", (root_key, turn_id, turn_id, turn_id)).fetchone() is not None
