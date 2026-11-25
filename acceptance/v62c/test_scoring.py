"""Causal controls use controlled event orders. The public pilot exercises the same score on official Goal runs."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import shutil
from unittest.mock import patch
from tempfile import TemporaryDirectory
import time
import unittest

from acceptance.v62c import scoring


ASSAY = {"capability": "series@1", "parameters": {"mask": "complete"}, "output_name": "coverage.json",
         "nature": "analysis", "metric_id":"coverage", "unit":"1","numerator":"/n","denominator":"/d"}
ASSIGN = {"artifact_ref":"raw","sha256":"1"*64,"event_id":5,"role":"structure"}


def example(*, delegated=False, offered=True, condition="CHANGE"):
    pin = {k: ASSIGN[k] for k in ("artifact_ref", "sha256")}
    method = {"id":"series","version":"1","sha256":"c"*64}
    count = 2 if condition=="CHANGE" else 9
    metric = dict(schema="gx.verified_metric/1",metric_id="coverage",artifact_ref="out",sha256="e"*64,
        job_id="job",output_name="coverage.json",method=method,scope={"mask":"complete"},value=count/10,
        numerator=count,denominator=10,unit="1",op="ratio/1",metric_ref="metric_a",
        selectors={"numerator":"/n","denominator":"/d"},admission_sha256="a"*64,manifest_sha256="f"*64)
    proof = dict(run_id="run",scope="module:m" if delegated else "run",job_id="job",verified_metrics=[deepcopy(metric)],
        artifact_ref="out",sha256="e"*64,document={"n":count,"d":10},source_inputs=[{**pin,"role":"structure"}],data_refs=[pin],
        original_observations=[pin],original_inputs_grounded=True,scope_authorized=True,grant_event=6 if delegated else 5,actor_cursor=8,
        commission_event=9,admission_event=10,settled_event=11,output_name="coverage.json",capability="series@1",
        parameters={"mask":"complete"},nature="analysis",method=method,admission_sha256="a"*64,completion_sha256="f"*64)
    candidate = {"metric":metric}
    if delegated:
        candidate.update(report_ref="report",scope="module:m",native_publication=True,published_event=12,
                         released_event=13,report_sources=["out"])
    plan = {"rationale":"Measure all positions if sufficient.", "event_id":1,
            "ops":[{"op":"open_branch","branch":"original"}], "decision_ref":"plan"}
    chosen = {"event_id":15, "rationale":"Restrict the scope." if condition=="CHANGE" else "Keep the plan.",
              "cited":["report" if delegated else "out"],"effects":[{"kind":"withdrawn_branch" if condition=="CHANGE" else "completed_job",
                  "branch":"original","ref":"effect"}],"preplanned_jobs":[]}
    first = {"window":"first", "cursor":0,"brief_sha256":"1", "candidates":[], "action":None}
    received = {"window":"arrival","cursor":14,"brief_sha256":"2","candidates":[candidate],"action":chosen}
    return dict(schema=scoring.SCHEMA,run_id="run",assay=ASSAY,assignment=ASSIGN,measurements={"out":proof},
                lead_windows=[first,received],initial_plan=plan,costs={"cpu_seconds":1},closed=True,missing=[],
                actor_class="SCRIPTED_REHEARSAL",offered=offered,opened=int(delegated))


def evaluate(trace, condition="CHANGE", disposition=None, **overrides):
    lock=scoring.lock_opportunity(trace)
    review={"reviewer":"public-oracle","scope_appropriate":True,"unsupported_support":False,"lock_sha256":lock["sha256"],
            "condition":condition,"disposition":disposition or condition,"prior_plan_excerpt":"Measure all positions",
            "rationale_excerpt":"Restrict" if condition=="CHANGE" else "Keep","reason":"Compare the executed route to plan.",
            "effect_ref":"effect",**overrides}
    return scoring.adjudicate(trace,lock,review)


class CausalControls(unittest.TestCase):
    def test_direct_and_report_share_science_denominator_and_both_directions(self):
        for expected in ("CHANGE","MAINTAIN"):
            for offered,delegated,label in ((False,False,"NOT_APPLICABLE"),(True,False,"NOT_USED"),(True,True,"DEMONSTRATED")):
                with self.subTest(expected=expected,delegated=delegated,offered=offered):
                    outcome=evaluate(example(offered=offered,delegated=delegated,condition=expected),expected)
                    self.assertTrue(outcome["science_pass"])
                    self.assertEqual(label,outcome["delegation"])
                    self.assertEqual(0,outcome["live_model_decisions"])

    def test_preopened_and_later_opened_with_prospective_grant_and_real_work(self):
        for grant in (5,7):
            trace=example(delegated=True)
            trace["measurements"]["out"]["grant_event"]=grant
            self.assertEqual(evaluate(trace)["delegation"],"DEMONSTRATED")
        for change in ({"scope_authorized":False},{"actor_cursor":4},{"commission_event":4},
                       {"source_inputs":[]},{"grant_event":10},{"settled_event":30},{"original_observations":[]},{"original_inputs_grounded":False}):
            trace=example(delegated=True)
            trace["measurements"]["out"].update(change)
            with self.subTest(change=change):
                self.assertEqual(scoring.lock_opportunity(trace)["state"],"NO_ELIGIBLE_DELIVERY")

    def test_novelty_is_locked_on_delivery_before_decision_and_cannot_be_replayed(self):
        t=example(delegated=True)
        t["lead_windows"].append({**deepcopy(t["lead_windows"][1]),"window":"replay","cursor":20})
        t["lead_windows"][1]["action"]=None
        self.assertFalse(evaluate(t,"CHANGE","MISSED")["science_pass"])
        self.assertEqual(scoring.lock_opportunity(t)["window"],"arrival")
        self.assertFalse(evaluate(t)["science_pass"])
        t["lead_windows"][1]["candidates"]=[]
        self.assertEqual(scoring.lock_opportunity(t)["window"],"replay")
        t["lead_windows"][2]["candidates"]=[]
        self.assertEqual(scoring.lock_opportunity(t)["state"],"NO_ELIGIBLE_DELIVERY")

    def test_posthoc_claim_wrong_method_invented_reading_and_cross_scope(self):
        for change in ({"capability":"series@2"},{"nature":"observed"},{"output_name":"other"},
                       {"run_id":"another"},{"parameters":{"mask":"different"}}):
            t=example(); t["measurements"]["out"].update(change)
            self.assertFalse(evaluate(t)["science_pass"], change)
        for change in ({"scope":"module:peer"},{"native_publication":False},{"released_event":23},
                       {"published_event":9},{"report_sources":[]},{"report_ref":None}):
            t=example(delegated=True);t["lead_windows"][1]["candidates"][0].update(change)
            self.assertFalse(evaluate(t)["science_pass"],change)
        t=example()
        t["lead_windows"][1]["candidates"][0]["metric"]["numerator"]=7
        self.assertFalse(evaluate(t)["science_pass"])
        t=example();t["lead_windows"][1]["action"]["cited"]=["unrelated"]
        self.assertEqual(evaluate(t)["opportunity"],"DELIVERED")
        self.assertFalse(evaluate(t)["science_pass"])

    def test_automatic_preplanned_and_prepared_text_do_not_get_science_or_chain_credit(self):
        t=example(delegated=True)
        turn=t["lead_windows"][1]
        turn["action"]=None;turn["automatic_nodes"]=["auto"]
        o=evaluate(t,disposition="AUTOMATIC_ONLY")
        self.assertFalse(o["science_pass"]);self.assertEqual(o["delegation"],"USED_INCOMPLETE")
        turn["candidates"][0]["metric"]={"text":"The appropriate response is to change course."}
        self.assertEqual(scoring.lock_opportunity(t)["state"],"NO_ELIGIBLE_DELIVERY")

    def test_unfinished_timeout_unenacted_and_changed_plan_do_not_pass(self):
        t=example(condition="MAINTAIN")
        for key,val in (("closed",False),("missing",["delivery"])):
            tx=deepcopy(t);tx[key]=val
            self.assertFalse(evaluate(tx,"MAINTAIN")["science_pass"])
        for e in ([],[{"kind":"completed_job","ref":"effect","branch":"other"}]):
            tx=deepcopy(t);tx["lead_windows"][1]["action"]["effects"]=e
            self.assertFalse(evaluate(tx,"MAINTAIN")["science_pass"])
        self.assertFalse(evaluate(t,"MAINTAIN",unsupported_support=True)["science_pass"])
        lock=scoring.lock_opportunity(t,prior_accommodated_excerpt="Measure all positions")
        self.assertEqual(lock["state"],"PLAN_ALREADY_ACCOMMODATED")
        with self.assertRaises(ValueError):
            scoring.lock_opportunity(t,prior_accommodated_excerpt="An invented initial plan")

    def test_unresolved_and_missed_judgments_never_demonstrate_scope_use(self):
        t=example(delegated=True)
        for disposition in ("MISSED","UNRESOLVED"):
            result=evaluate(t,disposition=disposition)
            self.assertFalse(result["action_realized"])
            self.assertEqual(result["delegation"], "USED_INCOMPLETE")

    def test_changed_lock_or_reviewer_identity_is_rejected(self):
        t=example()
        lock=scoring.lock_opportunity(t)
        lock["source_refs"]=["unrelated"]
        with self.assertRaises(ValueError):
            scoring.outcome_packet(t,lock)
        with self.assertRaises(ValueError):
            evaluate(t,reviewer="scripted-opportunity-reader")

    def test_versioned_registration_uses_product_nature_terms(self):
        for nature in ("observed","analysis"):
            self.assertEqual(scoring.validate_assay({**ASSAY,"nature":nature})["nature"],nature)
        self.assertEqual(scoring.normalize_nature("observation",scoring.LEGACY),"observed")
        for nature in ("observation","unclassified","invented","model_prediction"):
            with self.assertRaises(ValueError):
                scoring.validate_assay({**ASSAY,"nature":nature})

    def test_registration_against_real_acquisition_and_named_output(self):
        from tests import test_v62_outputs as series
        from tests.test_v62_outputs import start_series_goal, acquired, METHOD
        from tests.test_v6_decisions import bind_native_fixture, branch, draft
        from gx_core.artifacts import ArtifactService
        from gx_core.p0 import manifest, v621_metrics, v6_goal, v6_jobs, v6_decisions, v62_outputs
        from gx_core.persistence import store
        tmp=Path(self.enterContext(TemporaryDirectory())).resolve()
        db=self.enterContext(closing(store.open_store(tmp/"gx.db")))
        # Run an actual instrument export alongside an analysis of its replicate readings.
        fixture = tmp / "source"
        shutil.copytree(series.FIXTURE, fixture)
        decl = json.loads((fixture / "series.json").read_bytes())
        method_outputs = decl["manifest"]["outputs"]
        for o in method_outputs:
            o["metric_specs"] = {"coverage":{"op":"ratio/1","reader":"json/1","unit":"1","places":3,
                                             "numerator":"/n","denominator":"/d"}}
            o["source_roles"] = ["readings"]
        (fixture / "series.json").write_text(json.dumps(decl))
        (fixture / "measure.py").write_text("""import json, sys
from pathlib import Path
raw = json.loads(Path(sys.argv[1]).read_bytes())
values = raw["readings"]
mean = sum(values) / len(values)
out = Path(sys.argv[2])
(out / "optional.json").write_text(json.dumps(raw["instrument_counts"]))
(out / "mean.json").write_text(json.dumps({"n":sum(v <= mean for v in values),"d":len(values)}))
(out / "next.json").write_text(json.dumps({"n":2,"d":3}))
""")
        with patch.object(series, "FIXTURE", fixture):
            rid,ref=start_series_goal(self,db,tmp,readings={"readings":[1,3,7],"instrument_counts":{"n":1,"d":3}})
        v6_goal.record_observation(db,rid,ref,source="registered-reading",acquisition=acquired("s1"))
        node=dict(op="add_node",node="local:reading",branch="local:sample",capability=METHOD,inputs=[ref],
                  parameters={},depends_on=[],question="Compare the two named statistics.")
        receipt=v6_decisions.adopt(db,**bind_native_fixture(db,rid,tmp),decision=draft(
            {**branch(),"budget_limits":{"job_submissions":1,"cpu_seconds":60}},node))
        node_id=receipt["aliases"]["local:reading"]
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            v6_jobs.advance(db,rid)
            state,job=db.execute("SELECT status,job_id FROM v6_nodes WHERE node_id=?",(node_id,)).fetchone()
            if state in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(.03)
        self.assertEqual(state,"SUCCEEDED")
        self.assertEqual(v62_outputs.classify(db,rid,ref)["nature"], "observed")
        sealed=manifest.read_manifest(db,job)
        assets=ArtifactService(db)
        by_type={}
        for o in v62_outputs.published_outputs(db,rid,job):
            kind=o["nature"]
            if kind=="model_prediction":
                with self.assertRaises(ValueError):
                    scoring.validate_assay({**ASSAY,"nature":kind})
                continue
            rule=scoring.validate_assay({**ASSAY,"capability":METHOD,"output_name":o["output_name"],
                                         "nature":kind,"parameters":{}})
            ref=o["artifact_ref"]
            metrics=v621_metrics.from_output(db,rid,ref)
            proof={**o,"document":json.loads(assets.read_bytes(ref)), "verified_metrics":metrics,"job_id":job,
                   "method":sealed["admission"]["capability"], "completion_sha256":sealed["manifest_hash"],
                   "admission_sha256":sealed["admission_hash"]}
            self.assertTrue(scoring._rendered(metrics[0],proof,rule))
            by_type[kind]=(metrics[0]["numerator"],metrics[0]["denominator"])
        self.assertEqual(by_type, {"observed":(1,3),"analysis":(2,3)})


if __name__=="__main__":
    unittest.main()
