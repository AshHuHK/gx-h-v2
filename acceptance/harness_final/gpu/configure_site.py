"""Prepare an operator-selected local deployment for the registered natural-food prediction."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance/science_plugin_blueprint/b0"))
from prepare_deployment import write_json


def configure(release, mount):
    harness = release / "gx-h"
    package = "org.gx.natural-food-fold/1.0.0"
    if not (harness / "plugins/installed" / package / "gx_plugin.json").is_file():
        raise ValueError("Install the natural food prediction provider before configuring the station.")
    local = harness / "var/archival-af3"
    local.mkdir(parents=True, exist_ok=True)
    for name, relative in (("source", "run_alphafold.py"), ("env", "bin/python"), ("weights", "af3.bin.zst")):
        source, extension = (mount / name).resolve(strict=True), local / name
        if not (source / relative).is_file():
            raise ValueError(f"Missing required site file {name}/{relative}")
        if extension.is_symlink() and extension.resolve() == source:
            continue
        if extension.exists() or extension.is_symlink():
            raise ValueError(f"Existing mount points elsewhere at {extension}")
        extension.symlink_to(source, target_is_directory=True)
    limits_path = harness / "config/limits.json"
    limits = json.loads(limits_path.read_bytes())
    scope = "natural_food_structure"
    if limits["switches"]["gpu_jobs"]["value"] and limits["switches"]["gpu_jobs"].get("scope") != scope:
        raise ValueError("Use a separate field deployment to preserve the station's existing GPU scope.")
    limits["switches"]["gpu_jobs"] = {"value": True, "scope": scope}
    limits["gpu"]["max_self_memory_mib"] = max(limits["gpu"]["max_self_memory_mib"], 16384)
    write_json(limits_path, limits)
    approvals_path = harness / "config/approvals.json"
    approvals = json.loads(approvals_path.read_bytes())
    if not any(rule["action_kind"] == "gpu_job" for rule in approvals["rules"]):
        approvals["rules"].append({"id": "food-shape-within-declared-gpu-budget", "action_kind": "gpu_job"})
        write_json(approvals_path, approvals)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("release", type=Path)
    p.add_argument("mount", type=Path, help="Existing read-only AF3 source/env/weights station mount")
    args = p.parse_args()
    configure(args.release.resolve(), args.mount.resolve())
