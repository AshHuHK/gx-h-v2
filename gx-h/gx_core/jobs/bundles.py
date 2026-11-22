"""The ``execution_bundle`` validator: what an executor reports, held against what ran.

An executor hands in the jobs it ran for a path and the outputs it wants carried
forward. It reports no score — the evaluation is run afterwards by the backend under the
frozen specification — and nothing it says about a job is taken from the bundle: every
job id is looked up, and status, capability, parameters and outputs are read from the
row the job service wrote.

What is refused, record by record:

* a job that is not this run's, this path's and this task's, or that has not ended as
  far as the backend has observed;
* an output that the named job did not publish under that name, or whose bytes no longer
  hash to their record;
* a job that ran something the path was not accepted with, unless a deviation says so;
* a job the backend recorded for this path that the bundle leaves out — a failed attempt
  cannot be dropped from the account.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import contracts
from ..capabilities import storage
from ..context import Ctx
from ..errors import GxError
from ..persistence import store
from ..registry import BundleVerdict, RecordVerdict
from . import service as job_service

BUNDLE_KIND = "execution_bundle"

#: Failure classes that are verdicts about the science. They need an evaluation to carry
#: them; an executor that only saw a tool fail cannot declare the method wrong.
OUTCOME_CLASSES = ("candidate_below_threshold", "method_inapplicable", "hypothesis_unsupported")

#: The class no executor may name: an executor is scored under the specification, so its word
#: that the evaluator is wrong is a suspicion, and only opens a review (SCHEDULING_DESIGN §6.1, §6.3).
EVALUATOR_BUG = "evaluator_bug"
if EVALUATOR_BUG not in contracts.failure_classes():  # pragma: no cover - the contract moved
    raise RuntimeError(f"failure class {EVALUATOR_BUG!r} left the contract")


def _refuse(index: int, field: str | None, code: str, message: str, **details: Any) -> RecordVerdict:
    extra = {"field": field} if field else {}
    return RecordVerdict(index=index, ok=False, code=code, message=message, details={**extra, **details})


def judge(ctx: Ctx, task: Mapping[str, Any], bundle: Mapping[str, Any], *, commit: bool) -> BundleVerdict:
    conn = ctx.conn
    records: list[RecordVerdict] = []
    path_id = bundle["path_id"]
    run_id = task.get("run_id")
    task_id = task.get("task_id")

    path = conn.execute("SELECT * FROM paths WHERE path_id=?", (path_id,)).fetchone()
    if path is None or (run_id is not None and path["run_id"] != run_id):
        return BundleVerdict(
            bundle_kind=BUNDLE_KIND, accepted=False,
            records=[_refuse(-1, None, "ARTIFACT_NOT_FOUND",
                             f"no path {path_id!r} in the run this task belongs to", path_id=path_id)],
            summary="the bundle names a path that is not this run's",
        )
    run_id = path["run_id"]
    if task.get("path_id") not in (None, path_id):
        return BundleVerdict(
            bundle_kind=BUNDLE_KIND, accepted=False,
            records=[_refuse(-1, None, "SCHEMA_INVALID",
                             f"task {task_id} executes path {task.get('path_id')}, not {path_id}",
                             path_id=path_id, task_path_id=task.get("path_id"))],
            summary="the bundle is about another path than its task",
        )
    spec = store.loads(path, "spec_json", {}) or {}
    steps = list(spec.get("method_sequence") or [])
    deviations = list(bundle.get("deviations") or [])

    # ---- the jobs
    job_rows: dict[str, Any] = {}
    seen: set[str] = set()
    for index, job_id in enumerate(bundle["job_ids"]):
        if job_id in seen:
            records.append(_refuse(index, "job_ids", "SCHEMA_INVALID", f"job {job_id} is listed twice"))
            continue
        seen.add(job_id)
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            records.append(_refuse(index, "job_ids", "ARTIFACT_NOT_FOUND", f"no job {job_id!r}"))
            continue
        where = [name for name, got, want in (
            ("run", row["run_id"], run_id), ("path", row["path_id"], path_id)) if want is not None and got != want]
        # A job submitted for the path without naming a task belongs to the task that executes
        # the path, as the omission rule below already says; one of another task does not (audit J7).
        if task_id is not None and row["task_id"] not in (None, task_id):
            where.append("task")
        if where:
            records.append(_refuse(
                index, "job_ids", "SCOPE_DENIED",
                f"job {job_id} does not belong to this {', '.join(where)}",
                job={"run_id": row["run_id"], "path_id": row["path_id"], "task_id": row["task_id"]}))
            continue
        if row["status"] not in job_service.TERMINAL:
            records.append(_refuse(
                index, "job_ids", "DEPENDENCY_UNMET",
                f"job {job_id} is {row['status']}; a job is reported once it has ended. Wait for it "
                "with gx_job_wait, or cancel it", status=row["status"]))
            continue
        intent = store.loads(row, "submit_intent_json", {}) or {}
        matched = [k for k, step in enumerate(steps)
                   if step.get("capability_version") == intent.get("capability_version")
                   and dict(step.get("parameters") or {}) == dict(intent.get("parameters") or {})]
        if not matched and not any(job_id in str(d.get("what", "")) for d in deviations):
            records.append(_refuse(
                index, "job_ids", "SCHEMA_INVALID",
                f"job {job_id} ran {intent.get('capability_version')} with parameters "
                f"{intent.get('parameters') or {}}, which is no step of the path as it was accepted; "
                "list it as a deviation that names the job id",
                ran={"capability_version": intent.get("capability_version"),
                     "parameters": intent.get("parameters") or {}},
                accepted_steps=steps))
            continue
        job_rows[job_id] = row
        records.append(RecordVerdict(
            index=index, ok=True, ref=job_id,
            message=f"job {job_id} ended {row['status']}",
            details={"field": "job_ids", "status": row["status"], "matched_steps": matched,
                     "capability_version": intent.get("capability_version")}))

    # ---- what the bundle leaves out
    omitted = [row for row in conn.execute(
        "SELECT job_id, status, task_id FROM jobs WHERE path_id=? ORDER BY created_at", (path_id,))
        if row["job_id"] not in seen and (task_id is None or row["task_id"] in (None, task_id))]
    for row in omitted:
        records.append(_refuse(
            -1, "job_ids", "SCHEMA_INVALID",
            f"job {row['job_id']} ({row['status']}) was recorded for this path and is not in the "
            "bundle; every attempt is reported, the failed ones included",
            omitted_job_id=row["job_id"], status=row["status"]))

    # ---- the outputs
    artifacts = storage.artifact_service(conn)
    for index, output in enumerate(bundle["outputs"]):
        row = job_rows.get(output["job_id"])
        if row is None:
            records.append(_refuse(
                index, "outputs", "SCHEMA_INVALID",
                f"output {output['output_name']!r} names job {output['job_id']}, which is not an "
                "accepted job of this bundle"))
            continue
        published = {item["output_name"]: item for item in
                     (store.loads(row, "outcome_json", {}) or {}).get("outputs") or []}
        item = published.get(output["output_name"])
        if item is None or item["artifact_ref"] != output["artifact_ref"]:
            records.append(_refuse(
                index, "outputs", "ARTIFACT_NOT_FOUND",
                f"job {output['job_id']} published no output {output['output_name']!r} as "
                f"{output['artifact_ref']}", published=sorted(published)))
            continue
        try:
            verified = artifacts.verify(output["artifact_ref"])
        except GxError as exc:
            records.append(_refuse(index, "outputs", exc.code, exc.message))
            continue
        if not verified["ok"]:
            records.append(_refuse(
                index, "outputs", "ARTIFACT_HASH_MISMATCH",
                f"{output['artifact_ref']} no longer hashes to its record ({verified['reason']})",
                recorded_sha256=verified["recorded_sha256"], observed_sha256=verified["observed_sha256"]))
            continue
        records.append(RecordVerdict(
            index=index, ok=True, ref=output["artifact_ref"],
            message=f"{output['output_name']} of job {output['job_id']}",
            details={"field": "outputs", "sha256": item["sha256"], "kind": item["kind"]}))

    # ---- deviations and the failure report
    for index, deviation in enumerate(deviations):
        if not 0 <= deviation["step"] < max(len(steps), 1) or not deviation["what"].strip() \
                or not deviation["why"].strip():
            records.append(_refuse(
                index, "deviations", "SCHEMA_INVALID",
                f"a deviation names a step of the accepted path (0..{max(len(steps) - 1, 0)}), what "
                "was done instead and why"))
    failure = bundle.get("failure")
    if failure is not None:
        problem = _failure_problem(conn, failure, path_id, job_rows)
        if problem:
            records.append(_refuse(-1, "failure", "SCHEMA_INVALID", problem))

    succeeded = {k for row in job_rows.values() if row["status"] == job_service.SUCCEEDED
                 for k, step in enumerate(steps)
                 if step.get("capability_version") ==
                 (store.loads(row, "submit_intent_json", {}) or {}).get("capability_version")
                 and dict(step.get("parameters") or {}) ==
                 dict((store.loads(row, "submit_intent_json", {}) or {}).get("parameters") or {})}
    deviated = {d["step"] for d in deviations}
    unaccounted = [k for k in range(len(steps)) if k not in succeeded and k not in deviated]
    if unaccounted and failure is None:
        records.append(_refuse(
            -1, None, "SCHEMA_INVALID",
            f"step(s) {unaccounted} of the accepted path have no job that succeeded, no deviation "
            "and no failure report; say how the path was not executed as planned",
            unaccounted_steps=unaccounted))

    accepted = all(record.ok for record in records) and bool(job_rows or failure is not None)
    summary = (
        f"{len(job_rows)} job(s) and {sum(1 for r in records if r.ok and r.details.get('field') == 'outputs')} "
        f"output(s) resolved; {sum(1 for r in records if not r.ok)} refusal(s). "
        + ("The path was executed as planned." if accepted and not deviations and failure is None
           else "Accepted with what differed on record." if accepted
           else "Not accepted.")
    )
    if commit and accepted:
        store.append_event(
            conn, kind="path.execution_reported", actor=ctx.actor, run_id=run_id,
            subject_kind="path", subject_id=path_id, evidence_level="EXECUTION_OBSERVED",
            payload={"task_id": task_id, "job_ids": sorted(job_rows),
                     "outputs": [o["artifact_ref"] for o in bundle["outputs"]],
                     "deviations": deviations, "failure": failure},
        )
    return BundleVerdict(bundle_kind=BUNDLE_KIND, records=records, accepted=accepted, summary=summary)


def _failure_problem(conn: Any, failure: Mapping[str, Any], path_id: str,
                     job_rows: Mapping[str, Any]) -> str | None:
    taxonomy = contracts.failure_classes()
    name = failure["failure_class"]
    if name not in taxonomy:
        return f"failure_class {name!r} is not a class of the failure taxonomy: {sorted(taxonomy)}"
    if name == EVALUATOR_BUG:
        return (f"{EVALUATOR_BUG} is not an executor's to name (SCHEDULING_DESIGN §6.1). It is recorded only "
                "when an independent review of the evaluation reproduces a defect, or a known-answer case "
                "fails. Report what failed to run under the class that fits it, and say in "
                "root_cause_hypothesis what makes you suspect the evaluator: that opens the review")
    if name in OUTCOME_CLASSES:
        evaluated = conn.execute("SELECT 1 FROM evaluations WHERE path_id=? LIMIT 1", (path_id,)).fetchone()
        if evaluated is None:
            return (f"{name} is a verdict about the science and no evaluation of this path exists to "
                    "carry it; an executor reports what failed to run, the evaluation says what "
                    "the result means")
    job_id = failure.get("job_id")
    if job_id is not None and job_id not in job_rows:
        return f"the failure report names job {job_id}, which is not an accepted job of this bundle"
    for ref in failure["evidence_refs"]:
        if conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone() is None:
            return f"the failure report cites {ref}, which is not an artifact in the store"
    if not failure["error_summary"].strip() or not failure["root_cause_hypothesis"].strip():
        return "a failure report says what failed and what the executor thinks the cause is"
    return None


__all__ = ["BUNDLE_KIND", "EVALUATOR_BUG", "OUTCOME_CLASSES", "judge"]
