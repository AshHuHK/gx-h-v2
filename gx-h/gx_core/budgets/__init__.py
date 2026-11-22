"""Budgets: what a run may spend, what is held for whom, and what was actually used.

Three rows of bookkeeping and one rule.

* A **budget** has limit lines (``limits``), what was used (``spent``) and what is held
  for work that has been accepted and not finished (``reserved``). What is left of a line
  is ``limit - spent - reserved``.
* A **reservation** holds part of a budget for one subject until it is settled with what
  the subject really used, or released unused. The hold is what makes two planners, or two
  jobs, unable to promise the same minute twice.
* A **child budget** is a part of a parent carved out for one subject (a path). Its
  funding reservation in the parent is named ``fund:<child budget_ref>``, so the link
  cannot drift; work inside the child reserves against the child, and closing the child
  charges the parent what the child spent and frees the rest.
* An optional **project phase** supplies additional limits across a project's run budgets.
  Its recorded baseline preserves earlier charges. Admission reads the same balances
  used by settlement, including live work inside open children.

The rule: **a resource with no limit line cannot be reserved.** The first build treated a
missing line as "unlimited", so a run whose budget had no ``gpu_minutes`` line ran a GPU job
and charged nothing (review models-eval, G1 n). Here an absent line is a refusal that names
the line.

Nothing in this module is told a number by a caller that it then believes: ``settle`` is
handed what was measured and charges that, including when it is more than was held.

Reservation states are local to this module (``budget_reservations.status`` has no label
axis in the frozen contract): ``HELD``, ``SETTLED``, ``RELEASED``.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Mapping

from .. import config
from ..domain import clock, ids
from ..errors import (
    ApprovalRequired,
    ArtifactNotFound,
    BudgetExceeded,
    IdempotencyConflict,
    IllegalTransition,
    SchemaInvalid,
)
from ..persistence import store
from ..registry import Subject
from .lines import DIGITS, clean as _line_clean
from .lines import line

HELD = "HELD"
SETTLED = "SETTLED"
RELEASED = "RELEASED"
RESERVATION_STATES = (HELD, SETTLED, RELEASED)

#: Prefix of the reservation that funds a child budget. ``fund:<child budget_ref>``.
FUND_PREFIX = "fund:"

DEFAULT_PROFILE = "default"


# ----------------------------------------------------------------------------- numbers

def _amounts(raw: Any, *, what: str) -> dict[str, float]:
    """Validate a mapping of resource -> non-negative finite number.

    A negative amount would *add* to what is left, a boolean is an int in Python, and NaN
    compares false against every limit: each is a way to reserve nothing and be believed.
    """
    if not isinstance(raw, Mapping) or not raw:
        raise SchemaInvalid(
            f"{what} must be a non-empty object of resource -> amount",
            details={"got": type(raw).__name__},
        )
    out: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise SchemaInvalid(f"{what} has an empty resource name", details={"key": repr(key)})
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaInvalid(
                f"{what}.{key} must be a number, got {type(value).__name__}",
                details={"key": key},
            )
        if not math.isfinite(value) or value < 0:
            raise SchemaInvalid(
                f"{what}.{key}={value!r} must be finite and not negative",
                details={"key": key, "value": repr(value)},
            )
        out[key] = float(value) if isinstance(value, float) else int(value)
    return out


def check_amounts(raw: Any, *, what: str = "amounts") -> dict[str, float]:
    """A mapping of resource -> amount as this module accepts it, or ``SCHEMA_INVALID``."""
    return _amounts(raw, what=what)


def _row(conn: sqlite3.Connection, budget_ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM budgets WHERE budget_ref=?", (budget_ref,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no such budget {budget_ref!r}", details={"budget_ref": budget_ref})
    return row


def _reservation(conn: sqlite3.Connection, reservation_ref: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM budget_reservations WHERE reservation_ref=?", (reservation_ref,)
    ).fetchone()
    if row is None:
        raise ArtifactNotFound(
            f"no such budget reservation {reservation_ref!r}",
            details={"reservation_ref": reservation_ref},
        )
    return row


def _write(conn: sqlite3.Connection, budget_ref: str, *, spent: Mapping[str, Any] | None = None,
           reserved: Mapping[str, Any] | None = None, limits: Mapping[str, Any] | None = None) -> None:
    sets, params = ["updated_at=?"], [clock.now_iso()]
    for column, value in (("spent_json", spent), ("reserved_json", reserved), ("limits_json", limits)):
        if value is not None:
            sets.append(f"{column}=?")
            params.append(ids.canonical_json(dict(value)))
    params.append(budget_ref)
    conn.execute(f"UPDATE budgets SET {', '.join(sets)} WHERE budget_ref=?", params)


def _clean(value: float) -> float | int:
    """Keep integers integral so a ledger reads 3, not 3.0000000000000004 (:func:`gx_core.budgets.lines.clean`)."""
    return _line_clean(value)


def _available(limits: Mapping[str, Any], spent: Mapping[str, Any], reserved: Mapping[str, Any]) -> dict[str, Any]:
    """What is left of each line, read the one way every reader reads a line (:func:`gx_core.budgets.lines.line`).

    A switch kept beside the lines (a bool) is not a line and has nothing left of it; any other
    limit that is not a number is refused, as a line nobody can read.
    """
    return {key: line(limit, spent.get(key, 0), reserved.get(key, 0))["available"]
            for key, limit in limits.items() if not isinstance(limit, bool)}


# ----------------------------------------------------------------------- project phases

PROJECT_LINES = ("job_submissions", "gpu_jobs", "gpu_minutes", "cpu_seconds", "worker_tasks")
_PHASE_EVENT = "budget.project_phase"


def check_project_limits(raw: Any) -> dict[str, int]:
    """Validate the optional gx-project budget file's five resource allowances."""
    if not isinstance(raw, Mapping) or set(raw) != set(PROJECT_LINES):
        raise SchemaInvalid("project budget must contain exactly " + ", ".join(PROJECT_LINES))
    for key in PROJECT_LINES:
        if type(raw[key]) is not int or raw[key] < 0:
            raise SchemaInvalid(f"project budget {key} must be a nonnegative integer")
    return {key: raw[key] for key in PROJECT_LINES}


def _project_usage(conn: sqlite3.Connection, project_ref: str) -> tuple[dict, dict]:
    """Read each root once, including qualification roots whose budgets.run_id is NULL.

    A funded child's actual charges reach its parent's spent balance only on closure.
    Until then include its balances and cancel the corresponding internal funding hold.
    The remaining reservations cover actual work, including outstanding chain followers.
    """
    balances = conn.execute("""
        WITH RECURSIVE live(ref, funding) AS (
            SELECT budget_ref, NULL FROM budgets WHERE parent_ref IS NULL
            AND budget_ref IN (SELECT budget_ref FROM runs WHERE project_ref=?)
            UNION ALL
            SELECT child.budget_ref, f.amounts_json FROM live
            JOIN budgets child ON child.parent_ref=live.ref
            JOIN budget_reservations f ON f.reservation_ref=? || child.budget_ref
            WHERE f.budget_ref=live.ref AND f.status=?
        )
        SELECT b.spent_json, b.reserved_json, live.funding FROM live
        JOIN budgets b ON b.budget_ref=live.ref
        """, (project_ref, FUND_PREFIX, HELD))
    spent = dict.fromkeys(PROJECT_LINES, 0)
    reserved = dict.fromkeys(PROJECT_LINES, 0)
    for budget in balances:
        used = store.loads(budget, "spent_json", {})
        held = store.loads(budget, "reserved_json", {})
        funding = store.loads(budget, "funding", {})
        for key in PROJECT_LINES:
            spent[key] += used.get(key, 0)
            reserved[key] += held.get(key, 0) - funding.get(key, 0)
    return ({key: round(amount, DIGITS) for key, amount in spent.items()},
            {key: round(amount, DIGITS) for key, amount in reserved.items()})


def project_phase_event(conn: sqlite3.Connection, project_ref: str) -> sqlite3.Row | None:
    """The active envelope's activation event also marks its exact ledger boundary."""
    return conn.execute(
        "SELECT event_id,payload_json FROM events WHERE subject_kind='project_budget' AND subject_id=?"
        " AND kind=? ORDER BY event_id DESC LIMIT 1", (project_ref, _PHASE_EVENT)).fetchone()


def _current_phase(conn: sqlite3.Connection, project_ref: str) -> dict | None:
    row = project_phase_event(conn, project_ref)
    return store.loads(row, "payload_json", {}) if row else None


def _phase_state(project_ref: str, phase: dict, cumulative: dict, reserved: dict) -> dict:
    spent = {key: round(cumulative[key] - phase["baseline_spent"][key], DIGITS) for key in PROJECT_LINES}
    available = {key: round(phase["limits"][key] - spent[key] - reserved[key], DIGITS) for key in PROJECT_LINES}
    return {"project_ref": project_ref, **phase, "cumulative_spent": cumulative,
            "spent": spent, "reserved": reserved, "available": available}


def project_budget_state(conn: sqlite3.Connection, project_ref: str) -> dict | None:
    """Read the current envelope and ledger in one snapshot; unused projects return None."""
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        phase = _current_phase(conn, project_ref)
        return _phase_state(project_ref, phase, *_project_usage(conn, project_ref)) if phase else None
    finally:
        if own_snapshot:
            conn.rollback()


def activate_project_budget(conn: sqlite3.Connection, project_ref: str, limits: Mapping[str, Any],
                            *, phase_id: str) -> dict:
    """Apply one phase's additional allowance, reusing its original baseline on replay."""
    checked = check_project_limits(limits)
    if not isinstance(project_ref, str) or not project_ref.strip() or project_ref == "*":
        raise SchemaInvalid("a project budget requires one explicit project")
    if not isinstance(phase_id, str) or not phase_id.strip():
        raise SchemaInvalid("a project budget requires a phase identifier")
    with store.transaction(conn) as txn:
        current = _current_phase(txn, project_ref)
        cumulative, held = _project_usage(txn, project_ref)
        if current and current["phase_id"] == phase_id:
            if current["limits"] != checked:
                raise IdempotencyConflict(f"project budget phase {phase_id!r} already has other limits")
            return _phase_state(project_ref, current, cumulative, held)
        prior = txn.execute(
            "SELECT 1 FROM events WHERE subject_kind='project_budget' AND subject_id=? AND kind=?"
            " AND json_extract(payload_json,'$.phase_id')=? LIMIT 1",
            (project_ref, _PHASE_EVENT, phase_id)).fetchone()
        if prior:
            raise IdempotencyConflict(f"project budget phase {phase_id!r} was superseded")
        for key, limit in checked.items():
            if held[key] > limit:
                raise BudgetExceeded(f"project {project_ref} already holds {held[key]} {key}, above {limit}",
                                     details={"project_ref": project_ref, "phase_id": phase_id,
                                              "key": key, "reserved": held[key], "limit": limit})
        phase = {"phase_id": phase_id, "limits": checked, "baseline_spent": cumulative}
        store.append_event(txn, kind=_PHASE_EVENT, actor="gx-project", subject_kind="project_budget",
                           subject_id=project_ref, payload=phase)
        return _phase_state(project_ref, phase, cumulative, held)


def _check_project_reservation(conn: sqlite3.Connection, budget_ref: str, amounts: Mapping[str, Any]) -> None:
    if conn.execute("SELECT 1 FROM events WHERE subject_kind='project_budget' LIMIT 1").fetchone() is None:
        return
    # A run carries its budget reference even when the qualification budget has no run_id.
    projects = conn.execute(
        "WITH RECURSIVE lineage(ref,parent) AS ("
        " SELECT budget_ref,parent_ref FROM budgets WHERE budget_ref=? UNION ALL"
        " SELECT b.budget_ref,b.parent_ref FROM budgets b JOIN lineage ON b.budget_ref=lineage.parent"
        ") SELECT DISTINCT project_ref FROM runs JOIN lineage ON runs.budget_ref=lineage.ref"
        " WHERE lineage.parent IS NULL", (budget_ref,))
    for project in projects:
        state = project_budget_state(conn, project["project_ref"])
        if state is None:
            continue
        short = [{"key": key, "needed": amount, "requested": amount, "available": state["available"][key],
                  "limit": state["limits"][key], "spent": state["spent"][key], "reserved": state["reserved"][key]}
                 for key, amount in amounts.items()
                 if key in state["limits"] and amount > 0 and amount > state["available"][key]]
        if short:
            item = short[0]
            raise BudgetExceeded(
                f"project {state['project_ref']} phase {state['phase_id']}: {item['key']} needs {item['needed']}, "
                f"{item['available']} left", details={"project_ref": state["project_ref"],
                "phase_id": state["phase_id"], "budget_ref": budget_ref, "shortfalls": short})


# ----------------------------------------------------------------------------- profiles

def profile(name: str = DEFAULT_PROFILE) -> dict[str, Any]:
    """The limit lines of a named budget profile, read from ``config/limits.json`` now.

    ``budget_profiles`` is the one home of what a run may spend. A profile that is not
    there is ``SCHEMA_INVALID``: nothing is derived in its place, because a computed
    default is a number nobody chose. Two relations the file states in words are checked
    on every load, so an edit that breaks one is caught here and not at the fortieth
    minute of a GPU job: ``gpu_jobs`` never exceeds
    ``research_budget.automatic_gpu_job_budget``, and ``gpu_minutes`` does not exceed
    ``gpu_jobs`` jobs at ``gpu.max_wall_minutes`` each. Launches and minutes are
    independent limits: replacing a failed launch need not buy extra GPU time.
    """
    limits_file = config.limits()
    table = limits_file.get("budget_profiles")
    if not isinstance(table, Mapping):
        raise SchemaInvalid(
            "config/limits.json has no budget_profiles table; a run's budget is read from "
            "it and is never computed in its absence",
            details={"profile": name},
        )
    entry = table.get(name)
    if not isinstance(entry, Mapping):
        raise SchemaInvalid(
            f"no budget profile {name!r} in config/limits.json; "
            f"known: {sorted(k for k, v in table.items() if isinstance(v, Mapping))}",
            details={"profile": name},
        )
    lines = _amounts(entry, what=f"budget_profiles.{name}")
    gpu_jobs_ceiling = int(limits_file["research_budget"]["automatic_gpu_job_budget"])
    minutes_per_job = int(limits_file["gpu"]["max_wall_minutes"])
    gpu_jobs = lines.get("gpu_jobs", 0)
    gpu_minutes = lines.get("gpu_minutes", 0)
    problems = []
    if gpu_jobs != int(gpu_jobs):
        problems.append(f"gpu_jobs {gpu_jobs} is not an integer launch count")
    if gpu_jobs > gpu_jobs_ceiling:
        problems.append(
            f"gpu_jobs {gpu_jobs} exceeds research_budget.automatic_gpu_job_budget {gpu_jobs_ceiling}")
    if gpu_minutes > gpu_jobs * minutes_per_job:
        problems.append(
            f"gpu_minutes {gpu_minutes} exceeds gpu_jobs {gpu_jobs} x gpu.max_wall_minutes "
            f"{minutes_per_job} = {gpu_jobs * minutes_per_job}")
    if problems:
        raise SchemaInvalid(
            f"budget profile {name!r} of config/limits.json contradicts the limits it rests on: "
            + "; ".join(problems),
            details={"profile": name, "problems": problems},
        )
    return {"profile": name, "limits": lines, "source": "config/limits.json budget_profiles"}


# ------------------------------------------------------------------------------ create

def create_root(conn: sqlite3.Connection, *, run_id: str | None, profile_name: str = DEFAULT_PROFILE,
                actor: str, limits: Mapping[str, Any] | None = None) -> str:
    """Create the root budget of a run from a profile. Returns its ``budget_ref``.

    ``limits`` may only *tighten* the profile: a line may be lowered or left out (a line
    left out is a resource the run may not use), never raised and never invented.
    """
    spec = profile(profile_name)
    lines = dict(spec["limits"])
    if limits is not None:
        requested = _amounts(limits, what="limits")
        for key, value in requested.items():
            if key not in lines:
                raise SchemaInvalid(
                    f"limit line {key!r} is not in budget profile {profile_name!r}; a caller "
                    "may tighten a profile, not add to it",
                    details={"key": key, "profile": profile_name},
                )
            if value > lines[key]:
                raise BudgetExceeded(
                    f"limit {key}={value} is above the {profile_name!r} profile's {lines[key]}; "
                    "raising a ceiling needs a budget_increase approval",
                    details={"key": key, "requested": value, "ceiling": lines[key]},
                )
        lines = requested
    budget_ref = ids.new_id("budget")
    now = clock.now_iso()
    with store.transaction(conn) as txn:
        txn.execute(
            "INSERT INTO budgets(budget_ref, parent_ref, run_id, limits_json, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (budget_ref, None, run_id, ids.canonical_json(lines), now, now),
        )
        store.append_event(
            txn, kind="budget.created", actor=actor, run_id=run_id, subject_kind="budget",
            subject_id=budget_ref, evidence_level="EXECUTION_OBSERVED",
            payload={"profile": profile_name, "limits": lines, "profile_source": spec["source"]},
        )
    return budget_ref


def open_child(conn: sqlite3.Connection, *, parent_ref: str, subject_kind: str, subject_id: str,
               limits: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """Carve a child budget out of ``parent_ref`` for one subject.

    The child's limits are held in the parent by the reservation ``fund:<child>``. The sum
    of every child therefore stays inside the parent: the refusal is on the total of an
    expansion, not on each branch considered alone.
    """
    lines = _amounts(limits, what="limits")
    with store.transaction(conn) as txn:
        parent = _row(txn, parent_ref)
        child_ref = ids.new_id("budget")
        reservation = _reserve(
            txn, parent, reservation_ref=FUND_PREFIX + child_ref, subject_kind=subject_kind,
            subject_id=subject_id, amounts=lines, actor=actor,
        )
        now = clock.now_iso()
        txn.execute(
            "INSERT INTO budgets(budget_ref, parent_ref, run_id, limits_json, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (child_ref, parent_ref, parent["run_id"], ids.canonical_json(lines), now, now),
        )
        store.append_event(
            txn, kind="budget.child_opened", actor=actor, run_id=parent["run_id"],
            subject_kind="budget", subject_id=child_ref, evidence_level="EXECUTION_OBSERVED",
            payload={"parent_ref": parent_ref, "limits": lines, "for": [subject_kind, subject_id],
                     "reservation_ref": reservation["reservation_ref"]},
        )
    return {"budget_ref": child_ref, "parent_ref": parent_ref, "limits": lines,
            "reservation_ref": reservation["reservation_ref"]}


# ----------------------------------------------------------------------------- reserve

def _funding_state(conn: sqlite3.Connection, budget_ref: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM budget_reservations WHERE reservation_ref=?",
        (FUND_PREFIX + budget_ref,),
    ).fetchone()
    return None if row is None else row["status"]


def _reserve(conn: sqlite3.Connection, budget: sqlite3.Row, *, reservation_ref: str | None,
             subject_kind: str, subject_id: str, amounts: Mapping[str, Any], actor: str) -> dict[str, Any]:
    budget_ref = budget["budget_ref"]
    if not subject_kind or not subject_id:
        raise SchemaInvalid("a reservation names its subject", details={})
    if budget["parent_ref"] is not None and _funding_state(conn, budget_ref) != HELD:
        raise BudgetExceeded(
            f"budget {budget_ref} was closed: the hold that funded it in {budget['parent_ref']} "
            f"is {_funding_state(conn, budget_ref)}, so nothing more can be reserved from it",
            details={"budget_ref": budget_ref, "funding": _funding_state(conn, budget_ref)},
        )
    existing = conn.execute(
        "SELECT * FROM budget_reservations WHERE budget_ref=? AND subject_kind=? AND subject_id=?"
        " AND status=?",
        (budget_ref, subject_kind, subject_id, HELD),
    ).fetchone()
    if existing is not None:
        held = store.loads(existing, "amounts_json", {})
        if held == dict(amounts):
            return {"reservation_ref": existing["reservation_ref"], "budget_ref": budget_ref,
                    "amounts": held, "status": HELD, "reused": True}
        raise IdempotencyConflict(
            f"{subject_kind} {subject_id} already holds a reservation in {budget_ref} for other "
            "amounts; settle or release it before reserving again",
            details={"reservation_ref": existing["reservation_ref"], "held": held,
                     "requested": dict(amounts)},
        )

    limits = store.loads(budget, "limits_json", {})
    spent = store.loads(budget, "spent_json", {})
    reserved = store.loads(budget, "reserved_json", {})
    shortfalls: list[dict[str, Any]] = []
    for key, requested in amounts.items():
        if key not in limits:
            shortfalls.append({"key": key, "requested": requested, "limit": None,
                               "why": "the budget has no line for this resource"})
            continue
        left = limits[key] - spent.get(key, 0) - reserved.get(key, 0)
        if requested > left:
            shortfalls.append({"key": key, "requested": requested, "limit": limits[key],
                               "spent": spent.get(key, 0), "reserved": reserved.get(key, 0),
                               "available": _clean(left), "why": "not enough left"})
    if shortfalls:
        first = shortfalls[0]
        if first["limit"] is None:
            message = (f"budget {budget_ref} has no {first['key']!r} line, so {first['key']} cannot "
                       "be reserved from it; an absent line is a refusal, not an unlimited resource")
        else:
            message = (f"budget {budget_ref}: {first['key']}={first['requested']} requested, "
                       f"{first['available']} left of {first['limit']}")
        raise BudgetExceeded(message, details={"budget_ref": budget_ref, "shortfalls": shortfalls,
                                               "subject": [subject_kind, subject_id]})

    if reservation_ref is None:
        _check_project_reservation(conn, budget_ref, amounts)
    for key, requested in amounts.items():
        reserved[key] = _clean(reserved.get(key, 0) + requested)
    _write(conn, budget_ref, reserved=reserved)
    ref = reservation_ref or f"rsv_{ids.new_id('budget')[4:]}"
    conn.execute(
        "INSERT INTO budget_reservations(reservation_ref, budget_ref, subject_kind, subject_id,"
        " amounts_json, status, created_at) VALUES(?,?,?,?,?,?,?)",
        (ref, budget_ref, subject_kind, subject_id, ids.canonical_json(dict(amounts)), HELD,
         clock.now_iso()),
    )
    store.append_event(
        conn, kind="budget.reserved", actor=actor, run_id=budget["run_id"], subject_kind="budget",
        subject_id=budget_ref, new_state=HELD, evidence_level="EXECUTION_OBSERVED",
        payload={"reservation_ref": ref, "for": [subject_kind, subject_id], "amounts": dict(amounts)},
    )
    return {"reservation_ref": ref, "budget_ref": budget_ref, "amounts": dict(amounts),
            "status": HELD, "reused": False}


def reserve(conn: sqlite3.Connection, *, budget_ref: str, subject_kind: str, subject_id: str,
            amounts: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """Hold ``amounts`` of ``budget_ref`` for one subject, or refuse with ``BUDGET_EXCEEDED``.

    Every requested resource must have a limit line with enough left after what is spent
    and what is already held. All shortfalls are listed, not only the first. The same
    subject asking again for the same amounts gets the reservation it already holds.
    """
    checked = _amounts(amounts, what="amounts")
    with store.transaction(conn) as txn:
        return _reserve(txn, _row(txn, budget_ref), reservation_ref=None,
                        subject_kind=subject_kind, subject_id=subject_id, amounts=checked,
                        actor=actor)


def spend(conn: sqlite3.Connection, *, budget_ref: str, subject_kind: str, subject_id: str,
          amounts: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """Charge something that is used the moment it is granted (a worker task, a submission).

    A reserve and a settle in one unit, leaving the same record a two-step charge leaves.
    """
    checked = _amounts(amounts, what="amounts")
    with store.transaction(conn) as txn:
        held = _reserve(txn, _row(txn, budget_ref), reservation_ref=None, subject_kind=subject_kind,
                        subject_id=subject_id, amounts=checked, actor=actor)
        return settle(txn, held["reservation_ref"], actual=checked, actor=actor)


# ------------------------------------------------------------------------------- chains
#
# Work that opens a chain — a research round whose extraction must be reviewed by another
# worker — is admitted whole or not at all. The head is charged when it is created, and
# every mandatory follower is held at the same moment, in the same unit, under a key that
# names the head and the follower's place in the chain. A follower created later draws on
# its own hold, so it can never be refused for want of budget; a chain that cannot be held
# whole is refused before its head exists. A chain given up returns what its followers held.

CHAIN_KIND = "chain_follower"


def chain_key(head_subject_id: str, follower: str) -> str:
    """The subject a follower's hold is filed under: the head it follows and its name in the chain."""
    if not head_subject_id or not follower or ":" in follower:
        raise SchemaInvalid("a chain follower is named by its head and a name without ':'",
                            details={"head": head_subject_id, "follower": follower})
    return f"{head_subject_id}:{follower}"


def reserve_chain(conn: sqlite3.Connection, *, budget_ref: str, head_kind: str, head_id: str,
                  head_amounts: Mapping[str, Any], followers: Mapping[str, Mapping[str, Any]],
                  actor: str) -> dict[str, Any]:
    """Charge a chain's head and hold each mandatory follower, all in one unit or nothing.

    ``followers`` maps a follower's name in the chain (``"review"``) to what it will cost.
    The whole chain is checked against what is left before anything is written, and a
    refusal lists every short line with what the whole chain needed. Asked again for the
    same head and the same followers, it answers what it holds and writes nothing.
    """
    head = _amounts(head_amounts, what="head_amounts")
    wanted = {name: _amounts(amounts, what=f"followers.{name}") for name, amounts in followers.items()}
    keys = {name: chain_key(head_id, name) for name in wanted}
    with store.transaction(conn) as txn:
        budget = _row(txn, budget_ref)
        previous = txn.execute(
            "SELECT * FROM budget_reservations WHERE budget_ref=? AND subject_kind=? AND subject_id=?",
            (budget_ref, head_kind, head_id)).fetchone()
        if previous is not None:
            held = {name: txn.execute(
                "SELECT reservation_ref, amounts_json, status FROM budget_reservations WHERE budget_ref=?"
                " AND subject_kind=? AND subject_id=? ORDER BY rowid DESC LIMIT 1",
                (budget_ref, CHAIN_KIND, key)).fetchone() for name, key in keys.items()}
            same = (store.loads(previous, "amounts_json", {}) == head
                    and all(row is not None and store.loads(row, "amounts_json", {}) == wanted[name]
                            for name, row in held.items()))
            if not same:
                raise IdempotencyConflict(
                    f"{head_kind} {head_id} already opened a chain in {budget_ref} with other figures",
                    details={"budget_ref": budget_ref, "head": [head_kind, head_id]})
            return {"budget_ref": budget_ref, "head": previous["reservation_ref"], "reused": True,
                    "followers": {name: {"reservation_ref": row["reservation_ref"], "subject_id": keys[name],
                                         "status": row["status"]} for name, row in held.items()}}
        total: dict[str, float] = dict(head)
        for amounts in wanted.values():
            for key, value in amounts.items():
                total[key] = total.get(key, 0) + value
        limits = store.loads(budget, "limits_json", {})
        left = _available(limits, store.loads(budget, "spent_json", {}), store.loads(budget, "reserved_json", {}))
        short = [{"key": key, "needed": _clean(value), "available": left.get(key),
                  "why": "the budget has no line for this resource" if key not in limits else "not enough left"}
                 for key, value in sorted(total.items()) if key not in limits or value > left[key]]
        if short:
            raise BudgetExceeded(
                f"budget {budget_ref} cannot hold the whole chain that {head_kind} {head_id} opens: "
                f"{short[0]['key']} needs {short[0]['needed']}, {short[0]['available']} left. A chain is "
                "admitted whole or not at all",
                details={"budget_ref": budget_ref, "chain_total": {k: _clean(v) for k, v in total.items()},
                         "shortfalls": short, "followers": sorted(wanted)})
        charged = _reserve(txn, budget, reservation_ref=None, subject_kind=head_kind, subject_id=head_id,
                           amounts=head, actor=actor)
        _settle(txn, _reservation(txn, charged["reservation_ref"]), head, actor=actor)
        out = {}
        for name, amounts in wanted.items():
            held = _reserve(txn, _row(txn, budget_ref), reservation_ref=None, subject_kind=CHAIN_KIND,
                            subject_id=keys[name], amounts=amounts, actor=actor)
            out[name] = {"reservation_ref": held["reservation_ref"], "subject_id": keys[name], "status": HELD}
        return {"budget_ref": budget_ref, "head": charged["reservation_ref"], "followers": out, "reused": False}


def draw_follower(conn: sqlite3.Connection, *, budget_ref: str, head_id: str, follower: str,
                  subject_kind: str, subject_id: str, actor: str) -> dict[str, Any]:
    """Charge a follower, when it is created, from the hold its chain made for it.

    ``ARTIFACT_NOT_FOUND`` when no chain holds that follower: then it was never admitted
    with a chain and is charged like any other work, by the caller, with :func:`spend`.
    """
    key = chain_key(head_id, follower)
    with store.transaction(conn) as txn:
        row = txn.execute(
            "SELECT * FROM budget_reservations WHERE budget_ref=? AND subject_kind=? AND subject_id=?"
            " ORDER BY rowid DESC LIMIT 1", (budget_ref, CHAIN_KIND, key)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no chain in {budget_ref} holds {follower!r} for {head_id}",
                                   details={"budget_ref": budget_ref, "chain_key": key})
        drawn = txn.execute(
            "SELECT payload_json FROM events WHERE kind='budget.drawn' AND subject_kind='budget' AND subject_id=?"
            " AND json_extract(payload_json, '$.reservation_ref')=?", (budget_ref, row["reservation_ref"])).fetchone()
        if row["status"] == SETTLED and drawn is not None:
            by = store.loads(drawn, "payload_json", {}).get("for")
            if by == [subject_kind, subject_id]:
                return {"reservation_ref": row["reservation_ref"], "budget_ref": budget_ref, "status": SETTLED,
                        "for": by, "reused": True}
            raise IdempotencyConflict(f"the hold for {follower!r} of {head_id} was drawn by {by}",
                                      details={"chain_key": key, "drawn_by": by})
        if row["status"] != HELD:
            raise IllegalTransition(f"the hold for {follower!r} of {head_id} is {row['status']}: the chain was "
                                    "given up", details={"chain_key": key, "status": row["status"]})
        amounts = store.loads(row, "amounts_json", {})
        settled = _settle(txn, row, amounts, actor=actor)
        store.append_event(
            txn, kind="budget.drawn", actor=actor, run_id=_row(txn, budget_ref)["run_id"], subject_kind="budget",
            subject_id=budget_ref, evidence_level="EXECUTION_OBSERVED",
            payload={"reservation_ref": row["reservation_ref"], "chain_key": key, "for": [subject_kind, subject_id],
                     "amounts": amounts})
        return {**settled, "for": [subject_kind, subject_id]}


def draw_reservation(conn: sqlite3.Connection, reservation_ref: str, *, subject_kind: str, subject_id: str,
                     actor: str) -> dict[str, Any]:
    """:func:`draw_follower` by the hold's own reference, as ``create_task`` passes it (r2 §4.6).

    Refuses (``ARTIFACT_NOT_FOUND``) a reference that is not a chain's hold for a follower, so an
    ordinary reservation cannot be drawn as if a chain had made it.
    """
    row = conn.execute("SELECT budget_ref, subject_kind, subject_id FROM budget_reservations WHERE reservation_ref=?",
                       (reservation_ref,)).fetchone()
    if row is None or row["subject_kind"] != CHAIN_KIND or ":" not in str(row["subject_id"]):
        raise ArtifactNotFound(f"{reservation_ref} is not a chain's hold for a follower",
                               details={"reservation_ref": reservation_ref})
    head_id, follower = str(row["subject_id"]).rsplit(":", 1)
    return draw_follower(conn, budget_ref=row["budget_ref"], head_id=head_id, follower=follower,
                         subject_kind=subject_kind, subject_id=subject_id, actor=actor)


def release_chain(conn: sqlite3.Connection, *, budget_ref: str, head_id: str, actor: str,
                  reason: str) -> list[dict[str, Any]]:
    """Give back every hold a chain still keeps for followers that will not be created."""
    prefix = f"{head_id}:"
    rows = conn.execute(
        "SELECT reservation_ref FROM budget_reservations WHERE budget_ref=? AND subject_kind=? AND status=?"
        " AND substr(subject_id, 1, ?)=? ORDER BY rowid",
        (budget_ref, CHAIN_KIND, HELD, len(prefix), prefix)).fetchall()
    return [release(conn, row["reservation_ref"], actor=actor, reason=reason) for row in rows]


# ------------------------------------------------------------------------------ settle

def settle(conn: sqlite3.Connection, reservation_ref: str, *, actual: Mapping[str, Any],
           actor: str) -> dict[str, Any]:
    """Replace a hold with what was really used.

    ``actual`` is what the backend measured. It is charged as it is: less than was held
    frees the difference, more than was held is still charged and reported as an overrun.
    A measurement for a resource the budget has no line for is not charged and is listed.
    """
    used = _amounts(actual, what="actual") if actual else {}
    with store.transaction(conn) as txn:
        row = _reservation(txn, reservation_ref)
        if reservation_ref.startswith(FUND_PREFIX):
            raise IllegalTransition(
                f"{reservation_ref} funds a child budget; close it with release(), which charges "
                "what the child spent",
                details={"reservation_ref": reservation_ref},
            )
        held = store.loads(row, "amounts_json", {})
        if row["status"] == SETTLED:
            previous = _settled_actual(txn, reservation_ref)
            if previous == used:
                return _settle_result(txn, row, used, held, reused=True)
            raise IdempotencyConflict(
                f"reservation {reservation_ref} was already settled with other figures",
                details={"reservation_ref": reservation_ref, "settled_with": previous,
                         "offered": used},
            )
        if row["status"] != HELD:
            raise IllegalTransition(
                f"reservation {reservation_ref} is {row['status']}, not {HELD}",
                details={"reservation_ref": reservation_ref, "status": row["status"]},
            )
        return _settle(txn, row, used, actor=actor)


def _settled_actual(conn: sqlite3.Connection, reservation_ref: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind='budget.settled' AND subject_kind='budget'"
        " AND json_extract(payload_json, '$.reservation_ref')=? ORDER BY event_id DESC LIMIT 1",
        (reservation_ref,),
    ).fetchone()
    if row is None:
        return None
    return store.loads(row, "payload_json", {}).get("actual")


def _settle(conn: sqlite3.Connection, row: sqlite3.Row, used: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
    budget = _row(conn, row["budget_ref"])
    held = store.loads(row, "amounts_json", {})
    limits = store.loads(budget, "limits_json", {})
    spent = store.loads(budget, "spent_json", {})
    reserved = store.loads(budget, "reserved_json", {})
    for key, amount in held.items():
        reserved[key] = _clean(reserved.get(key, 0) - amount)
    unbudgeted = {key: value for key, value in used.items() if key not in limits}
    for key, value in used.items():
        if key in limits:
            spent[key] = _clean(spent.get(key, 0) + value)
    _write(conn, budget["budget_ref"], spent=spent, reserved=reserved)
    now = clock.now_iso()
    conn.execute(
        "UPDATE budget_reservations SET status=?, settled_at=? WHERE reservation_ref=?",
        (SETTLED, now, row["reservation_ref"]),
    )
    store.append_event(
        conn, kind="budget.settled", actor=actor, run_id=budget["run_id"], subject_kind="budget",
        subject_id=budget["budget_ref"], prev_state=HELD, new_state=SETTLED,
        evidence_level="EXECUTION_OBSERVED",
        payload={"reservation_ref": row["reservation_ref"], "held": held, "actual": dict(used),
                 "for": [row["subject_kind"], row["subject_id"]], "unbudgeted": unbudgeted},
    )
    return _settle_result(conn, row, used, held, reused=False)


def _settle_result(conn: sqlite3.Connection, row: sqlite3.Row, used: Mapping[str, Any],
                   held: Mapping[str, Any], *, reused: bool) -> dict[str, Any]:
    budget = _row(conn, row["budget_ref"])
    limits = store.loads(budget, "limits_json", {})
    spent = store.loads(budget, "spent_json", {})
    overrun = {key: _clean(value - held.get(key, 0)) for key, value in used.items()
               if key in limits and value > held.get(key, 0)}
    over_limit = {key: _clean(spent[key] - limits[key]) for key in limits
                  if spent.get(key, 0) > limits[key]}
    return {
        "reservation_ref": row["reservation_ref"], "budget_ref": row["budget_ref"],
        "status": SETTLED, "held": dict(held), "actual": dict(used), "overrun": overrun,
        "over_limit": over_limit,
        "unbudgeted": {key: value for key, value in used.items() if key not in limits},
        "reused": reused,
    }


# ----------------------------------------------------------------------------- release

def release(conn: sqlite3.Connection, reservation_ref: str, *, actor: str, reason: str) -> dict[str, Any]:
    """Give back a hold.

    For an ordinary reservation nothing was used and the whole hold returns to the budget.
    For the hold that funds a child budget this *closes the child*: the parent is charged
    what the child spent, the rest returns, and the child accepts nothing more. A child
    with work still holding part of it cannot be closed, because that work may yet spend.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise SchemaInvalid("releasing a reservation needs a reason", details={})
    with store.transaction(conn) as txn:
        row = _reservation(txn, reservation_ref)
        if row["status"] == RELEASED or (
            row["status"] == SETTLED and reservation_ref.startswith(FUND_PREFIX)
        ):
            return {"reservation_ref": reservation_ref, "budget_ref": row["budget_ref"],
                    "status": row["status"], "reused": True}
        if row["status"] != HELD:
            raise IllegalTransition(
                f"reservation {reservation_ref} is {row['status']}; only a {HELD} one is released",
                details={"reservation_ref": reservation_ref, "status": row["status"]},
            )
        budget = _row(txn, row["budget_ref"])
        held = store.loads(row, "amounts_json", {})
        reserved = store.loads(budget, "reserved_json", {})
        spent = store.loads(budget, "spent_json", {})
        charged: dict[str, Any] = {}
        new_state = RELEASED
        if reservation_ref.startswith(FUND_PREFIX):
            child_ref = reservation_ref[len(FUND_PREFIX):]
            open_inside = txn.execute(
                "SELECT reservation_ref, subject_kind, subject_id FROM budget_reservations"
                " WHERE budget_ref=? AND status=? ORDER BY rowid",
                (child_ref, HELD),
            ).fetchall()
            if open_inside:
                raise IllegalTransition(
                    f"budget {child_ref} still has {len(open_inside)} unsettled reservation(s); "
                    "work that may yet spend is settled or released first",
                    details={"budget_ref": child_ref,
                             "unsettled": [dict(item) for item in open_inside]},
                )
            child = _row(txn, child_ref)
            charged = {key: value for key, value in store.loads(child, "spent_json", {}).items()
                       if value}
            for key, value in charged.items():
                spent[key] = _clean(spent.get(key, 0) + value)
            new_state = SETTLED if charged else RELEASED
        for key, amount in held.items():
            reserved[key] = _clean(reserved.get(key, 0) - amount)
        _write(txn, budget["budget_ref"], spent=spent, reserved=reserved)
        txn.execute(
            "UPDATE budget_reservations SET status=?, settled_at=? WHERE reservation_ref=?",
            (new_state, clock.now_iso(), reservation_ref),
        )
        store.append_event(
            txn, kind="budget.released", actor=actor, run_id=budget["run_id"],
            subject_kind="budget", subject_id=budget["budget_ref"], prev_state=HELD,
            new_state=new_state, reason=reason, evidence_level="EXECUTION_OBSERVED",
            payload={"reservation_ref": reservation_ref, "held": held, "charged": charged,
                     "for": [row["subject_kind"], row["subject_id"]]},
        )
    return {"reservation_ref": reservation_ref, "budget_ref": row["budget_ref"],
            "status": new_state, "held": held, "charged": charged, "reused": False}


# ------------------------------------------------------------------------------- limits

def tighten(conn: sqlite3.Connection, budget_ref: str, *, limits: Mapping[str, Any], actor: str,
            reason: str) -> dict[str, Any]:
    """Lower limit lines. Never below what is already spent plus held, never upward."""
    wanted = _amounts(limits, what="limits")
    with store.transaction(conn) as txn:
        budget = _row(txn, budget_ref)
        current = store.loads(budget, "limits_json", {})
        spent = store.loads(budget, "spent_json", {})
        reserved = store.loads(budget, "reserved_json", {})
        for key, value in wanted.items():
            if key not in current:
                raise SchemaInvalid(f"budget {budget_ref} has no {key!r} line to tighten",
                                    details={"key": key})
            if value > current[key]:
                raise ApprovalRequired(
                    f"{key}={value} is above the current limit {current[key]}; a budget is "
                    "tightened freely and raised only through a budget_increase approval",
                    details={"key": key, "current": current[key], "requested": value},
                )
            committed = spent.get(key, 0) + reserved.get(key, 0)
            if value < committed:
                raise BudgetExceeded(
                    f"{key} cannot be lowered to {value}: {committed} is already spent or held",
                    details={"key": key, "requested": value, "committed": _clean(committed)},
                )
        current.update(wanted)
        _write(txn, budget_ref, limits=current)
        store.append_event(
            txn, kind="budget.tightened", actor=actor, run_id=budget["run_id"],
            subject_kind="budget", subject_id=budget_ref, reason=reason,
            evidence_level="EXECUTION_OBSERVED", payload={"limits": wanted},
        )
    return state(conn, budget_ref)


INCREASE_ACTION = "budget_increase"
SUBJECT_KIND = "budget"


def increase_subject(budget_ref: str, key: str, limit: float) -> Subject:
    """What an approval to raise one line is bound to: this budget, this line, this figure."""
    [checked] = _amounts({key: limit}, what="limit").items()
    return Subject(kind=SUBJECT_KIND, id=budget_ref,
                   sha256=ids.sha256_of_obj({"budget_ref": budget_ref, "key": checked[0],
                                             "limit": checked[1]}))


def profile_of(conn: sqlite3.Connection, budget_ref: str) -> str | None:
    """The profile a root budget was created from, read from the event that created it."""
    row = conn.execute(
        "SELECT json_extract(payload_json, '$.profile') AS profile FROM events"
        " WHERE kind='budget.created' AND subject_kind='budget' AND subject_id=? ORDER BY event_id LIMIT 1",
        (budget_ref,),
    ).fetchone()
    return None if row is None else row["profile"]


def increase_condition(conn: sqlite3.Connection, subject: Subject,
                       request: Mapping[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    """Whether the standing rule for raising a line holds: the figure stays inside the profile.

    A run's root budget may be created tighter than its profile; raising a line back up to
    what ``config/limits.json`` gives that profile is routine. Raising it past that line is
    raising the ceiling, which the rule does not cover, and neither is a line the profile
    does not have or a budget that is a path's allotment (that is re-planned, not raised).
    Everything is read here: the budget row, the event that names its profile, the limits
    file as it is now.
    """
    reasons: list[str] = []
    facts: dict[str, Any] = {"budget_ref": subject.id}
    budget = conn.execute("SELECT * FROM budgets WHERE budget_ref=?", (subject.id,)).fetchone()
    if subject.kind != SUBJECT_KIND or budget is None:
        return False, facts, ["the approval names no budget the store holds"]
    try:
        [checked] = _amounts({request.get("key"): request.get("limit")}, what="limit").items()
    except SchemaInvalid as exc:
        return False, facts, [f"the request does not name a line and a figure: {exc.message}"]
    key, limit = checked
    facts.update({"key": key, "limit": limit,
                  "current": store.loads(budget, "limits_json", {}).get(key)})
    if increase_subject(subject.id, key, limit).sha256 != subject.sha256:
        reasons.append("the approval is bound to another line or figure than it asks for")
    if budget["parent_ref"] is not None:
        reasons.append("this budget is an allotment inside another; it is changed by planning "
                       "again inside the parent, not by raising it")
        return False, facts, reasons
    name = profile_of(conn, subject.id)
    facts["profile"] = name
    if not name:
        reasons.append("the budget records no profile it was created from")
        return False, facts, reasons
    try:
        ceiling = profile(name)["limits"].get(key)
    except SchemaInvalid as exc:
        reasons.append(f"its profile cannot be read: {exc.message}")
        return False, facts, reasons
    facts["ceiling"] = ceiling
    if ceiling is None:
        reasons.append(f"profile {name!r} has no line {key!r}; a resource with no line is not "
                       "opened by a routine approval")
    elif limit > ceiling:
        reasons.append(f"{key}={limit} is above the {name!r} profile's {ceiling}; raising the "
                       "ceiling is a person's decision")
    return not reasons, facts, reasons


def request_increase(ctx: Any, budget_ref: str, *, key: str, limit: float, reason: str) -> dict[str, Any]:
    """Ask for the approval to raise one line of a budget. Returns the approval as govern reads it.

    GRANTED at once when the standing rule holds (:func:`increase_condition`), PENDING
    otherwise. The raise itself is :func:`raise_limit` with the approval's id.
    """
    from ..governance import approvals

    budget = _row(ctx.conn, budget_ref)
    [checked] = _amounts({key: limit}, what="limit").items()
    if not str(reason or "").strip():
        raise SchemaInvalid("a request to raise a budget line says why", details={"key": key})
    return approvals.request(
        ctx, action_kind=INCREASE_ACTION, subject=increase_subject(budget_ref, key, limit),
        run_id=budget["run_id"], request={"key": checked[0], "limit": checked[1], "reason": str(reason)},
    )


def raise_limit(conn: sqlite3.Connection, budget_ref: str, *, key: str, limit: float,
                approval_id: str, actor: str) -> dict[str, Any]:
    """Raise one limit line under a GRANTED ``budget_increase`` approval for this budget.

    The approval row is read here; nothing the caller says about it is used. It must name
    this budget as its subject and the line and figure in its request, so a grant for one
    raise cannot be spent on another.
    """
    [checked] = _amounts({key: limit}, what="limit").items()
    with store.transaction(conn) as txn:
        budget = _row(txn, budget_ref)
        approval = txn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        request = store.loads(approval, "request_json", {}) if approval is not None else {}
        problems = []
        if approval is None:
            problems.append("no such approval")
        else:
            if approval["status"] != "GRANTED":
                problems.append(f"approval is {approval['status']}, not GRANTED")
            if approval["action_kind"] != INCREASE_ACTION:
                problems.append(f"approval is for {approval['action_kind']!r}, not {INCREASE_ACTION!r}")
            if approval["subject_kind"] != SUBJECT_KIND or approval["subject_id"] != budget_ref:
                problems.append("approval names another subject")
            if request.get("key") != checked[0] or request.get("limit") != checked[1]:
                problems.append("approval was requested for another line or another figure")
            if approval["subject_sha256"] != increase_subject(budget_ref, checked[0], checked[1]).sha256:
                problems.append("approval is bound to another sha256 than this line and figure make")
        if problems:
            raise ApprovalRequired(
                f"raising {key} of budget {budget_ref} to {limit} is not approved: "
                + "; ".join(problems),
                details={"budget_ref": budget_ref, "approval_id": approval_id, "problems": problems},
            )
        current = store.loads(budget, "limits_json", {})
        previous = current.get(checked[0])
        if previous is not None and checked[1] < previous:
            raise SchemaInvalid("raise_limit lowers nothing; use tighten()", details={"key": key})
        current[checked[0]] = checked[1]
        _write(txn, budget_ref, limits=current)
        store.append_event(
            txn, kind="budget.raised", actor=actor, run_id=budget["run_id"], subject_kind="budget",
            subject_id=budget_ref, reason=f"approval {approval_id}",
            evidence_level="EXECUTION_OBSERVED",
            payload={"key": checked[0], "from": previous, "to": checked[1],
                     "approval_id": approval_id},
        )
    return state(conn, budget_ref)


# -------------------------------------------------------------------------------- read

def state(conn: sqlite3.Connection, budget_ref: str) -> dict[str, Any]:
    """Limits, spent, held, what is left, and every reservation, for one budget."""
    budget = _row(conn, budget_ref)
    limits = store.loads(budget, "limits_json", {})
    spent = store.loads(budget, "spent_json", {})
    reserved = store.loads(budget, "reserved_json", {})
    reservations = [
        {"reservation_ref": row["reservation_ref"], "subject_kind": row["subject_kind"],
         "subject_id": row["subject_id"], "amounts": store.loads(row, "amounts_json", {}),
         "status": row["status"], "created_at": row["created_at"], "settled_at": row["settled_at"]}
        for row in conn.execute(
            "SELECT * FROM budget_reservations WHERE budget_ref=? ORDER BY rowid",
            (budget_ref,),
        )
    ]
    funding = _funding_state(conn, budget_ref) if budget["parent_ref"] is not None else None
    return {
        "budget_ref": budget_ref, "parent_ref": budget["parent_ref"], "run_id": budget["run_id"],
        "limits": limits, "spent": spent,
        "reserved": {key: value for key, value in reserved.items() if value},
        "available": _available(limits, spent, reserved),
        "reservations": reservations,
        "open": budget["parent_ref"] is None or funding == HELD,
        "funding": funding,
    }


#: The subject kind of the allotment a path's confirmatory read is funded by (SCHEDULING_DESIGN §5.9).
CONFIRMATORY_KIND = "confirmatory_read"


def confirmatory_allotment(conn: sqlite3.Connection, path_id: str) -> str | None:
    """The open child budget that funds the confirmatory read of a path, or None."""
    row = conn.execute(
        "SELECT reservation_ref FROM budget_reservations WHERE subject_kind=? AND subject_id=? AND status=?"
        " AND reservation_ref LIKE ? ORDER BY rowid DESC LIMIT 1",
        (CONFIRMATORY_KIND, path_id, HELD, FUND_PREFIX + "%")).fetchone()
    return None if row is None else row["reservation_ref"][len(FUND_PREFIX):]


def budget_for(conn: sqlite3.Connection, *, run_id: str, path_id: str | None = None) -> str:
    """The budget work is charged to: the path's own allotment, else the run's root.

    A path whose own allotment was closed (it ended) and that holds an open allotment for its
    confirmatory read (:data:`CONFIRMATORY_KIND`) is charged to that one: a confirmatory read
    runs the path's method again on held-out inputs, after the path's outcome.
    """
    if path_id is not None:
        path = conn.execute(
            "SELECT run_id, budget_json FROM paths WHERE path_id=?", (path_id,)
        ).fetchone()
        if path is None or path["run_id"] != run_id:
            raise ArtifactNotFound(f"no path {path_id!r} in run {run_id!r}",
                                   details={"path_id": path_id, "run_id": run_id})
        ref = store.loads(path, "budget_json", {}).get("budget_ref")
        if not ref:
            raise BudgetExceeded(
                f"path {path_id} has no budget: nothing was reserved for it at acceptance, so "
                "nothing can be spent on it",
                details={"path_id": path_id},
            )
        if _funding_state(conn, ref) not in (None, HELD):
            confirming = confirmatory_allotment(conn, path_id)
            if confirming is not None:
                return confirming
        return ref
    run = conn.execute("SELECT budget_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
    if not run["budget_ref"]:
        raise BudgetExceeded(f"run {run_id} has no budget", details={"run_id": run_id})
    return run["budget_ref"]


def verify(conn: sqlite3.Connection, budget_ref: str) -> dict[str, Any]:
    """Recompute ``reserved`` from the HELD reservations and compare with the stored total.

    The two are written together, so they agree unless something wrote one without the
    other. A ledger that is never recounted is a ledger nobody knows is right.
    """
    budget = _row(conn, budget_ref)
    stored = {k: v for k, v in store.loads(budget, "reserved_json", {}).items() if v}
    recount: dict[str, Any] = {}
    for row in conn.execute(
        "SELECT amounts_json FROM budget_reservations WHERE budget_ref=? AND status=?",
        (budget_ref, HELD),
    ):
        for key, value in store.loads(row, "amounts_json", {}).items():
            recount[key] = _clean(recount.get(key, 0) + value)
    recount = {k: v for k, v in recount.items() if v}
    return {"budget_ref": budget_ref, "ok": stored == recount, "stored_reserved": stored,
            "recounted_reserved": recount}


__all__ = [
    "CHAIN_KIND", "CONFIRMATORY_KIND", "DEFAULT_PROFILE", "FUND_PREFIX", "HELD", "INCREASE_ACTION",
    "PROJECT_LINES", "RELEASED", "RESERVATION_STATES", "SETTLED", "SUBJECT_KIND", "activate_project_budget",
    "budget_for", "chain_key", "check_amounts", "check_project_limits",
    "confirmatory_allotment", "create_root", "draw_follower", "draw_reservation", "line",
    "increase_condition", "increase_subject", "open_child", "profile", "profile_of", "project_budget_state",
    "project_phase_event", "raise_limit", "release",
    "release_chain", "request_increase", "reserve", "reserve_chain", "settle", "spend", "state", "tighten",
    "verify",
]
