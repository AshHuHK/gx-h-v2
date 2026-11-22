"""Record the actual outcomes of the public defensive and resource-limit controls."""
import argparse
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[3] / "gx-h"))
from tests.test_v21_compute import ComputeTest


class Results(unittest.TextTestResult):
    def __init__(self,*args):
        super().__init__(*args)
        self.rows = []

    def addSuccess(self,test):
        super().addSuccess(test)
        self.rows.append({"test":test.id(),"observed":test.observations})


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args=parser.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ComputeTest)
    result=unittest.TextTestRunner(resultclass=Results,verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    if args.output:
        args.output.write_text(json.dumps({"controls":result.rows,"passed":result.testsRun},indent=2,sort_keys=True)+"\n")
