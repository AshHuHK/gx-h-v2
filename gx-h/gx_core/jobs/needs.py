"""When a need is cleared: the three predicates the exec stream holds the facts for.

SCHEDULING_DESIGN r2.2 §2.8.2: a run pauses only on something a person or the machine can
clear, each kind of need has one clearing predicate, published by the stream that holds the
fact, and ``continue`` is refused while a need is uncleared. This stream holds three:

    readiness           the capability is ACTIVE at the planned sha, or the environment
                        reads HEALTHY
    device_capacity     a fresh capacity snapshot says the job can start
    path_reachability   the recorded host, mount and permission check passes

    cleared(conn, need) -> {"kind", "cleared": bool, "why": str, "facts": {...}, "observed_at": iso}

``need`` is a need of the closed vocabulary (``gx_core.scheduling.needs``): ``{kind,
subject, what, why, clear_by}``. What the predicate is asked about is its ``subject``:

    readiness           {"capability_version": "id@version", "manifest_sha256"?: str}
                          that version is ACTIVE (at that sha256, when one is named)
                        {"capability_id": "id"}
                          some version of it is ACTIVE: a repair is a version that supersedes
                        {"env_id": "…"}
                          its stored probe reads HEALTHY, made on this host
    device_capacity     {"capability_version": "id@version"}
                          a job of it would start now: not merely be admitted and queue,
                          because the need was recorded when the device stayed busy
    path_reachability   {"capability_version": "id@version"}
                          the program, weights, data and scripts its manifest names exist,
                          as the first rung of the ladder checks them
                        {"env_id": "…"}
                          its interpreter and binaries exist and may be executed
                        {"paths": [{"path": "/abs", "access": "read" | "execute" | "dir"}], "host"?: str}
                          each exists with that access; on the named host only

**Read-only, and no probe.** Nothing here runs a tool, probes an environment, starts a job
or writes a row; the one process started is ``nvidia-smi``, for ``device_capacity``. A
predicate answers from records and from the filesystem as they are, so it may be asked
inside a transaction and as often as a status view likes. What makes an uncleared need
clear is somebody's act (a repair, a probe, a person's change to the limits) or time (a
device that frees up); the predicate only says whether it has happened.

**A need this stream cannot answer is not cleared.** A subject that names nothing the
predicate knows, or a kind another stream holds, is a programming error and is raised as
one (``ValueError``): answering "cleared" for a question nobody understood would let a run
continue on a need that still stands.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from ..capabilities import environments, registry
from ..domain import clock
from ..errors import GxError

KINDS = ("readiness", "device_capacity", "path_reachability")
ACCESS = {"read": os.R_OK, "execute": os.X_OK, "dir": os.R_OK | os.X_OK}


def _answer(kind: str, ok: bool, why: str, **facts: Any) -> dict[str, Any]:
    return {"kind": kind, "cleared": bool(ok), "why": why, "facts": facts, "observed_at": clock.now_iso()}


def _subject(need: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    if need.get("kind") != kind:
        raise ValueError(f"this predicate clears a {kind} need, not {need.get('kind')!r}")
    subject = need.get("subject")
    if not isinstance(subject, Mapping) or not subject:
        raise ValueError(f"a {kind} need names its subject; this one names none, so nothing can be said to "
                         "have cleared")
    return subject


# ------------------------------------------------------------------------------ readiness

def readiness_cleared(conn: sqlite3.Connection, need: Mapping[str, Any]) -> dict[str, Any]:
    """The capability is ACTIVE at the planned sha, or the environment reads HEALTHY."""
    subject = _subject(need, "readiness")
    if subject.get("capability_version"):
        wanted = subject.get("manifest_sha256")
        try:
            view = registry.candidate(conn, subject["capability_version"])
        except GxError as exc:
            return _answer("readiness", False, f"{exc.code}: {exc.message}",
                           capability_version=subject["capability_version"])
        facts = {"capability_version": view["capability_version"], "status": view["status"],
                 "manifest_sha256": view["manifest_sha256"], "planned_sha256": wanted}
        if view["status"] != registry.ACTIVE:
            return _answer("readiness", False, f"{view['capability_version']} is {view['status']}, not ACTIVE",
                           **facts)
        if wanted and wanted != view["manifest_sha256"]:
            return _answer("readiness", False, f"{view['capability_version']} is ACTIVE at sha256 "
                           f"{view['manifest_sha256'][:16]}…, and the plan names {str(wanted)[:16]}…", **facts)
        return _answer("readiness", True, f"{view['capability_version']} is ACTIVE"
                       + (" at the planned sha256" if wanted else ""), **facts)
    if subject.get("capability_id"):
        active = registry.active_versions(conn, subject["capability_id"])
        return _answer("readiness", bool(active),
                       (f"{', '.join(active)} is ACTIVE" if active else
                        f"no version of {subject['capability_id']} is ACTIVE"),
                       capability_id=subject["capability_id"], active_versions=active)
    if subject.get("env_id"):
        view = environments.readiness_view(conn, subject["env_id"])
        return _answer("readiness", view["ready"],
                       view["why"] or f"environment {subject['env_id']} reads {view['status']} by its probe "
                                      f"of {view['probed_at']} on this host",
                       env_id=subject["env_id"], status=view["status"], probed_at=view["probed_at"],
                       host=view["host"], same_host=view["same_host"])
    raise ValueError(f"a readiness need is about a capability_version, a capability_id or an env_id; this one "
                     f"names {sorted(subject)}")


# ------------------------------------------------------------------------- device capacity

def device_capacity_cleared(conn: sqlite3.Connection, need: Mapping[str, Any]) -> dict[str, Any]:
    """A fresh capacity snapshot says the job can start."""
    from . import capacity                                  # reads the machine; imported when asked

    subject = _subject(need, "device_capacity")
    if not subject.get("capability_version"):
        raise ValueError(f"a device_capacity need is about a capability_version; this one names {sorted(subject)}")
    fit = capacity.fits(conn, subject["capability_version"])
    facts = {"capability_version": subject["capability_version"], "can_start": fit["can_start"],
             "would_queue": fit["would_queue"], "ever_fits": fit["ever_fits"], "never": fit["never"],
             "until_changed": fit["until_changed"], "not_now": fit["not_now"], "needs": fit["needs"],
             "snapshot_at": fit["observed_at"]}
    if fit["can_start"]:
        why = "a job of it would start now"
    elif fit["would_queue"]:
        why = ("a job of it would be admitted and would wait for the device: the device is still in use, "
               "which is what this need was recorded for")
    else:
        why = "; ".join(fit["reasons"]) or "a job of it cannot start now"
    return _answer("device_capacity", fit["can_start"], why, **facts)


# ----------------------------------------------------------------------- path reachability

def _path_problems(items: Any) -> list[str]:
    found: list[str] = []
    if not isinstance(items, list) or not items:
        raise ValueError("a path_reachability need about paths lists them: [{path, access}]")
    for item in items:
        path, access = (item or {}).get("path"), (item or {}).get("access", "read")
        if not isinstance(path, str) or not os.path.isabs(path) or access not in ACCESS:
            raise ValueError(f"a path is {{'path': '/absolute', 'access': one of {sorted(ACCESS)}}}, not {item!r}")
        target = Path(path)
        if not target.exists():
            found.append(f"{path} does not exist (or the mount that holds it is not there)")
        elif access == "dir" and not target.is_dir():
            found.append(f"{path} is not a directory")
        elif access != "dir" and not target.is_file():
            found.append(f"{path} is not a file")
        elif not os.access(path, ACCESS[access]):
            found.append(f"{path} may not be used for {access} by this process")
    return found


def path_reachability_cleared(conn: sqlite3.Connection, need: Mapping[str, Any]) -> dict[str, Any]:
    """The recorded host, mount and permission check passes."""
    subject = _subject(need, "path_reachability")
    here = os.uname().nodename
    if subject.get("capability_version"):
        from ..capabilities import ladder                   # its first rung is this check

        try:
            view = registry.candidate(conn, subject["capability_version"])
        except GxError as exc:
            return _answer("path_reachability", False, f"{exc.code}: {exc.message}", host=here,
                           capability_version=subject["capability_version"])
        result, detail = ladder.probe_files(conn, view)
        missing = list(detail.get("missing") or ([detail["reason"]] if detail.get("reason") else []))
        return _answer("path_reachability", result == ladder.PASS,
                       ("every file the capability names is there" if result == ladder.PASS
                        else "; ".join(missing)[:1000]),
                       host=here, capability_version=view["capability_version"], missing=missing,
                       program=detail.get("program"))
    if subject.get("env_id"):
        try:
            env = environments.get(conn, subject["env_id"])
        except GxError as exc:
            return _answer("path_reachability", False, f"{exc.code}: {exc.message}", host=here,
                           env_id=subject["env_id"])
        programs = [{"path": path, "access": "execute"} for path in
                    ([env["python_executable"]] if env.get("python_executable") else [])
                    + list((env.get("binary_paths") or {}).values())]
        missing = _path_problems(programs) if programs else ["the environment records no program"]
        return _answer("path_reachability", not missing,
                       "its interpreter and binaries are there and may be executed" if not missing
                       else "; ".join(missing)[:1000], host=here, env_id=subject["env_id"], missing=missing,
                       checked=len(programs))
    if "paths" in subject:
        wanted_host = subject.get("host")
        if wanted_host and wanted_host != here:
            return _answer("path_reachability", False,
                           f"the paths were recorded on host {wanted_host}; this is {here}, and what is "
                           "reachable there cannot be checked from here", host=here, recorded_host=wanted_host)
        missing = _path_problems(subject["paths"])
        return _answer("path_reachability", not missing,
                       "every path is there with the access asked for" if not missing
                       else "; ".join(missing)[:1000], host=here, missing=missing, checked=len(subject["paths"]))
    raise ValueError(f"a path_reachability need is about a capability_version, an env_id or paths; this one "
                     f"names {sorted(subject)}")


# ---------------------------------------------------------------------------------- entry

PREDICATES: dict[str, Callable[[sqlite3.Connection, Mapping[str, Any]], dict[str, Any]]] = {
    "readiness": readiness_cleared,
    "device_capacity": device_capacity_cleared,
    "path_reachability": path_reachability_cleared,
}


def cleared(conn: sqlite3.Connection, need: Mapping[str, Any]) -> dict[str, Any]:
    """Whether a need of a kind this stream holds has cleared. See the module docstring."""
    kind = need.get("kind") if isinstance(need, Mapping) else None
    if kind not in PREDICATES:
        raise ValueError(f"the exec stream clears needs of kinds {list(KINDS)}, not {kind!r}")
    return PREDICATES[kind](conn, need)


__all__ = ["ACCESS", "KINDS", "PREDICATES", "cleared", "device_capacity_cleared", "path_reachability_cleared",
           "readiness_cleared"]
