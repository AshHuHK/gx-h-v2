"""What the capability registry publishes: two tools, a validator, eight checks, two hooks.

    gx_capability_inspect        what is registered, on what evidence
    gx_capability_register       an environment from a probe the backend runs, or a
                                 capability candidate from a manifest bundle (operator)
    capability_manifest_bundle   the same intake, for a bundle a worker hands in
    the eight adapter checks     :mod:`.checks`
    enter_capability_catalogue   what ACTIVE means here
    on_transition TOOL_ADAPTER   the mirror of the asset's status in the capabilities table

Loaded by ``gx_core.registry.load_providers``. Nothing here is called by name from
another stream; they reach it through the registry.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..context import Ctx
from ..errors import ContentHashMismatch, GxError, SchemaInvalid
from ..governance.effects import effect, on_transition, review_package
from ..persistence import store
from ..registry import (
    BriefSection,
    BundleVerdict,
    RecordVerdict,
    Subject,
    ToolResult,
    brief_section,
    bundle_validator,
    check,
    tool,
)
from . import checks, environments, ladder, registry, review, storage
from . import manifest as manifest_mod

# ------------------------------------------------------------------------------- tools

@tool("gx_capability_inspect")
def capability_inspect(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    data = registry.inspect(
        ctx.conn, capability_id=args.get("capability_id"), filters=args.get("filters"),
        include=args.get("include"), limit=args.get("limit"),
    )
    if data["truncated"]:
        ctx.warn(f"more capabilities are registered than the {data['limit']} listed; narrow the "
                 "filters or name a capability_id")
    return ToolResult(data=data)


@tool("gx_capability_register")
def capability_register(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    """Register an environment, a capability candidate, or both, and climb the ladder.

    The registration itself is one idempotent unit in the store. Probing is observation:
    with ``probe`` (the default) the environment is probed before it is written, and the
    candidate's probe ladder is climbed as far as it may go now. The ladder is resumable,
    so a later call with a new key and the same bundle continues it — after a review, or
    while a probe job is still running.
    """
    spec = args.get("environment")
    bundle_ref = args.get("bundle_ref")
    run_probe = args.get("probe", True)
    if spec is None and bundle_ref is None:
        raise SchemaInvalid("give an environment specification, a bundle_ref, or both",
                            details={})
    conn = ctx.conn
    key = store.tool_key(ctx.tool, args["idempotency_key"])
    request = {"environment": spec, "bundle_ref": bundle_ref, "probe": bool(run_probe)}

    bundle = None
    if bundle_ref is not None:
        bundle = storage.artifact_service(conn).read_json(bundle_ref)
    done = conn.execute(
        "SELECT completed_at FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
    observed = None
    if spec is not None and run_probe and (done is None or done["completed_at"] is None):
        observed = environments.probe(spec)  # starts processes: before any transaction

    def work(_txn: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"env_id": None, "environment_status": None, "asset_ref": None,
                               "status": None, "probes": []}
        if spec is not None:
            result = environments.register(conn, spec, actor=ctx.actor, run_probe=False,
                                           observed=observed)
            out.update(env_id=result["env_id"], environment_status=result["environment_status"],
                       probes=[{**row, "subject": result["env_id"]} for row in result["probes"]])
        if bundle is not None:
            registered = registry.register_candidate(ctx, bundle, origin="AUTHORED")
            out.update(asset_ref=registered["asset_ref"], status=registered["status"],
                       capability_version=registered["capability_version"])
        return out

    response, replayed = store.run_idempotent(conn, key, ctx.tool, request, work)
    response = dict(response)
    if replayed:
        ctx.warn("this idempotency key was already used for the same registration; the stored "
                 "answer is returned and nothing was registered again")
    capability_version = response.pop("capability_version", None)
    if capability_version and run_probe:
        climbed = ladder.advance(ctx, capability_version)
        response["status"] = climbed["status"]
        if climbed.get("reused_controls"):
            response["reused_controls"] = climbed["reused_controls"]
        response["probes"] = list(response.get("probes") or []) + [
            {**probe, "subject": capability_version} for probe in climbed["probes"]]
        if climbed["stopped"]:
            ctx.warn(f"the probe ladder of {capability_version} stopped: {climbed['stopped']}")
    return ToolResult(data=response)


# --------------------------------------------------------------------------- validator

@bundle_validator(manifest_mod.BUNDLE_KIND)
def capability_manifest_bundle(ctx: Ctx, task: Mapping[str, Any], bundle: Mapping[str, Any], *,
                               commit: bool) -> BundleVerdict:
    """A worker's capability bundle becomes a candidate, or is refused with every reason.

    What is accepted is a candidate and nothing more: it is not callable, none of its code
    has run, and its gates are still ahead of it. Imported content lands QUARANTINED.
    """
    kind = manifest_mod.BUNDLE_KIND
    origin = "IMPORTED" if task.get("kind") == "import_asset" else "AUTHORED"
    if task.get("refused_on_shape"):
        return BundleVerdict(
            bundle_kind=kind, accepted=False,
            records=[RecordVerdict(index=-1, ok=False, code="SCHEMA_INVALID",
                                   message="part of the bundle was refused on shape; a capability is "
                                           "registered whole or not at all",
                                   details={"stage": "validator",
                                            "refused_on_shape": task.get("refused_on_shape")})],
            summary="the capability bundle was not registered: part of it was refused on shape",
        )
    try:
        result = registry.register_candidate(
            ctx, bundle, origin=origin, run_id=task.get("run_id"), task_id=task.get("task_id"),
            commit=commit,
        )
    except GxError as exc:
        problems = exc.details.get("problems") if isinstance(exc.details, dict) else None
        return BundleVerdict(
            bundle_kind=kind, accepted=False,
            records=[RecordVerdict(index=-1, ok=False, code=exc.code, message=exc.message,
                                   details={"stage": "validator", **(
                                       {"problems": problems[:20]} if problems else dict(exc.details))})],
            summary=f"the capability bundle was refused: {exc.code}",
        )
    ref = result["capability_version"]
    # "assets" is what gx_asset_submit reads to say which asset the bundle became.
    created = {"assets": [ref], "capabilities": [ref]} if commit else {}
    return BundleVerdict(
        bundle_kind=kind, accepted=True,
        records=[RecordVerdict(
            index=-1, ok=True, ref=ref,
            message=(f"{ref} is a candidate at sha256 {result['manifest_sha256'][:16]}… "
                     f"({result['status'] or 'not yet written'}); it becomes callable only at ACTIVE"),
            details={"manifest_sha256": result["manifest_sha256"], "asset_ref": result["asset_ref"],
                     "status": result["status"], "already_registered": not result["created"]},
        )],
        created=created,
        summary=f"{ref} accepted as a candidate capability",
    )


# ------------------------------------------------------------------------------ checks

for _check_id, _fn in checks.ALL.items():
    check(_check_id)(_fn)


# ------------------------------------------------------------------------------- briefs

@brief_section("capability_under_review", kinds=("review_asset",), order=40)
def capability_under_review(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    """For a review task whose subject is a capability: what would run, and what to decide.

    Applies only when the subject is a TOOL_ADAPTER asset at the sha256 the task names, and
    is built from the registered content and the backend's own records of it.
    """
    if task.get("subject_kind") != "asset" or not task.get("subject_id"):
        return None
    row = ctx.conn.execute(
        "SELECT asset_id, version, content_artifact_ref FROM assets WHERE asset_id=? AND content_sha256=?"
        " AND kind=?", (task["subject_id"], task.get("subject_sha256"), registry.ASSET_KIND)).fetchone()
    if row is None:
        return None
    task_input = task.get("input") or {}
    package_ref = task_input.get("review_package_ref")
    questions = None
    if package_ref or "review_items" in task_input:
        from ..governance.tools import review_items
        questions = [(item["item_id"], item["question"]) for item in review_items(task, ctx.conn)
                     if item.get("kind") == "question"]
    try:
        found = review.material(ctx.conn, f"{row['asset_id']}@{row['version']}", questions=questions)
    except GxError as exc:
        return BriefSection(title="The capability under review",
                            body=f"The capability's content could not be read: {exc.code}: {exc.message}",
                            refs=[row["content_artifact_ref"]])
    refs = [row["content_artifact_ref"]] + [ref["artifact_ref"] for ref in found["scripts"].values()]
    if package_ref:
        refs.append(package_ref)
    return BriefSection(title="The capability under review", body=review.render(found), refs=refs)


# ------------------------------------------------------------------------------- hooks

@review_package(registry.ASSET_KIND)
def capability_review_package(conn: Any, asset: Mapping[str, Any]) -> dict[str, Any]:
    """What a reviewer of a capability gives a verdict on: each question of
    ``review.QUESTIONS`` that applies to the capability's kind (``q<n>``), and every scan
    finding nobody has adjudicated (governance's default items). A question shown in the brief
    and never answered is not a review, so each is an item with a verdict of its own."""
    from ..governance import reviews

    package = reviews.default_package(conn, asset)
    try:
        manifest = registry.candidate(conn, f"{asset['asset_id']}@{asset['version']}")["manifest"]
    except GxError:
        manifest = None
    kind = manifest["kind"] if manifest is not None else None
    questions = [{"item_id": item_id, "kind": "question", "question": text}
                 for item_id, text in review.questions_for(kind)]
    known = registry.known_answer_view(manifest, asset.get("status")) if manifest is not None else None
    not_exercised = ("" if known is None or known["declared"] else
                     f"{known['not_exercised']}. For VALIDATED, this version must pass the L3 smoke job "
                     "and the malformed-input check. ")
    return {**package, "items": questions + list(package["items"]),
            **({"known_answer": known} if known is not None else {}),
            "instructions": (
                "Each question q<n> is answered from the capability's bytes and the brief's section "
                "'The capability under review': SUPPORTED when the answer is what a capability that is "
                "safe to run would answer, and REQUEST_CHANGES or REJECT with the reason in notes when it "
                "is not. " + not_exercised + package["instructions"])}


@on_transition(registry.ASSET_KIND)
def mirror_status(ctx: Ctx, asset: Mapping[str, Any], prev_state: str, new_state: str) -> None:
    """Keep ``capabilities.status`` equal to the asset's. A mirror, never read as the truth."""
    ctx.conn.execute(
        "UPDATE capabilities SET status=? WHERE capability_id=? AND version=?",
        (new_state, asset["asset_id"], asset["version"]),
    )


@effect("enter_capability_catalogue", kinds=(registry.ASSET_KIND,))
def enter_catalogue(ctx: Ctx, asset: Mapping[str, Any]) -> dict[str, Any]:
    """What ACTIVE means on this side: the version is one ``resolve()`` will hand out.

    Refuses — and with it the promotion — when the registry holds no such capability, holds
    it at another sha256, or its environment is not ready on this host: an ACTIVE capability
    that cannot be resolved or launched is a catalogue entry for nothing.
    """
    capability_version = f"{asset['asset_id']}@{asset['version']}"
    row = ctx.conn.execute(
        "SELECT manifest_sha256, env_id, probe_rung, test_status FROM capabilities"
        " WHERE capability_id=? AND version=?", (asset["asset_id"], asset["version"])).fetchone()
    if row is None:
        raise ContentHashMismatch(
            f"{capability_version} is an asset and not a registered capability; a capability "
            "enters the catalogue through the capability registry",
            details={"capability_version": capability_version},
        )
    if row["manifest_sha256"] != asset["sha256"]:
        raise ContentHashMismatch(
            f"{capability_version} is registered at {row['manifest_sha256'][:16]}… and the asset "
            f"being activated is {str(asset['sha256'])[:16]}…",
            details={"manifest_sha256": row["manifest_sha256"], "asset_sha256": asset["sha256"]},
        )
    environments.require_ready(ctx.conn, row["env_id"], actor=ctx.actor, reprobe_when_stale=False)
    return {"capability_version": capability_version, "manifest_sha256": row["manifest_sha256"],
            "env_id": row["env_id"], "probe_rung": row["probe_rung"],
            "test_status": row["test_status"]}
