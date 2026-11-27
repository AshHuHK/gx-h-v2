"""Run the ordinary GX CLI with the deployment's existing stock ChatGPT OAuth account."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v623.oauth import host
from gx_core.p0.profiles import codex_executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path, help="Private directory for this study's Codex sessions")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="gx-p0 command and its ordinary arguments")
    args = parser.parse_args()
    if not args.command:
        parser.error("supply a gx-p0 command")
    command = codex_executable()
    binary = shutil.which(command)
    if binary is None:
        parser.exit(2, f"GX cannot find {command}. Put Codex on PATH or set GX_P0_CODEX.\n")
    binary = os.path.abspath(binary)
    work = args.work.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = hashlib.sha256(os.fsencode(work)).hexdigest()
    env = host._env(host._state() / ("science-" + identity), binary, "gpt-6.2-astra", "max",
                    sessions=work / "codex/sessions")
    node = shutil.which("node")
    if node:
        env["PATH"] = os.path.dirname(os.path.abspath(node)) + os.pathsep + env["PATH"]
    os.chdir(ROOT)
    cli = str(ROOT / "bin/gx-p0")
    os.execve(cli, [cli, *args.command], env)


if __name__ == "__main__":
    main()
