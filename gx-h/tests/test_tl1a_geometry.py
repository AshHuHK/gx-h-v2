"""Real GX Jobs, cold checkpoints and independent numeric reviewer products on a recorded-coordinate fixture.

The transport peer supplies deterministic human/worker messages. The public model acceptance is separate.
"""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, intent
from gx_core.p0.driver import context, project
from gx_core.p0.roots import Roots
from gx_core.persistence import store
from workflows.tl1a_geometry import domain, review_tools


class OfflineIngress:
    """Acknowledge the test operator's initial action without a model or a network request."""
    def __init__(self, conn, directory, rid):
        self.conn, self.run = conn, rid

    def messages(self):
        for row in self.conn.execute("SELECT * FROM p0_operator_messages WHERE run_id=? AND result_json IS NULL", (self.run,)):
            intent.bind_main(self.conn, self.run, "test-main")
            mid = row["message_id"]
            intent.stage_input(self.conn, self.run, mid, json.loads(row["action_json"]))
            reply = intent.accept_input(self.conn, self.run, "test-main", mid + "-turn", mid)
            self.conn.execute("UPDATE p0_operator_messages SET result_json=? WHERE message_id=?", (ids.canonical_json(reply), mid))

    def finish_main(self, mid):
        pass

    def close(self):
        pass


def product_for(roots, task, inputs, tid):
    db = roots.conn
    assets = ArtifactService(db)
    kind = inputs["output_kind"]
    if kind == "geometry_plan":
        body = {"source_ref": inputs["structure_ref"], "protocols": ["common_backbone", "common_ca"],
                "rationale": "Prespecify paired angular and positional tolerances for two atom counts on the observed union.",
                "criteria": inputs["criteria_template"],
                "hypotheses": [{"id": "invariant", "expected": "stable", "prediction": "All angle and RMSD changes lie within both bounds."},
                               {"id": "selection", "expected": "sensitive", "prediction": "At least one chain pair exceeds a bound."}]}
    else:
        ref, evidence_ref = inputs["proposal_ref"], inputs["evidence_ref"]
        evidence = assets.read_json(evidence_ref)
        plan = assets.read_json(ref)
        body = {"proposal_ref": ref, "evidence_ref": evidence_ref}
        if kind == "geometry_assessment":
            checks = {}
            for route in evidence["routes"]:
                answer = roots._call({"threadId": tid, "turnId": "turn", "callId": route["protocol"],
                                     "tool": "gx_recalculate_geometry", "arguments": {"protocol": route["protocol"]}})
                checks[route["protocol"]] = answer["check_ref"]
            status = evidence["comparison"]["status"]
            findings = {h["id"]: {"supported": status == h["expected"] if status != "insufficient" else None,
                        "reason": evidence["comparison"]["reason"]} for h in plan["hypotheses"]}
            body.update(checks=checks, findings=findings, status=status, limitations=domain.LIMITS)
        else:
            chosen = inputs["available_protocols"][:1]
            body.update(assessment_ref=inputs["assessment_ref"], reason="Assess whether more selective atoms leave an interpretable fit.",
                        counterevidence="The common-atom and full-occupancy masks capture different residues.",
                        uncertainty=domain.LIMITS, next_protocols=chosen, stop_reason=None if chosen else "No further funded, distinct selection.")
    body["intent_responses"] = [{"item_id": k, "version": i["version"], "reason": "Use the bound deposited coordinates."}
                                for k, i in inputs.get("intent_items", {}).items()]
    return body


def submit_peer(conn, directory, run_id, task):
    roots = Roots(conn, run_id=run_id, cwd=directory, worker_tools=review_tools.WORKER_TOOLS,
                  worker_prepare=lambda row, name, args, env: review_tools.recalculate_now(conn, row, args),
                  worker_call=lambda row, name, args, env, prepared=None: review_tools.worker_call(
                      conn, name, args, row, env, prepared=prepared))
    row = roots._claim(task["task_id"])
    if not row:
        return None
    tid = "test-root-" + task["task_id"]
    conn.execute("UPDATE p0_roots SET state='ready',thread_id=? WHERE root_key=?", (tid, row["root_key"]))
    conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (tid, row["lease_id"]))
    roots.active_turns[tid] = "turn"
    inputs = store.loads(conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone(), "input_json")
    product = product_for(roots, task, inputs, tid)
    answer = roots._call({"threadId": tid, "turnId": "turn", "callId": "submit", "tool": "gx_submit_product",
                         "arguments": {"product": product}})
    if conn.execute("SELECT status FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()[0] != "ACCEPTED":
        raise AssertionError(answer)
    return product


class GeometryWorkflow(unittest.TestCase):
    def test_quality_frozen_plan_real_jobs_independent_review_and_next_investigation(self):
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state, patch(
                "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
            spec = cli.geometry_spec()
            with closing(store.open_store(state.db_path)) as conn, patch.object(cli, "Driver", OfflineIngress):
                first = cli.start(conn, Path(tmp), spec)
                run = first["run_id"]
                self.assertFalse(first["tasks"])
                self.assertEqual(len(first["jobs"]), 1)
                self.assertEqual(set(first["intent"]), {"objective", "coordinates", "job-limit"})
            for cycle in range(12):
                with closing(store.open_store(state.db_path)) as conn:
                    for (jid,) in conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)):
                        JobService(conn).wait(jid, timeout_seconds=15)
                    reactor.lazy(context(conn), run)
                    reactor.drive_science(context(conn), run)
                    row = project(conn)
                    if row["status"] == "COMPLETED":
                        break
                    self.assertNotIn(row["status"], ("STOPPED", "PAUSED"))
                    for task in conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY'", (run,)).fetchall():
                        submit_peer(conn, tmp, run, task)
            with closing(store.open_store(state.db_path)) as conn:
                final = cli.snapshot(conn)
                self.assertEqual(final["status"], "COMPLETED")
                self.assertEqual(len(final["jobs"]), 4)
                self.assertEqual(len(final["rounds"]), 2)
                self.assertEqual([t["stage"] for t in final["tasks"]],
                                 ["proposal", "assessment", "decision", "assessment", "decision"])
                assets = ArtifactService(conn)
                statuses = [assets.read_json(r["assessment_ref"])["status"] for r in final["rounds"]]
                self.assertEqual(statuses, ["stable", "insufficient"])
                second = assets.read_json(final["rounds"][1]["proposal_ref"])
                self.assertEqual(second["previous_decision_ref"], final["rounds"][0]["artifact_ref"])
                workflows = conn.execute("SELECT workflow_id,status FROM runs ORDER BY rowid").fetchall()
                self.assertEqual([tuple(r) for r in workflows], [("tl1a_geometry","COMPLETED"),("tl1a_coordinate_quality","COMPLETED")])
                for (admission,) in conn.execute("SELECT admission_json FROM p0_execution_manifests WHERE "
                                                 "json_extract(admission_json,'$.capability.id')='tl1a-geometry'"):
                    record = json.loads(admission)
                    selection = next(row for row in record["inputs"] if row["role"] == "selection")
                    self.assertEqual(selection["artifact_ref"], second["mask_ref"])
                    self.assertEqual(record["evaluator"], second["evaluator"])
                self.assertTrue(all(s["status"] == "ACCEPTED" for s in final["tasks"]))
                self.assertIsNone(reactor.drive_science(context(conn), run))

    def test_confirmed_changed_objective_reuses_original_job_receipts_in_same_project(self):
        cases = ("operator_paused", "active_driver")
        for situation in cases:
            with self.subTest(situation=situation), tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state:
                spec = {**cli.geometry_spec(), "max_jobs": 3}
                with closing(store.open_store(state.db_path)) as conn, patch.object(cli, "Driver", OfflineIngress), patch(
                        "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
                    run = cli.start(conn, Path(tmp), spec)["run_id"]
                    gx = context(conn)
                    def progress():
                        for (jid,) in conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)):
                            JobService(conn).wait(jid, timeout_seconds=15)
                        reactor.lazy(gx, run)
                        reactor.drive_science(gx, run)

                    progress()
                    planner = conn.execute("SELECT * FROM tasks WHERE run_id=? AND stage='proposal'", (run,)).fetchone()
                    submit_peer(conn, tmp, run, planner)
                    progress()
                    progress()
                    assets = ArtifactService(conn)
                    before = store.loads(store.latest_checkpoint(conn, run), "state_json")
                    first = assets.read_json(before["outputs"]["proposal"]["proposal_ref"])
                    original = list(conn.execute("SELECT job_id,scheduler_ref FROM jobs WHERE run_id=? ORDER BY job_id", (run,)))
                    self.assertEqual(len(original), 3)
                    if situation == "operator_paused":
                        cli.change_state(conn, project(conn), "pause")
                    def human(mid, action):
                        intent.stage_input(conn, run, mid, action)
                        return intent.accept_input(conn, run, "test-main", mid + "-turn", mid)
                    reply = human("new-question", {"kind": "change", "items": [{"item_id": "objective",
                         "item_type": "objective", "value": "Which observed pair changes most across atom selections?",
                         "expected_version": 1}]})
                    approved = human("confirm", {"kind": "confirm", "change_id": reply["change_id"], "preview_sha256": reply["preview_sha256"]})
                    self.assertEqual(approved["status"], "committed")
                    if situation == "operator_paused":
                        cli.change_state(conn, project(conn), "continue")
                    progress()
                    fresh = conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY' AND stage='proposal'", (run,)).fetchone()
                    self.assertIsNotNone(fresh)
                    self.assertEqual(store.loads(fresh, "input_json")["objective_item"]["version"], 2)
                    self.assertEqual(store.loads(fresh, "input_json")["available_jobs"], 0)
                    self.assertEqual(set(store.loads(fresh, "input_json")["reusable_protocols"]),
                                     {"common_ca", "common_backbone"})
                    for _ in range(6):
                        for task in conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY'", (run,)).fetchall():
                            submit_peer(conn, tmp, run, task)
                        progress()
                    final = cli.snapshot(conn)
                    self.assertEqual(final["status"], "COMPLETED")
                    refined = assets.read_json(final["rounds"][-1]["proposal_ref"])
                    self.assertEqual(refined["objective_item"]["version"], 2)
                    self.assertNotEqual(first["hypotheses"][0]["hypothesis_id"], refined["hypotheses"][0]["hypothesis_id"])
                    self.assertEqual(original, list(conn.execute("SELECT job_id,scheduler_ref FROM jobs WHERE run_id=? ORDER BY job_id", (run,))))

    def test_formal_geometry_criterion_and_changed_criterion_on_original_measurements(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state, closing(store.open_store(state.db_path)) as conn, patch(
                "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp), patch.object(cli, "Driver", OfflineIngress):
            fixed = {k: copy.deepcopy(domain.TEMPLATE[k]) for k in domain.SECTIONS}
            fixed["coverage"]["min_residues"] = 170  # First-stage coverage cannot meet the user's bound.
            spec = {**cli.geometry_spec(), "max_jobs": 3, "criteria": fixed}
            first = cli.start(conn, Path(tmp), spec)
            rid = first["run_id"]
            assets = ArtifactService(conn)
            def progress():
                for (jid,) in conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (rid,)):
                    JobService(conn).wait(jid, timeout_seconds=15)
                reactor.lazy(context(conn), rid)
                reactor.drive_science(context(conn), rid)
            def ready():
                return conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY' ORDER BY rowid", (rid,)).fetchone()
            progress()
            planner = ready()
            inputs = store.loads(planner, "input_json")
            planned = {
                "source_ref": inputs["structure_ref"], "rationale": "Weakening user coverage would change the question.",
                "protocols": ["common_ca", "common_backbone"], "criteria": {k: domain.TEMPLATE[k] for k in domain.SECTIONS},
                "hypotheses": [{"id": "constant", "prediction": "All pairs stay below tolerance.", "expected": "stable"},
                               {"id": "variable", "prediction": "A pair exceeds tolerance.", "expected": "sensitive"}]}
            with self.assertRaisesRegex(SchemaInvalid, "user's exact"):
                domain.validate_plan(inputs, planned)
            submit_peer(conn, tmp, rid, planner)
            progress()
            progress()
            reviewer = ready()
            before = submit_peer(conn, tmp, rid, reviewer)
            self.assertEqual(before["status"], "insufficient")
            self.assertTrue(all(f["supported"] is None for f in before["findings"].values()))
            for ref in before["checks"].values():
                reproduced = assets.read_json(ref)
                self.assertEqual(reproduced["status"], "matches")
                self.assertTrue(reproduced["reviewer_call_id"])
                self.assertEqual(reproduced["reviewer_task_id"], reviewer["task_id"])
            progress()
            old_ref = before["evidence_ref"]
            old_eval = assets.read_json(old_ref)
            initial_jobs = set((r[0],r[1]) for r in conn.execute("SELECT job_id,scheduler_ref FROM jobs WHERE run_id=?", (rid,)))
            original = intent.read_item(conn, rid, "evaluation")
            self.assertEqual(original["value"]["coverage"]["min_residues"], 170)
            replacement = {**original["value"], "coverage": {**fixed["coverage"], "min_residues": 60}}
            intent.stage_input(conn, rid, "criterion-change", {"kind": "change", "items":[{"item_id": "evaluation",
                  "item_type": "evaluation_spec", "expected_version": 1, "value": replacement}]})
            change = intent.accept_input(conn, rid, "test-main", "criterion-turn", "criterion-change")
            if change["status"] == "awaiting_confirmation":
                intent.stage_input(conn, rid, "criterion-confirm", {"kind":"confirm","change_id":change["change_id"],
                                                                  "preview_sha256": change["preview_sha256"]})
                intent.accept_input(conn, rid, "test-main", "confirm-turn", "criterion-confirm")
            payload = json.loads(conn.execute("SELECT payload_json FROM outbox WHERE topic='p0.intent.reevaluate'").fetchone()[0])
            replay = domain.reevaluate_intent(conn, payload)
            self.assertEqual(replay, domain.reevaluate_intent(conn, payload))
            revised = assets.read_json(replay)
            self.assertEqual([old_eval["comparison"]["status"], revised["comparison"]["status"]], ["insufficient","stable"])
            self.assertEqual(revised["original_evaluation_id"], old_eval["eval_id"])
            self.assertTrue(all(r["rescored_ref"] != r["observations_ref"] for r in revised["routes"]))
            for _ in range(6):
                progress()
                if t := ready():
                    submit_peer(conn, tmp, rid, t)
            self.assertEqual(project(conn)["status"], "COMPLETED")
            self.assertEqual(initial_jobs, {(r[0],r[1]) for r in conn.execute("SELECT job_id,scheduler_ref FROM jobs WHERE run_id=?", (rid,))})
            self.assertEqual(assets.read_json(old_ref), old_eval)
            last = cli.snapshot(conn)["rounds"][-1]
            self.assertEqual(assets.read_json(last["proposal_ref"])["evaluation_item"]["version"], 2)
            self.assertEqual(assets.read_json(last["assessment_ref"])["status"], "stable")
