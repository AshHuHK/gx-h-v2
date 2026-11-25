"""Replay the independent scientific controls through offline Goal and observation entry points."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PRODUCT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(PRODUCT), str(PRODUCT / "gx-h")]

from acceptance.v62a.interleavings_fixture import QUALITY, Scenario, draft
from gx_core.p0 import v6_claims, v62_maturity

METRIC = "public.thermometer.celsius/1"
POPULATION = "morning samples in a registered survey"


def check(truth, explanation):
    if not truth:
        raise AssertionError(explanation)


def register(scenario, refs):
    claim = {
        "level": "open", "statement": "Readings exceed ten degrees in the registered survey.",
        "scope": POPULATION, "criteria": "Temperature in degrees Celsius greater than ten.",
        "limitations": "Restricted to the registered sampling frame and instrument.",
        "rationale": "Choose a decision rule before measuring the registered samples.",
        "evidence_refs": [refs[0]],
        "prediction": {"data_refs": refs, "observable": "/measurement/value", "operator": "gt",
                       "threshold": 10, "target": "experimental_observation", "measurement_schema": METRIC},
    }
    staged = scenario.call(scenario.lead, "gx_stage_claim", claim, "stage-before-measurement")
    decision = draft(evidence=[refs[0], staged["claim_ref"]])
    adopted = scenario.call(scenario.lead, "gx_decide_revision", {"decision": decision}, "register")
    return adopted["claim_revisions"][0]


def supply(scenario, name, body, metadata, scope="run"):
    operator = scenario.directory / "instrument"
    operator.mkdir(exist_ok=True)
    value, source = operator / (name + ".json"), operator / (name + "-metadata.json")
    value.write_text(json.dumps(body))
    source.write_text(json.dumps(metadata))
    cmd = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "observe", "--state", str(scenario.state),
           "--run-id", scenario.run_id, "--source", name, "--scope", scope,
           "--input", str(value), "--acquisition", str(source)]
    reply = subprocess.run(cmd, text=True, capture_output=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    if reply.returncode:
        raise RuntimeError(reply.stderr + reply.stdout)
    return json.loads(reply.stdout)["artifact_ref"]


def provenance(label, inputs, **extra):
    axes = {axis: axis + "-" + label for axis in ("data", "acquisition", "sample", "source", "sampling")}
    return {**axes, "publication": [], "processing": "calibrated-thermometry", "applicability": POPULATION,
            "data_refs": inputs, **extra}


def shared_bottle(path):
    path.mkdir()
    frame = path / "field-sample.json"
    frame.write_text('{"schema":"public.field-plan/1","site":"north"}')
    spec = {"research_effort": "deep", "research_modules": [],
            "input_files": [{"role": "material", "path": str(frame)}]}
    with Scenario(path / "goal", spec_updates=spec) as s:
        s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-aliquots")
        samples = [supply(s, "aliquot-" + label, {"schema": "public.intake/1", "aliquot": label},
                          provenance(label, [s.materials[0]], sample="whole-bottle-1", source="spring-runoff"))
                   for label in ("A", "B")]
        s.next_actor("run")
        first = register(s, samples)
        measured = [supply(s, f"reading-{n}", {"schema": METRIC, "measurement": {"value": 11 + n}, "tube": n},
                           provenance(f"instrument-{n}", [ref])) for n, ref in enumerate(samples)]
        s.next_actor("run")
        current, = v6_claims.current(s.conn, s.run_id)
        result = current["maturity"]
        pair, = result["replication"]
        check(first["maturity"]["stage"] == "preregistered" and result["stage"] == "tested"
              and result["direction"] == "supported", "Two reads of one bottle changed the claim incorrectly")
        check(pair["relation"] == "dependent_acquisitions" and pair["shared"]["sample"] == ["whole-bottle-1"]
              and pair["shared"]["source"] == ["spring-runoff"], "The common sample or source disappeared")
        check(set(result["supporting_refs"]) == set(measured), "Both actual instrument readings must remain cited")
    with Scenario(path / "goal", reopen=True) as cold:
        for record in (first, current):
            check(record["maturity"] == v62_maturity.recompute(cold.conn, cold.run_id, record["claim_revision_id"]),
                  "Cold reconstruction changed an immutable judgment")
        check(not v62_maturity.reassess(cold.conn, cold.run_id), "A repeat created another assessment")
    return {"registration": "preregistered", "stage": result["stage"], "direction": result["direction"],
            "pair": pair, "cold_identical": True, "duplicate_revisions": 0}


def unreleased_job(path):
    with Scenario(path) as s:
        opened = s.open("scoped-inventory", caps=[QUALITY])
        worker = s.bind(opened["task_id"])
        first = register(s, [s.materials[0]])
        accepted = s.submit_node(worker, "quality")
        job = s.settle(accepted["aliases"]["local:quality"])
        before, = v6_claims.current(s.conn, s.run_id)
        serialized = json.dumps(before["maturity"])
        check(first["maturity"]["stage"] == before["maturity"]["stage"] == "preregistered"
              and job["result_ref"] not in serialized and job["job_id"] not in serialized
              and not s.rows("SELECT event_id FROM v62_module_wakes"), "An unpublished result affected the Lead")
        reporter = s.next_actor(worker.scope)
        report = s.report(reporter, job["result_ref"], text="The quality inventory is available for review.")
        s.next_actor("run")
        after, = v6_claims.current(s.conn, s.run_id)
        gaps = [gap["kind"] for gap in after["maturity"]["gaps"] if gap.get("artifact_ref") == job["result_ref"]]
        check(gaps == ["method_or_measurement_schema_mismatch"], "Published output lacks its applicability finding")
        return {"job_status": job["status"], "job": job["job_id"], "report": report["report_ref"],
                "unreleased_result_visible": False, "after_release": gaps}


def hidden_prior(path, same_population):
    with Scenario(path) as s:
        frame = s.materials[-1]
        opened = s.open("branch-calibration", caps=[QUALITY], inputs=[s.materials[0], frame])
        worker = s.bind(opened["task_id"])
        branch = s.brief(worker)["module"]["branch_id"]
        s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-private-calibration")
        job = s.submit_node(worker, "independent-inventory", input_ref=s.materials[0])
        s.settle(job["aliases"]["local:independent-inventory"])
        scientific_scope = POPULATION if same_population else "unrelated summer calibration"
        old = supply(s, "reserved-reading", {"schema": METRIC, "measurement": {"value": 35}},
                     provenance("earlier", [frame], applicability=scientific_scope), scope=branch)
        supply(s, "survey-calendar", {"schema": "survey.schedule/1", "date": "March"}, provenance("calendar", []))
        s.next_actor("run")
        claim = register(s, [frame])
        observed = claim["maturity"]
        check(old not in json.dumps(claim) and not s.rows("SELECT event_id FROM v62_module_wakes"),
              "An unreleased result was disclosed in the registration")
        check(observed["preexisting_result"] == same_population
              and (observed["stage"] == "preregistered") != same_population,
              "Prospective status failed to follow the earlier acquisition's scientific scope")
        return {"same_population": same_population, "stage": observed["stage"],
                "preexisting": observed["preexisting_result"], "unreleased_reference_visible": False}


def main():
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--work", required=True, type=Path, help="A new directory outside the release")
    work = options.parse_args().work.resolve()
    if work.is_relative_to(PRODUCT):
        options.error("Place the four independent Goal states outside the product checkout")
    work.mkdir(parents=True, exist_ok=False)
    results = {"shared_bottle": shared_bottle(work / "shared-bottle"),
               "unreleased_job": unreleased_job(work / "unreleased-job"),
               "hidden_same_population": hidden_prior(work / "hidden-same-population", True),
               "hidden_different_population": hidden_prior(work / "hidden-different-population", False)}
    print(json.dumps({"scripted_native_transport": True, "provider_calls": 0, "cases": results}, indent=2))


if __name__ == "__main__":
    main()
