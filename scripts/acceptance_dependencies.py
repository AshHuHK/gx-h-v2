"""Verify the deterministic test toolchain and build private dependencies offline."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_assets import checked, digest, install_cordis, integrity_digest


def versions(command, env):
    return subprocess.check_output(command, env=env, text=True, stderr=subprocess.STDOUT).strip()


def verify_assets(root, *, historical_pins=None):
    assets = root / "release-assets"
    lock = json.loads((assets / "lock.json").read_text())
    pins = lock.get("deterministic_test_runtime") or historical_pins
    if pins is None:
        raise ValueError("release lock lacks deterministic test runtime pins")
    requirements = checked(assets / "python/requirements.lock", pins["requirements_sha256"])
    cordis = checked(root / lock["cordis_lock"], pins["cordis_lock_sha256"])
    wheels = lock["scientific_review"]["wheels"]
    expected = {f"{name}=={pin['version']} --hash=sha256:{pin['sha256']}" for name, pin in wheels.items()}
    if set(requirements.read_text().splitlines()) != expected:
        raise ValueError("wheel requirements differ from the release lock")
    archives = {}
    for pin in wheels.values():
        path = checked(assets / "python" / pin["file"], pin["sha256"])
        archives[path.name] = pin["sha256"]
    for name, pin in json.loads(cordis.read_text())["packages"].items():
        if name:
            algorithm, hash_value = integrity_digest(pin["integrity"])
            path = checked(assets / "npm" / pin["resolved"].rsplit("/", 1)[1], hash_value, algorithm)
            archives[path.name] = digest(path)
    return lock, requirements, cordis, archives


def select_node(root, version, node, env):
    if node is None:
        # Both the installed release and the source workspace have offline layouts.
        choices = [root / "vendor/node/bin/node", root.parent.parent / f"node-v{version}-linux-x64/bin/node"]
        on_path = shutil.which("node", path=env["PATH"])
        if on_path:
            choices.append(Path(on_path))
        node = next((path for path in choices if path.is_file()), None)
    if node is None:
        raise FileNotFoundError(f"supply --node pointing to the offline Node {version} installation")
    return Path(node).resolve(strict=True)


def check_version(name, actual, expected):
    if actual != expected:
        raise ValueError(f"{name} version mismatch: found {actual}, release lock requires {expected}")


def runtime(root, work, *, python, node, env, pins):
    python = Path(python).absolute()
    node = select_node(root, pins["node"], node, env)
    npm = node.with_name("npm")
    if not npm.is_file():
        raise FileNotFoundError(f"Node installation lacks its bundled npm: {npm}")
    # Start with the caller's isolated profile and explicitly selected binaries. These
    # language-specific search/config paths must never inject user or global modules.
    env = {k: v for k, v in env.items() if not k.upper().startswith(("PYTHON", "PIP_", "NPM_", "NODE_"))}
    env.update(PATH=os.pathsep.join([str(node.parent), str(python.parent), env["PATH"]]),
               PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1",
               npm_config_offline="true", npm_config_globalconfig=os.devnull,
               npm_config_userconfig=str(work / "empty-npmrc"))
    implementation, version = versions([python, "-I", "-S", "-c",
        "import platform; print(platform.python_implementation(), platform.python_version())"], env).split()
    check_version("Python implementation", implementation, pins["implementation"])
    check_version("Python", version, pins["python"])
    node_version = versions([node, "--version"], env).removeprefix("v")
    check_version("Node", node_version, pins["node"])
    npm_version = versions([npm, "--version"], env)
    check_version("npm", npm_version, pins["npm"])

    return python, node, npm, version, node_version, npm_version, env


def inspect_python(venv, env, version, wheels):
    child = venv / "bin/python"
    measurement = json.loads(versions([child, "-I", "-c", """
import Bio, json, numpy, platform, site, sqlite3, sys
from pathlib import Path
modules = {name: {"version": module.__version__, "file": str(Path(module.__file__).resolve())}
           for name, module in (("biopython", Bio), ("numpy", numpy))}
print(json.dumps({"version": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                  "base_prefix": sys.base_prefix, "prefix": sys.prefix, "user_site": site.ENABLE_USER_SITE,
                  "modules": modules, "sys_path": sys.path}))
"""], env))
    check_version("venv Python", measurement["version"], version)
    if Path(measurement["prefix"]) != venv or measurement["base_prefix"] == measurement["prefix"] or measurement["user_site"]:
        raise ValueError("test Python must be a private isolated virtualenv")
    for name, module in measurement["modules"].items():
        check_version(name, module["version"], wheels[name]["version"])
        if not Path(module["file"]).is_relative_to(venv):
            raise ValueError(f"{name} was imported outside the test venv")
    return measurement


def prepare(root, work, *, python, env, node=None):
    """Provision a fresh, private venv and Cordis host. Paths in the receipt live under work."""
    root, work = Path(root).resolve(), Path(work).resolve()
    lock, requirements, cordis, archives = verify_assets(root)
    python, node, npm, version, node_version, npm_version, env = runtime(
        root, work, python=python, node=node, env=env, pins=lock["deterministic_test_runtime"])

    work.mkdir(parents=True, exist_ok=False)
    venv = work / "venv"
    venv_command = [python, "-I", "-S", "-m", "venv", venv]
    subprocess.run(venv_command, check=True, env=env)
    child = venv / "bin/python"
    pip = [child, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
           "--no-index", "--no-cache-dir", "--require-hashes", "--find-links", requirements.parent, "-r", requirements]
    subprocess.run(pip, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    measurement = inspect_python(venv, env, version, lock["scientific_review"]["wheels"])
    host = work / "plugin-host"
    shutil.copytree(cordis.parent, host, ignore=shutil.ignore_patterns("node_modules"))
    packages = install_cordis(manifest=host / cordis.name, archives=root / "release-assets/npm", env=env, npm=str(npm))
    node_check = subprocess.run([node, "--experimental-strip-types", "--test", "host.test.ts"],
                                cwd=host, env=env, check=True, text=True, stdout=subprocess.PIPE)
    lock_hashes = {str(p.relative_to(root)): digest(p) for p in (root / "release-assets/lock.json", requirements, cordis)}
    return {"passed": True, "python": {**measurement, "path": str(child), "seed": str(python),
            "executable_sha256": digest(child)}, "node": {"path": str(node), "version": node_version,
            "executable_sha256": digest(node), "npm": str(npm), "npm_version": npm_version, "host": str(host),
            "packages": packages, "host_tests": node_check.stdout},
            "lock_sha256": lock_hashes, "offline_archive_sha256": archives,
            "venv_argv": [str(arg) for arg in venv_command], "install_argv": [str(arg) for arg in pip]}


def dependency_controls(root, work, *, python, node, env):
    """Exercise rejected runtime/asset inputs without touching the original lock or wheels."""
    work.mkdir(parents=True, exist_ok=False)
    poison = work / "ambient"
    poison.mkdir()
    (poison / "sitecustomize.py").write_text("raise SystemExit('ambient Python module loaded')\n")
    (poison / "node.cjs").write_text("throw Error('ambient Node module loaded')\n")
    hostile = {**env, "PYTHONPATH": str(poison), "NODE_PATH": str(poison),
               "NODE_OPTIONS": f"--require={poison / 'node.cjs'}", "PIP_INDEX_URL": "http://127.0.0.1:1"}
    result = prepare(root, work / "positive", python=python, node=node, env=hostile)
    errors = {}
    def rejected(label, invoke, error_type, text):
        try:
            invoke()
        except error_type as exc:
            if text not in str(exc):
                raise AssertionError(f"{label} failed for an unexpected reason") from exc
            errors[label] = str(exc)
        else:
            raise AssertionError(f"{label} unexpectedly passed")

    lock = json.loads((root / "release-assets/lock.json").read_text())
    # Use the actual executables with an intentionally incompatible fixture pin.
    for name in ("python", "node"):
        pins = {**lock["deterministic_test_runtime"], name: "0.0.0"}
        rejected("wrong_" + name, lambda: runtime(root, work, python=python, node=Path(result["node"]["path"]),
                 env=env, pins=pins), ValueError, "version mismatch")
    fixture = work / "damaged-release"
    wheelhouse = fixture / "release-assets/python"
    wheelhouse.mkdir(parents=True)
    (fixture / "release-assets/lock.json").symlink_to(root / "release-assets/lock.json")
    manifest = fixture / lock["cordis_lock"]
    manifest.parent.mkdir(parents=True)
    manifest.symlink_to(root / lock["cordis_lock"])
    (fixture / "release-assets/npm").symlink_to(root / "release-assets/npm", target_is_directory=True)
    for path in (root / "release-assets/python").iterdir():
        (wheelhouse / path.name).symlink_to(path)
    wheel = wheelhouse / lock["scientific_review"]["wheels"]["biopython"]["file"]
    wheel.unlink()
    rejected("missing_wheel", lambda: verify_assets(fixture), FileNotFoundError, wheel.name)
    wheel.write_bytes(b"changed fixture wheel")
    rejected("wrong_wheel_digest", lambda: verify_assets(fixture), ValueError, "sha256 mismatch")
    return {**result, "checks": {"clean_install": True, "ambient_injection_excluded": True,
                                 "missing_or_mismatched_dependencies_refused": True}, "expected_errors": errors}


if __name__ == "__main__":
    from acceptance.closure.profile import test_environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="new directory for this environment")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--node", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-controls", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="gx-dependencies-profile-") as scratch:
        env = test_environment(Path(scratch), python=args.python)
        provision = dependency_controls if args.check_controls else prepare
        try:
            receipt = provision(ROOT, args.work, python=args.python, node=args.node, env=env)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            receipt = {"passed": False, "error": str(error),
                       "output": getattr(error, "stdout", None)}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        if not receipt["passed"]:
            raise SystemExit(receipt["error"])
