"""What governance publishes: four tools, the checks every kind takes, and the review validator.

Tools (``contracts/mcp_tools.json``): ``gx_asset_submit``, ``gx_asset_status`` for workers;
``gx_asset_promote`` and ``gx_approval_grant`` for the operator surface.

Checks (``contracts/gates.json``): ``byte_binding``, ``manifest_schema``,
``licence_recorded`` and ``scope_scan``. Each is computed from bytes and rows the backend
holds, every time it is asked for.

Bundle validator: ``asset_review_bundle`` — the only way a worker's review becomes a row,
with the independence the backend computed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Mapping

from .. import config, contracts
from ..bundles import framework, schema
from ..context import SCOPE_OPERATOR, Ctx
from ..domain import ids
from ..errors import (
    ArtifactHashMismatch,
    ArtifactNotFound,
    ContentHashMismatch,
    GxError,
    ImplementationGap,
    ScopeDenied,
    SchemaInvalid,
)
from ..persistence import store
from ..registry import (
    BundleVerdict,
    CheckOutcome,
    RecordVerdict,
    Subject,
    ToolResult,
    bundle_validator,
    check,
    tool,
    validator_for,
)
from . import _artifacts, approvals, assets, checks, effects, gate, manifests, reviews, scan, subjects

SUPPORTED = contracts.require_label("evidence_review_verdict", "SUPPORTED")
APPROVE = contracts.require_label("review_verdict", "APPROVE")
UNVERIFIED_LICENCE = "UNVERIFIED"

#: Prefix of a review item that stands for a diagnostic rather than a trace row or a case.
DIAGNOSTIC_ITEM_PREFIX = "diag:"


# =============================================================================== tools

def _task_mapping(conn: Any, task_id: str | None) -> dict[str, Any] | None:
    """The task row in the shape a bundle validator is handed, or None when no task is named."""
    if not task_id:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no task {task_id!r}", details={"task_id": task_id})
    # Core's view of a task, the one its submission path hands to every validator. Imported
    # here because the task service loads the bundle framework, which loads the providers.
    from ..teams.service import task_mapping

    return task_mapping(conn, row)


def _replayed(conn: Any, key: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
    """The stored response of a keyed call that already completed with this same request."""
    row = conn.execute("SELECT * FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
    if row is None or row["completed_at"] is None or row["side_effect_status"] == "UNKNOWN":
        return None
    if row["request_sha"] != ids.sha256_of_obj(dict(request)):
        return None
    return store.loads(row, "response_json", None)


def _check_scope(ctx: Ctx, namespace: str | None) -> None:
    """An asset registered under a project's namespace is visible to principals of that project."""
    if not namespace or ":" not in namespace:
        return
    project = namespace.split(":", 1)[1].split("/", 1)[0]
    if not ctx.principal.may_access_project(project):
        raise ScopeDenied(
            f"the asset belongs to project {project!r}, outside this principal's projects",
            details={"namespace": namespace, "allowed": list(ctx.principal.project_refs)},
        )


def _parse_ref(ref: str) -> tuple[str, str]:
    asset_id, _, version = ref.partition("@")
    return asset_id, version


def _summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"check_id": r["check_id"], "result": r["result"], "check_run_id": r["check_run_id"]} for r in results]


@tool("gx_asset_submit")
def asset_submit(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    """Register a candidate from a bundle, then run the checks its next transition lists."""
    kind = args["kind"]
    if kind not in contracts.label_values("asset_kind"):
        raise SchemaInvalid(
            f"{kind!r} is not an asset kind; known: {contracts.label_values('asset_kind')}",
            details={"kind": kind},
        )
    bundle_kind = manifests.KIND_BUNDLE.get(kind)
    if bundle_kind and validator_for(bundle_kind) is None:
        raise SchemaInvalid(f"no installed asset provider accepts {kind!r}", details={"kind": kind})
    conn = ctx.conn
    bundle = _artifacts.read_json(conn, args["bundle_ref"])
    task = _task_mapping(conn, args.get("task_id"))
    run_id = args.get("run_id") or (task or {}).get("run_id")
    request = {k: v for k, v in args.items() if k != "idempotency_key"}
    key = store.tool_key(ctx.tool or "gx_asset_submit", args["idempotency_key"])

    def register(tx: Any) -> dict[str, Any]:
        if kind in manifests.KIND_BUNDLE:
            bundle_kind = manifests.KIND_BUNDLE[kind]
            # An asset is one object. The shape step of the framework drops malformed records
            # and judges the rest, which is right for a list of findings and wrong here: the
            # validator is told how many records it never saw, and refuses to register a
            # version whose content is not what was handed in.
            judged_as = dict(task or {})
            judged_as["refused_on_shape"] = schema.check_bundle(bundle_kind, bundle).refused_count
            verdict = framework.judge(ctx, judged_as, bundle_kind, bundle, commit=True)
            gaps = [r for r in verdict.records if r.code == "IMPLEMENTATION_GAP"]
            if gaps:
                raise ImplementationGap(gaps[0].message, details={"bundle_kind": bundle_kind})
            if not verdict.accepted:
                raise SchemaInvalid(
                    f"the {bundle_kind} was not accepted: {verdict.summary or 'see the per-record refusals'}",
                    details={"bundle_kind": bundle_kind, "verdict": framework.full_view(verdict)},
                )
            created = verdict.created.get("assets") or []
            if len(created) != 1:
                raise ImplementationGap(
                    f"the {bundle_kind} validator accepted the bundle and registered {len(created)} "
                    f"object(s); gx_asset_submit registers exactly one",
                    details={"bundle_kind": bundle_kind, "created": verdict.created},
                )
            asset_id, version = _parse_ref(created[0])
            return {"asset_id": asset_id, "version": version, "kind": kind}
        if manifests.schema_source(kind) is None:
            raise ImplementationGap(
                f"nothing declares what the content of a {kind} looks like; the frozen contract has no "
                "bundle for it and no service has declared one",
                details={"kind": kind},
            )
        content_ref = assets.publish_content(ctx, bundle)
        out = assets.submit_asset(
            ctx, kind=kind, manifest=bundle["manifest"], content_ref=content_ref,
            origin="AUTHORED" if ctx.principal.scope == SCOPE_OPERATOR else "GENERATED",
            run_id=run_id, task_id=args.get("task_id"),
        )
        return {"asset_id": out["asset_ref"]["asset_id"], "version": out["asset_ref"]["version"], "kind": kind}

    registered, _replay = store.run_idempotent(conn, key, ctx.tool or "gx_asset_submit", request, register)

    ran = assets.run_required(ctx, registered["asset_id"], registered["version"])
    asset = assets.get(conn, registered["asset_id"], registered["version"])
    return ToolResult(
        data={"asset_ref": assets.asset_ref(asset), "status": asset["status"], "queued_checks": _summaries(ran)},
        run_id=run_id,
    )


@tool("gx_asset_status")
def asset_status(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """What is on record for an asset's current sha256 and what its next transition still lacks."""
    asset = assets.get(ctx.conn, args["asset_id"], args.get("version"), kind=args.get("kind"))
    _check_scope(ctx, asset["namespace"])
    full = assets.status(ctx.conn, asset["asset_id"], asset["version"])
    data = {key: full[key] for key in
            ("asset_ref", "kind", "status", "checks", "reviews", "approvals", "missing", "next_state")}
    data["projection"] = projection_view(ctx.conn, asset)
    result = ToolResult(data=data)
    limited = projection_limits(ctx.conn, asset)
    if limited:
        # the same fact as projection.limited, in words a reader of the answer sees first
        result.warnings.append(limited)
    basis = known_answer_note(asset)
    if basis:
        # the lead's ruling of 2026-11-13T01:15Z: a known-answer rung never reads as passed when nothing
        # was checked; inspect and status both say what the version's VALIDATED rests on
        result.warnings.append(basis)
    return result


#: The asset kind a capability version is registered as.
TOOL_ADAPTER = contracts.require_label("asset_kind", "TOOL_ADAPTER")


def known_answer_note(asset: Mapping[str, Any]) -> str | None:
    """What a capability's checks did not exercise, in the capability registry's own words
    (``gx_core.capabilities.registry.known_answer_view``): for a version whose manifest declares no
    known-answer control, the sentence that L4 was not exercised, and from VALIDATED on what VALIDATED
    rests on instead. None for another kind, and for a version that declares one.
    """
    if asset.get("kind") != TOOL_ADAPTER:
        return None
    from ..capabilities import registry as capability_registry  # local: capabilities loads governance

    view = capability_registry.known_answer_view(asset.get("manifest"), asset.get("status"))
    if view.get("declared"):
        return None
    return view.get("validated_basis") or view.get("not_exercised")


#: Where the host files of an asset that has a host projection are (labels.projection_state, r5).
HOST_FOLDERS = contracts.require_label("projection_state", "HOST_FOLDERS")
STAGED = contracts.require_label("projection_state", "STAGED")
BACKEND_ONLY = contracts.require_label("projection_state", "BACKEND_ONLY")
NOT_ACTIVE = contracts.require_label("projection_state", "NOT_ACTIVE")
#: The effect that writes host files (the host stream's ``hostobs.projection.project_to_hosts``).
PROJECTION_EFFECT = "project_to_hosts"


def projected_kinds() -> tuple[str, ...]:
    """The kinds that have a host projection: those ``projection_parity`` applies to (contracts/gates.json)."""
    return tuple(contracts.check_spec("projection_parity")["applies_to"])


def _licence_limits(conn: Any, asset: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """``(limited, unverified)`` from the asset's latest ``licence_recorded`` run for these bytes."""
    subject = Subject(kind=subjects.ASSET, id=asset["asset_id"], sha256=asset["sha256"], version=asset["version"])
    latest = checks.latest_results(conn, subject).get("licence_recorded")
    if latest is None:
        return False, []
    detail = checks.get_run(conn, latest["check_run_id"])["detail"] or {}
    if not detail.get("projection_limited"):
        return False, []
    return True, [str(item) for item in detail.get("unverified") or []]


def projection_limits(conn: Any, asset: Mapping[str, Any]) -> str | None:
    """Why an asset gets no host projection, from its latest ``licence_recorded`` run; None when it does.

    An unestablished licence -- the asset's own recorded as UNVERIFIED, or a cited source with
    none on record -- does not fail the asset; it limits where it may be projected. A reader of
    the status or of the skill is told so, so the absence of a host file is not a mystery.
    """
    limited, unverified = _licence_limits(conn, asset)
    if not limited:
        return None
    return ("projection limited: no established licence for " + "; ".join(unverified[:6])
            + ("; …" if len(unverified) > 6 else "")
            + ". The asset is served by the backend (gx_skill_get for a skill) and is not written into "
              "a host's skill or agent folders")


def _projection_record(conn: Any, asset: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """What the projection effect reported for exactly these bytes: ``(written, skipped)``, the last report.

    Read from the ``asset.effects`` event the promotion writes in the transaction of the move
    to ACTIVE, so a file is listed because the effect said it wrote it, and ``installed``
    is the effect's own word for whether it went into an installed project.
    """
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT payload_json FROM events WHERE kind='asset.effects' AND subject_kind=? AND subject_id=?"
        " ORDER BY event_id", (subjects.ASSET, asset["asset_id"]),
    ):
        payload = store.loads(row, "payload_json", {}) or {}
        if payload.get("sha256") != asset["sha256"]:
            continue
        result = (payload.get("effects") or {}).get(PROJECTION_EFFECT)
        if not isinstance(result, Mapping):
            continue
        written = [{"host": item.get("host"), "path": item.get("path"), "sha256": item.get("sha256"),
                    "installed": bool(item.get("installed"))}
                   for item in result.get("written") or [] if isinstance(item, Mapping)]
        skipped = [{"host": item.get("host"), "reason": str(item.get("reason") or "")}
                   for item in result.get("skipped") or [] if isinstance(item, Mapping)]
    return written, skipped


def projection_view(conn: Any, asset: Mapping[str, Any]) -> dict[str, Any] | None:
    """Where an asset's host files are (contract r5 ``projection`` on gx_asset_status and gx_skill_get).

    ``{"where", "limited", "unverified", "written", "skipped"}``, or None for a kind that has no
    host projection. ``where`` is NOT_ACTIVE for a version that is not ACTIVE; for an ACTIVE one
    HOST_FOLDERS when a file went into an installed project, STAGED when files exist beside the
    store only, BACKEND_ONLY when none was written (an unestablished licence, or every host
    skipped). ``limited`` and ``unverified`` come from the latest ``licence_recorded`` run.
    """
    if asset["kind"] not in projected_kinds():
        return None
    limited, unverified = _licence_limits(conn, asset)
    active = asset["status"] == assets.ACTIVE
    written, skipped = _projection_record(conn, asset) if active else ([], [])
    if not active:
        where = NOT_ACTIVE
    elif any(item["installed"] for item in written):
        where = HOST_FOLDERS
    elif written:
        where = STAGED
    else:
        where = BACKEND_ONLY
    return {"where": where, "limited": limited, "unverified": unverified, "written": written, "skipped": skipped}


def _gate_summary(report: Mapping[str, Any], effects_run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "met": report["met"], "to_state": report["to_state"],
        "checks": [{"check_id": c["check_id"], "result": c["result"], "check_run_id": c.get("check_run_id")}
                   for c in report["checks"]],
        "review": ({"review_id": report["review"]["review_id"],
                    "independence_level": report["review"]["independence_level"]}
                   if report["review"] else None),
        "approval": ({"approval_id": report["approval"]["approval_id"], "status": report["approval"]["status"],
                      "granted_by": report["approval"].get("granted_by")}
                     if report["approval"] else None),
        "effects": dict(effects_run),
    }


@tool("gx_asset_promote")
def asset_promote(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """Run the gate for one transition; refuse when anything it requires is missing."""
    conn = ctx.conn
    asset_id, version, to_state = args["asset_id"], args["version"], args["to_state"]
    request = {k: v for k, v in args.items() if k != "idempotency_key"}
    key = store.tool_key(ctx.tool or "gx_asset_promote", args["idempotency_key"])
    stored = _replayed(conn, key, request)
    if stored is not None:
        return stored
    if args.get("kind"):
        assets.get(conn, asset_id, version, kind=args["kind"])  # ARTIFACT_NOT_FOUND when the kind differs

    # What the backend can establish now is established and stays on record even if the
    # promotion is then refused; only the move itself is keyed.
    assets.prepare_promotion(ctx, asset_id, version, to_state, args["expected_sha256"])

    def move(tx: Any) -> dict[str, Any]:
        out = assets.commit_promotion(ctx, asset_id, version, to_state, args["expected_sha256"])
        return {"asset_ref": out["asset_ref"], "status": out["status"],
                "gate": _gate_summary(out["gate"], out["effects"])}

    response, _replay = store.run_idempotent(conn, key, ctx.tool or "gx_asset_promote", request, move)
    return response


@tool("gx_approval_grant")
def approval_grant(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """Decide one approval for one subject and one sha256."""
    request = {k: v for k, v in args.items() if k != "idempotency_key"}
    key = store.tool_key(ctx.tool or "gx_approval_grant", args["idempotency_key"])

    def decide(tx: Any) -> dict[str, Any]:
        decided = approvals.decide(
            ctx, approval_id=args["approval_id"], decision=args["decision"], note=args["note"],
            expected_subject_sha256=args.get("expected_subject_sha256"), policy_ref=args.get("policy_ref"),
            decision_detail=args.get("decision_detail"),
        )
        return {"approval_id": decided["approval_id"], "status": decided["status"],
                "granted_by": decided["decided_by"]}

    response, _replay = store.run_idempotent(ctx.conn, key, ctx.tool or "gx_approval_grant", request, decide)
    return response


# ============================================================================== checks

def _asset_of(ctx: Ctx, subject: Subject) -> dict[str, Any]:
    info = subjects.resolve(ctx.conn, subject)
    if subject.kind != subjects.ASSET:
        raise ImplementationGap(
            f"this check reads the content of an asset; where the content of a {subject.kind} lives has "
            "not been agreed with the stream that owns it",
            details={"subject": subjects.as_dict(subject)},
        )
    return assets.get(ctx.conn, info["row"]["asset_id"], info["row"]["version"])


@check("byte_binding")
def byte_binding(ctx: Ctx, subject: Subject) -> CheckOutcome:
    """Re-hash the content and compare it with what was registered, reviewed and approved."""
    asset = _asset_of(ctx, subject)
    conn = ctx.conn
    verified = _artifacts.verify(conn, asset["content_artifact_ref"])
    problems: list[str] = []
    observed = verified.get("observed_sha256")
    if not verified["ok"]:
        problems.append(
            f"the content artifact does not re-hash to its record ({verified['reason']}): recorded "
            f"{str(verified['recorded_sha256'])[:16]}…, observed {str(observed)[:16]}…"
        )
    if verified["recorded_sha256"] != asset["sha256"]:
        problems.append("the content artifact's recorded sha256 is not the one registered at intake")
    if observed is not None and observed != subject.sha256:
        problems.append("the bytes on disk are not the sha256 this check run is about")

    strays = [
        {"review_id": row["review_id"], "sha256": row["subject_sha256"]}
        for row in conn.execute(
            "SELECT review_id, subject_sha256 FROM reviews WHERE subject_kind=? AND subject_id=?"
            " AND subject_version=? AND superseded=0",
            (subject.kind, subject.id, asset["version"]),
        ) if row["subject_sha256"] != asset["sha256"]
    ]
    if strays:
        problems.append(f"{len(strays)} review(s) of this version are bound to another sha256")
    reviews_here = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE subject_kind=? AND subject_id=? AND subject_sha256=?"
        " AND superseded=0", (subject.kind, subject.id, asset["sha256"]),
    ).fetchone()["n"]
    approvals_here = [
        row["approval_id"] for row in conn.execute(
            "SELECT approval_id FROM approvals WHERE subject_kind=? AND subject_id=? AND subject_sha256=?"
            " AND status=?", (subject.kind, subject.id, asset["sha256"], approvals.GRANTED))
    ]
    detail = {
        "intake_sha256": asset["sha256"], "artifact_recorded_sha256": verified["recorded_sha256"],
        "observed_sha256": observed, "observed_bytes": verified.get("observed_bytes"),
        "content_artifact_ref": asset["content_artifact_ref"], "reviews_at_this_sha256": int(reviews_here),
        "reviews_at_another_sha256": strays, "granted_approvals_at_this_sha256": approvals_here,
    }
    if problems:
        detail["problems"] = problems
    return CheckOutcome(checks.FAIL if problems else checks.PASS, detail=detail)


@check("manifest_schema")
def manifest_schema(ctx: Ctx, subject: Subject) -> CheckOutcome:
    """Validate the content against the schema of its kind: unknown fields refused."""
    asset = _asset_of(ctx, subject)
    kind = asset["kind"]
    source = manifests.schema_source(kind)
    if source is None:
        return CheckOutcome(checks.FAIL, detail={
            "problems": [f"nothing declares what the content of a {kind} looks like"], "kind": kind})
    content = assets.content(ctx.conn, asset)
    found = manifests.violations(kind, content)
    if isinstance(content, dict) and content.get("manifest") != asset["manifest"]:
        found.append({"pointer": "$.manifest", "message": "is not the manifest registered for this version"})
    detail: dict[str, Any] = {"schema": source, "violations": len(found)}
    if found:
        detail["first_violations"] = found[:20]
    return CheckOutcome(checks.FAIL if found else checks.PASS, detail=detail)


def _basis(conn: Any, basis: str) -> tuple[str, bool, str]:
    """Resolve licence record references or files. Relative paths use the installed checkout."""
    text = basis.strip()
    if text.startswith("artifact:"):
        ref = text.split(":", 1)[1]
        return "artifact", conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone() is not None, ref
    if text.startswith("source:"):
        ref = text.split(":", 1)[1]
        return "source", conn.execute("SELECT 1 FROM sources WHERE source_id=?", (ref,)).fetchone() is not None, ref
    if text.startswith("import:"):
        ref = text.split(":", 1)[1]
        return "import", conn.execute("SELECT 1 FROM imports WHERE import_id=?", (ref,)).fetchone() is not None, ref
    path_text = text.split(":", 1)[1] if text.startswith("file:") else text
    path_text = path_text.split("#", 1)[0].strip()
    for prefix in ("{workspace}/", "{harness}/"):
        if path_text.startswith(prefix):
            path_text = path_text.removeprefix(prefix)
            break
    if not path_text or "\n" in path_text or len(path_text) > 1024:
        return "text", False, text[:120]
    candidate = contracts.HARNESS_ROOT / path_text
    try:
        if candidate.is_file():
            return "file", True, str(candidate)
    except (OSError, ValueError):
        pass
    return ("file" if ("/" in path_text or "." in path_text) else "text"), False, path_text


def licence_standing(conn: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What is on record about the licence of an asset and of each source it cites.

    ``{"problems": [...], "unverified": [...], "detail": {...}}``. A *problem* is something
    the asset's own manifest gets wrong: no licence stated, no basis, or a basis that does
    not resolve to a file or a record the backend can find. *Unverified* lists what has no
    established licence — the asset itself when it says ``UNVERIFIED``, and every cited
    source whose row records no licence, records ``UNVERIFIED``, or records one without
    saying where it was read. An unestablished licence does not fail the asset; it limits
    where the asset may be projected, and whoever projects it reads this function.

    A cited source is an entry of ``manifest.sources``: a source id, or ``<source_id>@<sha256>``.
    """
    problems: list[str] = []
    unverified: list[str] = []
    licence, basis = manifest.get("licence"), manifest.get("licence_basis")
    detail: dict[str, Any] = {"licence": licence, "licence_basis": basis}

    if not (isinstance(licence, str) and licence.strip()):
        problems.append("the manifest states no licence")
    if not (isinstance(basis, str) and basis.strip()):
        problems.append("the manifest does not say where its licence was read from")
    elif isinstance(licence, str) and licence.strip().upper() == UNVERIFIED_LICENCE:
        unverified.append("the asset itself")
        detail["basis_kind"] = "statement of what was looked for"
    else:
        basis_kind, present, looked_for = _basis(conn, basis)
        detail.update(basis_kind=basis_kind, basis_resolved_to=looked_for if present else None)
        if not present:
            problems.append(
                f"the licence basis does not resolve to a file or a record the backend can find "
                f"({basis_kind}: {looked_for}); a licence nobody can re-read is recorded as {UNVERIFIED_LICENCE}"
            )

    sources_checked = 0
    for entry in manifest.get("sources") or []:
        source_id = str(entry).split("@", 1)[0]
        row = conn.execute(
            "SELECT source_id, license, license_basis FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        sources_checked += 1
        if row is None:
            problems.append(f"cited source {source_id} is not a source the backend holds")
        elif not (row["license"] and str(row["license"]).strip()):
            unverified.append(f"{source_id} (no licence on record)")
        elif str(row["license"]).strip().upper() == UNVERIFIED_LICENCE:
            unverified.append(f"{source_id} ({UNVERIFIED_LICENCE})")
        elif not (row["license_basis"] and str(row["license_basis"]).strip()):
            unverified.append(f"{source_id} (a licence with no record of where it was read)")
    detail.update(sources_checked=sources_checked, unverified=unverified,
                  projection_limited=bool(unverified))
    return {"problems": problems, "unverified": unverified, "detail": detail}


@check("licence_recorded")
def licence_recorded(ctx: Ctx, subject: Subject) -> CheckOutcome:
    """A licence and the file or record it was read from are present, for the asset and its sources."""
    asset = _asset_of(ctx, subject)
    standing = licence_standing(ctx.conn, asset["manifest"])
    detail = dict(standing["detail"])
    if standing["problems"]:
        detail["problems"] = standing["problems"]
    return CheckOutcome(checks.FAIL if standing["problems"] else checks.PASS, detail=detail)


def script_refs(content: Any) -> list[dict[str, Any]]:
    """Every ``ScriptRef`` (path, artifact_ref, sha256) anywhere in an asset's content."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if {"path", "artifact_ref", "sha256"} <= set(node) and all(
                isinstance(node[k], str) for k in ("path", "artifact_ref", "sha256")
            ):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(content)
    return found


@check("scope_scan")
def scope_scan(ctx: Ctx, subject: Subject) -> CheckOutcome:
    """Scan every bundled script; whatever it finds is a diagnostic for a reviewer to judge."""
    asset = _asset_of(ctx, subject)
    conn = ctx.conn
    refs = script_refs(assets.content(conn, asset))
    if not refs:
        return CheckOutcome(checks.PASS, detail={"scripts": 0})
    forbidden = list(config.paths().get("forbidden_paths", {}).get("patterns", []))
    problems: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    scanned = 0
    for ref in refs:
        path = ref["path"]
        try:
            data = _artifacts.read_bytes(conn, ref["artifact_ref"])
        except (ArtifactNotFound, ArtifactHashMismatch) as exc:
            problems.append(f"{path}: {exc.message}")
            continue
        observed = hashlib.sha256(data).hexdigest()
        if observed != ref["sha256"]:
            problems.append(f"{path}: the bundle pins sha256 {ref['sha256'][:16]}… and the artifact is {observed[:16]}…")
            continue
        if not path.endswith(".py"):
            fingerprint = hashlib.sha256(f"{observed}\0unscannable\0{path}".encode()).hexdigest()[:32]
            diagnostics.append({"fingerprint": fingerprint, "script": path, "script_sha256": observed,
                                "category": "unscannable", "line": 0,
                                "what": "not a Python source; this scan cannot read it", "source": ""})
            continue
        try:
            findings = scan.scan_python(data.decode("utf-8", "replace"), forbidden_patterns=forbidden)
        except SyntaxError as exc:
            problems.append(f"{path}: does not parse as Python ({exc.msg}, line {exc.lineno})")
            continue
        scanned += 1
        diagnostics.extend(f.as_diagnostic(path=path, script_sha256=observed) for f in findings)
    by_category: dict[str, int] = {}
    for item in diagnostics:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
    detail: dict[str, Any] = {"scripts": len(refs), "scanned": scanned, "findings": len(diagnostics),
                              "by_category": by_category}
    if problems:
        detail["problems"] = problems
        return CheckOutcome(checks.FAIL, detail=detail)
    if diagnostics:
        return CheckOutcome(checks.DIAGNOSTIC, detail=detail, diagnostics=diagnostics)
    return CheckOutcome(checks.PASS, detail=detail)


# ===================================================================== the review bundle

def review_items(task: Mapping[str, Any], conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """The items a review task names: each ``{"item_id", ...}``; a diagnostic carries
    ``check_run_id`` and ``fingerprint`` and its id is ``diag:<fingerprint>``.

    A task row is an instruction, not a container (core caps its input at 8 KiB), so a task
    that names many items names them by reference: ``input.review_package_ref`` is an
    artifact the backend published whose ``items`` are the list. It is read, re-hashed, from
    the store; an input that names a package the store cannot produce is an error, not an
    empty list — an unreadable list of what to judge would otherwise be a review of nothing.
    """
    task_input = task.get("input") or {}
    items = list(task_input.get("review_items") or [])
    package_ref = task_input.get("review_package_ref")
    if package_ref:
        if conn is None:
            raise SchemaInvalid("the review task names its items by reference and no store was given to read them")
        package = _artifacts.read_json(conn, package_ref)
        items.extend(package.get("items") or [])
    return [item for item in items if isinstance(item, dict) and item.get("item_id")]


@bundle_validator("asset_review_bundle")
def judge_asset_review(ctx: Ctx, task: Mapping[str, Any] | None, bundle: dict, *, commit: bool) -> BundleVerdict:
    """Judge a review: the right bytes, an independent reviewer, every named item answered.

    Accepted when those hold. The verdict is then recorded whatever it is; only an APPROVE
    at a sufficient independence level meets a gate, and the answer says whether this one does.
    """
    task = dict(task or {})
    conn = ctx.conn
    records: list[RecordVerdict] = []
    whole_ok = True

    def refuse(code: str, message: str, **details: Any) -> None:
        nonlocal whole_ok
        whole_ok = False
        records.append(RecordVerdict(index=-1, ok=False, code=code, message=message, details=details))

    subject: Subject | None = None
    info: dict[str, Any] | None = None
    try:
        subject = subjects.from_ref(bundle["subject"])
        info = reviews.resolve_review_subject(ctx, subject)
    except GxError as exc:
        refuse(exc.code, exc.message, **exc.details)

    human = ctx.principal.scope == SCOPE_OPERATOR and approvals.person_shown(ctx) and not task.get("task_id")
    human_task = reviews.human_task_review(ctx, task) if task.get("task_id") else False
    basis: dict[str, Any] | None = None
    if subject is not None and info is not None:
        if info["current_sha256"] != subject.sha256:
            refuse("CONTENT_HASH_MISMATCH",
                   f"the review is about sha256 {subject.sha256[:16]}… and the store holds "
                   f"{str(info['current_sha256'])[:16]}…", current_sha256=info["current_sha256"])
        elif human:
            basis = {"level": reviews.HUMAN, "why": "recorded by a person through the operator surface"}
        elif not task.get("task_id"):
            refuse("SCHEMA_INVALID", "a worker's review is handed in under a review task; none is named")
        else:
            mismatch = [k for k, v in (("subject_kind", subject.kind), ("subject_id", subject.id),
                                       ("subject_sha256", subject.sha256)) if task.get(k) != v]
            if mismatch:
                refuse("CONTENT_HASH_MISMATCH",
                       "the review task was created for another subject or another sha256",
                       task_subject={k: task.get(k) for k in ("subject_kind", "subject_id", "subject_sha256")})
            elif human_task:
                basis = {"level": reviews.HUMAN, "why": "a person submitted a leased review through the confirmed operator surface"}
            else:
                author_task_id = task.get("independent_of_task_id") or info["author_task_id"]
                basis = reviews.explain_independence(
                    conn, author_task_id=author_task_id, reviewer_task_id=task["task_id"],
                    author_lease_id=info["author_lease_id"] if author_task_id == info["author_task_id"] else None,
                    author_principal=info["author_principal"], reviewer_lease_id=task.get("lease_id"),
                )
                if basis["level"] == reviews.NOT_INDEPENDENT:
                    refuse("INDEPENDENCE_REQUIRED",
                           f"the review is not independent of the author: {basis['why']}", independence=basis)
    if bundle.get("reproducing_case") is not None:
        refuse("SCHEMA_INVALID", "an asset review accepts diagnostic findings in row_verdicts",
               field="reproducing_case")

    # every named item has a verdict, and an approving review has no item it does not support
    verdict = bundle["verdict"]
    row_verdicts = bundle.get("row_verdicts") or []
    def decision(row: Mapping[str, Any]) -> tuple[str, str]:
        return row["verdict"], row.get("note") or ""

    given: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, row in enumerate(row_verdicts):
        previous = given.get(row["row_id"])
        if previous is not None and decision(previous[1]) != decision(row):
            refuse("SCHEMA_INVALID", f"item {row['row_id']!r} has conflicting repeated verdicts or notes",
                   field="row_verdicts", item_id=row["row_id"], indexes=[previous[0], index])
        given[row["row_id"]] = (index, row)
    try:
        named = review_items(task, conn)
    except GxError as exc:
        named = []
        refuse(exc.code, f"the items this review task names cannot be read: {exc.message}", **exc.details)
    all_named_answered = True
    for item in named:
        if item["item_id"] not in given:
            all_named_answered = False
            records.append(RecordVerdict(
                index=-1, ok=False, code="SCHEMA_INVALID",
                message=f"the task names item {item['item_id']!r} and the review gives it no verdict",
                details={"field": "row_verdicts", "item_id": item["item_id"]},
            ))
    adjudication_plan: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for item in named:
        if not str(item["item_id"]).startswith(DIAGNOSTIC_ITEM_PREFIX) or item["item_id"] not in given:
            continue
        row = given[item["item_id"]][1]
        key = (item["check_run_id"], item["fingerprint"])
        previous = adjudication_plan.get(key)
        if previous is not None and decision(previous[1]) != decision(row):
            refuse("SCHEMA_INVALID", "one diagnostic pair has conflicting verdicts or notes",
                   field="row_verdicts", check_run_id=key[0], fingerprint=key[1])
        else:
            adjudication_plan.setdefault(key, (item, row))
    # what a review of this kind of asset must carry beyond the general rules
    reviewed_asset: dict[str, Any] | None = None
    kind_rules_ok = True
    if subject is not None and info is not None and subject.kind == subjects.ASSET:
        reviewed_asset = assets.get(conn, subject.id, subject.version)
        for problem in effects.review_problems(ctx, reviewed_asset, bundle):
            kind_rules_ok = False
            records.append(RecordVerdict(
                index=int(problem.get("index", -1)), ok=False, code="SCHEMA_INVALID",
                message=str(problem.get("message")),
                details={"field": problem["field"]} if problem.get("field") and int(problem.get("index", -1)) >= 0 else {},
            ))

    unsupported = [row["row_id"] for row in row_verdicts if row["verdict"] != SUPPORTED]
    consistent = not (verdict == APPROVE and unsupported)
    for index, row in enumerate(row_verdicts):
        if verdict == APPROVE and row["verdict"] != SUPPORTED:
            records.append(RecordVerdict(
                index=index, ok=False, code="SCHEMA_INVALID",
                message=f"an approving review cannot carry item {row['row_id']!r} judged {row['verdict']}; "
                        "a review that finds a defect says REQUEST_CHANGES or REJECT",
                details={"field": "row_verdicts"},
            ))
        else:
            records.append(RecordVerdict(index=index, ok=True, ref=row["row_id"], details={"field": "row_verdicts"}))

    accepted = (whole_ok and all_named_answered and consistent and kind_rules_ok
                and subject is not None and basis is not None)
    created: dict[str, list[str]] = {}
    summary = "not accepted"
    if accepted:
        sufficient = basis["level"] in contracts.sufficient_independence()
        roles = reviews.review_roles(info)
        in_role = human or not roles or task.get("role") in roles
        summary = (
            f"review {verdict} at independence level {basis['level']}"
            + ("" if sufficient else "; this level meets no gate")
            + ("" if in_role else f"; handed in under role {task.get('role')!r}, and a gate counts a review "
                                  f"done as {' or '.join(sorted(roles))}")
        )
        records.append(RecordVerdict(
            index=-1, ok=True, message=summary,
            details={"independence_level": basis["level"], "why": basis["why"],
                     "task_role": task.get("role"), "gate_roles": sorted(roles),
                     "meets_gate": sufficient and in_role and verdict == APPROVE},
        ))
    if accepted and commit:
        published = _artifacts.publish_content_addressed(
            conn, data=_artifacts.canonical_bytes(bundle), kind="asset_review", actor=ctx.actor,
            area="reviews", provenance={"task_id": task.get("task_id"), "subject": subjects.as_dict(subject)},
            origin="HOST_PUT",
        )
        if human:
            review = reviews.record_human_review(
                ctx, subject=subject, verdict=verdict, rubric=bundle.get("rubric") or [],
                note=bundle.get("notes") or "",
            )
        else:
            review = reviews.record_review(
                ctx, subject=subject, task=task, verdict=verdict, rubric=bundle.get("rubric") or [],
                bundle_artifact_ref=published.artifact_ref,
            )
        created["reviews"] = [review["review_id"]]
        adjudicated: list[str] = []
        for item, row in adjudication_plan.values():
            decided = checks.adjudicate(
                ctx, check_run_id=item["check_run_id"], fingerprint=item["fingerprint"],
                decision=checks.ADJ_ACCEPTED if row["verdict"] == SUPPORTED else checks.ADJ_DEFECT,
                note=row.get("note") or f"judged {row['verdict']} in review {review['review_id']}",
                review_id=review["review_id"],
            )
            adjudicated.append(decided["adjudication_id"])
        if adjudicated:
            created["adjudications"] = adjudicated
        if reviewed_asset is not None:
            # The owner of the kind is told, inside this transaction: a skill moves on as far
            # as its gates now allow and has its scenarios handed out.
            followed = effects.notify_review(ctx, reviewed_asset, review, task)
            for key, value in followed.items():
                if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                    created[key] = list(value)
            if followed.get("summary"):
                summary = f"{summary}; {followed['summary']}"
    return BundleVerdict(bundle_kind="asset_review_bundle", records=records, accepted=accepted,
                         created=created, summary=summary)
