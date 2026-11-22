"""A scripted assessment worker which consumes the composed grid's real tool replies."""
import json

from e2e_responses import ReadArtifact, brief_from, fixture_action, output_json, tool_history


TOOL = "gx_grid_measurement"


class MeasurementPeer:
    def __init__(self, foreign_run):
        self.foreign_run = foreign_run
        self.replies = {}
        self.assessment = None

    def __call__(self, body, request_id):
        offered = [tool.get("name") for group in body.get("tools", [])
                   for tool in (group.get("tools") or [group])]
        if TOOL not in offered:
            return fixture_action(body, request_id)
        brief = brief_from(body)
        task = brief["input"]
        if (task["round"], task["output_kind"]) != (0, "science_assessment"):
            return fixture_action(body, request_id)
        history = body["input"]
        artifacts, submitted = tool_history(history)
        if submitted:
            return fixture_action(body, request_id)
        try:
            proposal = artifacts.read_json(task["proposal_ref"])
            evidence = artifacts.read_json(task["evidence_refs"][0])
        except ReadArtifact:
            return fixture_action(body, request_id)

        info = {"round": 0, "output_kind": task["output_kind"], "task_id": brief["task_id"]}
        calls = {item["call_id"]: json.loads(item["arguments"]) for item in history
                 if item.get("type") == "function_call" and item.get("name") == TOOL}
        answered = {}
        for item in history:
            if item.get("type") != "function_call_output" or item.get("call_id") not in calls:
                continue
            arguments = calls[item["call_id"]]
            reply = output_json(item["output"])
            answered[json.dumps(arguments, sort_keys=True)] = reply
            self.replies[item["call_id"]] = {"arguments": arguments, "result": reply, "task_id": brief["task_id"]}

        def call(name, arguments):
            return ({"type": "function_call", "name": name, "arguments": json.dumps(arguments),
                     "call_id": "call-" + request_id}, {**info, "tool": name, "arguments": arguments})

        routes = evidence["routes"]
        # A real foreign Run ID is supplied by the operator test. Each attempted
        # read must still use only the current Root's assigned data and argument schema.
        for request in ({"path_id": routes[0]["path_id"], "run_id": self.foreign_run},
                        {"path_id": "outside-the-assigned-round"}):
            key = json.dumps(request, sort_keys=True)
            if key not in answered:
                return call(TOOL, request)
            if not answered[key].get("error"):
                raise ValueError("expected scoped refusal for " + repr(request))
        findings = []
        for route in routes:
            path = route["path_id"]
            request = {"path_id": path}
            key = json.dumps(request, sort_keys=True)
            if key not in answered:
                return call(TOOL, request)
            measured = answered[key]
            prediction, = [entry for entry in proposal["paths"] if entry["id"] == path]
            expected = prediction.get("expected_reaches_threshold", True)
            if not (measured["path_id"] == path and measured["proposal_ref"] == task["proposal_ref"]
                    and measured["evidence_ref"] in task["evidence_refs"]
                    and measured["prediction"] == prediction["prediction"]
                    and measured["expected_reaches_threshold"] == expected
                    and measured["job_id"] == route["job_id"] and measured["input_ref"] == route["input_ref"]
                    and measured["observations_ref"] == route["observations_ref"]
                    and measured["evaluation_ref"] == route["evaluation_ref"]
                    and measured["threshold"] == proposal["threshold"] and measured["status"] == "SUCCEEDED"):
                raise ValueError("plugin tool returned a different assignment, experiment or evaluation")
            best = measured["metrics"]["max_score"]
            meets = measured["metrics"]["count_at_or_above"] > 0
            comparison = "reached" if meets else "fell below"
            findings.append({"path_id": path, "evaluation_ref": measured["evaluation_ref"],
                             "supported": meets == expected,
                             "reason": f"Best measured score {best:g} {comparison} the threshold {proposal['threshold']:g}."})
        self.assessment = {"proposal_ref": task["proposal_ref"], "evidence_refs": task["evidence_refs"],
                           "findings": findings}
        return call("gx_submit_product", {"product": self.assessment})
