"""Check original and derived materials through both public operator commands."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, draft
from acceptance.v62c import materials


class MaterialSource(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.src = materials.PARENT
        self.other = PRODUCT / "acceptance/v61/adaptive/data/2O0O.cif"
        self.descriptors = PRODUCT / "examples/v621-coordinate-analysis"
        self.out = []
        self.state = self.enterContext(Scenario(self.work / "study", spec_updates={
            "research_effort": "deep", "research_modules": [], "plugins": ["org.gx.protein@0.3.0"],
            "capabilities": ["deposited-chain-geometry@2"],
            "input_files": [{"path": str(self.src), "role": "reference"}, {"path": str(self.other), "role": "structure"}]}))
        self.plan = self.state.call(self.state.lead, "gx_decide_revision", {"decision": draft()}, "plan")["decision_ref"]

    def command(self, cmd, source, path, metadata, *, existing_ref=None, digest=None, media="chemical/x-mmcif"):
        digest = digest or hashlib.sha256(path.read_bytes()).hexdigest()
        meta = self.work / "descriptor.json"
        meta.write_text(json.dumps(metadata))
        args = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), cmd, "--state", str(self.state.state),
                "--run-id", self.state.run_id, "--source", source, "--acquisition", str(meta), "--sha256", digest]
        if existing_ref:
            args += ["--artifact-ref", existing_ref]
        else:
            args += ["--input", str(path), "--media-type", media]
        if cmd == "assign-material":
            args += ["--after-decision", self.plan, "--role", "structure"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=25, env={**os.environ, "GX_CODEX_OFFLINE":"1"})
        result = json.loads(proc.stdout) if proc.returncode == 0 else {}
        self.out.append(dict(command=cmd, source=source, expected_sha256=digest,
                             descriptor=metadata, exit_code=proc.returncode, result=result,
                             error=proc.stderr.strip()))
        return proc.returncode, result

    def record(self, case):
        if d := os.getenv("GX622_SOURCE_RECEIPTS"):
            path = Path(d)
            path.mkdir(parents=True, exist_ok=True)
            (path / (case+".json")).write_text(json.dumps(dict(case="R5", run_id=self.state.run_id,
                registered_decision=self.plan, operations=self.out, observations=self.state.rows(
                  "SELECT event_id,payload_json FROM events WHERE kind='v6.observation.added' AND run_id=? ORDER BY event_id",
                   self.state.run_id), real_cpu_jobs=0, paid_model_calls=0), indent=2)+"\n")

    def test_wrong_accession_refused_at_both_public_operators(self):
        wrong = json.loads((self.descriptors / "2O0O.json").read_bytes())
        # The digest is correct; the conflict is now solely the archive accession.
        wrong["data"] = "https://files.rcsb.org/download/2O0O.cif#sha256=" + materials.PARENT_SHA
        exits = []
        for cmd, ref in (("observe",self.state.materials[0]), ("assign-material",None)):
            code, _ = self.command(cmd, "opaque-"+cmd, self.src, wrong, existing_ref=ref)
            exits.append(code)
        self.record("wrong-accession")
        self.assertEqual(exits,[1,1])
        self.assertEqual(self.state.rows("SELECT event_id FROM events WHERE kind IN ('v6.observation.added','v621.material.assigned')"), [])
        self.assertTrue(all("accession" in item["error"].lower() for item in self.out))

    def test_originals_descendants_and_operator_attestation(self):
        def succeeded(cmd, source, path, descriptor, **kw):
            rc, outcome = self.command(cmd, source, path, descriptor, **kw)
            self.assertEqual(rc, 0, self.out[-1]["error"])
            eid = outcome.get("acquisition_event_id", outcome["event_id"])
            row, = self.state.rows("SELECT payload_json FROM events WHERE event_id=?", eid)
            acquisition = json.loads(row["payload_json"])["acquisition"]
            return outcome, acquisition

        p, proof = succeeded("observe", "archive", self.src, materials.parent_acquisition(),
                             existing_ref=self.state.materials[0])
        self.assertEqual(proof["source_identity"]["entry_id"], "2RE9")
        other, _ = succeeded("observe", "other-archive", self.other,
                             json.loads((self.descriptors / "2O0O.json").read_bytes()),
                             existing_ref=self.state.materials[1])
        parent = {k:p[k] for k in ("artifact_ref","sha256")}
        oracle = materials.prepare(self.work/"custody")
        for code in ("P4","K7"):
            slot = self.work / "custody" / code
            target = slot / "specimen.cif"
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            descriptor = {**materials.parent_acquisition(), "derived_from":[parent],
                          "processing":["coordinate-record withholding/"+oracle[code]["transform_sha256"]],
                          "transformation":json.loads((slot/"transform.json").read_bytes()), "sha256":digest}
            receipt, acq = succeeded("assign-material","assigned-"+code,target,descriptor)
            replay, _ = succeeded("assign-material","assigned-"+code,target,descriptor)
            self.assertEqual(replay, receipt)
            ident = acq["source_identity"]
            self.assertEqual((ident["entry_id"],ident["status"],ident["parent_sha256"]),
                             ("2RE9","verified_derivation",materials.PARENT_SHA))
            self.assertEqual(ident["transformation"]["descriptor_sha256"], oracle[code]["transform_sha256"])
            self.assertEqual(ident["transformation"]["result_sha256"],digest)
            self.assertEqual(acq["derived_from"],[parent])
            if code == "K7":
                # Reuse the other legitimate child, its transform, or another real parent.
                p4 = self.work/"custody/P4"
                sliced = target.read_bytes().replace(b"ATOM ", b"ATOM  ", 1)
                altered = self.work/"altered.cif"
                altered.write_bytes(sliced)
                for label,path,changes in (
                        ("wrong-parent",target,{"derived_from":[{k:other[k] for k in ("artifact_ref","sha256")}]}),
                        ("wrong-rule",target,{"transformation":json.loads((p4/"transform.json").read_bytes()),
                             "processing":["coordinate-record withholding/"+oracle["P4"]["transform_sha256"]]}),
                        ("wrong-child",p4/"specimen.cif",{"sha256":oracle["P4"]["sha256"]}),
                        ("changed-bytes",altered,{"sha256":hashlib.sha256(sliced).hexdigest()}),
                        ("unverified-processing",target,{"transformation":None})):
                    rc, _ = self.command("assign-material",label,path,descriptor|changes)
                    self.assertNotEqual(rc,0,label)
        sensor = self.work/"instrument.json"
        sensor.write_text('{"schema":"public.sensor/1","reading":3}\n')
        _, acq = succeeded("observe","sensor",sensor,{"source":"operator:thermometer", "applicability":"recorded interval"},
                           media="application/json")
        self.assertEqual(acq["source_identity"]["status"],"operator_attested")
        self.assertNotIn("entry_id",acq["source_identity"])
        local_cif = self.work/"unpublished.cif"
        local_cif.write_text("data_local\n_entry.id project-model-A\n")
        _, local_acq = succeeded("observe","2025",local_cif,{"source":"operator:local-model"})
        self.assertEqual(local_acq["source_identity"]["status"], "operator_attested")
        self.assertEqual(self.state.rows("SELECT COUNT(*) AS n FROM events WHERE kind='v621.material.assigned'")[0]["n"],2)
        self.record("verified-assignments")

    def test_hash_and_accession_fields_are_checked_independently(self):
        # A conflicting claimed SHA must fail even with a matching intrinsic and declared accession.
        original = json.loads((self.descriptors / "2RE9.json").read_bytes())
        for key, overrides in (("wrong-descriptor-hash", {"sha256":"0"*64}),
                               ("wrong-archive-hash", {"data":"https://files.rcsb.org/download/2RE9.cif#sha256="+"0"*64}),
                               ("conflicting-accessions", {"acquisition":"wwPDB:2O0O:revision-1"})):
            code, _ = self.command("observe", key, self.src, original|overrides, existing_ref=self.state.materials[0])
            self.assertNotEqual(code,0)
        self.record("invalid-descriptors")

if __name__ == "__main__":
    unittest.main()
