"""What the machine's limits allow a job to need at all: one predicate for every reader.

Job admission refuses a job whose declared needs are outside ``config/limits.json``. The
same question is asked earlier by planning (a step a job would refuse is refused at
acceptance), by selection, by :mod:`.applicability`, and by ``capacity.fits`` (its
``never`` reasons). If each kept its own copy, one could admit what another refuses; so
they all ask this module.

    limit_findings(resources, kind=None, limits=None) -> [GxError]
        every refusal, as the error admission raises for it, in admission's order
    limit_problems(resources, kind=None, limits=None) -> [str]
        the same, as messages
    switch_findings(resources, kind=None, limits=None) -> [GxError]
        the switches among them alone: what admission asks first of all, before anything
        about the capability's review state, because a switch is the operator's decision
        about the whole machine

Neither raises a refusal: every refusal is returned. (A ``resources`` or a ``limits``
document that lacks a field is a programming error and raises as one.)

``resources`` is ``manifest.effective_resources(manifest, tighten)``: ``cpu_threads``,
``wall_seconds``, ``gpu`` (None, or ``{memory_mib, scope}``). The optional ``kind``
field lets callers use the same signature for all capability types.
``limits`` is a document shaped like ``config.limits()`` (a scheduler's
snapshot, say); None reads the file now. Nothing here looks at the machine's load, a run's
budget or a path's state: a need outside the limits is outside them however idle the
machine is, which is what makes it a ``never``.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .. import config
from ..domain import clock
from ..errors import ApprovalRequired, GxError, ResourceDenied

GPU_SWITCH = "gpu_jobs"

#: Seconds a GPU job may wait for the device's lease before its runner gives up and the job
#: ends FAILED not_started; and, the same bound, how long an admitted job may stay without a
#: runner. This is the figure while ``config/limits.json`` has no line for it; the line
#: ``gpu.queue_wait_seconds`` arrives with contract r5, so that a test can lower it in a
#: relocated copy (SCHEDULING_DESIGN r2.2 §7.1).
DEFAULT_GPU_QUEUE_WAIT_SECONDS = 1800


def gpu_queue_wait_seconds(limits: Mapping[str, Any] | None = None) -> int:
    """How long a GPU job may wait for the device's lease: ``gpu.queue_wait_seconds`` of the
    limits (read now, or of the document given), ``DEFAULT_GPU_QUEUE_WAIT_SECONDS`` when the
    limits have no such line. A line that is not a positive whole number is an error of the
    file, raised as one: a wait nobody can read is not a wait of zero."""
    document = config.limits() if limits is None else limits
    value = (document.get("gpu") or {}).get("queue_wait_seconds", DEFAULT_GPU_QUEUE_WAIT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"gpu.queue_wait_seconds must be a positive whole number of seconds, got {value!r}")
    return value


def gpu_queue_seconds_ahead(ahead: Iterable[Mapping[str, Any]], *, at: str | None = None) -> int:
    """How long the GPU jobs ahead of a new one may still hold the device, at the latest: for
    each its own time limit, less what one whose tool is already running has used.

    ``ahead`` is ``[{"wall_seconds": int, "tool_started_at": iso | None}]``: a job that has
    not started its tool counts for its whole limit. ``at`` is the instant it is judged at
    (ISO text), the clock's now when None. An upper bound: each job may end sooner, none may
    run longer, because its runner ends it at its limit.
    """
    now = clock.parse(at) if at is not None else clock.now()
    total = 0.0
    for job in ahead:
        wall = float(job.get("wall_seconds") or 0)
        started = job.get("tool_started_at")
        used = max(0.0, (now - clock.parse(started)).total_seconds()) if started else 0.0
        total += max(0.0, wall - used)
    return int(math.ceil(total))


def gpu_queue_finding(ahead: Iterable[Mapping[str, Any]], *, limits: Mapping[str, Any] | None = None,
                      at: str | None = None) -> GxError | None:
    """The refusal for a GPU job that could not reach the device before its queue wait ends,
    or None. Admission and ``capacity.fits`` both ask this, so they cannot disagree.

    A job is admitted to the queue only when the jobs of this store ahead of it can all be
    done, at their own time limits, within ``gpu.queue_wait_seconds``. Admitted behind more
    than that it would hold its budget for the whole wait and then end not started (audit,
    jobs skeptic: four queued jobs with 20-minute limits against a 30-minute wait). Jobs of
    other stores in the same queue are not counted: their limits are not known here.
    """
    listed = [dict(job) for job in ahead]
    seconds = gpu_queue_seconds_ahead(listed, at=at)
    wait = gpu_queue_wait_seconds(limits)
    if seconds <= wait:
        return None
    return ResourceDenied(
        f"the {len(listed)} GPU job(s) of this store ahead of this one may hold the device for up to "
        f"{seconds} s more (their own time limits), and a job may wait for the device at most {wait} s "
        "(gpu.queue_wait_seconds): admitted now it could end not started after holding its budget for the "
        "whole wait. Submit it again when a job ahead has ended",
        details={"rule": "gpu_queue_wait", "ahead_seconds": seconds, "queue_wait_seconds": wait,
                 "ahead": [{"job_id": job.get("job_id"), "wall_seconds": job.get("wall_seconds"),
                            "tool_started_at": job.get("tool_started_at")} for job in listed]},
    )


def cpu_threads_finding(needed: int, *, free: int, allowed: int, held: int,
                        holders: Iterable[str] = ()) -> GxError | None:
    """The refusal for a job that needs more threads than are free now, or None. Admission
    (inside the transaction that writes the job's row) and ``capacity.fits`` both ask this of
    one count (``capacity.cpu_room``), so that what fits says and what submit does are one
    predicate (audit J8: with the threads held, fits said a job could not start and submit
    started it).

    ``allowed`` is how many threads this store's jobs may hold together, ``held`` how many
    they hold now, ``free`` what is left, ``holders`` the jobs that hold them. Not a
    ``never``: the threads come back when a job ends. A scheduler's WAIT answers it, and
    ``resource_waits_per_path`` bounds how often.
    """
    needed, free, allowed, held = int(needed), int(free), int(allowed), int(held)
    if needed <= free:
        return None
    return ResourceDenied(
        f"needs {needed} threads and {free} are free: this store's jobs hold {held} of the {allowed} threads "
        "jobs may use (cpu.max_pool_workers, cpu.max_quota_fraction). Submit it again when one of them has ended",
        details={"rule": "cpu.threads_free", "needed": needed, "free": free, "allowed": allowed, "held": held,
                 "holders": list(holders)},
    )


def switch_finding(document: Mapping[str, Any], name: str, scope: str | None) -> GxError | None:
    """``config.require_switch`` asked of a limits document, without raising: the switch is
    on, and its declared scope covers ``scope``. The same words and details as that
    function's refusal; a value that is not a real boolean is a switch that is off."""
    spec = (document.get("switches") or {}).get(name)
    if spec is None:
        return ApprovalRequired(f"switch {name!r} is off: no such switch in the limits",
                                details={"switch": name})
    value = spec.get("value")
    if value is not True:
        why = (spec.get("reason", "no reason recorded") if isinstance(value, bool)
               else f"its value {value!r} is not a real boolean")
        return ApprovalRequired(f"switch {name!r} is off: {why}", details={"switch": name})
    declared = spec.get("scope")
    if declared is not None and scope is not None and scope != declared:
        return ApprovalRequired(
            f"switch {name!r} is limited to scope {declared!r}, requested {scope!r}",
            details={"switch": name, "declared_scope": declared, "requested_scope": scope},
        )
    return None


def switch_findings(resources: Mapping[str, Any], *, kind: str | None = None,
                    limits: Mapping[str, Any] | None = None) -> list[GxError]:
    """The configured GPU switch and its optional scope for a job that requests a GPU.

    Admission asks this before it says anything about the capability's review state. A
    switch is the operator's decision about the whole machine: it holds whatever state an
    asset is in, and a caller who can take nothing through a review is told about it too.
    """
    document = config.limits() if limits is None else limits
    found: list[GxError] = []
    gpu = resources.get("gpu")
    if gpu is not None:
        refused = switch_finding(document, GPU_SWITCH, gpu.get("scope"))
        if refused is not None:
            found.append(refused)
    return found


def limit_findings(resources: Mapping[str, Any], *, kind: str | None = None,
                   limits: Mapping[str, Any] | None = None) -> list[GxError]:
    """Check the per-job thread cap and, when requested, the GPU switch, memory and wall time."""
    document = config.limits() if limits is None else limits
    found: list[GxError] = []
    max_threads = int(document["cpu"]["max_pool_workers"])
    if int(resources["cpu_threads"]) > max_threads:
        found.append(ResourceDenied(
            f"the capability needs {resources['cpu_threads']} threads; cpu.max_pool_workers "
            f"is {max_threads}",
            details={"rule": "cpu.max_pool_workers", "needed": resources["cpu_threads"],
                     "limit": max_threads},
        ))
    found.extend(switch_findings(resources, kind=kind, limits=document))
    gpu = resources.get("gpu")
    if gpu is None:
        return found
    gpu_limits = document["gpu"]
    if int(gpu["memory_mib"]) > int(gpu_limits["max_self_memory_mib"]):
        found.append(ResourceDenied(
            f"the capability needs {gpu['memory_mib']} MiB of GPU memory; "
            f"gpu.max_self_memory_mib is {gpu_limits['max_self_memory_mib']}",
            details={"rule": "gpu.max_self_memory_mib", "needed": gpu["memory_mib"],
                     "limit": gpu_limits["max_self_memory_mib"]},
        ))
    max_wall = int(gpu_limits["max_wall_minutes"]) * 60
    if int(resources["wall_seconds"]) > max_wall:
        found.append(ResourceDenied(
            f"the capability's time limit is {resources['wall_seconds']} s; "
            f"gpu.max_wall_minutes is {gpu_limits['max_wall_minutes']}. Lower it with tighten "
            "if the job is known to be shorter",
            details={"rule": "gpu.max_wall_minutes", "needed_seconds": resources["wall_seconds"],
                     "limit_seconds": max_wall},
        ))
    return found


def limit_problems(resources: Mapping[str, Any], *, kind: str | None = None,
                   limits: Mapping[str, Any] | None = None) -> list[str]:
    """:func:`limit_findings` as the messages admission would give."""
    return [finding.message for finding in limit_findings(resources, kind=kind, limits=limits)]


__all__ = ["DEFAULT_GPU_QUEUE_WAIT_SECONDS", "GPU_SWITCH", "gpu_queue_finding", "gpu_queue_seconds_ahead",
           "gpu_queue_wait_seconds", "limit_findings", "limit_problems", "switch_finding", "switch_findings"]
