"""Record the installed research code, two site registries and prospective field protocols."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


PRODUCT = ("bin", "release-assets", "gx-h", "gx-h/gx_core", "gx-h/adapters",
           "gx-h/config", "gx-h/contracts", "gx-h/plugins")
SITE = ("gx-h/contracts/CONTRACT_SHA", "gx-h/config/workflow_packages.json",
        "gx-h/config/science_site_access.json", "gx-h/config/codex_project_role_pins.json")
PROTOCOLS = ("field/preregistered.json", "field/materials.json", "field/readiness.json",
             "gpu/preregistration.json", "benchmark/long/preregistered.json", "benchmark/short/preregistered.json")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def digests(root, files):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def site(root, commit):
    commit = git(root, "rev-parse", commit or "HEAD")
    if git(root, "status", "--porcelain", "--", *PRODUCT):
        raise ValueError(f"Commit the deployed product before freezing {root.name}")
    if git(root, "diff", "--name-only", commit, "--", *PRODUCT):
        raise ValueError("The selected deployment has changed since its candidate commit")
    return {
        "branch": git(root, "branch", "--show-current"),
        "commit": commit,
        "product_trees": {name: git(root, "rev-parse", commit + ":" + name) for name in PRODUCT},
        "site_files_sha256": digests(root, SITE),
        "python": subprocess.check_output([str(root / ".venv/bin/python"), "-V"], text=True).strip(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("full", type=Path)
    parser.add_argument("slim", type=Path)
    parser.add_argument("--full-commit")
    parser.add_argument("--slim-commit")
    args = parser.parse_args()
    full, slim = args.full.resolve(), args.slim.resolve()
    result = {"recorded_utc": datetime.now(timezone.utc).isoformat(),
              "full": site(full, args.full_commit), "slim": site(slim, args.slim_commit),
              "protocol_files_sha256": digests(full / "acceptance/harness_final", PROTOCOLS)}
    print(json.dumps(result, indent=2))
