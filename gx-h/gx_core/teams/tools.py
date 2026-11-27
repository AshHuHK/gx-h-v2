"""The four task tools, published through the registry.

Thin on purpose: each builds the task service for the caller's context and forwards.
Nothing here decides anything the service does not, and nothing reads an owner, a role
or an approval out of the arguments -- the lease holder is the principal the backend
settled on, never a field of the request.
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..registry import ToolResult, tool
from .service import TeamService


def _service(ctx: Ctx) -> TeamService:
    return TeamService(ctx.conn, ctx=ctx)


@tool("gx_shared_board")
def shared_board(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    from .board import SharedBoardService, bounded_transport

    out = SharedBoardService(ctx.conn, ctx=ctx).snapshot(
        run_id=args["run_id"], after_event_id=args.get("after_event_id", 0),
        event_limit=args.get("event_limit", 100), after_reaction_id=args.get("after_reaction_id", 0),
        after_feedback_event_id=args.get("after_feedback_event_id", 0),
        board_limit=args.get("board_limit", 50), after_board=args.get("after_board"),
    )
    out = bounded_transport(out, after_board=args.get("after_board"), board_limit=args.get("board_limit", 50))
    return ToolResult(data=out, run_id=args["run_id"], revision=out["run"]["revision"], status=out["run"]["status"])


@tool("gx_model_activity")
def model_activity(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    from ..research.model_activity import native_activity
    result = native_activity(_service(ctx), args["task_id"])
    return ToolResult(data=result, run_id=result["run_id"],
                      artifact_refs=[result["artifact_ref"]] if result["artifact_ref"] else [])


@tool("gx_task_acquire")
def task_acquire(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _service(ctx).acquire_task(task_id=args["task_id"])
    return ToolResult(data=out, run_id=out.get("run_id"), revision=out.get("revision"),
                      artifact_refs=[out["brief_ref"]] if out.get("brief_ref") else [])


@tool("gx_task_claim")
def task_claim(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    # Contract r5: the precondition is the task's (expected_attempt, the plan's as_of_event_id);
    # the run revision is not compared (task.rules.claim).
    out = _service(ctx).claim(
        task_id=args["task_id"], worker_role=args["worker_role"], lease_seconds=args.get("lease_seconds"),
        expected_attempt=args["expected_attempt"], as_of_event_id=args.get("as_of_event_id"),
    )
    refs = [out["brief_ref"]] if out.get("brief_ref") else []
    if not refs:
        ctx.warn(
            "this task has no compiled brief; the task object in this answer (kind, input, "
            "bundle_kind, acceptance, previous_attempts) is the whole instruction"
        )
    return ToolResult(data=out, artifact_refs=refs, run_id=out["task"]["run_id"],
                      revision=out["revision"])


@tool("gx_task_bind")
def task_bind(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _service(ctx).bind(
        lease_ref=args["lease_ref"], host=args["host"], evidence_level=args["evidence_level"],
        idempotency_key=args["idempotency_key"], agent_ref=args.get("agent_ref"),
        host_session_ref=args.get("host_session_ref"), model=args.get("model"),
        effort=args.get("effort"), delegation_mode=args.get("delegation_mode"),
    )
    if out.get("replayed"):
        ctx.warn("this idempotency key was already used for the same bind; the stored answer is returned")
    # Which session was recorded is said in a warning: the tool's declared output has no key
    # for it, and an answer carries no key its contract entry does not declare.
    session, basis = out.pop("host_session_ref", None), out.pop("host_session_ref_basis", None)
    if basis == "launch_environment":
        ctx.warn(f"the worker named no host session; the session this call came from ({session}) "
                 "was recorded, so the host's record of that session is what is read")
    return ToolResult(data=out, run_id=out.get("run_id"))


@tool("gx_task_finish")
def task_finish(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _service(ctx).finish_task(task_id=args["task_id"], manifest_ref=args["manifest_ref"],
                                    summary=args.get("summary"))
    return ToolResult(data=out, artifact_refs=out["artifact_refs"], run_id=out["run_id"])


@tool("gx_task_submit")
def task_submit(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _service(ctx).submit(
        task_id=args["task_id"], lease_ref=args["lease_ref"],
        artifact_refs=args.get("artifact_refs") or [], summary=args.get("summary"),
        idempotency_key=args["idempotency_key"],
    )
    if out.get("replayed"):
        ctx.warn("this idempotency key was already used for the same submission; the stored answer is returned")
    settle_warning = out.pop("settle_warning", None)
    if settle_warning:
        ctx.warn(settle_warning)
    validation = out.get("validation") or {}
    if validation.get("held"):
        ctx.warn(
            "the submission is kept and not judged: the worker is outside the run's agent "
            f"policy ({validation.get('policy_verdict')}). The run waits for approval "
            f"{validation.get('approval_id')}; nothing continues on another model by default"
        )
    elif validation.get("complete") is False:
        ctx.warn(
            f"{validation.get('records_total')} records were judged and "
            f"{validation.get('records_shown')} are shown, refusals first; every verdict is "
            "in the attached artifact"
        )
    refs = [out["records_ref"]] if out.get("records_ref") else []
    return ToolResult(data=out, artifact_refs=refs, run_id=out.get("run_id"),
                      revision=out.get("revision"))


@tool("gx_bundle_check")
def bundle_check(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    out = _service(ctx).bundle_check(
        artifact_ref=args["artifact_ref"], bundle_kind=args["bundle_kind"],
        task_id=args.get("task_id"), lease_ref=args.get("lease_ref"),
    )
    for message in out.pop("warnings", []):
        ctx.warn(message)
    if out.get("records_ref"):
        ctx.warn(
            f"{out['records_total']} records were judged and {len(out['records'])} are shown, "
            "refusals first; every verdict is in the attached artifact"
        )
    refs = [out["records_ref"]] if out.get("records_ref") else []
    return ToolResult(data=out, artifact_refs=refs, run_id=out.get("run_id"))

# This package publishes its additive family through its single provider entry.
from . import manager_tools as _manager_tools  # noqa: F401,E402
