"""Controlled native callbacks, real CPU Jobs, and a measurement-led cross-round handoff."""
import argparse
import json
from pathlib import Path

from acceptance.v621.test_verified_metrics import METHOD, SOURCE_METADATA
from acceptance.v622.test_close import CloseStudy, actual_packet
from acceptance.v62a.interleavings_fixture import draft, save
from gx_core.p0 import v6_goal


def open_investigation(study, key, inputs, expected_delivery, round_number):
    request = study.open_args(key, inputs=inputs, caps=[METHOD], round=round_number)
    request.update(module_id="investigation", focus="evaluation", expected_delivery=expected_delivery)
    return study.call(study.lead, "gx_open_module", request, key)


def measure(study, actor, original, mask, evidence=()):
    op = study.node(actor, mask, original, capability=METHOD, parameters={"mask": mask})
    op["question"] = "How much of the same original coordinate set is covered by this atom mask?"
    decision = draft(op, evidence=evidence, watches=[{"event": "job_settled", "subject_ref": op["node"]}])
    if evidence:
        decision["rationale"] = "The reported ordered-atom mask omits positions. Check a less restrictive mask."
    accepted = study.call(actor, "gx_decide_revision", {"decision": decision}, "measure-" + mask)
    return accepted, study.settle(accepted["aliases"][op["node"]])


def check(directory):
    settings = {"research_effort": "medium", "research_modules": ["investigation@1"],
                "plugins": ["org.gx.investigation@1.0.0", "org.gx.protein@0.3.0"], "capabilities": [METHOD]}
    with CloseStudy(directory, spec_updates=settings) as study:
        original = study.materials[0]
        v6_goal.record_observation(study.conn, study.run_id, original, source="original-2RE9",
                                   acquisition=json.loads(SOURCE_METADATA.read_bytes()))
        first = open_investigation(study, "ordered", [original], "Measure ordered backbone coverage.", 1)
        actor = study.bind(first["task_id"])
        study.call(study.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-ordered")
        _, ordered = measure(study, actor, original, "ordered_backbone")
        reporter = study.next_actor(first["scope"])
        ordered_ref = ordered["result_ref"]
        receipt = study.report(reporter, ordered_ref, findings=[], metric_refs=[{"artifact_ref": ordered_ref,
                                                                                "metric_id": "coverage"}])
        assert receipt["wake_event_id"]
        # Some Runs first contain a previously prepared Lead brief. Only use the brief actually delivered.
        lead = study.next_actor("run")
        if receipt["report_ref"] not in actual_packet(study, lead)["packet"]["evidence_refs"]:
            study.call(lead, "gx_decide_revision", {"decision": draft()}, "receive-new-report")
            lead = study.next_actor("run")
        reference = receipt["report_ref"]
        assert reference in actual_packet(study, lead)["packet"]["evidence_refs"]
        followup = open_investigation(study, "mask-test", [original, reference],
                                      "Test whether mask choice explains the reported omission.", 2)
        analyst = study.bind(followup["task_id"])
        assigned = actual_packet(study, analyst)["packet"]
        assert {original, reference, ordered_ref} <= {m["artifact_ref"] for m in assigned["materials"]}
        report = study.call(analyst, "gx_read_artifact", {"ref": reference}, "read-published-report")
        assert any(s["artifact_ref"] == ordered_ref for s in report["sources"])
        previous = study.call(analyst, "gx_read_artifact", {"ref": ordered_ref}, "read-published-reading")
        assert previous["mask"] == "ordered_backbone" and previous["selected_residues"] < previous["observed_union_residues"]
        study.call(lead, "gx_decide_revision", {"decision": draft(evidence=[reference])}, "commission-mask-check")
        accepted, control = measure(study, analyst, original, "common_backbone", [reference, ordered_ref])
        checked = study.next_actor(followup["scope"])
        current = study.call(checked, "gx_read_artifact", {"ref": control["result_ref"]}, "read-control")
        assert current["selected_residues"] > previous["selected_residues"]
        confirmed = study.report(checked, control["result_ref"], findings=[], metric_refs=[
            {"artifact_ref": control["result_ref"], "metric_id": "coverage"}])
        result = {"run_id": study.run_id, "transport": "scripted native callbacks and acknowledged task briefs",
                  "real_model_calls": 0, "method": METHOD, "original": original, "first": {
                      "module": first["scope"], "task": actor.task_id, "job": ordered["job_id"],
                      "selected": previous["selected_residues"], "denominator": previous["observed_union_residues"],
                      "output": ordered_ref, "report": reference, "wake_event": receipt["wake_event_id"]},
                  "followup": {"module": followup["scope"], "task": analyst.task_id, "received": [reference, ordered_ref],
                               "decision": accepted["decision_ref"], "job": control["job_id"], "mask": current["mask"],
                               "selected": current["selected_residues"], "denominator": current["observed_union_residues"],
                               "output": control["result_ref"], "report": confirmed["report_ref"]}}
        save(directory / "handoff.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().work.resolve()), ensure_ascii=False))
