"""Run the final engineering cases with the selected deployment's own Python and CLI."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    files = sorted(Path(__file__).resolve().parent.glob("*/test_*.py"))
    if not files:
        raise RuntimeError("No harness acceptance tests found")
    modules = [str(p.relative_to(ROOT).with_suffix("")).replace(os.sep, ".") for p in files]
    imports = (".", "gx-h", "gx-h/tests", "acceptance/v62a")
    env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(str(ROOT / path) for path in imports))
    return subprocess.call([sys.executable, "-B", "-m", "unittest", "-v", *modules],
                           cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
