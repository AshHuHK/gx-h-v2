"""Durable keyed task expansions shared by controllers and fact reactions.

The idempotency claim, tasks, lineage and schedule.expanded commit together.
Keys identify products, never polling calls or process identities.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .. import config
from ..context import Ctx
from ..domain import clock, ids
from ..errors import IdempotencyConflict, OutcomeUnknown, PhaseDenied, StaleRevision
from ..persistence import store
from ..teams.service import TeamService
from . import seams


def fact_key(run_id: str, rule: str, fact_id: Any) -> str:
    return f"exp:{run_id}:{rule}:{fact_id}"


def round_key(run_id: str, stage: str, round_index: int, rule: str, index: Any = 0) -> str:
    return f"exp:{run_id}:{stage}:r{round_index}:{rule}:{index}"


def current_round(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='workflow.reentered'"
                       " ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
    return int((store.loads(row, "payload_json", {}) or {}).get("round") or 0) if row else 0


def round_start(conn: sqlite3.Connection, run_id: str, index: int) -> int:
    if index == 0:
        return 0
    row = conn.execute("SELECT event_id FROM events WHERE run_id=? AND kind='workflow.reentered'"
                       " AND json_extract(payload_json, '$.round')=? ORDER BY event_id DESC LIMIT 1",
                       (run_id, index)).fetchone()
    return int(row["event_id"]) if row else 0


def task_round(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute("SELECT e.run_id, e.event_id FROM events e WHERE e.subject_kind='task'"
                       " AND e.subject_id=? ORDER BY e.event_id LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return 0
    prior = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='workflow.reentered'"
                         " AND event_id<? ORDER BY event_id DESC LIMIT 1",
                         (row["run_id"], row["event_id"])).fetchone()
    return int((store.loads(prior, "payload_json", {}) or {}).get("round") or 0) if prior else 0


def stage_tasks(conn: sqlite3.Connection, run_id: str, stage: str,
                index: int | None = None) -> list[sqlite3.Row]:
    index = current_round(conn, run_id) if index is None else index
    rows=list(conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid",(run_id,)))
    rounds={row["task_id"]:task_round(conn,row["task_id"]) for row in rows}
    members={row["task_id"] for row in rows if row["stage"]==stage and rounds[row["task_id"]]==index}
    # Canonical owner listeners written before stages can create their review or
    # scenario followers with stage NULL. Their stored lineage still affiliates
    # them with this chain; status/resume must not lose live work for that omission.
    parents: dict[str,set[str]]={row["task_id"]:set() for row in rows}
    for row in rows:
        if row["independent_of_task_id"]:
            parents[row["task_id"]].add(row["independent_of_task_id"])
    for row in conn.execute("SELECT d.task_id,d.depends_on FROM task_deps d JOIN tasks t ON t.task_id=d.task_id WHERE t.run_id=?",(run_id,)):
        parents[row["task_id"]].add(row["depends_on"])
    for edge in conn.execute("SELECT d.* FROM dependencies d JOIN tasks t ON t.task_id=d.subject_id"
        " WHERE t.run_id=? AND d.subject_kind='task' AND d.relation IN ('follows','independent_of','waits_on')",(run_id,)):
        parent=edge["depends_on_id"]
        if edge["depends_on_kind"]=="job":
            job=conn.execute("SELECT task_id FROM jobs WHERE job_id=?",(parent,)).fetchone()
            parent=job["task_id"] if job else None
        elif edge["depends_on_kind"]!="task":
            continue
        if parent:
            parents[edge["subject_id"]].add(parent)
    changed=True
    while changed:
        changed=False
        for row in rows:
            task_id=row["task_id"]
            if task_id not in members and not row["stage"] and rounds[task_id]==index and parents[task_id]&members:
                members.add(task_id)
                changed=True
    return [row for row in rows if row["task_id"] in members]


def replay(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
    if row is None:
        return None
    response = store.loads(row, "response_json", None)
    return dict(response) if isinstance(response, Mapping) else None


def guard(conn: sqlite3.Connection, run_id: str, *, stage: str | None,
          round_index: int | None) -> sqlite3.Row:
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or run["status"] not in ("ACTIVE", "PLANNING", "EVALUATING", "WAITING_CLIENT",
                                          "WAITING_EXTERNAL_JOB", "WAITING_APPROVAL"):
        raise PhaseDenied("tasks expand only while the run is dispatching",
                          details={"run_id": run_id, "status": None if run is None else run["status"]})
    if stage is not None and run["phase"] != stage:
        raise StaleRevision("the expansion's stage has closed", details={"stage": stage, "phase": run["phase"]})
    if round_index is not None and current_round(conn, run_id) != round_index:
        raise StaleRevision("the expansion's round has closed", details={"round": round_index})
    return run


def expand(ctx: Ctx, run_id: str, *, rule: str, key: str, trigger: Mapping[str, Any],
           tasks: Sequence[Mapping[str, Any]], stage: str | None = None, round_index: int | None = None,
           follows: Sequence[tuple[str, str]] = (), independent_of: Sequence[str] = (),
           followers: Mapping[str, Mapping[str, Any]] | None = None,
           predicate: Callable[[sqlite3.Connection], bool] | None = None,
           teams: TeamService | None = None) -> dict[str, Any]:
    """Create one product, funding mandatory followers before its head.

    Existing follower holds are supplied through a task's reservation_ref. The
    predicate rechecks stored facts in the same transaction as task creation.
    """
    conn = ctx.conn
    config.check_holds("orchestration")
    service = teams or TeamService(conn, ctx=ctx)
    normalized=[]
    for spec in tasks:
        item=dict(spec)
        subject=item.get("subject")
        if subject is not None and not isinstance(subject,Mapping):
            item["subject"]={name:getattr(subject,name,None) for name in ("kind","id","sha256","version")}
        normalized.append(item)
    product_sha=ids.sha256_of_obj({"tasks":normalized,"follows":list(follows),
        "independent_of":list(independent_of),"followers":dict(followers or {}),"stage":stage,"round":round_index})
    with store.transaction(conn) as tx:
        record = store.begin_idempotent(tx, key, "schedule.expand", {"rule": rule, "key": key})
        if record.replay:
            if (record.response or {}).get("product_sha256") not in (None,product_sha):
                raise IdempotencyConflict("the expansion key already names a different immutable product",
                                          details={"key":key})
            return {**(record.response or {}), "replayed": True}
        if not record.fresh:
            raise OutcomeUnknown("an expansion is incomplete; reconcile its product before retrying",
                                 details={"key": key, "side_effect_status": record.side_effect_status})
        run = guard(tx, run_id, stage=stage, round_index=round_index)
        if predicate is not None and not predicate(tx):
            raise StaleRevision("the expansion predicate no longer holds", details={"key": key})
        wanted = {f"task{i}": {"worker_tasks": 1} for i, spec in enumerate(tasks)
                  if not spec.get("reservation_ref")}
        wanted.update(dict(followers or {}))
        reservations: dict[str, Any] = {}
        if run["budget_ref"] and wanted:
            chain = seams.reserve_task_chain(tx, budget_ref=run["budget_ref"], head_id=key,
                                             followers=wanted, actor=ctx.actor)
            reservations = dict(chain.get("followers") or {})
        created = []
        for index, spec in enumerate(tasks):
            arguments = dict(spec)
            arguments.setdefault("stage", stage)
            arguments.setdefault("acceptance", {})
            arguments.setdefault("input", {})
            arguments.setdefault("actor", ctx.actor)
            if f"task{index}" in reservations:
                arguments["reservation_ref"] = reservations[f"task{index}"]["reservation_ref"]
            task = service.create_task(run_id=run_id, **arguments)
            created.append(task["task_id"])
            for kind, subject_id in follows:
                tx.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?,?,?,?,?)",
                           ("task", task["task_id"], kind, subject_id, "follows", clock.now_iso()))
            for other in independent_of:
                tx.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?,?,?,?,?)",
                           ("task", task["task_id"], "task", other, "independent_of", clock.now_iso()))
        event_id = store.append_event(tx, kind="schedule.expanded", actor=ctx.actor, run_id=run_id,
                                     subject_kind="run", subject_id=run_id, evidence_level="EXECUTION_OBSERVED",
                                     payload={"decided_by": "backend", "key": key, "rule": rule,
                                              "trigger": dict(trigger), "created": created,
                                              "stage": stage, "round": round_index})
        answer = {"key": key, "task_ids": created, "event_id": event_id, "reservations": reservations,
                  "product_sha256":product_sha}
        store.complete_idempotent(tx, key, answer)
    return {**answer, "replayed": False}


def follower_reservation(conn: sqlite3.Connection, head_task_id: str, follower: str) -> str | None:
    for row in conn.execute("SELECT payload_json FROM events WHERE kind='schedule.expanded'"
                            " ORDER BY event_id DESC"):
        event = store.loads(row, "payload_json", {}) or {}
        if head_task_id in (event.get("created") or []):
            record = replay(conn, event["key"]) or {}
            held = (record.get("reservations") or {}).get(follower) or {}
            if held.get("reservation_ref"):
                return str(held["reservation_ref"])
    return None
