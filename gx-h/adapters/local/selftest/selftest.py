"""Adapter module of the selftest utility. See the package docstring."""

ENTRYPOINT_PROBE = {
    "argv": ["{env.python}", "-I", "-B", "{entry_script}", "--help"],
    "ok_exit_codes": [0],
    "expect": "--mode",
    "timeout_seconds": 60,
}


def _request(mode, seconds, text="hello selftest\n"):
    return {"inputs": {"text": {"filename": "input.txt", "text": text}},
            "parameters": {"mode": mode, "seconds": seconds}}


def smoke_request():
    return _request("echo", 0)


def slow_request():
    request = _request("sleep", 30)
    request["wall_seconds"] = 1
    return request


def malformed_request():
    return _request("echo", 0, text="")


def validate_inputs(inputs, parameters):
    problems = []
    with open(inputs["text"], encoding="utf-8") as handle:
        if not handle.read().strip():
            problems.append("the input text is empty")
    if parameters.get("mode") not in ("echo", "sleep", "fail", "silent", "child"):
        problems.append(f"mode {parameters.get('mode')!r} is not a selftest mode")
    return problems


def parse(job):
    import os

    path = os.path.join(job["out_dir"], "report.txt")
    if not os.path.exists(path):
        # Deliberately accommodating: an adapter that returns a document whatever the tool
        # did. The backend's own check of required outputs is what a test of it relies on.
        return {"report": {"schema": "gx.selftest_report/1", "mode": "nothing-written", "characters": 0}}
    with open(path, encoding="utf-8") as handle:
        mode, _, body = handle.read().partition("\n")
    return {"report": {"schema": "gx.selftest_report/1", "mode": mode, "characters": len(body)}}


def classify_failure(job):
    return None
