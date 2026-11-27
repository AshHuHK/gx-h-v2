"""Compare the public Goal launch and native discovery callback without inference or external jobs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]

from acceptance.v62a.interleavings_fixture import Scenario


def probe(directory, *, discovery=False):
    experiments = []
    for name, plugin, methods, queries in (
        ("literature", "org.gx.scholarly-sources@1.0.0", ["scholarly-search@1", "scholarly-fetch@1"],
         [("method", None), ("method", "Crossref"), ("research", "Crossref"), ("method", "JATS"),
          ("research", "JATS"), ("research", "文獻"), ("research", "文献")]),
        ("database", "org.gx.yeast-enzyme-evidence@1.0.1", ["yeast-reaction-index@2"],
         [("method", "BLASTp"), ("database", "BLASTp")]),
    ):
        grant = {"evidence_policy":"gx.evidence/6.2.4", "research_effort":"deep", "research_modules":[],
                 "plugins":[plugin], "capabilities":methods, "capability_options":[], "input_files":[]}
        if discovery:
            grant["capability_collections"] = []
        with Scenario(directory / name, spec_updates=grant) as run:
            results = []
            for i, (kind, term) in enumerate(queries):
                args = {"kind":kind, **({"query":term} if term else {})}
                call = f"probe-{i}"
                answer = run.call(run.lead, "gx_query_catalog", args, call)
                results.append({"call":call, "arguments":args, "answer":answer})
            experiments.append({"run_id":run.run_id, "grant":grant, "thread":run.lead.thread,
                                "tools":[t["name"] for t in run.roots._tools_for(run.roots._binding(run.lead.thread))],
                                "results":results})
    observed = [ROOT / name for name in (
        "gx-h/config/workflow_packages.json", "gx-h/config/research_asset_cards.json", "gx-h/contracts/mcp_tools.json",
        "gx-h/gx_core/p0/v62_catalog.py", "gx-h/gx_core/p0/catalog_resources.py", "gx-h/gx_core/p0/catalog_index.py")]
    return {"source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in observed if p.is_file()}, "policy_opt_in": discovery,
            "version":subprocess.check_output(["git", "rev-parse", "HEAD"],cwd=ROOT,text=True).strip(),
            "transport":"gx-p0 start; controlled native callback; no external jobs or model calls",
            "experiments":experiments}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--discovery", action="store_true", help="start new Goals under gx.catalog/1")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output path to preserve prior receipts")
    with tempfile.TemporaryDirectory() as work:
        data = probe(Path(work), discovery=args.discovery)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
