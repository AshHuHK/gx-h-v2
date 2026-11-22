"""What can start now: one read-only look at the machine and at our own jobs.

A scheduler deciding what to run next needs facts it cannot get by submitting: whether
the GPU lease is free, how many threads the cgroup allows and how many our jobs already
hold, whether a hold or a switch stands in the way. Submitting to find out would start
a job; this looks without touching anything.

    snapshot(conn)                       the machine and our jobs, now
    fits(conn, "id@version", snapshot)   would a job of this capability be admitted now,
                                         as far as the machine is concerned

**Read-only, literally.** Nothing here writes a row, settles a job, probes an
environment or starts a tool. Jobs are counted from the ``jobs`` table as recorded and
then each is *looked at* (its runner's files and ``/proc``), so a job recorded RUNNING
whose runner has already written its outcome is reported as finished-but-unsettled
rather than as holding a thread. The one process started is ``nvidia-smi``, for the
reading. The GPU lease is tested with a shared, non-blocking lock that is released at
once.

**A snapshot is an instant.** Every figure carries ``observed_at``. Admission is still
decided by ``JobService.submit``, which reads all of this again; a scheduler that acts
on a snapshot can be refused, and that refusal is the truth.

What it cannot see: another tenant's processes on the same GPU appear only as device
memory in use (``nvidia-smi`` PIDs here are the host's), and CPU contention from other
sessions only as load and cgroup pressure.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .. import config
from ..capabilities import probe, registry, storage
from ..capabilities import manifest as manifest_mod
from ..domain import clock
from ..errors import GxError
from ..persistence import store
from . import executor
from . import limits as limits_mod
from .service import CREATED, GPU_QUEUE_MAX, IN_FLIGHT, start_deadline_seconds

CGROUP = Path("/sys/fs/cgroup")

#: How ``jobs_state`` lists a CPU job that has its row and no runner yet.
ADMITTED = "ADMITTED"


# --------------------------------------------------------------------------- the machine

def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def cpu_state() -> dict[str, Any]:
    """The CPU this process is allowed, from the cgroup, and how pressed it is."""
    state: dict[str, Any] = {"logical_cpus": os.cpu_count(), "cgroup_quota_cpus": None,
                             "source": "cgroup v2 cpu.max"}
    raw = _read(CGROUP / "cpu.max")
    if raw:
        quota, _, period = raw.partition(" ")
        if quota != "max" and period.isdigit() and quota.isdigit() and int(period) > 0:
            state["cgroup_quota_cpus"] = round(int(quota) / int(period), 2)
        state["cgroup_cpu_max"] = raw
    else:
        state["source"] = "no cgroup v2 cpu.max; the logical CPU count is all that is known"
    try:
        state["load_average"] = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        state["load_average"] = None
    pressure = _read(CGROUP / "cpu.pressure")
    if pressure:
        for line in pressure.splitlines():
            kind, _, rest = line.partition(" ")
            fields = dict(item.split("=", 1) for item in rest.split() if "=" in item)
            if kind == "some" and "avg10" in fields:
                state["pressure_some_avg10"] = float(fields["avg10"])
                state["pressure_some_avg60"] = float(fields.get("avg60", "nan"))
    stat = _read(CGROUP / "cpu.stat")
    if stat:
        fields = dict(line.split(None, 1) for line in stat.splitlines() if " " in line)
        state["throttled_periods_total"] = int(fields.get("nr_throttled", 0))
    return state


def memory_state() -> dict[str, Any]:
    limit, current = _read(CGROUP / "memory.max"), _read(CGROUP / "memory.current")
    return {
        "cgroup_limit_mib": int(limit) // 2**20 if limit and limit.isdigit() else None,
        "cgroup_in_use_mib": int(current) // 2**20 if current and current.isdigit() else None,
    }


def lease_state(device: int, lock_path: str | None = None) -> dict[str, Any]:
    """Whether the GPU lease of a device is held, by a test that changes nothing.

    A shared lock is asked for without waiting and dropped at once. It is refused exactly
    when a runner holds the exclusive one. ``lock_path`` is for a test of this function;
    everything else uses the device's own lock file.
    """
    path = lock_path or executor.gpu_lock_path(device)
    state: dict[str, Any] = {"device": device, "lock_path": path, "held": False}
    try:
        handle = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        state["note"] = "no job has ever taken this lease"
        return state
    except OSError as exc:
        state.update(held=None, note=f"the lock file cannot be opened: {exc}")
        return state
    try:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        state["held"] = True
    finally:
        os.close(handle)
    state["queue"] = executor.gpu_queue(path)
    return state


# ------------------------------------------------------------------------------ our jobs

def jobs_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Our jobs that have not ended, as recorded and as they look now. Writes nothing.

    A job holds resources while its runner is starting it, waiting for the GPU lease, or
    running it. It holds CPU threads only while starting or running: a GPU job in the
    lease's queue has not started its tool, and holds its place in the queue and its
    budget, not threads (``queued_for_gpu``).

    A CPU job holds its threads from the moment it is admitted (``admitted_not_started``,
    listed with ``observed`` ADMITTED): its submitter starts it at once, and admission
    counted on those threads being free (audit J8). A job that is admitted and never started
    holds them until it is ended or its start deadline passes, whichever is first: past the
    deadline it can never start (the next look ends it), so a submitter that died before
    starting its job keeps no threads out of the pool (``admitted_past_start_deadline``
    lists such jobs; they hold nothing). A GPU job that is
    admitted waits first, for its approval and then for the device; it is counted under
    ``waiting_for_approval`` only, and holds threads once its tool runs.
    """
    marks = ",".join("?" for _ in IN_FLIGHT)
    rows = conn.execute(
        f"SELECT job_id, run_id, path_id, capability_id, capability_version, status, resources_json,"
        f" runner_pid, runner_started, created_at FROM jobs WHERE status IN ({marks}) ORDER BY created_at",
        IN_FLIGHT).fetchall()
    root = storage.store_dir(conn) / "jobs"
    listed: list[dict[str, Any]] = []
    threads = 0
    gpu_jobs: list[str] = []
    queued: list[str] = []
    unsettled = 0
    unknown = 0
    for row in rows:
        resources = store.loads(row, "resources_json", {}) or {}
        seen = executor.observe(root / row["job_id"], runner_pid=row["runner_pid"],
                                runner_started=row["runner_started"])
        on_cpu = seen["state"] in (executor.STARTING, executor.RUNNING)
        holding = on_cpu or seen["state"] == executor.QUEUED
        if seen["state"] == executor.UNKNOWN:
            # What it says cannot be read. A runner that is alive, or that cannot be named,
            # is counted as holding what it was given: a scheduler must not hand it out twice.
            holding = on_cpu = seen.get("runner_alive") is not False
            unknown += 1
        uses_gpu = resources.get("gpu") is not None
        if on_cpu:
            threads += int(resources.get("cpu_threads") or 0)
        if holding and uses_gpu:
            gpu_jobs.append(row["job_id"])
        if seen["state"] == executor.QUEUED:
            queued.append(row["job_id"])
        if not holding and seen["state"] != executor.UNKNOWN:
            unsettled += 1
        runner_says = seen.get("status") or {}
        entry = {
            "job_id": row["job_id"], "run_id": row["run_id"], "path_id": row["path_id"],
            "capability_version": f"{row['capability_id']}@{row['capability_version']}",
            "recorded_status": row["status"], "observed": seen["state"], "holding_resources": holding,
            "holding_cpu_threads": on_cpu,
            "cpu_threads": resources.get("cpu_threads"), "gpu": uses_gpu,
            "limit_wall_seconds": resources.get("wall_seconds"), "created_at": row["created_at"],
            # When its tool started, if its runner says it is running: what is left of its
            # time limit is what a job queued behind it waits for at most.
            "tool_started_at": runner_says.get("started_at") if seen["state"] == executor.RUNNING else None,
        }
        if seen["state"] == executor.UNKNOWN:
            entry["unknown_because"] = seen.get("reason")
        listed.append(entry)
    waiting = 0
    admitted: list[str] = []
    stale: list[str] = []
    deadline = start_deadline_seconds()
    for row in conn.execute(
            "SELECT job_id, run_id, path_id, capability_id, capability_version, status, resources_json,"
            " created_at FROM jobs WHERE status=? ORDER BY created_at", (CREATED,)).fetchall():
        waiting += 1
        resources = store.loads(row, "resources_json", {}) or {}
        if resources.get("gpu") is not None:
            continue
        if clock.age_seconds(row["created_at"]) > deadline:
            stale.append(row["job_id"])                # it can never start: it holds nothing
            continue
        threads += int(resources.get("cpu_threads") or 0)
        admitted.append(row["job_id"])
        listed.append({
            "job_id": row["job_id"], "run_id": row["run_id"], "path_id": row["path_id"],
            "capability_version": f"{row['capability_id']}@{row['capability_version']}",
            "recorded_status": row["status"], "observed": ADMITTED, "holding_resources": True,
            "holding_cpu_threads": True, "cpu_threads": resources.get("cpu_threads"), "gpu": False,
            "limit_wall_seconds": resources.get("wall_seconds"), "created_at": row["created_at"],
            "tool_started_at": None,
        })
    return {
        "in_flight": sum(1 for job in listed if job["holding_resources"]),
        "finished_not_yet_settled": unsettled, "outcome_unknown": unknown,
        "waiting_for_approval": waiting, "admitted_not_started": admitted,
        "admitted_past_start_deadline": stale,
        "cpu_threads_held": threads, "gpu_jobs_in_flight": gpu_jobs, "queued_for_gpu": queued,
        "jobs": listed,
    }


def cpu_room(conn: sqlite3.Connection, *, limits: Mapping[str, Any] | None = None,
             cpu: Mapping[str, Any] | None = None, jobs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """How many threads this store's jobs may hold together, how many they hold now and what
    is left: ``{allowed, held, free, rule, holders}``. The one count: ``snapshot`` reports it
    (``cpu.threads_free``), ``fits`` judges by it, and admission refuses by it inside the
    transaction that writes a job's row (audit J8). It reads the cgroup, the limits and each
    of our jobs' files, and writes nothing; what has been read already can be handed in.

    ``allowed`` is the smaller of ``cpu.max_pool_workers`` and ``cpu.max_quota_fraction`` of
    the CPUs the cgroup gives this process, and at least 1. ``held`` is ``jobs_state``'s
    ``cpu_threads_held``, and ``holders`` the jobs that hold them.
    """
    document = config.limits() if limits is None else limits
    cpu = cpu_state() if cpu is None else cpu
    jobs = jobs_state(conn) if jobs is None else jobs
    cpu_limits = document["cpu"]
    quota = cpu["cgroup_quota_cpus"] or cpu["logical_cpus"] or 1
    by_fraction = int(quota * float(cpu_limits["max_quota_fraction"]))
    allowed = max(1, min(int(cpu_limits["max_pool_workers"]), by_fraction))
    held = int(jobs["cpu_threads_held"])
    return {
        "allowed": allowed, "held": held, "free": max(0, allowed - held),
        "rule": (f"min(cpu.max_pool_workers={cpu_limits['max_pool_workers']}, "
                 f"cpu.max_quota_fraction={cpu_limits['max_quota_fraction']} x {quota} CPUs)"),
        "holders": [job["job_id"] for job in jobs["jobs"] if job["holding_cpu_threads"]],
    }


# ------------------------------------------------------------------------------ snapshot

def _switch_view(spec: Mapping[str, Any]) -> dict[str, Any]:
    view = {"value": spec.get("value"), "scope": spec.get("scope")}
    if "reason" in spec:
        view["reason"] = spec["reason"]
    return view


def _limits_of(now: Mapping[str, Any]) -> dict[str, Any]:
    """The limits a snapshot recorded, as the document :mod:`.limits` judges by. A snapshot
    taken before it carried ``switches`` falls back to the file for those switches."""
    switches = dict(now.get("switches") or {})
    switches[limits_mod.GPU_SWITCH] = dict(now["gpu"]["switch"])
    return {"cpu": {"max_pool_workers": now["cpu"]["max_threads_per_job"]},
            "gpu": dict(now["gpu"]["limits"]), "switches": switches}


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """The machine and our jobs, now. See the module docstring for what it does not see."""
    limits = config.limits()
    cpu_limits, gpu_limits = limits["cpu"], limits["gpu"]
    cpu = cpu_state()
    jobs = jobs_state(conn)
    room = cpu_room(conn, limits=limits, cpu=cpu, jobs=jobs)
    allowed = room["allowed"]
    cpu.update(
        threads_allowed_for_jobs=allowed, threads_held_by_our_jobs=room["held"], threads_free=room["free"],
        threads_held_by=room["holders"], max_threads_per_job=int(cpu_limits["max_pool_workers"]), rule=room["rule"],
    )

    holds = config.active_holds("jobs")
    switch = limits["switches"]["gpu_jobs"]
    reading = probe.read_gpu_state()
    devices = []
    for device in reading["devices"]:
        devices.append({**device, "lease": lease_state(device["index"])})
    gpu_reasons: list[str] = []
    if holds:
        gpu_reasons.append(f"hold {holds[0].get('id', '?')} is active")
    if switch.get("value") is not True:
        gpu_reasons.append("switch gpu_jobs is off")
    if reading["problems"] or not devices:
        gpu_reasons.append("no usable GPU reading: " + ("; ".join(reading["problems"]) or "no devices"))
    if jobs["gpu_jobs_in_flight"]:
        gpu_reasons.append(f"{len(jobs['gpu_jobs_in_flight'])} of our GPU job(s) in flight; one job is on "
                           "the device at a time")
    best = max(devices, key=lambda device: device["memory_free_mib"]) if devices else None
    if best is not None:
        if best["memory_free_mib"] < int(gpu_limits["min_free_mib"]):
            gpu_reasons.append(f"device {best['index']} has {best['memory_free_mib']} MiB free, below "
                               f"gpu.min_free_mib={gpu_limits['min_free_mib']}")
        if best["lease"]["held"]:
            gpu_reasons.append(f"the lease of device {best['index']} is held (a job of this or another "
                               "store is on the GPU); a job submitted now is admitted and queues behind "
                               f"it and {len(best['lease'].get('queue') or [])} waiting job(s)")
    cpu_reasons = [f"hold {holds[0].get('id', '?')} is active"] if holds else []
    if cpu["threads_free"] < 1:
        cpu_reasons.append(f"our jobs hold {jobs['cpu_threads_held']} of the {allowed} threads jobs may use")

    return {
        "observed_at": clock.now_iso(), "host": os.uname().nodename,
        "holds": holds,
        "cpu": cpu, "memory": memory_state(),
        "switches": {limits_mod.GPU_SWITCH: _switch_view(switch)},
        "gpu": {
            "observed_at": reading["observed_at"], "devices": devices, "problems": reading["problems"],
            "switch": _switch_view(switch),
            "limits": {**{key: gpu_limits[key] for key in ("min_free_mib", "max_self_memory_mib",
                                                           "max_concurrent_gpu_jobs", "max_wall_minutes")},
                       "queue_wait_seconds": limits_mod.gpu_queue_wait_seconds(limits)},
            "our_jobs_in_flight": jobs["gpu_jobs_in_flight"],
            "pid_attribution": "not available: nvidia-smi PIDs on this pod are the host's",
        },
        "jobs": jobs,
        "can_start": {
            "cpu_job": {"ok": not cpu_reasons, "reasons": cpu_reasons, "threads_free": cpu["threads_free"]},
            # ok: the job would start at once. would_queue: it would be admitted and wait for the lease.
            "gpu_job": {"ok": not gpu_reasons, "reasons": gpu_reasons,
                        "would_queue": bool(gpu_reasons) and all(
                            "lease" in reason or "in flight" in reason for reason in gpu_reasons),
                        "device": best["index"] if best is not None else None,
                        "free_mib": best["memory_free_mib"] if best is not None else None},
        },
        "note": "an instant, not a promise: JobService.submit reads all of this again and decides",
    }


#: How long a reason in ``fits`` lasts, for a scheduler that must decide between waiting and
#: branching (failure class ``resource``):
#: ``never`` — the capability's declared need is outside this machine's configured limits; no
#: change to the store or to the machine's load makes it fit, only a person changing the
#: limits or a new version of the capability;
#: ``until_changed`` — the store holds something that can change by a step of the platform
#: (the capability is not ACTIVE yet, its environment was last observed not ready);
#: ``not_now`` — the machine is busy or held: threads, device memory, the lease, a hold.
FIT_KINDS = ("never", "until_changed", "not_now")


def fits(conn: sqlite3.Connection, capability_version: str,
         snapshot_now: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Would a job of this capability be admitted now, as far as the machine is concerned,
    and if not, could it ever be.

    Checks what does not depend on a run: the capability is ACTIVE, its environment is
    ready as last observed on this host (no probe is run), its needs are inside the limits,
    and the machine has room. A run's budget and its path's state are the caller's to know;
    they are not looked at here.

    Each reason is in ``reasons`` (prose, as before) and, by how long it lasts, in one of
    ``never``, ``until_changed`` and ``not_now`` (see ``FIT_KINDS``). ``ever_fits`` is False
    exactly when ``never`` is not empty. When the capability cannot be resolved its
    declared need is unknown here, so ``never`` cannot be judged: ``ever_fits`` is None.

    ``never`` is admission's own predicate (:func:`.limits.limit_findings`) asked of the
    limits recorded in the snapshot, in the words of the refusal admission would give.
    ``would_queue`` is True when the only reasons it cannot start now are the GPU lease and
    this store's jobs ahead of it: a submit now is admitted and waits for the lease, it is
    neither refused nor started at once.
    """
    now = dict(snapshot_now) if snapshot_now is not None else snapshot(conn)
    found: dict[str, list[str]] = {kind: [] for kind in FIT_KINDS}
    answer: dict[str, Any] = {"capability_version": capability_version, "observed_at": now["observed_at"],
                              "can_start": False, "reasons": [], "needs": None, "measured": None,
                              "ever_fits": None, "would_queue": False, **found}

    def note(kind: str, reason: str) -> None:
        if reason not in found[kind]:
            found[kind].append(reason)

    try:
        capability = registry.resolve(conn, capability_version)
    except GxError as exc:
        note("until_changed", f"{exc.code}: {exc.message}")
        answer["reasons"] = list(found["until_changed"])
        return answer
    resources = manifest_mod.effective_resources(capability["manifest"], None)
    answer["needs"] = {key: resources[key] for key in ("cpu_threads", "wall_seconds", "memory_mib", "gpu")}
    answer["measured"] = capability.get("measured")
    if not capability["environment"].get("ready"):
        note("until_changed", "environment " + capability["env_id"] + " is not ready: "
             + ", ".join(capability["environment"].get("reasons") or []))
    # never: admission's own predicate, asked of the limits this snapshot recorded.
    for finding in limits_mod.limit_findings(resources, kind=capability["manifest"]["kind"],
                                             limits=_limits_of(now)):
        note("never", finding.message)
    # not_now: the threads. Admission asks the same of the same count, inside the transaction
    # that writes the row (a need above the per-job cap is a ``never`` above, not this).
    if resources["cpu_threads"] <= now["cpu"]["max_threads_per_job"]:
        busy = limits_mod.cpu_threads_finding(
            resources["cpu_threads"], free=now["cpu"]["threads_free"],
            allowed=now["cpu"]["threads_allowed_for_jobs"], held=now["cpu"]["threads_held_by_our_jobs"])
        if busy is not None:
            note("not_now", busy.message)
    for reason in now["can_start"]["cpu_job"]["reasons"]:
        if reason.startswith("hold "):                      # an operator's hold; the threads are said above
            note("not_now", reason)
    gpu = resources["gpu"]
    queue_reasons: list[str] = []
    if gpu is not None:
        for reason in now["can_start"]["gpu_job"]["reasons"]:
            if reason != "switch gpu_jobs is off":
                note("not_now", reason)
                if "lease" in reason or "in flight" in reason:
                    queue_reasons.append(reason)
        in_flight = len(now["gpu"].get("our_jobs_in_flight") or [])
        if in_flight >= GPU_QUEUE_MAX:
            note("not_now", f"{in_flight} GPU job(s) of this store are running or queued; the queue holds at "
                            f"most {GPU_QUEUE_MAX}, so a submit now is refused")
        # The same question admission asks: could the job get through the queue before its
        # own wait ends, the jobs ahead counted at their own time limits.
        ours = set(now["gpu"].get("our_jobs_in_flight") or [])
        ahead = [{"job_id": job["job_id"], "wall_seconds": job.get("limit_wall_seconds"),
                  "tool_started_at": job.get("tool_started_at")}
                 for job in now["jobs"]["jobs"] if job["job_id"] in ours]
        too_long = limits_mod.gpu_queue_finding(ahead, limits=_limits_of(now), at=now["observed_at"])
        if too_long is not None:
            note("not_now", too_long.message)
        free = now["can_start"]["gpu_job"].get("free_mib")
        if free is not None and gpu["memory_mib"] > free:
            note("not_now", f"needs {gpu['memory_mib']} MiB and the device has {free} MiB free")
    answer.update(found)
    answer["reasons"] = list(dict.fromkeys(found["never"] + found["until_changed"] + found["not_now"]))
    answer["ever_fits"] = not found["never"]
    answer["can_start"] = not answer["reasons"]
    # Would a submit now be admitted and wait for the GPU lease, rather than be refused or start?
    answer["would_queue"] = bool(gpu is not None and found["not_now"] and not found["never"]
                                 and not found["until_changed"]
                                 and all(reason in queue_reasons for reason in found["not_now"]))
    return answer


__all__ = ["ADMITTED", "FIT_KINDS", "cpu_room", "cpu_state", "fits", "jobs_state", "lease_state", "memory_state",
           "snapshot"]
