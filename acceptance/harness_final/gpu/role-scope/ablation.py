"""Replay the original B claim revisions against four isolated in-memory counterfactuals.

Example
    python -B ablation.py --release /path/to/food-release-362157ae... \
        --state /path/to/food-P00711-01/state

Run with the Python environment used by the GX installation. Output is JSON; the
private Run, its artifacts, and the installed GX code are opened only for reading.
The counterfactuals specifically demonstrate which condition rejected the *same*
archived endpoint. They are neither an evaluator nor a change to the closed Run.
"""
from __future__ import annotations

import argparse
from contextlib import closing, ExitStack
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
RUN = "gxr_01m8g0ad2w0vh088y0"
REGISTERED_CLAIMS = ("gxh_01m8g0d369stvr26rk@3", "gxh_01m8g0v8vf68pvj773@2")
TARGET_REGISTRATION = 114
SCORE_REF = "gxa_01m8g0v9x00tph5xwy"


def check_sealed_tuple(conn, manifest, run, source, registration):
    decision = conn.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND event_id=? AND kind='v6.claim.adopted'",
        (run, registration),
    ).fetchone()
    event = json.loads(decision["payload_json"])
    row = conn.execute(
        "SELECT request_json FROM v6_decisions WHERE decision_ref=? AND run_id=?",
        (event["decision_ref"], run),
    ).fetchone()
    asset = conn.execute("SELECT job_id FROM artifacts WHERE run_id=? AND artifact_ref=? AND published=1", (run, source)).fetchone()
    sealed = manifest.read_manifest(conn, asset["job_id"])["admission"]
    capability = sealed["capability"]
    ops = json.loads(row["request_json"])["decision"]["ops"]
    nodes = [op for op in ops if op["op"] == "add_node" and op["capability"] == f"{capability['id']}@{capability['version']}"]
    if len(nodes) != 1 or sealed["owner"]["decision_ref"] != event["decision_ref"]:
        raise ValueError("The archived case does not contain one scorer from the enrolling decision.")
    if [v["artifact_ref"] for v in sealed["inputs"]] != nodes[0]["inputs"] or not set(nodes[0]["inputs"]) <= set(event["evidence_refs"]):
        raise ValueError("The frozen ordered node inputs and the claim's cited artifacts do not bind this result.")
    roles = [v["role"] for v in sealed["inputs"]]
    if roles != ["prediction", "reference", "mapping"]:
        raise ValueError("Unexpected or ambiguous registered input roles.")
    return {"registration_event": registration, "decision_ref": event["decision_ref"],
            "node": nodes[0]["node"], "capability": capability, "output_ref": source,
            "job_id": asset["job_id"], "sealed_inputs": sealed["inputs"]}


def summarize(outcome):
    return {
        "stage": outcome["stage"],
        "direction": outcome["direction"],
        "opposing_refs": outcome["opposing_refs"],
        "endpoint_rejections": [g["kind"] for g in outcome["gaps"] if g.get("artifact_ref") == SCORE_REF],
        "material_coverage": [{k: m.get(k) for k in (
            "artifact_ref", "qualification", "registration_event_id", "endpoint_first_available_event_id"
        )} for m in outcome.get("material_coverage", [])],
    }


def run_case(conn, maturity, measurement, run, *, project_scope, bind_role):
    original_match = measurement.match
    original_applies = maturity._applies

    def match(conn, reg, fact, **kwargs):
        selected = (reg["event_id"] == TARGET_REGISTRATION and fact.get("artifact_ref") == SCORE_REF
                    and reg["method_pin"]["measurement_spec"]["input_role"] == "reference")
        if not selected:
            return original_match(conn, reg, fact, **kwargs)
        scope = reg["method_pin"]["measurement_spec"]["applicability"]
        if project_scope and scope in fact["applicability_scopes"]:
            # Analysis only: leave the physical output and all input pins untouched.
            fact = {**fact, "applicability_scopes": [scope]}
        error, reading = original_match(conn, reg, fact, **kwargs)
        if reading and bind_role:
            selected_inputs = [i for i in reading["source_inputs"] if i["role"] == "reference"]
            if len(selected_inputs) != 1 or selected_inputs[0]["artifact_ref"] != reg["data"][0]["artifact_ref"]:
                raise ValueError("The role-binding test requires the single enrolled original.")
            reading = {**reading, "material_inputs": selected_inputs}
        return error, reading

    def applies(fact, scope):
        if project_scope and fact.get("artifact_ref") == SCORE_REF and scope in fact["applicability_scopes"]:
            return True
        return original_applies(fact, scope)

    with ExitStack() as stack:
        if project_scope or bind_role:
            stack.enter_context(patch.object(measurement, "match", match))
        if project_scope:
            stack.enter_context(patch.object(maturity, "_applies", applies))
        return [dict(claim_revision_id=revision, **summarize(maturity.recompute(conn, run, revision)))
                for revision in REGISTERED_CLAIMS]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True, help="Original frozen GX checkout or installation containing gx-h/")
    parser.add_argument("--state", type=Path, required=True, help="Original B private workspace containing gx.db and artifacts/")
    opts = parser.parse_args()
    sys.path.insert(0, str(opts.release.resolve(strict=True) / "gx-h"))
    from gx_core.p0 import manifest, v622_measurement, v62_maturity, v62_outputs

    db = (opts.state / "gx.db").resolve(strict=True)
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        before = conn.total_changes
        binding = check_sealed_tuple(conn, manifest, RUN, SCORE_REF, TARGET_REGISTRATION)
        originals = [{"role": p["role"], "nature": f.get("nature"), "record_kind": f.get("record_kind"),
                      "original_inputs_grounded": f.get("original_inputs_grounded"), "scopes": f.get("applicability_scopes")}
                     for p in binding["sealed_inputs"]
                     for f in [v62_outputs.classify(conn, RUN, p["artifact_ref"], through_event=134)]]
        fact = v62_outputs.classify(conn, RUN, SCORE_REF, through_event=134)
        cases = {}
        for name, p, b in (("original", False, False), ("role_only", False, True),
                           ("applicability_only", True, False), ("applicability_and_role", True, True)):
            cases[name] = run_case(conn, v62_maturity, v622_measurement, RUN, project_scope=p, bind_role=b)
        expected = {
            "original": "out_of_scientific_scope", "role_only": "out_of_scientific_scope",
            "applicability_only": "no_individual_material_attribution",
            "applicability_and_role": "missing_original_acquisition",
        }
        for name, required in expected.items():
            if required not in cases[name][1]["endpoint_rejections"]:
                raise SystemExit(f"The original B candidate is required for this ablation: {name} did not give {required}.")
            if cases[name][0] != cases["original"][0]:
                raise ValueError(f"The distinct initial claim was accidentally changed in condition {name}.")
        if conn.total_changes != before:
            raise AssertionError("The replay altered a stored row.")
        result = {"run": RUN, "sealed_tuple": binding, "original_inputs": originals,
                  "fact_nature": fact["nature"], "fact_original_inputs_grounded": fact["original_inputs_grounded"],
                  "fact_inherited_applicability": fact["applicability_scopes"], "cases": cases,
                  "database_rows_modified": conn.total_changes - before, "new_model_calls": 0, "new_jobs": 0}
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
