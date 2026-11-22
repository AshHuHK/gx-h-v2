"""Install a release-pinned local method the first time a workflow uses it."""
import json
import sys

from .. import config
from ..context import Ctx
from ..domain import ids
from ..errors import ContentHashMismatch
from ..persistence import store
from . import environments, registry


GRID = "fixture-grid@1"
GRID_MANIFEST = "adapters/local/p01_grid/manifest.json"
QUALITY = "tl1a-coordinate-quality@1"
GEOMETRY = "tl1a-geometry@1"
MANIFESTS = {
    GRID: GRID_MANIFEST,
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


def ensure(ctx: Ctx, capability):
    conn = ctx.conn
    name, version = capability.rsplit("@", 1)
    present = registry.asset_row(conn, name, version)
    if present:
        if present["status"] == "ACTIVE":
            view = registry.candidate(conn, capability)
            if portable_digest(view["bundle"]) != pin(capability):
                raise ContentHashMismatch(f"Registered {capability} differs from the shipped method pin")
        # JobService applies its normal execution admission including existing lifecycle decisions.
        return capability

    bundle = json.loads((config.HARNESS_ROOT / MANIFESTS[capability]).read_text())
    env_id = bundle["manifest"]["env_id"]
    if not conn.execute("SELECT 1 FROM environments WHERE env_id=?", (env_id,)).fetchone():
        environments.register(conn, {"env_id": env_id, "kind": "SYSTEM_BINARY",
                                     "python_executable": sys.executable}, actor=ctx.actor)
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
    capability = view["capability_version"]
    if (capability not in MANIFESTS or view["status"] != "DRAFT"
            or not source.get("shipped_sha256")):
        return False
    return source["shipped_sha256"] == pin(capability) == portable_digest(view["bundle"])
