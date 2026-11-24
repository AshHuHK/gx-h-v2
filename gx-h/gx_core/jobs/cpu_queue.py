"""CPU queue positions belong to a parked or resuming Workflow node."""

from ..domain import clock, machines
from ..errors import ResourceDenied


def exists(conn):
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_cpu_waits'").fetchone()
        is not None
    )


def park(conn, run_id, node, key, needed, since, event):
    # Historical stores acquire this optional Job index on their first contention.
    # Both the index and the checkpoint are written in Engine's park transaction.
    conn.execute("""CREATE TABLE IF NOT EXISTS job_cpu_waits (
        sequence INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        node TEXT NOT NULL, job_key TEXT NOT NULL, required_threads INTEGER NOT NULL,
        since TEXT NOT NULL, UNIQUE(run_id,node))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_cpu_wait_key ON job_cpu_waits(job_key)")
    conn.execute(
        """INSERT INTO job_cpu_waits(sequence,run_id,node,job_key,required_threads,since)
        VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,node) DO UPDATE SET
        sequence=excluded.sequence,job_key=excluded.job_key,required_threads=excluded.required_threads,since=excluded.since
        WHERE job_cpu_waits.job_key<>excluded.job_key""",
        (event, run_id, node, key, needed, since),
    )


def positions(conn, allowed, *, head_only=False):
    if not exists(conn):
        return []
    states = machines.machine("run")["executable"]
    marks = ",".join("?" for _ in states)
    rows = conn.execute(
        f"""SELECT w.* FROM job_cpu_waits w JOIN runs r ON r.run_id=w.run_id
        WHERE r.status IN ({marks}) AND w.required_threads<=?
        ORDER BY w.sequence {'LIMIT 1' if head_only else ''}""",
        (*states, allowed),
    )
    return [dict(row) for row in rows]


def retire_obsolete(conn, run_id, state):
    """The checkpoint's writer removes positions a rewind, replan, or terminal transition abandoned."""
    if not exists(conn):
        return
    pending = conn.execute("SELECT node,job_key FROM job_cpu_waits WHERE run_id=?", (run_id,)).fetchall()
    for row in pending:
        latest = (
            state.get("node_waits", {}).get(row["node"])
            or state.get("resumptions", {}).get(row["node"])
            or {}
        )
        if latest.get("cpu_admission") != row["job_key"]:
            conn.execute("DELETE FROM job_cpu_waits WHERE run_id=? AND node=?", (run_id, row["node"]))


def admitted(conn, key):
    if exists(conn):
        conn.execute("DELETE FROM job_cpu_waits WHERE job_key=?", (key,))


def refusal(conn, room, needed, key=None):
    from .limits import cpu_threads_finding

    problem = cpu_threads_finding(
        needed, free=room["free"], allowed=room["allowed"], held=room["held"], holders=room["holders"]
    )
    if needed > room["allowed"]:
        return problem
    head = positions(conn, room["allowed"], head_only=True)
    if head and head[0]["job_key"] != key:
        first = head[0]
        problem = ResourceDenied(
            f"CPU admission is waiting for {first['run_id']} / {first['node']} "
            f"({first['required_threads']} threads)",
            details={
                "rule": "cpu.threads_free",
                "needed": needed,
                "allowed": room["allowed"],
                "free": room["free"],
                "held": room["held"],
                "holders": room["holders"],
                "ahead": first["job_key"],
                "ahead_wait_seconds": max(0.0, clock.age_seconds(first["since"])),
            },
        )
    if problem and key:
        problem.details["job_key"] = key
    return problem
