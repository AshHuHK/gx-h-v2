"""The selftest tool: does exactly what its ``--mode`` says, so a test knows what to expect.

    echo        write the input, upper-cased, to <out>/report.txt
    sleep       sleep --seconds, then behave like echo
    fail        write a line to stderr and exit 3
    silent      exit 0 and write nothing
    child       start a child process that would sleep for an hour, record its pid in
                <out>/child.pid, then sleep --seconds
"""

import argparse
import os
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True,
                        choices=["echo", "sleep", "fail", "silent", "child"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seconds", type=float, default=0.0)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as handle:
        text = handle.read()
    if args.mode == "fail":
        print("selftest: asked to fail", file=sys.stderr)
        return 3
    if args.mode == "silent":
        return 0
    if args.mode == "child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        with open(os.path.join(args.out, "child.pid"), "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
    if args.mode in ("sleep", "child"):
        time.sleep(args.seconds)
    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as handle:
        handle.write(args.mode + "\n" + text.upper())
    print("selftest: done", args.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
