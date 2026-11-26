"""Detached Job entry point. The Job supplies its pinned archive method and source reader."""
import argparse
import json
from pathlib import Path
import runpy
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=("search", "fetch"))
parser.add_argument("--implementation", required=True, type=Path)
parser.add_argument("--archive", required=True, type=Path)
parser.add_argument("--out", required=True, type=Path)
parser.add_argument("--entry-id")
parser.add_argument("--identity-reader", type=Path)
args = parser.parse_args()
method = runpy.run_path(str(args.implementation))
raw = args.archive.read_bytes()
if args.action == "search":
    documents = {"candidates.json": method["search"](raw)}
else:
    sys.path.insert(0, str(args.identity_reader.resolve().parents[2]))
    from adapters.scientific import archive_coordinates
    original, acquisition, context = method["fetch"](json.loads(raw), args.entry_id, archive_coordinates)
    (args.out / "structure.cif").write_bytes(original)
    documents = {"acquisition.json": acquisition, "structure-context.json": context}
for name, document in documents.items():
    (args.out / name).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
