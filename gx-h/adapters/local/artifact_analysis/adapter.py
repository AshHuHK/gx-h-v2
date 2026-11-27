"""Input framing for Artifact Analysis. Supplied expressions execute only in the isolated worker."""

ENTRYPOINT_PROBE = {"argv": ["{env.python}", "-I", "-B", "{entry_script}", "--help"],
                    "ok_exit_codes": [0], "expect": "--inputs", "timeout_seconds": 15}


def smoke_request():
    import json
    schema = {"type": "object", "properties": {"n": {"type": "integer"}},
              "required": ["n"], "additionalProperties": False}
    return {"inputs": {"spec": {"filename": "spec.json", "text": json.dumps(
                    {"script": "{'n': count(data['measurements'])}", "output_schema": schema})},
                       "input_measurements": {"filename": "data.json", "text": "[2,3,4]"}}, "parameters": {}}


def malformed_request():
    control = smoke_request()
    control["inputs"]["spec"]["text"] = ""
    return control


def validate_inputs(inputs, parameters):
    from pathlib import Path
    if parameters or "spec" not in inputs or not 0 < Path(inputs["spec"]).stat().st_size <= 16384:
        return ["supply a bounded program artifact and at least one named dataset"]
    return []


def parse(job):
    return {}


def classify_failure(job):
    return None
