"""The detached runner: one process that owns one tool from launch to outcome.

v1 ran the tool as a child of whoever submitted it and waited for it there. When that
process died the tool carried on unowned: past its time limit, with nobody to write down
how it ended, until the next reconcile found a job it could not explain and deleted the
directory that held its output. The owner of a tool has to be a process whose only work
is that tool. This is it.

    python -I -B runner.py --detach <job_dir>    start a runner and return at once
    python -I -B runner.py <job_dir>             be the runner (what --detach forks into)

The runner is told everything in ``<job_dir>/spec.json`` and answers only through files
in that directory. It never opens the store, imports nothing from this tree, and reads
no environment of its own, so a runner that started under one version of the code
finishes under that version whatever is edited meanwhile.

    spec.json        written by the job service before the runner starts
    launch.claim     made by the launcher, once: a directory is given one runner, ever
    runner.json      pid and start time of the runner (its identity; a pid alone is reused)
    status.json      starting -> waiting_gpu -> running -> finished
    heartbeat        rewritten every few seconds while the runner is alive
    cancel.request   written by the job service; the runner ends the tool when it sees it
    outcome.json     how it ended, with measurements; written once, atomically

What it enforces, whoever else has died:

* **the time limit** — the tool's whole process group is ended at ``timeout_seconds``;
* **the GPU lease** — a GPU job starts only while it holds an exclusive lock on the
  device's lock file, so two of them never overlap; and it reads the device again at that
  moment, because the reading taken at submission is by then some seconds old;
* **the limits file** — holds and the ``gpu_jobs`` switch are read again immediately
  before launch; a hold placed after submission stops a queued job from starting;
* **shared GPU capacity** — the locked preflight checks space for the reservation;
  subsequent whole-device readings provide diagnostic information about all tenants;
* **no orphans** — the tool runs in its own session with a parent-death signal, and
  whatever is left of its process group when it exits is ended too;
* **the network route** — a job with no network need is given proxy variables pointing at a
  closed loopback port, and does not start if anything listens there; a server launched for
  one call is given the proxy, and does not start if the proxy does not answer;
* **read-only trees stay read-only** — when the specification names ``readonly_guard``
  roots (a server of the earlier platform launched for one call), a marker is taken
  before the tool starts and every entry under each root whose mtime or ctime is at or
  after it, or a change in what each root holds, is found after the whole process group
  has ended. A change fails the job and the outcome names the paths. Changes elsewhere in
  the guard's wider tree are listed and attributed to nobody.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import errno
import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
import traceback

resource_guard_mod = None


def _load_resource_guard():
    # -I intentionally removes the script directory from sys.path. Only this
    # code-owned sibling contains the job resource observation functions.
    import importlib.util
    location = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resource_guard.py")
    found = importlib.util.spec_from_file_location("gx_job_resource_guard", location)
    module = importlib.util.module_from_spec(found)
    found.loader.exec_module(module)
    return module

SCHEMA = "gx.job_runner/2"

RESULT_EXITED = "exited"
RESULT_TIMED_OUT = "timed_out"
RESULT_CANCELLED = "cancelled"
RESULT_TERMINATED = "runner_terminated"       # the runner itself was signalled; not a cancel
RESULT_GPU_MEMORY = "gpu_memory_exceeded"
RESULT_REFUSED = "refused"                    # holds, a switch, or a limit moved under the job
RESULT_PREFLIGHT_REFUSED = "preflight_refused"
RESULT_LEASE_TIMEOUT = "lease_timeout"
RESULT_SPAWN_FAILED = "spawn_failed"
RESULT_RUNNER_ERROR = "runner_error"
RESULT_READONLY_CHANGED = "readonly_tree_changed"   # the tool ran and changed a read-only tree

#: Entries a read-only root may hold for the check to cover it whole. Larger, and the job
#: is not started: a marker that covers part of a tree is not a marker.
GUARD_MAX_ENTRIES = 400_000

#: Results after which the tool never started.
NOT_STARTED_RESULTS = (RESULT_REFUSED, RESULT_PREFLIGHT_REFUSED, RESULT_LEASE_TIMEOUT,
                       RESULT_SPAWN_FAILED)

POLL_SECONDS = 0.2
MAX_LISTED_FILES = 2000
NVIDIA_QUERY = "index,memory.used,memory.total,utilization.gpu"

_PR_SET_PDEATHSIG = 1
try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
except OSError:  # pragma: no cover - a libc that cannot be loaded
    _LIBC = None


# ----------------------------------------------------------------------------- small tools

def now_iso() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def write_json_atomic(path: str, document: dict) -> None:
    """Whole or not at all: a reader never sees half a document."""
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def start_ticks(pid: int) -> str | None:
    """When ``pid`` started, in clock ticks since boot; None for no process or a zombie."""
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
    return fields[19]


def _set_parent_death_signal() -> None:
    """Runs in the tool's process before exec: die if the runner dies."""
    if _LIBC is not None:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def list_files(root: str) -> tuple[list[dict], bool]:
    """Regular files under ``root`` with size and sha256. Symlinks are named, not followed."""
    found: list[dict] = []
    truncated = False
    if os.path.realpath(root) != os.path.abspath(root):
        return [], True
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return [], True

    def incomplete(_error):
        nonlocal truncated
        truncated = True

    try:
        for directory, subdirs, names, fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False, onerror=incomplete):
            subdirs.sort()
            for name in sorted(names):
                path = os.path.join(directory, name)
                relative = os.path.normpath(path)
                if len(found) >= MAX_LISTED_FILES:
                    truncated = True
                    continue
                try:
                    mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                    if stat.S_ISLNK(mode):
                        found.append({"path": relative, "symlink": True})
                        continue
                    if not stat.S_ISREG(mode):
                        found.append({"path": relative, "not_regular": True})
                        continue
                    raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(raw, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            found.append({"path": relative, "not_regular": True})
                            continue
                        digest = hashlib.sha256()
                        for block in iter(lambda: stream.read(1 << 20), b""):
                            digest.update(block)
                        found.append({"path": relative, "bytes": info.st_size, "sha256": digest.hexdigest()})
                except OSError as exc:
                    found.append({"path": relative, "error": str(exc)})
    finally:
        os.close(root_fd)
    return found, truncated


# --------------------------------------------------------------------------------- the GPU

def read_gpu(nvidia_smi: str | None, device: int, *, timeout_seconds: float = 30) -> dict:
    """One reading of one device. Free memory and utilisation only: PIDs here are the host's."""
    reading: dict = {"observed_at": now_iso(), "source": "nvidia-smi", "device": device,
                     "ok": False}
    if not nvidia_smi:
        reading["problem"] = "no nvidia-smi path was recorded for this job"
        return reading
    try:
        done = subprocess.run(
            [nvidia_smi, f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        reading["problem"] = f"nvidia-smi did not run: {type(exc).__name__}: {exc}"
        return reading
    if done.returncode != 0:
        reading["problem"] = f"nvidia-smi exited {done.returncode}: {done.stderr[:200]}"
        return reading
    for line in done.stdout.splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != 4:
            continue
        try:
            index, used, total = int(cells[0]), int(cells[1]), int(cells[2])
        except ValueError:
            continue
        if index != device or total <= 0 or not 0 <= used <= total:
            continue
        try:
            utilization: int | None = int(cells[3])
        except ValueError:
            utilization = None
        reading.update(ok=True, memory_used_mib=used, memory_total_mib=total,
                       memory_free_mib=total - used, utilization_pct=utilization)
        return reading
    reading["problem"] = f"device {device} is not in the nvidia-smi reading"
    return reading


def read_gpu_identity(nvidia_smi: str | None, device: int, *, timeout_seconds: float) -> dict:
    if not nvidia_smi:
        return {"ok": False, "problem": "missing nvidia-smi"}
    try:
        done = subprocess.run([nvidia_smi, "--query-gpu=index,uuid,name,memory.total,driver_version",
                               "--format=csv,noheader,nounits"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, errors="replace", timeout=timeout_seconds)
        for line in done.stdout.splitlines() if done.returncode == 0 else []:
            cells = [x.strip() for x in line.split(",")]
            if len(cells) == 5 and int(cells[0]) == device:
                return {"ok": True, "uuid": cells[1], "name": cells[2],
                        "total_mib": int(cells[3]), "driver_version": cells[4]}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "problem": type(exc).__name__}
    return {"ok": False, "problem": "physical GPU identity missing or unreadable"}


def launch_gate(spec: dict) -> tuple[str | None, dict]:
    """Read the limits file again. Returns ``(reason to refuse or None, the limits read)``.

    Fails closed: a limits file that cannot be read is a refusal, because the alternative
    is to launch under limits nobody can see.
    """
    path = spec.get("limits_path")
    limits = read_json(path) if path else None
    if not isinstance(limits, dict):
        return f"the limits file {path!r} could not be read immediately before launch", {}
    for hold in limits.get("holds") or []:
        if isinstance(hold, dict) and hold.get("scope") in (None, "*", spec.get("hold_scope", "jobs")):
            return f"hold {hold.get('id', '?')} is active: {hold.get('reason', 'no reason recorded')}", limits
    gpu = spec.get("gpu")
    if gpu:
        switch = (limits.get("switches") or {}).get("gpu_jobs") or {}
        if switch.get("value") is not True:
            return "switch gpu_jobs is off", limits
        declared = switch.get("scope")
        if declared is not None and gpu.get("scope") != declared:
            return f"switch gpu_jobs is limited to scope {declared!r}; this job is {gpu.get('scope')!r}", limits
        table = limits.get("gpu") or {}
        try:
            max_self = int(table["max_self_memory_mib"])
            max_wall = int(table["max_wall_minutes"]) * 60
        except (KeyError, TypeError, ValueError):
            return "the gpu limits of the limits file are unreadable", limits
        if int(gpu["need_mib"]) > max_self:
            return f"the job needs {gpu['need_mib']} MiB and gpu.max_self_memory_mib is now {max_self}", limits
        if int(spec["timeout_seconds"]) > max_wall:
            return (f"the job's time limit is {spec['timeout_seconds']} s and gpu.max_wall_minutes "
                    f"is now {max_wall // 60}"), limits
    for needed in spec.get("switches") or []:
        name = needed.get("name")
        switch = (limits.get("switches") or {}).get(name) or {}
        if switch.get("value") is not True:
            return f"switch {name} is off", limits
        declared = switch.get("scope")
        if declared is not None and needed.get("scope") != declared:
            return f"switch {name} is limited to scope {declared!r}; this job is {needed.get('scope')!r}", limits
    return None, limits


def loopback_answers(host: str, port: int, timeout: float = 2.0) -> bool:
    """Whether something accepts a connection on a loopback address. Loopback only: this
    never opens a connection that leaves the machine."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(f"{host} is not a loopback address")
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def network_gate(spec: dict) -> tuple[str | None, dict | None]:
    """The job's route to the network, checked immediately before the tool starts.

    Launch plans pass the configured proxy to the child. When the plan requests
    a readiness probe, a failed probe stops launch. Historical closed-loopback
    terms remain readable for already-written specifications.
    """
    network = spec.get("network")
    if not network:
        return None, None
    record = {"mode": network.get("mode"), "proxy": network.get("proxy"), "checked_at": now_iso()}
    if network.get("must_not_answer"):
        host, port = network["must_not_answer"]
        record["listener"] = loopback_answers(host, port)
        if record["listener"]:
            return (f"something is listening on {host}:{port}, where a job with no network need sends its "
                    "proxy traffic; the route is not closed, so the tool is not started"), record
    if network.get("must_answer"):
        host, port = network["must_answer"]
        record["proxy_answers"] = loopback_answers(host, port)
        if not record["proxy_answers"]:
            return (f"the proxy at {host}:{port} does not answer; a job may reach the network only through "
                    "it, so the tool is not started"), record
    return None, record


# ------------------------------------------------------------------------ read-only trees

def tree_census(root: str) -> dict:
    """What a root holds, counted without following links. A root that does not exist is
    a census too: if it exists afterwards, something made it."""
    if not os.path.lexists(root):
        return {"exists": False}
    counts = {"exists": True, "files": 0, "dirs": 0, "links": 0, "other": 0, "unreadable": 0}
    if os.path.islink(root) or not os.path.isdir(root):
        counts["files" if os.path.isfile(root) else "other"] += 1
        return counts
    stack = [root]
    total = 0
    while stack:
        directory = stack.pop()
        counts["dirs"] += 1
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    total += 1
                    if total > GUARD_MAX_ENTRIES:
                        raise OverflowError(f"{root} holds more than {GUARD_MAX_ENTRIES} entries")
                    if entry.is_symlink():
                        counts["links"] += 1
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        counts["files"] += 1
                    else:
                        counts["other"] += 1
        except OSError:
            counts["unreadable"] += 1
    return counts


def changed_since(root: str, marker_ns: int, *, limit: int, depth: int | None = None,
                  skip: tuple = ()) -> tuple[list[str], int]:
    """Entries under ``root``, the root included, whose mtime or ctime is at or after the
    marker, without following links. ``depth`` bounds how far down; ``skip`` are subtrees
    looked at elsewhere."""
    found: list[str] = []
    count = 0

    def note(path: str, stat: os.stat_result) -> None:
        nonlocal count
        if stat.st_mtime_ns >= marker_ns or stat.st_ctime_ns >= marker_ns:
            count += 1
            if len(found) < limit:
                found.append(path)

    try:
        note(root, os.lstat(root))
    except OSError:
        return found, count
    stack = [(root, 0)]
    while stack:
        directory, level = stack.pop()
        if depth is not None and level >= depth:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.path in skip:
                        continue
                    try:
                        note(entry.path, entry.stat(follow_symlinks=False))
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, level + 1))
        except OSError:
            continue
    return found, count


def guard_before(guard: dict) -> dict:
    """Take the marker, then count what each root holds.

    Timestamps on this machine's shared filesystem are whole seconds, so the marker is the
    start of a fresh second, taken by waiting for it: a change in the same second as the
    marker is still at or after it, and nothing before the marker shares its second.
    """
    now = time.time_ns()
    marker = (now // 1_000_000_000 + 1) * 1_000_000_000
    time.sleep(max(0.0, (marker - time.time_ns()) / 1e9) + 0.01)
    before = {root: tree_census(root) for root in guard["roots"]}
    return {"marker_ns": marker, "before": before}


def guard_after(guard: dict, state: dict) -> dict:
    limit = int(guard.get("max_listed", 200))
    marker = int(state["marker_ns"])
    report: dict = {"marker_at": _iso_from_ns(marker), "roots": [], "changed": [], "changed_count": 0,
                    "elsewhere": [], "elsewhere_count": 0, "violated": False}
    for root in guard["roots"]:
        after = tree_census(root)
        changed, count = changed_since(root, marker, limit=limit) if after.get("exists") else ([], 0)
        before = state["before"][root]
        counts_moved = after != before
        report["roots"].append({"root": root, "before": before, "after": after, "changed_count": count,
                                "counts_moved": counts_moved})
        report["changed"].extend(changed[: max(0, limit - len(report["changed"]))])
        report["changed_count"] += count
        if count or counts_moved:
            report["violated"] = True
    wider = guard.get("elsewhere") or {}
    if wider.get("root") and os.path.isdir(wider["root"]):
        listed, count = changed_since(wider["root"], marker, limit=limit, depth=int(wider.get("depth", 4)),
                                      skip=tuple(guard["roots"]))
        report["elsewhere"] = [path for path in listed if path != wider["root"]]
        report["elsewhere_count"] = count - (1 if wider["root"] in listed else 0)
        report["elsewhere_note"] = ("changed under the wider tree since the marker, outside the roots this "
                                    "job's backend could touch; attributed to nobody")
    return report


def _iso_from_ns(value: int) -> str:
    moment = _dt.datetime.fromtimestamp(value / 1e9, tz=_dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# ------------------------------------------------------------------------------ the runner

class Runner:
    def __init__(self, job_dir: str) -> None:
        self.job_dir = os.path.abspath(job_dir)
        self.spec = read_json(os.path.join(self.job_dir, "spec.json"))
        if not isinstance(self.spec, dict):
            raise RuntimeError(f"{self.job_dir}/spec.json is missing or not JSON")
        if self.spec.get("resource_guard") or self.spec.get("gpu"):
            global resource_guard_mod
            if resource_guard_mod is None:
                resource_guard_mod = _load_resource_guard()
        self.pid = os.getpid()
        self.started = start_ticks(self.pid)
        self.terminate_requested = False
        self.last_heartbeat = 0.0
        self.state: dict = {}

    def path(self, name: str) -> str:
        return os.path.join(self.job_dir, name)

    def status(self, state: str, **extra: object) -> None:
        self.state = {"schema": SCHEMA, "job_id": self.spec.get("job_id"), "state": state,
                      "runner_pid": self.pid, "runner_started": self.started, "at": now_iso(),
                      **{**{k: v for k, v in self.state.items() if k.startswith("tool_")}, **extra}}
        write_json_atomic(self.path("status.json"), self.state)

    def heartbeat(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_heartbeat < float(self.spec.get("heartbeat_seconds", 2.0)):
            return
        self.last_heartbeat = now
        try:
            with open(self.path("heartbeat"), "w", encoding="utf-8") as handle:
                handle.write(now_iso())
        except OSError:  # a full disk must not end the tool; the pid is the liveness
            pass

    # ---------------------------------------------------------------- the GPU queue

    def take_ticket(self, gpu: dict) -> str:
        """Join the queue for a device: a file whose name sorts by the moment of joining."""
        queue = gpu["lock_path"] + ".queue"
        os.makedirs(queue, exist_ok=True)
        ticket = os.path.join(queue, f"{time.time_ns():020d}-{self.pid}")
        write_json_atomic(ticket, {"runner_pid": self.pid, "runner_started": self.started,
                                   "job_id": self.spec.get("job_id"), "at": now_iso()})
        return ticket

    def first_in_queue(self, gpu: dict, ticket: str) -> bool:
        """Whether this job is at the head of the queue, after clearing tickets of dead runners."""
        queue = os.path.dirname(ticket)
        if not os.path.exists(ticket):          # somebody tidied it away; rejoin at the back
            write_json_atomic(ticket, {"runner_pid": self.pid, "runner_started": self.started,
                                       "job_id": self.spec.get("job_id"), "at": now_iso()})
        mine = os.path.basename(ticket)
        for name in sorted(os.listdir(queue)):
            if name == mine:
                return True
            if ".tmp." in name:
                continue
            holder = read_json(os.path.join(queue, name)) or {}
            if start_ticks(int(holder.get("runner_pid") or 0)) == holder.get("runner_started") \
                    and holder.get("runner_started"):
                return False                     # a live job joined before this one
            try:
                os.unlink(os.path.join(queue, name))   # its runner is gone: it is not waiting any more
            except OSError:
                pass
        return True

    @staticmethod
    def drop_ticket(ticket: str) -> None:
        try:
            os.unlink(ticket)
        except OSError:
            pass

    def stop_reason(self) -> tuple[str, str] | None:
        """Why the tool should not go on, if anything says so. A cancel is a file; a signal
        to the runner is not a cancel and is reported as what it was."""
        if os.path.exists(self.path("cancel.request")):
            return RESULT_CANCELLED, "a cancel was requested"
        if self.terminate_requested:
            return RESULT_TERMINATED, "the runner was signalled to terminate"
        return None

    def _on_terminate(self, signum: int, frame: object) -> None:
        self.terminate_requested = True

    # ------------------------------------------------------------------------- main

    def run(self) -> int:
        write_json_atomic(self.path("runner.json"), {
            "schema": SCHEMA, "job_id": self.spec.get("job_id"), "runner_pid": self.pid,
            "runner_started": self.started, "at": now_iso(),
        })
        signal.signal(signal.SIGTERM, self._on_terminate)
        signal.signal(signal.SIGINT, self._on_terminate)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.status("starting")
        self.heartbeat(force=True)
        lock_fd: int | None = None
        try:
            outcome, lock_fd = self.execute()
        except BaseException as exc:  # noqa: BLE001 - whatever happens, an outcome is written
            outcome = {"result": RESULT_RUNNER_ERROR, "error": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc()[-4000:]}
            if self.spec.get("resource_guard") and self.state.get("tool_pid"):
                # A guard exception cannot silently settle/release an unproven child.
                outcome.update(started=True, resource_guard={"schema": resource_guard_mod.SCHEMA,
                    "abort_reason": "resource_observation_or_cleanup_unproven",
                    "cleanup": {"proven_gone": False}, "tool_pid": self.state["tool_pid"]})
        outcome = {"schema": SCHEMA, "job_id": self.spec.get("job_id"), "runner_pid": self.pid,
                   "runner_started": self.started, "written_at": now_iso(), **outcome}
        write_json_atomic(self.path("outcome.json"), outcome)
        self.status("finished", result=outcome.get("result"))
        if lock_fd is not None:
            os.close(lock_fd)  # the lease ends with the outcome on disk, not before
        return 0

    def execute(self) -> tuple[dict, int | None]:
        spec = self.spec
        reason, _ = launch_gate(spec)
        if reason:
            return {"result": RESULT_REFUSED, "reason": reason}, None

        gpu = spec.get("gpu")
        guard_policy = spec.get("resource_guard")
        lock_fd: int | None = None
        gpu_record: dict | None = None
        queue_seconds = 0.0
        if gpu:
            self.status("waiting_gpu")
            waited_from = time.monotonic()
            lock_fd = os.open(gpu["lock_path"], os.O_RDWR | os.O_CREAT, 0o666)
            ticket = self.take_ticket(gpu)
            try:
                while True:
                    # Only the job at the head of the queue asks for the lease, so jobs get the
                    # GPU in the order they started waiting, whichever store they belong to.
                    if self.first_in_queue(gpu, ticket):
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError as exc:
                            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                                raise
                    queue_seconds = time.monotonic() - waited_from
                    stop = self.stop_reason()
                    if stop:
                        return {"result": stop[0], "reason": stop[1], "started": False,
                                "queue_seconds": round(queue_seconds, 3)}, lock_fd
                    if queue_seconds > float(gpu["queue_timeout_seconds"]):
                        return {"result": RESULT_LEASE_TIMEOUT, "queue_seconds": round(queue_seconds, 3),
                                "reason": "the GPU lease was held by other jobs for longer than this "
                                          "job may queue"}, lock_fd
                    self.heartbeat()
                    time.sleep(0.5)
            finally:
                self.drop_ticket(ticket)
            queue_seconds = time.monotonic() - waited_from

            # The lease is held. Everything read before it is now stale: read again.
            reason, limits = launch_gate(spec)
            if reason:
                return {"result": RESULT_REFUSED, "reason": reason,
                        "queue_seconds": round(queue_seconds, 3)}, lock_fd
            reading = read_gpu(gpu.get("nvidia_smi"), int(gpu["device"]),
                               timeout_seconds=guard_policy["gpu_read_timeout_seconds"] if guard_policy else 30)
            try:
                min_free = int((limits.get("gpu") or {})["min_free_mib"])
            except (KeyError, TypeError, ValueError):
                min_free = int(gpu["min_free_mib"])
            need = int(gpu["need_mib"])
            floor = max(min_free, need)
            gpu_record = {"device": int(gpu["device"]), "need_mib": need, "preflight": reading,
                          "min_free_mib": min_free, "queue_seconds": round(queue_seconds, 3),
                          "lease_acquired_at": now_iso()}
            if not reading.get("ok"):
                return {"result": RESULT_PREFLIGHT_REFUSED, "gpu": gpu_record,
                        "reason": f"no usable GPU reading at launch: {reading.get('problem')}"}, lock_fd
            if reading["memory_free_mib"] < floor:
                return {"result": RESULT_PREFLIGHT_REFUSED, "gpu": gpu_record,
                        "reason": f"device {gpu['device']} has {reading['memory_free_mib']} MiB "
                                  f"free at launch, below the {floor} MiB this job must see"}, lock_fd
            if guard_policy:
                observed_identity = read_gpu_identity(gpu.get("nvidia_smi"), int(gpu["device"]),
                    timeout_seconds=guard_policy["gpu_read_timeout_seconds"])
                expected_identity = guard_policy["expected_gpu"]
                if (not observed_identity.get("ok") or (expected_identity is not None and
                        any(observed_identity.get(k) != v for k, v in expected_identity.items()))):
                    return {"result": RESULT_PREFLIGHT_REFUSED, "started": False, "gpu": gpu_record,
                            "reason": "selected physical GPU identity differs", "gpu_identity": observed_identity}, lock_fd
                gpu_record["physical_identity"] = observed_identity
            gpu_record.update(baseline_used_mib=reading["memory_used_mib"],
                              peak_used_mib=reading["memory_used_mib"], peak_delta_mib=0,
                              samples=0, sample_problems=0)

        stop = self.stop_reason()
        if stop:
            return {"result": stop[0], "reason": stop[1], "started": False, "gpu": gpu_record}, lock_fd
        return self.run_tool(gpu_record), lock_fd

    def run_tool(self, gpu_record: dict | None) -> dict:
        spec = self.spec
        gpu = spec.get("gpu")
        guard_policy = spec.get("resource_guard")
        guard_record = {"schema": resource_guard_mod.SCHEMA, "peak_group_rss_bytes": 0,
                        "observations": 0, "abort_reason": None} if guard_policy else None
        cleanup = None
        if guard_policy:
            if guard_policy.get("implementation_sha256") != resource_guard_mod.source_sha():
                return {"result": RESULT_REFUSED, "started": False, "reason": "resource guard implementation changed"}
            for name, value in guard_policy["required_env"].items():
                if spec.get("env", {}).get(name) != value:
                    return {"result": RESULT_REFUSED, "started": False, "reason": "job resource environment differs"}
            resource_guard_mod.enable_subreaper()
        timeout = float(spec["timeout_seconds"])
        refusal, network_record = network_gate(spec)
        if refusal:
            return {"result": RESULT_REFUSED, "refused_by": "network", "started": False, "gpu": gpu_record,
                    "network": network_record, "reason": refusal}
        guard = spec.get("readonly_guard")
        guard_state: dict | None = None
        if guard:
            self.status("guarding")
            try:
                guard_state = guard_before(guard)
            except OverflowError as exc:
                return {"result": RESULT_REFUSED, "started": False, "gpu": gpu_record,
                        "reason": f"the read-only check cannot cover its tree, so the tool is not started: {exc}"}
        try:
            stdout = open(spec["stdout_path"], "wb")
            stderr = open(spec["stderr_path"], "wb")
        except OSError as exc:
            return {"result": RESULT_SPAWN_FAILED, "reason": f"log files: {exc}", "gpu": gpu_record}
        if guard_policy and gpu_record is not None:
            write_json_atomic(self.path("gpu_launch_identity.json"), {
                "schema": "gx.job_gpu_launch_identity/1", "job_id": spec["job_id"],
                "runner_pid": self.pid, "runner_started": self.started, "device": gpu["device"],
                "identity": gpu_record["physical_identity"], "reading": gpu_record["preflight"],
                "resource_guard_sha256": resource_guard_mod.canonical_sha(guard_policy),
                "observed_at": now_iso(), "source": "runner_locked_observation",
                "pid_attribution": "not used; whole shared device metadata only"})
        started_at = now_iso()
        t0 = time.monotonic()
        child_env = dict(spec["env"])
        if guard_policy and gpu_record is not None and not guard_policy.get("instrument_selftest"):
            child_env["CUDA_VISIBLE_DEVICES"] = gpu_record["physical_identity"]["uuid"]
        try:
            process = subprocess.Popen(  # noqa: S603 - an argv array from the manifest, no shell
                list(spec["argv"]), cwd=spec["cwd"], env=child_env,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
                start_new_session=True, close_fds=True,
                preexec_fn=(lambda: resource_guard_mod.before_exec(self.pid, guard_policy)) if guard_policy else _set_parent_death_signal,
            )
        except (OSError, ValueError) as exc:
            stdout.close()
            stderr.close()
            return {"result": RESULT_SPAWN_FAILED, "reason": f"{type(exc).__name__}: {exc}",
                    "gpu": gpu_record}
        finally_close = (stdout, stderr)
        pid = process.pid
        tool_started = resource_guard_mod.child_start_ticks(pid) if guard_policy else start_ticks(pid)
        self.status("running", tool_pid=pid, tool_started=tool_started, tool_pgid=pid,
                    started_at=started_at)

        result = RESULT_EXITED
        kill_reason: str | None = None
        last_sample = t0
        wait_status: int | None = None
        usage = None
        sample_every = float((gpu or {}).get("sample_seconds", 2.0))
        if gpu:
            gpu_policy = guard_policy or {"expected_gpu": None, "gpu_max_delta_mib": gpu["need_mib"],
                                          "gpu_min_free_mib": gpu_record["min_free_mib"]}
            read_timeout = gpu_policy.get("gpu_read_timeout_seconds", 1.0)
        while True:
            if guard_policy:
                exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    wait_status, usage, cleanup = resource_guard_mod.finish_owned_group(
                        pid, tool_started, guard_policy, terminate=False, grace=0)
                    break
            else:
                waited, wait_status, usage = os.wait4(pid, os.WNOHANG)
                if waited == pid:
                    break
            now = time.monotonic()
            stop = self.stop_reason()
            if stop:
                result, kill_reason = stop
            elif now - t0 > timeout:
                result, kill_reason = RESULT_TIMED_OUT, f"still running after its {timeout:g} s limit"
            if not kill_reason and guard_policy:
                try:
                    host = resource_guard_mod.group_sample(pid, maximum=guard_policy["max_members"])
                    guard_record["observations"] += 1
                    guard_record["peak_group_rss_bytes"] = max(guard_record["peak_group_rss_bytes"], host["rss_bytes"])
                    if (guard_policy["max_group_rss_bytes"] is not None
                            and host["rss_bytes"] > guard_policy["max_group_rss_bytes"]):
                        result, kill_reason = RESULT_RUNNER_ERROR, "host_group_rss_limit"
                except (OSError, ValueError) as exc:
                    result, kill_reason = RESULT_RUNNER_ERROR, "host_group_observation_unavailable"
            if not kill_reason and gpu and gpu_record is not None and now - last_sample >= sample_every:
                last_sample = now
                reading = read_gpu(gpu.get("nvidia_smi"), int(gpu["device"]),
                                   timeout_seconds=read_timeout)
                problem = resource_guard_mod.gpu_problem(reading, gpu_record["baseline_used_mib"], gpu_policy)
                if problem:
                    gpu_record["sample_problems"] += 1
                    gpu_record["last_sample_problem"] = problem
                if problem not in ("gpu_observation_unavailable", "gpu_observation_invalid"):
                    used = reading["memory_used_mib"]
                    gpu_record["samples"] += 1
                    gpu_record["peak_used_mib"] = max(gpu_record["peak_used_mib"], used)
                    delta = used - gpu_record["baseline_used_mib"]
                    gpu_record["peak_delta_mib"] = max(gpu_record["peak_delta_mib"], delta)
            if kill_reason:
                if guard_policy:
                    guard_record["abort_reason"] = kill_reason
                    wait_status, usage, cleanup = resource_guard_mod.finish_owned_group(
                        pid, tool_started, guard_policy, terminate=True, grace=float(spec.get("kill_grace_seconds", 3)))
                else:
                    wait_status, usage = self.end_group(pid)
                break
            self.heartbeat()
            time.sleep(POLL_SECONDS)

        wall = time.monotonic() - t0
        leftover = bool(cleanup.get("had_descendants")) if guard_policy else self.sweep_group(pid)
        if guard_policy:
            guard_record["cleanup"] = cleanup
            if (usage is not None and guard_policy["max_group_rss_bytes"] is not None
                    and usage.ru_maxrss * 1024 > guard_policy["max_group_rss_bytes"]):
                guard_record["abort_reason"] = "host_process_peak_rss_limit"
                result, kill_reason = RESULT_RUNNER_ERROR, "host_process_peak_rss_limit"
        if wait_status is not None:
            process.returncode = os.waitstatus_to_exitcode(wait_status)
        if gpu and gpu_record is not None:
            gpu_record["after"] = read_gpu(gpu.get("nvidia_smi"), int(gpu["device"]), timeout_seconds=read_timeout)
            if guard_policy:
                guard_record["final_gpu_problem"] = resource_guard_mod.gpu_problem(
                    gpu_record["after"], gpu_record["baseline_used_mib"], gpu_policy)
        for handle in finally_close:
            handle.close()

        code = os.waitstatus_to_exitcode(wait_status) if wait_status is not None else None
        files, truncated = list_files(spec["out_dir"])
        outcome = {
            "result": result, "reason": kill_reason,
            "exit_code": code if code is not None and code >= 0 else None,
            "signal": -code if code is not None and code < 0 else None,
            "started": True, "started_at": started_at, "finished_at": now_iso(),
            "wall_seconds": round(wall, 3), "timeout_seconds": timeout,
            "cpu_user_seconds": round(usage.ru_utime, 3) if usage is not None else None,
            "cpu_system_seconds": round(usage.ru_stime, 3) if usage is not None else None,
            "max_rss_kib": int(usage.ru_maxrss) if usage is not None else None,
            "tool_pid": pid, "tool_started": tool_started, "group_leftover_ended": leftover,
            "gpu": gpu_record,
            "files": files, "files_truncated": truncated,
            "stdout_bytes": _size(spec["stdout_path"]), "stderr_bytes": _size(spec["stderr_path"]),
            "network": network_record,
        }
        if guard_record is not None:
            outcome["resource_guard"] = guard_record
        if guard_state is not None:
            # After the whole group has ended: nothing of the tool can still be writing.
            report = guard_after(guard, guard_state)
            outcome["readonly_guard"] = report
            if report["violated"]:
                outcome["result_before_guard"] = result
                outcome["result"] = RESULT_READONLY_CHANGED
                outcome["reason"] = (f"{report['changed_count']} path(s) under the read-only root(s) changed "
                                     f"while the tool ran: {', '.join(report['changed'][:5])}")
        return outcome

    def end_group(self, pid: int) -> tuple[int | None, object]:
        """SIGTERM the tool's group, give it the grace period, then SIGKILL. Returns its status."""
        grace = float(self.spec.get("kill_grace_seconds", 3.0))
        for sig, patience in ((signal.SIGTERM, grace), (signal.SIGKILL, 30.0)):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass
            deadline = time.monotonic() + patience
            while time.monotonic() < deadline:
                waited, status, usage = os.wait4(pid, os.WNOHANG)
                if waited == pid:
                    return status, usage
                time.sleep(0.05)
        waited, status, usage = os.wait4(pid, 0)  # SIGKILL cannot be refused; this returns
        return status, usage

    @staticmethod
    def sweep_group(pid: int) -> bool:
        """End whatever the tool left behind in its process group. True if something was there."""
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            return False
        return True


def _size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------------- launch

def detach(job_dir: str) -> int:
    """Fork the runner into its own session and report its identity on stdout.

    The caller waits for *this* process, which exits at once, so the runner is nobody's
    child and nobody's zombie: it is re-parented to init the moment this returns.
    """
    job_dir = os.path.abspath(job_dir)
    if not os.path.isfile(os.path.join(job_dir, "spec.json")):
        print(json.dumps({"error": f"{job_dir}/spec.json does not exist"}))
        return 2
    # One job directory, one runner, ever: a second launch would run the tool a second time
    # under the same job id. The claim is made before the fork, atomically.
    try:
        claim = os.open(os.path.join(job_dir, "launch.claim"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        print(json.dumps({"error": f"{job_dir} has already been given a runner; a job is launched once"}))
        return 2
    os.write(claim, json.dumps({"launcher_pid": os.getpid(), "at": now_iso()}).encode("utf-8"))
    os.close(claim)
    sys.stdout.flush()
    child = os.fork()
    if child:
        identity = None
        for _ in range(200):  # the child exists as soon as fork returns; this is belt and braces
            identity = start_ticks(child)
            if identity:
                break
            time.sleep(0.01)
        if identity:
            # Written here, before the caller is answered, so that a caller killed at any
            # moment after this leaves a directory that says who its runner is.
            write_json_atomic(os.path.join(job_dir, "runner.json"), {
                "schema": SCHEMA, "runner_pid": child, "runner_started": identity, "at": now_iso(),
                "written_by": "launcher"})
        print(json.dumps({"runner_pid": child, "runner_started": identity}))
        sys.stdout.flush()
        os._exit(0 if identity else 3)
    # --- the runner ---
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDONLY)
    log = os.open(os.path.join(job_dir, "runner.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(devnull)
    os.close(log)
    os.chdir("/")
    try:
        code = Runner(job_dir).run()
    except BaseException:  # noqa: BLE001 - nothing may escape a detached process silently
        traceback.print_exc()
        code = 1
    os._exit(code)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--detach":
        return detach(argv[2])
    if len(argv) == 2:
        return Runner(argv[1]).run()
    print("usage: runner.py [--detach] <job_dir>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
