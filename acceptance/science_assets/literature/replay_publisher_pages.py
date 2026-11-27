"""Audit real archived catalogue responses without contacting either publisher."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[3] / "gx-h/plugins/installed"


def inspect(folder):
    api = runpy.run_path(str(ROOT / "org.gx.scholarly-sources/1.0.0/methods/scholarly.py"))
    pager = runpy.run_path(str(ROOT / "org.gx.scholarly-pages/1.1.0/methods/windows.py"))
    def no_network(url):
        raise RuntimeError(f"Offline replay requested a new page: {url}")

    previous = {"schema": api["REQUEST"]}
    pages = []
    for label, action in (("first", "first"), ("next", "next")):
        path = folder / label / "search-snapshot.json"
        content = path.read_bytes()
        snapshot = json.loads(content)
        frozen = {c["url"]:base64.b64decode(c["raw_b64"]) for c in snapshot["calls"]}
        def replay_reply(url):
            return frozen.pop(url)

        result, previous = pager["discover"](api, previous, snapshot["query"], action, replay_reply)
        if frozen:
            raise ValueError("New reader did not consume all archived requests")
        saved, again = pager["discover"](api, previous, snapshot["query"], "replay",
                                         no_network)
        assert saved["sources"] == result["sources"] and saved["merged"] == result["merged"] and again == previous
        pages.append({"page": label, "query":snapshot["query"],
                      "original_snapshot_sha256": hashlib.sha256(content).hexdigest(),
                      "sources":[{k:s[k] for k in ("provider","page","sha256","returned","provider_hits",
                                    "indexed_records","seen_through","new_unique","cumulative_unique","has_more",
                                    "access","history_complete")} for s in result["sources"]],
                      "merged":result["merged"],"replay_equal":True})
    return {"source_run":folder.name, "scope":"Read-only replay of the two archived query windows, no new model turns or Jobs.",
            "pages":pages}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run_directory", type=Path)
    print(json.dumps(inspect(parser.parse_args().source_run_directory), indent=2))
