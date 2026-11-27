"""Discover exact operator-installed plugin packages and capture their verified bytes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..domain.ids import sha256_of_obj
from .manifest import Manifest, check_identity, parse_manifest


Key = tuple[str, str]


@dataclass(frozen=True)
class Pin:
    id: str
    version: str
    digest: str


@dataclass(frozen=True)
class Package:
    pin: Pin
    manifest: Manifest
    directory: Path
    files: Mapping[str, bytes]


@contextmanager
def _directory(name, parent=None):
    if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
        raise ValueError(f"symlink in plugin directory path: {name}")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _physical_directory(path: Path):
    physical = Path(os.path.abspath(path))
    with ExitStack() as stack:
        directory = stack.enter_context(_directory(physical.anchor))
        for part in physical.parts[1:]:
            directory = stack.enter_context(_directory(part, directory))
        yield directory


def _tree(directory: int, prefix: str, files: dict[str, bytes]) -> None:
    for name in os.listdir(directory):
        relative = prefix + name
        mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            with _directory(name, directory) as child:
                _tree(child, relative + "/", files)
        elif stat.S_ISREG(mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError(f"package member is not a regular file: {relative}")
                files[relative] = stream.read()
        else:
            kind = "symlink" if stat.S_ISLNK(mode) else "special file"
            raise ValueError(f"package contains {kind}: {relative}")


def read_package(path: Path) -> dict[str, bytes]:
    """Read the operator's source directory, preserving every regular file."""
    files = {}
    with _physical_directory(path) as directory:
        _tree(directory, "", files)
    return files


def digest_files(files: Mapping[str, bytes]) -> str:
    index = [(name, hashlib.sha256(files[name]).hexdigest()) for name in sorted(files)]
    return sha256_of_obj(index)


def package_digest(path: Path) -> str:
    return digest_files(read_package(path))


class Catalog:
    """The installation document selects code by path and digest only."""

    def __init__(self, install_root: Path, catalog_path: Path):
        self.root = Path(os.path.abspath(install_root))
        document = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("plugins", []), list):
            raise ValueError("installation catalog must have a plugins array")
        self._locations = {}
        for row in document.get("plugins", []):
            if isinstance(row, dict) and isinstance(row.get("path"), str):
                self._locations.setdefault(row["path"], []).append(row)

    def has(self, key: Key) -> bool:
        return "/".join(key) in self._locations

    def verified(self, key: Key) -> Package:
        plugin_id, version = key
        check_identity(plugin_id, version)
        location = "/".join(key)
        records = self._locations.get(location, ())
        if not records:
            raise ValueError("no operator-approved installation")
        if len(records) != 1:
            raise ValueError(f"duplicate installation records for {plugin_id}@{version}")
        record = records[0]
        digest = record.get("sha256")
        if (set(record) != {"path", "sha256"} or not isinstance(digest, str)
                or not re.fullmatch("[a-f0-9]{64}", digest)):
            raise ValueError(f"invalid path or sha256 record for {location}")
        files = {}
        with _physical_directory(self.root / location) as directory:
            _tree(directory, "", files)
        actual = digest_files(files)
        if actual != digest:
            raise ValueError(f"package digest mismatch for {location}: expected {digest}, got {actual}")
        if "gx_plugin.json" not in files:
            raise ValueError(f"missing gx_plugin.json in {location}")
        manifest = parse_manifest(files["gx_plugin.json"])
        if (manifest.id, manifest.version) != key:
            raise ValueError(f"manifest {manifest.id}@{manifest.version} disagrees with installation {location}")
        return Package(Pin(plugin_id, version, digest), manifest, self.root / location, MappingProxyType(files))
