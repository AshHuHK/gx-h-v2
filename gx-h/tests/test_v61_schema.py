"""Released P0 SQL and the V6.1 contract must agree at SQLite's object level."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from gx_core import contracts
from gx_core.errors import StoreUnavailable
from gx_core.native_release import ReleaseStateError, native_state
from gx_core.p0 import schema
from gx_core.persistence import store


class PromotedSchema(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "gx.db"
        self.db = self.enterContext(closing(store.open_store(self.path)))
        sources = json.loads((contracts.CONFIG_DIR / "native_contract_migrations.json").read_text())
        self.v6 = next(r for r in sources["additive_schema_sources"]
                       if r["contract_sha256"].startswith("afe0646b"))

    def mark_old(self):
        self.db.executemany("UPDATE schema_meta SET value=? WHERE key=?", [
            (self.v6["db_schema_sha256"], "db_schema_sha256"),
            (self.v6["contract_sha256"], "contract_sha256"),
        ])

    def test_all_sql_objects_and_immutable_original_receipts(self):
        rows = list(self.db.execute("SELECT type,name FROM sqlite_schema "
                                    "WHERE name GLOB 'v6_*' OR name='p0_v6_native_calls'"))
        self.assertIn(("table", "v6_saved_recipes"), [tuple(r) for r in rows])
        self.assertEqual(sorted([r[0] for r in rows]),
                         sorted(["table"] * 10 + ["index"] * 8 + ["trigger"] * 4))
        for key, digest, _ in schema.migrations():
            self.assertEqual(store.read_meta(self.db)[key], digest)
        schema.verify_promoted(self.db, require_all=True)
        self.assertEqual(list(self.db.execute("PRAGMA foreign_key_check")), [])

    def test_incompatible_trigger_or_missing_recorded_table_refuses_without_restamp(self):
        self.mark_old()
        for name, change in (
            ("v6_graph_no_update", "CREATE TRIGGER v6_graph_no_update BEFORE UPDATE ON "
             "v6_graph_versions BEGIN SELECT 1; END"),
            ("v6_node_waits", None),
        ):
            with self.subTest(name=name):
                sql = self.db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()[0]
                self.db.execute(f"DROP {'TRIGGER' if change else 'TABLE'} {name}")
                if change:
                    self.db.execute(change)
                with self.assertRaisesRegex(StoreUnavailable, name):
                    store.ensure_schema(self.db)
                self.assertEqual(store.read_meta(self.db)["contract_sha256"], self.v6["contract_sha256"])
                if change:
                    self.db.execute(f"DROP TRIGGER {name}")
                self.db.execute(sql)
        self.assertEqual(store.ensure_schema(self.db), "resynced")

    def test_case_insensitive_sqlite_collision_before_original_receipt(self):
        self.mark_old()
        self.db.execute("DELETE FROM schema_meta WHERE key='p0.migration.v6_funding.sql'")
        self.db.execute("DROP INDEX v6_funding_event_cursor")
        self.db.execute("CREATE INDEX V6_FUNDING_EVENT_CURSOR ON events(kind)")
        with self.assertRaisesRegex(StoreUnavailable, "v6_funding_event_cursor"):
            store.ensure_schema(self.db)
        self.assertEqual(store.read_meta(self.db)["contract_sha256"], self.v6["contract_sha256"])

    def test_resync_crash_rolls_back_new_objects_and_retry_acknowledges_old_bytes(self):
        self.mark_old()
        key = "p0.migration.v6_funding.sql"
        self.db.execute("DELETE FROM schema_meta WHERE key=?", (key,))
        self.db.execute("DROP INDEX v6_funding_event_cursor")
        with patch.object(store, "_stamp", side_effect=RuntimeError("interrupt after object installation")):
            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                store.ensure_schema(self.db)
        self.assertIsNone(self.db.execute(
            "SELECT name FROM sqlite_schema WHERE name='v6_funding_event_cursor'").fetchone())
        self.assertEqual(store.read_meta(self.db)["contract_sha256"], self.v6["contract_sha256"])

        # Simulate an exit between the contract commit and the separate P0 ledger transaction.
        self.assertEqual(store.ensure_schema(self.db), "resynced")
        self.assertNotIn(key, store.read_meta(self.db))
        with closing(store.open_store(self.path)) as reopened:
            self.assertIn(key, store.read_meta(reopened))
            self.assertEqual(store.ensure_schema(reopened), "current")
            self.assertEqual(reopened.execute(
                "SELECT COUNT(*) FROM events WHERE kind='store.contract_resynced'").fetchone()[0], 1)
            schema.verify_promoted(reopened, require_all=True)

    def test_operator_only_partial_p0_upgrade_is_atomic_and_preserves_original_store_stamp(self):
        self.mark_old()
        key = "p0.migration.v6_job_indexes.sql"
        indexes = [r for r in schema._promoted_objects().items() if r[1][0] == key]
        self.assertGreater(len(indexes), 1)
        self.db.execute("DELETE FROM schema_meta WHERE key=?", (key,))
        missing, collision = (entry[0] for entry in indexes[:2])
        self.db.execute(f"DROP INDEX {missing}")
        self.db.execute(f"DROP INDEX {collision}")
        self.db.execute(f"CREATE INDEX {collision} ON jobs(job_id)")
        with self.assertRaisesRegex(StoreUnavailable, collision):
            schema.install(self.db)
        self.assertNotIn(key, store.read_meta(self.db))
        self.assertFalse(self.db.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (missing,)).fetchone())
        self.db.execute(f"DROP INDEX {collision}")
        schema.install(self.db)
        schema.install(self.db)
        schema.verify_promoted(self.db, require_all=True)
        self.assertEqual(store.read_meta(self.db)["contract_sha256"], self.v6["contract_sha256"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='store.contract_resynced'").fetchone()[0], 0)

    def test_native_preflight_requires_known_old_pair_and_current_version(self):
        directory = Path(self.temp) / "owned"
        with native_state(directory, env={}) as state, closing(store.open_store(state.db_path)) as conn:
            conn.execute("UPDATE schema_meta SET value=? WHERE key='db_schema_sha256'",
                         (self.v6["db_schema_sha256"],))
        with self.assertRaises(ReleaseStateError):
            with native_state(directory, env={}):
                self.fail("mixed metadata was accepted")
        with closing(sqlite3.connect(directory / "gx.db")) as conn:
            conn.execute("UPDATE schema_meta SET value=? WHERE key='contract_sha256'",
                         (self.v6["contract_sha256"],))
            conn.commit()
        with native_state(directory, env={}) as state, closing(store.open_store(state.db_path)) as conn:
            self.assertEqual(store.read_meta(conn)["contract_sha256"], contracts.contract_digest()["__combined__"])
