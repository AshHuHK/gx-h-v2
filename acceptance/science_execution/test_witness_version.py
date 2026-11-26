"""Compare the accepted action with the actual completed Job across pinned instrument versions."""
from contextlib import closing
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v622.public.witness import _named_job
from acceptance.v623.live import action_witness
from acceptance.v62a.interleavings_fixture import branch, draft
from acceptance.v62b.study import receipts
from acceptance.v62c.witness import _action
from gx_core.native_release import native_state
from gx_core.p0 import manifest, v621_metrics, v6_goal
from gx_core.persistence import store
from tests import test_v62_outputs as instrument


CAPS = [f"instrument-series@{version}" for version in (1, 2, 3)]


def install_versions(case, directory, *, default_window=False):
    package = directory / "fixture"
    shutil.copytree(instrument.FIXTURE, package)
    original = (package / "series.json").read_text()
    if default_window:
        worker = package / "measure.py"
        source = worker.read_text()
        worker.write_text(source.replace("    main(*sys.argv[1:])",
            '    assert sys.argv.pop() == "all"\n    main(*sys.argv[1:])'))
    for version, unit in ((1, "K"), (2, "K"), (3, "mK")):
        definition = json.loads(original)
        method = definition["manifest"]
        method["version"] = str(version)
        if default_window:
            method["parameter_defaults"] = {"window": "all"}
            method["entry"]["argv_template"].append("{param.window}")
        metric = {"op":"decimal/1", "reader":"json/1", "pointer":"/value", "unit":unit, "places":2}
        if unit == "mK":
            metric.update(source_unit="K", scale=1000)
        method["outputs"][0]["metric_specs"] = {"mean":metric}
        (package / f"v{version}.json").write_text(json.dumps(definition))
    (package / "plugin.py").write_text(
        "def register(ctx):\n"
        "    for filename in ('v1.json', 'v2.json', 'v3.json'):\n"
        "        ctx.capabilities.add_manifest(filename)\n")
    start = v6_goal.start
    def grant_versions(conn, spec, **kwargs):
        return start(conn, {**spec, "capabilities":CAPS, "research_modules":[],
                            "turn_token_budget":100, "project":"witness-instrument-versions"}, **kwargs)

    work = directory / "study"
    with native_state(work / "state"), closing(store.open_store(work / "state/gx.db")) as db:
        with patch.object(instrument, "FIXTURE", package), patch.object(v6_goal, "start", grant_versions):
            instrument.start_series_goal(case, db, directory, effort="deep")
    return case.enterContext(CloseStudy(work, reopen=True))


def scorer_projection(study, binding, receipt, actor):
    """Use the historical extractor's decision, Job and effect data for one acknowledged Lead turn."""
    db, rid = study.conn, study.run_id
    jobs = receipts.job_inventory(db, study.directory, rid)
    stored = db.execute("SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                        (rid, receipt["decision_ref"])).fetchone()
    graph = receipts.decision_effect(db, rid, stored, jobs)
    action = _action(db, rid, {"decision":graph}, {j["node_id"]: j for j in jobs})
    window = {"window":"versioned-instrument", "thread_id":actor.thread, "turn_id":actor.turn,
              "accepted":receipt, "action":action, "candidates":[]}
    trace = {"run_id":rid, "binding":binding, "initial_plan":{"rationale":"Measure temperature"},
             "status":"ACTIVE", "lead_windows":[window], "closed":False, "offered":False,
             "completed_jobs":[_named_job(j) for j in jobs if j.get("result_sha256")]}
    return trace, window


class PinnedInstrumentWitness(unittest.TestCase):
    def check_same_version_defaults(self, planned, submitted):
        from acceptance.science_execution import action_effect

        s = install_versions(self, Path(self.enterContext(TemporaryDirectory())), default_window=True)
        raw, = s.materials
        route = {**branch("route"), "budget_limits": {"job_submissions": 1, "cpu_seconds": 60}}
        comparison = {"branch":"local:route", "material_role":"readings", "reference_refs":[raw],
                      "method":{"capability":CAPS[0], "parameters":planned, "output_name":"mean.json",
                                "metric_id":"mean", "unit":"K"}}
        binding = s.call(s.next_actor("run"), "gx_decide_revision",
                         {"decision":{**draft(route), "comparison_plan":comparison}}, "register")["comparison"]
        self.assertEqual(binding["method"]["parameters"], planned)
        v6_goal.record_observation(s.conn, s.run_id, raw, source="temperature-series")
        actor = s.next_actor("run")
        node = s.node(actor, "same", raw, branch_id=binding["branch"], capability=CAPS[0], parameters=submitted)
        self.assertEqual(node["parameters"], submitted)
        receipt = s.call(actor, "gx_decide_revision", {"decision":draft(node)}, "continue-comparison")
        self.assertEqual(receipt["comparison_action"]["disposition"], "MAINTAIN")
        actual = s.settle(receipt["aliases"]["local:same"])
        self.assertEqual(actual["status"], "SUCCEEDED")
        frozen = manifest.read_manifest(s.conn, actual["job_id"])
        self.assertEqual(frozen["admission"]["parameters"], {"window":"all"})
        self.assertEqual(frozen["admission"]["capability"]["version"], "1")
        end, = s.rows("""SELECT event_id FROM events WHERE run_id=? AND kind='job.transition'
                         AND subject_id=? AND new_state='SUCCEEDED'""", s.run_id, actual["job_id"])
        self.assertGreater(end["event_id"], receipt["event_id"])
        trace, window = scorer_projection(s, binding, receipt, actor)
        with patch.object(action_witness, "first_window", return_value=(window, [], "NO_ELIGIBLE_DELIVERY")):
            _, first = action_witness.reader_packet(trace)
            _, historical = action_witness.outcome_packet(trace, {}, first)
            _, corrected = action_effect.outcome_packet(s.conn, trace, {}, first)
        self.assertEqual(historical["enacted"], "CHANGE")  # Retain the frozen scorer's original result.
        self.assertEqual(corrected["enacted"], "MAINTAIN")
        self.assertTrue(corrected["action_matches_intent"])
        self.assertFalse(corrected["scientifically_interpretable"])
        effect = corrected["effect"]
        self.assertEqual((effect["job_id"], effect["ref"], effect["event_id"]),
                         (actual["job_id"], actual["node_id"], end["event_id"]))

    def test_omitted_parameter_preserves_explicit_comparison_default(self):
        self.check_same_version_defaults({"window":"all"}, {})

    def test_explicit_default_preserves_implicit_comparison_parameter(self):
        self.check_same_version_defaults({}, {"window":"all"})

    def test_version_requires_compatible_registered_endpoint_and_exact_completed_job(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        s = install_versions(self, tmp)
        raw, = s.materials
        actor = s.next_actor("run")
        route = {**branch("route"), "budget_limits":{"job_submissions":3, "cpu_seconds":60}}
        opening = {**draft(route), "comparison_plan":{
            "branch":"local:route", "material_role":"readings", "reference_refs":[raw],
            "method":{"capability":CAPS[0], "parameters":{}, "output_name":"mean.json",
                      "metric_id":"mean", "unit":"K"}}}
        registered = s.call(actor, "gx_decide_revision", {"decision":opening}, "register")["comparison"]
        v6_goal.record_observation(s.conn, s.run_id, raw, source="raw sensor readings")

        # @3 provides the same named output and metric but measures it in mK.
        # Its actual successful Job cannot demonstrate replacement of the K comparison.
        actor = s.next_actor("run")
        wrong = s.node(actor, "milli", raw, branch_id=registered["branch"], capability=CAPS[2])
        plan = draft(wrong, watches=[{"event":"job_settled", "subject_ref":"local:milli"}])
        separate = s.call(actor, "gx_decide_revision", {"decision":plan}, "measure-millikelvin")
        self.assertNotIn("comparison_action", separate)
        mismatch = s.settle(separate["aliases"]["local:milli"])
        self.assertEqual(mismatch["status"], "SUCCEEDED")

        actor = s.next_actor("run")
        replace = s.node(actor, "replacement", raw, branch_id=registered["branch"], capability=CAPS[1])
        accepted = s.call(actor, "gx_decide_revision", {"decision":draft(replace)}, "replace-version")
        self.assertEqual(accepted["comparison_action"]["disposition"], "CHANGE")
        node = accepted["aliases"]["local:replacement"]
        self.assertEqual(s.rows("SELECT status FROM v6_nodes WHERE node_id=?", node), [{"status":"PENDING"}])
        from acceptance.science_execution import action_effect
        self.assertIsNone(action_effect.verified_change(s.conn, s.run_id, registered, separate))
        relabeled = {**separate, "comparison_action":accepted["comparison_action"]}
        self.assertIsNone(action_effect.verified_change(s.conn, s.run_id, registered, relabeled))
        self.assertIsNone(action_effect.verified_change(s.conn, s.run_id, registered, accepted))
        completed = s.settle(node)
        self.assertEqual(completed["status"], "SUCCEEDED")
        self.assertNotEqual(completed["job_id"], mismatch["job_id"])
        frozen = manifest.read_manifest(s.conn, completed["job_id"])
        self.assertEqual(frozen["admission"]["capability"]["version"], "2")
        self.assertEqual(frozen["completion"]["status"], "SUCCEEDED")
        done, = s.rows("""SELECT event_id FROM events WHERE run_id=? AND kind='job.transition'
                          AND subject_id=? AND new_state='SUCCEEDED'""", s.run_id, completed["job_id"])
        self.assertGreater(done["event_id"], accepted["event_id"])
        expected = {"kind":"completed_job", "ref":node, "job_id":completed["job_id"], "event_id":done["event_id"]}
        trace, window = scorer_projection(s, registered, accepted, actor)
        # These test runs use a sensor, whereas first_window selects the specific protein pilot's
        # readings. Select only the turn under test. Every graph event and Job comes from GX.
        with patch.object(action_witness, "first_window", return_value=(window, [], "NO_ELIGIBLE_DELIVERY")):
            _, opening = action_witness.reader_packet(trace)
            _, scored = action_effect.outcome_packet(s.conn, trace, {}, opening)
        self.assertEqual(scored["enacted"], "CHANGE")
        self.assertTrue(scored["action_matches_intent"])
        self.assertFalse(scored["scientifically_interpretable"])
        effect = scored["effect"]
        self.assertEqual({k:effect[k] for k in expected}, expected)
        reading, = v621_metrics.from_output(s.conn, s.run_id, effect["result_ref"])
        self.assertEqual((reading["metric_ref"], reading["job_id"], reading["metric_id"], reading["unit"], reading["value"]),
                         (effect["metric_ref"], completed["job_id"], "mean", "K", 3))
        self.assertEqual(action_effect.verified_change(s.conn, s.run_id, registered, accepted), effect)
