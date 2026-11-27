"""Expose one selected project's CLI, inbox and recorded transitions to a browser."""

import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from .. import contracts, project_messages
from ..hostobs.session_receipts import native_active
from ..project_continuation import run_project
from ..project_work import next_project_work


class Conflict(ValueError):
    """The request conflicts with the current project or an earlier message."""


def read_db(state):
    conn = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def launch_project(state, project, *, prompt=None, settings=None):
    argv = [str(contracts.HARNESS_ROOT / "bin" / "gx-project"),
            "--project", project, "--state-dir", str(state), "--background"]
    if prompt is not None:
        argv.extend(["--prompt-file", str(prompt)])
    for name, value in (settings or {}).items():
        argv.extend(["--" + name.replace("_", "-"), str(value)])
    try:
        proc = subprocess.run(argv, cwd=contracts.HARNESS_ROOT, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out waiting for the gx-project startup receipt") from exc
    if proc.returncode not in (0, 1) or not proc.stdout:
        raise RuntimeError("gx-project did not return a startup receipt")
    receipt = json.loads(proc.stdout)
    if receipt.get("status") == "INSPECTION_FAILED":
        raise ValueError(receipt.get("reason") or "GX could not start the project")
    return receipt


def snapshot(state, project):
    report = run_project(state, project, status_only=True)
    view = {k: report[k] for k in ("project", "status", "reason", "run_id", "outcome_run_id",
                                     "run_outcome", "progress", "configured_model", "collaboration",
                                     "max_sessions", "max_wall_seconds") if k in report}
    view["feedback"] = project_messages.deliveries(state)
    if (report.get("work") or {}).get("status") == "TERMINAL":
        view["scope_settled"] = True
    label = report.get("last_launch_id")
    if label and Path(label).name == label:
        result = state / f"result-{label}.json"
        if result.is_file():
            receipt = json.loads(result.read_bytes())
            final = receipt.get("root_final_output") or {}
            if (final.get("capture_status") == "CAPTURED" and final.get("thread_id")
                    and final["thread_id"] == receipt.get("root_thread_id")):
                view["latest_root_reply"] = final.get("text")
    return view


def settled_for_feedback(state, project, view):
    if view.get("scope_settled") or view["status"].startswith("RUN_") or view["status"] == "SCOPE_SETTLED":
        return True
    if view["status"] != "NATIVE_SESSION_ACTIVE" or not (state / "gx.db").is_file():
        return False
    settings = state / "project-driver.json"
    run_id = json.loads(settings.read_bytes()).get("run_id") if settings.is_file() else None
    with closing(read_db(state)) as conn:
        return next_project_work(conn, project, run_id, after_root_turn=True)["status"] == "TERMINAL"


def feedback(state, project, ident, text):
    if not isinstance(ident, str) or not 1 <= len(ident) <= 128:
        raise ValueError("A stable client_message_id (1–128 characters) is required")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 128 * 1024:
        raise ValueError("Feedback must be nonempty and at most 128 KiB")
    existing = project_messages.delivery_status(state, ident)
    if existing is None:
        if not ((state / "project-driver.json").is_file() or (state / "native-session.json").is_file()):
            raise Conflict("Start this project before sending feedback")
        if settled_for_feedback(state, project, snapshot(state, project)):
            raise Conflict("The project is settled")
        project_messages.enqueue(state, ident, text)
    with closing(project_messages.inbox(state)) as db:
        body, phase = db.execute("SELECT body,phase FROM messages WHERE id=?", (ident,)).fetchone()
    if body != text:
        raise Conflict("This client_message_id was used for different feedback")
    result = {"message_id": ident, "delivery": phase}
    if phase == "queued" and (state / "project-driver.json").is_file() and not native_active(state):
        observed = run_project(state, project, status_only=True)
        if observed["status"] == "READY" and observed.get("queued_feedback"):
            result["project_status"] = launch_project(state, project)["status"]
    return result


def milestones(state, project, after):
    if not (state / "gx.db").is_file():
        return []
    with closing(read_db(state)) as conn:
        rows = conn.execute(
            """SELECT e.event_id AS id,e.ts AS at,e.subject_kind AS entity,
                      e.subject_id AS subject,e.new_state AS status
               FROM events e JOIN runs r ON r.run_id=e.run_id
               WHERE r.project_ref=? AND e.event_id>?
                 AND e.new_state IS NOT NULL AND (e.prev_state IS NULL OR e.prev_state<>e.new_state)
                 AND (e.subject_kind IN ('run','job') OR (e.subject_kind='task'
                      AND e.new_state IN ('RUNNING','COMPLETED','ACCEPTED','REJECTED','FAILED')))
               ORDER BY e.event_id LIMIT 200""",
            (project, after),
        )
        return [dict(row) for row in rows]
