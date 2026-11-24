"""Construct an isolated child environment for GX acceptance commands."""

import os
from pathlib import Path
import shutil
import sys


def test_environment(work: Path, *, profile: str = "deterministic", python: Path | None = None,
                     binary: Path | None = None) -> dict[str, str]:
    """Create private homes and keep only the tools and routes required by the tests."""
    if profile not in {"deterministic", "real"}:
        raise ValueError(f"unknown test resource profile: {profile}")
    work = Path(work).resolve()
    home, temporary = work / "home", work / "tmp"
    codex = home / ".codex"
    for directory in (codex, temporary):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep the selected interpreter's virtualenv even when bin/python is a symlink.
    path = [str(Path(python or sys.executable).absolute().parent)]
    if binary is not None:
        path.append(str(Path(binary).absolute().parent))
    for tool in ("node", "npm", "git", *(("nvidia-smi",) if profile == "real" else ())):
        if program := shutil.which(tool):
            path.append(str(Path(program).absolute().parent))
    path.extend(("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"))
    env = {"PATH": os.pathsep.join(dict.fromkeys(path)), "HOME": str(home), "CODEX_HOME": str(codex),
           "TMPDIR": str(temporary), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PIP_NO_INDEX": "1"}
    # The caller may restrict the three loopback exceptions further.
    loopback = ("localhost", "127.0.0.1", "::1")
    bypass = set(loopback)
    for key in ("NO_PROXY", "no_proxy"):
        if key in os.environ:
            bypass.intersection_update(item.strip() for item in os.environ[key].split(","))
    proxy = {"HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890",
             "ALL_PROXY": "socks5h://127.0.0.1:7891",
             "NO_PROXY": ",".join(host for host in loopback if host in bypass)}
    env.update(proxy)
    env.update({key.lower(): value for key, value in proxy.items()})
    if profile == "deterministic":
        env.update(GX_TEST_RESOURCE_PROFILE="deterministic", CUDA_VISIBLE_DEVICES="",
                   OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
    elif "CUDA_VISIBLE_DEVICES" in os.environ:
        env["CUDA_VISIBLE_DEVICES"] = os.environ["CUDA_VISIBLE_DEVICES"]
    if binary is not None:
        env["GX_P0_CODEX"] = str(Path(binary).absolute())
    return env
