"""Run the ordinary GX CLI with the deployment's existing stock ChatGPT OAuth account."""
import argparse
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v623.oauth import host, pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path, help="Private directory for this study's Codex sessions")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="gx-p0 command and its ordinary arguments")
    args = parser.parse_args()
    if not args.command:
        parser.error("supply a gx-p0 command")
    work = args.work.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary, _ = host._pin(ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
    identity = hashlib.sha256(os.fsencode(work)).hexdigest()
    env = host._env(host._state() / ("science-" + identity), binary, "gpt-6.2-astra", "max",
                    sessions=work / "codex/sessions")
    env["PATH"] = pilot.science_plugin_path(env)
    os.chdir(ROOT)
    cli = str(ROOT / "bin/gx-p0")
    os.execve(cli, [cli, *args.command], env)


if __name__ == "__main__":
    main()
