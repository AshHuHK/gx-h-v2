"""The asset registry and the one promotion function.

Decision skills, executable workflows, tool adapters (capabilities), benchmarks, strategies
and role intents are all rows of ``assets`` and all move through the ``asset`` machine of
``contracts/states.json`` by one function, :func:`promote`. (An evaluation specification
has its own machine and its own table; it is checked by the same engine through
:mod:`gx_core.governance.gate`.)

    out = submit_asset(ctx, kind="DECISION_SKILL", manifest=m, content_ref=ref, origin="AUTHORED")
    promote(ctx, asset_id, version, "REVIEWED", expected_sha256=out["asset_ref"]["sha256"])
    require_active(conn, asset_id, version, sha256)       # what anyone naming an asset calls
    active_assets(conn, "DECISION_SKILL")

**A version is its bytes.** The content of a version is one JSON artifact with a top-level
``manifest``; its sha256 is the version's identity. The same id and version with the same
sha256 is the same registration; with different bytes it is ``VERSION_IMMUTABLE``.
Imported and downloaded content lands QUARANTINED.

**A promotion is earned.** ``promote`` re-hashes the content, runs the checks the transition
lists that are not already passing, asks for the approval the transition needs (the user's
standing policy answers at once), and then reads the gate from rows the backend wrote for
that sha256. Anything missing is ``GATE_NOT_MET`` with the list. Nothing in the call — not
the caller's scope, not an argument — stands in for a row.

**A promotion does something.** The effects the gate table names for the new state run
inside the same transaction, published by whichever stream owns what they touch
(:mod:`gx_core.governance.effects`). A state whose effect nobody implements is not entered.

**Becoming ACTIVE is announced.** The transaction that makes a version ACTIVE writes one
``asset.active`` outbox row (:func:`active_trigger`), so a run with a path that was waiting
for that version, or for the one it supersedes, is moved by the row and not by whoever
happens to look next.

**Going back wipes the slate.** Returning to QUARANTINED marks every check run and review
of that sha256 as no longer counting and withdraws its approvals.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping

from .. import contracts
from ..context import Ctx
from ..domain import clock, machines
from ..errors import (
    ArtifactHashMismatch,
    ArtifactNotFound,
    ContentHashMismatch,
    GateNotMet,
    GxError,
    ImplementationGap,
    SchemaInvalid,
    VersionImmutable,
)
from ..persistence import store
from ..registry import Subject
from . import _artifacts, approvals, checks, effects, gate, reviews, subjects

MACHINE = "asset"
DRAFT = machines.initial_state(MACHINE)
QUARANTINED, REVIEWED, VALIDATED, APPROVED, ACTIVE, DEPRECATED = (
    "QUARANTINED", "REVIEWED", "VALIDATED", "APPROVED", "ACTIVE", "DEPRECATED",
)
for _name in (QUARANTINED, REVIEWED, VALIDATED, APPROVED, ACTIVE, DEPRECATED):
    if _name not in machines.all_states(MACHINE):  # pragma: no cover - the contract moved
        raise RuntimeError(f"asset state {_name!r} left the contract")

EVALUATOR = subjects.EVALUATOR
#: Origins whose content lands QUARANTINED (labels.asset_origin, note).
QUARANTINED_ORIGINS = ("IMPORTED", "DOWNLOADED")
for _name in QUARANTINED_ORIGINS:
    contracts.require_label("asset_origin", _name)

#: The manifest field that carries each kind's id. One id space: an id names one kind.
ID_FIELD: dict[str, str] = {
    "DECISION_SKILL": "asset_id",
    "EXECUTABLE_WORKFLOW": "workflow_id",
    "TOOL_ADAPTER": "capability_id",
    "STRATEGY": "strategy_kind",
    "BENCHMARK": "benchmark_id",
    "ROLE_INTENT": "role_id",
}
for _name in ID_FIELD:
    contracts.require_label("asset_kind", _name)

BYTE_BINDING = "byte_binding"
contracts.check_spec(BYTE_BINDING)

#: The topic of the trigger a version writes when it becomes ACTIVE (SCHEDULING_DESIGN r2.2 §2.4.1).
ACTIVE_TOPIC = "asset.active"


def version_ref(asset_id: str, version: str) -> str:
    """``<asset_id>@<version>``: one word for one version. Neither part can contain ``@``."""
    return f"{asset_id}@{version}"


def active_trigger_key(asset_id: str, version: str) -> str:
    """The idempotency key of a version's ``asset.active`` outbox row: one row per version,
    ever, because the machine leaves ACTIVE only for DEPRECATED."""
    return f"{ACTIVE_TOPIC}:{version_ref(asset_id, version)}"


def active_trigger(asset: Mapping[str, Any], event_id: int, supersedes: str | None) -> dict[str, Any]:
    """What an ``asset.active`` outbox row says: which version of which kind became ACTIVE, with
    which bytes, the transition event that records it, and the registered version it takes the
    place of (``<asset_id>@<version>``, or ``None``). ``run_id`` is always ``None``: an ACTIVE
    version is a fact of the store that a path of any open run may have been waiting for, and
    the run it was authored in owns nothing of it. Identifiers and state words only."""
    return {"run_id": None, "subject": version_ref(asset["asset_id"], asset["version"]), "event_id": event_id,
            "asset_kind": asset["kind"], "sha256": asset["sha256"], "supersedes": supersedes}


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


# ------------------------------------------------------------------------------ reading

def _view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "asset_id": row["asset_id"], "version": row["version"], "kind": row["kind"],
        "status": row["status"], "title": row["title"], "sha256": row["content_sha256"],
        "content_artifact_ref": row["content_artifact_ref"],
        "manifest": store.loads(row, "manifest_json", {}) or {},
        "source": store.loads(row, "source_json", {}) or {},
        "origin": row["origin"], "origin_ref": row["origin_ref"],
        "author_principal": row["author_principal"], "author_task_id": row["author_task_id"],
        "author_lease_id": row["author_lease_id"], "namespace": row["namespace"],
        "license": row["license"], "license_basis": row["license_basis"],
        "limitations": row["limitations"], "superseded_by": row["superseded_by"],
        "test_status": row["test_status"], "created_at": row["created_at"],
        "promoted_at": row["promoted_at"],
    }


def asset_ref(asset: Mapping[str, Any]) -> dict[str, str]:
    return {"asset_id": asset["asset_id"], "version": asset["version"], "sha256": asset["sha256"]}


def _row(conn: sqlite3.Connection, asset_id: str, version: str | None, kind: str | None = None) -> sqlite3.Row:
    if version is not None:
        row = conn.execute(
            "SELECT * FROM assets WHERE asset_id=? AND version=?", (asset_id, str(version))
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM assets WHERE asset_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (asset_id,)
        ).fetchone()
    if row is None or (kind is not None and row["kind"] != kind):
        raise ArtifactNotFound(
            f"no asset {asset_id!r}" + (f" at version {version!r}" if version is not None else "")
            + (f" of kind {kind}" if kind else ""),
            details={"asset_id": asset_id, "version": version, "kind": kind},
        )
    return row


def get(conn: sqlite3.Connection, asset_id: str, version: str | None = None, *, kind: str | None = None) -> dict[str, Any]:
    """One asset version; the most recently registered one when no version is named."""
    return _view(_row(conn, asset_id, version, kind))


def versions(conn: sqlite3.Connection, asset_id: str) -> list[dict[str, Any]]:
    return [_view(row) for row in conn.execute(
        "SELECT * FROM assets WHERE asset_id=? ORDER BY created_at, rowid", (asset_id,))]


def require_active(
    conn: sqlite3.Connection, asset_id: str, version: str, sha256: str | None = None,
) -> dict[str, Any]:
    """The asset, if this exact version is ACTIVE (and, when given, is these bytes).

    Raises ``ARTIFACT_NOT_FOUND`` when there is no such version, ``CONTENT_HASH_MISMATCH``
    when it is not the sha256 the caller cites, and ``GATE_NOT_MET`` when it has not reached
    ACTIVE — with what its next transition still lacks.
    """
    asset = _view(_row(conn, asset_id, version))
    if sha256 is not None and sha256 != asset["sha256"]:
        raise ContentHashMismatch(
            f"{asset_id}@{version} is registered at sha256 {asset['sha256'][:16]}…, not the cited "
            f"{sha256[:16]}…; a changed asset is a new version and takes the gates again",
            details={"asset_id": asset_id, "version": version, "registered_sha256": asset["sha256"],
                     "cited_sha256": sha256},
        )
    if asset["status"] != ACTIVE:
        upcoming = next_state(asset["status"])
        missing = None
        if upcoming is not None:
            missing = gate.evaluate(conn, kind=asset["kind"], subject=_subject(asset), to_state=upcoming,
                                    manifest=asset["manifest"])["missing"]
        raise GateNotMet(
            f"{asset_id}@{version} is {asset['status']}, not {ACTIVE}: {machines.rule(MACHINE, 'active_gate')}",
            details={"asset_id": asset_id, "version": version, "status": asset["status"],
                     "next_state": upcoming, "missing": missing},
        )
    return asset


def active_assets(conn: sqlite3.Connection, kind: str, namespace: str | None = None) -> list[dict[str, Any]]:
    """Every ACTIVE asset of ``kind`` visible from ``namespace`` (all of them when None).

    An asset registered with no namespace is visible everywhere; one registered under a
    namespace is visible from that namespace and from anything beneath it.
    """
    contracts.require_label("asset_kind", kind)
    rows = conn.execute(
        "SELECT * FROM assets WHERE kind=? AND status=? ORDER BY asset_id, created_at", (kind, ACTIVE)
    )
    out = []
    for row in rows:
        own = row["namespace"]
        if namespace is None or own is None or namespace == own or namespace.startswith(own + "/"):
            out.append(_view(row))
    return out


def next_state(status: str) -> str | None:
    """The state that follows on the way to ACTIVE, or None when there is none."""
    for target in machines.legal_targets(MACHINE, status):
        if target not in (QUARANTINED, DEPRECATED):
            return target
    return None


# ----------------------------------------------------------------------------- content

def publish_content(ctx: Ctx, bundle: Mapping[str, Any]) -> str:
    """Publish a bundle as the content of an asset version and return its artifact ref.

    The bytes are the bundle's canonical JSON (sorted keys, no insignificant whitespace), so
    the sha256 does not depend on how the author formatted it, and they are stored by that
    sha256, so the same content is one artifact whoever registers it.
    """
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("manifest"), Mapping):
        raise SchemaInvalid("the content of an asset version is an object with a top-level 'manifest'")
    published = _artifacts.publish_content_addressed(
        ctx.conn, data=_artifacts.canonical_bytes(bundle), kind="asset_content", actor=ctx.actor,
        provenance={"published_by": ctx.actor, "tool": ctx.tool},
    )
    return published.artifact_ref


def content(conn: sqlite3.Connection, asset: Mapping[str, Any]) -> dict[str, Any]:
    """The content of an asset version, re-hashed against the registered sha256 before it is used."""
    ref = asset["content_artifact_ref"]
    recorded = _artifacts.recorded_sha256(conn, ref)
    if recorded != asset["sha256"]:
        raise ContentHashMismatch(
            f"{asset['asset_id']}@{asset['version']} is registered at {asset['sha256'][:16]}… but its "
            f"content artifact is recorded at {recorded[:16]}…",
            details={"asset_id": asset["asset_id"], "version": asset["version"],
                     "registered_sha256": asset["sha256"], "artifact_sha256": recorded},
        )
    try:
        return _artifacts.read_json(conn, ref)
    except ArtifactHashMismatch as exc:
        raise ContentHashMismatch(
            f"the bytes of {asset['asset_id']}@{asset['version']} changed under its sha256",
            details={"asset_id": asset["asset_id"], "version": asset["version"], **exc.details},
        ) from exc


# ------------------------------------------------------------------------------ intake

def _identify(kind: str, manifest: Mapping[str, Any]) -> tuple[str, str]:
    field = ID_FIELD[kind]
    asset_id, version = manifest.get(field), manifest.get("version")
    if not (isinstance(asset_id, str) and _SAFE_ID.fullmatch(asset_id)):
        raise SchemaInvalid(
            f"a {kind} manifest names its id in {field!r}: letters, digits, '.', '_' and '-', at most "
            "128 characters, starting with a letter or digit",
            details={"field": field, "value": asset_id},
        )
    if not (isinstance(version, str) and _SAFE_VERSION.fullmatch(version)):
        raise SchemaInvalid(
            "a manifest names its version as a string of letters, digits, '.', '_', '+' and '-'",
            details={"field": "version", "value": version},
        )
    return asset_id, version


def submit_asset(
    ctx: Ctx,
    *,
    kind: str,
    manifest: Mapping[str, Any],
    content_ref: str,
    origin: str,
    run_id: str | None = None,
    task_id: str | None = None,
    namespace: str | None = None,
    source: Mapping[str, Any] | None = None,
    origin_ref: str | None = None,
) -> dict[str, Any]:
    """Register a candidate asset version from content the backend already holds.

    Registers; does not run a check and does not promote. Returns ``{"asset_ref": {asset_id,
    version, sha256}, "kind", "status", "created"}``. ``created`` is False when this exact
    content was already registered under this id and version.
    """
    contracts.require_label("asset_kind", kind)
    contracts.require_label("asset_origin", origin)
    if kind == EVALUATOR:
        raise SchemaInvalid(
            "an evaluator is registered through the evaluation builder: it follows the eval_spec "
            "machine, and the governance engine checks it through gate.evaluate",
            details={"kind": kind},
        )
    if not isinstance(manifest, Mapping):
        raise SchemaInvalid("a manifest is an object")
    asset_id, version = _identify(kind, manifest)
    conn = ctx.conn

    # read_json re-hashes the bytes against the artifact's record before parsing them, so
    # content that changed since it was published is refused here (ARTIFACT_HASH_MISMATCH).
    held = _artifacts.read_json(conn, content_ref)
    sha256 = _artifacts.recorded_sha256(conn, content_ref)
    if not isinstance(held, Mapping) or held.get("manifest") != dict(manifest):
        raise SchemaInvalid(
            "the manifest being registered is not the one inside the content artifact; the sha256 of "
            "a version covers its manifest, so the two must be the same object",
            details={"content_ref": content_ref, "asset_id": asset_id, "version": version},
        )

    existing = conn.execute(
        "SELECT * FROM assets WHERE asset_id=? AND version=?", (asset_id, version)
    ).fetchone()
    if existing is not None:
        if existing["content_sha256"] == sha256 and existing["kind"] == kind:
            view = _view(existing)
            return {"asset_ref": asset_ref(view), "kind": kind, "status": view["status"], "created": False,
                    "content_artifact_ref": view["content_artifact_ref"]}
        raise VersionImmutable(
            f"{asset_id}@{version} is already registered as a {existing['kind']} at sha256 "
            f"{str(existing['content_sha256'])[:16]}…; a version cannot change in place — register a new one",
            details={"asset_id": asset_id, "version": version, "registered_kind": existing["kind"],
                     "registered_sha256": existing["content_sha256"], "offered_sha256": sha256},
        )
    other_kind = conn.execute(
        "SELECT kind FROM assets WHERE asset_id=? AND kind != ? LIMIT 1", (asset_id, kind)
    ).fetchone()
    if other_kind is not None:
        raise SchemaInvalid(
            f"the id {asset_id!r} already names a {other_kind['kind']}; asset ids are one id space",
            details={"asset_id": asset_id, "registered_kind": other_kind["kind"], "offered_kind": kind},
        )

    author_lease_id = None
    if task_id:
        task = conn.execute("SELECT lease_id, run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ArtifactNotFound(f"no task {task_id!r}", details={"task_id": task_id})
        author_lease_id = task["lease_id"]
        run_id = run_id or task["run_id"]
    if namespace is None and run_id:
        run = conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is not None and run["project_ref"]:
            # The project's own namespace: `real:<project>`, or the reference as written
            # when it already carries a realm (a synthetic test project stays synthetic).
            from ..knowledge.scopes import for_project

            namespace = for_project(run["project_ref"])

    now = clock.now_iso()
    source_record = {"run_id": run_id, "submitted_by": ctx.actor, **dict(source or {})}
    with store.transaction(conn) as tx:
        tx.execute(
            "INSERT INTO assets(asset_id, version, kind, status, title, content_sha256,"
            " content_artifact_ref, source_json, manifest_json, origin, origin_ref, author_principal,"
            " author_task_id, author_lease_id, namespace, license, license_basis, limitations, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset_id, version, kind, DRAFT, manifest.get("title") or asset_id, sha256, content_ref,
                store._j(source_record), store._j(dict(manifest)), origin, origin_ref,
                ctx.principal.subject, task_id, author_lease_id, namespace,
                manifest.get("licence") or manifest.get("license"),
                manifest.get("licence_basis") or manifest.get("license_basis"),
                manifest.get("limitations"), now,
            ),
        )
        store.append_event(
            tx, kind="asset.registered", actor=ctx.actor, run_id=run_id, subject_kind=subjects.ASSET,
            subject_id=asset_id, new_state=DRAFT, reason=origin, evidence_level="EXECUTION_OBSERVED",
            payload={"version": version, "kind": kind, "sha256": sha256, "origin": origin,
                     "content_artifact_ref": content_ref, "task_id": task_id},
        )
        status = DRAFT
        if origin in QUARANTINED_ORIGINS:
            store.transition(
                tx, machine=MACHINE, subject_id=asset_id, version=version, to_state=QUARANTINED,
                actor=ctx.actor, reason=f"{origin} content lands quarantined and takes every gate",
                evidence_level="EXECUTION_OBSERVED", payload={"version": version, "sha256": sha256},
            )
            status = QUARANTINED
    return {
        "asset_ref": {"asset_id": asset_id, "version": version, "sha256": sha256},
        "kind": kind, "status": status, "created": True, "content_artifact_ref": content_ref,
    }


# ------------------------------------------------------------------------------ checks

def run_required(
    ctx: Ctx, asset_id: str, version: str, to_state: str | None = None, *, force: bool = False,
) -> list[dict[str, Any]]:
    """Run the checks the asset's next transition (or ``to_state``) lists.

    ``byte_binding`` is always run: its whole point is the bytes as they are now. Any other
    check is run when it has no result for this sha256 or its latest result is FAIL or ERROR.
    A PASS is not repeated and a DIAGNOSTIC is left for its adjudications, unless ``force``.
    """
    asset = get(ctx.conn, asset_id, version)
    target = to_state or next_state(asset["status"])
    if target is None:
        return []
    needed = gate.required_checks(asset["kind"], target, asset["manifest"])
    subject = _subject(asset)
    latest = checks.latest_results(ctx.conn, subject)
    stale: set[str] = set()
    if asset["kind"] == "TOOL_ADAPTER":
        from ..capabilities import checks as capability_checks

        stale = capability_checks.stale_checks(ctx, subject, latest)
    to_run = [
        check_id for check_id in needed
        if force or check_id == BYTE_BINDING or check_id in stale or check_id not in latest
        or latest[check_id]["result"] in (checks.FAIL, checks.ERROR)
    ]
    return checks.run_checks(ctx, subject, to_run) if to_run else []


def _subject(asset: Mapping[str, Any]) -> Subject:
    return Subject(kind=subjects.ASSET, id=asset["asset_id"], sha256=asset["sha256"], version=asset["version"])


# --------------------------------------------------------------------------- promotion

def _compact(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "to_state": report["to_state"],
        "checks": {c["check_id"]: c.get("check_run_id") for c in report["checks"]},
        "review_id": report["review"]["review_id"] if report["review"] else None,
        "independence_level": report["review"]["independence_level"] if report["review"] else None,
        "approval_id": report["approval"]["approval_id"] if report["approval"] else None,
        "effects": list(report["effects"]),
    }


def _checked(conn: sqlite3.Connection, asset_id: str, version: str, to_state: str, expected_sha256: str) -> dict[str, Any]:
    """The asset, once the request is one the machine and the registered bytes allow."""
    asset = _view(_row(conn, asset_id, version))
    if expected_sha256 != asset["sha256"]:
        raise ContentHashMismatch(
            f"{asset_id}@{version} is registered at sha256 {asset['sha256'][:16]}…, not the expected "
            f"{str(expected_sha256)[:16]}…; what was reviewed and what is being promoted must be the same bytes",
            details={"asset_id": asset_id, "version": version, "registered_sha256": asset["sha256"],
                     "expected_sha256": expected_sha256},
        )
    machines.check_transition(MACHINE, asset["status"], to_state, subject_id=asset_id)
    absent = effects.missing(contracts.gate_requirements(asset["kind"], to_state)["effects"], asset["kind"])
    if absent:
        raise ImplementationGap(
            f"{to_state} has effect(s) {absent} for a {asset['kind']} and nothing publishes them; "
            f"{asset_id}@{version} stays {asset['status']} rather than entering a state nothing reads",
            details={"asset_id": asset_id, "version": version, "kind": asset["kind"], "to_state": to_state,
                     "unpublished_effects": absent},
        )
    return asset


def prepare_promotion(ctx: Ctx, asset_id: str, version: str, to_state: str, expected_sha256: str) -> dict[str, Any]:
    """Establish what the backend can establish now for a transition, and commit it.

    Re-hashes the content, runs the listed checks that are not passing, and asks for the
    approval the transition needs (the standing policy answers at once when the earlier
    gates are on record). Everything it writes stays written whether or not the promotion
    then goes through: a check that failed is part of the record.
    """
    asset = _checked(ctx.conn, asset_id, version, to_state, expected_sha256)
    kind, manifest, subject = asset["kind"], asset["manifest"], _subject(asset)
    requirements = contracts.gate_requirements(kind, to_state)
    ran: list[dict[str, Any]] = []
    if gate.required_checks(kind, to_state, manifest):
        ran = run_required(ctx, asset_id, version, to_state)
    approval = None
    if requirements["approval"]:
        pre = gate.evaluate(ctx.conn, kind=kind, subject=subject, to_state=to_state, manifest=manifest)
        if pre["approval"] is not None and not pre["approval"]["met"]:
            approval = approvals.request(
                ctx, action_kind=requirements["approval"]["action_kind"], subject=subject,
                request={"asset_kind": kind, "version": version, "to_state": to_state},
            )
    return {"checks_run": [{"check_id": r["check_id"], "result": r["result"],
                            "check_run_id": r["check_run_id"]} for r in ran],
            "approval": approval}


def commit_promotion(
    ctx: Ctx, asset_id: str, version: str, to_state: str, expected_sha256: str, *, reason: str | None = None,
) -> dict[str, Any]:
    """Read the gate from the rows on record and, if it is met, move the asset. One transaction.

    Runs no check and requests nothing: it only reads what :func:`prepare_promotion` and
    the review and approval paths wrote. The transition, its invalidations, its listeners
    and its effects succeed or fail together.
    """
    conn = ctx.conn
    with store.transaction(conn) as tx:
        asset = _checked(tx, asset_id, version, to_state, expected_sha256)
        kind, manifest, subject = asset["kind"], asset["manifest"], _subject(asset)
        requirements = contracts.gate_requirements(kind, to_state)
        report = gate.evaluate(tx, kind=kind, subject=subject, to_state=to_state, manifest=manifest)
        gate.raise_unmet(report)

        gated = bool(report["checks"] or report["review"] or report["approval"])
        extra: dict[str, Any] = {}
        if to_state == ACTIVE:
            extra["promoted_at"] = clock.now_iso()
        made = store.transition(
            tx, machine=MACHINE, subject_id=asset_id, version=version, to_state=to_state, actor=ctx.actor,
            reason=reason or (
                f"the gate for {to_state} is met for sha256 {asset['sha256'][:16]}…" if gated
                else f"moved to {to_state}"
            ),
            extra_updates=extra or None, evidence_level="EXECUTION_OBSERVED",
            payload={"version": version, "kind": kind, "sha256": asset["sha256"], "gate": _compact(report)},
        )
        if to_state == ACTIVE:
            replaced = _superseded(tx, asset)
            store.enqueue_outbox(
                tx, topic=ACTIVE_TOPIC, idempotency_key=active_trigger_key(asset_id, version),
                payload=active_trigger(
                    asset, made.event_id,
                    version_ref(replaced["asset_id"], replaced["version"]) if replaced is not None else None),
            )
        invalidated = None
        if to_state == QUARANTINED:
            invalidated = {
                "check_runs": checks.invalidate(tx, subject),
                "reviews": reviews.invalidate(tx, subject),
                "approvals": approvals.invalidate(
                    tx, subject, actor=ctx.actor,
                    reason=f"{asset_id}@{version} returned to quarantine; nothing recorded for its sha256 carries over",
                ),
            }
            store.append_event(
                tx, kind="asset.invalidated", actor=ctx.actor, subject_kind=subjects.ASSET,
                subject_id=asset_id, reason=machines.rule(MACHINE, "requarantine"),
                evidence_level="EXECUTION_OBSERVED",
                payload={"version": version, "sha256": asset["sha256"], **invalidated},
            )

        moved = _view(_row(tx, asset_id, version))
        effects.notify(ctx, moved, asset["status"], to_state)
        effect_results = effects.run(ctx, moved, requirements["effects"]) if requirements["effects"] else {}
        superseded = _retire_superseded(ctx, tx, moved) if to_state == ACTIVE else None
        if effect_results or superseded:
            store.append_event(
                tx, kind="asset.effects", actor=ctx.actor, subject_kind=subjects.ASSET, subject_id=asset_id,
                new_state=to_state, evidence_level="EXECUTION_OBSERVED",
                payload={"version": version, "sha256": asset["sha256"], "effects": effect_results,
                         "superseded": superseded},
            )

    out: dict[str, Any] = {
        "asset_ref": asset_ref(moved), "kind": kind, "status": to_state,
        "previous_status": asset["status"], "gate": report, "effects": effect_results,
    }
    if invalidated is not None:
        out["invalidated"] = invalidated
    if superseded:
        out["superseded"] = superseded
    return out


def promote(
    ctx: Ctx, asset_id: str, version: str, to_state: str, expected_sha256: str, *, reason: str | None = None,
) -> dict[str, Any]:
    """Move one asset version to ``to_state`` if, and only if, its gate is met for these bytes.

    Two steps. :func:`prepare_promotion` establishes and commits what the backend can
    establish now — the bytes re-hashed, the listed checks that were not passing run again,
    the approval requested. :func:`commit_promotion` then reads the gate from rows and
    moves the asset in one transaction with its effects.

    Returns ``{"asset_ref", "kind", "status", "previous_status", "gate", "effects"}``. Raises
    ``CONTENT_HASH_MISMATCH`` when ``expected_sha256`` is not the registered content,
    ``ILLEGAL_TRANSITION`` for a move the machine does not allow, ``GATE_NOT_MET`` (or
    ``INDEPENDENCE_REQUIRED``) with ``details["missing"]``, and ``IMPLEMENTATION_GAP`` when
    the new state has an effect nobody publishes.

    Call it outside any transaction you would roll back on a refusal: the check runs made
    on the way are part of the record and should outlive a refused promotion.
    """
    prepare_promotion(ctx, asset_id, version, to_state, expected_sha256)
    return commit_promotion(ctx, asset_id, version, to_state, expected_sha256, reason=reason)


#: How far along the way to ACTIVE a state is. DRAFT and QUARANTINED are the two starts.
_RANK = {DRAFT: 0, QUARANTINED: 0, REVIEWED: 1, VALIDATED: 2, APPROVED: 3, ACTIVE: 4}


def advance(ctx: Ctx, asset_id: str, version: str, *, until: str = ACTIVE) -> dict[str, Any]:
    """Move an asset as far along its lifecycle as its gates allow, and say where it stopped.

    Each step is :func:`promote`, so nothing is entered that the gate table does not allow
    for these bytes: this only saves a caller from asking four times. It is what the backend
    calls when something that a gate was waiting for has just been recorded — a review, the
    last answer set, an adjudication. Returns ``{"asset_ref", "status", "entered": [...],
    "stopped": None | {"to_state", "code", "message", "missing"}}``; a refusal is reported,
    not raised, because a gate that is not met yet is the ordinary state of an asset.
    """
    entered: list[str] = []
    stopped: dict[str, Any] | None = None
    if until not in _RANK:
        raise SchemaInvalid(f"{until!r} is not a state an asset is advanced to", details={"until": until})
    while True:
        asset = get(ctx.conn, asset_id, version)
        # An asset already at or beyond ``until`` is left where it is: "advance to REVIEWED"
        # said about an ACTIVE asset must not be read as "keep going".
        if _RANK.get(asset["status"], len(_RANK)) >= _RANK[until]:
            break
        target = next_state(asset["status"])
        if target is None:
            break
        try:
            promote(ctx, asset_id, version, target, asset["sha256"])
        except GxError as exc:
            stopped = {"to_state": target, "code": exc.code, "message": exc.message,
                       "missing": exc.details.get("missing")}
            break
        entered.append(target)
    asset = get(ctx.conn, asset_id, version)
    return {"asset_ref": asset_ref(asset), "status": asset["status"], "entered": entered, "stopped": stopped}


def _superseded(conn: sqlite3.Connection, asset: Mapping[str, Any]) -> sqlite3.Row | None:
    """The registered version that ``asset`` takes the place of, as its manifest's ``supersedes``
    names it: another version, of the same kind, and with the bytes the reference pins when it
    pins any. ``None`` when the manifest names none, or names something that is not that — a
    reference to a version nobody registered, to another kind, to other bytes, or to itself."""
    ref = asset["manifest"].get("supersedes")
    if not isinstance(ref, Mapping) or not ref.get("version"):
        return None
    old_id, old_version = ref.get("asset_id") or asset["asset_id"], str(ref["version"])
    if (old_id, old_version) == (asset["asset_id"], asset["version"]):
        return None
    old = conn.execute("SELECT * FROM assets WHERE asset_id=? AND version=?", (old_id, old_version)).fetchone()
    if old is None or old["kind"] != asset["kind"]:
        return None
    if ref.get("sha256") and ref["sha256"] != old["content_sha256"]:
        return None
    return old


def _retire_superseded(ctx: Ctx, tx: sqlite3.Connection, asset: Mapping[str, Any]) -> dict[str, Any] | None:
    """An ACTIVE version that the newly ACTIVE one names in ``supersedes`` is DEPRECATED."""
    old = _superseded(tx, asset)
    if old is None or old["status"] != ACTIVE:
        return None
    old_id = old["asset_id"]
    store.transition(
        tx, machine=MACHINE, subject_id=old_id, version=old["version"], to_state=DEPRECATED, actor=ctx.actor,
        reason=f"superseded by {asset['asset_id']}@{asset['version']}",
        extra_updates={"superseded_by": asset["version"]}, evidence_level="EXECUTION_OBSERVED",
        payload={"version": old["version"], "sha256": old["content_sha256"], "superseded_by": asset_ref(asset)},
    )
    retired = _view(_row(tx, old_id, old["version"]))
    effects.notify(ctx, retired, ACTIVE, DEPRECATED)
    return asset_ref(retired)


# ------------------------------------------------------------------------------ status

def status(conn: sqlite3.Connection, asset_id: str, version: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """What is on record for an asset's current sha256 and what its next transition still lacks."""
    asset = _view(_row(conn, asset_id, version, kind))
    subject = _subject(asset)
    latest = checks.latest_results(conn, subject)
    upcoming = next_state(asset["status"])
    missing: dict[str, Any] = {}
    if upcoming is not None:
        report = gate.evaluate(conn, kind=asset["kind"], subject=subject, to_state=upcoming, manifest=asset["manifest"])
        missing = dict(report["missing"])
        unpublished = effects.missing(report["effects"], asset["kind"])
        if unpublished:
            missing["effects"] = unpublished
    approval_rows = conn.execute(
        "SELECT approval_id, status, action_kind FROM approvals WHERE subject_kind=? AND subject_id=?"
        " AND subject_sha256=? ORDER BY rowid",
        (subjects.ASSET, asset_id, asset["sha256"]),
    ).fetchall()
    return {
        "asset_ref": asset_ref(asset),
        "kind": asset["kind"],
        "status": asset["status"],
        "title": asset["title"],
        "origin": asset["origin"],
        "checks": [
            {"check_id": c["check_id"], "result": c["result"], "check_run_id": c["check_run_id"],
             "at": c["at"], "met": c["met"]}
            for c in sorted(latest.values(), key=lambda entry: entry["check_id"])
        ],
        "reviews": [
            {"review_id": r["review_id"], "verdict": r["verdict"],
             "independence_level": r["independence_level"], "at": r["at"]}
            for r in reviews.reviews_for(conn, subject)
        ],
        "approvals": [dict(row) for row in approval_rows],
        "missing": missing,
        "next_state": upcoming,
    }


# -------------------------------------------------------------- the activation approval

def _earlier_states(kind: str, action_kind: str) -> list[str]:
    """The gated states an asset passes before the one that needs this approval."""
    table = contracts.gates()["requirements"][kind]
    earlier: list[str] = []
    for state, entry in table.items():
        if (entry.get("approval") or {}).get("action_kind") == action_kind:
            return earlier
        earlier.append(state)
    return earlier


@approvals.condition("asset_activation")
def _activation_after_gate(ctx: Ctx, *, subject: Subject, request: Mapping[str, Any], rule: Mapping[str, Any]) -> approvals.ConditionResult:
    """Rule ``asset-activation-after-gate``: every check and the review the earlier
    transitions require are on record for exactly this sha256, and the asset has got that far."""
    if subject.kind != subjects.ASSET:
        return approvals.ConditionResult(False, reasons=["an activation approval is about an asset"])
    try:
        info = subjects.resolve(ctx.conn, subject)
    except ArtifactNotFound:
        return approvals.ConditionResult(False, reasons=["the store holds no such asset"])
    if info["current_sha256"] != subject.sha256:
        return approvals.ConditionResult(False, reasons=["the approval is not about the bytes the store holds"])
    row = info["row"]
    kind = row["kind"]
    manifest = store.loads(row, "manifest_json", {}) or {}
    bound = Subject(kind=subjects.ASSET, id=row["asset_id"], sha256=row["content_sha256"], version=row["version"])
    earlier = _earlier_states(kind, "asset_activation")
    facts: dict[str, Any] = {"asset_status": row["status"], "sha256": subject.sha256, "gates_on_record": {}}
    reasons: list[str] = []
    # "On record" is about the bytes as they are at the moment of the grant, so they are hashed
    # again here: an approval is not given to content that changed since it was checked.
    if BYTE_BINDING in {c for state in earlier for c in gate.required_checks(kind, state, manifest)}:
        rehash = checks.run_checks(ctx, bound, [BYTE_BINDING])[0]
        facts["byte_binding_at_grant"] = {"result": rehash["result"], "check_run_id": rehash["check_run_id"]}
    if not earlier or row["status"] != earlier[-1]:
        reasons.append(
            f"the asset is {row['status']}; activation is approved from {earlier[-1] if earlier else '?'}"
        )
    for state in earlier:
        report = gate.evaluate(ctx.conn, kind=kind, subject=bound, to_state=state, manifest=manifest)
        facts["gates_on_record"][state] = _compact(report)
        if not report["met"]:
            lacking = [c["check_id"] for c in report["missing"]["checks"]]
            if report["missing"]["review"] is not None:
                lacking.append(f"review ({report['missing']['review']['state']})")
            reasons.append(f"the gate for {state} is not on record for this sha256: {', '.join(lacking)}")
    return approvals.ConditionResult(not reasons, facts=facts, reasons=reasons)
