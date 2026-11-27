"""Detached GX scholarly Job."""
import argparse
import json
from pathlib import Path
import runpy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["search", "fetch", "social"])
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--implementation", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--query")
parser.add_argument("--pmcid")
args = parser.parse_args()
implementation = runpy.run_path(str(args.implementation))
raw = args.source.read_bytes()
if args.action == "search":
    candidates, snapshot = implementation["discover"](json.loads(raw), args.query)
    documents = {"candidates.json": candidates, "search-snapshot.json": snapshot}
elif args.action == "fetch":
    data, acquisition = implementation["fetch"](json.loads(raw), args.pmcid)
    (args.out / "article.xml").write_bytes(data)
    documents = {"acquisition.json": acquisition}
else:
    documents = {"community-lead.json": implementation["social_intake"](raw)}
for name, value in documents.items():
    (args.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
