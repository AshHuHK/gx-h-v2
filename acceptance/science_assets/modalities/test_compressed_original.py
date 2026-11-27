"""Archive identity is read from decompressed CIF text while the receipt pins the supplied archive."""
from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from adapters.scientific import archive_coordinates
from gx_core.errors import SchemaInvalid


ARCHIVE = Path(__file__).parent / "fab/materials/4fab.cif.gz"


def identify(raw, entry="4FAB"):
    digest = sha256(raw).hexdigest()
    labels = [f"https://files.rcsb.org/download/{entry}.cif.gz#sha256={digest}", f"wwPDB:{entry}/revision/2.1"]
    return archive_coordinates.verify("application/gzip", labels, digest, lambda: raw, [], [], None,
                                      verify_revision=True, prove_endpoints=True)


class CompressedOriginal(unittest.TestCase):
    def test_supplied_archive_pin_and_internal_revision(self):
        result = identify(ARCHIVE.read_bytes())
        self.assertEqual(result["entry_id"], "4FAB")
        self.assertEqual(result["status"], "matched_accession")
        self.assertEqual(result["revision"], {"status":"verified","claimed":"2.1","observed":"2.1"})

    def test_damaged_envelope_and_mismatched_accession_are_rejected(self):
        raw = ARCHIVE.read_bytes()
        with self.assertRaisesRegex(SchemaInvalid, "Invalid gzip"):
            identify(raw[:-12])
        with self.assertRaisesRegex(SchemaInvalid, "accession differs"):
            identify(raw, "1MCP")

    def test_expansion_uses_existing_input_size_limit(self):
        with patch("gx_core.orchestration.service.MAX_COPIED_INPUT_BYTES", 512):
            with self.assertRaisesRegex(SchemaInvalid, "size bound"):
                identify(ARCHIVE.read_bytes())
