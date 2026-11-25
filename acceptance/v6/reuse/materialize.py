"""Materialize the V6 reuse set or verify its bytes offline. Python stdlib + git/curl."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
PROXY = "http://127.0.0.1:7890"


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def download(url, output):
    with socket.create_connection(("127.0.0.1", 7890), timeout=4):
        pass
    env = dict(os.environ, NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    subprocess.run(
        ["curl", "-q", "--silent", "--show-error", "--fail", "--location",
         "--proto", "=https", "--proto-redir", "=https", "--proxy", PROXY,
         "--connect-timeout", "10", "--max-time", "300", "--output", str(output), url],
        env=env, check=True,
    )


def transfer(source, target, args, manifest):
    pin = manifest["gx_v2"]["commit"]
    if source.get("archive"):
        subprocess.run(["git", "-C", str(args.repo), "-c", "tar.umask=0002", "archive",
                        "--format=tar", "--output", str(target), pin, "--", *source["archive"]], check=True)
    elif source.get("git_blob_path"):
        with target.open("wb") as output:
            subprocess.run(["git", "-C", str(args.repo), "cat-file", "blob", pin + ":" + source["git_blob_path"]],
                           check=True, stdout=output)
    elif source.get("kit_file"):
        shutil.copyfile(args.kits / source["kit_file"], target)
    elif source.get("binary") and args.binary_archive:
        shutil.copyfile(args.binary_archive, target)
    else:
        download(source["url"], target)


def check_file(target, record):
    return target.is_file() and target.stat().st_size == record["bytes"] and digest(target) == record["sha256"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["fetch", "verify"])
    parser.add_argument("destination", type=Path)
    parser.add_argument("--repo", type=Path, default=HERE.parents[2], help="gx-h-v2 checkout with the baseline Git commit")
    parser.add_argument("--kits", type=Path, default=HERE.parents[3], help="directory with the two original user ZIPs")
    parser.add_argument("--binary-archive", type=Path, help="optional exact release npm archive already fetched")
    args = parser.parse_args()
    manifest = json.loads((HERE / "manifest.json").read_text())
    entries = list(manifest["materials"])
    for project in manifest["external_references"]:
        repo, pin = project["repository"], project["commit"]
        for item in project["files"]:
            entries.append(dict(item, path=f"upstream/{repo.split('/')[-1]}/{item['file']}",
                                source={"url": f"https://raw.githubusercontent.com/{repo}/{pin}/{item['file']}"}))
    for entry in entries:
        path = args.destination.resolve() / entry["path"]
        if check_file(path, entry):
            continue
        if args.action == "verify":
            raise SystemExit(f"missing or changed: {entry['path']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            pending = Path(temp.name)
        try:
            transfer(entry["source"], pending, args, manifest)
            if not check_file(pending, entry):
                raise SystemExit(f"upstream bytes differ from pin: {entry['path']}")
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    print(f"VERIFIED {len(entries)} files ({sum(e['bytes'] for e in entries)} bytes)")


if __name__ == "__main__":
    main()
