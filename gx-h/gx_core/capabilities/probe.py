"""One reading of the GPU, and the preflight a GPU job must pass.

The PID columns of ``nvidia-smi`` are deliberately not queried. On this pod they are
*host* pids, so a process list cannot say whether resident memory belongs to this
container or to a neighbour. Free memory and utilisation are the only honest signals,
and they are the ones read here.

Who calls this: the job service, for every capability whose manifest declares a GPU need
— at submission, and the detached runner repeats the reading at the moment it holds the
GPU lease, because a reading is true for the instant it was taken. A caller cannot pass
the reading in: ``nvidia_smi_output`` exists for unit tests of the parsing and the rules,
and a record built from it says ``source: supplied``, which the job service refuses.
"""

from __future__ import annotations

import shutil
from typing import Any, Mapping

from .. import config
from ..domain import clock
from ..errors import ResourceDenied
from . import procs

NVIDIA_SMI_QUERY = "index,memory.used,memory.total,utilization.gpu"
NVIDIA_SMI_ARGV = (
    "nvidia-smi",
    f"--query-gpu={NVIDIA_SMI_QUERY}",
    "--format=csv,noheader,nounits",
)

#: The only source a launch may rest on.
SOURCE_OBSERVED = "nvidia-smi"
SOURCE_SUPPLIED = "supplied"


def parse_nvidia_smi(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """``index, used, total, util`` lines -> devices, and every line that did not parse."""
    devices: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != 4:
            problems.append(f"line {number}: expected 4 fields, got {len(cells)}: {line[:80]!r}")
            continue
        try:
            index, used, total = int(cells[0]), int(cells[1]), int(cells[2])
        except ValueError:
            problems.append(f"line {number}: non-integer memory field: {line[:80]!r}")
            continue
        try:
            utilization: int | None = int(cells[3])
        except ValueError:
            utilization = None  # "[N/A]" on some drivers; memory is still usable
        if total <= 0 or used < 0 or used > total:
            problems.append(f"line {number}: implausible memory used={used} total={total}")
            continue
        devices.append({
            "index": index, "memory_used_mib": used, "memory_total_mib": total,
            "memory_free_mib": total - used, "utilization_pct": utilization,
        })
    return devices, problems


def read_gpu_state(*, nvidia_smi_output: str | None = None) -> dict[str, Any]:
    """One reading of the GPUs."""
    observed_at = clock.now_iso()
    base = {"observed_at": observed_at, "query": NVIDIA_SMI_QUERY}
    if nvidia_smi_output is not None:
        devices, problems = parse_nvidia_smi(nvidia_smi_output)
        return {**base, "source": SOURCE_SUPPLIED, "devices": devices, "problems": problems}
    binary = shutil.which(NVIDIA_SMI_ARGV[0])
    if binary is None:
        return {**base, "source": SOURCE_OBSERVED, "devices": [],
                "problems": ["nvidia-smi not found on PATH"]}
    timeout = int(config.engineering_budget()["metadata_command_timeout_seconds"])
    result = procs.run_bounded([binary, *NVIDIA_SMI_ARGV[1:]], timeout=timeout)
    if not result.get("ran"):
        return {**base, "source": SOURCE_OBSERVED, "devices": [],
                "problems": [f"nvidia-smi did not run: {result.get('reason')}"]}
    if result.get("exit_code") != 0:
        return {**base, "source": SOURCE_OBSERVED, "devices": [],
                "problems": [f"nvidia-smi exited {result.get('exit_code')}: "
                             f"{(result.get('stderr') or '')[:200]}"]}
    devices, problems = parse_nvidia_smi(result.get("stdout", ""))
    return {**base, "source": SOURCE_OBSERVED, "devices": devices, "problems": problems}


def assert_preflight_fresh(record: Mapping[str, Any], *, limits: Mapping[str, Any] | None = None) -> float:
    """Refuse a preflight older than ``gpu.preflight_max_age_seconds``, or not observed.

    The window is read from ``config/limits.json`` on every call, so tightening it takes
    effect for work that has not launched yet rather than at the next restart.
    """
    gpu_limits = dict(limits or config.gpu_limits())
    max_age = float(gpu_limits["preflight_max_age_seconds"])
    if record.get("source") != SOURCE_OBSERVED:
        raise ResourceDenied(
            f"GPU preflight source is {record.get('source')!r}; a launch rests on a reading the "
            "backend took, never on one handed to it",
            details={"source": record.get("source")},
        )
    observed_at = record.get("observed_at")
    if not observed_at:
        raise ResourceDenied(
            "GPU preflight record carries no observed_at; it cannot be aged and is therefore "
            "not usable as a preflight",
            details={"max_age_seconds": max_age},
        )
    age = clock.age_seconds(str(observed_at))
    if age > max_age:
        raise ResourceDenied(
            f"GPU preflight is {age:.0f}s old, older than gpu.preflight_max_age_seconds="
            f"{max_age:.0f}; re-run the preflight immediately before launching",
            details={"observed_at": observed_at, "age_seconds": age, "max_age_seconds": max_age,
                     "calibration": "config/limits.json gpu.preflight_max_age_seconds"},
        )
    return age


def gpu_preflight(
    *,
    requested_mib: int,
    limits: Mapping[str, Any] | None = None,
    active_gpu_jobs: int = 0,
    nvidia_smi_output: str | None = None,
) -> dict[str, Any]:
    """Decide whether a GPU job may launch now. Returns the ``jobs.preflight_json`` record.

    Every rule of ``limits.json["gpu"]`` is enforced here:

    ``max_self_memory_mib``      the ceiling on what one job may need at all;
    ``min_free_mib``             the floor of free memory that must be visible;
    ``max_concurrent_gpu_jobs``  how many of *our* GPU jobs may be in flight — counted from
                                 our own job table and passed in, because the PIDs
                                 ``nvidia-smi`` reports here belong to the host;
    ``preflight_max_age_seconds``  enforced by :func:`assert_preflight_fresh` at launch.

    Raises ``RESOURCE_DENIED`` carrying the observed numbers, so a refusal can be read
    without re-running anything.
    """
    gpu_limits = dict(limits or config.gpu_limits())
    min_free = int(gpu_limits["min_free_mib"])
    max_self = int(gpu_limits["max_self_memory_mib"])
    max_concurrent = int(gpu_limits["max_concurrent_gpu_jobs"])
    requested = int(requested_mib)

    reading = read_gpu_state(nvidia_smi_output=nvidia_smi_output)
    record: dict[str, Any] = {
        "observed_at": reading["observed_at"], "source": reading["source"],
        "query": reading["query"], "devices": reading["devices"], "problems": reading["problems"],
        "requested_mib": requested,
        "limits_applied": {
            "min_free_mib": min_free, "max_self_memory_mib": max_self,
            "max_concurrent_gpu_jobs": max_concurrent,
            "preflight_max_age_seconds": gpu_limits.get("preflight_max_age_seconds"),
            "max_wall_minutes": gpu_limits.get("max_wall_minutes"),
        },
        "active_gpu_jobs": int(active_gpu_jobs),
        "pid_attribution": (
            "not used: nvidia-smi PIDs on this pod are host pids, so resident memory may belong "
            "to another container. Decided on free memory and utilisation."
        ),
        "passed": False, "selected_device": None,
    }

    def deny(message: str, rule: str) -> ResourceDenied:
        return ResourceDenied(message, details={**record, "rule": rule})

    if requested <= 0:
        raise deny(f"a GPU job must declare how much memory it needs; got {requested} MiB",
                   "declared_need")
    if requested > max_self:
        raise deny(f"needs {requested} MiB, above gpu.max_self_memory_mib={max_self}",
                   "max_self_memory_mib")
    if int(active_gpu_jobs) >= max_concurrent:
        raise deny(f"{active_gpu_jobs} GPU job(s) already in flight; "
                   f"gpu.max_concurrent_gpu_jobs={max_concurrent}", "max_concurrent_gpu_jobs")
    if bool(gpu_limits.get("abort_if_preflight_missing", True)) and not reading["devices"]:
        raise deny(
            "no usable GPU reading: "
            + ("; ".join(reading["problems"]) or "nvidia-smi returned no devices")
            + ". gpu.abort_if_preflight_missing is set, and an absent reading is not an empty one",
            "abort_if_preflight_missing",
        )
    if reading["problems"]:
        raise deny("GPU reading was partly unparseable, so free memory cannot be trusted: "
                   + "; ".join(reading["problems"]), "readable_reading")

    best = max(reading["devices"], key=lambda device: device["memory_free_mib"])
    record["selected_device"] = best
    if best["memory_free_mib"] < min_free:
        raise deny(
            f"best device {best['index']} has {best['memory_free_mib']} MiB free, below "
            f"gpu.min_free_mib={min_free} (used {best['memory_used_mib']} of "
            f"{best['memory_total_mib']} MiB, util {best['utilization_pct']}%)",
            "min_free_mib",
        )
    if requested > best["memory_free_mib"]:
        raise deny(f"needs {requested} MiB but device {best['index']} has only "
                   f"{best['memory_free_mib']} MiB free", "requested_fits_free")

    record["passed"] = True
    record["headroom_mib"] = best["memory_free_mib"] - requested
    record["note"] = ("a passing preflight is a reading at one instant; the runner reads again "
                      "when it holds the GPU lease")
    return record


__all__ = ["NVIDIA_SMI_ARGV", "NVIDIA_SMI_QUERY", "SOURCE_OBSERVED", "SOURCE_SUPPLIED",
           "assert_preflight_fresh", "gpu_preflight", "parse_nvidia_smi", "read_gpu_state"]
