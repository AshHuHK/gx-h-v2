"""Install the synthetic CPU method bundled with the grid workflow on first use."""
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


def portable_digest(bundle):
    """Bind executable and parser bytes independently of the store-local artifact IDs."""
    parts = dict(bundle)
    for key in ("entry_script", "parser"):
        parts[key] = {k: bundle[key][k] for k in ("path", "sha256")}
    return ids.sha256_of_obj(parts)


def _grid_pin():
    return json.loads((config.CONFIG_DIR / "shipped.json").read_text()).get("capabilities", {}).get(GRID)


def ensure_grid(ctx: Ctx):
    conn = ctx.conn
    present = registry.asset_row(conn, "fixture-grid", "1")
    if present:
        if present["status"] == "ACTIVE":
            view = registry.candidate(conn, GRID)
            if portable_digest(view["bundle"]) != _grid_pin():
                raise ContentHashMismatch("Registered fixture-grid@1 differs from the shipped method pin")
        # JobService applies its normal execution admission including existing lifecycle decisions.
        return GRID

    bundle = json.loads((config.HARNESS_ROOT / GRID_MANIFEST).read_text())
    env_id = bundle["manifest"]["env_id"]
    if not conn.execute("SELECT 1 FROM environments WHERE env_id=?", (env_id,)).fetchone():
        environments.register(conn, {"env_id": env_id, "kind": "SYSTEM_BINARY",
                                     "python_executable": sys.executable}, actor=ctx.actor)
    with store.transaction(conn):
        for key in ("entry_script", "parser"):
            bundle[key] = registry.publish_script(ctx, bundle[key])
        digest = portable_digest(bundle)
        if digest != _grid_pin():
            raise ContentHashMismatch("Shipped fixture-grid manifest or code differs from its release pin")
        registry.register_candidate(ctx, bundle, shipped_digest=digest)
    return GRID


def admitted(view, source):
    if (view["capability_version"] != GRID or view["status"] != "DRAFT"
            or not source.get("shipped_sha256")):
        return False
    return source["shipped_sha256"] == _grid_pin() == portable_digest(view["bundle"])
