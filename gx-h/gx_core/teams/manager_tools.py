"""Typed transport entries for the persistent team management service."""
from __future__ import annotations

import json
from typing import Any, Mapping

from ..context import Ctx
from ..registry import BriefSection, ToolResult, brief_section, tool
from .manager import TeamManager, assignment_artifacts, assignment_intent


@tool("gx_team_manage")
def team_manage(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = TeamManager(ctx).manage(**args)
    return ToolResult(data=out, run_id=out["run_id"], revision=out["revision"])


@tool("gx_team_message")
def team_message(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = TeamManager(ctx).message(**args)
    return ToolResult(data=out, run_id=out["run_id"], revision=out["revision"])


@tool("gx_team_status")
def team_status(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = TeamManager(ctx).snapshot(**args)
    return ToolResult(data=out, run_id=out["run_id"], revision=out["revision"])


@brief_section("team_assignment", order=12)
def team_assignment_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    assignment = ctx.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task["task_id"],)).fetchone()
    if assignment is None:
        return None
    intent = assignment_intent(ctx.conn, assignment)
    products = assignment_artifacts(ctx.conn, task, assignment)
    frozen = {"team_id": assignment["team_id"], "assignment_generation": assignment["assignment_generation"],
              "principal_subject": assignment["subject"], "intent_version": intent["version"],
              "intent_sha256": intent["sha256"], "context_ref": assignment["context_ref"],
              "products": products}
    lines = ["This task is bound to the following immutable assignment, independently of later team intent changes.",
             "Read the assigned team intent and any context artifact with gx_artifact_get.",
             "Team membership and messages grant no coordinator, approval or scientific review authority.",
             "```json", json.dumps(frozen, sort_keys=True, ensure_ascii=False, indent=1), "```"]
    refs = [assignment["team_id"], *[product["artifact_ref"] for product in products.values()]]
    return BriefSection(title="Frozen team assignment and context", body="\n".join(lines), refs=refs)
