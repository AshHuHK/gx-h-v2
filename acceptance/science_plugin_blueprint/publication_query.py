"""Question-only ordinary research admission with a scripted child and live public-source Jobs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from acceptance.v62a.interleavings_fixture import PRODUCT, draft, save
from acceptance.v622.test_close import SurveyedStudy
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.p0 import manifest, v6_jobs


QUESTION = "How do researchers evaluate the accuracy of urban air pollution sensors?"
QUERY = "urban air pollution sensors"
FIRST, PAGE, ORIGINAL = "literature-query@1", "literature-page@1", "literature-original@1"


def admit(work):
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    limits = dict(job_submissions=8, cpu_seconds=900, worker_tasks=60, api_calls=52,
                  tokens=120000, gpu_jobs=0, gpu_minutes=0, wall_hours=1)
    save(work / "bounds.json", dict(project="public-literature-control", research_effort="deep",
                                   turn_token_budget=1000, run_budget=limits, workspace_budget=limits))
    command = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state", str(work / "state"),
               "--run-key", "question-alone", "--goal", QUESTION, "--goal-config", str(work / "bounds.json"),
               "--timeout", "1e-12"]
    proc = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    save(work / "admission.json", dict(command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr))
    if proc.returncode not in (0, 75):
        raise RuntimeError(proc.stderr or proc.stdout)


def run(study, receipt, result, method_version=1):
    search, continuation, original = (name.rsplit("@", 1)[0] + f"@{method_version}" for name in (FIRST, PAGE, ORIGINAL))
    lead = study.next_actor("run")
    assert not study.materials
    result.update(run_id=study.run_id, researcher_materials=study.materials)
    detail = study.call(lead, "gx_query_catalog", {"kind": "method", "reference": search}, "inspect-first")
    assert detail["spec"]["inputs"] == []
    result["first_method"] = detail["spec"]
    for revision, method in enumerate((search, continuation, original)):
        study.call(lead, "gx_select_capability", dict(action="enable", method=method, revision=revision), "enable-" + method)
    assignment = study.open_args("source-review", caps=[search, continuation, original], inputs=[],
                                 limits={"cpu_seconds": 630, "job_submissions": 6, "worker_tasks": 20,
                                         "tokens": 25000, "api_calls": 18})
    assignment.update(module_id="publication_discovery", module_version=str(method_version), focus="literature",
                      expected_delivery="Check reported methods against an actual open article and note the coverage.")
    module = study.call(lead, "gx_open_module", assignment, "open")
    result["module_instance"] = module["module_instance_id"]
    study.call(lead, "gx_decide_revision", {"decision": draft()}, "wait")
    actor = study.next_actor(module["scope"])
    branch = study.brief(actor)["module"]["branch_id"]
    assets = ArtifactService(study.conn)

    def launch(label, method, inputs, parameters):
        nonlocal actor
        local = "local:" + label
        node = dict(op="add_node", node=local, branch=branch, inputs=inputs, parameters=parameters,
                    capability=method, depends_on=[], question=QUESTION)
        answer = study.call(actor, "gx_decide_revision",
                            {"decision": draft(node, watches=[{"event":"job_settled", "subject_ref":local}])}, label)
        nid = answer["aliases"][local]
        deadline = time.monotonic() + 130
        while time.monotonic() < deadline:
            study.advance()
            row = study.conn.execute("SELECT * FROM v6_nodes WHERE node_id=?", (nid,)).fetchone()
            if row["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(.1)
        event = dict(action=label, node_id=nid, decision_ref=answer["decision_ref"], job_id=row["job_id"],
                     status=row["status"], submitted_inputs=inputs, parameters=parameters)
        result["jobs"].append(event)
        save(receipt, result)
        assert row["status"] == "SUCCEEDED", event
        proof = manifest.read_manifest(study.conn, row["job_id"])
        ports = {o["output_name"]: o["artifact_ref"] for o in proof["completion"]["outputs"]}
        event.update(ports=ports, admission_inputs=proof["admission"]["inputs"], completion_sha256=proof["manifest_hash"],
                     output_sha256={name: assets.describe(ref)["sha256"] for name, ref in ports.items()})
        save(receipt, result)
        actor = study.next_actor(module["scope"])
        return ports

    first = launch("first", search, [], {"query": QUERY})
    assert result["jobs"][0]["admission_inputs"] == []
    candidates = assets.read_json(first["candidates.json"])
    result["first_sources"] = candidates["sources"]
    if "merged" in candidates:
        result["first_merged"] = candidates["merged"]
    assert any(row["has_more"] for row in candidates["sources"]), candidates["sources"]
    try:
        launch("wrong-port", continuation, [first["candidates.json"]], {"action":"replay"})
    except SchemaInvalid as error:
        result["wrong_port"] = error.to_payload()
    else:
        raise AssertionError("A candidate list was accepted as a page snapshot")
    next_page = launch("next", continuation, [first["search-snapshot.json"]], {"action":"next"})
    replayed = launch("replay", continuation, [next_page["search-snapshot.json"]], {"action":"replay"})
    assert assets.describe(replayed["search-snapshot.json"])["sha256"] == assets.describe(next_page["search-snapshot.json"])["sha256"]
    later = assets.read_json(next_page["candidates.json"])
    result["next_sources"] = later["sources"]
    if "merged" in later:
        result["next_merged"] = later["merged"]
        replay = assets.read_json(replayed["candidates.json"])
        assert later["merged"] == replay["merged"] and later["sources"] == replay["sources"]
    for ports in (first, next_page):
        data = json.loads(assets.read_bytes(ports["candidates.json"]))
        open_ids = [pmcid for row in data["candidates"] for pmcid in row["fetchable_pmcids"]]
        if open_ids:
            break
    if not open_ids:
        raise ValueError("Neither provider page returned an open JATS candidate")
    fetched = launch("original", original, [ports["candidates.json"]], {"pmcid":open_ids[0]})
    desc = json.loads(assets.read_bytes(fetched["acquisition.json"]))
    assert desc["sha256"] == assets.describe(fetched["article.xml"])["sha256"]
    result.update(original=desc, verified=True)
    save(receipt, result)


def read_original(study, result):
    from acceptance.multi_workflow.acquired_material_handoff import receive_reports

    ref = result["jobs"][-1]["ports"]["article.xml"]
    reader = study.next_actor("module:" + result["module_instance"])
    start = 0
    while start is not None:
        listing = study.call(reader, "gx_read_source", {"source_ref":ref, "start":start}, "list-jats-" + str(start))
        body = next((row for row in listing["segments"] if row["kind"] == "body" and "/p" in row["label"]), None)
        if body:
            break
        start = listing["next_start"]
    if not body:
        raise ValueError("The acquired original has no readable body paragraph")
    page = study.call(reader, "gx_read_source", {"source_ref":ref, "segment_id":body["segment_id"]}, "read-passage")
    assert result["original"]["sha256"] == page["source_sha256"]
    finding = "The retrieved article contains a readable body paragraph. The sampled result pages do not establish exhaustive coverage."
    report = study.report(reader, ref, "retrieve-original", text=finding)
    lead = receive_reports(study, [report["report_ref"]])
    decision = draft(evidence=[report["report_ref"]])
    decision["rationale"] = "Keep the retrieved full-text item for evaluation of the measurement conditions; the sampled pages cover only part of the catalogue."
    accepted = study.call(lead, "gx_decide_revision", {"decision":decision}, "keep-original")
    result["readback"] = {"article": {key: page[key] for key in (
        "source_sha256", "segment_id", "label", "text_sha256", "receipt_id", "task_id")},
        "report_ref":report["report_ref"], "lead_task":lead.task_id, "lead_decision_ref":accepted["decision_ref"],
        "decision": decision["rationale"]}


def export_originals(study, result, directory):
    """Keep verified original bytes and both ends of every Job outside the release."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets = ArtifactService(study.conn)
    for job in result["jobs"]:
        target = directory / job["action"]
        target.mkdir(mode=0o700, exist_ok=True)
        for name, ref in job["ports"].items():
            if name not in ("stdout", "stderr"):
                (target / name).write_bytes(assets.read_bytes(ref, expected_sha256=job["output_sha256"][name]))
        save(target / "job-manifest.json", manifest.read_manifest(study.conn, job["job_id"]))
    result["archived_originals"] = str(directory.resolve())
    save(directory / "receipt.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--export-originals", type=Path)
    parser.add_argument("--method-version", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    work = args.directory.resolve()
    receipt = work / "receipt.json"
    if work.exists():
        evidence = json.loads(receipt.read_text())
        if evidence.get("method_version", 1) != args.method_version:
            raise ValueError("Resume with the same method version used at admission")
        if not evidence.get("verified"):
            raise ValueError("The existing trial has unfinished Jobs. Inspect its original state before continuing.")
    else:
        admit(work)
        evidence = dict(question=QUESTION, transport="controlled native decisions, live publisher requests via Clash",
                        model_calls=0, method_version=args.method_version, jobs=[])
    with SurveyedStudy(work, reopen=True) as study:
        try:
            if not evidence.get("verified"):
                run(study, receipt, evidence, args.method_version)
            if not evidence.get("readback"):
                read_original(study, evidence)
            if args.export_originals:
                export_originals(study, evidence, args.export_originals)
        finally:
            evidence.setdefault("actions", []).extend(study.actions)
            save(receipt, evidence)
