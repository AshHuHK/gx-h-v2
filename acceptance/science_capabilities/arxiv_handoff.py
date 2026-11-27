"""Live arXiv through two real GX Jobs, followed by the assigned researcher's PDF page read."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import PRODUCT, draft, save
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest

QUERY = 'ti:"Observation of Gravitational Waves from a Binary Black Hole Merger"'
SEARCH, FETCH = "arxiv-search@1", "arxiv-fetch@1"
REVISION = "1602.03837v1"


def run(work):
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = work / "request.json"
    save(source, {"schema": "gx.scholarly-request/1"})
    budget = {"job_submissions": 8, "cpu_seconds": 1200, "worker_tasks": 32, "api_calls": 32,
              "tokens": 80000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}
    save(work/"grant.json", {"project": "arxiv-public-control", "capabilities": [SEARCH, FETCH],
         "plugins": ["org.gx.arxiv-sources@1.0.0"], "research_modules": ["arxiv_investigation@1"],
         "input_files": [{"role": "source", "path": str(source)}], "research_effort": "deep",
         "turn_token_budget": 1000, "run_budget": budget, "workspace_budget": budget})
    args = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state", str(work/"state"),
            "--run-key", "ligo-arxiv-original", "--goal-config", str(work/"grant.json"), "--timeout", ".2",
            "--goal", "Which observation do the authors of the original GW150914 preprint report?"]
    proc = subprocess.run(args, text=True, capture_output=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    save(work/"start.json", {"command": args, "status": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
    if proc.returncode not in (0, 75):
        raise RuntimeError(proc.stderr or proc.stdout)
    with CloseStudy(work, reopen=True) as s:
        request, = s.materials
        lead = s.next_actor("run")
        opts = s.open_args("preprint", inputs=[request], caps=[SEARCH, FETCH], limits={"cpu_seconds": 300})
        opts.update(module_id="arxiv_investigation", focus="literature", expected_delivery="Read the original preprint.")
        module = s.call(lead, "gx_open_module", opts, "assign-preprint")
        worker = s.bind(module["task_id"])
        s.call(lead, "gx_decide_revision", {"decision": draft()}, "await-original")
        submission = s.submit_node(worker, "find", request, capability=SEARCH, parameters={"query": QUERY})
        search = s.settle(submission["aliases"]["local:find"])
        worker = s.next_actor(module["scope"])
        candidates = s.call(worker, "gx_read_artifact", {"ref": search["result_ref"]}, "read-search")
        entry = next(r for r in candidates["candidates"] if r["arxiv_id"] == REVISION)
        submit = s.submit_node(worker, "acquire", search["result_ref"], capability=FETCH, parameters={"arxiv_id": REVISION})
        fetched = s.settle(submit["aliases"]["local:acquire"])
        return read_original(s)

def read_original(s):
    def completed(capability):
        row, = s.rows("SELECT job_id,result_ref FROM v6_nodes WHERE run_id=? AND status='SUCCEEDED' "
                      "AND json_extract(spec_json,'$.capability')=?", s.run_id, capability)
        return row
    search, fetched = completed(SEARCH), completed(FETCH)
    candidates = ArtifactService(s.conn).read_json(search["result_ref"])
    entry = next(r for r in candidates["candidates"] if r["arxiv_id"] == REVISION)
    module, = s.rows("SELECT scope FROM v62_modules WHERE run_id=? AND module_id='arxiv_investigation'", s.run_id)
    outputs = {o["output_name"]: o["artifact_ref"] for o in
               manifest.read_manifest(s.conn, fetched["job_id"])["completion"]["outputs"]}
    worker = s.next_actor(module["scope"])
    pages = s.call(worker, "gx_read_source", {"source_ref": outputs["article.pdf"]}, "list-pages")
    first = next(p for p in pages["segments"] if p["kind"] == "page_text" and p["label"].split()[:2] == ["page", "1"])
    passage = s.call(worker, "gx_read_source", {"source_ref": outputs["article.pdf"], "segment_id": first["segment_id"]},
                     "read-first-page")
    meta = ArtifactService(s.conn).read_json(outputs["acquisition.json"])
    assert meta["sha256"] == pages["source_sha256"]
    assert meta["external_source"]["accession"] == REVISION
    assert pages["text_coverage"] == "PARTIAL_TEXT" and passage["text_sha256"]
    assert "gravitational" in passage["text"].lower()
    assert meta["external_source"]["url"] == entry["pdf_url"]
    assert entry["title"].lower() in passage["text"].lower()
    return {"run_id": s.run_id, "query": QUERY, "search_job": search["job_id"], "sources": candidates["sources"],
            "match": {k: entry[k] for k in ("arxiv_id", "title", "doi", "abs_url", "pdf_url")},
            "source_mode": candidates["source_mode"], "fetch_job": fetched["job_id"], "descriptor": meta,
            "outputs": outputs, "reader": {k:passage[k] for k in (
                "source_ref", "segment_id", "label", "text_sha256", "receipt_id", "task_id")},
            "page_coverage": pages["text_coverage"], "segment_count": pages["segments_count"],
            "transport":"Scripted decision turns on the normal research entry. Both Jobs contacted official arXiv through Clash."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    work = parser.parse_args().directory.resolve()
    if (work/"state/gx.db").exists():
        with CloseStudy(work, reopen=True) as scenario:
            receipt = read_original(scenario)
    else:
        receipt = run(work)
    save(work/"receipt.json", receipt)
    print(json.dumps(receipt, indent=2))
