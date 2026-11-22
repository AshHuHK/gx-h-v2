"""Access to ``config/limits.json`` and ``config/paths.json``.

``limits.json`` is authoritative; ``LIMITS.md`` is prose and is never parsed. Switches
are real booleans with a separate ``scope`` field, because a scope written inside the
value — ``"yes (smoke only)"`` — gets read as a bare yes by the first naive parser and
the restriction silently disappears.

Programs re-read these files on every call: a limit read once at start-up is a limit
that cannot be tightened while work is running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ApprovalRequired, HoldActive, ResourceDenied

HARNESS_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = HARNESS_ROOT / "config"
# Set by the guarded native entry for its lifetime, never from model arguments.
RUNTIME_VAR_DIR: Path | None = None


def _read(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def limits() -> dict[str, Any]:
    """Always a fresh read. Do not cache."""
    return _read("limits.json")


def paths() -> dict[str, Any]:
    return _read("paths.json")


def ownership() -> dict[str, Any]:
    return _read("ownership.json")


# ------------------------------------------------------------------------- switches

def switch(name: str) -> dict[str, Any]:
    table = limits()["switches"]
    if name not in table:
        raise KeyError(f"unknown switch {name!r}; known: {sorted(table)}")
    return table[name]


def _switch_value(name: str, spec: dict[str, Any]) -> bool:
    value = spec["value"]
    if not isinstance(value, bool):
        raise TypeError(
            f"switch {name!r} must be a real boolean, got {value!r}; "
            "a scope belongs in the separate 'scope' field"
        )
    return value


def switch_enabled(name: str) -> bool:
    return _switch_value(name, switch(name))


def switch_scope(name: str) -> str | None:
    return switch(name).get("scope")


def require_switch(name: str, *, scope: str | None = None) -> None:
    """Raise unless the current switch permits this scope."""
    checked_switch(name, scope=scope)


def checked_switch(name: str, *, scope: str | None = None) -> dict[str, Any]:
    """Return the fresh switch specification whose value and scope permit this call."""
    spec = switch(name)
    if not _switch_value(name, spec):
        raise ApprovalRequired(
            f"switch {name!r} is off: {spec.get('reason', 'no reason recorded')}",
            details={"switch": name},
        )
    declared = spec.get("scope")
    if declared is not None and scope is not None and scope != declared:
        raise ApprovalRequired(
            f"switch {name!r} is limited to scope {declared!r}, requested {scope!r}",
            details={"switch": name, "declared_scope": declared, "requested_scope": scope},
        )
    return spec


# ---------------------------------------------------------------------------- holds

def active_holds(scope: str | None = None) -> list[dict[str, Any]]:
    holds = limits().get("holds", [])
    if scope is None:
        return list(holds)
    return [h for h in holds if h.get("scope") in (None, "*", scope)]


def check_holds(scope: str | None = None) -> None:
    found = active_holds(scope)
    if found:
        first = found[0]
        raise HoldActive(
            f"hold {first.get('id', '?')} is active: {first.get('reason', 'no reason recorded')}",
            details={"holds": found, "scope": scope},
        )


# --------------------------------------------------------------------------- budgets

def engineering_budget() -> dict[str, Any]:
    return limits()["engineering_budget"]


def research_budget() -> dict[str, Any]:
    return limits()["research_budget"]


def reference_budget() -> dict[str, Any]:
    return limits()["reference_budget"]


def gpu_limits() -> dict[str, Any]:
    return limits()["gpu"]


def cpu_limits() -> dict[str, Any]:
    return limits()["cpu"]


def network_policy() -> dict[str, Any]:
    return limits()["network"]


def approval_required_actions() -> list[str]:
    return list(limits()["approval_required_actions"])


def require_budget(kind: str, key: str, requested: float) -> None:
    """Raise ``RESOURCE_DENIED`` when a request exceeds a declared ceiling."""
    table = {"engineering": engineering_budget, "research": research_budget,
             "reference": reference_budget, "gpu": gpu_limits, "cpu": cpu_limits}[kind]()
    if key not in table:
        raise KeyError(f"unknown budget key {kind}.{key}")
    ceiling = table[key]
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)):
        raise TypeError(f"budget {kind}.{key} is not numeric: {ceiling!r}")
    if requested > ceiling:
        raise ResourceDenied(
            f"requested {requested} exceeds {kind}.{key} ceiling {ceiling}",
            details={"budget": f"{kind}.{key}", "ceiling": ceiling, "requested": requested},
        )


# ----------------------------------------------------------------------------- paths

def var_dir() -> Path:
    path = RUNTIME_VAR_DIR if RUNTIME_VAR_DIR is not None else HARNESS_ROOT / "var"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return var_dir() / "gx.db"


def artifact_root() -> Path:
    path = var_dir() / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prefixes() -> list[str]:
    return list(paths()["prefixes"].values())


def path_equals(left: str | Path, right: str | Path) -> bool:
    """Compare two paths across the project-symlink and tenant-realpath prefixes."""
    return Path(left).resolve() == Path(right).resolve()
