"""Compare the effective scientific grants of two deep Leads using a new, public sensor series."""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from acceptance.v62a import interleavings_fixture as fixture
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError
from gx_core.p0 import v6_native, v62_native

METHOD = "artifact-analysis@1"
READ_ONLY = "fixture-grid@1"
COMMISSIONS = {"gx_open_module", "gx_cancel_module"}
GOAL = "Measure the mean of the supplied local sensor series under the registered method."


def refused(call):
    try:
        call()
    except GxError as exc:
        return {"code": exc.code, "reason": str(exc)}
    raise AssertionError("GX accepted an operation outside the grant")


def materials(path):
    spec = {"script": "{'average': mean(data['A'])}", "output_schema": {
        "type": "object", "properties": {"average": {"type": "number"}},
        "required": ["average"], "additionalProperties": False}}
    files = (("method.json", "method_spec", json.dumps(spec)),
             ("trial.json", "measurements", "[4,8,12]"),
             ("guide.md", "literature", "# Method note\n\nThe instrument reports whole units on each successive visit.\n"))
    out = []
    for name, role, raw in files:
        file = path / name
        file.write_text(raw)
        out.append({"path": str(file), "sha256": ids.sha256_of_file(file), "role": role})
    return out


def neutral(text):
    for label in ("2RE9", "2O0O", "49/164", "149/164", "152/153"):
        assert label not in text, label


def initial(s):
    lead = s.lead
    root = s.roots._binding(lead.thread)
    settings = s.roots._settings(root, new=True)
    tool_specs = s.roots._tools_for(root)
    tools = {t["name"] for t in tool_specs}
    common_specs = sorted((t for t in tool_specs if t["name"] not in COMMISSIONS), key=lambda t:t["name"])
    brief = v6_native.brief(root)
    spec, series, note = s.materials
    prompt = settings["developerInstructions"]
    neutral(prompt + root["brief"])
    # A real artifact in the same Run, whose bytes were never in the frozen Lead grant.
    marker = "SYNTHETIC-PRIVATE-TEST-MARKER"
    hidden = ArtifactService(s.conn).publish_bytes(namespace=s.run_id+"/private", rel_path="marker.txt",
        data=marker, kind="raw_source", media_type="text/plain", run_id=s.run_id).artifact_ref
    directory = []
    offset = 0
    while offset is not None:
        page = s.call(lead, "gx_query_catalog", {"kind": "method", "offset": offset}, f"methods-{offset}")
        directory.extend(page["items"])
        offset = page["next_offset"]
    assert {m["capability_version"] for m in directory if m["execution_granted"]} == {METHOD}
    method = s.call(lead, "gx_query_catalog", {"kind": "method", "reference": METHOD}, "method")
    assert method["execution_granted"] is True
    card = s.call(lead, "gx_read_artifact", {"ref": method["source_ref"]}, "read-card")
    assert card["capability_version"] == METHOD
    second = s.call(lead, "gx_query_catalog", {"kind": "method", "reference": READ_ONLY}, "other-card")
    assert second["execution_granted"] is False
    assert s.call(lead, "gx_read_artifact", {"ref": series}, "read-series") == [4,8,12]
    assert s.call(lead, "gx_read_artifact", {"ref": spec}, "read-recipe")["script"] == "{'average': mean(data['A'])}"
    source = s.call(lead, "gx_read_source", {"source_ref": note, "segment_id": "p2"}, "read-note")
    assert "whole units" in source["text"]
    foreign = refused(lambda: s.call(lead, "gx_read_source", {"source_ref": hidden}, "not-granted"))
    raw = refused(lambda: s.call(lead, "gx_read_artifact", {"ref": hidden}, "raw-not-granted"))
    assert "SCOPE_DENIED" == foreign["code"] == raw["code"]
    assert marker not in str((source, foreign, raw, card, brief, prompt))
    neutral(json.dumps((card, second, foreign, raw)))
    node = s.node(lead, "forbidden", spec, capability=READ_ONLY, branch_id="local:trial")
    bad = refused(lambda: s.call(lead, v6_native.DECIDE, {"decision": fixture.draft(fixture.branch("trial"), node)}, "ungranted-method"))
    return {"run_id": s.run_id, "thread_id": lead.thread, "task_id": lead.task_id,
            "context_ref": s.conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (lead.task_id,)).fetchone()[0],
            "frozen_brief_sha256": ids.sha256_of_obj(brief),
            "public_method_ids": sorted(m["capability_version"] for m in directory),
            "public_methods_sha256": ids.sha256_of_obj(directory), "common_tools_sha256": ids.sha256_of_obj(common_specs),
            "native_tools": sorted(tools), "native_model": settings["model"],
            "model_effort": settings["config"]["model_reasoning_effort"],
            "prompt_sha256": ids.sha256_of_obj(prompt),
            "common_prompt_sha256": ids.sha256_of_obj(prompt.replace(v62_native.LEAD_INSTRUCTIONS, "")),
            "policy": brief["evidence_policy"], "research_effort": brief["research_effort"],
            "materials": sorted(m["sha256"] for m in brief["materials"]),
            "granted_methods": [c["capability_version"] for c in brief["capabilities"]],
            "read_receipts": {"method_ref": method["source_ref"], "card_sha256": ids.sha256_of_obj(card),
                              "source_ref": note, "segment_id": source["segment_id"], "source_receipt_id": source["receipt_id"]},
            "public_card_without_execution": {"method": READ_ONLY, "artifact_ref": second["source_ref"]},
            "rejections": {"outside_grant": foreign, "private_artifact": raw, "ungranted_execution": bad},
            "private_negative_control": {"ref": hidden, "sha256": ArtifactService(s.conn).describe(hidden)["sha256"]}}


def measure(s, delegated):
    lead = s.lead
    spec, series, note = s.materials
    if delegated:
        payload = s.open_args("inspect", inputs=[spec, series], caps=[METHOD])
        payload.update(module_version="2", expected_delivery="Measure the sensor series under the registered method.")
        opened = s.call(lead, "gx_open_module", payload, "commission")
        worker = s.bind(opened["task_id"])
        row = s.roots._binding(worker.thread)
        prompt = s.roots._settings(row, new=True)["developerInstructions"] + row["brief"]
        neutral(prompt)
        denied = refused(lambda: s.call(worker, "gx_read_artifact", {"ref": note}, "outside-module"))
        s.call(worker, "gx_read_artifact", {"ref": series}, "worker-read")
        s.call(lead, v6_native.DECIDE, {"decision": fixture.draft()}, "await-survey")
        node = s.node(worker, "measure", spec, capability=METHOD)
        operations = [node]
        actor = worker
    else:
        node = s.node(lead, "measure", spec, capability=METHOD, branch_id="local:test")
        operations = [fixture.branch("test"), node]
        actor = lead
    node.update(inputs=[spec, series], question="What is the sample mean under the supplied method?")
    ack = s.call(actor, v6_native.DECIDE, {"decision": fixture.draft(*operations,
                 watches=[{"event": "job_settled", "subject_ref": "local:measure"}])}, "authorize-job")
    result = s.settle(ack["aliases"]["local:measure"])
    if delegated:
        worker = s.next_actor(opened["scope"])
        assert s.call(worker, "gx_read_artifact", {"ref": result["result_ref"]}, "read-output") == {"average":8.0}
        published = s.report(worker, result["result_ref"], findings=[{
            "text": "The linked output reports the mean for the assigned readings.", "source_refs": [result["result_ref"]]}])
    next_lead = s.next_actor("run")
    if delegated:
        assert published["report_ref"] in s.brief(next_lead)["evidence_refs"]
    value = s.call(next_lead, "gx_read_artifact", {"ref": result["result_ref"]}, "lead-read-output")
    assert value == {"average":8.0}
    row = s.roots._binding(next_lead.thread)
    tools = {t["name"] for t in s.roots._tools_for(row)}
    assert "gx_delegate_branch" not in tools
    denied_branch = refused(lambda: s.call(next_lead, "gx_delegate_branch", {
        "branch_id": result["branch_id"], "question": "Start an additional branch decision"}, "ungranted-worker"))
    return {"job_id": result["job_id"], "result_ref": result["result_ref"], "result_sha256": ids.sha256_of_obj(value),
            "mean": value["average"], "physical_jobs": len(s.rows("SELECT job_id FROM jobs WHERE run_id=?",s.run_id)),
            "branch_delegation_refused_after_open": denied_branch,
            **({"module_scope": opened["scope"], "module_private_read_refusal": denied,
                "report_ref": published["report_ref"]} if delegated else {})}


def inapplicable(path, files):
    method = "deposited-chain-geometry@2"
    settings = {"research_effort": "deep", "research_modules": [], "branch_delegation": False,
                "plugins": ["org.gx.protein@0.3.0"], "capabilities": [method], "input_files": files}
    with patch.object(fixture, "GOAL", "Determine whether the assigned sensor file fits this method."), fixture.Scenario(path, spec_updates=settings) as s:
        card = s.call(s.lead, "gx_query_catalog", {"kind": "method", "reference": method}, "conditions")
        full = s.call(s.lead, "gx_read_artifact", {"ref": card["source_ref"]}, "read-conditions")
        assert "mmCIF" in full["method"]["mechanism"] and "coverage" in full["outputs"][0]["metric_specs"]
        group = fixture.branch("applicability")
        group["budget_limits"]["cpu_seconds"] = 35
        group.update(hypothesis="The supplied file could satisfy the method contract.",
                     alternative="Required coordinate fields could be absent.",
                     discriminating_observation="The registered reader checks its input format.")
        node = s.node(s.lead, "input-check", s.materials[1], capability=method,
                      parameters={"mask": "common_ca"}, branch_id="local:applicability")
        receipt = s.call(s.lead, v6_native.DECIDE, {"decision": fixture.draft(group, node)}, "attempt")
        node_id = receipt["aliases"]["local:input-check"]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            s.advance()
            [row] = s.rows("SELECT status,job_id,result_ref FROM v6_nodes WHERE node_id=?", node_id)
            if row["status"] == "FAILED":
                break
            time.sleep(.04)
        assert row["status"] == "FAILED" and row["result_ref"] is None, row
        stderr = s.state / "jobs" / row["job_id"] / "stderr.log"
        message = stderr.read_text().splitlines()[-1]
        assert "Expected a single mmCIF entry and one tabular atom_site loop" in message
        neutral(json.dumps([full, message]))
        return {"run_id": s.run_id, "method": method, "input_sha256": files[1]["sha256"],
                "method_ref": card["source_ref"], "method_card_sha256": ids.sha256_of_obj(full),
                "job_id": row["job_id"], "status": row["status"], "result_ref": row["result_ref"],
                "reason": message, "stderr_sha256": ids.sha256_of_file(stderr)}


def run(path):
    path = path.resolve()
    if path.exists() or path.is_relative_to(ROOT):
        raise ValueError("Supply a fresh directory outside the checkout")
    path.mkdir(parents=True,mode=0o700)
    files = materials(path)
    rows = {}
    with patch.object(fixture, "GOAL", GOAL):
        for name, selected in (("single", []), ("delegated", ["data_inventory@2"])):
            spec = {"research_effort": "deep", "research_modules": selected, "branch_delegation": False,
                    "plugins": [], "capabilities": [METHOD], "input_files": files}
            with fixture.Scenario(path / name, spec_updates=spec) as s:
                entry = rows[name] = initial(s)
                entry.update(measure(s, name == "delegated"))
                assert entry["physical_jobs"] == 1
    ls, rs = (set(entry["native_tools"]) for entry in rows.values())
    assert not ls - rs and rs - ls == COMMISSIONS
    for field in ("policy", "research_effort", "native_model", "model_effort", "granted_methods", "materials",
                  "common_prompt_sha256", "public_methods_sha256", "common_tools_sha256", "result_sha256"):
        assert rows["single"][field] == rows["delegated"][field], field
    assert rows["single"]["read_receipts"]["card_sha256"] == rows["delegated"]["read_receipts"]["card_sha256"]
    receipt = {"case":"deep_scientific_access", "status":"PASS", "paid_model_calls":0, "provider_tokens":None,
               "host":"GX native Roots callbacks with local scripted transport; public Goal CLI and CPU JobService",
               "extra_tools_in_delegated":sorted(rs-ls),"cases":rows,
               "unfit_input":inapplicable(path/"outside-method", files)}
    fixture.save(path/"result.json",receipt)
    return receipt


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    print(run(args.work)["status"])
