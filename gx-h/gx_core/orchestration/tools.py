"""The six run tools, published through the registry.

Thin on purpose: each forwards to :mod:`gx_core.orchestration.service` with the
caller's context. The caller of a run-level write is identified by the coordinator lease
it presents and the principal the backend settled on, never by a field of the request.
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..registry import ToolResult, tool
from . import service


def _lift(ctx: Ctx, out: dict[str, Any]) -> dict[str, Any]:
    for message in out.pop("warnings", []) or []:
        ctx.warn(message)
    if out.pop("replayed", False):
        ctx.warn("this idempotency key was already used for the same request; the stored answer is returned")
    return out


@tool("gx_run_start")
def run_start(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _lift(ctx, service.run_start(ctx, args))
    return ToolResult(data=out, run_id=out["run_id"], revision=out["revision"], status=out["status"])


@tool("gx_run_status")
def run_status(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _lift(ctx, service.run_status(ctx, args["run_id"]))
    return ToolResult(data=out, run_id=args["run_id"], revision=out["revision"], status=out["status"])


@tool("gx_next_actions")
def next_actions(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _lift(ctx, service.next_actions(ctx, args["run_id"], args.get("limit")))
    refs = [item["brief_ref"] for item in out["next_actions"] if item.get("brief_ref")]
    return ToolResult(data=out, artifact_refs=refs, run_id=args["run_id"], revision=out["revision"])


@tool("gx_run_resume")
def run_resume(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _lift(ctx, service.run_resume(ctx, args))
    return ToolResult(data=out, run_id=args["run_id"], revision=out["revision"], status=out["status"])


@tool("gx_coordinator_claim")
def coordinator_claim(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _lift(ctx, service.coordinator_claim(ctx, args))
    return ToolResult(data=out, run_id=args["run_id"], revision=out["revision"])


@tool("gx_run_control")
def run_control(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _lift(ctx, service.run_control(ctx, args))
    return ToolResult(data=out, run_id=args["run_id"], revision=out["revision"], status=out["status"])
