"""Operator commands for locally reviewed GX plugin packages."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .. import config


def add_commands(commands: argparse._SubParsersAction) -> None:
    group = commands.add_parser("plugin", help="Manage installed research plugins")
    actions = group.add_subparsers(dest="action", required=True)
    digest = actions.add_parser("digest", help="Calculate the content digest of a package")
    digest.add_argument("source", type=Path)
    install = actions.add_parser("install", help="Install a package matching the reviewed digest")
    install.add_argument("source", type=Path)
    install.add_argument("--sha256", required=True)
    actions.add_parser("list", help="List operator-approved install records")
    verify = actions.add_parser("verify", help="Load the exact versions and their dependencies")
    verify.add_argument("plugins", nargs="+", metavar="ID@VERSION")
    group.set_defaults(deployment_handler=run)


@contextmanager
def _catalog_writer(path):
    with path.with_name(".plugin-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_catalog(path, content):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            json.dump(content, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            os.chmod(name, path.stat().st_mode & 0o777)
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def _install(source, expected, root, catalog_path):
    from .catalog import digest_files, package_digest, read_package
    from .manifest import parse_manifest

    files = read_package(source)
    digest = digest_files(files)
    if digest != expected:
        raise ValueError(f"reviewed digest mismatch: package digest is {digest}")
    manifest = parse_manifest(files["gx_plugin.json"])
    relative = f"{manifest.id}/{manifest.version}"
    destination = root / relative
    record = {"path": relative, "sha256": digest}
    with _catalog_writer(catalog_path):
        catalog = json.loads(catalog_path.read_bytes())
        entries = catalog.setdefault("plugins", [])
        matches = [row for row in entries if row["path"] == relative]
        if matches and matches != [record]:
            raise ValueError(f"installed version has a conflicting catalog record: {relative}")
        if root.is_symlink() or destination.parent.is_symlink() or destination.is_symlink():
            raise ValueError(f"install directory must be a physical directory: {relative}")
        if destination.exists():
            if package_digest(destination) != digest:
                raise ValueError(f"installed files differ from the reviewed package: {relative}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=destination.parent) as stage:
                tree = Path(stage) / "package"
                tree.mkdir()
                for filename, data in files.items():
                    target = tree / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                os.rename(tree, destination)
        if not matches:
            entries.append(record)
            _write_catalog(catalog_path, catalog)
    return {"id": manifest.id, "version": manifest.version, **record}


def run(args):
    from ..p0.operator import authorize_project, local_context
    authorize_project(local_context(None), None)
    catalog = config.CONFIG_DIR / "workflow_packages.json"
    root = config.HARNESS_ROOT / "plugins" / "installed"
    failed = False
    if args.action == "digest":
        from .catalog import package_digest
        result = {"sha256": package_digest(args.source)}
    elif args.action == "install":
        result = _install(args.source, args.sha256, root, catalog)
    elif args.action == "list":
        result = {"plugins": json.loads(catalog.read_bytes()).get("plugins", [])}
    else:
        from .loader import bootstrap
        selected = []
        for ref in args.plugins:
            identity = ref.split("@")
            if len(identity) != 2 or not all(identity):
                raise ValueError(f"expected an exact plugin ID@VERSION, got {ref!r}")
            selected.append(tuple(identity))
        registry = bootstrap(root, catalog, selected)
        plugins = []
        for identity in registry.order:
            loaded = registry.get(*identity)
            pin = loaded.pin
            contributions = loaded.contributions
            plugins.append({
                "id": pin.id, "version": pin.version, "sha256": pin.digest,
                "namespace": loaded.namespace,
                "contributions": {
                    "workflows": sorted("@".join(ref) for ref in contributions.workflows),
                    "tools": sorted(contributions.tools),
                    "capabilities": sorted(contributions.capabilities),
                    **({"research_modules": sorted("@".join(ref) for ref in contributions.research_modules)}
                       if contributions.research_modules else {}),
                },
            })
        result = {
            "plugins": plugins,
            "failures": {"@".join(ref): reason for ref, reason in registry.failures.items()},
        }
        failed = bool(registry.failures)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(failed)
