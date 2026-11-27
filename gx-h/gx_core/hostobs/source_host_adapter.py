"""Launch the GX embedding built in the pinned Codex Rust workspace."""
import os
from pathlib import Path
from shutil import which

from ..errors import ScopeDenied
from . import app_server_adapter
from .app_server_adapter import cancel_activity


NAME = "codex_source_host"
DEFAULT = "codex_app_server"


def select_backend(previous, requested=None):
    requested = requested or os.environ.get("GX_RUNTIME_BACKEND")
    pinned = previous.get("runtime_backend", DEFAULT) if previous else None
    if pinned and requested and pinned != requested:
        raise ScopeDenied(f"saved thread belongs to {pinned}; resume with its original backend")
    backend = requested or pinned or DEFAULT
    if backend not in (DEFAULT, NAME):
        raise ScopeDenied(f"unknown native execution backend {backend!r}")
    return backend


def launch_argv(binary, cli_argv, outcomes_directory):
    """Translate the shared launcher options into the source host's config overrides."""
    feature_keys = {"--enable": "true", "--disable": "false"}
    if cli_argv[-3:] != ["app-server", "--listen", "stdio://"]:
        raise ValueError("the GX Codex source host requires the stdio transport")
    args = iter(cli_argv[1:-3])
    translated = [binary, "--outcome-directory", str(outcomes_directory)]
    for flag in args:
        value = next(args, None)
        if value is None:
            raise ValueError(f"missing value for {flag}")
        if flag == "-c":
            translated.extend(("-c", value))
        elif flag in feature_keys:
            translated.extend(("-c", f"features.{value}={feature_keys[flag]}"))
        else:
            raise ValueError(f"unsupported GX source host argument {flag!r}")
    return translated


def binary_path():
    target = os.environ.get("GX_CODEX_HOST")
    sibling = Path(__file__).resolve().parents[3] / "bin" / "gx-codex-host"
    path = which(target) if target else which(str(sibling)) or which("gx-codex-host")
    if path is None:
        raise ScopeDenied(f"GX source host executable unavailable at {target or sibling!s}; build gx-codex-host first")
    return str(Path(path).resolve())


def run_session(prompt, **options):
    return app_server_adapter.run_session(prompt, runtime_backend=NAME, **options)


def capabilities(runtime_namespace):
    result = app_server_adapter.capabilities(runtime_namespace)
    result["runtime"]["runtime_kind"] = NAME
    result["usage_reporting"] = "delta_and_snapshot"
    result.update(app_server_adapter.integration_evidence(NAME))
    return result
