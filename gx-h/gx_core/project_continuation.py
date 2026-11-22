"""Start or resume a Codex-native GX project from a Markdown task and an optional resource budget."""
import argparse
from contextlib import closing, ExitStack
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from . import budgets, config, contracts, metrics, project_followup as followups, project_messages
from .errors import GxError
from .hostobs import host_policy, native_context
from .hostobs.session_receipts import closure_obstacle, native_active, native_sessions, read_json
from .jobs import executor
from .native_cli import COLLABORATION_MODES
from .native_release import _check_path, native_state, write_native_bytes
from .project_followup import save_settings
from .project_work import next_project_work, project_objective_key, project_progress


WAITING = {"WAITING_JOB", "WAITING_WORKER"}
DEFAULTS = {"max_sessions": 4, "max_wall_seconds": 28800}


def settings(state, project, run_id, max_sessions, max_wall_seconds, collaboration=None, model=None,
             worker_effort_floor=None):
    path = state / "project-driver.json"
    record = read_json(path) if path.exists() else {}
    if record and (record["project"] != project or run_id is not None and record.get("run_id") not in (None, run_id)):
        raise ValueError("this driver already belongs to another project or run")
    if not project.strip() or project == "*":
        raise ValueError("one explicit project is required")
    if "model" in record and record["model"] not in host_policy.PROJECT_MODELS:
        raise ValueError("unknown Codex model in project-driver.json")
    if "worker_effort_floor" in record and record["worker_effort_floor"] not in host_policy.PROJECT_WORKER_EFFORT_FLOORS:
        raise ValueError("unknown worker effort floor in project-driver.json")
    if model is not None and model not in host_policy.PROJECT_MODELS:
        raise ValueError("unsupported native project model")
    if worker_effort_floor is not None and worker_effort_floor not in host_policy.PROJECT_WORKER_EFFORT_FLOORS:
        raise ValueError("unsupported native project worker effort floor")
    if model is not None or worker_effort_floor is not None:
        existing = (path.exists() or (state / "gx.db").exists() or (state / "native-session.json").exists()
                    or any(any(state.glob(pattern)) for pattern in ("launch-*.json", "result-*.json", "events-*.jsonl")))
        if not existing and run_id is not None:
            raise ValueError("the selected run is absent from this new project")
        for key, choice, historical in (("model", model, host_policy.DEFAULT_MODEL),
                                        ("worker_effort_floor", worker_effort_floor, None)):
            if choice is None:
                continue
            previous = record.get(key, historical)
            if existing and choice != previous:
                raise ValueError(f"this project already uses {previous or 'role defaults'} for {key}; "
                                 f"select {choice} in a new state directory")
            if not existing:
                record[key] = choice
    record = {**DEFAULTS, "project": project, "run_id": run_id, **record}
    if run_id is not None:
        record["run_id"] = run_id
    if collaboration is not None:
        record["collaboration"] = collaboration
    if record.get("collaboration", "auto") not in COLLABORATION_MODES:
        raise ValueError("unknown project collaboration mode")
    for name, value in (("max_sessions", max_sessions), ("max_wall_seconds", max_wall_seconds)):
        if value is not None:
            record[name] = value
        if type(record[name]) is not int or record[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    return record


def previous_handoffs(record, launch_id):
    dispatch = record.get("dispatch", {})
    if launch_id and dispatch.get("launch_id") == launch_id:
        return dispatch.get("worker_recovery", [])
    return []


def recoverable_workers(conn, task_ids, root):
    """Read the original native target of each still-held, non-Job task in this snapshot."""
    from .teams.recovery import staged_task_product

    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    workers = []
    for task in conn.execute(
            "SELECT t.task_id, l.lease_id, l.principal, r.host_json FROM tasks t JOIN leases l ON l.lease_id=t.lease_id"
            " JOIN runs r ON r.run_id=t.run_id WHERE t.task_id IN (" + placeholders + ") ORDER BY t.task_id", task_ids):
        principal = task["principal"] or ""
        if native_context.source_of_run(task) == native_context.SOURCE and principal.startswith("native:"):
            host = json.loads(task["host_json"])
            thread = principal.removeprefix("native:")
            if thread and host.get("session_ref") == root and host["policy_selection"].get("native_agent_ref") == root:
                workers.append({"task_id": task["task_id"], "thread": thread, "lease_id": task["lease_id"],
                                "saved_product": staged_task_product(conn, task["task_id"], task["lease_id"])})
    return workers


def read_scope(conn, report, phase, *, include_budget):
    run_id = report["run_id"]
    if phase and not run_id:
        run_id = followups.registered_successor(conn, report["project"], phase)
        report["run_id"] = run_id
    if include_budget:
        budget = budgets.project_budget_state(conn, report["project"])
        if budget is not None:
            report["project_budget"] = budget
    return run_id


def observe(state, record, prompt=None, *, include_progress=False, check_settlement=False, preview_host=None):
    project, run_id = record["project"], record["run_id"]
    report = {key: record[key] for key in ("project", "run_id", "max_sessions", "max_wall_seconds")}
    report["state_dir"] = str(state)
    report["collaboration"] = record.get("collaboration", "auto")
    report["configured_model"] = record.get("model", host_policy.DEFAULT_MODEL)
    if "worker_effort_floor" in record:
        report["worker_effort_floor"] = record["worker_effort_floor"]
    phase = followups.current_phase(record)
    scope = phase or record
    report.update({key: scope[key] for key in ("model_budget", "model_budget_file", "resource_estimation_method",
                                              "lower_explicit_caps") if key in scope})
    if phase:
        run_id = phase.get("run_id")
        report.update(run_id=run_id, follow_up=record["active_follow_up"],
                      predecessor_run_id=phase["predecessor_run_id"], idempotency_key=phase["idempotency_key"],
                      max_sessions=phase["max_sessions"], max_wall_seconds=phase["max_wall_seconds"])
        if phase.get("settlement"):
            return {**report, **phase["settlement"]}
    if native_active(state):
        if include_progress:
            try:
                with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True, timeout=0)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("BEGIN")
                    run_id = read_scope(conn, report, phase, include_budget=True)
                    if run_id or not phase:
                        progress = project_progress(conn, project, run_id)
                        report.update(run_id=run_id or progress["anchor_run_id"], progress=progress)
            except sqlite3.OperationalError:
                # Bootstrap and exclusive locks can temporarily preclude a snapshot.
                pass
        status = "REGISTERING" if phase and not run_id and phase.get("registration_launch_id") else "NATIVE_SESSION_ACTIVE"
        return {**report, "status": status}
    sessions = native_sessions(state, latest_events_only=True)
    last = sessions[-1] if sessions else {}
    descriptor = state / "native-session.json"
    settled_effects = []
    native_session = read_json(descriptor) if descriptor.is_file() else None
    reason = closure_obstacle(sessions, native_session, project,
                              state=state, model=report["configured_model"], settled_effects=settled_effects)
    if settled_effects:
        report["native_closure"] = {"status": last["status"], "root_turn_status": last["root_turn_status"],
                                    "settled_effects": settled_effects}
    report.update(native_launches=len(sessions), last_launch_id=last.get("launch_id"))
    if phase:
        report["phase_launches"] = len(sessions) - phase["baseline_launches"]
    elif reason:
        return {**report, "status": "OWNER_ACTION_REQUIRED", "reason": reason}
    dispatch = record.get("dispatch", {})
    previous_workers = previous_handoffs(record, last.get("launch_id"))
    prior = last.get("project_continuation") or {}
    if prior.get("reason") == "INBOX_UNAVAILABLE":
        report["inbox_unavailable_at"] = last["launch_id"]
    worker_recovery = []
    db = state / "gx.db"
    if db.exists():
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            run_id = read_scope(conn, report, phase, include_budget=include_progress or phase or record.get("budget_limits"))
            if reason:
                return {**report, "status": "OWNER_ACTION_REQUIRED", "reason": reason}
            if phase and not run_id:
                work = {"status": "REGISTER", "actions": [], "run_ids": [], "run_statuses": {}}
            else:
                work = next_project_work(conn, project, run_id, include_progress=include_progress,
                                         after_root_turn=bool(sessions))
            if check_settlement and work["status"] == "TERMINAL":
                outstanding = followups.open_work(conn, project)
                if outstanding:
                    report["outstanding_work"] = outstanding
            if "progress" in work:
                report["progress"] = work.pop("progress")
            if work["status"] in WAITING and work.get("waiting_task_ids") and last.get("root_thread_id"):
                # A completed project handoff has no active native turn. A Job
                # may still be working for a different task in the same project.
                waiting = recoverable_workers(conn, work["waiting_task_ids"], last["root_thread_id"])
                handed_off = last["status"] == "OUTCOME_UNKNOWN" or prior.get("reason") in WAITING
                # A driver killed after acquire has no closing status. Wake only the
                # original worker whose still-current lease the transaction proves.
                interrupted_tasks = set()
                if last["status"] is None:
                    for effect in settled_effects:
                        if "attempt" in effect and conn.execute(
                                "SELECT 1 FROM tasks WHERE task_id=? AND lease_id=? AND attempt=?",
                                (effect["task_id"], effect["lease_ref"], effect["attempt"])).fetchone():
                            interrupted_tasks.add(effect["task_id"])
                # Old driver records kept only a flag for a single recovery.
                if previous_workers is True:
                    worker_recovery = [item for item in waiting if item["saved_product"]
                                       or item["task_id"] in interrupted_tasks]
                else:
                    worker_recovery = [item for item in waiting if item["task_id"] in interrupted_tasks
                                       or (handed_off or item["saved_product"]) and item not in (previous_workers or [])]
    else:
        if reason:
            return {**report, "status": "OWNER_ACTION_REQUIRED", "reason": reason}
        work = {"status": "BLOCKED" if run_id else "EMPTY", "actions": [], "run_ids": [], "run_statuses": {},
                "blockers": ["selected run is absent from this project"] if run_id else []}
    report.update(work=work)
    status = work["status"]
    # An addition belongs to this owned project and its admitted Root. Never
    # select a missing run, register a new phase or clear a transport obstacle
    # on the strength of an inbox entry.
    if (db.is_file() and (last.get("status") == "COMPLETED" or settled_effects) and status != "REGISTER"
            and (run_id is None or run_id in work["run_ids"])):
        report["queued_feedback"] = project_messages.has_queued(state)
    feedback = report.get("queued_feedback", False)
    if status == "TERMINAL" and last.get("status") == "OUTCOME_UNKNOWN" and not settled_effects:
        # The Root may time out while composing its final message after GX committed
        # the final decision. Preserve that timeout without spending another turn.
        if (last.get("failure_type") not in ("AppServerTimeout", "AppServerDisconnected")
                or last.get("pending_requests") != [] or native_session.get("pending_effects") != []
                or project_messages.has_queued(state)):
            return {**report, "status": "OWNER_ACTION_REQUIRED",
                    "reason": "GX is terminal and the native owner has outstanding work to reconcile"}
        report["native_closure"] = {"status": last["status"], "failure_type": last["failure_type"],
                                    "pending_effects": [], "root_turn_status": last.get("root_turn_status")}
    if status == "TERMINAL":
        anchor = run_id or work.get("anchor_run_id")
        if anchor:
            outcome = work["run_statuses"][anchor]
            report.update(outcome_run_id=anchor, run_outcome=outcome)
        if not feedback or check_settlement:
            if anchor:
                return {**report, "status": "RUN_" + outcome}
            return {**report, "status": "SCOPE_SETTLED", "reason": "all recorded runs are terminal; no overall objective was declared"}
    if feedback:
        status = "READY"
    if status == "EMPTY":
        if sessions:
            return {**report, "status": "NO_PROGRESS", "reason": "the native launch left no recorded GX run to continue"}
        if preview_host and "started_at" not in record:
            report.update(preview_host)
        status = "READY" if prompt or (state / "project-prompt.md").is_file() else "NEEDS_PROMPT"
    if status not in WAITING | {"READY", "REGISTER"}:
        return {**report, "status": status}
    if config.active_holds("orchestration") or not config.switch_enabled("headless_host_sessions"):
        return {**report, "status": "HELD", "reason": "the orchestration hold or native switch currently prevents launch"}

    local_work = work.get("reactor_runs") or (work.get("science_job_wait") and status == "WAITING_JOB")
    same_scope = prior.get("run_statuses", work["run_statuses"]) == work["run_statuses"]
    if (not local_work and not feedback and status == "READY" and last.get("status") == "COMPLETED" and prior.get("reason") == "NO_PROGRESS"
            and same_scope and prior.get("actions") == work["actions"]):
        return {**report, "status": "NO_PROGRESS", "reason": "the same actionable GX work remained after its last turn"}

    now = time.time()
    first = datetime.fromisoformat(sessions[0]["started_at"]).timestamp() if sessions else record.get("started_at", now)
    origin = phase["started_at"] if phase else min(first, record.get("started_at", first))
    remaining = int(origin + report["max_wall_seconds"] - now)
    if remaining < 1 or (not local_work and report.get("phase_launches", len(sessions)) >= report["max_sessions"]):
        return {**report, "status": "BUDGET_LIMIT", "reason": "session count or elapsed wall reached its configured bound"}
    wall_limit = native_wall_limit(remaining)
    if status == "REGISTER":
        return {**report, "status": "SUCCESSOR_NOT_REGISTERED" if phase.get("registration_launch_id") else "READY_TO_REGISTER",
                "next_max_wall_seconds": wall_limit}
    if worker_recovery:
        status = "READY"
        report["worker_recovery"] = worker_recovery
    if status in WAITING:
        return {**report, "status": status, "next_check_seconds": min(60, remaining)}

    if (not local_work and not feedback and not worker_recovery and dispatch and dispatch["launch_id"] == last.get("launch_id")
            and dispatch["actions"] == work["actions"]
            and dispatch.get("run_statuses", work["run_statuses"]) == work["run_statuses"]
            and (not prior and last.get("status") == "COMPLETED"
                 or last.get("status") == "OUTCOME_UNKNOWN" and dispatch.get("transport_recovery"))):
        return {**report, "status": "NO_PROGRESS", "reason": "the same GX actions remain after native continuation"}
    return {**report, "status": "READY", "next_max_wall_seconds": wall_limit,
            "transport_recovery": last.get("status") == "OUTCOME_UNKNOWN"}


def continuation_prompt(state, worker_recovery):
    # The native owner supplies the current actions from a fresh snapshot after
    # reconnecting. Repeating the potentially large queue here can make it stale.
    if worker_recovery:
        path = state / "project-worker-resume.md"
        lines = ["Resume the original work on this same Root. For the following still-leased tasks, first check current GX task ownership and original Job state. "
                 "Only for a still-held task without a running Job, use native followup_task on the ORIGINAL worker thread to continue its saved work and submit under its "
                 "existing lease. If the original worker is unavailable, report that and leave its lease intact. "
                 "Do not dispatch a replacement, reassign the task or repeat a Job."]
        for item in worker_recovery:
            line = f"GX task {item['task_id']}, original Codex worker {item['thread']}"
            if item.get("saved_product"):
                line += f". Use its saved work {item['saved_product']} to finish the outstanding submission."
            lines.append(line)
        write_native_bytes(path, "\n".join(lines).encode())
        return path
    path = state / "project-next.md"
    if not path.exists():
        write_native_bytes(path, b"Continue the original project and its existing work on this same Codex thread.")
    return path


def status_view(state, record, prompt=None, *, preview_host=None):
    report = observe(state, record, prompt, include_progress=True, preview_host=preview_host)
    if report["status"] == "SUCCESSOR_NOT_REGISTERED":
        label = followups.current_phase(record)["registration_launch_id"]
        if not (state / f"result-{label}.json").exists():
            # A background driver may be starting gx-native before it acquires .native.lock.
            fd = os.open(state, os.O_DIRECTORY | os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                report["status"] = "REGISTERING"
            finally:
                os.close(fd)
    return report


def checked_host_limits(requested, declared, source):
    for name, value in declared.items():
        if requested.get(name) is not None and requested[name] != value:
            raise ValueError(f"--{name.replace('_', '-')} conflicts with {source}")
    return {**requested, **declared}


def project_status(state, project, run_id, follow_up, prompt, max_sessions, max_wall_seconds, collaboration,
                   preview_host, funding_preview=None):
    record = settings(state, project, None, max_sessions, max_wall_seconds, collaboration)
    if follow_up and run_id is not None and follow_up in record.get("follow_ups", {}):
        record["active_follow_up"] = follow_up
        report = status_view(state, record, prompt)
        if run_id not in (report["predecessor_run_id"], report["run_id"]):
            raise ValueError("--status selected incompatible --run-id and --follow-up")
        return report
    if run_id is not None and not follow_up:
        phase_key = next((k for k, v in record.get("follow_ups", {}).items() if v["run_id"] == run_id), None)
        if not phase_key and record.get("initial_settlement", {}).get("outcome_run_id") == run_id:
            return {**record["initial_settlement"], "run_id": run_id}
        record.update(run_id=run_id, active_follow_up=phase_key)
    elif follow_up:
        if follow_up not in record.get("follow_ups", {}):
            view = status_view(state, record)
            result = {"status": "NOT_STARTED", "project": project, "follow_up": follow_up,
                      "original_project": record.get("initial_settlement", view)}
            if record.get("active_follow_up"):
                result["current_phase"] = view
            return result
        record["active_follow_up"] = follow_up
    if not followups.current_phase(record):
        requested = {"max_sessions": max_sessions, "max_wall_seconds": max_wall_seconds, **preview_host}
        checked_host_limits(requested, record.get("budget_host_limits", {}), "the project's saved Markdown budget")
    report = status_view(state, record, prompt, preview_host=preview_host)
    if (funding_preview and "started_at" not in record and not follow_up and run_id is None
            and not report.get("native_launches") and report.get("work", {}).get("status") == "EMPTY"):
        limits, requested_host = funding_preview
        host = {name: value if value is not None else record[name] for name, value in requested_host.items()}
        report.update(host, proposed_budget={**limits, **host})
        if "next_max_wall_seconds" in report:
            report["next_max_wall_seconds"] = native_wall_limit(host["max_wall_seconds"])
    return report


def native_wall_limit(remaining):
    return min(remaining, config.switch("headless_host_sessions")["native_launch"]["max_wall_seconds"])


def wait_for_project_work(state, record, report):
    """Re-read the work owner on a committed change or its original wall-clock timeout."""
    with ExitStack() as readers:
        connections = {}

        def data_version(path):
            # SQLite versions are comparable only on the same connection. These
            # read-only handles hold no transaction between samples.
            try:
                if path not in connections:
                    if not path.is_file():
                        return None
                    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
                    connections[path] = readers.enter_context(closing(conn))
                return connections[path].execute("PRAGMA data_version").fetchone()[0]
            except sqlite3.Error:
                return None  # An unavailable hint leaves the timed work check in place.

        def changes(outcomes, inbox):
            receipts = []
            for path in outcomes:
                try:
                    stat = path.stat()
                    receipts.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    receipts.append(None)
            return (data_version(state / "gx.db"), data_version(state / project_messages.INBOX_FILE) if inbox else None,
                    receipts)

        while report["status"] in WAITING:
            jobs = report["work"].get("waiting_job_ids", [])
            outcomes = [executor.paths(state / "jobs", job).job_dir / "outcome.json" for job in jobs
                        if job and Path(job).name == job and job not in (".", "..")]
            inbox = report.get("queued_feedback") is False
            before = changes(outcomes, inbox)
            # Arm first, then read the business state. A commit on either side
            # of this read is seen here or in the comparison below.
            report = observe(state, record)
            if (report["status"] not in WAITING or report["work"].get("waiting_job_ids", []) != jobs
                    or (report.get("queued_feedback") is False) != inbox):
                continue
            wait_started = time.monotonic_ns()
            deadline = time.monotonic() + report["next_check_seconds"]
            while (remaining := deadline - time.monotonic()) > 0 and changes(outcomes, inbox) == before:
                time.sleep(min(1.0, remaining))
            metrics.record("project_wait", started=wait_started, run_id=report.get("run_id"),
                           work_status=report["status"], waiting_jobs=len(jobs),
                           wake_reason="deadline" if remaining <= 0 else "observed_change")
    return report


def run_project(state, project, *, run_id=None, prompt=None, budget_file=None, follow_up=None,
                max_sessions=None, max_wall_seconds=None, collaboration=None, model=None, worker_effort_floor=None,
                status_only=False, background=False, inherited_fd=None):
    state, _ = _check_path(Path(state), os.environ)
    if follow_up is not None and not follow_up.strip():
        raise ValueError("--follow-up needs a nonempty key")
    host_limits = {"max_sessions": max_sessions, "max_wall_seconds": max_wall_seconds}
    for name, amount in host_limits.items():
        if amount is not None and (type(amount) is not int or amount < 1):
            raise ValueError(f"{name} must be a positive integer")

    def load():
        record = settings(state, project, None if follow_up else run_id, None, None, collaboration, model,
                          worker_effort_floor)
        if follow_up and run_id is not None:
            phase = record.get("follow_ups", {}).get(follow_up)
            if phase:
                if run_id not in (phase["predecessor_run_id"], phase["run_id"]):
                    raise ValueError("--run-id belongs to another follow-up")
            elif record["run_id"] in (None, run_id):
                record["run_id"] = run_id
            else:
                raise ValueError("--run-id differs from the current project run")
        if not follow_up and not record.get("active_follow_up"):
            selected = checked_host_limits(host_limits, record.get("budget_host_limits", {}), "the project's saved Markdown budget")
            record.update({name: amount for name, amount in selected.items() if amount is not None})
        return record

    if status_only:
        if model is not None:
            raise ValueError("--status reads the saved model; use --model when starting a new project")
        if worker_effort_floor is not None:
            raise ValueError("--status reads the saved worker effort floor")
        if budget_file is not None and prompt is None:
            raise ValueError("--status --budget-file requires the commission's --prompt-file")
        prompt_data = followups.read_prompt(prompt) if prompt is not None else None
        limits, markdown_host, model_budget = followups.commission_budget(prompt, prompt_data, budget_file)
        followups.check_model_budget(model_budget)
        host_limits = checked_host_limits(host_limits, markdown_host, "the Markdown budget")
        proposed = (limits, host_limits) if limits is not None else None
        return project_status(state, project, run_id, follow_up, prompt, max_sessions, max_wall_seconds,
                              collaboration, markdown_host, proposed)
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state, os.O_DIRECTORY | os.O_RDONLY) if inherited_fd is None else inherited_fd

    def finish(report):
        # Enqueue commits before the website notifies the driver. Release its
        # lock before the last inbox read: either the posting process can acquire
        # the lock, or this owner sees the addition which raced with its exit.
        if report.get("queued_feedback") is False:
            fcntl.flock(fd, fcntl.LOCK_UN)
            if project_messages.has_queued(state):
                return run_project(state, project, run_id=run_id, follow_up=follow_up,
                                   max_sessions=max_sessions, max_wall_seconds=max_wall_seconds,
                                   collaboration=collaboration, background=background)
        return report

    try:
        if inherited_fd is not None and (fd < 3 or not os.path.samestat(os.fstat(fd), state.stat())):
            raise ValueError("inherited project lock belongs to a different state directory")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "ALREADY_RUNNING", "state_dir": str(state)}
        prompt_data = followups.read_prompt(prompt) if prompt is not None else None
        limits, markdown_host, model_budget = followups.commission_budget(prompt, prompt_data, budget_file)
        host_limits = checked_host_limits(host_limits, markdown_host, "the Markdown budget")
        record = load()
        original = state / "project-prompt.md"
        key = follow_up or record.get("active_follow_up")
        original_input = prompt_data if not key else None
        if original_input is not None and original.exists() and original_input != original.read_bytes():
            raise ValueError("this project already has a different commission; use --follow-up KEY with the new Markdown")
        followups.check_model_budget(model_budget)
        phase = record.get("follow_ups", {}).get(key)
        new_phase = key is not None and phase is None
        if new_phase:
            original_view = observe(state, record, include_progress=True, check_settlement=True)
            if (not original_view.get("outcome_run_id") or not original_view.get("native_launches")
                    or original_view.get("outstanding_work")):
                return {"status": "ORIGINAL_PROJECT_NOT_SETTLED", "follow_up": key, "original_project": original_view}
            prior = followups.current_phase(record)
            if prior:
                prior["settlement"] = original_view
            else:
                record["initial_settlement"] = original_view
            phase = followups.create_phase(state, record, key, original_view["outcome_run_id"],
                    original_view["native_launches"], prompt_data, limits,
                    {name: amount if amount is not None else DEFAULTS[name] for name, amount in host_limits.items()})
            # Predecessor feedback cannot consume a separately commissioned
            # experiment's allowance. Keep the original rows and their receipts.
            if cursor := project_messages.last_entry(state):
                phase["feedback_after"] = cursor
            if model_budget is not None:
                phase["model_budget"] = model_budget
        elif phase:
            if model_budget is not None and model_budget != phase.get("model_budget"):
                raise ValueError("this follow-up key already has a different model budget")
            followups.verify_reuse(phase, prompt_data, limits, host_limits)
            if key != record["active_follow_up"]:
                return observe(state, {**record, "active_follow_up": key}, include_progress=True)

        report = observe(state, record, prompt)
        if phase and report.get("run_id") and not phase.get("run_id"):
            phase["run_id"] = record["run_id"] = report["run_id"]
            save_settings(state, record)
        if not phase and original_input is not None and not original.is_file() and report.get("native_launches"):
            raise ValueError("this existing project needs --follow-up KEY to accept a new commission")
        if not phase and model_budget is not None:
            if "model_budget" in record and model_budget != record["model_budget"]:
                raise ValueError("this project already has a different initial model budget")
            if "model_budget" not in record and ("started_at" in record or report.get("native_launches")
                    or report.get("work", {}).get("status") != "EMPTY"):
                raise ValueError("a new model budget belongs to a fresh project or a new --follow-up")
        if not phase and limits is not None:
            if "budget_limits" in record and record["budget_limits"] != limits:
                raise ValueError("this project already has a different initial budget")
            if "budget_limits" not in record and (report.get("native_launches") or
                    report.get("work", {}).get("status") != "EMPTY"):
                raise ValueError("use --budget-file on a fresh project or a new --follow-up")
        if (model is not None or worker_effort_floor is not None) and not (state / "project-driver.json").exists():
            # Serialize initial selection with an independent gx-native launch in this store.
            with native_state(state):
                selected = load()
                if "model" in selected or "worker_effort_floor" in selected:
                    save_settings(state, selected)
        registering = report["status"] in ("READY_TO_REGISTER", "SUCCESSOR_NOT_REGISTERED")
        if not registering and report["status"] not in WAITING | {"READY"}:
            work = report.get("work")
            if (not new_phase and collaboration is not None and work
                    and (record["run_id"] is None or record["run_id"] in work["run_ids"])):
                save_settings(state, record)
            return finish(report)
        if original_input is not None and not report["native_launches"] and not original.is_file():
            write_native_bytes(original, original_input)
        if (not phase and record["run_id"] is None and not report["native_launches"]
                and report["work"]["status"] == "EMPTY" and original.is_file()):
            record["objective_key"] = project_objective_key(project)
        if not phase and limits is not None:
            record["budget_limits"] = limits
            if markdown_host:
                record.setdefault("budget_host_limits", {}).update(markdown_host)
        if not phase and model_budget is not None:
            record["model_budget"] = model_budget
        scope = phase or record
        model_terms = scope if "model_budget" in scope else None
        if model_terms:
            followups.save_model_budget(state, record, phase, report.get("project_budget"))
            report.update(model_budget=scope["model_budget"], model_budget_file=scope["model_budget_file"])
        record.setdefault("started_at", time.time())
        budget_limits = phase["budget_limits"] if phase else record.get("budget_limits")
        phase_id = phase["idempotency_key"] if phase else "initial"
        budget = followups.publish_commission(state, record, budget_limits, phase_id, report.get("project_budget"))
        if budget is not None:
            report["project_budget"] = budget
        if background:
            log = state / "project-background.log"
            with log.open("ab") as out:
                child = subprocess.Popen([str(contracts.HARNESS_ROOT / "bin/gx-project"), "--state-dir", str(state),
                        "--project", project, "--inherited-lock-fd", str(fd)], stdin=subprocess.DEVNULL,
                        stdout=out, stderr=out, start_new_session=True, pass_fds=(fd,))
            return {**report, "status": "BACKGROUND_STARTED", "pid": child.pid, "log": str(log)}
        if registering:
            source = followups.launch_brief(Path(phase["prompt_file"]), report.get("project_budget"), phase=phase,
                                            state=state, project=project,
                                            host={name: phase[name] for name in host_limits}, model_terms=model_terms)
            label = "register-" + uuid.uuid4().hex
            phase["registration_launch_id"] = label
            save_settings(state, record)
            cmd = [str(contracts.HARNESS_ROOT / "bin/gx-native"), "--state-dir", str(state), "--project", project,
                   "--launch-id", label, "--prompt-file", str(source),
                   "--max-wall-seconds", str(report["next_max_wall_seconds"])]
            code = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, start_new_session=True, pass_fds=(fd,)).wait()
            report = observe(state, record, include_progress=True)
            if report.get("run_id"):
                phase["run_id"] = record["run_id"] = report["run_id"]
                save_settings(state, record)
            if not phase["run_id"] or not (state / f"result-{label}.json").is_file():
                if not phase["run_id"]:
                    report["registration_status"] = "SUCCESSOR_NOT_REGISTERED"
                if report["status"] in ("READY_TO_REGISTER", "SUCCESSOR_NOT_REGISTERED"):
                    report["status"] = "SUCCESSOR_NOT_REGISTERED"
                if phase["run_id"] and report["status"] in WAITING | {"READY"}:
                    report["status"] = "NATIVE_ENTRY_STOPPED"
                return {**report, "entry_exit_code": code, "launch_id": label}
        last_attempt = None
        while report["status"] in WAITING | {"READY"}:
            if report["work"].get("reactor_runs"):
                # Consume a durable delivery or reconcile a completed local Job before
                # deciding whether a worker needs a model turn.
                from .orchestration import reactor
                from .persistence import store
                from .teams.service import backend_ctx
                progressed = False
                with native_state(state) as owned, closing(store.open_store(owned.db_path)) as conn:
                    for rid in report["work"]["reactor_runs"]:
                        ctx = backend_ctx(conn)
                        effects = reactor.lazy(ctx, rid)
                        advanced = reactor.drive_science(ctx, rid)
                        progressed |= bool(advanced or effects["reconciled"] or
                                           any(r["outcome"] == "reacted" for r in effects["reactions"]))
                refreshed = observe(state, record)
                if (not progressed and refreshed.get("work") == report.get("work") and
                        refreshed.get("work", {}).get("reactor_runs")):
                    return {**refreshed, "status": "NO_PROGRESS",
                            "reason": "GX could not settle the recorded work. Inspect pending reactions and Job receipts."}
                report = refreshed
                continue
            if report["status"] in WAITING:
                report = wait_for_project_work(state, record, report)
            else:
                work = report["work"]
                if not report["native_launches"] and original.is_file():
                    source = followups.launch_brief(original, report.get("project_budget"),
                                                    state=state, project=project,
                                                    host={name: record[name] for name in host_limits}, model_terms=model_terms)
                else:
                    source = continuation_prompt(state, report.get("worker_recovery"))
                    if model_terms:
                        source = followups.launch_brief(source, report.get("project_budget"), continuation=True,
                                                        state=state, project=project,
                                                        host={name: scope[name] for name in host_limits}, model_terms=model_terms)
                label = "project-" + uuid.uuid4().hex
                remembered = previous_handoffs(record, report["last_launch_id"])
                waiting_ids = set(work.get("waiting_task_ids", []))
                handoffs = {}
                if isinstance(remembered, list):
                    handoffs.update((item["task_id"], item) for item in remembered if item["task_id"] in waiting_ids)
                handoffs.update((item["task_id"], item) for item in report.get("worker_recovery", []))
                record["dispatch"] = {"launch_id": label, "actions": work["actions"], "run_statuses": work["run_statuses"],
                                      "transport_recovery": report["transport_recovery"],
                                      "worker_recovery": list(handoffs.values())}
                save_settings(state, record)
                cmd = [str(contracts.HARNESS_ROOT / "bin/gx-native"), "--continue-project", "--state-dir", str(state),
                       "--project", project, "--launch-id", label, "--prompt-file", str(source),
                       "--max-wall-seconds", str(report["next_max_wall_seconds"])]
                if "collaboration" in record:
                    cmd.extend(["--collaboration", record["collaboration"]])
                if record["run_id"] is not None:
                    cmd.extend(["--run-id", record["run_id"]])
                print(json.dumps({"launch_id": label, "project": project, "work": work["status"]}), file=sys.stderr, flush=True)
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, start_new_session=True, pass_fds=(fd,))
                code = proc.wait()
                last_attempt = label
                if code not in (0, 1) or not (state / f"result-{label}.json").is_file():
                    return {**report, "status": "NATIVE_ENTRY_STOPPED", "entry_exit_code": code, "launch_id": label}
                report = observe(state, record)
            if last_attempt and report.get("inbox_unavailable_at") == last_attempt:
                return {**report, "status": "INBOX_UNAVAILABLE", "reason": "the original Root could not read its queued additions"}
        return finish(report)
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", help="optional existing GX run and its linked children")
    parser.add_argument("--model", choices=host_policy.PROJECT_MODELS,
                        help="Codex model for a new project; continuation reuses its saved selection")
    parser.add_argument("--worker-effort-floor", choices=host_policy.PROJECT_WORKER_EFFORT_FLOORS,
                        help="minimum reasoning effort across native worker roles in a new project")
    parser.add_argument("--collaboration", choices=COLLABORATION_MODES,
                        help="save a collaboration choice for future project work (default: auto)")
    parser.add_argument("--prompt-file", type=Path, help="Markdown commission with optional ## GX 预算 and ## 模型预算 sections")
    parser.add_argument("--budget-file", type=Path, help="separate Markdown or JSON project-wide resource budget for this phase")
    parser.add_argument("--follow-up", help="stable key for a new commission on the same settled project and native Root")
    parser.add_argument("--max-sessions", type=int, help="native launch allowance for the project or new follow-up phase")
    parser.add_argument("--max-wall-seconds", type=int, help="wall budget including time spent waiting and offline")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", dest="status_only", help="observe without launching Codex")
    mode.add_argument("--background", action="store_true", help="run the bounded project after terminal exit")
    parser.add_argument("--inherited-lock-fd", type=int, dest="inherited_fd", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        report = run_project(args.state_dir, args.project, run_id=args.run_id, prompt=args.prompt_file,
                             budget_file=args.budget_file, follow_up=args.follow_up,
                             collaboration=args.collaboration, model=args.model, worker_effort_floor=args.worker_effort_floor,
                             max_sessions=args.max_sessions, max_wall_seconds=args.max_wall_seconds,
                             status_only=args.status_only, background=args.background, inherited_fd=args.inherited_fd)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, GxError) as exc:
        report = {"status": "INSPECTION_FAILED", "reason": str(exc), "project": args.project}
    except KeyboardInterrupt:
        report = {"status": "LAUNCHER_INTERRUPTED", "project": args.project,
                  "reason": "An active native session retains its own bounded lifetime and original store."}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] in {"BACKGROUND_STARTED", "RUN_COMPLETED", "SCOPE_HANDOFF"} or args.status_only and report["status"] != "INSPECTION_FAILED" else 1
