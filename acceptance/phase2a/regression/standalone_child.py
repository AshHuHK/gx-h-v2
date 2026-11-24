"""Run the candidate's official child double-loss experiment for the legacy supplemental gate."""
import argparse
import json
from pathlib import Path
import subprocess


def run(stage, out):
    stage = stage.resolve()
    out.mkdir(parents=True, exist_ok=False)
    python = stage / ".venv/bin/python"
    binary = stage / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    command = [str(python), "-B", "acceptance/closure/child_double_loss.py",
               "--out", str(out / "evidence"), "--binary", str(binary)]
    with (out / "child.log").open("w") as log:
        attempt = subprocess.run(command, cwd=stage, stdout=log, stderr=subprocess.STDOUT)
    receipt = out / "evidence/receipt.json"
    data = json.loads(receipt.read_text()) if receipt.is_file() else {}
    status = "PASS" if attempt.returncode == 0 and data.get("status") == "PASS" and data.get("closure") == "CLOSED" else "FAIL"
    report = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=stage, text=True).strip(),
              "gate": "supplemental official child double loss and cold Driver settlement",
              "argv": command, "returncode": attempt.returncode, "observed_status": status,
              "receipt": "evidence/receipt.json", "manifest": "evidence/manifest.json"}
    (out / "observation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.stage, args.out.resolve()))
