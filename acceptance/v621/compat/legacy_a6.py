"""Replay released A6 with its original waiting semantics on a fresh explicitly pinned Goal."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(PRODUCT), str(PRODUCT / "gx-h")]
from acceptance.v62a import interleavings, interleavings_limits

POLICY = "gx.evidence/6.2"


def pinned_goal(directory, **kwargs):
    updates = {**kwargs.pop("spec_updates", {}), "evidence_policy": POLICY}
    return interleavings.Scenario(directory, spec_updates=updates, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    # Only the input Goal is pinned. Every production call and the original A6 assertions execute.
    with patch.object(interleavings_limits, "Scenario", pinned_goal), patch.object(
            sys, "argv", [sys.argv[0], "--work", str(work), "--case", "A6"]):
        interleavings.main()
    scenarios = {}
    for name in ("exhausted", "large-report", "gated"):
        db = work / "A6-state" / name / "state/gx.db"
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            run_id, policy = conn.execute(
                "SELECT run_id, json_extract(inputs_json, '$.inputs.evidence_policy') FROM runs"
            ).fetchone()
        if policy != POLICY:
            raise RuntimeError(f"{name} did not retain the released evidence policy")
        scenarios[name] = {"run_id": run_id, "evidence_policy": policy}
    receipt = {"case": "A6", "replay": "fresh explicitly pinned Goals",
               "input_adapter_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "scenarios": scenarios}
    (work / "policy-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
