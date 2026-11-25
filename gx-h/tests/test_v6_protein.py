"""V6 ordered methods use newly admitted Jobs and original measurements in a fresh GX store."""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.capabilities import shipped
from gx_core.domain import ids
from gx_core.errors import IdempotencyConflict, SchemaInvalid, ScopeDenied
from gx_core import config
from gx_core.p0 import manifest, v6_claims, v6_decisions, v6_goal, v6_jobs, v6_method_context
from gx_core.plugins import goal, selection
from gx_core.persistence import store
from tests.test_v6_decisions import bind_native_fixture, branch, draft


class ProteinAdapter(unittest.TestCase):
    def test_exact_order_and_parameter_contract(self):
        package, = selection.select(["org.gx.protein@0.2.0"], "gx.goal","1").plugins
        for cap, roles, params in [
            (shipped.GEOMETRY, ["structure"], {"protocol": "common_ca", "chain_pairs": ["A/C"]}),
            (shipped.GEOMETRY, ["structure"] * 2, {"protocol": "common_ca"}),
            (shipped.GEOMETRY, ["structure"], {"protocol": "none"}),
            (shipped.GEOMETRY, ["structure"], {"protocol": "common_ca", "criteria": {"mapping": {}}}),
            (shipped.GEOMETRY, ["structure"], {"protocol": "common_ca", "criteria": {"coverage": {"min_fraction": 0}}}),
            (shipped.QUALITY, ["structure"], {"selection": "common_ca"}),
            (shipped.REVIEW, ["structure"], {}),
        ]:
            with self.subTest(cap=cap, roles=roles, params=params), self.assertRaises(SchemaInvalid):
                provider = package.contributions.goal_methods[cap]
                v6_method_context.invoke(provider.validate, None, cap, roles, params, producers={})

    def test_real_methods_independent_fits_and_threshold_only_reading(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as conn:
            data = config.HARNESS_ROOT / "adapters/scientific/tl1a_geometry/data/2RE9.cif"
            pins = [shipped.QUALITY, shipped.GEOMETRY, shipped.REVIEW]
            spec = {"project": "coordinates", "mode": "goal", "objective": "Compare deposited coordinate protocols",
                    "evidence_policy": "gx.evidence/6.2", "historical_replay": True,  # Retrospective evaluator and original claim wording.
                    "plugins": ["org.gx.protein@0.2.0"], "capabilities": pins,
                    "input_files": [{"path": str(data), "role": "structure"}]}
            run = v6_goal.start(conn, spec, run_key="coordinate-graph", current=None)["run_id"]
            record = conn.execute("SELECT budget_ref,inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()
            budget, structure = record["budget_ref"], json.loads(record["inputs_json"])["files"][0]["artifact_ref"]
            methods = json.loads(record["inputs_json"])["inputs"]["capability_catalog"]
            self.assertEqual({row["capability_version"]: row["portable_sha256"] for row in methods},
                             {cap: shipped.pin(cap) for cap in pins})
            self.assertEqual({row["plugin"]["version"] for row in methods}, {"0.2.0"})
            assets = ArtifactService(conn)

            def bind(assigned_run, cap, refs, params):
                ctx = v6_method_context.MethodContext(conn, assigned_run, refs, writable=True)
                return v6_method_context.invoke(goal.method(conn, run, cap).bind, ctx, cap, refs, params)

            def evaluate(refs, criteria, **call):
                return v6_method_context.call_tool(conn, "gx_reevaluate_geometry",
                                                   {"measurement_refs": refs, "criteria": criteria},
                                                   run_id=run, **call)

            with self.assertRaises(SchemaInvalid):
                bind("other-run", shipped.QUALITY, [structure], {})

            def node(name, cap, deps, params):
                return dict(op="add_node", branch="local:sample", node="local:" + name, capability=cap,
                            inputs=[structure, *deps], depends_on=deps, parameters=params,
                            question="Measure or check the selected coordinate protocol")

            proposal = draft({**branch(), "budget_limits": {"job_submissions": 4, "cpu_seconds": 120}},
                             node("inventory", shipped.QUALITY, [], {}),
                             node("ca", shipped.GEOMETRY, ["local:inventory"], {"protocol": "common_ca"}),
                             node("backbone", shipped.GEOMETRY, [], {"protocol": "common_backbone"}),
                             node("review", shipped.REVIEW, ["local:ca", "local:backbone"], {}))
            proposal["reconsider_when"] = [{"event": "job_settled", "subject_ref": "local:review"}]
            call = bind_native_fixture(conn, run, Path(tmp))
            receipt = v6_decisions.adopt(conn, **call, decision=proposal)
            self.assertEqual(receipt["result_head"]["version"], 1)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                v6_jobs.advance(conn, run)
                rows = conn.execute("SELECT node_id,status,result_ref FROM v6_nodes WHERE run_id=?", (run,)).fetchall()
                self.assertFalse([dict(r) for r in rows if r["status"] in {"FAILED", "BLOCKED", "CANCELLED"}])
                if all(r["status"] == "SUCCEEDED" for r in rows):
                    break
                time.sleep(0.03)
            else:
                self.fail(f"Coordinate nodes did not settle: {[dict(r) for r in rows]}")
            outputs = {r["node_id"]: r["result_ref"] for r in rows}
            inventory, ca, backbone, checked = [outputs[receipt["aliases"]["local:" + name]]
                                                for name in ("inventory", "ca", "backbone", "review")]
            full_inventory = assets.read_json(inventory)
            brief = goal.method(conn, run, shipped.QUALITY).project(full_inventory)
            self.assertEqual(brief["mapping"], full_inventory["mapping"])
            self.assertLess(len(ids.canonical_json(brief)), len(ids.canonical_json(full_inventory)) // 5)
            for name, mask in brief["protocol_masks"].items():
                self.assertEqual(mask["selected_atoms"], len(full_inventory["protocol_masks"][name]["selected_keys"]))
                self.assertIn(f"/protocol_masks/{name}/selected_keys", brief["view"]["omitted_pointers"])
            calculations = assets.read_json(checked)["results"]
            self.assertEqual(set(calculations), {"common_ca", "common_backbone"})
            for protocol, result in calculations.items():
                self.assertEqual(result["status"], "matches", result)
                self.assertEqual(len(result["differences"]), 3)
                self.assertTrue(all(p["within_reproduction_tolerance"] for p in result["differences"]))
                self.assertEqual(result["observations_ref"], ca if protocol == "common_ca" else backbone)
            with self.assertRaises(SchemaInvalid):
                bind(run, shipped.REVIEW, [structure, inventory], {})

            self.assertIsNotNone(v6_goal.ensure_task(conn, run))
            identity = bind_native_fixture(conn, run, Path(tmp))
            identity.pop("run_id")
            identity["context_ref"] = conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?",
                                                    (identity["task_id"],)).fetchone()[0]
            original = {ref: ids.sha256_of_obj(assets.read_json(ref)) for ref in (ca, backbone)}
            before = store.budget_state(conn, budget)
            original_read = evaluate([ca, backbone], {}, **identity)
            revised_call = {**identity, "call_id": "new-threshold"}
            change = {"coverage": {"min_residues": 10000}}
            updated = evaluate([ca, backbone], change, **revised_call)
            self.assertEqual(updated, evaluate([ca, backbone], change, **revised_call))
            with self.assertRaises(IdempotencyConflict):
                evaluate([ca, backbone], {}, **revised_call)
            with self.assertRaises(ScopeDenied):
                evaluate(["gxa_outside"], {}, **{**identity, "call_id": "different-source"})
            self.assertNotEqual(original_read["evaluation_ref"], updated["evaluation_ref"])
            self.assertEqual(updated["evaluation"]["comparison"]["status"], "insufficient")
            self.assertEqual([s["artifact_ref"] for s in updated["evaluation"]["measurements"]], [ca, backbone])
            self.assertEqual(updated["evaluation"], assets.read_json(updated["evaluation_ref"]))
            self.assertEqual(original, {ref: ids.sha256_of_obj(assets.read_json(ref)) for ref in (ca, backbone)})
            self.assertEqual(store.budget_state(conn, budget), before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='v6.evaluation.recorded' "
                                          "AND run_id=?", (run,)).fetchone()[0], 2)

            refs = [ca, backbone, updated["evaluation_ref"]]
            claim = {"level": "insufficient", "scope": "2RE9 chain pairs under the test coverage threshold",
                     "statement": "Neither mask meets the deliberately raised minimum of 10,000 positions.",
                     "criteria": "Test-only retrospective minimum of 10,000 observed positions",
                     "limitations": "One deposition; numerical reproduction shares the same coordinates.",
                     "rationale": "The stored coverage counts fall below this threshold despite reproducible fits.",
                     "evidence_refs": refs}
            identity["call_id"] = "stage-restricted"
            proposed = v6_claims.stage(conn, run_id=run, **identity, payload=claim, allowed_refs=refs)
            stop = draft({"op": "close_run", "reason": "All assigned checks returned; close the bounded test.",
                          "claim_refs": [proposed["claim_ref"]], "unresolved": ["Independent deposition"]})
            stop["evidence_refs"] = refs
            close_call = {k: v for k, v in identity.items() if k != "context_ref"}
            close_call.update(run_id=run, call_id="close-test")
            final = v6_decisions.adopt(conn, **close_call, decision=stop)
            v6_jobs.advance(conn, run)
            with closing(store.open_store(Path(tmp) / "gx.db")) as reopened:
                self.assertEqual(reopened.execute("SELECT status FROM runs WHERE run_id=?", (run,)).fetchone()[0],
                                 "STOPPED")
                self.assertEqual(reopened.execute("SELECT status FROM v6_goals WHERE run_id=?", (run,)).fetchone()[0], "CLOSED")
                self.assertEqual(v6_decisions.adopt(reopened, **close_call, decision=stop), final)
                self.assertEqual(len(v6_claims.current(reopened, run)), 1)
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 4)
            if target := os.environ.get("GX_V6_PROTEIN_RECEIPT_DIR"):
                out = Path(target)
                out.mkdir(parents=True, exist_ok=True)
                jobs, originals = {}, {}
                for name, ref in dict(inventory=inventory, ca=ca, backbone=backbone, review=checked).items():
                    meta = assets.describe(ref)
                    raw = assets.read_bytes(ref, expected_sha256=meta["sha256"])
                    (out / (name + ".json")).write_bytes(raw)
                    jobs[name] = manifest.read_manifest(conn, meta["job_id"])
                    originals[name] = {k: meta[k] for k in ("artifact_ref", "sha256", "bytes", "job_id")}
                record = {"passed": True, "evidence": ["SCRIPTED", "REAL_COMPUTE"], "run_id": run,
                          "scope": "The test supplies the scientific decision and hypothetical threshold.",
                          "source_sha256": assets.describe(structure)["sha256"], "source_ref": structure,
                          "product_catalog": methods, "first_decision": receipt, "closing_decision": final,
                          "geometry_provenance": originals, "job_manifests": jobs,
                          "original_evaluation": original_read, "threshold_only_evaluation": updated,
                          "claim": v6_claims.current(conn, run)[0], "budget_at_observation": before,
                          "budget_after_reevaluation": store.budget_state(conn, budget),
                          "reopened_status": "STOPPED", "jobs_at_reading": 4, "jobs_after_reopen": 4,
                          "independent_fits": sum(len(v["differences"]) for v in calculations.values()),
                          "additional_model_requests": 0}
                (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
