"""Verify a labelled PDF using the selected release Python, public Goal CLI and scoped source tool."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import PRODUCT, save
import pypdf


def exercise(work):
    original = PRODUCT / "gx-h/tests/fixtures/labelled-source.pdf"
    budget = dict(tokens=20000, worker_tasks=8, api_calls=8, job_submissions=0, cpu_seconds=0,
                  gpu_jobs=0, gpu_minutes=0, wall_hours=1)
    grant = dict(project="pdf-page-locator-control", research_effort="deep",
                 research_modules=["offline_literature_mechanism@1"], capabilities=[],
                 run_budget=budget, workspace_budget=budget, turn_token_budget=1000,
                 input_files=[dict(role="literature", path=str(original))])
    save(work / "grant.json", grant)
    proc = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "start", "--state", str(work / "state"),
                           "--goal", "Read the labelled PDF and identify which page contains the observation.",
                           "--goal-config", str(work / "grant.json"), "--run-key", "labelled-pdf"],
                          env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr or proc.stdout)
    with CloseStudy(work, reopen=True) as s:
        actor = s.next_actor("run")
        ref, = s.materials
        listing = s.call(actor, "gx_read_source", {"source_ref": ref}, "list-pages")
        segment, = listing["segments"]
        read = s.call(actor, "gx_read_source", {"source_ref": ref, "segment_id": segment["segment_id"]},
                      "read-labelled-page")
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        assert listing["source_sha256"] == digest and read["source_sha256"] == digest
        assert read["parsing"]["extractor"] == "pypdf " + pypdf.__version__
        assert (read["text_coverage"], read["parsing"]["page_count"], read["parsing"]["pages_with_text"]) == (
            "PARTIAL_TEXT", 2, [2])
        assert (read["label"], read["text"]) == ("page 2 [S-8]", "Published observation on page two.")
        return {"run_id": s.run_id, "transport": "scripted local native turn; product CLI and scoped source tool",
                "python": platform.python_version(), "reader": read["parsing"]["extractor"],
                "paper_sha256": digest, "source_ref": ref, "coverage": read["text_coverage"],
                "parsing": read["parsing"], "segment_id": segment["segment_id"], "original_page_label": read["label"],
                "text_sha256": read["text_sha256"], "receipt": read["receipt_id"], "task": read["task_id"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as work:
        result = exercise(Path(work))
    if args.output:
        save(args.output, result)
    print(json.dumps(result, indent=2))
