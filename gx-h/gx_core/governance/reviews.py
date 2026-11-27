"""Create and record independent reviews of versioned assets and run submissions.

Review tasks carry a content digest and a reference to the package the reviewer
must inspect. The store derives author and reviewer identities from the task
leases and native host observations and retains the basis for each decision.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .. import contracts
from ..context import SCOPE_OPERATOR, Ctx
from ..domain import clock
from ..errors import ArtifactNotFound, ContentHashMismatch, IndependenceRequired, PermissionDenied, SchemaInvalid, ScopeDenied
from ..persistence import store
from ..registry import Subject
from . import _ids, approvals, gate, subjects

HUMAN = contracts.require_label("independence_level", "HUMAN")
DIFFERENT_HOST = contracts.require_label("independence_level", "DIFFERENT_HOST")
FRESH_CONTEXT_SAME_HOST = contracts.require_label("independence_level", "FRESH_CONTEXT_SAME_HOST")
CLIENT_REPORTED_ONLY = contracts.require_label("independence_level", "CLIENT_REPORTED_ONLY")
NOT_INDEPENDENT = contracts.require_label("independence_level", "NOT_INDEPENDENT")

#: Sources of an observation that are a record the host or the backend made, as opposed to
#: what a worker said about itself.
HOST_RECORDED = tuple(
    value for value in contracts.label_values("observation_source") if value != "client_report"
)

APPROVE = contracts.require_label("review_verdict", "APPROVE")

# ------------------------------------------------------------------------- who did a task

_ACTOR = re.compile(r"^(?:mcp|cli):[a-z_]+:(.+)$")


def _subject_of(value: str | None) -> str | None:
    """The principal's subject, whether the column holds it bare or inside an actor string."""
    if not value:
        return None
    match = _ACTOR.match(value)
    return match.group(1) if match else value

def _lease(conn: sqlite3.Connection, task_id: str, lease_id: str | None) -> sqlite3.Row | None:
    if lease_id:
        row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is not None:
            return row
    task = conn.execute("SELECT lease_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is not None and task["lease_id"]:
        row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (task["lease_id"],)).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM leases WHERE task_id=? ORDER BY issued_at DESC, rowid DESC LIMIT 1", (task_id,)
    ).fetchone()


def worker_identity(row: sqlite3.Row) -> str | None:
    """Who a host-recorded observation is about, or None when the row does not say.

    ``observed_worker_ref`` is the identity the host stream writes from a host record:
    ``<host>:<host session>:<agent id>``, with ``main`` for a session's main thread — so the
    main thread has an identity and two sessions' main threads are told apart. A row with no
    such reference names no worker, whatever its other columns say: a host record always
    carries it, so a host-sourced row without it is not an observation (the lead's ruling of
    2026-11-12T16:15Z). Rows a worker reported about itself never reach this function: the
    caller selects on ``observation_source``.

    Until 19:29Z on 2026-11-12 a host-sourced row with only an ``agent_ref`` was read from its
    own columns, because core's ``records.record_worker_observation`` and two streams' test
    fixtures wrote such rows. Core's writer now writes client reports only (19:27:18Z), the
    fixtures write through the host stream's ``write_observation``, and that fallback is gone.
    """
    ref = row["observed_worker_ref"]
    return str(ref) if ref else None


def _host_view(conn: sqlite3.Connection, task_id: str, lease_id: str) -> dict[str, Any] | None:
    """The host stream's reading of who held a lease (``hostobs.observation.identity_of``), or None.

    It adds what the host's hooks saw beyond the observation rows: the agent its shell hook
    tied to the subject the worker typed, every subject that agent was seen calling the
    backend under, and every agent seen under that subject.
    """
    try:
        from ..hostobs import observation as hostobs  # local: hostobs loads the host adapters
    except ImportError:
        return None
    return hostobs.identity_of(conn, task_id=task_id, lease_id=lease_id)


def worker_facts(conn: sqlite3.Connection, task_id: str | None, *, lease_id: str | None = None) -> dict[str, Any]:
    """What the backend holds about who did a task: its lease, its principal, its host-recorded workers."""
    facts: dict[str, Any] = {
        "task_id": task_id, "lease_id": None, "principal": None, "identities": set(),
        "hosts": set(), "observation_ids": [], "one_worker_one_name": None, "names": {},
    }
    if not task_id:
        return facts
    lease = _lease(conn, task_id, lease_id)
    if lease is not None:
        facts["lease_id"] = lease["lease_id"]
        facts["principal"] = _subject_of(lease["principal"] or lease["owner"])
        seen = _host_view(conn, task_id, lease["lease_id"])
        if seen is not None:
            if seen.get("source") == "host_protocol" and seen.get("native_source") == "codex_native_events_v1":
                facts.update(native_source=seen["native_source"], fresh_context=seen.get("fresh_context"),
                             forked_from_id=seen.get("forked_from_id"))
            if seen.get("observed_worker_ref") and seen.get("source") in HOST_RECORDED and seen.get("host"):
                facts["identities"].add((seen["host"], str(seen["observed_worker_ref"])))
                facts["hosts"].add(seen["host"])
            facts["one_worker_one_name"] = seen.get("one_worker_one_name")
            facts["names"] = {"subjects_of_agent": list(seen.get("subjects_of_agent") or []),
                              "agents_of_subject": list(seen.get("agents_of_subject") or [])}
    placeholders = ",".join("?" for _ in HOST_RECORDED)
    rows = conn.execute(
        f"SELECT * FROM worker_observations WHERE task_id=? AND observation_source IN ({placeholders})"
        " ORDER BY observed_at",
        (task_id, *HOST_RECORDED),
    ).fetchall()
    for row in rows:
        # An observation that names a lease belongs to that attempt; one that names none is
        # about the task as a whole.
        if row["lease_id"] and facts["lease_id"] and row["lease_id"] != facts["lease_id"]:
            continue
        identity = worker_identity(row)
        if identity is None:
            continue
        facts["identities"].add((row["host"], identity))
        facts["hosts"].add(row["host"])
        facts["observation_ids"].append(row["worker_observation_id"])
    return facts


def _level(author: Mapping[str, Any], reviewer: Mapping[str, Any], *, author_principal: str | None = None) -> tuple[str, str]:
    """The level two sets of facts support, and the sentence that says why."""
    if author["task_id"] and author["task_id"] == reviewer["task_id"]:
        return NOT_INDEPENDENT, "the review was handed in under the authoring task itself"
    if author["lease_id"] and author["lease_id"] == reviewer["lease_id"]:
        return NOT_INDEPENDENT, "author and reviewer hold the same lease"
    a_principal = author["principal"] or author_principal
    if a_principal and reviewer["principal"] and a_principal == reviewer["principal"]:
        return NOT_INDEPENDENT, f"author and reviewer are the same principal ({a_principal})"
    shared = author["identities"] & reviewer["identities"]
    if shared:
        host, agent = sorted(shared)[0]
        return NOT_INDEPENDENT, f"the host recorded the same worker ({agent}) on both tasks"
    if reviewer.get("one_worker_one_name") is False:
        # The lead's ruling F: one host-recorded agent seen under several subjects, or several
        # agents under one subject, is a name that does not stand for one worker; the reviews
        # done under it show no independence, whoever else is on record.
        names = reviewer.get("names") or {}
        return NOT_INDEPENDENT, (
            "the host saw the reviewing worker's name stand for more than one worker: its agent called the "
            f"backend under {names.get('subjects_of_agent') or []} and its subject was used by "
            f"{names.get('agents_of_subject') or []}"
        )
    if not reviewer["lease_id"]:
        return CLIENT_REPORTED_ONLY, "the backend holds no lease for the reviewing task"
    if not reviewer["identities"]:
        return CLIENT_REPORTED_ONLY, "no host record shows which worker did the review"
    if author["task_id"] and not author["identities"]:
        return CLIENT_REPORTED_ONLY, "no host record shows which worker did the authoring task"
    if author["identities"] and author["hosts"].isdisjoint(reviewer["hosts"]):
        return DIFFERENT_HOST, "the host-recorded workers are on different hosts"
    if reviewer.get("native_source") == "codex_native_events_v1" and reviewer.get("fresh_context") is not True:
        if reviewer.get("fresh_context") is False:
            return NOT_INDEPENDENT, "the official native reviewer inherited a forked context; this MVP only admits explicit fresh reviewers"
        return CLIENT_REPORTED_ONLY, "the official native metadata did not establish a fresh reviewing context"
    if not author["task_id"]:
        return FRESH_CONTEXT_SAME_HOST, (
            "the content was not written by a worker under a task here (an import or a backend-built "
            "object); the reviewer is a host-recorded worker with its own lease and another principal"
        )
    return FRESH_CONTEXT_SAME_HOST, "two leases and two host-recorded workers"


def independence_level(conn: sqlite3.Connection, *, author_task_id: str | None, reviewer_task_id: str) -> str:
    """The independence the backend can show between the work of two tasks."""
    return explain_independence(conn, author_task_id=author_task_id, reviewer_task_id=reviewer_task_id)["level"]


def explain_independence(
    conn: sqlite3.Connection,
    *,
    author_task_id: str | None,
    reviewer_task_id: str,
    author_lease_id: str | None = None,
    author_principal: str | None = None,
    reviewer_lease_id: str | None = None,
) -> dict[str, Any]:
    """The level with the facts it rests on, for the record and for the refusal message."""
    author = worker_facts(conn, author_task_id, lease_id=author_lease_id)
    reviewer = worker_facts(conn, reviewer_task_id, lease_id=reviewer_lease_id)
    level, why = _level(author, reviewer, author_principal=author_principal)
    return {
        "level": level, "why": why,
        "author": {"task_id": author["task_id"], "lease_id": author["lease_id"],
                   "principal": author["principal"] or author_principal,
                   "workers": sorted(a for _h, a in author["identities"])},
        "reviewer": {"task_id": reviewer["task_id"], "lease_id": reviewer["lease_id"],
                     "principal": reviewer["principal"],
                     "workers": sorted(a for _h, a in reviewer["identities"]),
                     "observation_ids": reviewer["observation_ids"]},
    }


# ------------------------------------------------------------------------------ records

def review_roles(info: Mapping[str, Any]) -> set[str]:
    """Submission content review has a verifier role and is not an asset gate."""
    return {"verifier"} if info.get("review_scope") == "submission" else gate.review_roles(info["governed_kind"])


def resolve_review_subject(ctx: Ctx, subject: Subject, *, run_id: str | None = None) -> dict[str, Any]:
    """Authorize a submission's stored project before its resolver re-hashes any bytes."""
    if subject.kind not in (subjects.ASSET, subjects.SUBMISSION):
        raise SchemaInvalid("reviews accept an asset or a submission", details={"subject_kind": subject.kind})
    if subject.kind == subjects.SUBMISSION:
        owner = ctx.conn.execute("SELECT t.run_id, r.project_ref FROM submissions s JOIN tasks t ON t.task_id=s.task_id"
                                  " JOIN runs r ON r.run_id=t.run_id WHERE s.submission_id=?", (subject.id,)).fetchone()
        if owner is not None and (not ctx.principal.may_access_project(owner["project_ref"])
                                  or (run_id is not None and owner["run_id"] != run_id)):
            raise ScopeDenied("the submission review stays in its stored run and project",
                              details={"submission_id": subject.id})
    info = subjects.resolve(ctx.conn, subject)
    if info.get("review_scope") == "submission":
        owner = ctx.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (info.get("run_id"),)).fetchone()
        if owner is None or not ctx.principal.may_access_project(owner["project_ref"]):
            raise ScopeDenied("the submission review belongs to another project")
    return info

def _view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "review_id": row["review_id"], "verdict": row["verdict"],
        "independence_level": row["independence_level"], "at": row["created_at"],
        "task_id": row["task_id"], "lease_id": row["lease_id"], "principal": row["principal"],
        "worker_observation_id": row["worker_observation_id"], "superseded": bool(row["superseded"]),
        "bundle_artifact_ref": row["bundle_artifact_ref"],
        "subject": {"kind": row["subject_kind"], "id": row["subject_id"],
                    "version": row["subject_version"], "sha256": row["subject_sha256"]},
    }


def reviews_for(conn: sqlite3.Connection, subject: Subject, *, include_superseded: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM reviews WHERE subject_kind=? AND subject_id=? AND subject_sha256=?"
        + ("" if include_superseded else " AND superseded=0") + " ORDER BY rowid",
        (subject.kind, subject.id, subject.sha256),
    )
    return [_view(row) for row in rows]


def _insert(
    ctx: Ctx, subject: Subject, version: str | None, *, task_id: str | None, lease_id: str | None,
    principal: str | None, worker_observation_id: str | None, level: str, verdict: str,
    rubric: Sequence[Any], bundle_artifact_ref: str | None, basis: Mapping[str, Any],
    in_role: bool = True,
) -> dict[str, Any]:
    review_id = _ids.new_id("review")
    at = clock.now_iso()
    sufficient = level in contracts.sufficient_independence() and in_role
    with store.transaction(ctx.conn) as conn:
        conn.execute(
            "INSERT INTO reviews(review_id, subject_kind, subject_id, subject_version, subject_sha256,"
            " task_id, lease_id, principal, worker_observation_id, independence_level, verdict,"
            " rubric_json, bundle_artifact_ref, superseded, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (review_id, subject.kind, subject.id, version, subject.sha256, task_id, lease_id, principal,
             worker_observation_id, level, verdict, store._j(list(rubric or [])), bundle_artifact_ref, at),
        )
        store.append_event(
            conn, kind="review.recorded", actor=ctx.actor, subject_kind=subject.kind, subject_id=subject.id,
            reason=verdict, evidence_level="EXECUTION_OBSERVED",
            payload={"review_id": review_id, "verdict": verdict, "independence_level": level,
                     "sha256": subject.sha256, "version": version, "task_id": task_id,
                     "independence_basis": dict(basis)},
        )
    return {
        "review_id": review_id, "verdict": verdict, "independence_level": level, "at": at,
        "meets_gate": sufficient and verdict == APPROVE,
        "independence_basis": dict(basis),
    }


def record_review(
    ctx: Ctx,
    *,
    subject: Subject,
    task: Mapping[str, Any],
    verdict: str,
    rubric: Sequence[Any] = (),
    bundle_artifact_ref: str | None = None,
) -> dict[str, Any]:
    """Record the review a review task handed in. Called by the review bundle's validator.

    The subject must be the one the task was created for, at the sha256 the store holds now.
    Independence is computed here from the subject's author and the task's lease and host
    records; a review that is NOT_INDEPENDENT is refused with ``INDEPENDENCE_REQUIRED`` and
    leaves no row.
    """
    contracts.require_label("review_verdict", verdict)
    info = resolve_review_subject(ctx, subject)
    if info["current_sha256"] != subject.sha256:
        raise ContentHashMismatch(
            f"the review is about sha256 {subject.sha256[:16]}… and the store holds "
            f"{str(info['current_sha256'])[:16]}…; a review belongs to the bytes that were read",
            details={"subject": subjects.as_dict(subject), "current_sha256": info["current_sha256"]},
        )
    task_id = task.get("task_id")
    if not task_id:
        raise SchemaInvalid("a worker's review is handed in under a review task; this one names no task")
    for key, expected in (("subject_kind", subject.kind), ("subject_id", subject.id), ("subject_sha256", subject.sha256)):
        if task.get(key) != expected:
            raise ContentHashMismatch(
                f"review task {task_id} was created for {task.get('subject_kind')} "
                f"{task.get('subject_id')} at sha256 {str(task.get('subject_sha256'))[:16]}…, not for "
                "the subject of this review",
                details={"task_id": task_id, "task_subject": {k: task.get(k) for k in
                         ("subject_kind", "subject_id", "subject_sha256")},
                         "review_subject": subjects.as_dict(subject)},
            )
    if human_task_review(ctx, task):
        basis = {"level": HUMAN, "why": "a person claimed and submitted this review through the confirmed operator surface",
                 "reviewer": {"lease_id": task["lease_id"], "principal": f"person:{ctx.principal.subject}",
                              "observation_ids": []}, "transport": ctx.transport}
    else:
        author_task_id = task.get("independent_of_task_id") or info["author_task_id"]
        basis = explain_independence(
            ctx.conn, author_task_id=author_task_id, reviewer_task_id=task_id,
            author_lease_id=info["author_lease_id"] if author_task_id == info["author_task_id"] else None,
            author_principal=info["author_principal"], reviewer_lease_id=task.get("lease_id"),
        )
    if basis["level"] == NOT_INDEPENDENT:
        raise IndependenceRequired(
            f"the review of {subject.id}@{info['version']} is not independent of its author: {basis['why']}",
            details={"subject": subjects.as_dict(subject), "independence": basis},
        )
    # The gate table names the role a review is done in. A review handed in under a task of
    # another role is kept and says so; the gate reads the task's role and does not count it.
    roles = review_roles(info)
    task_role = task.get("role")
    if task_role is None:
        row = ctx.conn.execute("SELECT role FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        task_role = row["role"] if row is not None else None
    basis["role"] = {"task_role": task_role, "gate_roles": sorted(roles)}
    observation_ids = basis["reviewer"]["observation_ids"]
    return _insert(
        ctx, subject, info["version"], task_id=task_id, lease_id=basis["reviewer"]["lease_id"],
        principal=basis["reviewer"]["principal"],
        worker_observation_id=observation_ids[-1] if observation_ids else None,
        level=basis["level"], verdict=verdict, rubric=rubric, bundle_artifact_ref=bundle_artifact_ref,
        basis=basis, in_role=(not roles or task_role in roles),
    )


def human_task_review(ctx: Ctx, task: Mapping[str, Any]) -> bool:
    """r5: a terminal-confirmed operator can deliver a leased review at HUMAN level.

    The lease/task binding is retained; declaring operator scope or a flag in the bundle
    cannot turn a model's hand-in into a person's review.
    """
    if ctx.principal.scope != SCOPE_OPERATOR or not approvals.person_shown(ctx):
        return False
    task_id, lease_id = task.get("task_id"), task.get("lease_id")
    if not task_id or not lease_id:
        return False
    row = ctx.conn.execute("SELECT kind, lease_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None or row["kind"] != "review_asset" or row["lease_id"] != lease_id:
        return False
    lease = store.validate_lease(ctx.conn, lease_id, task_id=task_id)
    return lease["principal"] == ctx.principal.subject and lease["owner"] == ctx.actor


def record_human_review(
    ctx: Ctx, *, subject: Subject, verdict: str, rubric: Sequence[Any] = (), note: str = "",
) -> dict[str, Any]:
    """A person's review, recorded through the operator surface after the call was shown to them."""
    contracts.require_label("review_verdict", verdict)
    if ctx.principal.scope != SCOPE_OPERATOR or not approvals.person_shown(ctx):
        raise PermissionDenied(
            "a review at the HUMAN level is recorded by a person through the operator surface; this "
            "call was not shown to one",
            details={"scope": ctx.principal.scope},
        )
    info = resolve_review_subject(ctx, subject)
    if info["current_sha256"] != subject.sha256:
        raise ContentHashMismatch(
            "the review is not about the bytes the store holds",
            details={"subject": subjects.as_dict(subject), "current_sha256": info["current_sha256"]},
        )
    basis = {"level": HUMAN, "why": "recorded by a person through the operator surface",
             "reviewer": {"principal": ctx.principal.subject, "transport": ctx.transport}, "note": note}
    return _insert(
        ctx, subject, info["version"], task_id=None, lease_id=None,
        principal=f"person:{ctx.principal.subject}", worker_observation_id=None, level=HUMAN,
        verdict=verdict, rubric=rubric, bundle_artifact_ref=None, basis=basis,
    )


REVIEW_PACKAGE_KIND = "review_package"


def package_body(subject: Subject, package: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical review content. Its subject is the backend-resolved subject."""
    return {**dict(package), "subject": subjects.as_dict(subject)}


def open_review_tasks(conn: sqlite3.Connection, subject: Subject, *, task_kind: str = "review_asset") -> list[str]:
    """Open asset-review tasks bound to the subject and digest the caller will inspect."""
    return [
        row["task_id"] for row in conn.execute(
            "SELECT task_id FROM tasks WHERE kind=? AND subject_kind=? "
            "AND subject_id=? AND subject_sha256=? AND status NOT IN ('ACCEPTED', 'CANCELLED') ORDER BY rowid",
            (task_kind, subject.kind, subject.id, subject.sha256),
        )
    ]


#: ``dependencies.relation`` of the row that says who waits for a task: (subject = the
#: waiter) waits on (depends_on = the task) — the scheduler reads it (``gx_core.scheduling``).
WAITS_ON = "waits_on"


def record_waiter(conn: sqlite3.Connection, *, waiter_kind: str, waiter_id: str, task_id: str) -> None:
    """Write that ``waiter`` waits on ``task_id`` (a gate on a review, an answer set, …). Idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO dependencies(subject_kind, subject_id, depends_on_kind, depends_on_id, relation,"
        " created_at) VALUES(?,?,'task',?,?,?)",
        (waiter_kind, waiter_id, task_id, WAITS_ON, clock.now_iso()),
    )


def request_review(
    ctx: Ctx,
    *,
    subject: Subject,
    run_id: str,
    package: Mapping[str, Any],
    task_kind: str = "review_asset",
    acceptance: Mapping[str, Any] | None = None,
    follows: Sequence[str] = (),
    stage: str | None = None,
    reservation_ref: str | None = None,
) -> dict[str, Any]:
    """Create the review task for one subject at one sha256, in ``run_id``.

    ``package`` is what the reviewer is to judge — ``{"items": [{"item_id", …}], …}`` — and
    is stored as an artifact the task names by reference: the reviewer receives each item
    with what it is supposed to support, not the author's reasoning. The task is for the
    role the gate table names. An open task is reused only for the same run, task kind
    and exact canonical review package. A byte-level review and a later comparison review
    of those bytes are different work. Returns ``{"task_id", "created", "review_package_ref", "items"}``.

    Lineage (the scheduler orders a task by the chain behind it): the review depends on the
    task that authored its subject when that task is in this run — already ACCEPTED, or about
    to be, so the edge costs nothing and records where the review came from — and is also
    independent of it. ``follows`` names further tasks of this run it follows (a revision's
    review follows the objection it answers). The subject's gate waits on the review: that is
    written as a ``waits_on`` row, so a review something is waiting for is handed out first.
    """
    with store.transaction(ctx.conn):
        return _request_review(ctx, subject=subject, run_id=run_id, package=package, task_kind=task_kind,
                               acceptance=acceptance, follows=follows, stage=stage, reservation_ref=reservation_ref)


def _request_review(
    ctx: Ctx, *, subject: Subject, run_id: str, package: Mapping[str, Any], task_kind: str,
    acceptance: Mapping[str, Any] | None, follows: Sequence[str], stage: str | None, reservation_ref: str | None,
) -> dict[str, Any]:
    info = resolve_review_subject(ctx, subject, run_id=run_id)
    if info["current_sha256"] != subject.sha256:
        raise ContentHashMismatch(
            "a review is requested for the bytes the store holds",
            details={"subject": subjects.as_dict(subject), "current_sha256": info["current_sha256"]},
        )
    conn = ctx.conn
    from . import _artifacts  # local: this module is imported by the artifact helpers' users
    body = package_body(subject, package)
    content = _artifacts.canonical_bytes(body)
    expected_sha = hashlib.sha256(content).hexdigest()
    roles = review_roles(info)
    if len(roles) != 1:
        raise SchemaInvalid(
            f"the gate table names {sorted(roles) or 'no role'} for a review of a {info['governed_kind']}; "
            "a review task is created for exactly one",
            details={"governed_kind": info["governed_kind"], "roles": sorted(roles)},
        )
    for task_id in open_review_tasks(conn, subject, task_kind=task_kind):
        row = conn.execute("SELECT run_id, kind, role, stage, input_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        held = store.loads(row, "input_json", {}) or {}
        if (row["run_id"] != run_id or row["kind"] != task_kind or row["role"] not in roles
                or (stage is not None and row["stage"] != stage)
                or held.get("review_package_sha256") != expected_sha or not held.get("review_package_ref")):
            continue
        # Verify the registered bytes, not just a hash asserted in task input.
        if _artifacts.read_bytes(conn, held["review_package_ref"]) != content:
            continue
        return {"task_id": task_id, "created": False, "review_package_ref": held["review_package_ref"],
                "review_package_sha256": expected_sha, "items": None}
    from ..teams.service import TeamService  # local: the task service loads the bundle framework

    published = _artifacts.publish_content_addressed(
        conn, data=content, kind=REVIEW_PACKAGE_KIND, actor="backend",
        area="review_packages", provenance={"subject": subjects.as_dict(subject)},
    )
    author_task = info["author_task_id"]
    if author_task:
        same_run = conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND run_id=?", (author_task, run_id)).fetchone()
        if same_run is None:
            author_task = None  # independence is computed against the subject's author when the review arrives
    lineage = list(dict.fromkeys(([author_task] if author_task else []) + [
        task_id for task_id in follows
        if conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone() is not None
    ]))
    items = [item for item in body.get("items") or [] if isinstance(item, Mapping) and item.get("item_id")]
    task = TeamService(conn).create_task(
        run_id=run_id, kind=task_kind, role=next(iter(roles)), bundle_kind="asset_review_bundle",
        acceptance={"a_verdict_for_each_of": len(items), **dict(acceptance or {})},
        input={"review_package_ref": published.artifact_ref, "review_package_sha256": published.sha256,
               "subject": f"{subject.id}@{info['version']}", "governed_kind": info["governed_kind"],
               **({"review_cycle_id": body["cycle_id"]} if body.get("cycle_id") else {})},
        subject=subject, independent_of_task_id=author_task, depends_on=lineage,
        stage=stage, reservation_ref=reservation_ref,
    )
    record_waiter(conn, waiter_kind=subject.kind, waiter_id=f"{subject.id}@{info['version']}", task_id=task["task_id"])
    store.append_event(
        conn, kind="review.requested", actor="backend", run_id=run_id, subject_kind=subject.kind,
        subject_id=subject.id, evidence_level="EXECUTION_OBSERVED",
        payload={"sha256": subject.sha256, "version": info["version"], "task_id": task["task_id"],
                 "review_package_ref": published.artifact_ref, "items": len(items), "follows": lineage},
    )
    return {"task_id": task["task_id"], "created": True, "review_package_ref": published.artifact_ref,
            "review_package_sha256": published.sha256, "items": len(items)}


def default_package(conn: sqlite3.Connection, asset: Mapping[str, Any]) -> dict[str, Any]:
    """What the reviewer of an asset is given when its kind's owner has published no package.

    The asset by reference, and as items the findings of its own check runs that nobody has
    adjudicated yet (``diag:<fingerprint>``): each gets a verdict, and an approving review
    adjudicates them. What else to read is the brief's (the owner's brief section for the
    kind). Read-only.
    """
    from . import checks  # local: checks imports this module's neighbours

    subject = Subject(kind=subjects.ASSET, id=asset["asset_id"], sha256=asset["sha256"], version=asset["version"])
    items: list[dict[str, Any]] = []
    for check_id, latest in sorted(checks.latest_results(conn, subject).items()):
        if latest["result"] != checks.DIAGNOSTIC or not latest["unadjudicated"]:
            continue
        run = checks.get_run(conn, latest["check_run_id"])
        wanted = set(latest["unadjudicated"])
        for diagnostic in run["diagnostics"]:
            if diagnostic.get("fingerprint") in wanted:
                items.append({
                    "item_id": f"diag:{diagnostic['fingerprint']}", "kind": "diagnostic", "check_id": check_id,
                    "check_run_id": latest["check_run_id"], "fingerprint": diagnostic["fingerprint"],
                    "finding": {k: v for k, v in diagnostic.items() if k != "fingerprint"},
                })
    return {
        "asset": {"asset_id": asset["asset_id"], "version": asset["version"], "sha256": asset["sha256"],
                  "kind": asset["kind"], "title": asset.get("title"),
                  "content_artifact_ref": asset.get("content_artifact_ref")},
        "items": items,
        "instructions": (
            "Judge these bytes, not their description: read the content by reference and what the brief lays "
            "out. Each item needs a verdict: SUPPORTED when a finding is something this asset has to do, the "
            "verdict that says how it falls short otherwise. An objection is REQUEST_CHANGES or REJECT with the "
            "reason in notes: it blocks these bytes, and the answer to it is a new version."
        ),
    }


def request_review_for(
    ctx: Ctx, asset: Mapping[str, Any], *, run_id: str, follows: Sequence[str] = (),
    stage: str | None = None, reservation_ref: str | None = None,
) -> dict[str, Any]:
    """Request the review of an asset with the package its kind's owner publishes (or the default)."""
    from . import effects  # local: effects imports registry, which loads providers

    builder = effects.package_builder(asset["kind"])
    package = dict(builder(ctx.conn, asset) if builder is not None else default_package(ctx.conn, asset))
    acceptance = dict(package.pop("acceptance", None) or {})
    subject = Subject(kind=subjects.ASSET, id=asset["asset_id"], sha256=asset["sha256"], version=asset["version"])
    return request_review(ctx, subject=subject, run_id=run_id, package=package, acceptance=acceptance,
                          follows=follows, stage=stage, reservation_ref=reservation_ref)


def invalidate(conn: sqlite3.Connection, subject: Subject) -> int:
    """Mark every review of this sha256 as no longer counting. Called on requarantine only."""
    cur = conn.execute(
        "UPDATE reviews SET superseded=1 WHERE subject_kind=? AND subject_id=? AND subject_sha256=?"
        " AND superseded=0",
        (subject.kind, subject.id, subject.sha256),
    )
    return int(cur.rowcount or 0)


def get(conn: sqlite3.Connection, review_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no review {review_id!r}", details={"review_id": review_id})
    return _view(row)
