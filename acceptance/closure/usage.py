"""Run the offline usage closure cases and retain each ledger and its event history."""
import argparse
import json
from pathlib import Path
import sys
import unittest

from test_usage import UsageClosure


def run(directory, selected=None):
    directory.mkdir(parents=True, exist_ok=False)
    cases = {}
    for name in unittest.defaultTestLoader.getTestCaseNames(UsageClosure):
        case_id = name.split("_")[1] if name.startswith("test_U") else "NO_GRANT"
        if selected and case_id not in selected:
            continue
        case = UsageClosure(name)
        case.artifact_dir = directory / case_id
        outcome = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(case)
        cases[case_id] = {"status":"PASS" if outcome.wasSuccessful() else "FAIL", "test":name,
                          "evidence_class":"SCRIPTED", "ledger":f"{case_id}/ledger.db",
                          "events":f"{case_id}/events.json", "observations":f"{case_id}/usage.json"}
    result = {"evidence_class":"SCRIPTED", "cases":cases, "passed":all(r["status"] == "PASS" for r in cases.values())}
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append", choices=[*(f"U{i:02d}" for i in range(1, 9)), "NO_GRANT"])
    args = parser.parse_args()
    sys.exit(0 if run(args.output, args.case) else 1)
