"""Run the V6.2.2 research regressions and retain their normal-entry receipts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from acceptance.closure.profile import test_environment
from scripts.release_assets import checked

OUTPUTS = {
    "GX_MEASUREMENT_DEFAULTS_RECEIPT": "R1-defaults.json",
    "GX_R4_REPAIR_TRACE": "R4-retries.json",
    "GX_R1_RECEIPT": "R1.json",
    "GX_V622_PROPOSITION_RECEIPTS": "propositions",
    "GX_R4_TRACE": "R4.json",
    "GX622_SOURCE_RECEIPTS": "sources",
    "GX622_CLOSE_TRACE_DIR": "close",
}


def run(work):
    if work.is_relative_to(ROOT):
        raise ValueError("Use a fresh directory outside the repository for the Run records")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    lock = json.loads((ROOT / "release-assets/lock.json").read_bytes())
    python = ROOT / ".venv/bin/python"
    binary = ROOT / "vendor/codex-runtime/package" / lock["linux_x64"]["binary"]
    checked(binary, lock["linux_x64"]["binary_sha256"])
    argv = [str(python), "-B", "-m", "unittest", "discover", "-s", "acceptance/v622",
            "-p", "test_*.py", "-v"]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with TemporaryDirectory() as tmp, (work / "repairs.log").open("wb") as log:
        env = test_environment(Path(tmp), python=python, binary=binary)
        env.update(GX_CODEX_OFFLINE="1", PYTHONPATH=os.pathsep.join(map(str, (ROOT, ROOT / "gx-h"))))
        env.update({key: str(work / path) for key, path in OUTPUTS.items()})
        result = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)
    receipt = dict(source_commit=head, argv=argv, cwd=str(ROOT), log="repairs.log",
                   receipt_outputs=OUTPUTS, exit_code=result.returncode, codex_offline=True,
                   binary_sha256=lock["linux_x64"]["binary_sha256"])
    (work / "command.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return result.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    sys.exit(run(parser.parse_args().work.resolve()))
