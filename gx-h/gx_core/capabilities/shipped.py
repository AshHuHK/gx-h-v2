"""Install a release-pinned local method the first time a workflow uses it."""
import hashlib
import json
import os
from pathlib import PurePosixPath
import sys

from .. import config
from ..context import Ctx
from ..domain import ids
from ..errors import ContentHashMismatch, SchemaInvalid, VersionImmutable
from ..persistence import store
from . import environments, manifest, registry


ARTIFACT_ANALYSIS = "artifact-analysis@1"
GRID = "fixture-grid@1"
GRID_MANIFEST = "adapters/local/p01_grid/manifest.json"
REVIEW = "tl1a-review@1"
QUALITY = "tl1a-coordinate-quality@1"
GEOMETRY = "tl1a-geometry@1"
MANIFESTS = {
    ARTIFACT_ANALYSIS: "adapters/local/artifact_analysis/manifest.json",
    GRID: GRID_MANIFEST,
    REVIEW: "adapters/scientific/tl1a_geometry/review_batch.json",
    QUALITY: "adapters/scientific/tl1a_geometry/quality.json",
    GEOMETRY: "adapters/scientific/tl1a_geometry/geometry.json",
}


def portable_digest(bundle):
    """Bind executable and parser bytes independently of the store-local artifact IDs."""
    parts = dict(bundle)
    for key in ("entry_script", "parser"):
        if bundle.get(key):
            parts[key] = {k: bundle[key][k] for k in ("path", "sha256")}
    return ids.sha256_of_obj(parts)


def pin(capability):
    return json.loads((config.CONFIG_DIR / "shipped.json").read_text()).get("capabilities", {}).get(capability)


def _ensure_environment(ctx, env_id):
    if env_id == "control-plane-python" and not ctx.conn.execute(
            "SELECT 1 FROM environments WHERE env_id=?", (env_id,)).fetchone():
        environments.register(ctx.conn, {"env_id": env_id, "kind": "SYSTEM_BINARY",
                                         "python_executable": os.path.abspath(sys.executable)}, actor=ctx.actor)
    else:
        environments.get(ctx.conn, env_id)


def ensure(ctx: Ctx, capability):
    conn = ctx.conn
    name, version = manifest.parse_capability_version(capability)
    present = registry.asset_row(conn, name, version)
    if present:
        if present["status"] == "ACTIVE":
            expected = pin(capability)
            if expected is not None or capability in MANIFESTS:
                view = registry.candidate(conn, capability)
                if portable_digest(view["bundle"]) != expected:
                    raise ContentHashMismatch(f"Registered {capability} differs from the shipped method pin")
        # JobService applies its normal execution admission including existing lifecycle decisions.
        return capability

    if capability not in MANIFESTS:
        return registry.candidate(conn, capability)["capability_version"]
    bundle = json.loads((config.HARNESS_ROOT / MANIFESTS[capability]).read_text())
    _ensure_environment(ctx, bundle["manifest"]["env_id"])
    with store.transaction(conn):
        for key in ("entry_script", "parser"):
            if bundle.get(key):
                bundle[key] = registry.publish_script(ctx, bundle[key])
        digest = portable_digest(bundle)
        if digest != pin(capability):
            raise ContentHashMismatch(f"Shipped {capability} manifest or code differs from its release pin")
        registry.register_candidate(ctx, bundle, shipped_digest=digest)
    return capability


def ensure_grid(ctx: Ctx):
    return ensure(ctx, GRID)


def admitted(view, source):
    if view["status"] != "DRAFT" or source.get("admission") != "SHIPPED" or not source.get("shipped_sha256"):
        return False
    return source["shipped_sha256"] == pin(view["capability_version"]) == portable_digest(view["bundle"])


def _package_bundle(package, reference):
    """Use the same on-disk bundle shape as shipped methods, with package-relative script names."""
    def member(path):
        if not isinstance(path, str) or not path:
            raise SchemaInvalid("A package member needs a relative path")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != path:
            raise SchemaInvalid(f"Invalid package-relative member {path!r}")
        try:
            return package.read_file(path)
        except KeyError as exc:
            raise SchemaInvalid(f"Package {package.pin.id}@{package.pin.version} has no member {path!r}") from exc

    bundle = json.loads(member(reference))
    method = bundle.get("manifest") if isinstance(bundle, dict) else None
    if not isinstance(method, dict) or not all(isinstance(method.get(k), str) for k in ("capability_id", "version", "env_id")):
        raise SchemaInvalid("A capability bundle needs manifest.capability_id, version and env_id")
    for key in ("entry_script", "parser"):
        relative = bundle.get(key)
        if relative is not None:
            content = member(relative)
            path = f"plugins/installed/{package.pin.id}/{package.pin.version}/{relative}"
            bundle[key] = {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
    if not isinstance(method.get("method_card"), dict):
        raise SchemaInvalid("A package capability requires a method card")
    if found := manifest.domain_problems(method):
        raise SchemaInvalid("; ".join(found))
    return bundle


def attach(ctx: Ctx, reference: str, *, package=None) -> dict:
    """Attach a verified package document or an existing capability to a Run's composition.

    A receipt identifies the portable method and its canonical document in this workspace.
    Governance and JobService determine whether that method can execute.
    """
    if not isinstance(reference, str) or not reference:
        raise SchemaInvalid("A capability reference needs an id@version or a package-relative manifest path")
    if "@" in reference and "/" not in reference:
        capability = ensure(ctx, reference)
        name, version = manifest.parse_capability_version(capability)
        asset = registry.asset_row(ctx.conn, name, version)
        content = registry.load_bundle(ctx.conn, name, version, expected_sha256=asset["content_sha256"])
    else:
        if package is None:
            raise SchemaInvalid("A package-relative capability reference needs its verified plugin")
        bundle = _package_bundle(package, reference)
        method = bundle["manifest"]
        capability = manifest.capability_version(method)
        name, version = manifest.parse_capability_version(capability)
        asset = registry.asset_row(ctx.conn, name, version)
        expected = pin(capability)
        if asset:
            content = registry.load_bundle(ctx.conn, name, version, expected_sha256=asset["content_sha256"])
            for key in ("entry_script", "parser"):
                if bundle.get(key) is not None:
                    held = content.get(key)
                    if held is None or any(held[k] != v for k, v in bundle[key].items()):
                        raise VersionImmutable(f"Package {key} differs from the registered {capability}")
                    bundle[key] = held
            if manifest.require_valid(bundle) != content:
                raise VersionImmutable(f"Package manifest differs from the registered {capability}")
        else:
            if ctx.conn.in_transaction:
                raise RuntimeError("Attach a new capability before opening the Run transaction")
            _ensure_environment(ctx, method["env_id"])
            with store.transaction(ctx.conn):
                for key in ("entry_script", "parser"):
                    if script := bundle.get(key):
                        bundle[key] = registry.publish_script(ctx, script["path"], expected_sha256=script["sha256"])
                content = manifest.require_valid(bundle)
                portable = portable_digest(content)
                if expected is not None and portable != expected:
                    raise ContentHashMismatch(f"Package {capability} differs from the shipped method pin")
                registry.register_candidate(ctx, content, shipped_digest=expected, installed_package={
                    "id": package.pin.id, "version": package.pin.version, "sha256": package.pin.digest,
                    "portable_sha256": portable})
                asset = registry.asset_row(ctx.conn, name, version)
    portable = portable_digest(content)
    expected = pin(capability)
    if expected is not None and portable != expected:
        raise ContentHashMismatch(f"Registered {capability} differs from the shipped method pin")
    return {"capability_version": capability, "portable_sha256": portable,
            "manifest_sha256": asset["content_sha256"], "content_artifact_ref": asset["content_artifact_ref"]}


def participates(conn, run_id, capability):
    """A package-owned method is available to Runs holding its exact workspace receipt."""
    row = conn.execute(
        "SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if not row:
        return False
    from ..plugins.runtime import recorded
    binding = recorded(row)
    data = json.loads(row["inputs_json"] or "{}")
    inputs = data.get("inputs", {})
    recorded_goal = binding is not None and binding.get("kind") == "goal"
    catalog = inputs.get("capability_catalog") if recorded_goal else None
    if binding is None and inputs.get("p0") is True and "capability_catalog" in inputs:
        if conn.execute("SELECT 1 FROM v6_goals WHERE run_id=?", (run_id,)).fetchone():
            catalog = inputs["capability_catalog"]
    # Goal grants include the exact workspace manifest and artifact. Historical
    # Goals saved this catalog before compositions became a first-class receipt.
    # A later Goal selection owns its own immutable method and plugin receipt.
    from ..p0.capability_views import latest
    revision = latest(conn, run_id)
    if revision is not None:
        catalog = revision["catalog"]
    grant = None
    if catalog is not None:
        grant = next((entry for entry in catalog if entry["capability_version"] == capability), None)
        if grant is None:
            return False
    if capability in MANIFESTS and grant is None:
        return True
    name, version = manifest.parse_capability_version(capability)
    asset = conn.execute(
        "SELECT content_sha256,content_artifact_ref,source_json FROM assets"
        " WHERE asset_id=? AND version=? AND kind='TOOL_ADAPTER'", (name, version)).fetchone()
    if not asset:
        return False
    pkg = json.loads(asset["source_json"] or "{}").get("installed_package")
    if grant is not None:
        if (grant.get("content_artifact_ref") != asset["content_artifact_ref"] or
                grant.get("manifest_sha256") != asset["content_sha256"]):
            return False
        if recorded_goal and revision is None:
            fields = ("capability_version", "portable_sha256", "manifest_sha256", "content_artifact_ref")
            receipt = {field: grant.get(field) for field in fields}
            if receipt not in binding["capabilities"] or (grant.get("plugin") and grant["plugin"] not in binding["packages"]):
                return False
        if pkg:
            owner = {key: pkg.get(key) for key in ("id", "version", "sha256")}
            if grant.get("plugin") != owner or pkg.get("portable_sha256") != grant.get("portable_sha256"):
                return False
        return True
    if not pkg:
        return True
    receipt = {"capability_version": capability, "portable_sha256": pkg["portable_sha256"],
               "manifest_sha256": asset["content_sha256"], "content_artifact_ref": asset["content_artifact_ref"]}
    return binding is not None and receipt in binding["capabilities"]
