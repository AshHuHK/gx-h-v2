#!/usr/bin/env python3
"""Materialize verified Codex and Cordis release inputs without network access."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "release-assets"
LOCK = json.loads((ASSETS / "lock.json").read_text())


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def checked(path, expected, algorithm="sha256"):
    if digest(path, algorithm) != expected:
        raise ValueError(f"{algorithm} mismatch for {path}")
    return path


def integrity_digest(integrity):
    algorithm, encoded = integrity.split("-", 1)
    return algorithm, base64.b64decode(encoded, validate=True).hex()


def cache_key(integrity):
    algorithm, value = integrity_digest(integrity)
    return Path("_cacache/content-v2") / algorithm / value[:2] / value[2:4] / value[4:]


def official_archive(supplied=None):
    pin = LOCK["linux_x64"]
    archive = Path(supplied) if supplied else ASSETS / pin["file"]
    if not supplied and not archive.is_file():
        cache = Path(os.environ.get("npm_config_cache", Path.home() / ".npm"))
        archive = cache / cache_key(pin["integrity"])
    if not archive.is_file():
        raise FileNotFoundError(f"Supply the official {pin['file']} using --codex-archive. See RELEASE.md.")
    algorithm, expected = integrity_digest(pin["integrity"])
    checked(archive, expected, algorithm)
    return checked(archive, pin["sha256"])


def install_tar(archive, destination):
    if destination.is_dir():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as stage:
        tree = Path(stage) / "tree"
        tree.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(tree, filter="data")
        tree.rename(destination)


def verify_source(tree, archive):
    count = 0
    with tarfile.open(archive, "r|gz") as source:
        if source.pax_headers.get("comment") != LOCK["source"]["commit"]:
            raise ValueError("Codex git-archive commit differs from the release lock")
        for member in source:
            path = tree / member.name
            if member.issym():
                if path.readlink().as_posix() != member.linkname:
                    raise ValueError(f"Codex source symlink differs: {member.name}")
                count += 1
            elif member.isfile():
                with source.extractfile(member) as stream:
                    checked(path, hashlib.file_digest(stream, "sha256").hexdigest())
                if bool(member.mode & 0o111) != bool(path.stat().st_mode & 0o111):
                    raise ValueError(f"Codex source executable mode differs: {member.name}")
                count += 1
    checked(tree / "LICENSE", LOCK["source"]["license_sha256"])
    checked(tree / "NOTICE", LOCK["source"]["notice_sha256"])
    checked(ASSETS / "licenses/codex/LICENSE", LOCK["source"]["license_sha256"])
    checked(ASSETS / "licenses/codex/NOTICE", LOCK["source"]["notice_sha256"])
    return count


def verify_schema(tree):
    pin = LOCK["experimental_schema"]
    manifest = checked(ROOT / pin["manifest"], pin["manifest_sha256"])
    files = set()
    for line in manifest.read_text().splitlines():
        expected, name = line.split("  ", 1)
        checked(tree / name, expected)
        files.add(name)
    actual = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    if actual != files or len(files) != pin["files"]:
        raise ValueError("Experimental protocol file set differs from the release lock")
    return len(files)


def install_codex(supplied):
    source, schema = LOCK["source"], LOCK["experimental_schema"]
    source_archive = checked(ASSETS / source["file"], source["sha256"])
    schema_archive = checked(ASSETS / schema["file"], schema["sha256"])
    tree, schemas = ROOT / "vendor/codex", ROOT / "generated/codex-schema"
    install_tar(source_archive, tree)
    install_tar(schema_archive, schemas)
    source_count = verify_source(tree, source_archive)
    schema_count = verify_schema(schemas)
    runtime = ROOT / "vendor/codex-runtime"
    install_tar(official_archive(supplied), runtime)
    binary = checked(runtime / "package" / LOCK["linux_x64"]["binary"], LOCK["linux_x64"]["binary_sha256"])
    if not os.access(binary, os.X_OK):
        raise ValueError("Bundled Codex is missing its executable bit")
    return {"source_commit": source["commit"], "source_files": source_count, "experimental_schema_files": schema_count,
            "official_codex": str(binary), "binary_sha256": digest(binary)}


def install_cordis(*, manifest=None, archives=None, env=None, npm="npm"):
    manifest = manifest or ROOT / LOCK["cordis_lock"]
    archives = archives or ASSETS / "npm"
    packages = {k: v for k, v in json.loads(manifest.read_text())["packages"].items() if k}
    with tempfile.TemporaryDirectory(dir=manifest.parent) as tmp:
        stage = Path(tmp)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(manifest.parent / name, stage / name)
        for pin in packages.values():
            archive = archives / pin["resolved"].rsplit("/", 1)[1]
            key = cache_key(pin["integrity"])
            algorithm, expected = integrity_digest(pin["integrity"])
            checked(archive, expected, algorithm)
            cached = stage / "cache" / key
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, cached)
        target = manifest.parent / "node_modules"
        if not target.exists():
            subprocess.run([npm, "ci", "--offline", "--ignore-scripts", "--no-fund", "--no-audit",
                            "--cache", str(stage / "cache")], cwd=stage, check=True, stdout=subprocess.DEVNULL, env=env)
            (stage / "node_modules").rename(target)
    for path, pin in packages.items():
        installed = json.loads((manifest.parent / path / "package.json").read_text())
        if installed["version"] != pin["version"]:
            raise ValueError(f"{path} has {installed['version']}, expected {pin['version']}")
    return {path: pin["version"] for path, pin in packages.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("codex", "cordis", "all"), nargs="?", default="all")
    parser.add_argument("--codex-archive", type=Path)
    args = parser.parse_args()
    report = {}
    if args.component in ("codex", "all"):
        report["codex"] = install_codex(args.codex_archive)
    if args.component in ("cordis", "all"):
        report["cordis"] = install_cordis()
    print(json.dumps(report, indent=2))
