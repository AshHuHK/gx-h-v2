"""Live Clash acquisition of 1VYM, scripted native callbacks, and two real downstream Jobs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from acceptance.v622.test_close import CloseStudy, actual_packet, close_decision
from acceptance.v62a.interleavings_fixture import PRODUCT, draft, save
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.p0 import manifest, v62_outputs

METHOD = "deposited-chain-geometry@2"
SEARCH = "pcna-archive-search@1"
FETCH = "pcna-archive-fetch@1"
ENTRY = "1VYM"


def open_module(study, key, refs, methods, question, round_number):
    args = study.open_args(key, inputs=refs, caps=methods, round=round_number, limits={"cpu_seconds": 300})
    args.update(module_id="investigation", focus="evaluation", expected_delivery=question)
    return study.call(study.lead, "gx_open_module", args, key)


def received(study, actor, ref):
    packet = actual_packet(study, actor)
    material, = [m for m in packet["packet"]["materials"] if m["artifact_ref"] == ref]
    assert material["role"] == "structure", material
    return {"task_id": actor.task_id, "brief_sha256": packet["text_sha256"], "material": material}


def cli_entry(directory):
    request = directory / "archive-request.json"
    save(request, {"schema": "gx.pcna-archive-request/1", "accession": "P12004", "entry_ids": [ENTRY]})
    limits = {"job_submissions": 8, "cpu_seconds": 1500, "worker_tasks": 40, "api_calls": 40,
              "tokens": 60000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}
    grant = directory / "grant.json"
    save(grant, {"project": "pcna-archive-handoff-control", "research_effort": "medium",
                 "research_modules": ["investigation@1"], "plugins": ["org.gx.pcna-archive@1.0.0",
                 "org.gx.protein@0.3.0", "org.gx.investigation@1.0.0"], "capabilities": [SEARCH, FETCH, METHOD],
                 "input_files": [{"path": str(request), "role": "archive"}],
                 "run_budget": limits, "workspace_budget": limits, "turn_token_budget": 1000})
    argv = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "start", "--state", str(directory / "state"),
            "--run-key", "archive-handoff-control", "--goal", "Measure the same acquired human PCNA trimer "
            "under two coordinate masks.", "--goal-config", str(grant)]
    result = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    save(directory / "cli.json", {"argv": argv, "exit": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return argv, json.loads(result.stdout)["run_id"]


def receive_reports(study, refs):
    # A material-assignment wake may have frozen a Lead turn before a report was published.
    for _ in range(len(refs) + 2):
        lead = study.next_actor("run")
        visible = actual_packet(study, lead)["packet"]["evidence_refs"]
        if set(refs) <= set(visible):
            return lead
        study.call(lead, "gx_decide_revision", {"decision": draft()}, "await-reports")
    raise AssertionError(f"Released reports were missing from the Lead's acknowledged brief: {refs}")


def acquire(study, archive):
    scope = open_module(study, "archive", [archive], [SEARCH, FETCH], "Locate and retrieve the registered trimer.", 1)
    actor = study.bind(scope["task_id"])
    study.call(study.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-archive")
    search = study.node(actor, "search", archive, capability=SEARCH)
    fetch = study.node(actor, "fetch", search["node"], capability=FETCH, parameters={"entry_id": ENTRY})
    fetch["depends_on"] = [search["node"]]
    queued = study.call(actor, "gx_decide_revision", {"decision": draft(search, fetch, watches=[
        {"event": "job_settled", "subject_ref": fetch["node"]}])}, "locate-and-fetch")
    search_job = study.settle(queued["aliases"][search["node"]])
    assets = ArtifactService(study.conn)
    index = assets.read_json(search_job["result_ref"])
    assert index["accession"] == "P12004" and index["source_mode"] == "live_uniprot_record", index
    assert [item["entry_id"] for item in index["candidates"]] == [ENTRY], index
    fetch_job = study.settle(queued["aliases"][fetch["node"]])
    record = manifest.read_manifest(study.conn, fetch_job["job_id"])
    fetch_input, = record["admission"]["inputs"]
    assert (fetch_input["role"], fetch_input["artifact_ref"]) == ("archive", search_job["result_ref"])
    outputs = {o["output_name"]: o["artifact_ref"] for o in record["completion"]["outputs"]}
    ref = outputs["structure.cif"]
    assert assets.describe(ref)["origin"] == "JOB_OUTPUT"
    reporter = study.next_actor(scope["scope"])
    producer_brief = received(study, reporter, ref)
    context_ref = outputs["structure-context.json"]
    context = study.call(reporter, "gx_read_artifact", {"ref": context_ref}, "interpret-archive-context")
    assert context["entry_id"] == ENTRY and context["structure_sha256"] == assets.describe(ref)["sha256"]
    assert all(context[k] is None for k in ("ligand_roles", "direct_binding", "apo_status"))
    note = "The deposition records the modeled species. Ligand roles, direct binding and apo status remain unknown."
    receipt = study.report(reporter, ref, findings=[{"text": note, "source_refs": [context_ref]}],
                           artifact_refs=[ref, outputs["acquisition.json"], context_ref])
    sources = assets.read_json(receipt["report_ref"])["sources"]
    assert context_ref in {source["artifact_ref"] for source in sources}
    raw, = [source for source in sources if source["artifact_ref"] == ref]
    assert any(proof.get("job_id") == fetch_job["job_id"] for proof in raw["receipts"]), raw
    return {"module": scope["scope"], "search_job": search_job["job_id"], "search_output": search_job["result_ref"],
            "index_source": index["source"], "index_sha256": index["source_sha256"], "fetch_job": fetch_job["job_id"],
            "outputs": outputs, "producer_brief": producer_brief, "reported_context": context, "report": receipt["report_ref"]}


def examine(study, scope, mask, source, archive_report):
    actor = study.bind(scope["task_id"])
    brief = received(study, actor, source)
    report = study.call(actor, "gx_read_artifact", {"ref": archive_report}, "read-acquisition-report")
    assert source in {s["artifact_ref"] for s in report["sources"]}
    op = study.node(actor, mask, source, capability=METHOD, parameters={"mask": mask})
    decision = draft(op, evidence=[archive_report, source], watches=[{"event": "job_settled", "subject_ref": op["node"]}])
    queued = study.call(actor, "gx_decide_revision", {"decision": decision}, "measure-" + mask)
    node = study.settle(queued["aliases"][op["node"]])
    record = manifest.read_manifest(study.conn, node["job_id"])
    material, = record["admission"]["inputs"]
    assert (material["role"], material["artifact_ref"], material["sha256"]) == (
        "structure", source, brief["material"]["sha256"]), material
    current = study.next_actor(scope["scope"])
    output = study.call(current, "gx_read_artifact", {"ref": node["result_ref"]}, "read-coverage")
    assert output["mask"] == mask and output["input_sha256"] == material["sha256"]
    assert 0 < output["selected_residues"] <= output["observed_union_residues"]
    facts = v62_outputs.classify(study.conn, study.run_id, node["result_ref"])
    assert facts["nature"] == "analysis" and facts["original_inputs_grounded"]
    assert facts["original_observations"] == [{"artifact_ref": source, "sha256": material["sha256"]}]
    reported = study.report(current, node["result_ref"], findings=[],
                            metric_refs=[{"artifact_ref": node["result_ref"], "metric_id": "coverage"}])
    return {"module": scope["scope"], "brief": brief, "job": node["job_id"], "input": material,
            "result": node["result_ref"], "selected": output["selected_residues"],
            "denominator": output["observed_union_residues"], "report": reported["report_ref"]}


def check(directory, control):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    argv, run_id = cli_entry(directory)
    with CloseStudy(directory, reopen=True) as study:
        assert study.run_id == run_id
        archive, = study.materials
        initial = actual_packet(study, study.next_actor("run"))["packet"]["materials"]
        assert [material["role"] for material in initial] == ["archive"], initial
        transfer = acquire(study, archive)
        original, descriptor = (transfer["outputs"][key] for key in ("structure.cif", "acquisition.json"))
        assets = ArtifactService(study.conn)
        control_bytes = control.read_bytes()
        assert assets.read_bytes(original) == control_bytes
        metadata = assets.read_json(descriptor)
        digest = hashlib.sha256(control_bytes).hexdigest()
        assert metadata["sha256"] == digest
        fact = v62_outputs.classify(study.conn, run_id, original)
        assert (fact["nature"], fact["record_kind"], fact["original_inputs_grounded"]) == (
            "observed", "supplied_observation", True), fact
        assert fact["source_identity"]["revision"]["status"] == "verified"
        assert fact["original_observations"] == [{"artifact_ref": original, "sha256": digest}]
        report = transfer["report"]
        lead = receive_reports(study, [report])
        lead_brief = received(study, lead, original)
        context_ref = transfer["outputs"]["structure-context.json"]
        readout = study.call(lead, "gx_read_artifact", {"ref": context_ref}, "read-archive-context")
        assert readout == transfer["reported_context"]
        release = study.call(lead, "gx_read_artifact", {"ref": report}, "read-archive-report")
        assert any(context_ref in finding["source_refs"] and "apo status" in finding["text"]
                   for finding in release["findings"])

        scopes = {mask: open_module(study, mask, [original, report], [METHOD],
                                    "Measure coverage with the assigned " + mask + " mask.", 2)
                  for mask in ("common_ca", "ordered_backbone")}
        study.call(lead, "gx_decide_revision", {"decision": draft(evidence=[report])}, "commission-coverage")
        downstream = {mask: examine(study, scope, mask, original, report) for mask, scope in scopes.items()}
        assert downstream["common_ca"]["selected"] > downstream["ordered_backbone"]["selected"]
        reports = [item["report"] for item in downstream.values()]
        reviewer = receive_reports(study, reports)
        conclusion = close_decision(reports)
        conclusion["metric_refs"] = []
        conclusion["rationale"] = "The common alpha-carbon mask covers more deposited residues than the ordered-backbone mask."
        for ref in reports:
            request = {"ref": ref, "endpoint_offset": 0}
            call = "read-" + ref
            page = study.call(reviewer, "gx_read_artifact", request, call)
            study.roots.delivery.observe("item/completed", {"threadId": reviewer.thread, "turnId": reviewer.turn,
                "item": {"type": "dynamicToolCall", "tool": "gx_read_artifact", "id": call, "arguments": request,
                         "status": "completed", "success": True, "contentItems": [
                             {"type": "inputText", "text": ids.canonical_json(page)}]}})
            row, = [r for r in page["endpoint_rows"] if r["id"] == "coverage"]
            conclusion["metric_refs"].append(ref + "#" + row["key"])
        close = study.call(reviewer, "gx_decide_revision", {"decision": conclusion}, "close-control")
        assert len(close["endpoint_use"]["used_metric_refs"]) == 2
        study.advance()
        assert study.rows("SELECT status FROM v6_goals WHERE run_id=?", run_id) == [{"status": "CLOSED"}]
        jobs = study.rows("SELECT job_id,capability_id,capability_version,status,submitted_at,finished_at "
                          "FROM jobs WHERE run_id=? ORDER BY rowid", run_id)
        binding, = study.conn.execute("SELECT json_extract(inputs_json,'$.plugin_binding') FROM runs "
                                      "WHERE run_id=?", (run_id,)).fetchone()
        assert len(jobs) == 4 and all(job["status"] == "SUCCEEDED" for job in jobs), jobs
        copies = study.rows("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=? AND published=1", run_id, digest)
        assert copies == [{"artifact_ref": original}], copies
        result = {"run_id": run_id, "entry": ENTRY, "transport": "real archive Jobs using Clash HTTP CONNECT "
                  "127.0.0.1:7890; native actor replies scripted", "model_calls": 0, "initial_materials": initial,
                  "archive": transfer, "descriptor": metadata, "original_classification": fact, "lead": lead_brief,
                  "downstream": downstream, "closure": close["decision_ref"],
                  "used_endpoints": close["endpoint_use"]["used_metric_refs"], "admitted_plugins": json.loads(binding)["packages"], "jobs": jobs}
    replay = list(argv)
    replay[3] = "research"
    done = subprocess.run(replay, env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True,
                          check=True, timeout=30)
    reopened = json.loads(done.stdout)
    assert reopened["run_id"] == run_id and reopened["goal_status"] == "CLOSED", reopened
    assert reopened["invocation"]["added"] == {"jobs": 0, "host_turns": 0}, reopened
    save(directory / "research-reopen.json", reopened)
    result["reopened_run"] = reopened["run_id"]
    save(directory / "handoff.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--control", type=Path, default=PRODUCT / "examples/pcna/data/1VYM.cif")
    args = parser.parse_args()
    print(json.dumps(check(args.work.resolve(), args.control.resolve()), indent=2))
