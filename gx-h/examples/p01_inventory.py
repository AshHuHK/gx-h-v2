"""Print the actual source, catalog and compatibility boundary; opens no project."""
import hashlib
import json
import platform
import shutil
import subprocess

from gx_core import contracts, registry
from gx_core.hostobs import native_app_server, native_context
from gx_core.hostobs.runtime import runtime_adapter


def inventory():
    root = contracts.HARNESS_ROOT
    policy = json.loads((root / "config/role_policy.codex.json").read_text())
    installed = shutil.which("codex")
    version = (subprocess.run([installed, "--version"], capture_output=True, text=True).stdout.strip()
               if installed else None)
    catalogs = {}
    for name in native_app_server.TOOL_PROFILES:
        tools = native_app_server.dynamic_tools(profile=name)
        catalogs[name] = {"exposed": sorted(t["name"] for t in tools),
                          "digest_sha256": hashlib.sha256(json.dumps(tools, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest()}
    return {
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "contract": contracts.contract_digest(), "python_version": platform.python_version(),
        "installed_codex_version": version or None, "model_configuration": policy["pinned_model"]["id"],
        "compatibility": {"host_versions": sorted(native_context.SUPPORTED_HOST_VERSIONS),
                          "native_release": json.loads((root / "config/native_release.json").read_text()),
                          "session_schema": "gx.native_session/1", "host_source": native_context.SOURCE,
                          "saved_session": "native-session.json"},
        "capabilities": runtime_adapter().capabilities("p01-offline-baseline"),
        "catalogs": catalogs, "registration": registry.report(),
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), indent=2))
