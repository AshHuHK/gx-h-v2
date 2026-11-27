"""The check runner: the only writer of ``check_runs``.

A gate is met by what the backend found when it looked, never by what somebody said it
would find. So a check is a function the backend calls — published with
``@registry.check`` by the stream that owns what it inspects — and its outcome is written
here, in one place, as an append-only row bound to one subject at one content sha256.
There is no function in this module, and no tool anywhere, through which a caller hands in
a result.

    run_checks(ctx, subject, ["byte_binding", "manifest_schema"])  -> [ {check_id, result, …} ]
    latest_results(conn, subject)                                  -> {check_id: {result, met, …}}

Four results (``contracts/gates.json``): ``PASS``; ``FAIL``; ``ERROR`` — the check could
not run, which never counts as a pass, and which is also what a check nobody has published
yet produces; and ``DIAGNOSTIC`` — the check found items that need a judgement. A
diagnostic's gate stays unmet until every fingerprint has an adjudication that accepts it,
made under a review at a sufficient independence level or by a person.

A check is recomputed from the rows and bytes the backend holds every time it is asked
for. A later run does not erase an earlier one: the gate reads the most recent run of each
check for the current sha256, and the earlier rows stay.
"""

from __future__ import annotations

import sqlite3
import traceback
from typing import Any, Sequence

from .. import contracts, registry
from ..context import SCOPE_OPERATOR, Ctx
from ..domain import clock
from ..errors import (
    ArtifactNotFound,
    ContentHashMismatch,
    GxError,
    PermissionDenied,
    SchemaInvalid,
    VersionImmutable,
)
from ..persistence import store
from ..registry import CheckOutcome, Subject
from . import _ids, subjects

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
DIAGNOSTIC = "DIAGNOSTIC"
for _name in (PASS, FAIL, ERROR, DIAGNOSTIC):
    if _name not in contracts.gates()["check_result"]["values"]:  # pragma: no cover
        raise RuntimeError(f"check result {_name!r} left the contract")

#: What an adjudication may decide about one diagnostic (``contracts/labels.json``, axis
#: ``adjudication_decision``): ACCEPTED means "looked at, not a defect of the asset, with the
#: reason"; DEFECT means "a real defect", which leaves the gate unmet for this sha256 for
#: good — the repair is a new version.
ADJ_ACCEPTED = contracts.require_label("adjudication_decision", "ACCEPTED")
ADJ_DEFECT = contracts.require_label("adjudication_decision", "DEFECT")
ADJUDICATION_DECISIONS = tuple(contracts.label_values("adjudication_decision"))

HUMAN = contracts.require_label("independence_level", "HUMAN")


# ------------------------------------------------------------------------------ running

def _governed(conn: sqlite3.Connection, subject: Subject) -> dict[str, Any]:
    info = subjects.resolve(conn, subject)
    if info["current_sha256"] != subject.sha256:
        raise ContentHashMismatch(
            f"{subject.kind} {subject.id}@{info['version']} is held at sha256 "
            f"{str(info['current_sha256'])[:16]}…, not {subject.sha256[:16]}…; a check run belongs "
            "to the content the store holds",
            details={"subject": subjects.as_dict(subject), "current_sha256": info["current_sha256"]},
        )
    return info


def _call(ctx: Ctx, check_id: str, subject: Subject) -> tuple[CheckOutcome, str]:
    """Run one published check. Whatever goes wrong inside it becomes an ERROR outcome."""
    fn = registry.check_for(check_id)
    if fn is None:
        providers = {name: why for name, why in registry.load_providers().items() if why}
        return CheckOutcome(
            ERROR,
            detail={"reason": "no implementation is published for this check", "unbound": True,
                    "providers_not_loaded": providers},
        ), "unbound"
    runner = f"{fn.__module__}.{fn.__qualname__}"
    try:
        outcome = fn(ctx, subject)
    except GxError as exc:
        return CheckOutcome(ERROR, detail={"reason": "the check raised", "error": exc.to_payload()}), runner
    except Exception as exc:  # a check's own bug is a finding about the check, not a pass
        last = traceback.extract_tb(exc.__traceback__)[-1]
        return CheckOutcome(
            ERROR,
            detail={"reason": "the check raised", "exception": type(exc).__name__, "message": str(exc)[:500],
                    "where": f"{last.filename}:{last.lineno}"},
        ), runner
    if not isinstance(outcome, CheckOutcome):
        return CheckOutcome(
            ERROR, detail={"reason": f"the check returned {type(outcome).__name__}, not a CheckOutcome"}
        ), runner
    return outcome, runner


def run_checks(ctx: Ctx, subject: Subject, check_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Run the named checks against ``subject`` and append one ``check_runs`` row for each.

    Refused before anything is written: a subject the store does not hold, a sha256 that is
    not the one the store holds for it, a check id the contract does not define, and a
    check that does not apply to this kind of object.
    """
    info = _governed(ctx.conn, subject)
    kind = info["governed_kind"]
    subject = Subject(kind=subject.kind, id=subject.id, sha256=subject.sha256, version=info["version"])
    for check_id in check_ids:
        try:
            spec = contracts.check_spec(check_id)
        except KeyError as exc:
            raise SchemaInvalid(
                f"{check_id!r} is not a check contracts/gates.json defines",
                details={"check_id": check_id, "known": contracts.check_ids()},
            ) from exc
        if kind not in spec["applies_to"]:
            raise SchemaInvalid(
                f"check {check_id!r} does not apply to a {kind}; it applies to {spec['applies_to']}",
                details={"check_id": check_id, "kind": kind, "applies_to": spec["applies_to"]},
            )

    results: list[dict[str, Any]] = []
    for check_id in check_ids:
        outcome, runner = _call(ctx, check_id, subject)
        results.append(_append(ctx, subject, check_id, outcome, runner))
    return results


def _append(ctx: Ctx, subject: Subject, check_id: str, outcome: CheckOutcome, runner: str) -> dict[str, Any]:
    check_run_id = _ids.new_id("check_run")
    at = clock.now_iso()
    inputs = {"triggered_by": ctx.actor, "tool": ctx.tool, "request_id": ctx.request_id}
    with store.transaction(ctx.conn) as conn:
        conn.execute(
            "INSERT INTO check_runs(check_run_id, subject_kind, subject_id, subject_version,"
            " subject_sha256, check_id, runner, inputs_json, result, detail_json,"
            " diagnostics_json, artifact_ref, job_id, superseded, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                check_run_id, subject.kind, subject.id, subject.version, subject.sha256, check_id,
                runner, store._j(inputs), outcome.result, store._j(outcome.detail),
                store._j(outcome.diagnostics), outcome.artifact_ref, outcome.job_id, at,
            ),
        )
        store.append_event(
            conn, kind="check.run", actor=ctx.actor, subject_kind=subject.kind,
            subject_id=subject.id, reason=check_id, evidence_level="EXECUTION_OBSERVED",
            payload={"check_run_id": check_run_id, "check_id": check_id, "result": outcome.result,
                     "sha256": subject.sha256, "version": subject.version, "runner": runner,
                     "diagnostics": len(outcome.diagnostics)},
        )
    return {
        "check_id": check_id, "result": outcome.result, "check_run_id": check_run_id, "at": at,
        "runner": runner, "detail": outcome.detail, "diagnostics": outcome.diagnostics,
        "artifact_ref": outcome.artifact_ref, "job_id": outcome.job_id,
    }


# ------------------------------------------------------------------------------ reading

def _rows(conn: sqlite3.Connection, subject: Subject) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT rowid AS seq, * FROM check_runs WHERE subject_kind=? AND subject_id=?"
        " AND subject_sha256=? AND superseded=0 ORDER BY rowid",
        (subject.kind, subject.id, subject.sha256),
    ))


def _adjudications(conn: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, list[sqlite3.Row]]:
    by_fingerprint: dict[str, list[sqlite3.Row]] = {}
    for run_id in run_ids:
        for row in conn.execute(
            "SELECT * FROM adjudications WHERE check_run_id=? ORDER BY created_at", (run_id,)
        ):
            by_fingerprint.setdefault(row["diagnostic_fingerprint"], []).append(row)
    return by_fingerprint


def latest_results(conn: sqlite3.Connection, subject: Subject) -> dict[str, dict[str, Any]]:
    """The most recent run of each check for this subject at this sha256, and whether it is met.

    ``met`` is True for a PASS, and for a DIAGNOSTIC whose every fingerprint has been
    accepted — on this run or an earlier run of the same check for the same sha256 — under a
    sufficient independence level, with no fingerprint judged a defect.
    """
    sufficient = set(contracts.sufficient_independence())
    by_check: dict[str, list[sqlite3.Row]] = {}
    for row in _rows(conn, subject):
        by_check.setdefault(row["check_id"], []).append(row)

    latest: dict[str, dict[str, Any]] = {}
    for check_id, rows in by_check.items():
        row = rows[-1]
        entry: dict[str, Any] = {
            "check_id": check_id, "result": row["result"], "check_run_id": row["check_run_id"],
            "at": row["created_at"], "runner": row["runner"], "runs": len(rows),
            "unadjudicated": [], "defects": [], "met": row["result"] == PASS,
            "job_id": row["job_id"], "artifact_ref": row["artifact_ref"],
        }
        if row["result"] == DIAGNOSTIC:
            judged = _adjudications(conn, [r["check_run_id"] for r in rows])
            for item in store.loads(row, "diagnostics_json", []) or []:
                fingerprint = item.get("fingerprint")
                decisions = judged.get(fingerprint, [])
                if any(d["decision"] == ADJ_DEFECT for d in decisions):
                    entry["defects"].append(fingerprint)
                elif not any(d["decision"] == ADJ_ACCEPTED and d["independence_level"] in sufficient
                             for d in decisions):
                    entry["unadjudicated"].append(fingerprint)
            entry["met"] = not entry["unadjudicated"] and not entry["defects"]
        latest[check_id] = entry
    return latest


def history(conn: sqlite3.Connection, subject: Subject, *, include_superseded: bool = True) -> list[dict[str, Any]]:
    """Every run on record for the subject's sha256, oldest first. Nothing is ever removed."""
    rows = conn.execute(
        "SELECT * FROM check_runs WHERE subject_kind=? AND subject_id=? AND subject_sha256=?"
        + ("" if include_superseded else " AND superseded=0") + " ORDER BY rowid",
        (subject.kind, subject.id, subject.sha256),
    )
    return [
        {"check_run_id": r["check_run_id"], "check_id": r["check_id"], "result": r["result"],
         "at": r["created_at"], "runner": r["runner"], "superseded": bool(r["superseded"])}
        for r in rows
    ]


def get_run(conn: sqlite3.Connection, check_run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM check_runs WHERE check_run_id=?", (check_run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no check run {check_run_id!r}", details={"check_run_id": check_run_id})
    return {
        "check_run_id": row["check_run_id"], "check_id": row["check_id"], "result": row["result"],
        "subject": {"kind": row["subject_kind"], "id": row["subject_id"],
                    "version": row["subject_version"], "sha256": row["subject_sha256"]},
        "runner": row["runner"], "at": row["created_at"], "superseded": bool(row["superseded"]),
        "detail": store.loads(row, "detail_json", {}) or {},
        "diagnostics": store.loads(row, "diagnostics_json", []) or [],
        "artifact_ref": row["artifact_ref"], "job_id": row["job_id"],
    }


# ------------------------------------------------------------------------ adjudications

def adjudicate(
    ctx: Ctx,
    *,
    check_run_id: str,
    fingerprint: str,
    decision: str,
    note: str,
    review_id: str | None = None,
    person: bool = False,
) -> dict[str, Any]:
    """Record a judgement on one diagnostic of one check run.

    The judgement carries the independence of whoever made it, and that is never taken from
    the caller: with ``review_id`` it is the level the backend computed for that review, which
    must be a review of the same subject at the same sha256; with ``person=True`` it is HUMAN,
    which only the operator surface may pass after it has shown the call to a person.
    """
    if decision not in ADJUDICATION_DECISIONS:
        raise SchemaInvalid(
            f"{decision!r} is not an adjudication decision; allowed: {list(ADJUDICATION_DECISIONS)}",
            details={"decision": decision, "allowed": list(ADJUDICATION_DECISIONS)},
        )
    if not (isinstance(note, str) and note.strip()):
        raise SchemaInvalid("an adjudication states its reason; an empty note judges nothing")
    run = get_run(ctx.conn, check_run_id)
    if run["result"] != DIAGNOSTIC:
        raise SchemaInvalid(
            f"check run {check_run_id} is {run['result']}, not {DIAGNOSTIC}; only a diagnostic is adjudicated",
            details={"check_run_id": check_run_id, "result": run["result"]},
        )
    if run["superseded"]:
        raise SchemaInvalid(
            f"check run {check_run_id} was invalidated when its asset returned to quarantine",
            details={"check_run_id": check_run_id},
        )
    if fingerprint not in {item.get("fingerprint") for item in run["diagnostics"]}:
        raise SchemaInvalid(
            f"check run {check_run_id} has no diagnostic with fingerprint {fingerprint!r}; an "
            "adjudication is bound to the exact diagnostic it judged",
            details={"check_run_id": check_run_id, "fingerprint": fingerprint},
        )

    if review_id is not None:
        review = ctx.conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if review is None:
            raise ArtifactNotFound(f"no review {review_id!r}", details={"review_id": review_id})
        same = (review["subject_kind"], review["subject_id"], review["subject_sha256"]) == (
            run["subject"]["kind"], run["subject"]["id"], run["subject"]["sha256"])
        if not same or review["superseded"]:
            raise ContentHashMismatch(
                "the review and the check run are not about the same subject at the same sha256",
                details={"review_id": review_id, "check_run_id": check_run_id},
            )
        level = review["independence_level"]
        decided_by = review["principal"] or f"review:{review_id}"
    elif person:
        if ctx.principal.scope != SCOPE_OPERATOR:
            raise PermissionDenied(
                "a person's adjudication is recorded through the operator surface",
                details={"scope": ctx.principal.scope},
            )
        level = HUMAN
        decided_by = f"person:{ctx.principal.subject}"
    else:
        raise SchemaInvalid(
            "an adjudication is made under a review the backend recorded, or by a person through "
            "the operator surface; it is not taken on a caller's word",
            details={"check_run_id": check_run_id},
        )

    adjudication_id = _ids.new_id("adjudication")
    at = clock.now_iso()
    with store.transaction(ctx.conn) as conn:
        try:
            conn.execute(
                "INSERT INTO adjudications(adjudication_id, check_run_id, diagnostic_fingerprint,"
                " decision, decided_by, independence_level, note, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (adjudication_id, check_run_id, fingerprint, decision, decided_by, level, note, at),
            )
        except sqlite3.IntegrityError as exc:
            raise VersionImmutable(
                f"diagnostic {fingerprint!r} of check run {check_run_id} already has an adjudication; "
                "a judgement on record is not replaced. Run the check again to judge it afresh",
                details={"check_run_id": check_run_id, "fingerprint": fingerprint},
            ) from exc
        store.append_event(
            conn, kind="check.adjudicated", actor=ctx.actor, subject_kind=run["subject"]["kind"],
            subject_id=run["subject"]["id"], reason=decision, evidence_level="EXECUTION_OBSERVED",
            payload={"adjudication_id": adjudication_id, "check_run_id": check_run_id,
                     "check_id": run["check_id"], "fingerprint": fingerprint, "decision": decision,
                     "independence_level": level, "review_id": review_id,
                     "sha256": run["subject"]["sha256"]},
        )
    return {"adjudication_id": adjudication_id, "check_run_id": check_run_id, "fingerprint": fingerprint,
            "decision": decision, "independence_level": level, "decided_by": decided_by, "at": at}


def invalidate(conn: sqlite3.Connection, subject: Subject) -> int:
    """Mark every check run of this sha256 as no longer counting. Called on requarantine only."""
    cur = conn.execute(
        "UPDATE check_runs SET superseded=1 WHERE subject_kind=? AND subject_id=?"
        " AND subject_sha256=? AND superseded=0",
        (subject.kind, subject.id, subject.sha256),
    )
    return int(cur.rowcount or 0)
