"""Apply the pinned read-only archive reader to a scientist's literal query."""
import argparse
import json
from pathlib import Path
import runpy


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--reader", type=Path, required=True)
    cli.add_argument("--mount-member", type=Path, required=True)
    cli.add_argument("--taxid", type=int, required=True)
    cli.add_argument("--substrate", required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    reader = runpy.run_path(str(args.reader))
    record = reader["lookup"]({"schema": reader["QUERY"], "taxid": args.taxid,
                               "substrate": args.substrate}, args.mount_member.parents[2])
    (args.out / "parent-records.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
