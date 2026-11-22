"""Job directories and the runner: prepare, launch, observe, cancel. No store.

Everything here is about files and processes. What a job *is* — its capability, its
budget, its state — is the service's; this module answers four questions for it:

* where does a job keep its files (:func:`paths`);
* start a runner for it and tell me who that is (:func:`launch`);
* what can be seen of it now (:func:`observe`) — finished, running, queued for the GPU,
  never started, *lost* (a runner that is gone and left no outcome), or *unknown*: the
  evidence is there and cannot be read, so nothing may be decided from it;
* ask it to stop (:func:`request_cancel`), and end whatever a lost runner left behind
  (:func:`end_orphans`).

A runner is identified by pid *and* start time. v1 identified it by a row that said
SUBMITTED, and could not tell a running tool from a dead one.

A file that does not exist and a file that exists and cannot be read are different
observations. The first is evidence (a runner records itself before it starts the tool,
and writes its outcome before it exits); the second is the absence of evidence, and a
job read that way is UNKNOWN, never lost and never failed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..capabilities import procs
from ..domain import clock

RUNNER_PATH = Path(__file__).with_name("runner.py")

NOT_STARTED = "NOT_STARTED"   # no runner ever existed for this directory
STARTING = "STARTING"         # the runner is alive and has not started the tool yet
QUEUED = "QUEUED"             # the runner is waiting for the GPU lease
RUNNING = "RUNNING"           # the tool is running
FINISHED = "FINISHED"         # outcome.json is there
LOST = "LOST"                 # a runner existed, is gone, and left no outcome
UNKNOWN = "UNKNOWN"           # the evidence exists and cannot be read; nothing may be decided

#: The lock a GPU job must hold, one per device, shared by every store on this machine:
#: two stores have two job directories and still only one GPU.
GPU_LOCK_TEMPLATE = "/tmp/gx-gpu-device-{device}.lock"

LAUNCH_TIMEOUT_SECONDS = 60
HEARTBEAT_SECONDS = 2.0
STALE_AFTER_SECONDS = 20.0


class RunnerLaunchFailed(Exception):
    """The runner could not be launched."""


class LaunchReceiptUnknown(RunnerLaunchFailed):
    """The launcher may have handed off a runner, but its ACK was unreadable."""


@dataclass(frozen=True)
class JobPaths:
    job_dir: Path
    in_dir: Path
    home_dir: Path
    tmp_dir: Path
    work_dir: Path
    stdout: Path
    stderr: Path
    spec: Path

    def create(self) -> None:
        for path in (self.job_dir, self.in_dir, self.home_dir, self.tmp_dir, self.work_dir):
            path.mkdir(parents=True, exist_ok=True)


def paths(jobs_root: Path, job_id: str) -> JobPaths:
    job_dir = Path(jobs_root) / job_id
    return JobPaths(
        job_dir=job_dir, in_dir=job_dir / "in", home_dir=job_dir / "home", tmp_dir=job_dir / "tmp",
        work_dir=job_dir / "work", stdout=job_dir / "stdout.log", stderr=job_dir / "stderr.log",
        spec=job_dir / "spec.json",
    )


def gpu_lock_path(device: int) -> str:
    return GPU_LOCK_TEMPLATE.format(device=int(device))


def gpu_queue(lock_path: str) -> list[dict[str, Any]]:
    """The jobs waiting for a device's lease, in the order they will get it. Reads only.

    A ticket whose runner is no longer alive is left out here and cleared by the next
    runner that looks; this function removes nothing.
    """
    queue = Path(lock_path + ".queue")
    waiting: list[dict[str, Any]] = []
    try:
        names = sorted(name for name in os.listdir(queue) if ".tmp." not in name)
    except OSError:
        return waiting
    for name in names:
        holder = read_json(queue / name) or {}
        if procs.is_alive(holder.get("runner_pid"), holder.get("runner_started")):
            waiting.append({"job_id": holder.get("job_id"), "runner_pid": holder.get("runner_pid"),
                            "since": holder.get("at")})
    return waiting


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(document, problem)``. Both None when the file does not exist; a problem when it
    exists and cannot be read. The runner writes every record whole (write, fsync, rename),
    so a record that does not parse was not left half-written by it."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"{path.name} exists and cannot be read ({type(exc).__name__}: {str(exc)[:200]})"
    if not isinstance(value, dict):
        return None, f"{path.name} exists and is not a JSON object"
    return value, None


def _identity(*candidates: tuple[str, Mapping[str, Any] | None]) -> tuple[Any, Any, str | None]:
    """The first source that names a runner by both pid and start time, and which it was.
    A pid from one record and a start time from another would name nobody."""
    for name, candidate in candidates:
        if candidate and candidate.get("runner_pid") and candidate.get("runner_started"):
            return candidate["runner_pid"], candidate["runner_started"], name
    return None, None, None


def write_spec(job: JobPaths, spec: dict[str, Any]) -> None:
    temporary = job.spec.with_suffix(".json.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, job.spec)


def launch(job_dir: Path) -> dict[str, Any]:
    """Start a detached runner for ``job_dir``. Returns ``{runner_pid, runner_started}``.

    The launcher forks the runner into its own session and exits; this call waits for the
    launcher only. Whoever called this can die a millisecond later and the runner neither
    notices nor depends on it.
    """
    argv = [sys.executable, "-I", "-B", str(RUNNER_PATH), "--detach", str(job_dir)]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=LAUNCH_TIMEOUT_SECONDS, env=dict(procs.MINIMAL_ENV), cwd="/",
        )
    except subprocess.TimeoutExpired as exc:
        raise LaunchReceiptUnknown("the detached launcher timed out before acknowledging") from exc
    except OSError as exc:
        raise RunnerLaunchFailed(f"the launcher did not run: {type(exc).__name__}: {exc}") from exc
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        answer = {}
    if (done.returncode != 0 or not isinstance(answer, dict) or
            type(answer.get("runner_pid")) is not int or answer["runner_pid"] <= 0 or
            not str(answer.get("runner_started")).isdigit()):
        raise LaunchReceiptUnknown(
            f"the launcher exited {done.returncode} without a runner: "
            f"{(done.stdout or '')[-300:]} {(done.stderr or '')[-300:]}".strip()
        )
    return {"runner_pid": int(answer["runner_pid"]), "runner_started": str(answer["runner_started"])}


def observe(job_dir: Path, *, runner_pid: int | None = None,
            runner_started: str | None = None) -> dict[str, Any]:
    """What can be seen of a job now. Reads files and ``/proc``; changes nothing.

    ``runner_alive`` is True or False when a runner could be identified and looked at, and
    None when it could not. ``identity_from`` says which record named the runner: the
    launcher's or the runner's own ``runner.json``, the job row, or the runner's
    ``status.json``.
    """
    job_dir = Path(job_dir)
    record: dict[str, Any] = {"observed_at": clock.now_iso(), "state": NOT_STARTED, "outcome": None,
                              "status": None, "runner_alive": False, "heartbeat_age_seconds": None,
                              "stale": False}
    if not job_dir.is_dir():
        # A job's directory is made before its row exists and is never removed while the job
        # is open, so a missing one was taken away: whatever it said cannot be read.
        if not (runner_pid and runner_started):
            return {**record, "state": UNKNOWN, "runner_alive": None,
                    "reason": f"the job directory {job_dir} is gone and no runner is recorded on the "
                              "job: whether one ran, and how it ended, cannot be read"}
        alive = procs.is_alive(runner_pid, runner_started)
        record.update(runner_pid=runner_pid, runner_started=runner_started, runner_alive=alive,
                      identity_from="job row")
        if alive:
            # It is running somewhere: it holds what it holds. What it will say cannot be read.
            return {**record, "state": STARTING,
                    "reason": f"the job directory {job_dir} is gone; its runner pid {runner_pid} is alive"}
        return {**record, "state": UNKNOWN,
                "reason": f"the job directory {job_dir} is gone and its runner pid {runner_pid} is not "
                          "alive: how the job ended cannot be read"}

    outcome, outcome_problem = read_record(job_dir / "outcome.json")
    if outcome is not None:
        return {**record, "state": FINISHED, "outcome": outcome}

    launcher, launcher_problem = read_record(job_dir / "runner.json")
    status, status_problem = read_record(job_dir / "status.json")
    pid, started, source = _identity(
        ("runner.json", launcher), ("job row", {"runner_pid": runner_pid, "runner_started": runner_started}),
        ("status.json", status))
    record.update(runner_pid=pid, runner_started=started, identity_from=source)
    alive: bool | None = procs.is_alive(pid, started) if pid else None
    record["runner_alive"] = alive

    if outcome_problem:
        return {**record, "state": UNKNOWN, "reason": outcome_problem}
    if not pid:
        problems = [problem for problem in (launcher_problem, status_problem) if problem]
        if problems:
            return {**record, "state": UNKNOWN,
                    "reason": "a runner may have been started and cannot be identified: " + "; ".join(problems)}
        # The launcher claims before fork. A claim alone proves neither execution nor refusal.
        if (job_dir / "launch.claim").exists():
            return {**record, "state": UNKNOWN, "runner_alive": None,
                    "reason": "launch was claimed but no runner identity or outcome is available"}
        record["runner_alive"] = False
        record["reason"] = "no runner was ever recorded for this directory"
        return record
    if not alive:
        # The runner writes the outcome and then exits. Looking for the outcome first and
        # the process second can fall between the two; look once more before calling it lost.
        outcome, outcome_problem = read_record(job_dir / "outcome.json")
        if outcome is not None:
            return {**record, "state": FINISHED, "outcome": outcome}
        if outcome_problem:
            return {**record, "state": UNKNOWN, "reason": outcome_problem}
        return {**record, "state": LOST, "status": status,
                "reason": f"runner pid {pid} (started {started}) is gone and wrote no outcome"}

    record.update(status=status or {})
    if status_problem:
        record["reason"] = f"the runner is alive; {status_problem}"
    try:
        age = time.time() - (job_dir / "heartbeat").stat().st_mtime
        record["heartbeat_age_seconds"] = round(age, 3)
        record["stale"] = age > STALE_AFTER_SECONDS
    except OSError:
        pass
    record["state"] = {"waiting_gpu": QUEUED, "running": RUNNING}.get((status or {}).get("state"), STARTING)
    return record


def request_cancel(job_dir: Path, *, reason: str, actor: str) -> bool | None:
    """Ask the runner to end the tool. True if this call wrote the request, False if one
    was already there: a second cancel does not become a second request. None if there is
    no job directory to write it into, so no runner can ever read it."""
    path = Path(job_dir) / "cancel.request"
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    except (FileNotFoundError, NotADirectoryError):
        return None
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump({"reason": reason, "actor": actor, "at": clock.now_iso()}, fh)
    return True


def _proc_stat(pid: int) -> list[str] | None:
    """The fields of /proc/<pid>/stat after the command name, or None (gone, or a zombie)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    tail = raw.rsplit(") ", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) < 20 or fields[0] == "Z":
        return None
    return fields


def _ticks_at(wallclock: float) -> int | None:
    """A wall-clock time as clock ticks since boot, the unit of /proc start times."""
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            boot = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
    except (OSError, StopIteration, ValueError):
        return None
    return int((wallclock - boot) * os.sysconf("SC_CLK_TCK"))


def group_members(pgid: int, *, started_from: str | None, started_by: int | None) -> list[tuple[int, str]]:
    """Processes of process group ``pgid`` that started while the tool could have started
    them: not before the tool itself, and not after ``started_by`` (clock ticks). A pid that
    has been reused since belongs to a process that started later, and is left alone."""
    members: list[tuple[int, str]] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fields = _proc_stat(int(name))
        if fields is None or int(fields[2]) != int(pgid):
            continue
        start = int(fields[19])
        if started_from is not None and start < int(started_from):
            continue
        if started_by is not None and start > started_by:
            continue
        members.append((int(name), fields[19]))
    return members


def end_orphans(status: dict[str, Any] | None, *, job_dir: Path | None = None) -> dict[str, Any]:
    """End what a lost runner's tool left behind: the tool itself if it is still the process
    that was recorded, and every process of its group it started.

    The tool was started as the leader of its own process group, with a parent-death signal,
    so it dies with its runner; what it started does not. Those are found by their process
    group and by their start time: at or after the tool's own, and no later than the runner's
    last sign of life (its heartbeat), so that a pid reused since is never signalled.
    """
    status = status or {}
    pid, started = status.get("tool_pid"), status.get("tool_started")
    record: dict[str, Any] = {"tool_pid": pid, "leader_alive": False, "group_signalled": False, "ended": []}
    if not pid:
        return record
    leader_alive = procs.is_alive(pid, started)
    record["leader_alive"] = leader_alive
    last_alive = time.time()
    if job_dir is not None:
        try:
            last_alive = (Path(job_dir) / "heartbeat").stat().st_mtime
        except OSError:
            pass
    started_by = _ticks_at(last_alive + 2 * HEARTBEAT_SECONDS + 1)
    for member, member_started in group_members(int(pid), started_from=started, started_by=started_by):
        if member == os.getpid():
            continue
        if _proc_stat(member) is None or _proc_stat(member)[19] != member_started:
            continue
        try:
            os.kill(member, signal.SIGKILL)
            record["ended"].append(member)
        except OSError as exc:
            record.setdefault("errors", []).append(f"{member}: {exc}")
    record["group_signalled"] = bool(record["ended"])
    return record


def remove_tree(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


__all__ = ["FINISHED", "GPU_LOCK_TEMPLATE", "HEARTBEAT_SECONDS", "JobPaths", "LOST", "NOT_STARTED",
           "QUEUED", "RUNNER_PATH", "RUNNING", "RunnerLaunchFailed", "STARTING", "UNKNOWN", "end_orphans",
           "gpu_lock_path", "gpu_queue", "launch", "observe", "paths", "read_json", "read_record",
           "remove_tree", "request_cancel", "write_spec"]
