"""Run the original A1 or four B engineering peers against explicitly selected released policy."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
POLICY = "gx.evidence/6.2"


def a1():
    sys.path.insert(0, str(ROOT / "acceptance/v62a"))
    import official_inventory as verifier
    grant = json.loads((verifier.EXAMPLE / "grant.json").read_bytes())
    for material in grant["input_files"]:
        material["path"] = str((verifier.EXAMPLE / material["path"]).resolve())
    with TemporaryDirectory() as directory:
        example = Path(directory)
        (example / "grant.json").write_text(json.dumps({**grant, "evidence_policy": POLICY}))
        with patch.object(verifier, "EXAMPLE", example):
            verifier.main()
    return ("quick", "module")


def b4():
    sys.path.insert(0, str(ROOT / "acceptance/v62b/engineering"))
    import run as verifier
    released_grant = verifier.grant
    def choose_policy(arm):
        return {**released_grant(arm), "evidence_policy": POLICY}
    with patch.object(verifier, "grant", choose_policy):
        verifier.main()
    return verifier.ARMS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", choices=("a1", "b4"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
    with patch.object(sys, "argv", [sys.argv[0], "--work", str(work), "--binary", str(args.binary.resolve())]):
        arms = a1() if args.cohort == "a1" else b4()
    results = {}
    for arm in arms:
        state = work / arm / "state/gx.db"
        with closing(sqlite3.connect(state.as_uri() + "?mode=ro", uri=True)) as conn:
            row, = conn.execute("SELECT run_id, json_extract(inputs_json, '$.inputs.evidence_policy') FROM runs")
            if row[1] != POLICY:
                raise ValueError(f"{arm} did not persist the released evidence policy")
            jobs, = conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (row[0],)).fetchone()
        results[arm] = {"run_id": row[0], "evidence_policy": row[1], "jobs": jobs}
    receipt = {"cohort": args.cohort, "replay": "fresh old-policy runs", "arms": results,
               "input_adapter_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
               "original_test_result_sha256": sha256((work / "result.json").read_bytes()).hexdigest()}
    (work / "policy-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
