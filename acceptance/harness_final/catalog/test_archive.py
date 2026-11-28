"""A missing installation mount or rejected database never yields a scientific zero."""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness_final.catalog import archive_snapshots
from acceptance.harness_final.catalog.evaluate import unrestricted_opening
from acceptance.harness_refactor.catalog import test_open_discovery
from gx_core.capabilities import manifest


class ArchiveHealth(unittest.TestCase):
    admit = test_open_discovery.OpenDiscovery.admit

    def test_installation_without_mount_has_no_scientific_result(self):
        study, lead = self.admit("Which archived records support this annotation?")
        study.call(lead, "gx_query_catalog", {"kind":"all", **unrestricted_opening(study,lead)}, "opening")
        real_path = manifest.resolve_path
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        def disconnect(template, **kwargs):
            if "/var/science-assets/yeast-archive/" in template:
                return root/"absent"/Path(template).name
            return real_path(template, **kwargs)
        with patch.object(manifest, "resolve_path", side_effect=disconnect):
            card = study.call(lead, "gx_query_catalog", {"reference":"yeast-archive-lookup@2"}, "mount-unavailable")
        self.assertEqual(card["capability_facts"], {"installed":True, "dependency_ready":False,
                         "executed_in_run":False, "reference_only":False})
        self.assertEqual(card["status"], "selectable")
        self.assertEqual(study.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (study.run_id,)).fetchone()[0], 0)
        self.assertEqual({v["status"] for v in archive_snapshots.inspect_mount(root).values()}, {"missing"})

    def test_damaged_wrong_version_and_missing_rejected_by_exact_reader(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / archive_snapshots.declarations()["reactions"][0]
        path.parent.mkdir(parents=True)
        out = root/"out"
        out.mkdir()
        provider = archive_snapshots.PROVIDER
        for status in ("missing", "damaged", "unregistered_version"):
            if status == "damaged":
                path.write_bytes(b"damaged snapshot")
            if status == "unregistered_version":
                path.unlink()
                with closing(sqlite3.connect(path)) as db:
                    db.execute("CREATE TABLE release (version text)")
                    db.execute("INSERT INTO release VALUES ('fixture-other-version')")
                    db.commit()
            with self.subTest(status=status):
                check = archive_snapshots.inspect_mount(root)
                self.assertEqual(check["reactions"]["status"], status)
                error = subprocess.run(
                    [sys.executable, "-I", "-B", str(provider/"1.1.0/job-scripts/lookup.py"),
                     "--reader", str(provider/"1.0.0/archive.py"), "--mount-member", str(path),
                     "--taxid","559292", "--substrate","lactose", "--out",str(out)],
                    capture_output=True, text=True)
                self.assertNotEqual(error.returncode, 0)
                self.assertIn("FileNotFoundError" if status == "missing" else "registered archive version", error.stderr)
                self.assertEqual(list(out.iterdir()), [])

    def test_inspecting_archive_with_default_python_keeps_registered_package_bytes(self):
        # Run the actual source-site acceptance import in a private package copy.
        # Enabling ordinary Python cache writes would invalidate both installed providers.
        import shutil
        from gx_core.plugins.catalog import package_digest
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        relative = Path("gx-h/plugins/installed/org.gx.yeast-enzyme-archive/1.0.0")
        bundle = temp/relative
        shutil.copytree(archive_snapshots.PROVIDER/"1.0.0", bundle)
        relative_test = Path("acceptance/science_assets/enzyme/test_archive.py")
        reader_test = temp/relative_test
        reader_test.parent.mkdir(parents=True)
        shutil.copy2(archive_snapshots.HERE.parent/relative_test, reader_test)
        pin = package_digest(bundle)
        child = subprocess.run([sys.executable, "-E", "-c", "import runpy,sys; runpy.run_path(sys.argv[1])",
                                str(reader_test)], capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(package_digest(bundle), pin)
