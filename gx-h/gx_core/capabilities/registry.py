"""The capability registry: what is callable, on what evidence, and what it is.

Three questions get asked of a capability version, and v1 answered all three from
whatever the caller wrote:

1. *May it run?* — only when its asset is ACTIVE for the sha256 that was reviewed.
2. *What is it?* — family, mechanism, objective, estimator and training-data lineage.
3. *What does it need?* — interpreter, launch recipe, weights, threads, GPU, wall time.

Here each answer is read from something the backend holds. A capability version is a
``TOOL_ADAPTER`` asset: the asset row, written only by the governance engine, carries
its status, and this module never writes one. The version's content is the asset's
content artifact — the canonical bytes of its ``capability_manifest_bundle`` — and every
use re-reads those bytes and re-hashes them. The ``capabilities`` table is an index for
inspection whose ``status`` column mirrors the asset; execution reads the verified
document and the asset row, never the mirror.

    register_candidate   a bundle becomes a candidate (asset DRAFT, never callable)
    resolve              ACTIVE only: the verified document and its environment
    candidate            any registered version, for the probes and checks to look at
    method_card          the only source of method identity
    equivalence          what the backend measured about two versions' outputs
    record_probe         one rung of the ladder, as an observation

Published for other streams: :func:`resolve`, :func:`method_card`, :func:`equivalence`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import contracts
from ..context import Ctx
from ..domain import clock, ids
from ..errors import (
    ArtifactHashMismatch,
    ArtifactNotFound,
    CapabilityNotApproved,
    ContentHashMismatch,
    GxError,
    ImplementationGap,
    SchemaInvalid,
    VersionImmutable,
)
from ..persistence import store
from . import environments
from . import manifest as manifest_mod
from . import storage

ASSET_KIND = manifest_mod.ASSET_KIND
ACTIVE = "ACTIVE"

SCRIPT_KIND = "capability_script"
SCRIPT_NAMESPACE = "capabilities/scripts"

#: The ladder, lowest rung first. From ``labels.probe_rung``.
RUNGS = ("L0_FILES", "L1_METADATA", "L2_ENTRYPOINT", "L3_SMOKE", "L4_KNOWN_ANSWER")
#: Rungs that exist only as a job the backend ran.
JOB_RUNGS = ("L3_SMOKE", "L4_KNOWN_ANSWER")

#: LOCAL vocabulary, not from contracts/: what an identity control measured.
EQUIVALENT_OUTPUT = "EQUIVALENT_OUTPUT"
DIFFERENT_OUTPUT = "DIFFERENT_OUTPUT"
EQUIVALENCE_RELATIONS = (EQUIVALENT_OUTPUT, DIFFERENT_OUTPUT)

#: LOCAL: the one probe that is not a rung of ``labels.probe_rung``. It records the three
#: behaviours ``error_timeout_idempotency`` checks, observed through the job service.
ERROR_PROBE = "ERROR_TIMEOUT_IDEMPOTENCY"


# ------------------------------------------------------------------------ governance seam

def _governance_assets() -> Any:
    """``gx_core.governance.assets``: the one writer of asset rows and their status.

    Imported where it is used. Reading what is registered must not depend on the whole
    governance package importing, and a capability with no asset record is refused by
    :func:`resolve` whatever the reason there is none.
    """
    try:
        from ..governance import assets
    except ModuleNotFoundError as exc:  # pragma: no cover - a tree without governance
        raise ImplementationGap(
            "the asset lifecycle (gx_core.governance.assets) is not importable, so a capability "
            "can be neither registered nor read",
            details={"missing": exc.name},
        ) from exc
    return assets


def asset_row(conn: sqlite3.Connection, capability_id: str, version: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT asset_id, version, kind, status, content_sha256, content_artifact_ref"
        " FROM assets WHERE asset_id=? AND version=? AND kind=?",
        (capability_id, version, ASSET_KIND),
    ).fetchone()


# ------------------------------------------------------------------------------ scripts

def publish_script(ctx: Ctx, rel_path: str) -> dict[str, str]:
    """Publish one script of this tree by content and return the ScriptRef that binds it.

    ``rel_path`` is relative to this tree. The reference names the path the bytes were
    read from, the artifact that holds a copy of them, and their sha256; a job later
    refuses to launch if the file at that path no longer hashes to it.
    """
    path = manifest_mod.harness_root() / rel_path
    if (not manifest_mod.allowed_script_path(rel_path) or ".." in Path(rel_path).parts
            or not path.is_file()):
        raise SchemaInvalid(
            f"{rel_path!r} is not a file of this tree inside one of "
            f"{list(manifest_mod.SCRIPT_ROOTS)}",
            details={"path": rel_path},
        )
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    published = storage.artifact_service(ctx.conn).publish_bytes(
        namespace=f"{SCRIPT_NAMESPACE}/{digest[:16]}", rel_path=Path(rel_path).name,
        kind=SCRIPT_KIND, data=data, media_type="text/x-python", actor=ctx.actor,
        expected_sha256=digest, provenance={"path": rel_path, "sha256": digest},
    )
    return {"path": rel_path, "artifact_ref": published.artifact_ref, "sha256": digest}


def _check_script_ref(conn: sqlite3.Connection, ref: Mapping[str, Any], *, where: str) -> None:
    manifest_mod.script_path(ref)  # the file in the tree hashes to the bound sha256
    row = conn.execute(
        "SELECT sha256 FROM artifacts WHERE artifact_ref=?", (ref["artifact_ref"],)
    ).fetchone()
    if row is None:
        raise ArtifactNotFound(
            f"{where}.artifact_ref {ref['artifact_ref']!r} is not a published artifact",
            details={"artifact_ref": ref["artifact_ref"], "where": where},
        )
    if row["sha256"] != ref["sha256"]:
        raise ContentHashMismatch(
            f"{where}: the artifact holds {row['sha256'][:16]}… and the reference says "
            f"{ref['sha256'][:16]}…",
            details={"where": where, "artifact_sha256": row["sha256"],
                     "declared_sha256": ref["sha256"]},
        )
    check = storage.artifact_service(conn).verify(ref["artifact_ref"])
    if not check["ok"]:
        raise ArtifactHashMismatch(
            f"{where}: artifact {ref['artifact_ref']} no longer matches its recorded hash",
            details=check,
        )


# --------------------------------------------------------------------------- registration

def register_candidate(
    ctx: Ctx,
    bundle: Any,
    *,
    origin: str = "AUTHORED",
    run_id: str | None = None,
    task_id: str | None = None,
    commit: bool = True,
    shipped_digest: str | None = None,
) -> dict[str, Any]:
    """Take a manifest bundle in as a candidate capability version.

    Checked, in the order the refusals are useful: the bundle against the contract's
    schema and this package's rules; the environment exists; each bound script hashes to
    its reference, in the tree and in the artifact store; the id and version are new, or
    the content sha256 equals the registered one.

    A candidate is not callable. Its asset is created by the governance engine in its
    initial state (QUARANTINED for imported content) and takes the gates from there; this
    function never writes a status. With ``commit=False`` nothing is written and the
    answer says what would happen.
    """
    canonical = manifest_mod.require_valid(bundle)
    manifest = canonical["manifest"]
    contracts.require_label("asset_origin", origin)
    conn = ctx.conn
    capability_id, version = manifest["capability_id"], manifest["version"]
    capability_version = manifest_mod.capability_version(manifest)

    environments.get(conn, manifest["env_id"])  # ENV_NOT_READY when it is not registered
    for name in ("entry_script", "parser"):
        if canonical.get(name) is not None:
            _check_script_ref(conn, canonical[name], where=name)

    digest = manifest_mod.content_sha256(canonical)
    existing = _capability_row(conn, capability_id, version)
    if existing is not None and existing["manifest_sha256"] != digest:
        raise VersionImmutable(
            f"{capability_version} is registered with content {existing['manifest_sha256'][:16]}…; "
            f"this bundle hashes to {digest[:16]}…. Changed content is a new version",
            details={"capability_version": capability_version,
                     "registered_sha256": existing["manifest_sha256"], "offered_sha256": digest},
        )

    result: dict[str, Any] = {
        "capability_version": capability_version, "manifest_sha256": digest,
        "created": existing is None, "content_artifact_ref": None, "asset_ref": None,
        "status": existing["status"] if existing is not None else None,
    }
    if not commit:
        return result

    assets = _governance_assets()
    with store.transaction(conn) as txn:
        content_ref = assets.publish_content(ctx, canonical)
        answer = assets.submit_asset(
            ctx, kind=ASSET_KIND, manifest=manifest, content_ref=content_ref, origin=origin,
            run_id=run_id, task_id=task_id,
            source={"admission": "SHIPPED", "shipped_sha256": shipped_digest} if shipped_digest else None,
        )
        recorded = answer["asset_ref"]["sha256"]
        if recorded != digest:
            raise ContentHashMismatch(
                f"the asset registry recorded {recorded[:16]}… for {capability_version} and its "
                f"canonical content hashes to {digest[:16]}…",
                details={"asset_sha256": recorded, "content_sha256": digest},
            )
        result.update(content_artifact_ref=content_ref, asset_ref=answer["asset_ref"],
                      status=answer["status"])
        if existing is None:
            entry = manifest["entry"]
            txn.execute(
                "INSERT INTO capabilities(capability_id, version, kind, status, manifest_sha256,"
                " env_id, adapter_id, source_json, weights_json, required_data_json,"
                " argv_template_json, entry_json, cwd, method_card_json, resource_requirements_json,"
                " outputs_json, license, output_schema_ref, probe_rung, test_status,"
                " evidence_refs_json, limitations, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    capability_id, version, manifest["kind"], answer["status"], digest,
                    manifest["env_id"], (canonical.get("parser") or {}).get("path"),
                    ids.canonical_json(manifest["source"]),
                    ids.canonical_json(manifest["weights"]),
                    ids.canonical_json(manifest["required_data"]),
                    ids.canonical_json(entry["argv_template"]), ids.canonical_json(entry),
                    entry.get("cwd"), ids.canonical_json(manifest["method_card"]),
                    ids.canonical_json(manifest["resources"]),
                    ids.canonical_json(manifest["outputs"]), manifest["licence"], None, None,
                    contracts.require_label("test_status", "STATIC_REVIEW_ONLY"),
                    ids.canonical_json([content_ref]), manifest["limitations"], clock.now_iso(),
                ),
            )
            store.append_event(
                txn, kind="capability.candidate_registered", actor=ctx.actor, run_id=run_id,
                subject_kind="capability", subject_id=capability_version,
                new_state=answer["status"], evidence_level="LOCAL_CODE_OBSERVED",
                payload={"manifest_sha256": digest, "env_id": manifest["env_id"],
                         "kind": manifest["kind"], "origin": origin,
                         "content_artifact_ref": content_ref},
            )
        else:
            sync_status(txn, capability_id, version)
    return result


# ------------------------------------------------------------------------------- reading

def describe(row: sqlite3.Row, bundle: Mapping[str, Any], status: str) -> dict[str, Any]:
    """One registered version as other streams read it. ``manifest`` is the verified document."""
    manifest = bundle["manifest"]
    return {
        "capability_id": row["capability_id"], "version": row["version"],
        "capability_version": f"{row['capability_id']}@{row['version']}",
        "kind": manifest["kind"], "status": status, "manifest_sha256": row["manifest_sha256"],
        "env_id": manifest["env_id"], "resources": dict(manifest["resources"]),
        "outputs": [dict(output) for output in manifest["outputs"]],
        "method_card": dict(manifest["method_card"]), "manifest": manifest, "bundle": dict(bundle),
        "probe_rung": row["probe_rung"], "test_status": row["test_status"], "environment": {},
    }


def _capability_row(conn: sqlite3.Connection, capability_id: str, version: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM capabilities WHERE capability_id=? AND version=?", (capability_id, version)
    ).fetchone()


def _refusal(capability_version: str, reason: str, message: str, **details: Any) -> CapabilityNotApproved:
    return CapabilityNotApproved(
        message, details={"capability_version": capability_version, "reason": reason, **details})


def load_bundle(conn: sqlite3.Connection, capability_id: str, version: str, *,
                expected_sha256: str) -> dict[str, Any]:
    """The version's document, from the asset's content artifact, re-hashed before use.

    Three digests have to agree: the one this registry recorded at intake, the one on the
    asset row, and the one the bytes hash to now. Nothing is launched from content that
    moved, whichever of the three moved.
    """
    capability_version = f"{capability_id}@{version}"
    assets = _governance_assets()
    try:
        asset = assets.get(conn, capability_id, version, kind=ASSET_KIND)
    except ArtifactNotFound as exc:
        raise _refusal(capability_version, "no_asset_record",
                       f"{capability_version} has no asset record; a capability exists only "
                       "through the asset lifecycle") from exc
    if asset["sha256"] != expected_sha256:
        raise ContentHashMismatch(
            f"the asset of {capability_version} is bound to {asset['sha256'][:16]}… and the "
            f"capability was registered with {expected_sha256[:16]}…",
            details={"asset_sha256": asset["sha256"], "manifest_sha256": expected_sha256},
        )
    bundle = assets.content(conn, asset)  # CONTENT_HASH_MISMATCH when the bytes moved
    observed = manifest_mod.content_sha256(bundle)
    if observed != expected_sha256:
        raise ContentHashMismatch(
            f"the content of {capability_version} is not in canonical form: it hashes to "
            f"{observed[:16]}… canonically and was registered as {expected_sha256[:16]}…",
            details={"observed_sha256": observed, "manifest_sha256": expected_sha256},
        )
    return bundle


def measured(conn: sqlite3.Connection, capability_version: str) -> dict[str, Any]:
    """What jobs of this version used, from the runner's measurements of those that succeeded.

    ``resources`` in a manifest is what a capability *declares* it needs, and is what a
    job is admitted and limited by. This is what its jobs were *observed* to use, for a
    scheduler that wants to plan with real figures. It is empty until a job has succeeded,
    and it says how many jobs it rests on: one smoke job on a 20-nucleotide hairpin is a
    measurement of that, not of the capability at large.
    """
    capability_id, version = manifest_mod.parse_capability_version(capability_version)
    rows = conn.execute(
        "SELECT measured_json FROM jobs WHERE capability_id=? AND capability_version=? AND status='SUCCEEDED'",
        (capability_id, version)).fetchall()
    series: dict[str, list[float]] = {"wall_seconds": [], "cpu_seconds": [], "max_rss_mib": [],
                                      "gpu_peak_delta_mib": [], "tool_peak_reserved_mib": []}
    for row in rows:
        item = store.loads(row, "measured_json", {}) or {}
        for key in ("wall_seconds", "cpu_seconds", "max_rss_mib"):
            if isinstance(item.get(key), (int, float)):
                series[key].append(float(item[key]))
        gpu = item.get("gpu") or {}
        for key, source in (("gpu_peak_delta_mib", "peak_delta_mib"), ("tool_peak_reserved_mib", "tool_peak_reserved_mib")):
            peak = gpu.get(source)
            if isinstance(peak, (int, float)):
                series[key].append(float(peak))

    def summary(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        ordered = sorted(values)
        return {"max": ordered[-1], "median": ordered[len(ordered) // 2], "n": len(ordered)}

    return {
        "jobs": len(rows), **{key: summary(values) for key, values in series.items()},
        "basis": (f"the runner's measurements of {len(rows)} SUCCEEDED job(s) of this version in this "
                  "store. gpu_peak_delta_mib describes the shared card. tool_peak_reserved_mib covers "
                  "only the allocator peaks reported by the tool."),
    }


def candidate(conn: sqlite3.Connection, capability_version: str) -> dict[str, Any]:
    """Any registered version, whatever its status. For probes, checks and inspection."""
    capability_id, version = manifest_mod.parse_capability_version(capability_version)
    row = _capability_row(conn, capability_id, version)
    if row is None:
        raise _refusal(capability_version, "not_registered", f"{capability_version} is not registered")
    bundle = load_bundle(conn, capability_id, version, expected_sha256=row["manifest_sha256"])
    asset = asset_row(conn, capability_id, version)
    view = describe(row, bundle, asset["status"])
    view["measured"] = measured(conn, capability_version)
    return view


def resolve(conn: sqlite3.Connection, capability_version: str) -> dict[str, Any]:
    """An ACTIVE capability version, or ``CAPABILITY_NOT_APPROVED`` saying why not.

    ACTIVE is read from the asset row, for the sha256 this version was registered with.
    A name nobody registered, a version with no asset record and a version whose asset is
    anything but ACTIVE are the same answer to a caller who wants to run or plan with it;
    ``details.reason`` says which, and ``details.missing`` what its next gate lacks.
    """
    return active_view(conn, candidate(conn, capability_version))


def active_view(conn: sqlite3.Connection, view: dict[str, Any]) -> dict[str, Any]:
    """The second half of :func:`resolve`, for a caller that already holds the
    :func:`candidate` view and had something to ask of it first (job admission asks the
    operator's switches before it says anything about a version's state): the view with its
    environment's readiness, or ``CAPABILITY_NOT_APPROVED`` when the version is not ACTIVE."""
    capability_version = view["capability_version"]
    shipped = False
    if view["status"] == "DRAFT" and view["capability_version"] == "fixture-grid@1":
        from .shipped import admitted
        source = _governance_assets().get(conn, view["capability_id"], view["version"], kind=ASSET_KIND)["source"]
        shipped = admitted(view, source)
    if shipped:
        view["admission"] = "SHIPPED"
    elif view["status"] != ACTIVE:
        missing: Any = None
        try:
            missing = _governance_assets().status(conn, view["capability_id"], view["version"],
                                                  ASSET_KIND).get("missing")
        except GxError:
            pass
        raise _refusal(
            capability_version, "not_active",
            f"{capability_version} is {view['status']}, not {ACTIVE}; only an {ACTIVE} version "
            f"may be executed or planned against (probe rung reached: {view['probe_rung']})",
            status=view["status"], probe_rung=view["probe_rung"],
            test_status=view["test_status"], missing=missing,
        )
    try:
        view["environment"] = environments.readiness(environments.get(conn, view["env_id"]))
    except GxError as exc:
        view["environment"] = {"env_id": view["env_id"], "ready": False, "reasons": [exc.message]}
    return view


def active_versions(conn: sqlite3.Connection, capability_id: str | None = None) -> list[str]:
    """Every ACTIVE ``id@version``, of one capability or of all, oldest first.

    For a caller that knows a capability by id and needs the version it may run: the
    answer is read from the asset rows, and each entry still goes through :func:`resolve`
    when it is used.
    """
    query = ("SELECT a.asset_id, a.version FROM assets a JOIN capabilities c"
             " ON c.capability_id=a.asset_id AND c.version=a.version"
             " WHERE a.kind=? AND a.status=?")
    params: list[Any] = [ASSET_KIND, ACTIVE]
    if capability_id is not None:
        query += " AND a.asset_id=?"
        params.append(capability_id)
    rows = conn.execute(query + " ORDER BY a.asset_id, a.created_at, a.rowid", params).fetchall()
    return [f"{row['asset_id']}@{row['version']}" for row in rows]


def method_card(conn: sqlite3.Connection, capability_version: str) -> dict[str, Any]:
    """The method identity of a registered version. The only place it is read from."""
    view = candidate(conn, capability_version)
    card = view["method_card"]
    return {
        "capability_version": view["capability_version"],
        "manifest_sha256": view["manifest_sha256"], "status": view["status"], "kind": view["kind"],
        "family": card["family"], "mechanism": card["mechanism"], "objective": card["objective"],
        "estimator": card["estimator"], "training_data_lineage": card["training_data_lineage"],
        "citation_evidence": list(card.get("citation_evidence") or []),
    }


def sync_status(conn: sqlite3.Connection, capability_id: str, version: str) -> str | None:
    """Copy the asset's status into the capability row's mirror column. Returns it."""
    asset = asset_row(conn, capability_id, version)
    if asset is None:
        return None
    conn.execute(
        "UPDATE capabilities SET status=? WHERE capability_id=? AND version=? AND status<>?",
        (asset["status"], capability_id, version, asset["status"]),
    )
    return asset["status"]


# ---------------------------------------------------------------------------- equivalence

def _ordered(a: str, b: str) -> tuple[str, str]:
    manifest_mod.parse_capability_version(a)
    manifest_mod.parse_capability_version(b)
    if a == b:
        raise SchemaInvalid("an equivalence is between two different capability versions",
                            details={"capability": a})
    return (a, b) if a < b else (b, a)


def record_equivalence(
    conn: sqlite3.Connection,
    a: str,
    b: str,
    *,
    relation: str,
    basis_eval_ids: Sequence[str],
    conditions: str,
    actor: str = "backend",
) -> dict[str, Any]:
    """Write what an identity control measured about two versions. Backend only.

    There is no tool that reaches this function: a planner cannot pass an equivalence
    in, and v1's slice passed one it had typed. A record needs the evaluations it rests
    on and the conditions it was measured under, because "these two agree" measured on a
    74-nucleotide sequence says nothing about a 150-nucleotide one.
    """
    if relation not in EQUIVALENCE_RELATIONS:
        raise SchemaInvalid(f"relation {relation!r} is not one of {list(EQUIVALENCE_RELATIONS)}",
                            details={"relation": relation})
    if not basis_eval_ids:
        raise SchemaInvalid("an equivalence with no evaluation behind it is a statement, not a "
                            "measurement", details={})
    if not conditions.strip():
        raise SchemaInvalid("an equivalence names the conditions it was measured under", details={})
    marks = ",".join("?" for _ in basis_eval_ids)
    held = {row["eval_id"] for row in conn.execute(
        f"SELECT eval_id FROM evaluations WHERE eval_id IN ({marks})", tuple(basis_eval_ids))}
    absent = sorted(set(basis_eval_ids) - held)
    if absent:
        raise SchemaInvalid(
            f"basis evaluation(s) {absent} are not evaluations the backend ran",
            details={"absent": absent},
        )
    first, second = _ordered(a, b)
    now = clock.now_iso()
    with store.transaction(conn) as txn:
        txn.execute(
            "INSERT INTO method_equivalences(capability_a, capability_b, relation,"
            " basis_eval_ids_json, conditions, measured_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(capability_a, capability_b) DO UPDATE SET relation=excluded.relation,"
            " basis_eval_ids_json=excluded.basis_eval_ids_json, conditions=excluded.conditions,"
            " measured_at=excluded.measured_at",
            (first, second, relation, ids.canonical_json(list(basis_eval_ids)), conditions, now),
        )
        store.append_event(
            txn, kind="capability.equivalence_measured", actor=actor, subject_kind="capability",
            subject_id=first, new_state=relation, evidence_level="EXECUTION_OBSERVED",
            payload={"other": second, "basis_eval_ids": list(basis_eval_ids),
                     "conditions": conditions},
        )
    return equivalence(conn, a, b) or {}


def equivalence(conn: sqlite3.Connection, a: str, b: str) -> dict[str, Any] | None:
    """What the backend measured about ``a`` and ``b``, or None if it measured nothing."""
    first, second = _ordered(a, b)
    row = conn.execute(
        "SELECT * FROM method_equivalences WHERE capability_a=? AND capability_b=?",
        (first, second),
    ).fetchone()
    if row is None:
        return None
    return {
        "capability_a": row["capability_a"], "capability_b": row["capability_b"],
        "relation": row["relation"],
        "basis_eval_ids": store.loads(row, "basis_eval_ids_json", []) or [],
        "conditions": row["conditions"], "measured_at": row["measured_at"],
    }


def equivalences(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT capability_a, capability_b FROM method_equivalences"
        " ORDER BY capability_a, capability_b LIMIT ?", (limit,),
    ).fetchall()
    return [equivalence(conn, row["capability_a"], row["capability_b"]) or {} for row in rows]


# ------------------------------------------------------------------------- probe records

def record_probe(
    conn: sqlite3.Connection,
    capability_version: str,
    *,
    rung: str,
    result: str,
    detail: Mapping[str, Any],
    job_id: str | None = None,
) -> str:
    """One rung of the ladder as an observation; the derived columns are recomputed."""
    if rung not in contracts.label_values("probe_rung") and rung != ERROR_PROBE:
        raise SchemaInvalid(f"{rung!r} is not a probe rung", details={"rung": rung})
    if result not in contracts.gates()["check_result"]["values"]:
        raise SchemaInvalid(f"{result!r} is not a check result", details={"result": result})
    capability_id, version = manifest_mod.parse_capability_version(capability_version)
    probe_id = environments.new_probe_id()
    with store.transaction(conn) as txn:
        txn.execute(
            "INSERT INTO probe_records(probe_id, subject_kind, subject_id, rung, result,"
            " detail_json, job_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (probe_id, "capability", capability_version, rung, result,
             ids.canonical_json(dict(detail)), job_id, clock.now_iso()),
        )
        rung_reached, test_status = derived_status(txn, capability_version)
        txn.execute(
            "UPDATE capabilities SET probe_rung=?, test_status=? WHERE capability_id=? AND version=?",
            (rung_reached, test_status, capability_id, version),
        )
    return probe_id


def latest_probes(conn: sqlite3.Connection, capability_version: str) -> dict[str, dict[str, Any]]:
    """The most recent record of each rung."""
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT rowid, * FROM probe_records WHERE subject_kind='capability' AND subject_id=?"
        " ORDER BY created_at, rowid", (capability_version,),
    ).fetchall()
    for row in rows:
        out[row["rung"]] = {
            "probe_id": row["probe_id"], "rung": row["rung"], "result": row["result"],
            "job_id": row["job_id"], "created_at": row["created_at"],
            "detail": store.loads(row, "detail_json", {}) or {},
        }
    return out


#: What every record that says which rungs passed says of a capability whose manifest declares
#: no known-answer control: the gate does not require L4
#: then (gates.json ``required_when``), and nothing may read as if a known answer was checked.
L4_NOT_EXERCISED = "L4 not exercised: no known answer declared"
#: Asset states a capability reaches only through VALIDATED.
VALIDATED_OR_LATER = ("VALIDATED", "APPROVED", "ACTIVE", "DEPRECATED")


def known_answer_view(manifest: Mapping[str, Any] | None, status: str | None) -> dict[str, Any]:
    """Whether the manifest declares a known-answer control, and, when it does not, the
    sentence every summary of the rungs carries; ``validated_basis`` too once the version
    is VALIDATED or later, since its VALIDATED then rests on the smoke and error probes only."""
    control = (manifest or {}).get("known_answer")
    if control:
        return {"declared": True, "kind": control.get("kind"), "expected": control.get("expected")}
    view: dict[str, Any] = {"declared": False, "not_exercised": L4_NOT_EXERCISED}
    if status in VALIDATED_OR_LATER:
        view["validated_basis"] = (f"{L4_NOT_EXERCISED}: VALIDATED rests on the L3 smoke job and the "
                                  "malformed-input check only")
    return view


def _known_answer_of(conn: sqlite3.Connection, row: sqlite3.Row, status: str | None) -> dict[str, Any]:
    """:func:`known_answer_view` of a registered row, from its verified document. A document
    that cannot be read is said to be unreadable, never taken as declaring nothing."""
    try:
        bundle = load_bundle(conn, row["capability_id"], row["version"], expected_sha256=row["manifest_sha256"])
    except GxError as exc:
        return {"declared": None, "reason": f"the manifest could not be read: {exc.message}"}
    return known_answer_view(bundle["manifest"], status)


def derived_status(conn: sqlite3.Connection, capability_version: str) -> tuple[str | None, str]:
    """``(probe_rung, test_status)`` from the records. Neither is ever typed in.

    The rung reached is the highest one whose latest record is PASS with every rung below
    it also PASS. ``test_status`` follows the highest rung that has a job id behind it:
    a version that only answered ``--help`` is still STATIC_REVIEW_ONLY, because no job
    ran, and a version whose smoke job last failed says so.
    """
    latest = latest_probes(conn, capability_version)
    reached: str | None = None
    for rung in RUNGS:
        record = latest.get(rung)
        if record is None or record["result"] != "PASS":
            break
        reached = rung
    test_status = "STATIC_REVIEW_ONLY"
    smoke = latest.get("L3_SMOKE")
    if smoke is not None and smoke["job_id"]:
        test_status = "REAL_TOOL_SMOKE_TESTED" if smoke["result"] == "PASS" else "FAILED"
    return reached, contracts.require_label("test_status", test_status)


# ---------------------------------------------------------------------------- inspection

_INSPECT_FIELDS = ("capability_id", "version", "kind", "status", "env_id", "method_card",
                   "resources", "probe_rung", "test_status", "outputs", "limitations")
_FILTERS = ("status", "kind", "env_id", "probe_rung", "test_status", "family")


def inspect(
    conn: sqlite3.Connection,
    *,
    capability_id: str | None = None,
    filters: Mapping[str, Any] | None = None,
    include: Sequence[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """The ``gx_capability_inspect`` payload.

    ``status`` is the asset's. A capability with no asset row is listed as what it is — a
    candidate nothing can promote — rather than left out, because a missing method that
    looks like a method nobody registered is how gaps get lost.
    """
    max_rows = int(contracts.response_limits()["max_rows"])
    wanted = max(1, min(int(limit or 50), max_rows))
    unknown = sorted(set(filters or {}) - set(_FILTERS))
    if unknown:
        raise SchemaInvalid(f"unknown capability filter(s) {unknown}; allowed: {list(_FILTERS)}",
                            details={"unknown": unknown})
    extra = set(include or [])
    unknown_include = sorted(extra - {"entry", "weights", "source", "environment", "probes"})
    if unknown_include:
        raise SchemaInvalid(
            f"unknown include value(s) {unknown_include}; allowed: entry, weights, source, "
            "environment, probes", details={"unknown": unknown_include})

    where, params = [], []
    if capability_id:
        where.append("capability_id=?")
        params.append(capability_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM capabilities{clause} ORDER BY capability_id, created_at, version", params
    ).fetchall()

    out: list[dict[str, Any]] = []
    truncated = False
    for row in rows:
        asset = asset_row(conn, row["capability_id"], row["version"])
        status = asset["status"] if asset is not None else row["status"]
        card = store.loads(row, "method_card_json", {}) or {}
        record: dict[str, Any] = {
            "capability_id": row["capability_id"], "version": row["version"], "kind": row["kind"],
            "status": status, "env_id": row["env_id"], "method_card": card,
            "resources": store.loads(row, "resource_requirements_json", {}) or {},
            "probe_rung": row["probe_rung"], "test_status": row["test_status"],
            "outputs": store.loads(row, "outputs_json", []) or [],
            "limitations": row["limitations"], "manifest_sha256": row["manifest_sha256"],
            "has_asset_record": asset is not None,
            "known_answer": _known_answer_of(conn, row, status),
        }
        candidates = {**record, "family": card.get("family")}
        if any(candidates.get(key) != value for key, value in (filters or {}).items()):
            continue
        if "entry" in extra:
            record["entry"] = store.loads(row, "entry_json", {}) or {}
        if "weights" in extra:
            record["weights"] = store.loads(row, "weights_json", []) or []
        if "source" in extra:
            record["source"] = store.loads(row, "source_json", {}) or {}
        if "environment" in extra:
            try:
                record["environment"] = environments.readiness(environments.get(conn, row["env_id"]))
            except GxError as exc:
                record["environment"] = {"env_id": row["env_id"], "ready": False,
                                         "reasons": [exc.message]}
        if "probes" in extra:
            from . import ladder
            version_ref = f"{row['capability_id']}@{row['version']}"
            direct = latest_probes(conn, version_ref)
            record["probes"] = [
                {k: v for k, v in probe.items() if k != "detail"}
                for probe in direct.values()
            ]
            if ladder.L3 not in direct and asset is not None:
                try:
                    applicable = ladder.latest(conn, candidate(conn, version_ref))
                except GxError:
                    applicable = {}
                reused = {rung: probe["reused_from"] for rung, probe in applicable.items() if "reused_from" in probe}
                if reused:
                    record["reused_controls"] = reused
        if len(out) >= wanted:
            truncated = True
            break
        out.append(record)
    return {
        "capabilities": out, "equivalences": equivalences(conn), "truncated": truncated,
        "limit": wanted,
        "note": contracts.tool_spec("gx_capability_inspect").get("note"),
    }


__all__ = [
    "ACTIVE", "ASSET_KIND", "DIFFERENT_OUTPUT", "EQUIVALENCE_RELATIONS", "EQUIVALENT_OUTPUT",
    "ERROR_PROBE", "JOB_RUNGS", "RUNGS", "active_versions", "active_view", "asset_row", "candidate", "derived_status", "describe", "equivalence", "equivalences", "inspect",
    "latest_probes", "load_bundle", "measured", "method_card", "publish_script", "record_equivalence",
    "record_probe", "register_candidate", "resolve", "sync_status",
]
