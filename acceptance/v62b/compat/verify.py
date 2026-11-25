"""Verify the public B compatibility receipts against an unpacked B release, without Git or network."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def load(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(directory, product, revision):
    index = load(directory / "index.json")
    assert index["format"] == "gx.v62b.compatibility/1" and index["b_revision"] == revision
    for rel, expected in index["files"].items():
        path = (directory / rel).resolve()
        assert path.is_relative_to(directory) and sha(path) == expected, rel
    a = load(directory / "a-upgrade.json")
    historical = load(directory / "v61-upgrade.json")
    regression = load(directory / "a-regression.json")
    for result in (a, historical, regression):
        assert result["status"] == "PASS" and result["b_revision"] == revision and result["paid_model_calls"] == 0
    assert a["a_revision"] == index["a_runtime"] and historical["v61_revision"] == index["v61_runtime"]
    assert [r["reopen"] for r in a["reopens"]] == [1, 2] and a["rollback_matches"]
    assert all(r["business_sha256"] == a["original_business_sha256"] for r in a["reopens"])
    assert a["additional_jobs"] == a["additional_turns"] == a["additional_charges"] == 0
    assert len(a["unavailable_original_inputs_during_b_reopens"]) == len(a["execution_manifests"]) == 2
    states = historical["cases"]
    assert set(states) == {"quick", "static", "multi-hop"}
    assert len({j for row in states.values() for j in row["original"]}) == 5
    for name, row in states.items():
        assert row["original_manifest_and_state_rollback_matches"] and row["twice_equal"]
        assert row["additional_jobs"] == row["additional_turns"] == row["additional_token_charges"] == 0
        assert row["original"].keys() == row["upgraded"].keys()
        for job, old in row["original"].items():
            new = row["upgraded"][job]
            assert old["admission_sha256"] == new["admission_sha256"] and new["outputs"]
            if old["completion_sha256"]:
                assert old == new
            else:
                assert name == "static" and new["completion_schema"] == "gx.execution-manifest.completion/1"
                assert old["settlement"]["reservation_ref"] == new["settlement"]["reservation_ref"]
                assert old["settlement"]["event_id"] is None and new["settlement"]["actual"]["job_submissions"] == 1
    protocol = directory / "a-protocol"
    verifier = protocol / "verify.py"
    assert sha(verifier) == regression["original_a_verifier"]["verifier_sha256"]
    assert sha(protocol / "receipts/index.json") == regression["original_a_verifier"]["index_sha256"]
    assert sha(protocol / "upgrade/result.json") == historical["original_probe_sha256"]
    original = json.loads(subprocess.check_output([sys.executable, "-B", verifier, "--root", product,
                                                   "--directory", protocol, "--source", revision], text=True))
    assert original["status"] == "PASS" and original["source"] == revision
    return {"status": "PASS", "b_revision": revision, "compatibility_files": len(index["files"]),
            "verified_b_runtime_files": original["verified_runtime_files"], "replayed_cases": original["cases"],
            "v61_historical_jobs": 5, "a_historical_jobs": 2, "paid_model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "acceptance/v62b/receipts/compat")
    parser.add_argument("--product", type=Path, default=ROOT)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    result = verify(args.directory.resolve(), args.product.resolve(), args.revision)
    print(json.dumps(result))
