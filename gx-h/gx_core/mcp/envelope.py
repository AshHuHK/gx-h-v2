"""The one response shape, and the size limits that keep it readable.

``contracts/mcp_tools.json`` declares both: the ``ok / request_id / run_id / revision /
status / data / artifact_refs / warnings / error`` envelope, and its ceilings
(``max_response_bytes`` 24,000, ``max_rows`` 200). The ceiling sits below what either
host does to a tool result -- a shell result is cut near 30,000 characters and an MCP
result near 25,000 tokens -- because a result the host truncates is a result the worker
reads wrongly, and it cannot tell.

So an oversized result is never trimmed quietly. It is:

1. truncated to ``max_rows`` per list, with a warning naming the key and the real count;
2. stored whole as an artifact, with the reference returned in ``artifact_refs``;
3. replaced by a bounded ``response_overflow`` summary if even the trimmed form is too
   large.

If the full payload cannot be stored we do **not** mint a reference that nothing can
resolve. The response says the payload was left out.

A handler that knows its result may be large (a verdict over two thousand records, a
long listing) should bound it itself and attach its own artifact, the way
``gx_task_submit`` does; this module is the net under every handler, not the plan.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .. import contracts
from ..domain import clock, ids
from ..errors import GxError
from ..registry import ToolResult

OVERFLOW_KIND = "response_overflow"


def limits() -> tuple[int, int]:
    table = contracts.response_limits()
    return int(table["max_response_bytes"]), int(table["max_rows"])


def success(
    request_id: str,
    result: ToolResult,
    *,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "request_id": request_id,
        "run_id": result.run_id,
        "revision": result.revision,
        "status": result.status,
        "data": result.data,
        "artifact_refs": list(result.artifact_refs),
        "warnings": list(extra_warnings or []) + list(result.warnings),
        "error": None,
    }


def failure(
    request_id: str,
    error: GxError,
    *,
    run_id: str | None = None,
    revision: int | None = None,
    status: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": request_id,
        "run_id": run_id,
        "revision": revision,
        "status": status,
        "data": {},
        "artifact_refs": [],
        "warnings": list(warnings or []),
        "error": error.to_payload(),
    }


def encoded_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def bound_failure(envelope: dict[str, Any]) -> dict[str, Any]:
    """Keep a refusal inside the ceiling by trimming its ``details``, never its reason.

    A refusal carries what the caller must do next in ``code``, ``message`` and
    ``suggested_recovery``; ``details`` is supporting material and is the part that can
    grow (a list of two hundred violations). It is replaced by a note when the whole
    would not fit.
    """
    max_bytes, _ = limits()
    if encoded_size(envelope) <= max_bytes:
        return envelope
    error = dict(envelope.get("error") or {})
    details = error.get("details")
    if details is not None:
        error["details"] = {
            "truncated": True,
            "note": "the refusal's details exceeded the response limit and were left out",
            "keys": sorted(details)[:40] if isinstance(details, dict) else None,
        }
    message = str(error.get("message", ""))
    if len(message) > 2000:
        error["message"] = message[:2000] + " …[cut]"
    envelope["error"] = error
    envelope["warnings"] = list(envelope.get("warnings", []))[:20] + [
        "the refusal's details were trimmed to fit max_response_bytes"
    ]
    return envelope


def enforce_limits(
    envelope: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    tool: str | None = None,
    run_id: str | None = None,
    actor: str = "backend",
) -> dict[str, Any]:
    """Apply ``max_rows`` then ``max_response_bytes``, storing what does not fit."""
    if not envelope.get("ok"):
        return bound_failure(envelope)

    max_bytes, max_rows = limits()
    data = envelope.get("data")
    original = data if isinstance(data, dict) else {}

    spill_needed = False
    trimmed: dict[str, Any] = dict(original)
    for key, value in original.items():
        if isinstance(value, list) and len(value) > max_rows:
            trimmed[key] = value[:max_rows]
            envelope["warnings"].append(
                f"data.{key} truncated to max_rows={max_rows} of {len(value)} rows; "
                "the full set is in the attached artifact"
            )
            spill_needed = True
    envelope["data"] = trimmed

    if not spill_needed and encoded_size(envelope) <= max_bytes:
        return envelope

    spill = _spill(original, conn=conn, tool=tool, run_id=run_id,
                   request_id=envelope.get("request_id"), actor=actor)
    if spill.get("artifact_ref"):
        envelope["artifact_refs"] = list(envelope.get("artifact_refs", [])) + [spill["artifact_ref"]]
    else:
        envelope["warnings"].append(
            "the full payload could not be stored as an artifact "
            f"({spill.get('reason', 'unknown reason')}); it is not included in this response"
        )

    if encoded_size(envelope) <= max_bytes:
        return envelope

    summary = {
        "response_overflow": {
            "reason": f"response exceeded max_response_bytes={max_bytes}",
            "encoded_bytes": spill.get("bytes"),
            "sha256": spill.get("sha256"),
            "keys": sorted(original)[:50],
            "row_counts": {
                key: len(value) for key, value in original.items() if isinstance(value, list)
            },
            "artifact_ref": spill.get("artifact_ref"),
            "retrieve_with": "gx_artifact_get with artifact_ref and materialize=true when offered, "
                             "or with offset and limit for text pages. CLI clients can use gx get <artifact_ref> --out.",
        }
    }
    if tool == "gx_task_claim":
        # The claim already happened. Its small control fields must survive a
        # large scientific task/history; the attached artifact keeps all bodies.
        control = {key: original[key] for key in
                   ("lease_ref", "expires_at", "attempt", "brief_ref", "brief_sha256", "revision")
                   if key in original}
        task = original.get("task")
        if isinstance(task, dict):
            control["task"] = {key: task[key] for key in
                ("task_id", "run_id", "kind", "role", "status", "attempt", "brief_ref", "brief_sha256", "lease_id")
                if key in task}
        summary = {**control, **summary}
    elif tool in ("gx_job_status", "gx_job_wait"):
        summary.update({key: original[key] for key in
                        ("job_id", "status", "terminal", "exit_code", "waited_seconds") if key in original})
        # The complete Job view already contains every per-output reference. Repeating
        # those references above the summary can exceed the transport limit on its own.
        envelope["artifact_refs"] = ([spill["artifact_ref"]] if spill.get("artifact_ref")
                                     else envelope["artifact_refs"][:max_rows])
    envelope["data"] = summary
    envelope["warnings"] = list(envelope["warnings"])[:20] + [
        f"response body replaced by a bounded summary: it exceeded max_response_bytes={max_bytes}"
    ]
    return envelope


def _spill(
    payload: dict[str, Any],
    *,
    conn: sqlite3.Connection | None,
    tool: str | None,
    run_id: str | None,
    request_id: str | None,
    actor: str,
) -> dict[str, Any]:
    """Store the full payload as an artifact. Never fabricates a reference.

    The write goes through :class:`gx_core.artifacts.service.ArtifactService`, so the
    spill lands where ``gx_artifact_get`` and ``gx get --out`` will look for it. A
    payload with no run is filed under ``shared``.
    """
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    raw = body.encode("utf-8")
    sha = ids.sha256_of_obj({"tool": tool, "body": body})
    out: dict[str, Any] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "artifact_ref": None}

    if conn is None:
        out["reason"] = "no store connection on this request"
        return out

    try:
        from ..artifacts.service import ArtifactService
    except ImportError as exc:  # pragma: no cover - the artifact service is core
        out["reason"] = f"the artifact service is not available: {exc}"
        return out

    namespace = f"{run_id}/overflow" if run_id else "shared/overflow"
    rel_path = f"{tool or 'tool'}/{sha[:24]}.json"
    provenance = {"tool": tool, "request_id": request_id, "reason": "response exceeded the envelope limits",
                  "created_at": clock.now_iso()}
    if tool in ("gx_job_status", "gx_job_wait") and payload.get("job_id"):
        provenance["job_id"] = payload["job_id"]
    try:
        service = ArtifactService(conn)
        published = service.publish_bytes(
            namespace=namespace, rel_path=rel_path, kind=OVERFLOW_KIND, data=raw,
            run_id=run_id, media_type="application/json",
            provenance=provenance,
            actor=actor, origin="BACKEND",
        )
        out["artifact_ref"] = published.artifact_ref
        out["bytes"] = published.bytes
        out["sha256"] = published.sha256
    except (sqlite3.Error, OSError, GxError) as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out


def job_response_owner(conn: sqlite3.Connection, run_id: str, artifact_ref: str) -> str | None:
    """The Job named by this run's backend-generated status or wait response."""
    row = conn.execute("SELECT provenance_json FROM artifacts WHERE artifact_ref=? AND run_id=? "
                       "AND namespace=? AND kind=? AND origin='BACKEND' AND published=1",
                       (artifact_ref, run_id, f"{run_id}/overflow", OVERFLOW_KIND)).fetchone()
    if row is None:
        return None
    metadata = json.loads(row["provenance_json"])
    if metadata.get("tool") not in ("gx_job_status", "gx_job_wait"):
        return None
    if "job_id" not in metadata:
        # Earlier spills kept the Job ID only in the original, digest-verified response.
        from ..artifacts.service import ArtifactService
        metadata = ArtifactService(conn).read_json(artifact_ref)
    job_id = metadata.get("job_id") if isinstance(metadata, dict) else None
    return job_id if isinstance(job_id, str) else None


__all__ = [
    "OVERFLOW_KIND", "ToolResult", "bound_failure", "encoded_size", "enforce_limits",
    "failure", "limits", "success",
]
