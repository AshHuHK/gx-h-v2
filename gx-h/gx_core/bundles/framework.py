"""Judging a bundle: the contract's shape first, then the published validator.

A worker returns one bundle for one task. :func:`judge` is the single path by which a
bundle is judged, whether it is being checked before submission (``commit=False``,
nothing is written) or submitted (``commit=True``, inside the submission's transaction).
Both go through the same two steps, so what ``gx_bundle_check`` reports is what
``gx_task_submit`` will decide.

1. **Shape**, from ``contracts/bundles.json`` (:mod:`gx_core.bundles.schema`). A record
   that violates it is refused on its own; a bundle whose own shape is wrong is refused
   whole.
2. **Meaning**, by the validator its owning service published through
   ``gx_core.registry``. It sees only records that passed the first step and judges
   them against bytes and rows the backend holds.

A kind nobody has published a validator for is not accepted and not rejected on its
merits: its verdict carries ``IMPLEMENTATION_GAP`` and says which provider is missing.
v1 had exactly one acceptance rule for everything -- the name of the artifact's kind --
and applied it only where a test supplied the function that applied it.

For validator authors
---------------------
``fn(ctx, task, bundle, *, commit) -> BundleVerdict``.

* ``bundle`` is the cleaned bundle: refused elements are already gone from its lists and
  optional fields given as ``null`` are absent. Indices you return are positions in the
  lists you were given; :func:`judge` maps them back to the positions the worker sent.
* ``task`` is the task row as a mapping (``task_id``, ``run_id``, ``kind``, ``role``,
  ``attempt``, ``lease_id``, ``owner``, ``input``, ``acceptance``, ``subject_*``,
  ``independent_of_task_id`` ...). With no task it holds only the two entries below.
* ``task["refused_on_shape"]`` is how many records the first step refused before you
  were called, and ``task["refused_on_shape_by_field"]`` the same per list. You see
  only what was left, so a rule about the bundle as a whole -- "every record", "at
  least N of what was handed in" -- reads these, or a bundle of one good record and
  nine malformed ones is judged as a bundle of one.
* A verdict about a list other than :func:`gx_core.bundles.schema.primary_list` names it
  in ``RecordVerdict.details["field"]``.
* ``accepted`` is your reading of the task's acceptance rule over the records you saw.
* With ``commit=False`` write nothing. With ``commit=True`` you are inside the
  submission's transaction: persist the accepted records, and do not begin, commit or
  roll back a transaction of your own (``store.transaction`` nests and is fine).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .. import contracts, registry
from ..context import Ctx
from ..errors import SchemaInvalid
from ..registry import BundleVerdict, RecordVerdict
from . import schema

#: How many violations of one record are quoted in its verdict.
_MAX_VIOLATIONS_PER_RECORD = 8


def _validator_gap(kind: str) -> str:
    """Why no validator is bound for ``kind``, in terms the reader can act on."""
    providers = registry.load_providers()
    failed = {module: reason for module, reason in providers.items() if reason is not None}
    if failed:
        listed = "; ".join(f"{module}: {reason}" for module, reason in sorted(failed.items()))
        return f"no validator is published for {kind}. Providers that did not load: {listed}"
    return f"no validator is published for {kind} by any provider that loaded"


def _schema_refusals(report: schema.SchemaReport) -> list[RecordVerdict]:
    refusals: list[RecordVerdict] = []
    for field_name, items in report.refused.items():
        for index in sorted(items):
            violations = items[index]
            refusals.append(RecordVerdict(
                index=index, ok=False, code="SCHEMA_INVALID",
                message=violations[0].message + (
                    f" (and {len(violations) - 1} more)" if len(violations) > 1 else ""
                ),
                details={"field": field_name, "stage": "schema",
                         "violations": [v.as_dict() for v in violations[:_MAX_VIOLATIONS_PER_RECORD]]},
            ))
    return refusals


def judge(ctx: Ctx, task: Mapping[str, Any] | None, kind: str, bundle: Any, *, commit: bool) -> BundleVerdict:
    """Judge one bundle record by record. See the module docstring for the two steps."""
    if kind not in contracts.bundle_kinds():
        raise SchemaInvalid(
            f"{kind!r} is not a bundle kind of the contract",
            details={"bundle_kind": kind, "known": contracts.bundle_kinds()},
        )
    task = dict(task or {})
    spec = contracts.bundle_spec(kind)

    # A bundle answers the task it was produced for. A task that asked for one kind is
    # not satisfied by a well-formed bundle of another.
    task_kind = task.get("kind")
    if task_kind and task_kind not in spec["for_task_kinds"]:
        return BundleVerdict(
            bundle_kind=kind, accepted=False,
            records=[RecordVerdict(
                index=-1, ok=False, code="SCHEMA_INVALID",
                message=f"a {kind} does not answer a {task_kind} task; it answers "
                        f"{', '.join(spec['for_task_kinds'])}",
                details={"stage": "task", "task_kind": task_kind,
                         "for_task_kinds": list(spec["for_task_kinds"])},
            )],
            summary="the bundle kind does not match the task",
        )
    expected_kind = task.get("bundle_kind")
    if expected_kind and expected_kind != kind:
        return BundleVerdict(
            bundle_kind=kind, accepted=False,
            records=[RecordVerdict(
                index=-1, ok=False, code="SCHEMA_INVALID",
                message=f"task {task.get('task_id')} is answered by a {expected_kind}, not a {kind}",
                details={"stage": "task", "expected_bundle_kind": expected_kind},
            )],
            summary="the bundle kind does not match the task",
        )

    report = schema.check_bundle(kind, bundle)
    refusals = _schema_refusals(report)
    if not report.bundle_ok:
        whole = RecordVerdict(
            index=-1, ok=False, code="SCHEMA_INVALID",
            message=report.bundle_violations[0].message + (
                f" (and {len(report.bundle_violations) - 1} more)"
                if len(report.bundle_violations) > 1 else ""
            ),
            details={"stage": "schema",
                     "violations": [v.as_dict() for v in report.bundle_violations[:20]]},
        )
        return BundleVerdict(
            bundle_kind=kind, accepted=False, records=[whole] + refusals,
            summary=f"the bundle's own shape is wrong ({len(report.bundle_violations)} "
                    "violation(s)); no record was judged",
        )

    validator = registry.validator_for(kind)
    if validator is None:
        gap = RecordVerdict(
            index=-1, ok=False, code="IMPLEMENTATION_GAP", message=_validator_gap(kind),
            details={"stage": "validator", "bundle_kind": kind},
        )
        return BundleVerdict(
            bundle_kind=kind, accepted=False, records=[gap] + refusals,
            summary=f"shape checked ({report.refused_count} record(s) refused); the records "
                    "were not judged because no validator is installed for this kind",
        )

    # Never fewer than the shape check just found, whatever the caller passed. A caller that
    # removed records itself before asking for a judgement adds its own count by passing it.
    try:
        already = max(0, int(task.get("refused_on_shape") or 0))
    except (TypeError, ValueError):
        already = 0
    task["refused_on_shape"] = max(already, report.refused_count)
    task["refused_on_shape_by_field"] = {name: len(items) for name, items in report.refused.items()
                                         if items}
    verdict = validator(ctx, task, report.clean, commit=commit)
    if not isinstance(verdict, BundleVerdict):
        raise TypeError(
            f"the validator of {kind} returned {type(verdict).__name__}, not a BundleVerdict"
        )
    if verdict.bundle_kind != kind:
        raise ValueError(
            f"the validator of {kind} returned a verdict for {verdict.bundle_kind!r}"
        )

    primary = schema.primary_list(kind)
    for record in verdict.records:
        if record.index < 0:
            continue
        field_name = record.details.get("field", primary)
        original = report.original_index(field_name, record.index) if field_name else None
        if original is not None:
            record.index = original
            record.details.setdefault("field", field_name)

    summary = verdict.summary
    if refusals:
        summary = (summary + " " if summary else "") + (
            f"{len(refusals)} record(s) were refused on shape before the validator saw them."
        )
    return BundleVerdict(
        bundle_kind=kind, accepted=verdict.accepted,
        records=refusals + list(verdict.records), created=dict(verdict.created),
        summary=summary,
    )


# --------------------------------------------------------------------------- rendering

def record_view(record: RecordVerdict) -> dict[str, Any]:
    """One record in the shape ``gx_bundle_check`` declares: index, ok, code, message, detail."""
    view: dict[str, Any] = {
        "index": record.index, "ok": record.ok, "code": record.code,
        "message": record.message, "detail": dict(record.details),
    }
    if record.ref is not None:
        view["ref"] = record.ref
    return view


def full_view(verdict: BundleVerdict) -> dict[str, Any]:
    """Every record, for the artifact a large verdict is stored as."""
    return {
        "bundle_kind": verdict.bundle_kind,
        "would_accept": verdict.accepted,
        "accepted_count": verdict.accepted_count,
        "rejected_count": verdict.rejected_count,
        "summary": verdict.summary,
        "created": verdict.created,
        "records": [record_view(record) for record in verdict.records],
    }


def bounded_view(verdict: BundleVerdict, *, max_bytes: int) -> tuple[dict[str, Any], bool]:
    """``(view, complete)``: the verdict cut to fit a response, refusals first.

    A worker reads a verdict to find out what to correct, so refused records are never
    the ones dropped while an accepted one is shown. When records had to be left out
    ``complete`` is False and the caller attaches the full verdict as an artifact; the
    counts are always the true ones.
    """
    view: dict[str, Any] = {
        "bundle_kind": verdict.bundle_kind,
        "would_accept": verdict.accepted,
        "accepted_count": verdict.accepted_count,
        "rejected_count": verdict.rejected_count,
        "summary": verdict.summary,
        "records": [],
        "records_shown": 0,
        "records_total": len(verdict.records),
    }
    ordered = [r for r in verdict.records if not r.ok] + [r for r in verdict.records if r.ok]
    used = len(json.dumps(view, ensure_ascii=False))
    complete = True
    for record in ordered:
        item = record_view(record)
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
        if used + size > max_bytes:
            complete = False
            break
        view["records"].append(item)
        used += size
    view["records"].sort(key=lambda item: (item["ok"], item["index"]))
    view["records_shown"] = len(view["records"])
    return view, complete
