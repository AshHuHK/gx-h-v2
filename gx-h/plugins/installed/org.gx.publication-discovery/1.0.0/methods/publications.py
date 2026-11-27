"""Search public catalogues and acquire JATS using the release's pinned page readers."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy


SOURCES_SHA = "6f731eadb23f5a89bb8d0bfa92a2d8243158e92a0c192307d80aa8a6e7d88068"
PAGES_SHA = "6337ff50b4567a2edeba8dd80a9a806ea40c408714f092b7854901c0c010d6d9"


def load_reader(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"The installed publisher reader differs from its pinned version: {path.name}")
    return runpy.run_path(str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("method", choices=("query", "page", "original"))
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--query")
    parser.add_argument("--action", choices=("next", "replay"))
    parser.add_argument("--pmcid")
    args = parser.parse_args()
    api = load_reader(args.base, SOURCES_SHA)
    pages = load_reader(args.pages, PAGES_SHA)
    if args.method == "query":
        candidates, snapshot = pages["discover"](api, {"schema": pages["REQUEST"]}, args.query, "first")
    else:
        source = json.loads(args.source.read_bytes())
        if args.method == "original":
            raw, descriptor = api["fetch"](source, args.pmcid)
            (args.out / "article.xml").write_bytes(raw)
            (args.out / "acquisition.json").write_text(json.dumps(descriptor, ensure_ascii=False) + "\n")
            return
        candidates, snapshot = pages["discover"](api, source, source["query"], args.action)
    for name, document in (("candidates.json", candidates), ("search-snapshot.json", snapshot)):
        (args.out / name).write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
