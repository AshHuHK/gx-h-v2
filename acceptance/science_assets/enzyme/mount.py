"""Install the read-only archive mount into one GX release without copying data."""
import argparse
import json
from pathlib import Path
import runpy

if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--harness-root", required=True, type=Path)
    cli.add_argument("--source", required=True, type=Path)
    args = cli.parse_args()
    root = args.harness_root.resolve(strict=True)
    src = args.source.resolve(strict=True)
    package = root / "plugins/installed/org.gx.yeast-enzyme-archive/1.0.0"
    reader = runpy.run_path(str(package / "archive.py"))
    reader["verify_snapshots"](src, json.loads((package / "snapshots.json").read_bytes()))
    target = root / "var/science-assets/yeast-archive"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        if target.resolve(strict=True) != src:
            cli.error("Existing yeast archive mount points to a different location")
    else:
        target.symlink_to(src, target_is_directory=True)
    print("Verified four source snapshots on the yeast-archive mount")
