"""Read the runnable work of a native project from the existing business owners."""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from pathlib import Path

from .domain import clock, machines
from .errors import GxError
from .jobs import executor, service as jobs
from .orchestration import service as runs
from .orchestration.engine import Engine, NOT_A_CHANGE
from .persistence import store
from .scheduling import dispatch, snapshot


def _reconcile_reason(conn, job, due):
    """Only the Job service settles observations. A healthy runner needs no agent."""
    status = job["status"]
    if status == jobs.CREATED:
        return "start deadline" if due else None
    job_id = job["job_id"]
    if Path(job_id).name != job_id or job_id in ("", ".", ".."):
        raise ValueError("invalid Job directory identifier")
    directory = executor.paths(store.data_root(conn) / "jobs", job_id).job_dir
    seen = executor.observe(directory, runner_pid=job["runner_pid"], runner_started=job["runner_started"])
    observed = seen["state"]
    if observed == executor.FINISHED:
        receipt = seen["outcome"]
        return f"runner receipt {receipt.get('result')} exit {receipt.get('exit_code')}"
    if observed == executor.NOT_STARTED:
        if status == jobs.OUTCOME_UNKNOWN:
            return None
        if status == jobs.SUBMITTING and clock.age_seconds(job["observed_at"] or job["created_at"]) < jobs.SUBMIT_GRACE_SECONDS:
            return None
    if observed in (executor.LOST, executor.NOT_STARTED) or (
            observed == executor.UNKNOWN and status != jobs.OUTCOME_UNKNOWN):
        return observed
    if status == jobs.OUTCOME_UNKNOWN and seen.get("runner_alive") is True:
        return f"runner observed {observed}"
    return None


def _only_stage_job_progress(conn, run_id, state, standing, snapshot_jobs, waiting_job_ids):
    """A stage may resume on any changed fact. A runner starting alone needs no Root turn."""
    pending = state["pending_interrupt"]
    payload = pending.get("payload") or {}
    if (pending.get("kind") != "stage" or payload.get("waiting_state") != "WAITING_EXTERNAL_JOB"
            or pending.get("tasks") or pending.get("approval_ref") or payload.get("approval_refs")
            or payload.get("wait_for") or standing.complete or standing.quiescent or standing.stage_needs):
        return False
    refs = set(pending.get("job_refs", ()))
    running = {jobs.SUBMITTED, jobs.QUEUED, jobs.RUNNING}
    if (not refs or not refs.issubset(waiting_job_ids)
            or not refs.issubset(job.job_id for job in snapshot_jobs if job.status in running)):
        return False
    mark = state.get("controller_ran") or {}
    if mark.get("event_id") is None or Engine._limits_sha256() != mark.get("limits_sha256"):
        return False
    # Use the stage owner's cursor and exclusions. A result, a new scientific
    # fact or a change on a different Job still gives the controller its turn.
    events = conn.execute("SELECT kind,subject_id,prev_state,new_state FROM events WHERE run_id=? AND event_id>?",
                          (run_id, mark["event_id"]))
    return all(kind in NOT_A_CHANGE or (kind == "job.transition" and subject in refs
               and before in running | {jobs.SUBMITTING} and after in running)
               for kind, subject, before, after in events)


def _run_work(conn, run, view, child_refs):
    run_id = run["run_id"]
    actions = view["actions"]
    from .orchestration.service import hooks_for_run
    science = hooks_for_run(conn, run_id).mechanical_continuation
    def offer(action, *, mechanical=False):
        if mechanical and science:
            view.setdefault("reactor_runs", set()).add(run_id)
        else:
            actions.append(action)

    if run["status"] != "PAUSED" and any(not row["last_error"] for row in
            store.pending_triggers(conn, run_id, topics=("submission.recorded",))):
        view.setdefault("reactor_runs", set()).add(run_id)
    snap = snapshot.load(conn, run_id, now=clock.now_iso())
    plan = dispatch.plan(snap)
    view["pending_approval_ids"].extend(plan.progress.approvals)
    busy_tasks = set()
    busy_jobs = set()
    finished_by_task = {}
    for job in conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY job_id", (run_id,)):
        if job["status"] in jobs.TERMINAL:
            if job["task_id"] in plan.progress.in_flight:
                finished_by_task.setdefault(job["task_id"], []).append(f"{job['job_id']} {job['status']}")
            continue
        busy_jobs.add(job["job_id"])
        busy_tasks.add(job["task_id"])
        if job["status"] == jobs.CREATED and job["job_id"] not in plan.progress.jobs_due:
            intent = store.loads(job, "submit_intent_json", {})
            if intent.get("request_sha256"):
                approval = jobs.latest_admission_approval(conn, job["job_id"], intent)
                if approval:
                    if approval["status"] == "GRANTED":
                        prefix = f"job:{run_id}:"
                        if job["idempotency_key"].startswith(prefix):
                            key = job["idempotency_key"][len(prefix):]
                            actions.append(f"{run_id} continue original Job {job['job_id']} with gx_job_submit "
                                           f"using its admitted request and original client key {key}")
                        else:
                            view["blockers"].append(f"{job['job_id']} has no recoverable original client key")
                        continue
                    if approval["status"] in ("DENIED", "REVOKED"):
                        actions.append(f"{run_id} gx_job_status original Job {job['job_id']} after {approval['status']}")
                        continue
                    if approval["status"] == "PENDING":
                        continue
        reason = _reconcile_reason(conn, job, job["job_id"] in plan.progress.jobs_due)
        if reason:
            offer(f"{run_id} reconcile original Job {job['job_id']} {job['status']} via gx_job_status ({reason})", mechanical=True)
        else:
            view["waiting_job_ids"].append(job["job_id"])
            if science:
                view["science_job_wait"] = True

    if run["status"] == "PAUSED":
        view["blockers"].append(f"{run_id} paused")
        return
    for task_id in plan.progress.in_flight:
        expiry = snap.by_id[task_id].lease_expires_at
        if task_id not in busy_tasks and expiry:
            view["waiting_task_ids"].append(task_id)
            previous = view["earliest_lease_expiry"]
            view["earliest_lease_expiry"] = min(expiry, previous) if previous else expiry

    def saved_product_note(task_id):
        # A returning native worker may already have published the product on
        # an earlier lease, including a required artifact or a review decision.
        from .teams.recovery import staged_task_product

        if snap.by_id[task_id].attempt:
            for lease in conn.execute(
                    "SELECT lease_id,principal FROM leases WHERE task_id=? AND principal LIKE 'native:%'"
                    " ORDER BY attempt DESC,rowid DESC", (task_id,)):
                saved = staged_task_product(conn, task_id, lease["lease_id"])
                if saved:
                    worker = lease["principal"].removeprefix("native:")
                    return f"; saved task output {saved} from original worker {worker}"
        # Older stores can have a task product without an attributable native lease.
        product = conn.execute(
            "SELECT a.artifact_ref FROM artifacts a JOIN tasks t ON t.task_id=a.task_id"
            " WHERE t.run_id=? AND a.run_id=t.run_id AND t.task_id=? AND a.origin='HOST_PUT'"
            " AND a.kind=t.bundle_kind ORDER BY a.rowid DESC LIMIT 1", (run_id, task_id)).fetchone()
        return f"; saved task output {product[0]}" if product else ""

    def progress_note(task_id):
        note = saved_product_note(task_id)
        partial = conn.execute(
            "SELECT submission_id,accepted_count FROM submissions WHERE task_id=? AND accepted_count>0"
            " ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
        if partial:
            note += f"; retained submission {partial[0]} ({partial[1]} accepted records)"
        return note

    for task_id, finished in finished_by_task.items():
        if task_id not in busy_tasks:
            actions.append(f"{run_id} continue owned task {task_id} {snap.by_id[task_id].status} "
                           f"attempt {snap.by_id[task_id].attempt} with original Job {', '.join(finished)}" + progress_note(task_id))
    for row in plan.rows:
        if row.task_id not in busy_tasks:
            actions.append(f"{run_id} dispatch task {row.task_id} READY attempt {row.attempt} via gx_next_actions"
                           + progress_note(row.task_id))
    if plan.dispatching:
        for task_id in plan.progress.settling:
            task = snap.by_id[task_id]
            if task_id not in busy_tasks:
                actions.append(f"{run_id} settle task {task_id} {task.status} attempt {task.attempt} via its task owner"
                               + progress_note(task_id))
        for task in snap.tasks:
            if task.hold_approval_id and task.hold_approval_id not in plan.progress.approvals:
                row = conn.execute("SELECT status FROM approvals WHERE approval_id=?", (task.hold_approval_id,)).fetchone()
                if row and row[0] in ("GRANTED", "DENIED", "REVOKED"):
                    actions.append(f"{run_id} settle held task {task.task_id} attempt {task.attempt} "
                                   f"after approval {task.hold_approval_id} {row[0]}")

    checkpoint = store.latest_checkpoint(conn, run_id)
    if checkpoint is None:
        return
    state = store.loads(checkpoint, "state_json", {})
    pending = state.get("pending_interrupt") or {}
    payload = pending.get("payload") or {}
    if pending.get("kind") in ("client_work", "external_job"):
        # A live Job or task named by this interrupt keeps the gate open. Do not
        # reload a scientific workflow merely to repeat that fact at each poll.
        live_tasks = set(plan.progress.in_flight)
        open_busy_tasks = {tid for tid in busy_tasks if tid in snap.by_id
                           and snap.by_id[tid].status not in machines.terminal_states("task")}
        if busy_jobs.intersection(pending.get("job_refs", ())) or (live_tasks | open_busy_tasks).intersection(pending.get("tasks", ())):
            return
    registry = runs.default_registry(conn, workflow_id=run["workflow_id"], version=run["workflow_version"])
    workflow = registry.get(run["workflow_id"], str(run["workflow_version"]))
    if workflow.digest() != run["workflow_sha256"]:
        view["blockers"].append(f"{run_id} exact workflow version unavailable")
        return
    engine = Engine(conn, registry)
    idle = engine.idle(run_id)
    coverage = {}
    if workflow.objective_coverage and (idle or payload.get("wait_for") == "child_runs"):
        coverage = workflow.objective_coverage(conn, run_id)
        child_refs.extend(coverage.get("pending_child_run_ids", ()))
        for request in coverage.get("child_requests", ()):
            workflow_id = request.get("workflow_id") or (request.get("workflow") or {}).get("id")
            key = request.get("idempotency_key")
            if workflow_id and key:
                actions.append(f"{run_id} gx_run_start requested child {workflow_id} idempotency_key={key} "
                               "using current objective coverage.child_requests")
    if pending and (run["status"] in machines.waiting_states("run") or run["status"] == "ACTIVE"):
        if dispatch.run_holding_approvals(snap):
            return
        standing = engine.standing(run_id)
        if payload.get("wait_for") == "case_selection" and not standing.complete:
            scope = payload.get("execution_scope", "batch")
            if scope == "project":
                if snap.limits.holds:
                    view["blockers"].append(f"{run_id} case selection held by {', '.join(snap.limits.holds)}")
                    return
                exhausted = engine._budget_exhausted(engine._budget_snapshot(run_id))
                if exhausted:
                    view["blockers"].append(f"{run_id} exhausted its run budget for {', '.join(exhausted)}")
                    return
            view.setdefault("scope_handoffs", []).append({
                "run_id": run_id, "handoff_ref": payload["handoff_ref"],
                "execution_scope": scope, "eligible_stage_ids": payload["eligible_stage_ids"],
                "next_action": pending["reason"]})
        elif payload.get("coordinator_action") and not standing.complete:
            if not snap.limits.holds:
                ref = payload.get("handoff_ref") or run["interrupt_ref"]
                actions.append(f"{run_id} coordinator decision on {ref}: {payload['coordinator_action']}")
        elif (standing.complete if science else standing.resumable) and not _only_stage_job_progress(conn, run_id, state, standing,
                                                                  snap.jobs, view["waiting_job_ids"]):
            runs._require_no_held_submission(conn, run_id)
            runs._require_workers_on_policy(conn, run_id, pending)
            ref = state.get("interrupt_ref") or run["interrupt_ref"]
            offer(f"{run_id} gx_run_resume interrupt {ref} checkpoint {checkpoint['seq']}", mechanical=True)
    elif idle and not snap.limits.holds and not dispatch.run_holding_approvals(snap) and not (
            coverage and engine._open_child_work(conn, run, coverage)):
        offer(f"{run_id} gx_run_control continue idle checkpoint {checkpoint['seq']}", mechanical=True)


def project_objective_key(project_ref: str) -> str:
    """Use the existing run-start business key for work covering the original project question."""
    return f"gx-project-objective:{project_ref}"


def next_project_work(conn: sqlite3.Connection, project_ref: str, run_id: str | None = None, *,
                      include_progress: bool = False, after_root_turn: bool = False) -> dict:
    """Read one consistent snapshot, leaving a caller-owned transaction untouched."""
    if conn.in_transaction:
        return _project_work(conn, project_ref, run_id, include_progress, after_root_turn)
    conn.execute("BEGIN")
    try:
        return _project_work(conn, project_ref, run_id, include_progress, after_root_turn)
    finally:
        conn.rollback()


def _released_probe_budget(conn, run):
    """Find the local smoke receipt for a release Job reused by a closed onboarding."""
    owner = conn.execute(
        "SELECT status FROM runs WHERE run_id=? AND project_ref=? AND workflow_id='onboarding'",
        (run["qualification_owner"], run["project_ref"])).fetchone()
    if not owner or owner["status"] not in machines.terminal_states("run"):
        return False
    version, _, digest = run["objective"].removeprefix("probe ladder of ").rpartition(" at sha256 ")
    # The local ladder records the local manifest digest and materialized input;
    # the released Job can belong to a different store and has its own digest.
    receipt = conn.execute(
        "SELECT p.result,p.job_id,p.detail_json FROM probe_records p JOIN artifacts a"
        " ON a.artifact_ref=json_extract(p.detail_json,'$.input_manifest_ref')"
        " WHERE p.subject_kind='capability' AND p.subject_id=? AND p.rung='L3_SMOKE'"
        " AND a.run_id=? AND a.kind='input_manifest' AND a.published=1"
        " ORDER BY p.created_at DESC,p.rowid DESC LIMIT 1", (version, run["run_id"])).fetchone()
    if receipt is None or receipt["result"] != "PASS" or receipt["job_id"] is not None:
        return False
    detail = store.loads(receipt, "detail_json", {})
    source = detail.get("release_qualification")
    return (detail.get("manifest_sha256") == digest and isinstance(source, dict)
            and source.get("capability_version") == version
            and all(isinstance(source.get(k), str) and source[k]
                    for k in ("project_ref", "run_id", "job_id", "probe_id", "review_id")))


def _settled_probe_budget(conn, run):
    """A standalone ladder budget has no workflow outcome of its own."""
    if (run["status"] != "CREATED" or run["workflow_id"] is not None or run["objective_id"] is not None
            or not run["budget_ref"] or not run["objective"].startswith("probe ladder of ")):
        return False
    run_id = run["run_id"]
    matched = False
    for job in conn.execute("SELECT status, side_effect_status, submit_intent_json FROM jobs WHERE run_id=?", (run_id,)):
        intent = store.loads(job, "submit_intent_json", {})
        if (job["status"] not in jobs.TERMINAL or job["side_effect_status"] in ("UNKNOWN", "PARTIAL")
                or intent.get("purpose") != jobs.PURPOSE_PROBE):
            return False
        # Historical carriers store ladder.probe_run's exact identity as their
        # objective. Cross-implementation probes may also have a reference Job.
        version, digest = intent.get("capability_version"), intent.get("manifest_sha256")
        if version and digest and run["objective"] == f"probe ladder of {version} at sha256 {digest}":
            matched = True
    if not matched and not _released_probe_budget(conn, run):
        return False
    if store.latest_checkpoint(conn, run_id) is not None or conn.execute(
            "SELECT 1 FROM budget_reservations WHERE budget_ref=? AND status='HELD' LIMIT 1",
            (run["budget_ref"],)).fetchone():
        return False
    pending = conn.execute("SELECT 1 FROM approvals WHERE run_id=? AND status='PENDING' LIMIT 1", (run_id,))
    if pending.fetchone():
        return False
    finished = machines.terminal_states("task")
    return all(task[0] in finished for task in conn.execute("SELECT status FROM tasks WHERE run_id=?", (run_id,)))


def _linked_runs(root, children):
    linked = set()
    queue = [root]
    while queue:
        current = queue.pop()
        if current not in linked:
            linked.add(current)
            queue.extend(children.get(current, ()))
    return linked


def _project_runs(conn, project_ref, run_id):
    rows = list(conn.execute("SELECT run_id, project_ref, status, objective, objective_id, budget_ref,"
                             " workflow_id, workflow_version, workflow_sha256, interrupt_ref,"
                             " json_extract(inputs_json,'$.inputs.parent_run_id') AS parent,"
                             " json_extract(inputs_json,'$.inputs.study_run_id') AS study_parent,"
                             " json_extract(inputs_json,'$.qualification_onboarding_run_id') AS qualification_owner"
                             " FROM runs WHERE project_ref=? ORDER BY created_at,run_id", (project_ref,)))
    by_id = {r["run_id"]: r for r in rows}
    settled_probes = {row["run_id"] for row in rows if run_id is None and _settled_probe_budget(conn, row)}
    children = {}
    roots = set(by_id) - settled_probes
    consistent = True
    for row in rows:
        parents = {value for value in (row["parent"], row["study_parent"]) if isinstance(value, str) and value}
        if not parents:
            continue
        if len(parents) != 1 or not parents <= by_id.keys():
            consistent = False
            continue
        parent = parents.pop()
        children.setdefault(parent, []).append(row["run_id"])
        roots.discard(row["run_id"])
    anchor = None
    pending_objective_key = None
    if run_id is None:
        key = project_objective_key(project_ref)
        saved = conn.execute("SELECT json_extract(response_json,'$.run_id') AS run_id FROM idempotency"
                             " WHERE idempotency_key=? AND scope='gx_run_start' AND side_effect_status='APPLIED'"
                             " AND completed_at IS NOT NULL", (store.tool_key("gx_run_start", key),)).fetchone()
        if saved and saved["run_id"] in by_id and by_id[saved["run_id"]]["objective_id"]:
            anchor = saved["run_id"]
        else:
            try:
                registration = json.loads((store.data_root(conn) / "project-driver.json").read_bytes())
            except FileNotFoundError:
                registration = {}
            if registration.get("objective_key") == key:
                pending_objective_key = key
        if not anchor and not pending_objective_key and children and consistent and len(roots) == 1:
            # Preserve topology-based readback for historical projects without a commission key.
            root = next(iter(roots))
            linked = _linked_runs(root, children)
            if len(linked) == len(rows) - len(settled_probes) and by_id[root]["objective_id"]:
                anchor = root
    return by_id, children, settled_probes, anchor, pending_objective_key


def project_progress(conn, project_ref, run_id):
    """Describe durable science rows in the caller's snapshot without polling executors."""
    by_id, children, _, anchor, _ = _project_runs(conn, project_ref, run_id)
    return _progress_counts(conn, run_id, by_id, children, anchor)


def _progress_counts(conn, run_id, by_id, children, anchor):
    selected = by_id
    if run_id is not None:
        # Unlike a parent_run_id embedded in the scientific inputs, a commissioned
        # child is named by the run-start key recorded when its parent first waited.
        # Read the small run transition payloads, including previously settled rounds.
        scope_children = {parent: set(ids) for parent, ids in children.items()}
        if run_id in by_id:
            requests = conn.execute(
                "SELECT DISTINCT e.subject_id, json_extract(i.response_json,'$.run_id') FROM json_each(?) p"
                " CROSS JOIN events e ON e.subject_kind='run' AND e.subject_id=p.value"
                " CROSS JOIN json_each(e.payload_json,'$.payload.child_requests') req"
                " JOIN idempotency i ON i.idempotency_key=? || json_extract(req.value,'$.idempotency_key')"
                " WHERE req.type='object' AND e.kind='run.transition' AND e.new_state='WAITING_CLIENT'"
                " AND json_extract(e.payload_json,'$.payload.wait_for')='child_runs'"
                " AND i.scope='gx_run_start' AND i.side_effect_status='APPLIED' AND i.completed_at IS NOT NULL",
                (json.dumps(list(by_id)), store.tool_key("gx_run_start", "")))
            for parent, child in requests:
                if child in by_id:
                    scope_children.setdefault(parent, set()).add(child)
        linked = _linked_runs(run_id, scope_children) if run_id in by_id else set()
        onboarding = {rid for rid in linked if by_id[rid]["workflow_id"] == "onboarding"}
        # The ladder records its owning onboarding outside normal workflow inputs.
        # Include these standalone Job carriers in the counts for that branch.
        selected = linked | {
            row["run_id"] for row in by_id.values()
            if row["qualification_owner"] in onboarding and row["workflow_id"] is None
            and row["objective_id"] is None and row["budget_ref"] and row["objective"].startswith("probe ladder of ")
        }
        anchor = run_id if linked else None
    scope = (json.dumps(sorted(selected)),)
    where = " WHERE run_id IN (SELECT value FROM json_each(?))"
    jobs_by_status = dict(conn.execute("SELECT status,count(*) FROM jobs" + where + " GROUP BY status", scope))
    tasks_by_status = dict(conn.execute("SELECT status,count(*) FROM tasks" + where + " GROUP BY status", scope))
    terminal = sorted(jobs.TERMINAL)
    placeholders = ",".join("?" for _ in terminal)
    pending = conn.execute("SELECT job_id,run_id,status,capability_id,capability_version FROM jobs" + where
                           + f" AND status NOT IN ({placeholders}) ORDER BY created_at,job_id", (*scope, *terminal))
    return {"anchor_run_id": anchor, "jobs_by_status": jobs_by_status, "tasks_by_status": tasks_by_status,
            "nonterminal_jobs": [dict(row) for row in pending]}


def _project_work(conn, project_ref, run_id, include_progress, after_root_turn):
    by_id, children, settled_probes, anchor, pending_objective_key = _project_runs(conn, project_ref, run_id)
    view = {"status": "EMPTY", "anchor_run_id": anchor, "run_ids": [], "run_statuses": {}, "actions": [], "waiting_job_ids": [],
            "waiting_task_ids": [], "earliest_lease_expiry": None, "pending_approval_ids": [], "blockers": []}
    if include_progress:
        view["progress"] = _progress_counts(conn, run_id, by_id, children, anchor)
    if run_id is not None and run_id not in by_id:
        view.update(status="BLOCKED", blockers=["selected run is absent from this project"])
        return view
    terminal = machines.terminal_states("run")
    anchor = run_id or view["anchor_run_id"]
    halted = anchor is not None and by_id[anchor]["status"] in terminal - {"COMPLETED"}
    todo = deque([run_id] if run_id is not None else by_id)
    while todo:
        current = todo.popleft()
        if current in view["run_statuses"]:
            continue
        row = by_id[current]
        view["run_ids"].append(current)
        view["run_statuses"][current] = row["status"]
        if current in settled_probes:
            continue
        child_refs = list(children.get(current, ()))
        if not halted and row["status"] not in terminal:
            try:
                _run_work(conn, row, view, child_refs)
            except GxError as exc:
                view["blockers"].append(f"{current} {exc.code}: {exc.message}")
        for child in child_refs:
            if child in by_id:
                todo.append(child)
            else:
                view["blockers"].append(f"{current} declares a child absent from its project")
    research = {rid: status for rid, status in view["run_statuses"].items() if rid not in settled_probes}
    outcomes = set(research.values())

    def register_objective(context):
        if pending_objective_key:
            context += " Use the original commission supplied to this Root at project registration."
        view["actions"].append(context + " For the overall objective choose a workflow whose completion evidence "
                               "matches those requirements, using gx_run_start idempotency_key="
                               + project_objective_key(project_ref) + ".")

    if not anchor and by_id and outcomes <= terminal:
        refs = ", ".join(f"{current} ({status})" for current, status in research.items()) or "none"
        register_objective(f"Review the original project question against settled method runs {refs}. "
                           "Keep stopped or cancelled branches closed. Reuse their deliverables and address only "
                           "the original question's remaining requirements.")
    elif pending_objective_key and after_root_turn and not by_id:
        register_objective("The project has no registered scientific run for its question. "
                           "Continue from the saved commission and existing evidence on this Root.")
    if not view["actions"]:
        for handoff in view.get("scope_handoffs", ()):
            if handoff["execution_scope"] == "project":
                ref = handoff["handoff_ref"]
                eligible = json.dumps(handoff["eligible_stage_ids"], ensure_ascii=False)
                view["actions"].append(
                    f"{handoff['run_id']} coordinator decision on {ref} for eligible stages {eligible}. "
                    + handoff["next_action"])
    if view.get("reactor_runs"):
        view["reactor_runs"] = sorted(view["reactor_runs"])
        view["status"] = "READY"
    elif view["actions"]:
        view["status"] = "READY"
    elif halted or outcomes and outcomes <= terminal:
        view["status"] = "TERMINAL"
    elif view["waiting_job_ids"]:
        view["status"] = "WAITING_JOB"
    elif view["waiting_task_ids"]:
        view["status"] = "WAITING_WORKER"
    elif view["pending_approval_ids"]:
        view["status"] = "WAITING_APPROVAL"
    elif view.get("scope_handoffs") and not view["blockers"]:
        if pending_objective_key:
            refs = ", ".join(item["handoff_ref"] for item in view["scope_handoffs"])
            register_objective(f"Review the existing batch handoffs {refs} against the original project question "
                               "and reuse the applicable work.")
            view["status"] = "READY"
        else:
            view["status"] = "SCOPE_HANDOFF"
    elif by_id:
        view["status"] = "BLOCKED"
        if not view["blockers"]:
            view["blockers"].append("No runnable task, checkpoint or new Job observation in scope.")
    return view
