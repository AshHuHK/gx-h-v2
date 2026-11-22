"""A pinned builtin has a separate admission basis and preserves earlier asset decisions."""
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest

from gx_core.capabilities import environments, registry, shipped
from gx_core.context import Ctx, Principal
from gx_core.errors import CapabilityNotApproved
from gx_core.persistence import store


class ShippedGrid(unittest.TestCase):
    def context(self, conn):
        return Ctx(conn=conn, principal=Principal(subject="test", scope="operator"), tool="test")

    def test_existing_draft_is_not_promoted_by_using_workflow(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as conn:
            ctx = self.context(conn)
            bundle = json.loads((shipped.config.HARNESS_ROOT / shipped.GRID_MANIFEST).read_text())
            environments.register(conn, {"env_id": bundle["manifest"]["env_id"], "kind": "SYSTEM_BINARY",
                                         "python_executable": sys.executable}, actor=ctx.actor)
            for key in ("parser", "entry_script"):
                bundle[key] = registry.publish_script(ctx, bundle[key])
            registry.register_candidate(ctx, bundle)
            shipped.ensure_grid(ctx)
            self.assertEqual(registry.asset_row(conn, "fixture-grid", "1")["status"], "DRAFT")
            with self.assertRaises(CapabilityNotApproved):
                registry.resolve(conn, shipped.GRID)

    def test_admission_retains_real_environment_and_recovers_without_reinstall(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as conn:
            ctx = self.context(conn)
            shipped.ensure_grid(ctx)
            self.assertEqual(registry.resolve(conn, shipped.GRID)["admission"], "SHIPPED")
            self.assertTrue(registry.resolve(conn, shipped.GRID)["environment"]["ready"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            before = conn.total_changes
            shipped.ensure_grid(ctx)
            self.assertEqual(conn.total_changes, before)
