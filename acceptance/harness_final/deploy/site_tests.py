"""Run the specified compatibility cohort with an installation's own Python and source."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("suite", choices=("historical", "harness", "final"))
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    python = args.python.absolute() if args.python else root / ".venv/bin/python"
    env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(str(root / p) for p in ("gx-h", ".", "gx-h/tests", "acceptance/v62a")))
    if args.suite == "historical":
        cases = Path(__file__).with_name("historical-121.txt").read_text().splitlines()
        if len(cases) != 121 or len(set(cases)) != 121:
            raise SystemExit("The frozen 121-case cohort has changed.")
        cmd = [str(python), "-B", "-m", "unittest", "-v", *cases]
    else:
        entry = ("acceptance/harness_refactor/integration/regressions.py"
                 if args.suite == "harness" else "acceptance/harness_final/regressions.py")
        cmd = [str(python), "-B", entry]
    return subprocess.call(cmd, cwd=root, env=env)


if __name__ == "__main__":
    sys.exit(main())
