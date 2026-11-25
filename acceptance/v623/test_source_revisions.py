"""C1 exercises revision claims against the acquired coordinate bytes through both operators."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, draft
from acceptance.v622 import test_source_identity
from gx_core.p0 import v62_outputs


class RevisionIdentity(unittest.TestCase):
    command = test_source_identity.MaterialSource.command

    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.descriptors = PRODUCT / "examples/v621-coordinate-analysis"
        self.src = PRODUCT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
        self.other = PRODUCT / "acceptance/v61/adaptive/data/2O0O.cif"
        self.out = []

    def start(self, **spec):
        self.state = self.enterContext(Scenario(self.work / "study", spec_updates={
            "research_effort": "deep", "research_modules": [], "plugins": ["org.gx.protein@0.3.0"],
            "capabilities": ["deposited-chain-geometry@2"], **spec,
            "input_files": [{"path": str(self.src), "role": "reference"}, {"path": str(self.other), "role": "structure"}]}))
        self.plan = self.state.call(self.state.lead, "gx_decide_revision", {"decision": draft()}, "plan")["decision_ref"]

    def descriptor(self, entry):
        return json.loads((self.descriptors / f"{entry}.json").read_bytes())

    def identity(self, result):
        eid = result.get("acquisition_event_id", result["event_id"])
        row, = self.state.rows("SELECT payload_json FROM events WHERE event_id=?", eid)
        identity = json.loads(row["payload_json"])["acquisition"]["source_identity"]
        classified = v62_outputs.classify(self.state.conn, self.state.run_id, result["artifact_ref"])
        if "revision" in identity:
            self.assertEqual(classified.get("source_identity"), identity)
        else:
            self.assertNotIn("source_identity", classified)
        return identity

    def record(self, name):
        if target := os.getenv("GX623_SOURCE_RECEIPTS"):
            path = Path(target)
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{name}.json").write_text(json.dumps({
                "case": "C1", "name": name, "run_id": self.state.run_id,
                "operations": self.out, "observations": self.state.rows(
                    "SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v6.observation.added' ORDER BY event_id",
                    self.state.run_id), "paid_model_calls": 0}, indent=2) + "\n")

    def test_previous_revision_cannot_identify_current_bytes(self):
        self.start()
        descriptor = self.descriptor("2RE9") | {"source": "wwPDB:2RE9/revision/1.3"}
        results = []
        for cmd, ref in (("observe", self.state.materials[0]), ("assign-material", None)):
            code, _ = self.command(cmd, "archive-" + cmd, self.src, descriptor, existing_ref=ref)
            results.append(code)
        self.record("revision-conflict")
        self.assertEqual(results, [1, 1])
        self.assertTrue(all("revision" in x["error"].lower() for x in self.out))
        self.assertFalse(self.state.rows(
            "SELECT event_id FROM events WHERE run_id=? AND kind IN ('v6.observation.added','v621.material.assigned')",
            self.state.run_id))

    def test_both_archives_disclose_observed_and_declared_revision(self):
        self.start()
        for cmd, path, entry, version in (("observe", self.src, "2RE9", "1.4"), ("assign-material", self.other, "2O0O", "1.3")):
            code, result = self.command(cmd, "archive-" + entry, path, self.descriptor(entry))
            self.assertEqual(code, 0, self.out[-1]["error"])
            proof = self.identity(result)
            self.assertEqual(proof["entry_id"], entry)
            self.assertEqual(proof["artifact_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(proof["revision"], {"claimed": version, "observed": version, "status": "verified"})
        self.record("revision-matches")

    def test_insufficient_history_preserves_only_what_was_checked(self):
        self.start()
        path = self.work / "history-absent.cif"
        path.write_text("data_2RE9\n_entry.id 2RE9\n")
        code, result = self.command("observe", "local-record", path, {"source":"wwPDB:2RE9/revision/1.2"})
        self.assertEqual(code, 0, self.out[-1]["error"])
        proof = self.identity(result)
        self.assertEqual(proof["status"], "matched_accession")
        self.assertEqual(proof["revision"], {"claimed":"1.2", "observed":None, "status":"operator_attested"})
        code, result = self.command("assign-material", "accession-only", self.other, {"source":"wwPDB:2O0O"})
        self.assertEqual(code, 0, self.out[-1]["error"])
        self.assertEqual(self.identity(result)["revision"], {"claimed":None, "observed":"1.3", "status":"observed"})
        self.record("revision-attested")

    def test_coordinate_history_uses_content_type_and_ordinal(self):
        self.start()
        path = self.work / "mixed-history.cif"
        header = ("data_2RE9\n_entry.id 2RE9\nloop_\n"
                  "_pdbx_audit_revision_history.data_content_type\n"
                  "_pdbx_audit_revision_history.ordinal\n"
                  "_pdbx_audit_revision_history.major_revision\n"
                  "_pdbx_audit_revision_history.minor_revision\n")
        path.write_text(header + "'Structure model' 2 1 1\n'other' 8 9 9\n'Structure model' 1 1 0\n")
        code, result = self.command("observe", "coordinate-history", path, {"source": "wwPDB:2RE9/revision/1.1"})
        self.assertEqual(code, 0, self.out[-1]["error"])
        self.assertEqual(self.identity(result)["revision"]["status"], "verified")
        code, _ = self.command("observe", "coordinate-history", path, {
            "acquisition": "PDB:2RE9/revision/1.0", "source": "PDB:2RE9/revision/1.1"})
        self.assertEqual(code, 1)
        self.assertIn("revision", self.out[-1]["error"].lower())
        path.write_text(header + "'Structure model' 1 1 0\n'Structure model' 2 ? .\n")
        code, result = self.command("observe", "partial-history", path, {"source": "wwPDB:2RE9/revision/1.0"})
        self.assertEqual(code, 0, self.out[-1]["error"])
        self.assertEqual(self.identity(result)["revision"], {"claimed":"1.0", "observed":None, "status":"operator_attested"})
        code, _ = self.command("assign-material", "archive-with-fragment", self.src,
                               {"source": "wwPDB:2RE9/revision/1.0#coordinate-model"})
        self.assertEqual(code, 1)
        self.assertIn("revision", self.out[-1]["error"].lower())
        self.record("revision-archive-history")

    def test_historical_policy_retains_old_receipt(self):
        self.start(evidence_policy="gx.evidence/6.2.2", historical_replay=True)
        descriptor = self.descriptor("2RE9") | {"source":"wwPDB:2RE9/revision/1.3"}
        code, result = self.command("observe", "historical-archive", self.src, descriptor)
        self.assertEqual(code, 0, self.out[-1]["error"])
        self.assertEqual(self.identity(result), {"schema":"gx.source_identity/1", "reader":"gx.pdbx.source-identity/1",
                         "entry_id":"2RE9", "status":"matched_accession", "artifact_sha256":result["sha256"]})
        self.record("revision-historical")
