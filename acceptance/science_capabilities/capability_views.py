"""Run the scoped selection and crash-recovery acceptance on fresh disposable workspaces."""
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from tests.test_science_capability_views import CapabilityViews


class Receipts(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.passed = []
        self.jobs = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())
        if hasattr(test, "receipt"):
            self.jobs.append(test.receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CapabilityViews)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Receipts).run(suite)
    record = {"suite":"Goal capability selection and per-role catalogue",
              "host":"scripted native calls, actual GX SQLite/CLI/CPU Job",
              "passed":result.passed, "recovery":result.jobs,
              "failures":[{"test":t.id(), "trace":trace} for t, trace in result.failures + result.errors]}
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
