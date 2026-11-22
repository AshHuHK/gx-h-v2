"""Loader for the frozen contracts in ``contracts/``.

Every module and every test reads its vocabulary from here. Nothing in this package
may hard-code a state name, an error code, a label or a tool name: a gate that checks
against its own private copy of a table will eventually disagree with the pipeline it
is supposed to gate, and the disagreement is invisible until it matters.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from typing import Any

HARNESS_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = HARNESS_ROOT / "contracts"
CONFIG_DIR = HARNESS_ROOT / "config"

#: v2 adds the gate table and the bundle schemas; both are part of the frozen contract.
_CONTRACT_FILES = (
    "states.json", "errors.json", "labels.json", "mcp_tools.json", "gates.json", "bundles.json",
)


@functools.lru_cache(maxsize=None)
def _load(name: str) -> dict[str, Any]:
    path = CONTRACTS_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def states() -> dict[str, Any]:
    return _load("states.json")


def errors() -> dict[str, Any]:
    return _load("errors.json")


def labels() -> dict[str, Any]:
    return _load("labels.json")


def mcp_tools() -> dict[str, Any]:
    return _load("mcp_tools.json")


def gates() -> dict[str, Any]:
    return _load("gates.json")


def bundles() -> dict[str, Any]:
    return _load("bundles.json")


def db_schema_sql() -> str:
    return (CONTRACTS_DIR / "db_schema.sql").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- freeze

def contract_digest() -> dict[str, str]:
    """Per-file and combined sha256 over the frozen contract files plus the DDL.

    A long-running job records this at start; a milestone compares it. If the digest
    moved underneath a run, the run's results describe a contract that no longer exists.
    """
    per_file: dict[str, str] = {}
    combined = hashlib.sha256()
    for name in sorted(_CONTRACT_FILES + ("db_schema.sql",)):
        raw = (CONTRACTS_DIR / name).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        per_file[name] = digest
        combined.update(name.encode("utf-8"))
        combined.update(b"\0")
        combined.update(raw)
    per_file["__combined__"] = combined.hexdigest()
    return per_file


def assert_contract_pinned(expected_combined: str) -> None:
    """Raise if the contract moved since ``expected_combined`` was recorded."""
    actual = contract_digest()["__combined__"]
    if actual != expected_combined:
        raise RuntimeError(
            "contract digest moved: expected "
            f"{expected_combined[:16]}... observed {actual[:16]}...; "
            "results produced after this point describe a different contract"
        )


# --------------------------------------------------------------------- vocabularies

def label_values(axis: str) -> list[Any]:
    data = labels()
    if axis not in data:
        raise KeyError(f"unknown label axis {axis!r}; known: {sorted(k for k in data if k != 'schema_version')}")
    entry = data[axis]
    return list(entry["values"])


def is_label(axis: str, value: Any) -> bool:
    return value in label_values(axis)


def require_label(axis: str, value: Any) -> Any:
    if not is_label(axis, value):
        raise ValueError(f"{value!r} is not a valid {axis}; allowed: {label_values(axis)}")
    return value


def error_spec(code: str) -> dict[str, Any]:
    table = errors()["errors"]
    if code not in table:
        raise KeyError(f"unknown error code {code!r}")
    return table[code]


def error_codes() -> list[str]:
    return sorted(errors()["errors"])


def failure_classes() -> dict[str, Any]:
    return errors()["failure_taxonomy"]["classes"]


def tool_spec(name: str) -> dict[str, Any]:
    table = mcp_tools()["tools"]
    if name not in table:
        raise KeyError(f"unknown tool {name!r}")
    return table[name]


def tool_names() -> list[str]:
    return sorted(mcp_tools()["tools"])


def privileged_tool_names() -> list[str]:
    return sorted(mcp_tools()["privileged_tools"]["names"])


def response_limits() -> dict[str, Any]:
    return mcp_tools()["envelope"]["limits"]


def contract_version() -> int:
    """The contract generation every file must agree on."""
    return int(states()["schema_version"])


def privileged_tool_spec(name: str) -> dict[str, Any]:
    table = mcp_tools()["privileged_tools"]["tools"]
    if name not in table:
        raise KeyError(f"unknown privileged tool {name!r}")
    return table[name]


def any_tool_spec(name: str) -> dict[str, Any]:
    """Worker or operator tool; the caller decides separately who may call it."""
    worker = mcp_tools()["tools"]
    if name in worker:
        return worker[name]
    return privileged_tool_spec(name)


# ---------------------------------------------------------------------------- gates

def check_ids() -> list[str]:
    return sorted(gates()["checks"])


def check_spec(check_id: str) -> dict[str, Any]:
    table = gates()["checks"]
    if check_id not in table:
        raise KeyError(f"unknown check {check_id!r}")
    return table[check_id]


def gate_requirements(kind: str, to_state: str) -> dict[str, Any]:
    """What must be on record, for one content sha256, before ``kind`` may enter ``to_state``.

    Returns ``{"checks": [...], "review": {...} | None, "approval": {...} | None,
    "effects": [...]}``. A transition this table does not list has no requirement beyond
    being legal in ``contracts/states.json``.
    """
    data = gates()
    if kind == "EVALUATOR":
        entry = data["eval_spec_requirements"].get(to_state, {})
    else:
        if kind not in data["requirements"]:
            raise KeyError(f"no gate table for asset kind {kind!r}")
        entry = data["requirements"][kind].get(to_state, {})
    return {
        "checks": list(entry.get("checks", [])),
        "review": entry.get("review"),
        "approval": entry.get("approval"),
        "effects": list(entry.get("effects", [])),
    }


def sufficient_independence() -> list[str]:
    return list(labels()["independence_level"]["sufficient"])


# -------------------------------------------------------------------------- bundles

def bundle_kinds() -> list[str]:
    return sorted(bundles()["bundles"])


def bundle_spec(kind: str) -> dict[str, Any]:
    table = bundles()["bundles"]
    if kind not in table:
        raise KeyError(f"unknown bundle kind {kind!r}; known: {sorted(table)}")
    return table[kind]


def bundle_types() -> dict[str, Any]:
    return bundles()["types"]


def bundle_limits() -> dict[str, Any]:
    return bundles()["limits"]
