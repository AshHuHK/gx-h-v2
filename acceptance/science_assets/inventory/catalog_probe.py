"""Read the curated inventory through ordinary Goal admission and native catalogue callbacks."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from acceptance.science_plugin_blueprint.catalog_probe import ROOT, Scenario


def probe(directory):
    catalog_file = ROOT / "gx-h/config/science_asset_inventory.json"
    cards = json.loads(catalog_file.read_bytes())["cards"]
    evidence = []
    with Scenario(directory, spec_updates={"evidence_policy":"gx.evidence/6.2.4",
            "research_effort":"deep","research_modules":[],"plugins":[],"capabilities":[],
            "capability_options":[],"capability_collections":[],"input_files":[]}) as run:
        for i, card in enumerate(cards):
            for kind in ("all",card["kind"]):
                reply = run.call(run.lead,"gx_query_catalog",{"kind":kind,"reference":card["reference"]},f"asset-{i}-{kind}")
                obj = reply.get("spec") or reply
                if obj.get("digest") != card["digest"] or obj.get("status")!="reference_only":
                    raise AssertionError(f"Unexpected ordinary Goal result for {kind}/{card['reference']}: {reply!r}")
                if obj.get("execution_granted") is not False or obj.get("source") != card["source"]:
                    raise AssertionError("Reference origin or execution semantics changed")
                evidence.append({"kind":kind,"reference":card["reference"],"digest":obj["digest"],
                                 "status":obj["status"],"execution_granted":obj["execution_granted"]})
        return {"transport":"gx-p0 start; controlled native callbacks; zero live model turns",
                "catalog_sha256":hashlib.sha256(catalog_file.read_bytes()).hexdigest(),"run_id":run.run_id,
                "jobs":run.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run.run_id,)).fetchone()[0],
                "lookups":evidence}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, help="Use a fresh state directory and keep it after the probe.")
    args=parser.parse_args()
    if args.state:
        print(json.dumps(probe(args.state),indent=2))
    else:
        with tempfile.TemporaryDirectory() as work:
            print(json.dumps(probe(Path(work)/"probe"),indent=2))
