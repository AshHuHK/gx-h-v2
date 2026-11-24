"""Validate the acceptance launch scope before GX uses fixture resources."""

from functools import lru_cache
import os
from pathlib import Path


PROFILE_VAR = "GX_TEST_RESOURCE_PROFILE"
SCOPE_VAR = "GX_ACCEPTANCE_RESOURCE_SCOPE"
# The acceptance launcher holds one POSIX byte-range lock on an anonymous file.
# A locator alone conveys no authority. The kernel supplies both the lock and its PID.
SCOPE_LOCK_BYTE = 0x47585032


class ResourceProfileError(ValueError):
    pass


@lru_cache(maxsize=4)
def _check_scope(pid: int, parent: int, locator: str) -> int:
    owner_text, device_inode = locator.split("/", 1)
    owner = int(owner_text)
    if owner <= 0:
        raise ValueError("invalid owner")
    ancestor = pid
    while ancestor != owner:
        if ancestor <= 1:
            raise ValueError("unrelated launcher")
        if ancestor == pid:
            ancestor = parent
        else:
            stat = Path(f"/proc/{ancestor}/stat").read_text()
            ancestor = int(stat.rsplit(")", 1)[1].split()[1])
    expected = ["POSIX", "ADVISORY", "WRITE", str(owner), device_inode,
                str(SCOPE_LOCK_BYTE), str(SCOPE_LOCK_BYTE)]
    with open("/proc/locks", encoding="ascii") as locks:
        if any(line.split()[1:] == expected for line in locks):
            return owner
    raise ValueError("launcher lock is absent")


def deterministic_test_resources() -> bool:
    profile = os.environ.get(PROFILE_VAR)
    if profile is None:
        return False
    if profile != "deterministic":
        raise ResourceProfileError(
            f"{PROFILE_VAR}={profile!r} is an acceptance-only override. "
            "Remove it for a normal GX invocation with real host resources."
        )
    try:
        owner = _check_scope(os.getpid(), os.getppid(), os.environ.get(SCOPE_VAR, ""))
        # Cached scopes cease to be usable when their acceptance launcher exits.
        os.kill(owner, 0)
    except (ValueError, OSError, IndexError):
        raise ResourceProfileError(
            f"{PROFILE_VAR}=deterministic requires a live controlled acceptance launcher. "
            "Run bin/gx-acceptance, or remove the test override for a normal GX invocation."
        ) from None
    return True


def child_resource_environment() -> dict[str, str]:
    """Propagate the verified acceptance scope only along controlled GX child paths."""
    if not deterministic_test_resources():
        return {}
    names = (PROFILE_VAR, SCOPE_VAR, "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS",
             "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
    return {name: os.environ[name] for name in names if name in os.environ}
