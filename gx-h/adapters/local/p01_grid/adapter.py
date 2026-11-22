"""GX local parser for the deterministic fixture-grid capability."""
import json


ENTRYPOINT_PROBE = {
    "argv": ["{env.python}", "-I", "-B", "{entry_script}", "--help"],
    "ok_exit_codes": [0], "expect": "--grid", "timeout_seconds": 30,
}


def smoke_request():
    grid = {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
            "candidates": [{"candidate_id": "smoke", "x": 2}]}
    return {"inputs": {"grid": {"filename": "grid.json", "text": json.dumps(grid)}},
            "parameters": {"operation": "smoke"}}


def malformed_request():
    return {"inputs": {"grid": {"filename": "grid.json", "text": "{}"}},
            "parameters": {"operation": "malformed"}}


def validate_inputs(inputs, parameters):
    with open(inputs["grid"], encoding="utf-8") as stream:
        grid = json.load(stream)
    if grid.get("synthetic") is not True or not grid.get("candidates"):
        return ["expected a nonempty synthetic grid"]
    return []


def parse(job):
    # JobService publishes the required raw file from the manifest.
    return {}


def classify_failure(job):
    return None
