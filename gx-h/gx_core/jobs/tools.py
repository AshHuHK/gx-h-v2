"""What the job service publishes: four tools, a validator, and the GPU approval condition.

    gx_job_submit     start a job for a SELECTED or EXECUTING path; returns at once
    gx_job_status     the job as it is now, after looking at its runner
    gx_job_wait       the same, waiting at most sixty seconds for it to end
    gx_job_cancel     ask for it to be ended
    execution_bundle  what an executor reports, held against what ran (:mod:`.bundles`)
    earlier_jobs      a brief section: what was already submitted for an execute_path task
    gpu_job           the facts the standing GPU approval rule rests on, recomputed here

Loaded by ``gx_core.registry.load_providers``.
"""

from __future__ import annotations

from typing import Any, Mapping

import os
from pathlib import Path

from .. import config
from ..capabilities import manifest as manifest_mod
from ..capabilities import probe, registry
from ..context import Ctx
from ..domain import clock, ids
from ..errors import GxError
from ..governance.approvals import ConditionResult, condition
from ..persistence import store
from ..registry import BriefSection, Subject, ToolResult, brief_section, bundle_validator, tool
from . import bundles, leases
from . import service as job_service
from .service import CREATED, PURPOSE_PATH, JobService


def _job_data(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": view["job_id"], "status": view["status"], "terminal": view["terminal"],
        "stale": view["stale"], "scheduler_ref": view["scheduler_ref"],
        "observed_at": view["observed_at"], "exit_code": view["exit_code"],
        "measured": view["measured"], "artifact_refs": view["artifact_refs"],
        "outputs": view["outputs"], "failure": view["failure"],
    }


@tool("gx_job_submit")
def job_submit(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    answer = JobService(ctx.conn, ctx=ctx).submit(
        run_id=args["run_id"], path_id=args["path_id"], task_id=args.get("task_id"),
        capability_version=args["capability_version"],
        input_manifest_ref=args["input_manifest_ref"], parameters=args.get("parameters"),
        tighten=args.get("tighten"), idempotency_key=args["idempotency_key"], purpose=PURPOSE_PATH,
        lease_ref=args.get("lease_ref"), step_index=args.get("step_index"),
    )
    if answer["replayed"]:
        ctx.warn("this step of the path already has a job that is open or succeeded (contract r5: "
                 "one per step and input until it fails or is cancelled); that job is returned and "
                 "nothing was started" if answer.get("step_job") else
                 "this idempotency key was already used for the same job; the existing job is "
                 "returned and nothing was started again")
        _adopt(ctx, answer["job_id"], "gx_job_submit")
    return ToolResult(
        data={"job_id": answer["job_id"], "status": answer["status"],
              "scheduler_ref": answer["scheduler_ref"], "preflight": answer["preflight"]},
        run_id=args["run_id"],
    )


def _adopt(ctx: Ctx, job_id: str, via: str) -> None:
    """A look at a job by the worker that holds its task's live lease takes over a job of an
    earlier lease (SCHEDULING_DESIGN r2.2 §7.2.4). Who holds the lease is read from the store
    for the caller's own principal; the call names no lease."""
    row = ctx.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return
    taken = leases.adopt(ctx.conn, row, principal=ctx.principal.subject, actor=ctx.actor, via=via)
    if taken is not None:
        before = (f"it was submitted under the earlier lease {taken['from_lease_ref']}"
                  if taken["from_lease_ref"] else "no lease was on record for it")
        ctx.warn(f"job {job_id} is adopted under your lease {taken['lease_ref']} of task {taken['task_id']} "
                 f"({before}): it now counts as this attempt's, your lease is held open while it runs, and "
                 "it is listed in your bundle like a job you submitted. Do not submit its step again")


def _warn_about(ctx: Ctx, view: Mapping[str, Any]) -> None:
    if view.get("recovery_manifest_ref"):
        ctx.warn(f"Original unverified files retained for Job {view['job_id']}. "
                 f"Read its diagnostic inventory with gx_artifact_get {view['recovery_manifest_ref']}.")
    if view["stale"]:
        ctx.warn("the runner is alive and has not written a heartbeat recently; the status is "
                 "the last one it reported")
    if view["status"] == job_service.OUTCOME_UNKNOWN:
        ctx.warn("how this job is doing cannot be read, so nothing was decided about it: "
                 f"{view.get('observation') or 'no reason recorded'}. Do not submit it again; ask "
                 "again later, or cancel it to stop waiting")


@tool("gx_job_status")
def job_status(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    view = JobService(ctx.conn, ctx=ctx).status(args["job_id"])
    _warn_about(ctx, view)
    _adopt(ctx, view["job_id"], "gx_job_status")
    return ToolResult(data=_job_data(view), artifact_refs=view["artifact_refs"], run_id=view["run_id"])


@tool("gx_job_wait")
def job_wait(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    view = JobService(ctx.conn, ctx=ctx).wait(args["job_id"], args.get("timeout_seconds"))
    _warn_about(ctx, view)
    _adopt(ctx, view["job_id"], "gx_job_wait")
    data = _job_data(view)
    data["waited_seconds"] = view["waited_seconds"]
    return ToolResult(data=data, artifact_refs=view["artifact_refs"], run_id=view["run_id"])


@tool("gx_job_cancel")
def job_cancel(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    service = JobService(ctx.conn, ctx=ctx)
    answer = service.cancel(args["job_id"], reason=args["reason"],
                            idempotency_key=args["idempotency_key"])
    row = ctx.conn.execute("SELECT run_id FROM jobs WHERE job_id=?", (args["job_id"],)).fetchone()
    return ToolResult(data={"status": answer["status"], "note": answer["note"]},
                      run_id=row["run_id"] if row is not None else None)


@bundle_validator(bundles.BUNDLE_KIND)
def execution_bundle(ctx: Ctx, task: Mapping[str, Any], bundle: Mapping[str, Any], *, commit: bool):
    return bundles.judge(ctx, task, bundle, commit=commit)


#: How many jobs the ``earlier_jobs`` section lists. Every job that has not ended is always
#: listed; the rest of the room goes to the most recent ones that have.
EARLIER_JOBS_LISTED = 30


@brief_section("earlier_jobs", kinds=("execute_path",), order=6)
def earlier_jobs_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    """What was already submitted for an execute_path task, for whoever executes it next
    (SCHEDULING_DESIGN r2.2 §7.2.4). None while no job is on record for the task: a first
    attempt is told nothing. The rows are as recorded when the brief was compiled; a brief is
    compiled inside the claim, where no runner is looked at and nothing is settled."""
    if not task.get("task_id"):
        return None
    listed = leases.earlier_jobs(ctx.conn, task["task_id"])
    if not listed:
        return None
    total = len(listed)
    still_open = [job for job in listed if not job["terminal"]]
    ended = [job for job in listed if job["terminal"]]
    # Every job that has not ended is listed; of those that have, the most recent ones.
    shown_ended = ended[-max(0, EARLIER_JOBS_LISTED - len(still_open)):] if len(still_open) < EARLIER_JOBS_LISTED else []
    left_out = len(ended) - len(shown_ended)
    listed = sorted(still_open + shown_ended, key=lambda job: (job["created_at"], job["job_id"]))
    lines = [
        ("1 job is" if total == 1 else f"{total} jobs are") + " already on record for this task, submitted by "
        "an earlier attempt (or, without naming a task, for its path). Read this before you submit anything.",
        "",
        "- A step that has a job here is not submitted again. To take a job over, ask for its status "
        "(`gx_job_status`) or wait for it (`gx_job_wait`): your first such call adopts it. It then "
        "counts as this attempt's, and your lease is held open while it runs.",
        "- A job that SUCCEEDED has published its outputs. Use them, and list the job in your bundle.",
        "- A job that FAILED or was CANCELLED is listed in your bundle too. Whether its step may be "
        "submitted again depends on why it ended; what the backend observed is given below.",
        "- A job you do not adopt ends at its own time limit at the latest. One that is past its "
        "deadline is cancelled when a lease of this task is next bound.",
        "",
        f"As recorded at {clock.now_iso()}; `gx_job_status` gives the present state.",
    ]
    if left_out:
        lines.append(f"{left_out} older job(s) that had ended are not listed here; all of them are on the "
                     "path's record and belong in your bundle.")
    refs: list[str] = []
    for job in listed:
        lines += ["", f"### {job['job_id']}: {job['status']}",
                  f"- ran: `{job['capability_version']}` with parameters `{ids.canonical_json(job['parameters'])}`",
                  f"- inputs: `{job['input_manifest_ref']}`"]
        lease = job["lease"]
        lines.append("- lease: " + (f"`{lease['lease_ref']}` ({lease['basis']})" if lease["lease_ref"]
                                    else "none on record"))
        if job["terminal"]:
            lines.append(f"- ended at {job['finished_at']}")
        else:
            lines.append(f"- admitted at {job['created_at']}"
                         + (f", runner started at {job['submitted_at']}" if job["submitted_at"] else
                            ", no runner yet")
                         + f"; it has ended by {job['deadline_at']} at the latest"
                         + (" (that time has passed)" if job["overdue"] else ""))
        failure = job["failure"]
        if failure:
            lines.append(f"- what the backend observed: `{failure.get('observed')}` "
                         f"(class `{failure.get('failure_class')}`): {str(failure.get('summary'))[:300]}")
        for output in job["outputs"]:
            lines.append(f"- output `{output['output_name']}`: `{output['artifact_ref']}` ({output['kind']})")
            if output["artifact_ref"]:
                refs.append(output["artifact_ref"])
    return BriefSection(title="Jobs already submitted for this task", body="\n".join(lines), refs=refs)


@condition("environment_change")
def environment_change_is_local(ctx: Ctx, *, subject: Subject, request: Mapping[str, Any],
                                rule: Mapping[str, Any]) -> ConditionResult:
    """Rule ``harness-local-environment``: the environment being created or changed lives
    under this tree's own ``var/`` and is not one of the workspace's shared environments.

    The request names a prefix; where it really is — after following every symbolic link —
    is established here. ``Subject(kind="environment", id=<env_id>, sha256=<sha256 of the
    request>)`` is what the approval is bound to.
    """
    prefix = request.get("prefix")
    if subject.kind != "environment" or not isinstance(prefix, str) or not prefix.strip():
        return ConditionResult(False, reasons=["an environment_change approval is about an "
                                               "environment and names its prefix"])
    real = Path(os.path.realpath(prefix))
    own = Path(os.path.realpath(config.var_dir()))
    shared = [Path(os.path.realpath(Path(root) / "conda")) for root in config.prefixes()]
    facts = {"env_id": subject.id, "prefix": prefix, "resolved_prefix": str(real),
             "harness_var": str(own), "shared_environment_switch":
                 config.switch_enabled("modify_shared_conda_envs")}
    reasons: list[str] = []
    if own != real and own not in real.parents:
        reasons.append(f"{real} is not under this tree's var/ directory ({own})")
    if any(root == real or root in real.parents for root in shared):
        reasons.append(f"{real} is inside a shared conda directory of the workspace; switch "
                       "modify_shared_conda_envs is a prohibition")
    if real == own:
        reasons.append("the prefix is var/ itself, not an environment under it")
    return ConditionResult(not reasons, facts=facts, reasons=reasons)


@condition("gpu_job")
def gpu_job_within_limits(ctx: Ctx, *, subject: Subject, request: Mapping[str, Any],
                          rule: Mapping[str, Any]) -> ConditionResult:
    """Rule ``small-gpu-job``: inside the GPU limits, a fresh passing preflight, and a hold
    on the run's GPU job budget. Every fact is read here, now; ``request`` decides nothing.

    The need is the capability's own: memory and scope from its verified manifest, lowered
    by the job's ``tighten`` and by nothing else.
    """
    conn = ctx.conn
    if subject.kind != "job":
        return ConditionResult(False, reasons=["a gpu_job approval is about a job"])
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (subject.id,)).fetchone()
    if row is None:
        return ConditionResult(False, reasons=[f"the store holds no job {subject.id}"])
    intent = store.loads(row, "submit_intent_json", {}) or {}
    if intent.get("request_sha256") != subject.sha256:
        return ConditionResult(False, reasons=["the approval is not about the request this job holds"])
    if row["status"] != CREATED:
        return ConditionResult(False, reasons=[f"the job is {row['status']}; an approval is for a "
                                               "job that has not started"])
    facts: dict[str, Any] = {"job_id": subject.id, "capability_version": intent.get("capability_version")}
    reasons: list[str] = []
    try:
        view = registry.candidate(conn, intent["capability_version"])
        if view["manifest_sha256"] != intent.get("manifest_sha256"):
            return ConditionResult(False, facts=facts,
                                   reasons=["the capability's content is not what the job was admitted with"])
        resources = manifest_mod.effective_resources(view["manifest"], intent.get("tighten"))
    except GxError as exc:
        return ConditionResult(False, facts=facts, reasons=[exc.message])
    gpu = resources["gpu"]
    if gpu is None:
        return ConditionResult(False, facts=facts, reasons=["the capability declares no GPU need"])

    limits = config.gpu_limits()
    facts.update(need_mib=gpu["memory_mib"], scope=gpu["scope"], wall_seconds=resources["wall_seconds"],
                 limits={key: limits[key] for key in ("max_self_memory_mib", "max_wall_minutes",
                                                      "max_concurrent_gpu_jobs", "min_free_mib")})
    if gpu["memory_mib"] > int(limits["max_self_memory_mib"]):
        reasons.append(f"needs {gpu['memory_mib']} MiB, above gpu.max_self_memory_mib")
    if resources["wall_seconds"] > int(limits["max_wall_minutes"]) * 60:
        reasons.append(f"time limit {resources['wall_seconds']} s is above gpu.max_wall_minutes")
    declared = config.switch_scope("gpu_jobs")
    if declared is not None and gpu["scope"] != declared:
        reasons.append(f"switch gpu_jobs is limited to scope {declared!r}; the capability's is {gpu['scope']!r}")

    # gpu.max_concurrent_gpu_jobs is kept by the device lease: a runner starts its tool only
    # while it holds the lock, so a job admitted now waits behind those in flight.
    active = JobService(conn, ctx=ctx).active_gpu_jobs(excluding=subject.id)
    facts["queued_behind"] = active
    facts["concurrency"] = (f"at most {limits['max_concurrent_gpu_jobs']} job on the device at a time, "
                            "enforced by the lease")
    if int(limits["max_concurrent_gpu_jobs"]) != 1:
        reasons.append("gpu.max_concurrent_gpu_jobs is not 1; the lease admits exactly one job per device")
    if len(active) >= job_service.GPU_QUEUE_MAX:
        reasons.append(f"{len(active)} GPU job(s) are running or queued; the queue holds at most "
                       f"{job_service.GPU_QUEUE_MAX}")
    try:
        reading = probe.gpu_preflight(requested_mib=int(gpu["memory_mib"]), active_gpu_jobs=0)
        probe.assert_preflight_fresh(reading)
        device = reading["selected_device"]
        facts["preflight"] = {"observed_at": reading["observed_at"], "device": device["index"],
                              "free_mib": device["memory_free_mib"],
                              "utilization_pct": device["utilization_pct"], "passed": True}
    except GxError as exc:
        facts["preflight"] = {"passed": False, "rule": exc.details.get("rule")}
        reasons.append(f"preflight: {exc.message}")

    hold = conn.execute(
        "SELECT status, amounts_json FROM budget_reservations WHERE reservation_ref=?",
        (row["reservation_ref"],)).fetchone() if row["reservation_ref"] else None
    held = (store.loads(hold, "amounts_json", {}) or {}) if hold is not None else {}
    facts["budget_hold"] = {"reservation_ref": row["reservation_ref"],
                            "status": hold["status"] if hold is not None else None,
                            "gpu_jobs": held.get("gpu_jobs"), "gpu_minutes": held.get("gpu_minutes")}
    if hold is None or hold["status"] != "HELD" or not held.get("gpu_jobs"):
        reasons.append("the job holds no GPU job of its run's budget")
    return ConditionResult(not reasons, facts=facts, reasons=reasons)
