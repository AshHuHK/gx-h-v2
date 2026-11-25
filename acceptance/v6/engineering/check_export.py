"""Check an existing GX evidence export against the reviewer's detached anchor."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

V6 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V6))
from verify import verify


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("observed", "controls"))
    p.add_argument("--input-dir", type=Path)
    p.add_argument("--anchor", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    code = 0
    result = {"check": "detached export consistency", "mode": args.mode}
    if args.input_dir is None or args.anchor is None or not (args.input_dir / "bundle.json").is_file() or not args.anchor.is_file():
        result.update(status="BLOCKED", reason=f"Supply --{args.mode}-export and --{args.mode}-anchor from this experiment.")
        code = 2
    else:
        result.update(bundle_sha256=hashlib.sha256((args.input_dir / "bundle.json").read_bytes()).hexdigest(),
                      anchor_sha256=hashlib.sha256(args.anchor.read_bytes()).hexdigest())
        try:
            mode, count = verify(args.input_dir, args.anchor)
            if mode != args.mode:
                raise ValueError(f"Expected {args.mode}, got {mode}")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            result.update(status="FAIL", reason=str(exc))
            code = 1
        else:
            result.update(status="PASS", runs=count)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
