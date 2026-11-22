"""Run the unmodified paired replay with both exported release trees on the acquisition filesystem."""

import argparse
import json
from pathlib import Path
import tempfile

import replay


def main():
    cli = argparse.ArgumentParser(add_help=False)
    cli.add_argument("--storage", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    args, _ = cli.parse_known_args()
    storage = args.storage.resolve()
    assert storage.name == "v3-real-performance"
    archive = replay.checkout
    with tempfile.TemporaryDirectory(
        dir=storage, prefix="callback-paid-replay-source-"
    ) as location:
        code_path = Path(location)

        def checkout_on_acquisition_mount(repos, temp):
            return archive(repos, code_path)

        replay.checkout = checkout_on_acquisition_mount
        try:
            replay.main()
        finally:
            replay.checkout = archive
    path = args.out / "results.json"
    data = json.loads(path.read_bytes())
    data["metadata"].update(
        source_export_root=str(code_path),
        source_export_removed=not code_path.exists(),
        source_export_environment="same HCFS mount as the SQLite state, artifact tree, metrics and historical paid acquisition",
        shared_checkout_script_sha256=replay.sha(Path(__file__).read_bytes()),
    )
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
