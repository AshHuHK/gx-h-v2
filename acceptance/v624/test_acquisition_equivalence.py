"""Probe the public source ingress and the relation it certifies for a pinned endpoint."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest

from acceptance.v622 import test_source_identity
from acceptance.v62c import materials
from adapters.scientific import coordinate_equivalence
from gx_core.p0 import v622_sources, v622_measurement


class AcquisitionEdges(unittest.TestCase):
    command = test_source_identity.MaterialSource.command

    def setUp(self):
        test_source_identity.MaterialSource.setUp(self)
        self.edges = []

    def save_receipt(self):
        directory = os.getenv("GX624_EDGE_RECEIPTS")
        if directory:
            dest = Path(directory)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / (self._testMethodName + ".json")).write_text(json.dumps({
                "run_id": self.state.run_id, "edges": self.edges, "operations": self.out,
            }, sort_keys=True, indent=2) + "\n")

    def admit(self, title, path, parent=None, processing=None, transformation=None, existing_ref=None):
        desc = materials.parent_acquisition()
        if parent:
            desc["derived_from"] = [parent]
            desc["processing"] = [processing]
        if transformation:
            desc["transformation"] = transformation
        code, result = self.command("observe", title, path, desc, existing_ref=existing_ref)
        self.assertEqual(code, 0, self.out[-1])
        event = self.state.rows("SELECT payload_json FROM events WHERE event_id=?", result["event_id"])[0]
        identity = json.loads(event["payload_json"])["acquisition"]["source_identity"]
        self.edges.append({"source": title, "event_id": result["event_id"], "artifact_ref": result["artifact_ref"],
                           "source_identity": identity})
        return {k: result[k] for k in ("artifact_ref", "sha256")}, identity

    def method(self, mask="common_backbone"):
        fields = {"capability": "deposited-chain-geometry@2", "output": "geometry.json",
                  "parameters": {"mask": mask}, "input_role": "structure", "unit": "1", "metric_id": "coverage"}
        return v622_measurement.pin(self.state.conn, self.state.run_id, {"method": fields, "observable": "/metrics/coverage"})

    def effect(self, parent, child, proof, method=None):
        return v622_sources.endpoint_relation(self.state.conn, method or self.method(), proof,
                                              parent_sha256=parent["sha256"], child_sha256=child["sha256"])

    def test_mixed_annotation_withholding_annotation_chain(self):
        parent, _ = self.admit("archive", self.src, existing_ref=self.state.materials[0])
        source = self.src.read_bytes()
        comment_file = self.work / "comment.cif"
        comment_file.write_bytes(source + b"# envelope remark\n")
        comment, evidence = self.admit("comment", comment_file, parent, "append-envelope-comment/v1")
        self.assertEqual(self.effect(parent, comment, evidence), "equivalent")
        self.assertEqual(self.effect(parent, parent, evidence), "unknown")
        variants = materials.prepare(self.work/"variants")
        for code in ("P4","K7"):
            base = self.work/"variants"/code
            # The parent now includes the harmless trailing comment: replay the
            # same atom removals on that exact version rather than the original.
            rule = json.loads((base/"transform.json").read_bytes()) | {"parent_sha256":comment["sha256"]}
            rule_digest = hashlib.sha256((json.dumps(rule, indent=2, sort_keys=True)+"\n").encode()).hexdigest()
            material = self.work / (code + ".cif")
            material.write_bytes((base/"specimen.cif").read_bytes() + b"# envelope remark\n")
            child, proof = self.admit(code, material, comment, "coordinate-record withholding/"+rule_digest, rule)
            self.assertNotEqual(variants[code]["sha256"], child["sha256"])
            self.assertEqual(self.effect(comment, child, proof), "different")
            self.assertEqual(self.effect(comment, child, proof, self.method("common_ca")), "equivalent")
            self.assertEqual(self.effect(parent, child, proof), "unknown")
            for key, wrong in (("unit","nm"),("input_role","other"),("output_name","other"),
                               ("observable","/other"),("measurement_schema","other/1"),
                               ("parameters",{"mask":"unregistered"}),("metric_id","another")):
                method = copy.deepcopy(self.method())
                method["measurement_spec"][key] = wrong
                self.assertEqual(self.effect(comment, child, proof, method), "unknown", key)
            forged = copy.deepcopy(self.method())
            forged["manifest_sha256"] = forged["measurement_spec"]["manifest_sha256"] = "0" * 64
            self.assertEqual(self.effect(comment, child, proof, forged), "unknown")
            # Edit an archive annotation outside the atom loop and change whitespace
            # in the actual loop. The method still parses the identical records.
            annotated = self.work/(code + "-annotated.cif")
            annotated.write_bytes(material.read_bytes().replace(b"ATOM ", b"ATOM    ", 1) + b"# annotated\n")
            repack, repack_proof = self.admit(code+"-annotation", annotated, child, coordinate_equivalence.ANNOTATION)
            self.assertEqual(self.effect(child, repack, repack_proof), "equivalent")
            self.assertEqual(self.effect(comment, repack, repack_proof), "unknown")
        self.save_receipt()

    def test_unknown_transform_and_false_repack(self):
        parent, _ = self.admit("archive", self.src, existing_ref=self.state.materials[0])
        materials.prepare(self.work/"variants")
        material = self.work/"variants/K7/specimen.cif"
        child, proof = self.admit("unqualified", material, parent, "issuer-software/unknown-version")
        self.assertEqual(proof["status"], "unverified_derivation")
        self.assertTrue(proof["reason"])
        self.assertEqual(self.effect(parent, child, proof), "unknown")
        meta = materials.parent_acquisition() | {"derived_from":[parent], "processing":[coordinate_equivalence.ANNOTATION]}
        code, _ = self.command("observe", "false-annotation", material, meta)
        self.assertNotEqual(code, 0)
        self.assertIn("must preserve", self.out[-1]["error"])
        self.save_receipt()


if __name__ == "__main__":
    unittest.main()
