"""Verify the pinned local runtime and independently reaggregate the two shipped workloads."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
BASE = "10ed4155099cf05fd81de29f739bf481cc00a045"
PERF = ROOT / "acceptance/v3/performance"
RUST = (
    "codex-rs/app-server/src/bespoke_event_handling.rs",
    "codex-rs/app-server/src/outgoing_message.rs",
    "codex-rs/app-server/src/dynamic_tools.rs",
    "codex-rs/app-server/src/server_request_error.rs",
    "codex-rs/core/src/tools/handlers/dynamic.rs",
)


def sha(path):
    with path.open("rb") as fp:
        return hashlib.file_digest(fp, "sha256").hexdigest()


def pinned_codex():
    assets = ROOT / "release-assets"
    lock = json.loads((assets / "lock.json").read_text())
    elf = ROOT / "vendor/codex-runtime/package" / lock["linux_x64"]["binary"]
    entries = {}
    for key in ("source", "experimental_schema", "linux_x64"):
        digest = sha(assets / lock[key]["file"])
        if digest != lock[key]["sha256"]:
            raise ValueError(f"Asset digest mismatch: {key}")
        entries[key] = digest
    if sha(elf) != lock["linux_x64"]["binary_sha256"]:
        raise ValueError("Executable digest mismatch")
    source_members = {}
    with tarfile.open(assets / lock["source"]["file"]) as source:
        for name in RUST:
            with source.extractfile(name) as fp:
                digest = hashlib.file_digest(fp, "sha256").hexdigest()
            if digest != sha(ROOT / "vendor/codex" / name):
                raise ValueError(f"Different checked out Rust source: {name}")
            source_members[name] = digest
    return {"version": subprocess.check_output([elf, "--version"], text=True).strip(),
            "binary_sha256": sha(elf), "archives_sha256": entries, "source_commit": lock["source"]["commit"],
            "verified_vendor_files": source_members}


def workload(arm):
    path = PERF / arm / "receipts.jsonl.gz"
    summary = json.loads((path.parent / "summary.json").read_text())
    if sha(path) != summary["evidence_sha256"][path.name]:
        raise ValueError(f"Changed source receipts for {arm}")
    rows = defaultdict(list)
    with gzip.open(path, "rt") as fp:
        for line in fp:
            receipt = json.loads(line)
            rows[receipt["kind"]].append(receipt["data"])

    first_reviews = [s for s in rows["submission"] if s["attempt"] == 1 and s["role"] == "evidence_reviewer"]
    root_keys = {r["thread_id"]: r for r in rows["root"]}
    costs = sum(t["settled_tokens"] for t in rows["turn_meter"])
    responses = sum(t["native_responses"] for t in rows["official_native_turn"])
    if (costs, responses) != (summary["counts"]["ledger_spent_tokens"], summary["counts"]["gx_provider_response_receipts"]):
        raise ValueError(f"Native and GX denominators differ: {arm}")
    return {"source_sha256": sha(path), "runs": len(rows["run"]), "jobs": len(rows["job"]),
            "independent_cpu_recomputed_results": len(rows["recomputed_numerical_result"]),
            "verified_artifacts": len(rows["artifact"]), "turns": len(rows["turn_meter"]),
            "native_completed_responses": responses, "provider_settled_tokens": costs,
            "settlements_by_confidence": dict(Counter(t["confidence"] for t in rows["turn_meter"])),
            "first_reviewer_submissions": len(first_reviews),
            "first_reviewer_rejections": sum(s["result"] == "REJECTED" for s in first_reviews),
            "accepted_products_by_role": dict(Counter(s["role"] for s in rows["submission"] if s["result"] == "ACCEPTED")),
            "responses_by_role": dict(sum_by_role(rows["official_native_turn"], root_keys)),
            "raw_callback_count": {k: v["n"] for k, v in summary["callback_per_tool_ms"].items()},
            "first_request_input_tokens": None, "scope": "shipped receipts, no live model"}


def sum_by_role(turns, roots):
    counts = Counter()
    for turn in turns:
        counts[roots[turn["thread_id"]]["role"]] += turn["native_responses"]
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = {"base": BASE, "implementation_package_sha256": sha(ROOT / "GX_H_V3_1_Revised_Implementation_Package.zip"),
                "codex": pinned_codex(), "v2.1": workload("gate0"), "v3": workload("p1")}
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
