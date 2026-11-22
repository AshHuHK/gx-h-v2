"""Run one short, bounded command and report what happened. Never a shell.

Used by the probes: asking an interpreter for package metadata, asking an entry point
to answer, reading the GPU. A probe must not be able to hang the backend, leak the
backend's environment into the program, or leave a process behind, so every call here is:

* an argv array with ``shell=False``;
* run in its own session, and killed as a *group* on timeout. ``subprocess.run`` with
  captured output kills only the direct child and then waits for the pipes to close, so
  a program that left a grandchild holding stdout keeps the call blocked long past its
  deadline;
* given ``stdin=DEVNULL``, so a program that reads stdin when it does not recognise a
  flag sees end-of-file instead of waiting;
* given only the environment the caller built.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Mapping, Sequence

from ..errors import SchemaInvalid

#: Output kept per stream. A banner longer than this is a program doing something else.
MAX_CAPTURED_CHARS = 4000

MINIMAL_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


def kill_group(process: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL, the whole session. Killing a parent leaves its children."""
    try:
        group = os.getpgid(process.pid)
    except OSError:
        return
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(group, sig)
        except OSError:
            return
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        # The direct child is gone; make sure nothing it started is left in the group.
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
        return


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str = "/",
) -> dict[str, Any]:
    """Run ``argv`` and return a record. Raises only for an argv that is not one."""
    tokens = list(argv)
    if not tokens:
        raise SchemaInvalid("an empty argv is not a command", details={})
    for token in tokens:
        if not isinstance(token, str) or "\x00" in token:
            raise SchemaInvalid("argv must be strings without NUL bytes", details={})
    record: dict[str, Any] = {"argv": tokens, "cwd": cwd, "timeout_seconds": timeout}
    started = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - argv array, shell=False
            tokens, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            env=dict(env if env is not None else MINIMAL_ENV), cwd=cwd, start_new_session=True,
        )
    except OSError as exc:
        return {**record, "ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - the group refused to die
            stdout, stderr = "", ""
        return {
            **record, "ran": False, "reason": "timeout",
            "seconds": round(time.monotonic() - started, 3),
            "stdout": (stdout or "")[:MAX_CAPTURED_CHARS], "stderr": (stderr or "")[:MAX_CAPTURED_CHARS],
        }
    code = process.returncode
    return {
        **record, "ran": True, "exit_code": code,
        "signal": -code if code is not None and code < 0 else None,
        "seconds": round(time.monotonic() - started, 3),
        "stdout": (stdout or "")[:MAX_CAPTURED_CHARS], "stderr": (stderr or "")[:MAX_CAPTURED_CHARS],
    }


def process_start_ticks(pid: int) -> str | None:
    """When ``pid`` started, in clock ticks since boot, or None if there is no such process.

    A pid alone is not an identity: the kernel reuses it. A pid together with its start
    time is one, and comparing both is how liveness cannot be fooled by reuse.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    # The command name is in parentheses and may itself contain spaces or parentheses.
    tail = raw.rsplit(") ", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    # After the name: state is field 3, so starttime (field 22) is at index 19 here.
    if len(fields) < 20:
        return None
    if fields[0] == "Z":  # a zombie is not a running process
        return None
    return fields[19]


def is_alive(pid: int | None, started: str | None) -> bool:
    if not pid or not started:
        return False
    return process_start_ticks(int(pid)) == str(started)


__all__ = ["MAX_CAPTURED_CHARS", "MINIMAL_ENV", "is_alive", "kill_group", "process_start_ticks",
           "run_bounded"]
