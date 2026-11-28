"""Replay identical business probes on an isolated released tree and candidate source snapshot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


BASE = "50d31f5792a178529a817ffd93f66ef9cbbb0be4"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
PRODUCT = ["bin", "gx-h"]
FIXTURES = ["examples", "acceptance/v62a/interleavings_fixture.py",
            "acceptance/v621/test_evidence_gate.py", "acceptance/v622/test_close.py"]
TREES = PRODUCT + FIXTURES
PREFIX = "acceptance.harness_refactor."
CASES = [
    PREFIX + "probes.test_probes.Probes.test_single_material_and_method_revision_are_uniquely_bound",
    PREFIX + "probes.test_probes.Probes.test_two_steps_bind_real_output_and_charge_each_job_once",
    PREFIX + "closing.test_closeout.Closeout.test_objective_grades_require_body_or_actual_model_and_validation",
]
BASELINE_ERRORS = (
    "add_node at $.ops[0].inputs: missing required property 'inputs'",
    "Open an explicit funded branch for this measurement",
    "'body passage' not found in 'SCHEMA_INVALID: claim needs",
)
PROBES = ["acceptance/harness_refactor/probes/test_probes.py",
          "acceptance/harness_refactor/closing/test_closeout.py"]


def copy_from_worktree(rel, dest):
    src, target = ROOT / rel, dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target, follow_symlinks=False)


def snapshot(path, release):
    paths = TREES if release else FIXTURES
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "archive", BASE, "--", *paths], cwd=ROOT, stdout=archive, check=True)
        archive.seek(0)
        subprocess.run(["tar", "-xf", "-", "-C", path], stdin=archive, check=True)
    if not release:
        files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                                        "-z", "--", *PRODUCT], cwd=ROOT)
        for rel in os.fsdecode(files).split("\0"):
            if rel and os.path.lexists(ROOT / rel):
                copy_from_worktree(rel, path)
    for rel in PROBES:
        copy_from_worktree(rel, path)
    inventory = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            if not item.resolve(strict=True).is_relative_to(path):
                raise ValueError(f"External symlink in tested tree: {item}")
            data = os.fsencode(os.readlink(item))
        elif item.is_file():
            data = item.read_bytes()
        else:
            continue
        inventory.update(os.fsencode(item.relative_to(path)) + b"\0" + hashlib.sha256(data).digest())
    return inventory.hexdigest()


# Reporting a failed import as a product regression would invalidate the comparison.
RUNNER = """
import json, os, pathlib, sys, unittest
root = pathlib.Path.cwd()
for entry in ('gx-h','gx-h/tests','acceptance/v62a','.'):
    sys.path.insert(0, str(root / entry))
import gx_core
from acceptance.v62a import interleavings_fixture as fixture
assert fixture.PRODUCT.resolve() == root and pathlib.Path(gx_core.__file__).is_relative_to(root)
cases = sys.argv[1:]
for case in cases:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(case))
    if result.testsRun != 1 or result.skipped:
        raise RuntimeError('Probe did not execute to its business assertion')
    rows = result.errors + result.failures
    if any('ModuleNotFoundError' in tb or 'ImportError:' in tb or 'FileNotFoundError:' in tb for _,tb in rows):
        raise RuntimeError('Test infrastructure unavailable')
    print(json.dumps({'case':case, 'passed':result.wasSuccessful(),
                      'failure':[tb.strip().splitlines()[-1] for _,tb in rows]}), flush=True)
for name, module in list(sys.modules.items()):
    if name.split('.')[0] in ('gx_core','tests','acceptance') and getattr(module,'__file__',None):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), (name,module.__file__)
"""


def run(tree, destination, release):
    scratch = tree.parent / "scratch"
    scratch.mkdir()
    # Only the system interpreter/dependencies are shared. Test subprocesses import
    # the CLI from their own release. State stays beside the isolated product.
    imports = [str(tree / p) for p in (".", "gx-h", "gx-h/tests", "acceptance/v62a")]
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("GX_", "PYTHON")) or "PROXY" in k.upper()}
    environ.update(TMPDIR=str(scratch), PYTHONPATH=os.pathsep.join(imports),
                   GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    if not release:
        environ["GX_PROBE_RECEIPT"] = str(scratch / "two-jobs.json")
    binary = Path(sys._base_executable).resolve()
    if binary.is_relative_to(ROOT):
        raise RuntimeError("Use an interpreter independent of this checkout")
    completed = subprocess.run([binary, "-I", "-B", "-c", RUNNER, *CASES], cwd=tree, env=environ,
                               capture_output=True, text=True, timeout=150)
    label = "red" if release else "green"
    (destination / f"{label}.log").write_text(completed.stderr + completed.stdout)
    completed.check_returncode()
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    if [r["case"] for r in rows] != CASES:
        raise ValueError("Missing probe results")
    if any(r["passed"] == release for r in rows):
        raise ValueError(f"Unexpected {label} business result; inspect {destination}/{label}.log")
    if release:
        for row, expected in zip(rows, BASELINE_ERRORS, strict=True):
            if len(row["failure"]) != 1 or expected not in row["failure"][0]:
                raise ValueError(f"Expected business failure was not reached: {row}")
    else:
        shutil.copy2(scratch / "two-jobs.json", destination / "two-jobs.json")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Directory for the fresh logs and tree digests")
    out = parser.parse_args().out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary = {"baseline": BASE, "source_paths": TREES, "same_probes_sha256": {
        rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() for rel in PROBES}}
    for label in ("baseline", "candidate"):
        with tempfile.TemporaryDirectory(prefix="gx-refactor-" + label + "-") as tmp:
            tree = Path(tmp).resolve() / "product"
            tree.mkdir()
            digest = snapshot(tree, label == "baseline")
            summary[label + "_replay"] = {"tree_sha256": digest, "cases": run(tree, out, label == "baseline")}
    (out / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
