"""What a check run, a review or an approval is about.

A :class:`gx_core.registry.Subject` is one thing at one content sha256. Two conventions keep
every stream's rows joinable:

* Anything registered in the ``assets`` table — a decision skill, a workflow, a tool adapter
  (capability), a benchmark, a strategy, a role intent — is ``Subject(kind="asset",
  id=asset_id, version=version, sha256=content_sha256)``. The kind of governed object it is
  (``DECISION_SKILL`` …) is read from the row, never from the caller.
* An evaluation specification is ``Subject(kind="eval_spec", id=eval_spec_id,
  version=version, sha256=frozen_sha)``; it is judged with the ``EVALUATOR`` requirements
  of ``contracts/gates.json``.

Other subject kinds (an objective, a job, a submission) may carry approvals; only the two
above carry check runs and reviews.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .. import contracts
from ..errors import ArtifactNotFound, ContentHashMismatch, SchemaInvalid
from ..registry import Subject

ASSET = "asset"
EVAL_SPEC = "eval_spec"
SUBMISSION = "submission"

#: The governed kind an evaluation specification is judged as.
EVALUATOR = contracts.require_label("asset_kind", "EVALUATOR")


def as_dict(subject: Subject) -> dict[str, Any]:
    return {"kind": subject.kind, "id": subject.id, "version": subject.version, "sha256": subject.sha256}


def from_ref(ref: Mapping[str, Any]) -> Subject:
    """A subject from a ``SubjectRef`` of ``contracts/bundles.json`` or an ``AssetRef``."""
    if not isinstance(ref, Mapping):
        raise SchemaInvalid("a subject reference is an object with kind, id, version and sha256")
    kind = ref.get("kind") or (ASSET if "asset_id" in ref else None)
    identifier = ref.get("id") or ref.get("asset_id")
    sha256 = ref.get("sha256")
    if not (isinstance(kind, str) and kind and isinstance(identifier, str) and identifier
            and isinstance(sha256, str) and sha256):
        raise SchemaInvalid(
            "a subject reference names its kind, its id and its content sha256",
            details={"given": {k: ref.get(k) for k in ("kind", "id", "asset_id", "version", "sha256")}},
        )
    version = ref.get("version")
    return Subject(kind=kind, id=identifier, sha256=sha256, version=str(version) if version is not None else None)


def for_asset(row: Mapping[str, Any]) -> Subject:
    return Subject(kind=ASSET, id=row["asset_id"], sha256=row["content_sha256"], version=row["version"])


def _asset_row(conn: sqlite3.Connection, subject: Subject) -> sqlite3.Row | None:
    if subject.version is not None:
        return conn.execute(
            "SELECT * FROM assets WHERE asset_id=? AND version=?", (subject.id, subject.version)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM assets WHERE asset_id=? AND content_sha256=? ORDER BY created_at DESC",
        (subject.id, subject.sha256),
    ).fetchone()


def _eval_spec_row(conn: sqlite3.Connection, subject: Subject) -> sqlite3.Row | None:
    if subject.version is not None:
        return conn.execute(
            "SELECT * FROM eval_specs WHERE eval_spec_id=? AND version=?", (subject.id, subject.version)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM eval_specs WHERE eval_spec_id=? AND frozen_sha=? ORDER BY created_at DESC",
        (subject.id, subject.sha256),
    ).fetchone()


def resolve(conn: sqlite3.Connection, subject: Subject) -> dict[str, Any]:
    """What the store holds for a governed subject: its row, governed kind and current sha256.

    Raises ``ARTIFACT_NOT_FOUND`` when there is no such row and ``SCHEMA_INVALID`` when the
    subject kind is not one that takes checks and reviews.
    """
    if subject.kind == SUBMISSION:
        row=conn.execute("SELECT s.*,t.run_id FROM submissions s JOIN tasks t ON t.task_id=s.task_id"
                         " WHERE s.submission_id=? AND s.result='ACCEPTED'",(subject.id,)).fetchone()
        if row is None or not row["content_sha256"]:
            raise ArtifactNotFound("a submission review names an accepted canonical submission",
                                   details={"subject":as_dict(subject)})
        version=str(row["attempt"])
        if subject.version is not None and subject.version!=version:
            raise ContentHashMismatch("the submission's attempt differs from the review subject",
                                      details={"subject":as_dict(subject),"current_version":version})
        from ..artifacts.service import ArtifactService
        from ..persistence import store
        artifacts=ArtifactService(conn)
        references=store.loads(row,"artifact_refs_json",[]) or []
        if not references:
            raise SchemaInvalid("an accepted submission with no artifact has no content review product")
        for ref in references:
            info=artifacts.describe(ref)
            if info["run_id"]!=row["run_id"]:
                raise ArtifactNotFound("the accepted submission cites an artifact outside its run")
            artifacts.read_bytes(ref)  # re-hash the registered bytes, never a caller's path
        lease=conn.execute("SELECT principal FROM leases WHERE lease_id=? AND task_id=?",
                           (row["lease_id"],row["task_id"])).fetchone()
        run=conn.execute("SELECT project_ref FROM runs WHERE run_id=?",(row["run_id"],)).fetchone()
        return {"row":row,"governed_kind":None,"review_scope":"submission",
                "current_sha256":row["content_sha256"],"version":version,
                "author_task_id":row["task_id"],"author_lease_id":row["lease_id"],
                "author_principal":lease["principal"] if lease else None,"origin":"accepted_submission",
                "run_id":row["run_id"],"project_ref":run["project_ref"] if run else None,
                "artifact_refs":references}
    if subject.kind == ASSET:
        row = _asset_row(conn, subject)
        if row is None:
            raise ArtifactNotFound(
                f"no asset {subject.id!r} at version {subject.version!r}",
                details={"subject": as_dict(subject)},
            )
        return {
            "row": row, "governed_kind": row["kind"], "current_sha256": row["content_sha256"],
            "version": row["version"], "author_task_id": row["author_task_id"],
            "author_lease_id": row["author_lease_id"], "author_principal": row["author_principal"],
            "origin": row["origin"],
        }
    if subject.kind == EVAL_SPEC:
        row = _eval_spec_row(conn, subject)
        if row is None:
            raise ArtifactNotFound(
                f"no evaluation specification {subject.id!r} at version {subject.version!r}",
                details={"subject": as_dict(subject)},
            )
        return {
            "row": row, "governed_kind": EVALUATOR, "current_sha256": row["frozen_sha"],
            "version": row["version"], "author_task_id": row["author_task_id"],
            "author_lease_id": None, "author_principal": row["author_principal"], "origin": None,
        }
    raise SchemaInvalid(
        f"a subject of kind {subject.kind!r} takes no checks or reviews; governed subjects are "
        f"{ASSET!r} and {EVAL_SPEC!r}",
        details={"subject": as_dict(subject)},
    )
