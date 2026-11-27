"""Run a public-source handoff control. GX CLI/Jobs are real; native turns are scripted."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import PRODUCT, draft, save
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest

SEARCH = "scholarly-search@1"
FETCH = "scholarly-fetch@1"
MOD = "scholarly_investigation"
QUERY = "Evaluating cellular roles and phenotypes associated with trehalose degradation genes in Saccharomyces cerevisiae"
PAPER = "PMC11540316"


def start(work):
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    request = work / "sources.json"
    save(request, {"schema": "gx.scholarly-request/1"})
    limits = {"job_submissions": 8, "cpu_seconds": 1200, "worker_tasks": 32, "api_calls": 32,
              "tokens": 80000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}
    grant = {"project": "scholarly-source-handoff", "capabilities": [SEARCH, FETCH],
             "plugins": ["org.gx.scholarly-sources@1.0.0"], "research_modules": [MOD + "@1"],
             "input_files": [{"role": "source", "path": str(request)}], "research_effort": "deep",
             "turn_token_budget": 1000, "run_budget": limits, "workspace_budget": limits}
    save(work / "grant.json", grant)
    args = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "start", "--state", str(work / "state"),
            "--run-key", "scholarly-handoff-control", "--goal-config", str(work / "grant.json"), "--goal",
            "What measurements in a published primary study bear on trehalose degradation by baker's yeast?"]
    r = subprocess.run(args, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    save(work / "start.json", {"exit_code": r.returncode, "stdout": r.stdout, "stderr": r.stderr})
    if r.returncode:
        raise RuntimeError(r.stderr or r.stdout)


def handoff(s):
    request, = s.materials
    lead = s.next_actor("run")
    assignment = s.open_args("primary", inputs=[request], caps=[SEARCH, FETCH], limits={"cpu_seconds": 300})
    assignment.update(module_id=MOD, focus="literature", expected_delivery="Locate a primary study and cite a body paragraph.")
    module = s.call(lead, "gx_open_module", assignment, "open-literature")
    researcher = s.bind(module["task_id"])
    s.call(lead, "gx_decide_revision", {"decision": draft()}, "await-source")
    submit = s.submit_node(researcher, "search", request, capability=SEARCH, parameters={"query": QUERY})
    search = s.settle(submit["aliases"]["local:search"])
    reader = s.next_actor(module["scope"])
    candidates = s.call(reader, "gx_read_artifact", {"ref": search["result_ref"]}, "read-public-results")
    assert all(x["status"] == "hits" for x in candidates["sources"])
    assert any(PAPER in row["fetchable_pmcids"] for row in candidates["candidates"])
    submit = s.submit_node(reader, "article", search["result_ref"], capability=FETCH, parameters={"pmcid": PAPER})
    fetch = s.settle(submit["aliases"]["local:article"])
    outputs = {item["output_name"]: item["artifact_ref"] for item in
               manifest.read_manifest(s.conn, fetch["job_id"])["completion"]["outputs"]}
    ref = outputs["article.xml"]
    reader = s.next_actor(module["scope"])
    start = 0
    chosen = None
    while chosen is None:
        page = s.call(reader, "gx_read_source", {"source_ref": ref, "start": start}, "list-" + str(start))
        assert page["text_coverage"] == "FULL_TEXT"
        chosen = next((r for r in page["segments"] if r["kind"] == "body" and "/p" in r["label"]), None)
        start = page["next_start"]
        if chosen is None and start is None:
            raise ValueError("No body paragraph in acquired source")
    passage = s.call(reader, "gx_read_source", {"source_ref": ref, "segment_id": chosen["segment_id"]}, "read-fulltext")
    assets = ArtifactService(s.conn)
    descriptor = assets.read_json(outputs["acquisition.json"])
    assert descriptor["sha256"] == page["source_sha256"] == assets.describe(ref)["sha256"]
    assert descriptor["external_source"]["accession"] == PAPER
    assert passage["job_id"] == fetch["job_id"] and passage["text_sha256"] and passage["receipt_id"]
    return {"run_id": s.run_id, "control": "scripted native turns; actual GX CLI and public-source Jobs via Clash",
            "search_job": search["job_id"], "query": candidates["query"], "sources": candidates["sources"],
            "dedup": [candidates["returned_before_dedup"], candidates["unique_count"]],
            "fetch_job": fetch["job_id"], "descriptor": descriptor, "outputs": outputs,
            "module": module["scope"], "article_reader": {key: passage[key] for key in (
                "source_sha256", "segment_id", "label", "source_ref", "text_sha256", "receipt_id", "task_id")},
            "text_coverage": page["text_coverage"], "segments_count": page["segments_count"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new private directory outside the product tree")
    work = parser.parse_args().directory.resolve()
    start(work)
    with CloseStudy(work, reopen=True) as scenario:
        receipt = handoff(scenario)
    save(work / "receipt.json", receipt)
    print(json.dumps(receipt, indent=2))
