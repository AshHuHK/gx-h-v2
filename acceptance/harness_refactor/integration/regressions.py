"""Run the candidate harness controls through the deployment's own Python and CLI fixtures."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]


def main():
    modules = [p.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
               for p in sorted((ROOT / "acceptance/harness_refactor").glob("*/test_*.py"))]
    env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(str(ROOT / s) for s in (".","gx-h","gx-h/tests","acceptance/v62a")))
    return subprocess.call([sys.executable,"-B","-m","unittest","-v",*modules],cwd=ROOT,env=env)


if __name__ == "__main__":
    sys.exit(main())
