"""One local operator decision while the native project is between sessions.

The existing controlling-terminal prompt records explicit local confirmation in
GX's single-user trust domain. A PTY does not authenticate a person against
other processes sharing the same OS account. This entry is absent from all
native worker catalogs and the state owner excludes concurrent Host sessions.
"""
import json
import os
import pwd
from typing import Any

from ..context import Ctx, Principal, SCOPE_OPERATOR
from ..domain import machines
from ..errors import PermissionDenied, ScopeDenied
from ..hostobs import native_context, policy
from ..persistence import store
from ..teams.service import TeamService
from . import approvals


def _confirm_as_person(tool: str, arguments: dict[str, Any]) -> bool:
    """Ask the person at the controlling terminal. False when there is none.

    The terminal is opened by name rather than taken from stdin, so arguments piped in
    do not stand in for an answer and an answer cannot be piped in with them.

    It is opened as a bare descriptor. A buffered read-write text stream cannot be built
    on a device that does not seek, so ``open("/dev/tty", "r+")`` raised whenever there
    *was* a terminal, and the handler written for "there is none" turned that into
    "no": nobody could ever confirm. This is a procedural boundary like a scope: a
    process can give its child a terminal, and what is recorded is that a confirmation
    was read from one.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    try:
        if not os.isatty(fd):
            return False
        prompt = (
            f"\n{tool} records a decision as yours.\n"
            + json.dumps(arguments, ensure_ascii=False, indent=2)[:4000]
            + "\nType yes to make it: "
        )
        os.write(fd, prompt.encode("utf-8"))
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 256:
            chunk = os.read(fd, 64)
            if not chunk:
                break
            answer += chunk
        return answer.decode("utf-8", "replace").strip().lower() == "yes"
    finally:
        os.close(fd)


def _model_difference(conn, approval):
    """Show the native settings behind a submission hold, excluding refused work."""
    row = conn.execute(
        "SELECT t.required_model,t.required_effort_json,o.payload_json"
        " FROM submissions s JOIN tasks t ON t.task_id=s.task_id"
        " JOIN worker_observations o ON o.task_id=t.task_id AND o.lease_id=s.lease_id"
        " WHERE t.run_id=? AND s.submission_id=? AND s.content_sha256=?"
        " AND o.worker_observation_id=? AND o.host='codex' AND o.observation_source=?"
        " AND o.evidence_level='EXECUTION_OBSERVED' AND o.policy_verdict='MISMATCH'"
        " AND NOT EXISTS (SELECT 1 FROM worker_observations w WHERE w.task_id=t.task_id"
        " AND w.lease_id=s.lease_id AND w.policy_verdict='REFUSAL_OR_FALLBACK')",
        (approval["run_id"], approval["subject"]["id"], approval["subject"]["sha256"],
         approval["request"].get("worker_observation_id"), native_context.OBSERVATION_SOURCE),
    ).fetchone()
    if row is not None:
        efforts = store.loads(row, "required_effort_json", [])
        observed = store.loads(row, "payload_json", {}).get("observed") or {}
        epochs = observed.get("epochs") or [observed]
        if any(epoch.get("kind") == "reroute" for epoch in epochs):
            raise PermissionDenied("a recorded Host reroute cannot be granted as a settings mismatch")
        for epoch in epochs:
            model, effort = epoch.get("model"), epoch.get("effort")
            if model and effort and (not policy.is_model(model, row["required_model"]) or effort not in efforts):
                return {"required_model": row["required_model"], "allowed_efforts": efforts,
                        "observed_model": model, "observed_effort": effort}
    raise PermissionDenied("a grant requires a recorded native model/effort mismatch for this submission")


def decide_in_terminal(conn, project, approval_id, decision, note):
    approval = approvals.get(conn, approval_id)
    run = conn.execute("SELECT * FROM runs WHERE run_id=? AND project_ref=?",
                       (approval["run_id"], project)).fetchone()
    if run is None or native_context.source_of_run(run) != native_context.SOURCE:
        raise ScopeDenied("this approval does not belong to a native run in the selected project")
    kind = approval["action_kind"]
    if (kind, approval["subject"]["kind"]) not in {("worker_refusal", "task"), ("model_switch", "submission")}:
        raise PermissionDenied("the native terminal decides worker holds and submission model holds only")
    detail = {"choice": "skip"} if kind == "worker_refusal" else None
    if detail and decision != approvals.DENIED:
        raise PermissionDenied("a worker refusal can only be skipped here")
    result = {"approval_id": approval_id, "run_id": run["run_id"], "status": approval["status"],
              "run_status": run["status"], "host_started": False}
    if approval["status"] != approvals.PENDING:
        if approval["status"] == decision:
            return {**result, "already_decided": True}
        raise PermissionDenied(f"this approval is already {approval['status']}")
    if machines.is_terminal("run", run["status"]):
        raise PermissionDenied("the approval belongs to a finished run")
    settings = _model_difference(conn, approval) if decision == approvals.GRANTED else None
    shown = {"project": project, "run_id": run["run_id"], "approval_id": approval_id,
             "kind": kind, "subject": approval["subject"], "policy_verdict": approval["request"].get("policy_verdict"),
             "settings": settings, "decision": decision, "note": note, "decision_detail": detail,
             "reasons": [str(item)[:200] for item in (approval["request"].get("reasons") or [])[:4]]}
    if not _confirm_as_person("gx_approval_grant", shown):
        raise PermissionDenied("the local terminal did not confirm this decision; the approval is still pending")
    principal = Principal(pwd.getpwuid(os.geteuid()).pw_name, SCOPE_OPERATOR, (project,),
                          source=approvals.INTERACTIVE_SOURCE)
    ctx = Ctx(conn, principal, tool="gx_approval_grant")
    with store.transaction(conn):
        decided = approvals.decide(ctx, approval_id=approval_id, decision=decision, note=note,
                                   expected_subject_sha256=approval["subject"]["sha256"], decision_detail=detail)
        TeamService(conn, ctx=ctx).settle(run_id=run["run_id"])
    status = conn.execute("SELECT status FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0]
    return {**result, "status": decided["status"], "run_status": status, "already_decided": False}
