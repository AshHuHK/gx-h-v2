"""Run the inherited and V6.2B unit suites with the release's test-only resources."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from acceptance.closure.profile import test_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New directory for suite logs and counts")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    python = ROOT / ".venv/bin/python"
    binary = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    groups = {
        "core": (ROOT / "gx-h", ["discover", "-s", "tests", "-v"]),
        "modules": (ROOT, ["discover", "-s", "acceptance/v62a", "-p", "test_*.py", "-v"]),
        "rounds": (ROOT, ["-v", "acceptance.v62b.test_rounds"]),
        "study": (ROOT, ["discover", "-s", "acceptance/v62b/study", "-p", "test_*.py", "-v"]),
    }
    results = {}
    with TemporaryDirectory(prefix="gx-b-") as temporary:
        for name, (cwd, tests) in groups.items():
            env = test_environment(Path(temporary) / name, python=python, binary=binary)
            env["PYTHONPATH"] = str(ROOT / "gx-h") + ":" + str(ROOT)
            path = args.output / (name + ".log")
            with path.open("wb") as log:
                code = subprocess.run([python, "-B", "-m", "unittest", *tests], cwd=cwd, env=env,
                                      stdout=log, stderr=subprocess.STDOUT).returncode
            data = path.read_bytes()
            count = re.search(rb"^Ran (\d+) tests? in ([\d.]+)s$", data, re.M)
            final = next((line for line in reversed(data.decode(errors="replace").splitlines())
                          if line == "OK" or line.startswith(("OK (", "FAILED ("))), None)
            results[name] = {"tests": int(count[1]) if count else None, "exit_code": code,
                             "unittest": final, "seconds": float(count[2]) if count else None,
                             "sha256": hashlib.sha256(data).hexdigest()}
            print(name, results[name], flush=True)
    (args.output / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(r["exit_code"] != 0 or r["tests"] is None for r in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
