"""Exercise two live publisher pages as ordinary GX Jobs using controlled native actions."""
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

SEARCH = "scholarly-page-search@1"
FETCH = "scholarly-page-fetch@1"
MODULE = "scholarly_page_investigation"
QUERY = "rain gauge precipitation measurement"


def start(work):
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    request = work / "materials.json"
    save(request, {"schema": "gx.scholarly-request/1"})
    limits = dict(job_submissions=6, cpu_seconds=900, worker_tasks=30, api_calls=20,
                  tokens=45000, gpu_jobs=0, gpu_minutes=0, wall_hours=1)
    spec = dict(project="paged-source-control", capabilities=[SEARCH, FETCH],
                plugins=["org.gx.scholarly-pages@1.0.0"], research_modules=[MODULE+"@1"],
                input_files=[{"role": "source", "path": str(request)}], research_effort="deep",
                turn_token_budget=1000, run_budget=limits, workspace_budget=limits)
    save(work / "grant.json", spec)
    cmd = [sys.executable, "-B", str(PRODUCT/"bin/gx-p0"), "start", "--state", str(work/"state"),
           "--run-key", "public-publisher-page-control", "--goal-config", str(work/"grant.json"),
           "--goal", "How far did the search for public rain-gauge measurement reports extend?"]
    proc = subprocess.run(cmd, text=True, capture_output=True, env={**os.environ, "GX_CODEX_OFFLINE":"1"})
    save(work/"start.json", {"code":proc.returncode, "stdout":proc.stdout, "stderr":proc.stderr})
    if proc.returncode:
        raise RuntimeError(proc.stderr or proc.stdout)


def run(s, path):
    lead = s.next_actor("run")
    inputs = list(s.materials)
    request = inputs[0]
    assign = s.open_args("public-source", inputs=inputs, caps=[SEARCH, FETCH], limits={"cpu_seconds": 450, "job_submissions": 3})
    assign.update(module_id=MODULE, focus="literature", expected_delivery="Verify the source windows actually returned.")
    module = s.call(lead, "gx_open_module", assign, "open-public-source")
    actor = s.bind(module["task_id"])
    s.call(lead, "gx_decide_revision", {"decision": draft()}, "await-measurement")
    report = {"run_id":s.run_id,"mode":"controlled native actions, real publisher Jobs through Clash",
              "scope":module["scope"],"pages":[]}
    for action in ("first", "next", "replay"):
        item = s.submit_node(actor, action, request, capability=SEARCH, parameters={"query":QUERY,"action":action})
        node = s.settle(item["aliases"]["local:"+action])
        ports = {o["output_name"]:o["artifact_ref"] for o in
                 manifest.read_manifest(s.conn, node["job_id"])["completion"]["outputs"]}
        actor = s.next_actor(module["scope"])
        read = s.call(actor, "gx_read_artifact", {"ref":ports["candidates.json"]}, "read-"+action)
        archive = ArtifactService(s.conn).describe(ports["search-snapshot.json"])
        report["pages"].append({"action":action, "input_ref":request, "job_id":node["job_id"], "ports":ports,
                   "archive_sha256":archive["sha256"], "returned":read["returned_before_dedup"],
                   "unique":read["unique_count"], "sources":read["sources"]})
        save(path, report)
        request = ports["search-snapshot.json"]
        if action == "first" and not any(row["has_more"] for row in read["sources"]):
            return
    assert report["pages"][1]["sources"] == report["pages"][2]["sources"]
    assert report["pages"][1]["archive_sha256"] == report["pages"][2]["archive_sha256"]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("new_work_directory", type=Path)
    work = p.parse_args().new_work_directory.resolve()
    start(work)
    with CloseStudy(work, reopen=True) as study:
        run(study, work/"receipt.json")
    print(work/"receipt.json")
