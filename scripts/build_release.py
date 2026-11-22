#!/usr/bin/env python3
"""Pack the current GX candidate and its offline release inputs in a reproducible Linux tar."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from release_assets import ASSETS, LOCK, ROOT, digest, official_archive


def candidate_paths():
    def listed(*flags):
        data = subprocess.check_output(["git", "ls-files", "-z", *flags], cwd=ROOT)
        return {os.fsdecode(item) for item in data.split(b"\0") if item}

    paths = listed("--cached")
    # New release code, evidence and assets can be exercised before the maintainer tags a commit.
    # Ignore unrelated untracked directories and local installations, caches and project state.
    product_dirs = {"scripts", "gx-h", "bin", "codex_adapter", "gx_reference", "examples", "audit",
                    "acceptance", "release-assets", "acc_1"}
    for name in listed("--others", "--exclude-standard"):
        path = Path(name)
        if path.parts[0] in product_dirs or (len(path.parts) == 1 and path.suffix == ".md"):
            paths.add(name)
    return {name: ROOT / name for name in paths if (ROOT / name).is_file() or (ROOT / name).is_symlink()}


def header(name, mode, size):
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.size = size
    return member


def build(output, supplied):
    saved = ROOT / "RELEASE_CONTENTS.json"
    original = json.loads(saved.read_text()) if saved.is_file() else None
    if original:
        paths = {name: ROOT / name for name in original["files"]}
        base = original["git_base"]
    else:
        paths = candidate_paths()
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    pin = LOCK["linux_x64"]
    paths[(ASSETS / pin["file"]).relative_to(ROOT).as_posix()] = official_archive(supplied)
    files = {}
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        archive = Path(tmp) / "release.tar"
        with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
            for name, path in sorted(paths.items()):
                if path.is_symlink():
                    target = os.readlink(path)
                    member = header(name, 0o777, 0)
                    member.type, member.linkname = tarfile.SYMTYPE, target
                    tar.addfile(member)
                    files[name] = {"link": target}
                else:
                    mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    with path.open("rb") as stream:
                        value = hashlib.file_digest(stream, "sha256").hexdigest()
                        size = stream.seek(0, os.SEEK_END)
                        stream.seek(0)
                        tar.addfile(header(name, mode, size), stream)
                    files[name] = {"mode": format(mode, "o"), "sha256": value, "bytes": size}
            tree = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if original and tree != original["candidate_tree_sha256"]:
                raise ValueError("Extracted files differ from the manifest of the attachment being reproduced")
            manifest = {"format": 1, "git_base": base, "candidate_tree_sha256": tree, "files": files}
            body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            tar.addfile(header("RELEASE_CONTENTS.json", 0o644, len(body)), io.BytesIO(body))
        archive.replace(output)
    sha = digest(output)
    output.with_name(output.name + ".sha256").write_text(f"{sha}  {output.name}\n")
    return {"attachment": str(output), "sha256": sha, "bytes": output.stat().st_size,
            "candidate_tree_sha256": tree, "git_base": base, "files": len(files)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/gx-codex-p0-acc1-linux-x86_64.tar")
    parser.add_argument("--codex-archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.codex_archive), indent=2))
