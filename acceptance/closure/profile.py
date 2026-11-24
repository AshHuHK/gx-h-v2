"""Construct an isolated child environment for GX acceptance commands."""

import fcntl
from functools import lru_cache
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading


_scope_lock = threading.Lock()


@lru_cache(maxsize=1)
def _scope_for_process(pid: int):
    # The descriptor stays in the launching process. Even children that close all
    # inherited fds can check the kernel's owner/byte range in /proc/locks.
    handle = tempfile.TemporaryFile(dir="/tmp")
    fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0x47585032)
    stat = os.fstat(handle.fileno())
    locator = f"{pid}/{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
    return handle, locator


def controlled_resources() -> dict[str, str]:
    """Select fixture resources for this acceptance launcher and its descendants."""
    with _scope_lock:
        _, locator = _scope_for_process(os.getpid())
    return {"GX_TEST_RESOURCE_PROFILE": "deterministic", "GX_ACCEPTANCE_RESOURCE_SCOPE": locator}


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
    if profile == "real":
        # Prefer the system's GPU driver utility over any incidental tool directory.
        path.extend(os.defpath.split(os.pathsep))
    for tool in ("node", "npm", "git"):
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
        env.update(controlled_resources(), CUDA_VISIBLE_DEVICES="",
                   OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
    elif "CUDA_VISIBLE_DEVICES" in os.environ:
        env["CUDA_VISIBLE_DEVICES"] = os.environ["CUDA_VISIBLE_DEVICES"]
    if binary is not None:
        env["GX_P0_CODEX"] = str(Path(binary).absolute())
    return env
