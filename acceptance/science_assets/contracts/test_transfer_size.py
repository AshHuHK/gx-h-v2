"""Use a real above-bound publication to exercise streaming, rollback and cold storage."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.orchestration import service
from gx_core.persistence import store


class TransferSize(unittest.TestCase):
    def test_over_eight_mib_and_failed_admission(self):
        work = Path(self.enterContext(TemporaryDirectory()))
        with CloseStudy(work) as s:
            assets = ArtifactService(s.conn)
            namespace = f"{s.run_id}/inputs"
            directory = assets.staging_dir(namespace)
            source = directory / "source.bin"
            block = b"instrument trace\n" * 65536
            with source.open("wb") as output:
                while output.tell() <= service.MAX_COPIED_INPUT_BYTES:
                    output.write(block)
            pub = assets.publish(namespace=namespace, rel_path="prior/trace.bin", kind="run_input",
                                 staged_path=source, run_id=s.run_id, media_type="application/octet-stream")
            target = work / "frozen.bin"
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded Path.read_bytes")):
                with target.open("wb") as stream:
                    assets.copy_verified(pub.artifact_ref, stream, expected_sha256=pub.sha256)
                item = {"path": str(target), "sha256": pub.sha256, "bytes": pub.bytes, "role": "previous_trace"}
                selection = {"selected_materials": [{"parent_artifact_ref": pub.artifact_ref}]}
                with ExitStack() as pending:
                    stages = service._prepare_transfers(s.conn, [item], selection, pending)
                    self.assertFalse(s.conn.in_transaction)
                    target.write_text("Changed external file after the private copy was verified.")
                    options = dict(run_id=s.run_id, media_type=pub.media_type,
                                   provenance={"role": item["role"]}, actor="test")
                    path = "child/trace.bin"
                    with self.assertRaisesRegex(RuntimeError, "rollback"):
                        with store.transaction(s.conn):
                            failed = service._install_transfer(assets, stages[0], item, namespace, path, **options)
                            raise RuntimeError("rollback")
                    self.assertFalse(assets.resolve_path(namespace, path).exists())
                    self.assertIsNone(s.conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref=?", (failed,)).fetchone())
                    with store.transaction(s.conn):
                        copied = service._install_transfer(assets, stages[0], item, namespace, path, **options)
            self.assertNotEqual(copied, pub.artifact_ref)
            self.assertGreater(assets.describe(copied)["bytes"], 8 * 1024 * 1024)
            self.assertEqual(ids.sha256_of_file(assets.path_of(copied)), pub.sha256)
            self.assertEqual(ids.sha256_of_file(assets.path_of(pub.artifact_ref)), pub.sha256)
