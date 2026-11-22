"""Follow up on accepted asset submissions using the persisted task and subject."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..context import Ctx
from . import expansion, seams


def _open_stage(conn: sqlite3.Connection, run_id: str, task: sqlite3.Row) -> str | None:
    """Use the stage which still owns the task in the current workflow round."""
    run = conn.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
    stage = task["stage"]
    if stage and run and stage == run["phase"]:
        if expansion.task_round(conn, task["task_id"]) == expansion.current_round(conn, run_id):
            return stage
    return None


def review_accepted(ctx: Ctx, run_id: str, task: sqlite3.Row, submission: sqlite3.Row) -> dict[str, Any]:
    if task["subject_kind"] != "asset" or _open_stage(ctx.conn, run_id, task) is None:
        return {"reaction": "review_recorded"}
    for asset in ctx.conn.execute(
        "SELECT asset_id, version FROM assets WHERE asset_id=? AND content_sha256=?"
        " AND kind IN ('TOOL_ADAPTER','ROLE_INTENT')",
        (task["subject_id"], task["subject_sha256"]),
    ).fetchall():
        seams.advance_asset(ctx, asset["asset_id"], asset["version"])
    return {"reaction": "review_recorded"}


def asset_accepted(ctx: Ctx, run_id: str, task: sqlite3.Row, submission: sqlite3.Row) -> dict[str, Any]:
    stage = _open_stage(ctx.conn, run_id, task)
    if stage is None:
        return {"reaction": None, "why": "the submitting task has no open workflow stage"}
    created = []
    for asset in ctx.conn.execute(
        "SELECT asset_id, version FROM assets WHERE author_task_id=? AND kind='TOOL_ADAPTER'", (task["task_id"],),
    ).fetchall():
        review = seams.asset_review_for(
            ctx, asset["asset_id"], asset["version"], run_id=run_id, stage=stage,
            reservation_ref=expansion.follower_reservation(ctx.conn, task["task_id"], "review"),
        )
        created.append(review["task_id"])
    return {"reaction": "asset_review", "created": created}
