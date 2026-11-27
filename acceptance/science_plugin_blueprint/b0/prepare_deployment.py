"""Configure an operator-selected private GX installation for its archived Top7 GPU Goal."""
import argparse
import json
import os
from pathlib import Path
import tempfile


def write_json(path, body):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(body, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        name = stream.name
    os.chmod(name, path.stat().st_mode & 0o777)
    os.replace(name, path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--deployment", type=Path, required=True, help="Private product checkout with gx-h/config")
    for name in ("env", "source", "weights"):
        p.add_argument("--" + name, type=Path, required=True, help="Existing read-only AF3 directory")
    args = p.parse_args()
    h = args.deployment.resolve() / "gx-h"
    if not (h / "config/workflow_packages.json").is_file():
        p.error("Select an installed GX product root")
    mounted = h / "var/archival-af3"
    mounted.mkdir(parents=True, exist_ok=True)
    for name, required in (("env","bin/python"), ("source","run_alphafold.py"), ("weights","af3.bin.zst")):
        target = getattr(args, name).resolve(strict=True)
        if not (target / required).is_file():
            p.error(f"{target} is missing {required}")
        dest = mounted / name
        if dest.is_symlink() and dest.resolve() == target:
            continue
        if dest.exists() or dest.is_symlink():
            p.error(f"Existing mount differs at {dest}")
        dest.symlink_to(target, target_is_directory=True)
    limits_path = h / "config/limits.json"
    limits = json.loads(limits_path.read_bytes())
    limits["switches"]["gpu_jobs"] = {"value": True, "scope": "archival_fold_calibration"}
    limits["gpu"]["max_self_memory_mib"] = 16384
    write_json(limits_path, limits)
    approvals_path = h / "config/approvals.json"
    approvals = json.loads(approvals_path.read_bytes())
    if not any(rule["action_kind"] == "gpu_job" for rule in approvals["rules"]):
        approvals["rules"].append({"id": "archival-calibration-within-goal-gpu-budget", "action_kind": "gpu_job"})
        write_json(approvals_path, approvals)


if __name__ == "__main__":
    main()
