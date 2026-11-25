"""Synthetic coordinate controls and the real GX Job path, independent of any deposited study endpoint."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, branch, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v621_metrics

METHOD = "deposited-block-influence@1"
READ = "deposited-block-reading@1"
PACKAGE = "org.gx.block-influence@1.0.0"
METHODS = PRODUCT / "gx-h/plugins/installed/org.gx.block-influence/1.0.0/methods"
FITTER = PRODUCT / "gx-h/adapters/scientific/tl1a_geometry/worker.py"


def module(name):
    spec = importlib.util.spec_from_file_location(name, METHODS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


# Exact integer similarities avoid introducing rounding in the transformed synthetic CIF.
def synthetic_cif(*, size=100, transformed=False, deformed=True, missing=False, collinear=False, empty=False):
    lines = ["data_SYN", "_entry.id SYN", "#", "loop_", "_entity_poly_seq.entity_id", "_entity_poly_seq.num",
             "_entity_poly_seq.mon_id"]
    lines.extend(f"1 {p} GLY" for p in range(1, size + 1))
    lines += ["#", "loop_"]
    fields = ("group_PDB", "label_entity_id", "pdbx_PDB_model_num", "label_asym_id", "label_seq_id",
              "label_comp_id", "label_atom_id", "type_symbol", "label_alt_id", "occupancy", "B_iso_or_equiv",
              "Cartn_x", "Cartn_y", "Cartn_z")
    lines += ["_atom_site." + field for field in fields]
    for chain in "ABC":
        for pos in range(1, size + 1):
            for atom, offsets in (("N", (0, 0, -1)), ("CA", (0, 0, 0)), ("C", (1, 0, 0)), ("O", (1, 1, 0))):
                if empty or (missing and (pos > 95 or (chain == "B" and atom == "O" and pos <= 8))):
                    continue
                x, y, z = pos + offsets[0], (3 * pos) % 17 + offsets[1], pos * pos % 13 + offsets[2]
                if collinear:
                    x, y, z = pos, 0, 0
                if chain == "B":
                    x, y, z = -y + 30, x + 10, z + 15
                elif chain == "C":
                    x, y, z = z, y + 7, -x
                    if deformed and 21 <= pos <= 40:
                        x, y, z = x + 8, y + 11, z - 10
                if transformed:
                    x, y, z = z + 9, x - 4, y + 11
                lines.append(f"ATOM 1 1 {chain} {pos} GLY {atom} {atom[0]} . 1 20 {x} {y} {z}")
    return ("\n".join([*lines, "#"]) + "\n").encode()


class BlockSensitivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = module("block_influence")
        cls.projector = module("block_reading")
        cls.fit = cls.worker.load_fitter(FITTER)

    def test_known_deformation_and_common_motion_invariance(self):
        raw = synthetic_cif()
        doc = self.worker.measure(raw, "common_backbone", self.fit)
        moved = self.worker.measure(synthetic_cif(transformed=True), "common_backbone", self.fit)
        self.assertEqual(doc["eligible_blocks"], 5)
        self.assertEqual(doc["matched_atoms"], 400)
        self.assertEqual(doc["input_sha256"], hashlib.sha256(raw).hexdigest())
        for p, before in doc["baseline"].items():
            for endpoint in self.worker.ENDPOINTS:
                self.assertAlmostEqual(before[endpoint], moved["baseline"][p][endpoint], places=7)
        for b, c in zip(doc["blocks"], moved["blocks"], strict=True):
            self.assertEqual(b["positions"], c["positions"])
            self.assertEqual(len(b["controls"]), 20)
            observed = set()
            for ctrl in b["controls"]:
                sample = tuple(ctrl["positions"])
                self.assertEqual(len(sample), len(b["positions"]))
                self.assertTrue(set(sample).isdisjoint(b["positions"]))
                self.assertGreaterEqual(len({(p-1)//20 for p in sample}), 3)
                self.assertNotIn(sample, observed)
                observed.add(sample)
            for pair, row in b["pairs"].items():
                for metric in ("angle_deg", "rmsd_A", "delta_angle_deg", "delta_rmsd_A"):
                    self.assertAlmostEqual(row[metric], c["pairs"][pair][metric], places=7)
        block = doc["blocks"][1]
        self.assertGreater(block["pairs"]["AC"]["delta_rmsd_A"], 0.15)
        self.assertLess(block["pairs"]["AB"]["delta_rmsd_A"], 1e-8)
        self.assertTrue(block["pairs"]["AC"]["rmsd_A_assessment"]["exceeds_dispersed_range"])
        self.assertAlmostEqual(block["pairs"]["AC"]["rmsd_A"], 0, places=7)
        self.assertFalse(doc["summary"]["rmsd_A"]["all_within_limit"])
        control_doc = self.worker.measure(synthetic_cif(deformed=False), "common_ca", self.fit)
        self.assertTrue(control_doc["summary"]["rmsd_A"]["all_within_limit"])
        self.assertTrue(control_doc["summary"]["angle_deg"]["all_within_limit"])

    def test_gaps_degenerate_fit_controls_and_exact_boundary(self):
        empty = self.worker.measure(synthetic_cif(size=105, empty=True), "common_ca", self.fit)
        self.assertEqual(len(empty["blocks"]), 6)
        self.assertIn("empty_observed_union", empty["blocks"][0]["reasons"])
        self.assertIsNone(empty["baseline"]["AB"]["rmsd_A"])
        self.assertIsNone(empty["summary"]["rmsd_A"]["all_within_limit"])
        doc = self.worker.measure(synthetic_cif(size=105, missing=True), "common_backbone", self.fit)
        self.assertEqual(doc["positions"], list(range(9, 96)))
        self.assertEqual(doc["blocks"][-1]["positions"], [])
        self.assertFalse(doc["blocks"][-1]["eligible"])
        bad = self.worker.measure(synthetic_cif(deformed=False, collinear=True), "common_ca", self.fit)
        self.assertTrue(bad["baseline"]["AB"]["reason"])
        self.assertIsNone(bad["blocks"][0]["pairs"]["AB"]["delta_angle_deg"])
        self.assertIsNone(bad["blocks"][0]["median"]["angle_deg"]["concealed"])
        self.assertEqual(list(self.worker.dispersed(range(1, 40), "a" * 64, "common_ca", 2, 10)), [])
        baseline = {p: {"angle_deg": 0.0} for p in self.worker.PAIRS}
        pair_rows = {p: {"angle_deg": d, "delta_angle_deg": d} for p, d in zip(self.worker.PAIRS, (0., .5, 1.))}
        median = self.worker.median_effect(pair_rows, baseline, "angle_deg")
        self.assertEqual((median["delta"], median["exceptions"], median["concealed"]), (.5, ["BC"], True))

    def test_goal_runs_declared_method_and_projects_only_its_original_table(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = tmp / "sample.cif"
        path.write_bytes(synthetic_cif())
        with Scenario(tmp / "study", spec_updates={"research_effort": "deep", "research_modules": [],
                "plugins": [PACKAGE], "capabilities": [METHOD, READ],
                "input_files": [{"role": "structure", "path": str(path)}]}) as s:
            lead = s.lead
            lane = {**branch("geometry"), "budget_limits": {"job_submissions": 3, "cpu_seconds": 260}}
            raw = s.materials[0]
            primary = s.node(lead, "table", raw, branch_id="local:geometry", capability=METHOD)
            project = s.node(lead, "reading", "local:table", branch_id="local:geometry", capability=READ,
                             parameters={"pair": "AC", "block_index": 1})
            project["depends_on"] = ["local:table"]
            with self.assertRaises(SchemaInvalid):
                wrong = {**project, "inputs": [raw], "depends_on": []}
                s.call(lead, "gx_decide_revision", {"decision": draft(lane, wrong)}, "forged-table")
            decision = s.call(lead, "gx_decide_revision", {"decision": draft(lane, primary, project)}, "measure")
            job = s.settle(decision["aliases"]["local:table"])
            projected = s.settle(decision["aliases"]["local:reading"])
            assets = ArtifactService(s.conn)
            table = assets.read_json(job["result_ref"])
            cell = assets.read_json(projected["result_ref"])
            self.assertEqual(cell["input_sha256"], assets.describe(job["result_ref"])["sha256"])
            self.assertEqual(cell["material_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(cell["delta_rmsd_A"], table["blocks"][1]["pairs"]["AC"]["delta_rmsd_A"])
            metrics = {m["metric_id"]: m for m in v621_metrics.from_output(s.conn, s.run_id, projected["result_ref"])}
            value = metrics["delta_rmsd_A"]
            self.assertEqual((value["schema"], value["unit"], value["value"]), (v621_metrics.SCHEMA, "Å", cell["delta_rmsd_A"]))
            self.assertEqual(value["scope"]["pair"], "AC")
            self.assertEqual(value["scope"]["block_index"], 1)
            self.assertAlmostEqual(float(value["raw_value"]), cell["delta_rmsd_A"], places=12)
            self.assertEqual({m["schema"] for m in v621_metrics.from_output(s.conn, s.run_id, job["result_ref"])},
                             {v621_metrics.SCHEMA})
