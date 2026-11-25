"""Reproduce the V6.2.1 offline repair receipts from a provisioned release."""
import argparse
import hashlib
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

TESTS = [
    "acceptance.v621.test_claim_publication",
    "acceptance.v621.test_gap_resolution",
    "acceptance.v621.test_verified_metrics",
    "acceptance.v621.test_material_assignment",
    "acceptance.v621.test_goal_versions",
    "acceptance.v621.test_evidence_gate",
    "acceptance.v621.test_contract",
    "tests.test_v621_named_outputs",
    "tests.test_v621_coordinate_sources",
]
RECEIPTS = {
    "GX_V621_CLAIM_RECEIPTS": "claim-receipts",
    "GX_V621_METRICS_TRACE": "metrics-trace.json",
    "GX_GATE_TRACE": "gate-trace.json",
    "GX_V621_ASSIGNMENT_RECEIPT": "assigned-job.json",
    "GX_GAP_REGRESSION_RECEIPT": "gap-after.json",
    "GX_GAP_REFUSAL_RECEIPT": "gap-mixed-refusal.json",
    "GX_GAP_MATCH_RECEIPT": "gap-two-matches.json",
    "GX_V621_ANALYSIS_GAP_RECEIPT": "gap-acquired-analysis.json",
    "GX621_GOAL_TRACE": "goal-trace.json",
    "GX_NAMED_OUTPUT_RECEIPT": "named-receipt.json",
}


def run(work):
    if work.is_relative_to(ROOT):
        raise ValueError("Choose a new receipt directory outside the checkout")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    lock = json.loads((ROOT / "release-assets/lock.json").read_bytes())
    python = ROOT / ".venv/bin/python"
    binary = ROOT / "vendor/codex-runtime/package" / lock["linux_x64"]["binary"]
    args = [str(python), "-B", "-m", "unittest", "-v", *TESTS]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    record = {"source_commit": head, "cwd": str(ROOT), "argv": args, "profile": "deterministic",
              "model": "scripted native replies", "codex_offline": True, "log": "repairs.log",
              "receipt_outputs": RECEIPTS, "exit_code": None}
    log_path = work / record["log"]
    with TemporaryDirectory(prefix="gx-621-") as temporary, log_path.open("w") as log:
        env = test_environment(Path(temporary), python=python, binary=binary)
        env.update(GX_CODEX_OFFLINE="1", PYTHONPATH=os.pathsep.join(map(str, [ROOT, ROOT / "gx-h"])))
        env.update({key: str(work / path) for key, path in RECEIPTS.items()})
        try:
            checked(binary, lock["linux_x64"]["binary_sha256"])
            version = subprocess.check_output([str(python), "-I", "-S", "-c",
                                               "import platform; print(platform.python_version())"], env=env, text=True).strip()
            if version != lock["deterministic_test_runtime"]["python"]:
                raise ValueError("The provisioned Python version differs from release-assets/lock.json")
            record.update(python=version, codex_version=lock["codex_version"], codex_sha256=lock["linux_x64"]["binary_sha256"])
            record["exit_code"] = subprocess.run(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
            expected = [p for p in RECEIPTS.values() if p.endswith(".json")]
            expected.append("gate-trace.review.json")
            expected.extend(f"claim-receipts/{name}.json" for name in ("supported", "opposed", "mixed", "different-endpoint"))
            if record["exit_code"] == 0:
                for path in expected:
                    json.loads((work / path).read_bytes())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(str(error), file=log)
            record.update(exit_code=1, reason=str(error))
    record["log_sha256"] = hashlib.sha256(log_path.read_bytes()).hexdigest()
    (work / "command.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"Repair suite exited {record['exit_code']}. See {log_path}")
    return record["exit_code"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path, help="new external directory for logs and scientific receipts")
    raise SystemExit(run(parser.parse_args().work.resolve()))
