"""Write and verify one acceptance run, including every compressed evidence file."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile


class IntegrityError(ValueError):
    pass


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    temp.replace(path)


def read(path):
    return json.loads(path.read_text())


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_manifest(path, files):
    path.write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(files.items())))


def read_manifest(path):
    entries = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise IntegrityError(f"malformed checksum in {path.name}")
        digest, name = match.groups()
        parts = PurePosixPath(name)
        if (parts.is_absolute() or ".." in parts.parts or str(parts) != name
                or name == "." or name in entries):
            raise IntegrityError(f"invalid or duplicate evidence name: {name}")
        entries[name] = digest
    if not entries:
        raise IntegrityError(f"empty checksum manifest: {path.name}")
    return entries


PACKAGE_FILES = {"environment.json", "commands.json", "summary.json", "reproduce.json",
                 "raw.tar.gz", "raw-files.sha256"}


def package(run, raw):
    files = {}
    with tarfile.open(run / "raw.tar.gz", "w:gz", compresslevel=6) as archive:
        for path in sorted(raw.rglob("*")):
            if path.is_symlink():
                raise IntegrityError(f"unexpected evidence symlink: {path}")
            if path.is_file():
                name = path.relative_to(raw).as_posix()
                files[name] = sha256(path)
                archive.add(path, arcname=name, recursive=False)
    write_manifest(run / "raw-files.sha256", files)
    write_manifest(run / "manifest.sha256", {name: sha256(run / name) for name in PACKAGE_FILES})


def check(run, extract_to=None):
    outer = read_manifest(run / "manifest.sha256")
    if set(outer) != PACKAGE_FILES:
        raise IntegrityError("incomplete run manifest")
    for name, expected in outer.items():
        if sha256(run / name) != expected:
            raise IntegrityError(f"checksum mismatch: {name}")

    raw = read_manifest(run / "raw-files.sha256")
    seen = set()
    with tarfile.open(run / "raw.tar.gz", "r|gz") as archive:
        for member in archive:
            name = member.name
            if not member.isfile() or name in seen or name not in raw:
                raise IntegrityError(f"unexpected archive member: {name}")
            with archive.extractfile(member) as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != raw[name]:
                    raise IntegrityError(f"checksum mismatch: raw/{name}")
            seen.add(name)
    if seen != set(raw):
        raise IntegrityError(f"missing archive members: {sorted(set(raw) - seen)}")
    if extract_to is not None:
        extract_to.mkdir(parents=True, exist_ok=False)
        with tarfile.open(run / "raw.tar.gz", "r|gz") as archive:
            for member in archive:
                path = extract_to / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
    return {"run_id": run.name, "package_files": len(outer), "raw_files": len(raw),
            "raw_sha256": outer["raw.tar.gz"]}
