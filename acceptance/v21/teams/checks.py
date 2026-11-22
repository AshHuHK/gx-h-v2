"""Run the Team owner regressions and print their observed allocations and durable receipts."""
import json
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "gx-h"))

from tests.test_v21_teams import WorkspaceTeams

cases = list(unittest.defaultTestLoader.loadTestsFromTestCase(WorkspaceTeams))
suite = unittest.TestSuite(cases)
result = unittest.TextTestRunner(verbosity=2,stream=sys.stderr).run(suite)
report = {"suite":"Phase 4 Team allocation and dispatch", "passed":result.wasSuccessful(),
          "tests_run":result.testsRun,
          "execution":"Isolated disk-backed GX instances, concurrent SQLite writers, wire-fixture Codex transport, real local detached Job",
          "observations":{case._testMethodName:case.observed for case in cases if hasattr(case,"observed")}}
print(json.dumps(report,sort_keys=True,indent=2))
sys.exit(0 if result.wasSuccessful() else 1)
