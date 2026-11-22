"""Waiting for a run: the reading half of a coordinator's event-driven loop.

``gx wait <run> --after <event id> --until <time>`` is what a coordinator runs in the
background when tasks are in flight and it has nothing to launch
(``docs/v2/SCHEDULING_DESIGN.md`` r2.2 §8.3). It ends when something happened that changes
what a coordinator can do, when the time given is reached, or when the run has ended; its
exit is what wakes the session. This module holds what "something happened" means, the
loop, and the record a running wait leaves for a host's stop gate (§8.6).

Three rules, each tested.

**It writes nothing to the store.** No row and no event. A wait that wrote an event of
the run would wake itself and every other waiter, and would move the pair the no-spin
rule compares (§2.5.4). What a running wait leaves is a file beside the store,
``runs/<run_id>/wait/<pid>.json``, removed when the wait ends. :func:`live_waits` reads
those files and counts a wait only while its process is alive and its time has not
passed, so a wait that was killed exempts nobody.

**What counts is data.** :data:`WAKE_ON` and :data:`WAKE_PREFIXES` name the events that
change what a coordinator can do: a task that exists or left a worker's hands, a run,
approval, workflow or schedule event, a job that ended, a hand-out that closed. A worker's
claim, bind and hand-in, and the rows its work writes on the way, are not among them: a
coordinator beside eight working workers would otherwise be woken at every step of each.
``any_event`` restores the literal rule (every event of the run). Events already above
``after`` when the wait starts wake it at once, so nothing that happened between the plan
and the wait is missed.

**It always ends.** With no time given it ends ``task_budget.coordinator_lease_seconds``
after it began, and a time further away than that is refused: a wait is an exemption from
a host's stop gate, and an unbounded one would be a standing exemption. A time already
past is no refusal -- a lease that lapsed between the plan and the wait is the ordinary
race, and what the coordinator does next is what it does after a wait that ran out.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import config
from ..artifacts.service import ArtifactService
from ..domain import clock, machines
from ..errors import ArtifactNotFound, SchemaInvalid
from . import seams

EVENT, TIME, ENDED = "event", "time", "ended"
WOKE = (EVENT, TIME, ENDED)

#: The first pause between two looks at the store, and the longest. Every look is a read
#: of an indexed column; a reader does not wait for a writer on a write-ahead log.
FIRST_PAUSE_SECONDS = 0.2
LONGEST_PAUSE_SECONDS = 1.0

#: How many distinct kinds of new events an answer names.
KINDS_SHOWN = 16


def _states(machine: str, names: tuple[str, ...]) -> tuple[str, ...]:
    known = machines.all_states(machine)
    unknown = [name for name in names if name not in known]
    if unknown:  # pragma: no cover - contract drift stops the import
        raise RuntimeError(f"{machine} state(s) {unknown} left the contract; gx wait names them")
    return names


#: Event kind -> the states its subject must have moved to for the event to count, or
#: None when every event of the kind counts. A task counts when it exists or has left a
#: worker's hands: it can be offered, it freed a slot, or it needs somebody.
WAKE_ON: dict[str, tuple[str, ...] | None] = {
    "task.created": None,
    "task.transition": _states("task", ("READY", "ACCEPTED", "REJECTED", "RETRYABLE", "BLOCKED", "CANCELLED")),
    "task.attempt_lapsed": None,
    "task.dispatch_failed": None,
    "task.release_failed": None,
    "task.bind_refused": None,
    "task.hold_changed": None,
    "job.transition": tuple(sorted(machines.terminal_states("job"))),
    "dispatch.closed": None,
}
#: Every kind under one of these counts: what happens to the run itself, a decision of a
#: person, a step of the workflow, a decision of the scheduler.
WAKE_PREFIXES: tuple[str, ...] = ("run.", "approval.", "workflow.", "schedule.")


def counts(kind: str, new_state: str | None, *, any_event: bool = False) -> bool:
    """Whether an event changes what a coordinator can do. Pure."""
    if any_event:
        return True
    if kind in WAKE_ON:
        states = WAKE_ON[kind]
        return states is None or new_state in states
    return kind.startswith(WAKE_PREFIXES)


def longest_wait_seconds() -> int:
    """How far away a wait's time may be: the length of a coordinator lease."""
    return int((config.limits().get("task_budget") or {}).get("coordinator_lease_seconds", 1800))


def end_time(until: str | None) -> str:
    """The time a wait ends at: the one given, or the longest allowed from now.

    A time further away than the longest wait is refused, as is one that is not a time.
    One already past is returned as it is: the wait then ends at once.
    """
    ceiling = longest_wait_seconds()
    if until is None:
        return clock.iso_after(ceiling)
    try:
        at = clock.parse(str(until))
    except (TypeError, ValueError) as exc:
        raise SchemaInvalid(
            f"--until {until!r} is not a time; give the plan's earliest_lease_expiry "
            "(ISO 8601, for example 2026-11-12T21:30:00.000Z)",
            details={"until": str(until)[:80]},
        ) from exc
    away = (at - clock.now()).total_seconds()
    if away > ceiling:
        raise SchemaInvalid(
            f"--until {until} is {int(away)} s away and a wait lasts at most {ceiling} s "
            "(task_budget.coordinator_lease_seconds): a longer one would be a standing exemption "
            "from the stop gate. Wait again when this one ends",
            details={"until": str(until)[:80], "seconds_away": int(away), "longest_seconds": ceiling},
        )
    return at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def look(conn: sqlite3.Connection, run_id: str, after: int, *, any_event: bool = False) -> dict[str, Any]:
    """The run's events above ``after``: how many, of which kinds, and those that count. Reads only."""
    rows = conn.execute(
        "SELECT event_id, kind, subject_kind, subject_id, new_state FROM events"
        " WHERE run_id=? AND event_id>? ORDER BY event_id", (run_id, int(after)),
    ).fetchall()
    counted = [row for row in rows if counts(row["kind"], row["new_state"], any_event=any_event)]
    kinds: list[str] = []
    for row in rows:
        if row["kind"] not in kinds:
            kinds.append(row["kind"])
    return {
        "last_event_id": int(rows[-1]["event_id"]) if rows else int(after),
        "new_events": len(rows),
        "kinds": kinds[:KINDS_SHOWN],
        "counted": [{key: row[key] for key in row.keys()} for row in counted[:20]],
    }


# ------------------------------------------------------------------ the record of a wait

def _process_start(pid: int) -> str | None:
    """When a process of this machine started, as the kernel numbers it; None if it is gone
    or cannot be read. Two processes that were given the same pid differ in this."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    # The second field is the command in parentheses and may hold spaces: split after it.
    fields = text[text.rfind(")") + 2:].split()
    return fields[19] if len(fields) > 19 else None


def _alive(record: Mapping[str, Any]) -> bool:
    """Whether the process that wrote a wait record is still that process, on this machine.

    A record from another machine cannot be looked up here and is taken to be alive until
    its time: the store is for one host at a time, and the time bounds what that can cost.
    """
    if record.get("host") != os.uname().nodename:
        return True
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    started = _process_start(pid)
    if started is not None:
        return record.get("process_start") in (None, started)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_dir(conn: sqlite3.Connection, run_id: str, *, create: bool) -> Path:
    return ArtifactService(conn).wait_dir(run_id, create=create)


def _records(conn: sqlite3.Connection, run_id: str) -> list[tuple[Path, dict[str, Any]]]:
    directory = _wait_dir(conn, run_id, create=False)
    found = []
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    except OSError:
        return []
    for name in names:
        path = directory / name
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            found.append((path, record))
    return found


def _is_live(record: Mapping[str, Any]) -> bool:
    try:
        passed = clock.is_past(str(record.get("until")))
    except (TypeError, ValueError):
        return False
    return not passed and _alive(record)


def live_waits(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """The waits of a run that are running now: ``[{pid, session_ref, after_event_id, until, started_at}]``.

    A wait is listed while its process is alive and its time has not passed. For a host's
    stop gate (design r2.2 §8.6): a coordinator that has a wait running may end its turn.
    Reads files beside the store and nothing else; a run with no wait directory has none.
    """
    return [
        {"pid": record.get("pid"), "session_ref": record.get("session_ref"),
         "after_event_id": record.get("after_event_id"), "until": record.get("until"),
         "started_at": record.get("started_at")}
        for _path, record in _records(conn, run_id) if _is_live(record)
    ]


def _write_record(conn: sqlite3.Connection, run_id: str, record: Mapping[str, Any]) -> Path:
    directory = _wait_dir(conn, run_id, create=True)
    for path, other in _records(conn, run_id):   # what earlier waits left behind when they were killed
        if not _is_live(other):
            try:
                path.unlink()
            except OSError:
                pass
    target = directory / f"{record['pid']}.json"
    temporary = directory / f".{record['pid']}.{os.getpid()}.part"
    temporary.write_text(json.dumps(dict(record), sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return target


# ------------------------------------------------------------------------------ the wait

def wait(
    conn: sqlite3.Connection, run_id: str, *, after: int | None = None, until: str | None = None,
    any_event: bool = False, warnings: list[str] | None = None, record: bool = True,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Wait for the run and say what ended the wait. Writes nothing to the store.

    Returns ``{run_id, woke, after_event_id, last_event_id, new_events, kinds, until}``:
    ``woke`` is ``event`` (an event above ``after`` that counts), ``time`` (``until`` was
    reached) or ``ended`` (the run is in a terminal state and nothing new is above
    ``after``). ``last_event_id`` is the run's last event at that moment, whatever its
    kind: the next wait's ``after``. ``new_events`` and ``kinds`` describe every event
    above ``after``, counted or not.
    """
    notes = warnings if warnings is not None else []
    row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
    if after is None:
        after = int(conn.execute("SELECT COALESCE(MAX(event_id), 0) AS n FROM events WHERE run_id=?",
                                 (run_id,)).fetchone()["n"])
        notes.append(f"no --after was given: waiting for events after {after}, the run's last event now; "
                     "an event between the plan and this call is not seen")
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise SchemaInvalid("--after is an event id: a whole number, 0 or more", details={"after": str(after)[:40]})
    ends_at = end_time(until)

    written: Path | None = None
    if record:
        try:
            written = _write_record(conn, run_id, {
                "pid": os.getpid(), "process_start": _process_start(os.getpid()),
                "host": os.uname().nodename, "session_ref": seams.launch_session(),
                "after_event_id": after, "until": ends_at, "started_at": clock.now_iso(),
            })
        except OSError as exc:
            notes.append(f"this wait is not on record ({type(exc).__name__}: {exc}); a host's stop gate "
                         "cannot see it")
    try:
        pause = FIRST_PAUSE_SECONDS
        while True:
            seen = look(conn, run_id, after, any_event=any_event)
            if seen["counted"]:
                woke = EVENT
                break
            status = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()["status"]
            if status in machines.terminal_states("run"):
                woke = ENDED
                break
            left = (clock.parse(ends_at) - clock.now()).total_seconds()
            if left <= 0:
                woke = TIME
                break
            (sleep if sleep is not None else time.sleep)(min(pause, left))
            pause = min(pause * 2, LONGEST_PAUSE_SECONDS)
    finally:
        if written is not None:
            try:
                written.unlink()
            except OSError:
                pass
    return {"run_id": run_id, "woke": woke, "after_event_id": after, "last_event_id": seen["last_event_id"],
            "new_events": seen["new_events"], "kinds": seen["kinds"], "until": ends_at}


__all__ = ["ENDED", "EVENT", "TIME", "WAKE_ON", "WAKE_PREFIXES", "WOKE", "counts", "end_time", "live_waits",
           "longest_wait_seconds", "look", "wait"]
