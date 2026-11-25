"""Synthetic mapped geometry controls and the registered, single-Job GX entry."""
from copy import deepcopy
import csv
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from statistics import median
import tempfile
from types import SimpleNamespace
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, branch, draft
from acceptance.v623.test_block_method import synthetic_cif, FITTER
from acceptance.science_execution.study.independent import verify
from gx_core.artifacts import ArtifactService
from gx_core.p0 import v621_metrics

PACKAGE = "org.gx.block-influence@2.0.0"
METHOD = "deposited-block-influence@2"
WORKER = PRODUCT / "gx-h/plugins/installed/org.gx.block-influence/2.0.0/methods/segment.py"


def plugin():
    spec = importlib.util.spec_from_file_location("registered_mapped_fit", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def example(**options):
    """Label positions shifted by seven, then by eight after a one-site insertion.
    Reference position 26 lacks a target homolog. Model and chain IDs differ from v1.
    """
    source = synthetic_cif(size=150, **options)
    names = {"A": "DD", "B": "E4", "C": "K"}
    def shifted(p):
        return p + (7 if p < 31 else 8)
    out = []
    for line in source.decode().splitlines():
        fields = line.split()
        if line.startswith("ATOM "):
            chain, pos = fields[3], int(fields[4])
            fields[1:5] = ["7", "2", names[chain], str(shifted(pos))]
            if chain == "B" and 21 <= pos <= 40:
                # Different pair order on the two fit sets is possible.
                fields[-2] = str(int(fields[-2])+3)
            out.append(" ".join(fields))
        elif len(fields) == 3 and fields[0] == "1":
            out.append(f"7 {shifted(int(fields[1]))} GLY")
            if int(fields[1]) == 30:
                out.append("7 38 ALA")
        else:
            out.append(line)
    raw = ("\n".join(out)+"\n").encode()
    mapping = {"schema": "gx.coordinate-position-map/1", "model": "2", "entity": "7", "chains": list(names.values()),
               "reference": "synthetic reference/1/polymer 1", "structure_sha256": sha256(raw).hexdigest(),
               "segments": {"21-40": [None if p == 26 else shifted(p) for p in range(21, 41)],
                            "101-120": [shifted(p) for p in range(101, 121)]}}
    return raw, json.dumps(mapping, separators=(",", ":")).encode()


def historical_case_specs():
    yield "2RE9", "101-120", "angle_deg", "CONCEALED"
    yield "2O0O", "21-40", "rmsd_A", "MEDIAN_ALSO_OVER_LIMIT"


def historical_controls():
    impl = plugin()
    geometry = impl.released_geometry(FITTER)
    archive = PRODUCT / "acceptance/v623"
    inputs = {r["entry"]: r for r in json.loads((archive/"study/operator/reference_protocol.json").read_bytes())["inputs"]}
    with (archive/"evidence/study/tables/pair_intervals.csv").open() as stream:
        records = list(csv.DictReader(stream))
    for entry, interval, endpoint, outcome in historical_case_specs():
        item = inputs[entry]
        raw = (PRODUCT/item["repository_path"]).read_bytes()
        # These two controls replay each archive's originally published *local* labels.
        # This identity map is inapplicable to alignment between two depositions.
        mapping = {"schema": "gx.coordinate-position-map/1", "structure_sha256": item["sha256"],
                   "model": "1", "entity": "1", "chains": list("ABC"), "reference": entry + "/published-local-labels",
                   "segments": {"21-40": list(range(21, 41)), "101-120": list(range(101, 121))}}
        encoded = json.dumps(mapping, sort_keys=True).encode()
        table = impl.measure(raw, encoded, interval, "common_backbone", endpoint, geometry)
        report = verify(raw, encoded, table)
        errors = []
        archived_pairs = [r for r in records if r["entry"] == entry and r["mask"] == "common_backbone"
                          and r["endpoint"] == endpoint and r["first_label_seq_id"] == interval.split("-")[0]]
        if len(archived_pairs) != 3 or table["interpretation"] != outcome:
            raise ValueError("Published retrospective control did not reproduce")
        for i, row in enumerate(archived_pairs):
            key = ("pair12", "pair13", "pair23")[i]
            if "".join(table["rows"][key]["chains"]) != row["pair"]:
                raise ValueError("Historical pair identity mismatch")
            for field, archived in (("before", "baseline"), ("after", "after_deletion"), ("delta", "absolute_delta")):
                errors.append(abs(table["rows"][key][endpoint][field] - float(row[archived])))
            for field, archived in (("before", "median_before"), ("after", "median_after"), ("delta", "median_absolute_delta")):
                errors.append(abs(table["rows"]["median"][endpoint][field] - float(row[archived])))
        yield {"replay": "previously published local-label geometry", "source": item["repository_path"],
               "table": table, "independent": report, "historical_max_difference": max(errors)}


class MappedMethod(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.impl = plugin()
        cls.solver = cls.impl.released_geometry(FITTER)

    def test_shifted_insertions_nondefault_entity_chains_and_independent_coverage(self):
        raw, mapping = example()
        for interval, omitted in (("21-40", 19), ("101-120", 20)):
            for endpoint in ("rmsd_A", "angle_deg"):
                for mask in ("common_backbone", "common_ca"):
                    with self.subTest(mask=mask, interval=interval, endpoint=endpoint):
                        table = self.impl.measure(raw, mapping, interval, mask, endpoint, self.solver)
                        self.assertEqual(table["selected_residues"], 150)
                        self.assertEqual(table["omitted_residues"], omitted)
                        self.assertEqual(table["retained_residues"], 150-omitted)
                        report = verify(raw, mapping, table)
                        self.assertEqual(report["status"], "PASS")
                        self.assertLess(report["max_delta_difference"], report["delta_tolerance"])
                        self.assertTrue(set(table["rows"]) == {"pair12", "pair13", "pair23", "median"})
                        self.assertEqual(table["rows"]["pair13"]["chains"], ["DD", "K"])
                        self.assertLess(len(json.dumps(table, separators=(",", ":")).encode()), 2048)
        corrupted = deepcopy(table)
        corrupted["rows"]["pair13"][endpoint]["before"] += .02
        with self.assertRaisesRegex(ValueError, "disagreement"):
            verify(raw, mapping, corrupted)

    def test_median_of_fits_and_strict_boundary_with_qualifying_masks(self):
        raw, mapping = example()
        # Before=0,8,10; after=9,2,10. Difference of medians 1, median of deltas 6.
        def run(rows):
            values = iter(rows)
            return self.impl.measure(raw, mapping, "21-40", "common_ca", "angle_deg", SimpleNamespace(fit=lambda *_: (next(values), 0), _loop_rows=self.solver._loop_rows))
        table = run([0., 9., 8., 2., 10., 10.])
        self.assertEqual(table["rows"]["median"]["angle_deg"], {"before": 8., "after": 9., "delta": 1.})
        self.assertEqual(median(r["angle_deg"]["delta"] for k,r in table["rows"].items() if k != "median"), 6.)
        table = run([0., .5, 1., 2., 3., 3.])
        self.assertEqual(table["borderline"], ["pair12"])
        self.assertEqual(table["exceptions"], ["pair13"])
        table = run([0., 0., 1., 1., 3., 4.])
        self.assertTrue(table["concealed"])
        self.assertEqual(table["interpretation"], "CONCEALED")

    def test_ambiguous_atoms_degenerate_fits_and_partial_coverage(self):
        raw, mapping_bytes = example()
        raw = raw.replace(b"ATOM 7 2 E4 30 GLY O O .", b"ATOM 7 2 E4 30 GLY O O A")
        mapping = json.loads(mapping_bytes)
        mapping["structure_sha256"] = sha256(raw).hexdigest()
        mapping_bytes = json.dumps(mapping).encode()
        for mask, expected in (("common_ca", 150), ("common_backbone", 149)):
            table = self.impl.measure(raw, mapping_bytes, "21-40", mask, "rmsd_A", self.solver)
            self.assertEqual(table["selected_residues"], expected)
            verify(raw, mapping_bytes, table)
        mapping["segments"]["21-40"][5:] = [None] * 15
        mapping_bytes = json.dumps(mapping).encode()
        table = self.impl.measure(raw, mapping_bytes, "21-40", "common_ca", "angle_deg", self.solver)
        self.assertIn("fewer_than_10_mapped_matched_in_interval", table["reasons"])
        self.assertIsNone(verify(raw, mapping_bytes, table)["independent_rows"]["pair12"]["before"])
        raw, mapping_bytes = example(collinear=True, deformed=False)
        table = self.impl.measure(raw, mapping_bytes, "21-40", "common_ca", "angle_deg", self.solver)
        self.assertEqual(table["interpretation"], "UNDETERMINED")
        verify(raw, mapping_bytes, table)
        invalid = json.loads(mapping_bytes)
        invalid["segments"]["21-40"] = [32]*20
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            self.impl.measure(raw, json.dumps(invalid).encode(), "21-40", "common_ca", "angle_deg", self.solver)
        with self.assertRaisesRegex(ValueError, "material"):
            self.impl.measure(raw+b"\n", mapping_bytes, "21-40", "common_ca", "angle_deg", self.solver)

    def test_registered_historical_segments_match_published_rows_and_independent_svd(self):
        for case in historical_controls():
            self.assertLess(case["historical_max_difference"], 1e-9)
            self.assertLess(case["independent"]["max_delta_difference"], case["independent"]["delta_tolerance"])
        self.assertEqual(len(list(historical_case_specs())), 2)

    def test_registered_cpu_job_table_source_and_named_numerics(self):
        raw, mapping = example()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        structure, mapfile = directory/"sample.cif", directory/"mapping.json"
        structure.write_bytes(raw)
        mapfile.write_bytes(mapping)
        config = {"plugins": [PACKAGE], "research_modules": [], "research_effort": "deep",
                  "capabilities": [METHOD], "input_files": [{"role": "structure", "path": str(structure)},
                                                          {"role": "mapping", "path": str(mapfile)}]}
        with Scenario(directory/"study", spec_updates=config) as s:
            parameters = {"interval": "21-40", "endpoint": "rmsd_A", "mask": "common_backbone"}
            primary = s.node(s.lead, "table", s.materials[0], branch_id="local:mapped", capability=METHOD, parameters=parameters)
            primary["inputs"] = list(s.materials)
            lane = {**branch("mapped"), "budget_limits": {"job_submissions": 1, "cpu_seconds": 60}}
            decision = s.call(s.lead, "gx_decide_revision", {"decision": draft(lane, primary)}, "measure")
            job = s.settle(decision["aliases"]["local:table"])
            ref = job["result_ref"]
            table = ArtifactService(s.conn).read_json(ref)
            independent = verify(raw, mapping, table)
            self.assertEqual(independent["status"], "PASS")
            metrics = {m["metric_id"]: m for m in v621_metrics.from_output(s.conn, s.run_id, ref) if m["schema"] == v621_metrics.SCHEMA}
            self.assertEqual(len(metrics), 16)
            for row in ("pair12", "pair13", "pair23", "median"):
                for stage in ("before", "after", "delta"):
                    m = metrics[row+"_"+stage+"_rmsd_A"]
                    self.assertEqual(m["unit"], "Å")
                    self.assertEqual(m["value"], table["rows"][row]["rmsd_A"][stage])
                    self.assertEqual(m["scope"]["pair"], table["rows"][row]["chains"])
                    self.assertEqual(m["scope"]["mapping_sha256"], sha256(mapping).hexdigest())
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
