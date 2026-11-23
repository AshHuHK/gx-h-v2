"""Compare unchanged one-round planner requests across two builds using equal-length workspace paths."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brief_wire


def main(args):
    args.work.mkdir(parents=True, exist_ok=False)
    public = brief_wire.public
    original = public.ReviewerPeer.__call__

    def observe(peer, body, ident):
        event, info = original(peer, body, ident)
        packet = public.peer.brief_from(body)
        assert packet["input"]["max_rounds"] == 1 and not packet["input"]["supplement_sources"]
        assert "native_product_transport" not in packet
        if packet["role"] == "planner":
            submit, = [t for g in body["tools"] if g.get("name") == "gx" for t in g["tools"]
                       if t["name"] == "gx_submit_product"]
            raw = json.dumps(submit, sort_keys=True).encode()
            schemas.add(hashlib.sha256(raw).hexdigest())
        marker = "GX task brief\n"
        copies = Counter(instructions=(body.get("instructions") or "").count(marker))
        for item in body["input"]:
            if item.get("role") in ("user", "developer"):
                copies[item["role"]] += sum(t.count(marker) for t in public.peer.message_texts(item))
        return event, {**info, "copies_by_role": dict(copies)}

    keys = ("spec_sha256", "science_sha256", "http_requests", "scripted_provider_tokens",
            "post_terminal_requests", "compaction_http_requests", "total_http_request_bytes")
    metrics = ("brief_utf8_bytes", "first_request_http_bytes", "first_request_brief_copies",
               "requests", "total_http_bytes", "worker_tools")
    results = {}
    with patch.object(public.ReviewerPeer, "__call__", observe):
        for mode in ("review", "deterministic"):
            samples = {}
            for arm, revision in (("a0", args.before), ("b0", args.after)):
                schemas = set()
                case = brief_wire.compare_case(revision, mode, args.work / (mode + "-" + arm))
                workers = {w["role"]: {k: w[k] for k in metrics} for w in case["workers"].values()}
                samples[arm] = {**{k: case[k] for k in keys}, "planner_tool_schema_sha256": sorted(schemas),
                                "workers": workers}
            results[mode] = samples
    args.output.write_text(json.dumps({"before": args.before, "after": args.after, "samples": results}, indent=2) + "\n")
    assert all(arms["a0"] == arms["b0"] for arms in results.values())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("before", "after"):
        p.add_argument("--" + key, required=True)
    for key in ("work", "output"):
        p.add_argument("--" + key, required=True, type=Path)
    main(p.parse_args())
