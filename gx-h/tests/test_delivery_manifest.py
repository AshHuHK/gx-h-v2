"""GX integration with real SQLite, filesystem, and subprocess crashes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService, delivery
from gx_core.domain import ids
from gx_core.errors import ArtifactHashMismatch, ArtifactNotFound, SchemaInvalid
from gx_core.persistence import store
from tests.test_team_manager import ManagerFixture
from tests.test_native_facades import FacadeFixture


class DeliveryManifest(ManagerFixture):
    def setUp(self):
        super().setUp()
        self.art = ArtifactService(self.conn)
        self.task_id = self.task()
        self.inbox = self.art.inbox_dir(self.run_id)
        (self.inbox / "result.csv").write_text("x,y\n2,0\n")
        (self.inbox / "log.txt").write_text("completed\n")
        self.entries = [
            {"kind": "result", "media_type": "text/csv", "inbox_path": "result.csv"},
            {"kind": "log", "media_type": "text/plain", "inbox_path": "log.txt"},
        ]
        self.args = dict(run_id=self.run_id, task_id=self.task_id, attempt=1)
        self.provenance = {"method_version": "fixture/1"}

    def prepare(self):
        return delivery.prepare_manifest(self.art, **self.args, entries=self.entries, provenance=self.provenance)

    def test_freeze_rollback_register_reopen_and_cite(self):
        ref = self.prepare()
        self.assertEqual(ref, self.prepare())
        (self.inbox / "result.csv").write_text("changed")
        self.assertNotEqual(ref, self.prepare())
        prepared = delivery.load_manifest(self.art, ref, **self.args)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with store.transaction(self.conn):
                delivery.register_manifest(self.art, prepared, actor="test")
                raise RuntimeError("rollback")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(delivery.clean_orphans(self.art, min_age_seconds=0), [])
        with store.transaction(self.conn):
            refs = delivery.register_manifest(self.art, prepared, actor="test")
            self.assertEqual(delivery.register_manifest(self.art, prepared, actor="test"), refs)
        self.conn.close()
        self.conn = store.open_store(self.path)
        self.art = ArtifactService(self.conn)
        self.assertEqual(self.art.read_bytes(refs[0]), b"x,y\n2,0\n")
        self.assertEqual(self.art.read_bytes(refs[1]), b"completed\n")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='artifact.published'").fetchone()[0], 2)
        cite = delivery.prepare_manifest(self.art, **self.args, entries=[{"artifact_ref": refs[0]}], provenance={})
        citation = delivery.load_manifest(self.art, cite, **self.args)
        with store.transaction(self.conn):
            self.assertEqual(delivery.register_manifest(self.art, citation, actor="worker"), refs[:1])

    def test_scope_and_integrity_of_prepared_and_single_product(self):
        ref = self.prepare()
        with self.assertRaises(SchemaInvalid):
            delivery.load_manifest(self.art, ref, **dict(self.args, attempt=2))
        prepared = delivery.load_manifest(self.art, ref, **self.args)
        with store.transaction(self.conn):
            refs = delivery.register_manifest(self.art, prepared, actor="test")
        existing = delivery.load_manifest(self.art, refs[0], **self.args)
        self.assertEqual(existing.entries[0]["artifact_ref"], refs[0])
        with self.assertRaises(SchemaInvalid):
            delivery.load_manifest(self.art, refs[0], **dict(self.args, task_id=self.task()))
        Path(prepared.entries[0]["path"]).write_text("tampered")
        for identifier in (ref, refs[0]):
            with self.assertRaises(ArtifactHashMismatch):
                delivery.load_manifest(self.art, identifier, **self.args)

    def test_crash_during_blob_staging_gc_preserves_finished_directory(self):
        earlier = self.prepare()
        # Kill the writer after both full copies reached staging, before installation.
        code = """
import json, os, sys
from gx_core.artifacts import ArtifactService, delivery
from gx_core.persistence import store
db, run, task, entries = sys.argv[1:]
service = ArtifactService(store.open_store(db))
delivery.os.rename = lambda *args: os._exit(61)
delivery.prepare_manifest(service, run_id=run, task_id=task, attempt=1,
                          entries=json.loads(entries), provenance={"variant": "blob_crash"})
"""
        failed = subprocess.run([sys.executable, "-B", "-c", code, str(self.path), self.run_id,
                                 self.task_id, json.dumps(self.entries)], capture_output=True)
        self.assertEqual(failed.returncode, 61, failed.stderr)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(len(delivery.clean_orphans(self.art, min_age_seconds=0)), 1)
        self.assertEqual(delivery.clean_orphans(self.art, min_age_seconds=0), [])
        self.assertEqual(delivery.load_manifest(self.art, earlier, **self.args).manifest_ref, earlier)

    def test_discard_complete_orphan_only_after_attempt_end(self):
        ref = self.prepare()
        self.assertFalse(delivery.discard_orphan(self.art, **self.args, manifest_ref=ref))
        pending = delivery.load_manifest(self.art, ref, **self.args)
        (self.inbox / "result.csv").write_text("other original")
        registered = self.prepare()
        ready = delivery.load_manifest(self.art, registered, **self.args)
        with store.transaction(self.conn):
            refs = delivery.register_manifest(self.art, ready, actor="test")
            self.conn.execute("UPDATE tasks SET attempt=2 WHERE task_id=?", (self.task_id,))
        self.assertFalse(delivery.discard_orphan(self.art, **self.args, manifest_ref=registered))
        self.assertTrue(delivery.discard_orphan(self.art, **self.args, manifest_ref=ref))
        self.assertFalse(delivery.discard_orphan(self.art, **self.args, manifest_ref=ref))
        with self.assertRaises(ArtifactNotFound), store.transaction(self.conn):
            delivery.register_manifest(self.art, pending, actor="test")
        self.assertEqual(self.art.read_bytes(refs[0]), b"other original")

    def test_concurrent_publish_preserves_winning_original(self):
        barrier = threading.Barrier(2)
        original_hash = ids.sha256_of_file

        def synchronized_hash(path):
            digest = original_hash(path)
            barrier.wait(timeout=10)
            return digest

        def publish(value):
            connection = store.open_store(self.path)
            try:
                service = ArtifactService(connection)
                result = service.publish_bytes(namespace=self.run_id, rel_path="result.txt", data=value,
                                               kind="result", run_id=self.run_id, media_type="text/plain")
                return result.artifact_ref, value
            except SchemaInvalid:
                return None
            finally:
                connection.close()

        with patch.object(ids, "sha256_of_file", side_effect=synchronized_hash), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(publish, [b"left", b"right"]))
        winners = [item for item in results if item is not None]
        self.assertEqual(len(winners), 1)
        ref, content = winners[0]
        self.assertEqual(self.art.read_bytes(ref), content)

    def test_store_ancestor_flush_failure_precedes_registration(self):
        real_fsync = os.fsync
        ancestors = []
        def fail_run_directory(fd):
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if path.is_dir():
                ancestors.append(path)
                if path == self.art.root / self.run_id:
                    raise OSError("fixture: ancestor sync failed")
            real_fsync(fd)

        with patch("os.fsync", side_effect=fail_run_directory):
            with self.assertRaisesRegex(OSError, "ancestor sync failed"):
                self.prepare()
            with self.assertRaisesRegex(OSError, "ancestor sync failed"):
                self.art.publish_bytes(namespace=f"{self.run_id}/outputs", rel_path="raw/one",
                                       kind="result", data=b"bytes", run_id=self.run_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        self.assertIn(self.art.root / self.run_id / "deliveries", ancestors)
        # The directory rename may already have happened. Retry establishes its durability.
        ref = self.prepare()
        ready = delivery.load_manifest(self.art, ref, **self.args)
        with store.transaction(self.conn):
            refs = delivery.register_manifest(self.art, ready, actor="test")
        self.assertEqual(self.art.read_bytes(refs[0]), b"x,y\n2,0\n")

    def test_legacy_publisher_cannot_replace_prepared_manifest_via_run_namespace(self):
        ref = self.prepare()
        destination = f"deliveries/{self.task_id}/{ref[7:]}/manifest.json"
        with self.assertRaises(SchemaInvalid):
            self.art.publish_bytes(namespace=self.run_id, rel_path=destination, kind="result", data=b"other")
        reloaded = delivery.load_manifest(self.art, ref, **self.args)
        self.assertEqual(reloaded.provenance, self.provenance)

    def test_inbox_survives_outer_rollback_and_replacement_before_commit(self):
        def publish():
            return self.art.put(run_id=self.run_id, task_id=self.task_id, inbox_path="result.csv",
                                kind="result", media_type="text/csv", origin="HOST_PUT", put_by="test")

        with self.assertRaisesRegex(RuntimeError, "before outer commit"):
            with store.transaction(self.conn):
                publish()
                self.assertTrue((self.inbox / "result.csv").exists())
                raise RuntimeError("before outer commit")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        self.assertTrue((self.inbox / "result.csv").exists())
        with store.transaction(self.conn):
            original = publish()
            replacement = self.inbox / "new-output"
            replacement.write_bytes(b"next result")
            os.replace(replacement, self.inbox / "result.csv")
        self.assertEqual(self.art.read_bytes(original.artifact_ref), b"x,y\n2,0\n")
        self.assertEqual((self.inbox / "result.csv").read_bytes(), b"next result")
        with store.transaction(self.conn):
            current = publish()
        self.assertFalse((self.inbox / "result.csv").exists())
        self.assertEqual(self.art.read_bytes(current.artifact_ref), b"next result")

    def test_only_hashes_files_outside_a_writer_and_protects_active_staging(self):
        original_hash = ids.sha256_of_file
        counts = []
        def observe(path):
            counts.append(self.conn.in_transaction)
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(delivery.clean_orphans(self.art, min_age_seconds=0), [])
            return original_hash(path)
        with patch.object(ids, "sha256_of_file", side_effect=observe):
            ref = self.prepare()
            prepared = delivery.load_manifest(self.art, ref, **self.args)
            with store.transaction(self.conn):
                delivery.register_manifest(self.art, prepared, actor="test")
        self.assertGreaterEqual(len(counts), 4)


class HistoricalDelivery(FacadeFixture):
    def test_reopen_reference_only_delivery_after_finish_and_cleanup(self):
        from gx_core.teams.service import TeamService
        task_id, actor = self.assign()
        other, _ = self.assign("prices")
        claim = self.acquire(task_id, actor)
        args = dict(run_id=self.project.run_id, task_id=task_id, attempt=claim["attempt"])
        artifacts = ArtifactService(self.conn)
        plan = {"items": [{"key": "rows", "question": "How many rows?", "role": "executor"}]}
        raw = json.dumps(plan).encode()
        product = artifacts.publish_bytes(namespace=self.project.run_id, rel_path="shared/plan.json",
            kind="module_plan", run_id=self.project.run_id, task_id=other, media_type="application/json",
            data=raw, provenance={"method_version": "first-worker/1"})
        metadata = {"method_version": "second-worker/2", "input_refs": [product.artifact_ref]}
        ref = delivery.prepare_manifest(artifacts, **args, entries=[{"artifact_ref": product.artifact_ref}],
                                        provenance=metadata)
        request = {"task_id": task_id, "manifest_ref": ref}
        first = self.callback(actor, "gx_task_finish", request)
        self.assertTrue(first["ok"], first)
        receipt = first["data"]
        TeamService(self.conn).validate_delivery(submission_id=receipt["submission_id"])
        self.project.reconnect()
        self.router.conn = self.conn = self.project.conn
        artifacts = ArtifactService(self.conn)
        self.assertFalse(delivery.discard_orphan(artifacts, **args, manifest_ref=ref))
        again = self.callback(actor, "gx_task_finish", request)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["data"]["artifact_refs"], receipt["artifact_refs"])
        self.assertEqual(again["data"]["submission_id"], receipt["submission_id"])
        self.assertEqual(delivery.load_manifest(artifacts, ref, **args).provenance, metadata)
        source = delivery.load_manifest(artifacts, product.artifact_ref, **dict(args, task_id=other))
        self.assertEqual(source.provenance, {"method_version": "first-worker/1"})
        self.assertEqual(artifacts.read_bytes(product.artifact_ref), raw)


if __name__ == "__main__":
    unittest.main()
