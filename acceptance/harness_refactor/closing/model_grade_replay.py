"""Read-only calibration replay: verify the model, reference and separate two-input score against their actual Jobs."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from gx_core import config
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v6_claims


def replay(state):
    with closing(sqlite3.connect((state/"gx.db").resolve().as_uri()+"?mode=ro",uri=True)) as conn, \
            patch.object(config,"RUNTIME_VAR_DIR",state):
        conn.row_factory = sqlite3.Row
        judgment = conn.execute("""SELECT j.run_id,m.completion_json FROM jobs j JOIN p0_execution_manifests m
            USING(job_id) WHERE j.capability_id='mapped-structure-agreement' AND j.status='SUCCEEDED'
            ORDER BY j.created_at LIMIT 1""").fetchone()
        score = next(item for item in json.loads(judgment["completion_json"])["outputs"]
                     if item["output_name"] == "agreement.json")
        inputs = {item["role"]:item["artifact_ref"] for item in score["source_inputs"]}
        task = {"run_id":judgment["run_id"], "scope":"run",
                "event_cursor":conn.execute("SELECT max(event_id) FROM events").fetchone()[0]}
        check = {"grade":"model_evaluation_checked", "source_ref":inputs["prediction"],
                 "reference_ref":inputs["reference"], "evaluation_ref":score["artifact_ref"]}
        def grade(check):
            return v6_claims.evidence_grade(conn,task,{"evidence_refs":[check[key] for key in check if key.endswith("_ref")],
                                                       "evidence_check":check})
        proof = grade(check)
        assert proof["prediction_job"] != proof["evaluation_job"]
        try:
            grade({**check,"reference_ref":inputs["mapping"]})
        except SchemaInvalid as mismatch:
            refusal = str(mismatch)
        else:
            raise AssertionError("Coordinate correspondence file was incorrectly classified as an observed reference")
        assert not conn.total_changes
    return {"mode":"read-only replay of original model and scoring Jobs", "run_id":task["run_id"],
            "verified":proof, "mapping_as_observation_refused":refusal, "new_jobs":0,"new_model_turns":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state",type=Path)
    print(json.dumps(replay(parser.parse_args().state.resolve()), indent=2))
