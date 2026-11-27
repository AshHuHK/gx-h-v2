"""The two artifact tools: how content enters the store and how it is read back.

``gx_artifact_put`` is the tool v1 did not have. A task there required an artifact, the
protocol offered no way to create one, and the only thing that satisfied a requirement
was the bare name of the artifact's kind passed in-process. Content a host puts is a
claim (``origin = HOST_PUT``) until a validator has judged it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import contracts
from ..context import Ctx
from ..domain import machines
from ..errors import PhaseDenied, PolicyScreenBlocked, SchemaInvalid, ScopeDenied
from ..persistence import store
from ..registry import ToolResult, tool
from .service import ArtifactService

#: Bytes left for the envelope's own fields and the artifact's metadata around a read.
_READ_OVERHEAD = 3500


@tool("gx_artifact_put")
def artifact_put(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    run_id = args["run_id"]
    status = ctx.authz.get("status")
    if status in machines.terminal_states("run"):
        raise PhaseDenied(
            f"run {run_id} is {status}; nothing is put into a run that has ended",
            details={"run_id": run_id, "status": status},
        )
    service = ArtifactService(ctx.conn)
    request = {
        "run_id": run_id, "task_id": args.get("task_id"), "kind": args["kind"],
        "media_type": args["media_type"], "content": args.get("content"),
        "inbox_path": args.get("inbox_path"),
    }

    def work(_tx: Any) -> dict[str, Any]:
        published = service.put(
            run_id=run_id, kind=args["kind"], media_type=args["media_type"],
            content=args.get("content"), inbox_path=args.get("inbox_path"),
            task_id=args.get("task_id"), origin="HOST_PUT", put_by=ctx.actor,
        )
        return {
            "artifact_ref": published.artifact_ref, "sha256": published.sha256,
            "bytes": published.bytes, "kind": published.kind,
            "media_type": published.media_type, "origin": "HOST_PUT",
            "deduplicated": published.reused,
        }

    try:
        response, replayed = store.run_idempotent(
            ctx.conn, store.tool_key(ctx.tool, args["idempotency_key"]), ctx.tool, request, work
        )
    except PolicyScreenBlocked as exc:
        # The tool answers only with codes its contract entry lists. Until the entry lists
        # POLICY_SCREEN_BLOCKED (queued for contract r5) the same refusal is the format
        # error it also is; the details say which code it becomes.
        if "POLICY_SCREEN_BLOCKED" in (contracts.any_tool_spec(ctx.tool).get("errors") or []):
            raise
        raise SchemaInvalid(exc.message, details={**dict(exc.details or {}),
                                                  "code_once_declared": "POLICY_SCREEN_BLOCKED"}) from exc
    if replayed:
        ctx.warn("this idempotency key was already used for the same put; the stored answer is returned")
    return ToolResult(data=response, artifact_refs=[response["artifact_ref"]], run_id=run_id)


def held_out_label(conn: sqlite3.Connection, artifact_ref: str) -> list[str]:
    """The HELD_OUT records this artifact is a label of: their reference, or an output of the job that built one. Rows only.

    For a reader that serves artifacts to workers (``gx_artifact_get``): a label is read only by
    the backend's evaluator under a counted look. An output of the job that produced a held-out
    reference carries the label too (the alignment the reference was derived from). Empty: no label.
    """
    found = [f"{row['dataset_id']}@{row['version']}/{row['record_id']}" for row in conn.execute(
        "SELECT dataset_id, version, record_id FROM dataset_records WHERE split=? AND reference_artifact_ref=?",
        ("HELD_OUT", artifact_ref))]
    published = conn.execute("SELECT job_id FROM artifacts WHERE artifact_ref=?", (artifact_ref,)).fetchone()
    if published is not None and published["job_id"]:
        found += [f"{row['dataset_id']}@{row['version']}/{row['record_id']}" for row in conn.execute(
            "SELECT dataset_id, version, record_id FROM dataset_records WHERE split=? AND produced_by_job_id=?",
            ("HELD_OUT", published["job_id"]))]
    return sorted(set(found))


@tool("gx_artifact_get")
def artifact_get(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    """Bounded content by reference, sized so the whole answer fits one response.

    JSON escaping can double the size of text full of quotes and newlines, so the window
    is shrunk until the encoded answer fits. The answer carries a continuation offset.
    A local native thread may explicitly request a complete copy in its workspace.

    A held-out record's label is never served (the lead's ruling, 2026-11-13 01:47Z, addition 2
    for the confirmatory read): not to a worker, not to an operator. The backend's evaluator
    reads it through the service under a counted look; a person reads it outside the platform.
    """
    labels = held_out_label(ctx.conn, args["artifact_ref"])
    if labels:
        raise ScopeDenied(
            f"{args['artifact_ref']} is the label of held-out record(s) {labels[:5]}: a label is read only "
            "by the backend's evaluator, under a counted look",
            details={"artifact_ref": args["artifact_ref"], "held_out_records": labels})
    service = ArtifactService(ctx.conn)
    local_workspace = ctx.native_call._issuer.artifact_workspace if ctx.native_call is not None else None
    if ctx.authz.get("project_ref") not in ctx.principal.project_refs:
        local_workspace = None
    if args.get("materialize"):
        if local_workspace is None:
            raise ScopeDenied("file delivery requires this native thread's registered project workspace")
        if any(field in args for field in ("section", "offset", "limit")):
            raise SchemaInvalid("For a complete artifact with materialize=true, omit section, offset and limit.")
        path = service.materialize(args["artifact_ref"], workspace=local_workspace)
        metadata = service.describe(args["artifact_ref"])
        metadata.update(workspace_path=str(path), verified=True)
        return ToolResult(data={"metadata": metadata, "sha256": metadata["sha256"], "content": None,
                                "truncated": False}, artifact_refs=[args["artifact_ref"]], run_id=metadata["run_id"])
    ceiling = int(contracts.response_limits()["max_response_bytes"])
    budget = ceiling - _READ_OVERHEAD
    out = service.get(args["artifact_ref"], section=args.get("section"), limit=args.get("limit"),
                      offset=args.get("offset"), max_bytes=budget, max_encoded_bytes=budget + 1500)
    if ctx.native_call is not None and out.get("content") is None:
        out["note"] = ("To open the binary file, call gx_artifact_get with artifact_ref and materialize=true."
                       if local_workspace is not None else
                       "This thread returns binary metadata. Compatible GX Jobs can accept the artifact reference.")
    elif out.get("truncated"):
        if local_workspace is not None:
            ctx.warn("For the complete file, call gx_artifact_get with just artifact_ref and materialize=true, "
                     "then open data.metadata.workspace_path. Text pages continue at metadata.next_offset.")
        else:
            ctx.warn("Read the next text page by passing metadata.next_offset as offset to gx_artifact_get.")
    return ToolResult(data=out, artifact_refs=[args["artifact_ref"]],
                      run_id=out.get("metadata", {}).get("run_id"))
