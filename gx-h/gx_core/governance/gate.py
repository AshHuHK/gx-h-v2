"""Is the gate for a transition met, for these bytes?

One function answers that for every governed object, from rows the backend wrote and from
nothing else::

    report = evaluate(conn, kind="DECISION_SKILL", subject=subject, to_state="REVIEWED")
    report["met"], report["missing"]
    require_met(conn, kind="EVALUATOR", subject=subject, to_state="REVIEWED")

What a transition needs is ``contracts/gates.json``: checks whose most recent run for the
subject's sha256 is PASS (or a DIAGNOSTIC with every fingerprint accepted); a review that
says APPROVE at a sufficient independence level, handed in under a task of the role the
table names (or by a person), with no objection on record for the same bytes; an approval
bound to the sha256 that is GRANTED. The asset lifecycle calls this from ``promote``; the evaluation builder calls it
for the transitions of an evaluation specification.

A request cannot move any of the three. Check runs are written by the runner, reviews by
the review validator after it computed the independence itself, approvals by the operator
surface or by the standing policy.

One kind of review is on record for the same bytes and is not read here at all: the review
that looks for a defect in a FROZEN evaluator (``gx_core.governance.reviews``,
SCHEDULING_DESIGN §6.3). Whatever it says, it is neither an approval nor an objection for
any transition. It is told apart by the task the backend created for it
(``input.review_purpose``), never by anything the reviewer handed in.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping

from .. import contracts
from ..domain import clock
from ..errors import GateNotMet, IndependenceRequired
from ..registry import Subject
from . import checks, subjects

APPROVE = contracts.require_label("review_verdict", "APPROVE")
HUMAN = contracts.require_label("independence_level", "HUMAN")
GRANTED = "GRANTED"
if GRANTED not in contracts.states()["machines"]["approval"]["states"]:  # pragma: no cover
    raise RuntimeError("the approval machine no longer has a GRANTED state")

#: ``tasks.input.review_purpose`` of a review task that is not the review a gate waits for,
#: and its one value: the search for a defect in a FROZEN evaluator.
REVIEW_PURPOSE = "review_purpose"
EVALUATOR_DEFECT = "evaluator_defect"

#: Checks the gate table marks ``required_when``: the condition, computed from the manifest
#: the store holds. A check with a ``required_when`` and no entry here is a contract change
#: nobody implemented, and :func:`conditional_checks_without_predicate` reports it.
CONDITIONAL: dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "probe_L4_known_answer": lambda manifest: bool(manifest.get("known_answer")),
}


def conditional_checks_without_predicate() -> list[str]:
    return sorted(
        check_id for check_id, spec in contracts.gates()["checks"].items()
        if "required_when" in spec and check_id not in CONDITIONAL
    )


def required_checks(kind: str, to_state: str, manifest: Mapping[str, Any] | None) -> list[str]:
    """The checks this transition needs for an object with this manifest."""
    listed = contracts.gate_requirements(kind, to_state)["checks"]
    out = []
    for check_id in listed:
        predicate = CONDITIONAL.get(check_id)
        if predicate is not None and "required_when" in contracts.check_spec(check_id):
            if manifest is None or not predicate(manifest):
                continue
        out.append(check_id)
    return out


def _manifest(conn: sqlite3.Connection, subject: Subject) -> Mapping[str, Any] | None:
    if subject.kind != subjects.ASSET:
        return None
    row = subjects.resolve(conn, subject)["row"]
    from ..persistence import store

    return store.loads(row, "manifest_json", {}) or {}


def review_roles(kind: str) -> set[str]:
    """The worker roles under which a review of ``kind`` can meet one of its gates."""
    data = contracts.gates()
    table = data["eval_spec_requirements"] if kind == subjects.EVALUATOR else data["requirements"].get(kind, {})
    return {
        entry["review"]["role"] for entry in table.values()
        if isinstance(entry, Mapping) and entry.get("review") and entry["review"].get("role")
    }


def _reviewer_role(conn: sqlite3.Connection, review: sqlite3.Row) -> str | None:
    """The role of the task a review was handed in under, read from the task the backend created.

    A person's review is handed in under no task and has no worker role: the gate table's role
    is the role a *worker* reviews in.
    """
    if not review["task_id"]:
        return None
    task = conn.execute("SELECT role FROM tasks WHERE task_id=?", (review["task_id"],)).fetchone()
    return task["role"] if task is not None else None


def _review_state(conn: sqlite3.Connection, subject: Subject, requirement: Mapping[str, Any]) -> dict[str, Any]:
    sufficient = set(contracts.sufficient_independence())
    required_role = requirement.get("role")
    rows = list(conn.execute(
        "SELECT r.* FROM reviews r LEFT JOIN tasks t ON t.task_id = r.task_id"
        " WHERE r.subject_kind=? AND r.subject_id=? AND r.subject_sha256=? AND r.superseded=0"
        " AND COALESCE(json_extract(t.input_json, ?), '') != ? ORDER BY r.rowid",
        (subject.kind, subject.id, subject.sha256, f"$.{REVIEW_PURPOSE}", EVALUATOR_DEFECT),
    ))
    state: dict[str, Any] = {
        "required_role": required_role, "met": False, "review_id": None,
        "independence_level": None, "state": "NO_REVIEW", "reviews_on_record": len(rows),
    }

    def in_role(review: sqlite3.Row) -> bool:
        if required_role is None or (review["independence_level"] == HUMAN and not review["task_id"]):
            return True
        return _reviewer_role(conn, review) == required_role

    objections = [r for r in rows if r["verdict"] != APPROVE]
    approvals = [r for r in rows if r["verdict"] == APPROVE]
    independent = [r for r in approvals if r["independence_level"] in sufficient]
    good = [r for r in independent if in_role(r)]
    if objections:
        # REQUEST_CHANGES and REJECT stay on record; changes mean a new version, and no later
        # APPROVE of the same bytes takes their place.
        last = objections[-1]
        state.update(state="OBJECTION_ON_RECORD", review_id=last["review_id"],
                     independence_level=last["independence_level"], verdict=last["verdict"])
    elif good:
        best = good[-1]
        state.update(met=True, state="APPROVED", review_id=best["review_id"],
                     independence_level=best["independence_level"], verdict=APPROVE)
    elif independent:
        # Independent, approving, and handed in under a task of another role: the gate table
        # names the role a review is done in, and the task's role is the backend's own row.
        last = independent[-1]
        state.update(state="WRONG_ROLE", review_id=last["review_id"],
                     independence_level=last["independence_level"], verdict=APPROVE,
                     reviewer_role=_reviewer_role(conn, last))
    elif approvals:
        last = approvals[-1]
        state.update(state="INDEPENDENCE_INSUFFICIENT", review_id=last["review_id"],
                     independence_level=last["independence_level"], verdict=APPROVE,
                     sufficient=sorted(sufficient))
    return state


def _approval_state(conn: sqlite3.Connection, subject: Subject, requirement: Mapping[str, Any]) -> dict[str, Any]:
    action_kind = requirement["action_kind"]
    rows = list(conn.execute(
        "SELECT * FROM approvals WHERE action_kind=? AND subject_kind=? AND subject_id=?"
        " AND subject_sha256=? ORDER BY rowid",
        (action_kind, subject.kind, subject.id, subject.sha256),
    ))
    state: dict[str, Any] = {"action_kind": action_kind, "met": False, "approval_id": None, "status": None}
    for row in reversed(rows):
        expired = row["expires_at"] is not None and clock.is_past(row["expires_at"])
        if row["status"] == GRANTED and not expired:
            state.update(met=True, approval_id=row["approval_id"], status=GRANTED,
                         granted_by=row["decided_by"], policy_ref=row["policy_ref"])
            return state
    if rows:
        last = rows[-1]
        state.update(approval_id=last["approval_id"], status=last["status"])
    return state


def evaluate(
    conn: sqlite3.Connection,
    *,
    kind: str,
    subject: Subject,
    to_state: str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What is on record for this transition, what is missing, and whether the gate is met."""
    requirements = contracts.gate_requirements(kind, to_state)
    if manifest is None:
        manifest = _manifest(conn, subject)
    needed = required_checks(kind, to_state, manifest)
    latest = checks.latest_results(conn, subject)

    check_states: list[dict[str, Any]] = []
    missing_checks: list[dict[str, Any]] = []
    for check_id in needed:
        found = latest.get(check_id)
        if found is None:
            entry = {"check_id": check_id, "result": None, "met": False, "state": "NEVER_RUN"}
        else:
            entry = {
                "check_id": check_id, "result": found["result"], "met": found["met"],
                "check_run_id": found["check_run_id"], "at": found["at"],
                "state": found["result"] if found["met"] or found["result"] != checks.DIAGNOSTIC
                else ("DEFECT_ADJUDICATED" if found["defects"] else "DIAGNOSTIC_UNADJUDICATED"),
            }
            if found["unadjudicated"]:
                entry["unadjudicated"] = found["unadjudicated"]
            if found["defects"]:
                entry["defects"] = found["defects"]
        check_states.append(entry)
        if not entry["met"]:
            missing_checks.append(entry)

    review = _review_state(conn, subject, requirements["review"]) if requirements["review"] else None
    approval = _approval_state(conn, subject, requirements["approval"]) if requirements["approval"] else None

    missing: dict[str, Any] = {
        "checks": missing_checks,
        "review": review if review is not None and not review["met"] else None,
        "approval": approval if approval is not None and not approval["met"] else None,
    }
    met = not missing_checks and missing["review"] is None and missing["approval"] is None
    return {
        "kind": kind, "to_state": to_state, "subject": subjects.as_dict(subject), "met": met,
        "checks": check_states, "review": review, "approval": approval,
        "effects": requirements["effects"], "missing": missing,
    }


def raise_unmet(report: Mapping[str, Any]) -> None:
    """Raise the refusal a not-met report stands for. Does nothing when the gate is met."""
    if report["met"]:
        return
    missing = report["missing"]
    subject = report["subject"]
    review = missing["review"]
    only_independence = (
        not missing["checks"] and missing["approval"] is None
        and review is not None and review["state"] == "INDEPENDENCE_INSUFFICIENT"
    )
    details = {"subject": subject, "to_state": report["to_state"], "missing": missing}
    if only_independence:
        raise IndependenceRequired(
            f"{subject['id']}@{subject['version']} has an approving review at independence level "
            f"{review['independence_level']}, which does not meet a gate; a different worker, shown "
            "by a host record, has to review these bytes",
            details=details,
        )
    parts = []
    if missing["checks"]:
        parts.append("checks " + ", ".join(f"{c['check_id']} ({c['state']})" for c in missing["checks"]))
    if review is not None:
        parts.append(f"review ({review['state']})")
    if missing["approval"] is not None:
        parts.append(f"approval {missing['approval']['action_kind']} ({missing['approval']['status'] or 'not requested'})")
    raise GateNotMet(
        f"{subject['id']}@{subject['version']} cannot enter {report['to_state']} at sha256 "
        f"{subject['sha256'][:16]}…: missing " + "; ".join(parts),
        details=details,
    )


def require_met(
    conn: sqlite3.Connection,
    *,
    kind: str,
    subject: Subject,
    to_state: str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report when the gate is met; ``GATE_NOT_MET`` or ``INDEPENDENCE_REQUIRED`` otherwise."""
    report = evaluate(conn, kind=kind, subject=subject, to_state=to_state, manifest=manifest)
    raise_unmet(report)
    return report
