"""Start a chosen release using the operator's stock OAuth host and that release's Python."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(SOURCE), str(SOURCE / "gx-h")]
from acceptance.v623.oauth import host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("binary", type=Path)
    parser.add_argument("private", type=Path)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    target = options.deployment.resolve()
    private = options.private.resolve()
    private.mkdir(mode=0o700, exist_ok=True, parents=True)
    tag = hashlib.sha256(os.fsencode(private)).hexdigest()
    env = host._env(host._state()/("science-" + tag), str(options.binary.resolve()), "gpt-6.2-astra", "max",
                    sessions=private/"codex/sessions")
    node = shutil.which("node")
    paths = [str(target/".venv/bin")] + ([str(Path(node).parent)] if node else [])
    env["PATH"] = os.pathsep.join([*paths, env["PATH"]])
    os.chdir(target)
    binary = str(target/"bin/gx-p0")
    os.execve(binary, [binary, *options.args], env)


if __name__ == "__main__":
    main()
