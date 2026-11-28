"""Public numerical controls for prospective claims, independent acquisitions and cold replay."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import v6_claims, v6_goal, v6_jobs, v62_maturity
from gx_core.persistence import records, store
from tests import test_v6_claims


class ProspectiveClaims(unittest.TestCase):
    scope = "The three registered river transects during the spring survey"
    metric = "survey.water-temperature.celsius/1"

    def setUp(self):
        self.f = test_v6_claims.ClaimRevisions()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.db, self.run = self.f.conn, self.f.run
        self.db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs', json_object("
                        "'research_evidence_version',?)) WHERE run_id=?", (v62_maturity.VERSION, self.run))
        self.plans = [self.put({"transect": c}, "acquisition_plan") for c in ("A", "B", "C")]
        self.allow(*self.plans)
        self.prediction = {"data_refs": self.plans, "observable": "/measurement/value", "threshold": 10,
                           "operator": "gt", "target": "experimental_observation", "measurement_schema": self.metric}

    def put(self, doc, kind, *, run=None, alias=None):
        run = run or self.run
        return ArtifactService(self.db).publish_bytes(
            namespace=run, run_id=run, rel_path=(alias or ids.sha256_of_obj(doc)) + ".json",
            data=ids.canonical_json(doc), media_type="application/json", kind=kind, origin="INGEST").artifact_ref

    def allow(self, *refs):
        self.f.refs.extend(refs)
        self.db.execute("UPDATE v6_decision_tasks SET visible_refs_json=? WHERE task_id=?",
                        (json.dumps(self.f.refs), self.f.task))

    def stage(self, key="stage-prediction", **patch):
        payload = {"level": "open", "statement": "The registered transect temperatures exceed ten degrees Celsius.",
                   "rationale": "Choose an outcome and threshold before collecting the water samples.",
                   "criteria": "Temperature above 10 Celsius on the registered collection.",
                   "limitations": "This sampling frame covers three named transects.",
                   "scope": self.scope, "evidence_refs": self.plans[:1], "prediction": self.prediction, **patch}
        return self.f.submit(key, **payload)

    def adopt(self, proposal):
        return self.f.adopt(proposal, refs=self.plans)[0]

    def observe(self, value, index, *, run=None, plan=None, label=None, acquisition=None, metric=None, raw_ref=None):
        run = run or self.run
        token = label or chr(65 + index)
        artifact = raw_ref or self.put({"schema": metric or self.metric, "measurement": {"value": value},
                                        "sample": token}, "field_observation", run=run)
        lineage = {key: key + "-" + token for key in v62_maturity.DEPENDENCIES}
        lineage.update(applicability=self.scope, data_refs=[plan or self.plans[index]], processing="SOP-1")
        event = v6_goal.record_observation(self.db, run, artifact, source="instrument-" + token,
                                           acquisition={**lineage, **(acquisition or {})})
        return event

    def current(self):
        return v6_claims.current(self.db, self.run)[0]

    def test_registration_real_values_independence_and_late_counterevidence_are_replayable(self):
        proposal = self.stage()
        self.assertEqual(proposal, self.stage())
        self.assertEqual(v6_claims.current(self.db, self.run), [])
        first = self.adopt(proposal)
        self.assertEqual(first["maturity"]["stage"], "preregistered")
        original = self.observe(12.5, 0)
        tested, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual((tested["maturity"]["stage"], tested["maturity"]["direction"]), ("tested", "supported"))
        measured, = tested["maturity"]["results"]
        self.assertEqual((measured["value"], measured["artifact_ref"]), (12.5, original["artifact_ref"]))
        self.assertLess(first["maturity"]["registration"]["event_id"], measured["first_event_id"])
        second = self.observe(13, 1)
        replicated, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual(replicated["maturity"]["stage"], "replicated")
        pair, = replicated["maturity"]["replication"]
        self.assertEqual((pair["relation"], pair["shared"]["processing"]), ("independent_acquisitions", ["SOP-1"]))
        adverse = self.observe(8, 2)
        revised, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual((revised["maturity"]["stage"], revised["maturity"]["direction"]), ("tested", "mixed"))
        self.assertEqual(revised["maturity"]["opposing_refs"], [adverse["artifact_ref"]])
        self.assertEqual(revised["maturity"]["supporting_refs"], [original["artifact_ref"], second["artifact_ref"]])
        wake = self.db.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='v62.claim.reassessed' "
                               "ORDER BY event_id DESC LIMIT 1", (self.run,)).fetchone()
        self.assertTrue(json.loads(wake[0])["critical_counterevidence"])
        self.assertEqual(v62_maturity.reassess(self.db, self.run), [])
        cold = store.open_store(self.f.dbfile)
        self.addCleanup(cold.close)
        for doc in (first, tested, replicated, revised):
            self.assertEqual(doc["maturity"], v62_maturity.recompute(cold, self.run, doc["claim_revision_id"]))
        self.assertEqual(v6_claims.current(cold, self.run), [revised])
        self.assertEqual(v6_claims.read_revision(cold, self.run, proposal["claim_ref"]), first)

    def test_relabelled_shared_or_incomplete_acquisitions_do_not_replicate(self):
        self.adopt(self.stage())
        self.observe(11, 0)
        self.observe(11.1, 0, label="other-reader")
        self.observe(12, 1, acquisition={"sample":"sample-A"})
        self.observe(14, 2, acquisition={"publication":None})
        result, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual(result["maturity"]["stage"], "tested")
        relations = {r["relation"] for r in result["maturity"]["replication"]}
        self.assertIn("same_data_method_consistency", relations)
        self.assertIn("incomplete_provenance", relations)
        self.assertIn("dependent_acquisitions", relations)
        # A new, genuinely independent pair would be needed; the second reader misstates provenance.
        self.assertNotIn("independent_acquisitions", relations)

    def test_measurement_units_scope_boolean_and_fixture_cannot_test_the_rule(self):
        self.adopt(self.stage())
        self.observe(99, 0, metric="survey.length.cm/1")
        self.observe(42, 1, acquisition={"applicability":"a separate winter survey"})
        self.observe(True, 2)
        revised, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual((revised["maturity"]["stage"], revised["maturity"]["direction"]),
                         ("preregistered", "undetermined"))
        self.assertEqual({g["kind"] for g in revised["maturity"]["gaps"]},
                         {"method_or_measurement_schema_mismatch", "out_of_scientific_scope", "unmeasured_observable"})
        with self.assertRaisesRegex(SchemaInvalid, "classified measurement"):
            self.stage(key="fixture-refuted", level="refuted", evidence_refs=["reading"])

    def test_staging_before_results_and_hiding_them_from_the_lead_cannot_backdate_registration(self):
        staged = self.stage()
        known = self.observe(9.0, 0)
        late = self.adopt(staged)
        self.assertTrue(late["maturity"]["preexisting_result"])
        self.assertNotIn(late["maturity"]["stage"], ("preregistered", "tested", "replicated"))
        self.assertEqual(late["maturity"]["results"], [])
        self.assertNotIn(known["artifact_ref"], json.dumps(late["maturity"]))
        reread, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual(reread["maturity"]["stage"], "grounded")
        self.assertEqual(reread["maturity"]["gaps"][0]["kind"], "already_available")

    def test_prior_run_renamed_dataset_and_new_copy_of_result_keep_earliest_publication(self):
        other = records.create_run(self.db, objective="historical campaign", project_ref="control", actor="fixture")
        copy = self.put({"transect":"A"}, "acquisition_plan", run=other, alias="renamed-data")
        history = self.observe(23, 0, run=other, plan=copy, label="old")
        first = self.adopt(self.stage())
        self.assertTrue(first["maturity"]["preexisting_result"])
        self.assertNotIn(history["artifact_ref"], json.dumps(first["maturity"]))
        observed = ArtifactService(self.db).read_json(history["artifact_ref"])
        late_copy = self.put(observed, "field_observation", alias="redisclosed")
        self.observe(23, 0, label="renamed", raw_ref=late_copy)
        current, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual(current["maturity"]["stage"], "grounded")
        self.assertLess(current["maturity"]["gaps"][0]["first_event_id"], first["maturity"]["registration"]["event_id"])

    def test_withheld_bytes_disclosed_after_registration_remove_prospective_status(self):
        early = self.put({"schema":self.metric, "measurement":{"value":12}}, "field_observation")
        initial = self.adopt(self.stage())
        self.assertEqual(initial["maturity"]["stage"], "preregistered")
        self.observe(12, 0, raw_ref=early)
        corrected, = v62_maturity.reassess(self.db, self.run)
        self.assertTrue(corrected["maturity"]["preexisting_result"])
        self.assertEqual(corrected["maturity"]["stage"], "grounded")
        self.assertEqual(v62_maturity.recompute(self.db, self.run, initial["claim_revision_id"]), initial["maturity"])

    def test_failed_commission_is_gap_disconfirming_measurement_is_still_tested(self):
        first = self.adopt(self.stage())
        self.db.execute("INSERT INTO v6_graph_versions VALUES(?,1,?,NULL,?,?)",
                        (self.run, "a"*64, "{}", self.plans[0]))
        self.db.execute("INSERT INTO v6_branches(branch_id,run_id,decision_ref,spec_json) "
                        "VALUES('transect',?,?, '{}')", (self.run, self.plans[0]))
        spec = json.dumps({"inputs":[self.plans[0]], "question":"measure temperature", "capability":"read@1"})
        self.db.execute("INSERT INTO v6_nodes(node_id,run_id,branch_id,version,decision_ref,spec_json,operation_key) "
                        "VALUES('broken',?,'transect',1,?,?, 'broken')", (self.run, self.plans[0], spec))
        node = self.db.execute("SELECT * FROM v6_nodes WHERE node_id='broken'").fetchone()
        self.assertTrue(v6_jobs._finish(self.db, node, "FAILED", reason="unavailable instrument"))
        failed, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual(failed["maturity"]["stage"], first["maturity"]["stage"])
        self.assertEqual(failed["maturity"]["direction"], "undetermined")
        self.assertEqual(failed["maturity"]["gaps"][0]["kind"], "engineering_gap")
        self.observe(7, 0)
        measured, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual((measured["maturity"]["stage"], measured["maturity"]["direction"]), ("tested", "opposed"))

    def test_forbidden_data_invalid_pointers_and_criteria_are_refused_at_stage(self):
        foreign = self.put({"other":"secret"}, "acquisition_plan")
        with self.assertRaises(ScopeDenied):
            self.stage(prediction={**self.prediction,"data_refs":[foreign]})
        for field, value in (("observable","/bad~3"), ("threshold",float("nan")), ("operator","contains")):
            with self.subTest(field=field), self.assertRaises(SchemaInvalid):
                self.stage(prediction={**self.prediction,field:value})
        invalid = dict(self.prediction)
        del invalid["measurement_schema"]
        with self.assertRaisesRegex(SchemaInvalid,"observable/units"):
            self.stage(prediction=invalid)
        self.assertEqual(v6_claims.current(self.db,self.run), [])

    def test_scientific_scope_or_rule_changed_after_result_starts_another_registration(self):
        first = self.adopt(self.stage())
        self.observe(11, 0)
        tested, = v62_maturity.reassess(self.db, self.run)
        self.allow(tested["claim_revision_id"])
        replacement = self.stage(key="narrow-after-result", previous_claim_ref=tested["claim_revision_id"],
                                 scope="only transect A", prediction={**self.prediction, "data_refs": self.plans[:1]})
        posthoc = self.adopt(replacement)
        self.assertGreater(posthoc["maturity"]["registration"]["event_id"], first["maturity"]["registration"]["event_id"])
        self.assertTrue(posthoc["maturity"]["preexisting_result"])
        self.assertEqual(v6_claims.read_revision(self.db, self.run, first["claim_revision_id"]), first)


    def test_repacked_descriptor_points_to_preexisting_measurement_and_wrong_population_does_not(self):
        known = self.observe(11.5, 0)
        repacked = self.put({"schema":"field-plan/1", "id":"A-prime"}, "acquisition_plan")
        self.observe(0, 0, label="repacked-plan", raw_ref=repacked)
        self.allow(repacked)
        late = self.adopt(self.stage(prediction={**self.prediction, "data_refs":[repacked]}))
        self.assertTrue(late["maturity"]["preexisting_result"])
        self.assertNotIn(known["artifact_ref"], json.dumps(late["maturity"]))
        self.observe(25, 1, label="summer", acquisition={"applicability":"summer calibration"})
        spring = self.adopt(self.stage(key="spring", prediction={**self.prediction, "data_refs":[self.plans[1]]}))
        self.assertEqual(spring["maturity"]["stage"], "preregistered")
        right = self.observe(8, 1, label="spring")
        changed = {r["hypothesis_id"]: r for r in v62_maturity.reassess(self.db, self.run)}
        checked = changed[spring["hypothesis_id"]]["maturity"]
        self.assertEqual((checked["stage"], checked["direction"], checked["opposing_refs"]),
                         ("tested", "opposed", [right["artifact_ref"]]))

    def test_identical_measurement_bytes_cannot_distinguish_independent_reacquisition_from_an_alias(self):
        self.adopt(self.stage())
        doc = {"schema":self.metric, "measurement":{"value":12.2}}
        first = self.put(doc, "field_observation", alias="first")
        second = self.put(doc, "field_observation", alias="second")
        self.observe(12.2, 0, raw_ref=first)
        self.observe(12.2, 1, raw_ref=second)
        tested, = v62_maturity.reassess(self.db, self.run)
        self.assertEqual(tested["maturity"]["stage"], "tested")
        self.assertEqual(tested["maturity"]["replication"][0]["relation"], "ambiguous_identical_bytes")
        copy = self.put(doc, "field_observation", alias="reuploaded-a")
        sample_a = {key:key+"-A" for key in v62_maturity.DEPENDENCIES}
        self.observe(12.2, 0, label="new-name", raw_ref=copy, acquisition={**sample_a, "processing":"SOP-1"})
        self.assertEqual(v62_maturity.reassess(self.db, self.run), [])

    def test_earlier_identical_scalar_on_independently_attested_unrelated_sampling_is_excluded(self):
        prior_plan = self.put({"transect":"other-season"}, "acquisition_plan")
        common = {"schema":self.metric, "measurement":{"value":12}}
        first = self.put(common, "field_observation", alias="old-reader")
        self.observe(12, 0, plan=prior_plan, raw_ref=first, label="old-site",
                     acquisition={"applicability":"previous season"})
        registered = self.adopt(self.stage())
        self.assertEqual(registered["maturity"]["stage"], "preregistered")
        new = self.put(common, "field_observation", alias="spring-reader")
        self.observe(12, 0, raw_ref=new)
        checked, = v62_maturity.reassess(self.db,self.run)
        self.assertEqual(checked["maturity"]["stage"], "tested")
        measurement, = checked["maturity"]["results"]
        self.assertLess(measurement["first_bytes_event_id"],registered["maturity"]["registration"]["event_id"])
        self.assertGreater(measurement["first_event_id"],registered["maturity"]["registration"]["event_id"])

    def test_textual_review_lends_direction_only_to_its_proposition(self):
        claim = {"statement":"The signal rises", "scope":self.scope, "maturity_lineage":["p@1"]}
        review = {"source_class":"reviewed_source_evidence", "record_nature":"formal_evidence",
                  "verdict":"SUPPORTED", "direction":"opposes", "applicability":self.scope,
                  "claim":"The signal rises", "target_claim_ref":"q@1"}
        self.assertFalse(v62_maturity.qualified_ground(review, claim))
        self.assertTrue(v62_maturity.qualified_ground({**review,"target_claim_ref":"p@1"}, claim))
        self.assertTrue(v62_maturity.qualified_ground({**review,"target_claim_ref":None}, claim))
        self.assertFalse(v62_maturity.qualified_ground({**review,"target_claim_ref":None},
                                                      {**claim,"statement":"The signal falls"}))


class ComputedPredictions(unittest.TestCase):
    def test_same_pointer_for_real_sibling_outputs_uses_pinned_output_and_distinct_observation_type(self):
        self._sibling_decisions(historical_replay=True)

    def test_typed_sibling_measurements_keep_unknown_launch_order_separate(self):
        self._sibling_decisions(historical_replay=False)

    def _sibling_decisions(self, *, historical_replay):
        from gx_core.p0 import v6_decisions
        from tests.test_v6_decisions import bind_native_fixture, branch, draft
        from tests.test_v62_outputs import METHOD, SCOPE, acquired, start_series_goal
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db = self.enterContext(closing(store.open_store(tmp / "gx.db")))
        if historical_replay:
            run, raw = start_series_goal(self, db, tmp, effort="medium", historical_replay=True)
        else:
            # For a current Goal, the instrument declares its metric before registration.
            import shutil
            from tests import test_v62_outputs as instrument
            fixture = tmp / "measured-series"
            shutil.copytree(instrument.FIXTURE, fixture)
            card = fixture / "series.json"
            package = json.loads(card.read_bytes())
            for output in package["manifest"]["outputs"][:2]:
                output["metric_specs"] = {"reading": {"op": "decimal/1", "reader": "json/1",
                    "pointer": "/value", "unit": "1", "places": 2}}
            card.write_text(json.dumps(package))
            with patch.object(instrument, "FIXTURE", fixture):
                run, raw = start_series_goal(self, db, tmp, effort="medium")
        v6_goal.record_observation(db, run, raw, source="field-sensor", acquisition=acquired("s1"))
        call = bind_native_fixture(db, run, tmp)
        delegation = db.execute("SELECT context_ref,visible_refs_json FROM v6_decision_tasks WHERE task_id=?",
                                (call["task_id"],)).fetchone()
        refs = [raw]
        comparisons = [
            ("mean", "analysis", "mean.json"),
            ("forecast", "model_prediction", "next.json"),
            ("observed", "experimental_observation", "next.json"),
        ]
        for tag, target, output in comparisons:
            prediction = {"data_refs":[raw], "observable":"/value", "operator":"gt", "threshold":5,
                          "target":target, "method":{"capability":METHOD, "output":output}}
            claim = dict(statement=tag, scope=SCOPE, criteria="More than five on the named instrument-series output",
                         level="open", evidence_refs=[raw], rationale="Preregister the declared output.",
                         limitations="This series only.", prediction=prediction)
            staged = v6_claims.stage(db, **{**call, "call_id":tag}, context_ref=delegation["context_ref"],
                                     allowed_refs=json.loads(delegation["visible_refs_json"]), payload=claim)
            refs.append(staged["claim_ref"])
        node = dict(op="add_node", branch="local:sample", node="local:both", capability=METHOD,
                    inputs=[raw], parameters={}, depends_on=[], question="Measure the mean and forecast the next reading.")
        decision = draft({**branch(),"budget_limits":{"job_submissions":1,"cpu_seconds":60}},node)
        adopted = v6_decisions.adopt(db, **call, decision={**decision,"evidence_refs":refs})
        self.assertEqual({r["maturity"]["stage"] for r in adopted["claim_revisions"]}, {"preregistered"})
        # Freeze a second Lead task before dispatch, then submit its claim after the *actual*
        # detached launch and before GX has received any output. The scientific value is checked
        # retrospectively even if its later publication cursor makes it look prospective.
        v6_goal.record_observation(db, run, raw, source="later-instrument-review")
        next_task = v6_goal.ensure_task(db, run)
        second = bind_native_fixture(db, run, tmp, task_id=next_task)
        second_scope = db.execute("SELECT context_ref,visible_refs_json FROM v6_decision_tasks WHERE task_id=?",
                                  (second["task_id"],)).fetchone()
        from gx_core.jobs import executor
        real_launch = executor.launch

        def register_during_launch(directory):
            identity = real_launch(directory)
            pred = {"data_refs":[raw], "observable":"/value", "operator":"gt", "threshold":5,
                    "target":"analysis", "method":{"capability":METHOD, "output":"mean.json"}}
            late = dict(statement="after dispatch", scope=SCOPE, criteria="More than five on this named output",
                        level="open", evidence_refs=[raw], rationale="Concurrent post-launch registration",
                        limitations="First production is unknown.", prediction=pred)
            staged = v6_claims.stage(db, **{**second,"call_id":"late"}, context_ref=second_scope["context_ref"],
                                     allowed_refs=json.loads(second_scope["visible_refs_json"]), payload=late)
            v6_decisions.adopt(db, **second, decision={**draft(),"evidence_refs":[raw,staged["claim_ref"]]})
            return identity

        with patch.object(executor, "launch", side_effect=register_during_launch):
            v6_jobs.advance(db, run)
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            v6_jobs.advance(db, run)
            status, = db.execute("SELECT status FROM v6_nodes WHERE node_id=?", (adopted["aliases"]["local:both"],)).fetchone()
            if status in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(.05)
        self.assertEqual(status, "SUCCEEDED")
        v6_goal.ensure_task(db, run)
        actual = {r["statement"]:r["maturity"] for r in v6_claims.current(db, run)}
        self.assertEqual((actual["mean"]["stage"],actual["mean"]["direction"],actual["mean"]["results"][0]["value"]),
                         ("tested","opposed",3))
        self.assertEqual((actual["forecast"]["stage"],actual["forecast"]["direction"],actual["forecast"]["results"][0]["value"]),
                         ("tested","supported",7))
        self.assertEqual(actual["observed"]["stage"], "preregistered")
        self.assertFalse(actual["observed"]["results"])
        self.assertIn("nature_mismatch",{g["kind"] for g in actual["observed"]["gaps"]})
        late = actual["after dispatch"]
        expected = "opposed" if historical_replay else "undetermined"
        self.assertEqual((late["stage"], late["direction"]), ("grounded", expected))
        uncertain, = [g for g in late["gaps"] if g["kind"] == "unverified_production_order"]
        self.assertEqual((uncertain["supports"], uncertain["value"]), (False, 3))
        self.assertLess(uncertain["launch_event_id"],late["registration"]["event_id"])
        self.assertGreater(uncertain["first_event_id"],late["registration"]["event_id"])


if __name__ == "__main__":
    unittest.main()
