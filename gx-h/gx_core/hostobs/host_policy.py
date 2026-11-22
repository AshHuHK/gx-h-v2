"""Host-scoped model policy selected from backend-owned native evidence.

The default API remains the historical Claude policy. A declaration is correlation,
not authority to select the Codex policy. The scope marker is backend-pinned with
its policy snapshot; later callers cannot switch the run's policy family.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Mapping
from .. import config, contracts
from ..domain import ids
from ..errors import ContentHashMismatch, SchemaInvalid
from . import policy

CODEX_POLICY_PATH = contracts.CONFIG_DIR / "role_policy.codex.json"
DEFAULT_MODEL = "gpt-6.1-sol"
PROJECT_MODELS = (DEFAULT_MODEL, "gpt-6.2-astra")
PROJECT_WORKER_EFFORT_FLOORS = ("max",)
PROJECT_ROLE_PINS = contracts.CONFIG_DIR / "codex_project_role_pins.json"
SCOPE = "host-specific-v1"


def for_host(host: str | None = None) -> tuple[dict[str, Any], str]:
    """Internal or fixed-projection lookup; never authorize from a request host field."""
    if host is None or host == "claude_code" or host == "other_verified":
        return policy.agent_policy()
    if host != "codex":
        raise SchemaInvalid("no model policy is authorized for this Host")
    document, digest = policy.load_policy(CODEX_POLICY_PATH)
    if document.get("policy_host") != "codex" or document.get("policy_scope") != SCOPE:
        raise SchemaInvalid("the Codex policy has no valid Host scope")
    return document, digest


def _native_policy_for_model(model: str, worker_effort_floor: str | None = None) -> tuple[dict[str, Any], str]:
    if model not in PROJECT_MODELS:
        raise SchemaInvalid(f"unsupported native project model: {model!r}")
    document, digest = for_host("codex")
    if worker_effort_floor is not None and worker_effort_floor not in PROJECT_WORKER_EFFORT_FLOORS:
        raise SchemaInvalid(f"unsupported native worker effort floor: {worker_effort_floor!r}")
    if model == DEFAULT_MODEL and worker_effort_floor is None:
        return document, digest

    # Inherit the released role definitions, effort tiers and observation rules.
    # load_policy returns a fresh document; the default file and digest stay intact.
    basis = f"User selected {model} for this GX Harness project via gx-project --model."
    for key in ("updated_by", "updated_at", "authorization_source_question_item", "authorization_recorded_at"):
        document.pop(key, None)
    document["applies_to"] = "Official GX Harness Codex projects selecting this model via gx-project --model."
    document["base_policy_sha256"] = digest
    document["pinned_model"] = {"id": model, "aliases": [], "basis": [basis]}
    document["models"] = {model: {"aliases": []}}
    for task_class in document["task_classes"].values():
        task_class.update(model=model, model_basis=basis)
    if worker_effort_floor is not None:
        order = document["effort_order"]
        document["worker_effort_floor"] = worker_effort_floor
        for spec in document["task_classes"].values():
            for key in ("min_effort", "default_effort"):
                spec[key] = max(spec[key], worker_effort_floor, key=order.index)
            spec["basis"] = f"Project workers use at least {worker_effort_floor}; higher role requirements apply."
    problems = policy.problems_in(document)
    if problems:
        raise SchemaInvalid("invalid native project model policy", details={"problems": problems})
    return document, ids.sha256_of_obj(document)


def native_codex_policy(state_directory: Path | None = None) -> tuple[dict[str, Any], str]:
    """Load the official native project's selection from its owned state."""
    directory = state_directory if state_directory is not None else config.RUNTIME_VAR_DIR
    if directory is None:
        return for_host("codex")
    try:
        driver = json.loads((Path(directory) / "project-driver.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return for_host("codex")
    except (OSError, ValueError) as exc:
        raise SchemaInvalid("the native project model setting is unreadable") from exc
    if not isinstance(driver, dict):
        raise SchemaInvalid("the native project settings must be a JSON object")
    return _native_policy_for_model(driver.get("model", DEFAULT_MODEL), driver.get("worker_effort_floor"))


def native_role_pins(document: Mapping[str, Any]) -> dict[str, str]:
    """Return the installed role pins after checking the matching model's projections."""
    from . import codex_roles
    model = policy.pinned_model(document)
    floor = document.get("worker_effort_floor")
    if model == DEFAULT_MODEL and floor is None:
        admitted = codex_roles.pins()
    else:
        try:
            pins = json.loads(PROJECT_ROLE_PINS.read_text(encoding="utf-8"))
            admitted = codex_roles.pins() if model == DEFAULT_MODEL else pins[model]
            if floor is not None:
                admitted = {**admitted, **pins["worker_effort_floors"][floor][model]}
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ContentHashMismatch("no installed Codex role pins for this project model") from exc
    projected = {role: entry["sha256"] for role, entry in codex_roles.shipped(document).items()}
    if projected != admitted:
        raise ContentHashMismatch("the shipped Codex roles differ from the project's installed model policy")
    return admitted


def scope_of_run(run: Mapping[str, Any]) -> str | None:
    """Only a backend-pinned policy marker opts a run into the Codex policy family.

    Legacy runs remain legacy even when their caller declared host=codex. Never infer
    a new policy from host_json alone, the current caller, or the current process.
    """
    raw = run["agent_policy_json"]
    try:
        snapshot = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        raise ContentHashMismatch("the run's pinned agent policy snapshot is unreadable") from exc
    if not isinstance(snapshot, Mapping):
        if raw:
            raise ContentHashMismatch("the run's pinned agent policy snapshot is not an object")
        return None
    # Empty legacy snapshots retain no-policy semantics; a real invalid stored
    # document must never be masked by a valid current installation.
    if snapshot:
        problems = policy.problems_in(dict(snapshot))
        if problems:
            raise ContentHashMismatch("the run's pinned agent policy snapshot is invalid",
                                      details={"problems":problems})
    host, scope = snapshot.get("policy_host"), snapshot.get("policy_scope")
    if host is None and scope is None:
        return None
    if host != "codex" or scope != SCOPE:
        raise ContentHashMismatch("the run's pinned policy Host scope is unknown")
    try:
        host_record = json.loads(run["host_json"] or "{}")
    except (ValueError, TypeError) as exc:
        raise ContentHashMismatch("the run's pinned Host record is unreadable") from exc
    if not isinstance(host_record, Mapping):
        raise ContentHashMismatch("the run's pinned Host record is not an object")
    selected = host_record.get("policy_selection", {})
    from .native_context import SOURCE as NATIVE_SOURCE, MODEL_AUTHORITY
    if (host_record.get("kind") != "codex" or not isinstance(selected, Mapping)
            or selected.get("source") not in ("codex_native_invocation", NATIVE_SOURCE)
            or selected.get("session_ref") != host_record.get("session_ref")
            or not selected.get("identity_sha256")):
        raise ContentHashMismatch("the Codex policy has no backend-verified run Host binding")
    if selected.get("source") == NATIVE_SOURCE and (
            selected.get("model_authority") != MODEL_AUTHORITY
            or selected.get("upstream_tier_independently_verified") is not False
            or not selected.get("native_agent_ref") or not selected.get("host_version")):
        raise ContentHashMismatch("the official native policy binding has incomplete provenance")
    try:
        pinned = run["agent_policy_sha256"]
    except (KeyError,IndexError):
        pinned = None
    # Raw file bytes and stored JSON have different serializations. Both digests
    # originate from the same backend selection and are bound in its Host proof.
    if (not isinstance(pinned,str) or re.fullmatch(r"[0-9a-f]{64}",pinned) is None
            or selected.get("policy_sha256") != pinned
            or selected.get("policy_snapshot_sha256") != ids.sha256_of_obj(snapshot)):
        raise ContentHashMismatch("the scoped policy snapshot does not match its backend-bound pins")
    return "codex"


def current_for_run(run: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    host = scope_of_run(run)
    if host != "codex":
        return for_host(host)
    from .native_context import SOURCE
    selection = json.loads(run["host_json"])["policy_selection"]
    if selection["source"] == SOURCE:
        raw = run["agent_policy_json"]
        snapshot = json.loads(raw) if isinstance(raw, str) else raw
        current = _native_policy_for_model(policy.pinned_model(snapshot), snapshot.get("worker_effort_floor"))
    else:
        current = for_host(host)
    saved_pins = selection.get("role_intent_pins")
    installed_pins = native_role_pins(current[0])
    if saved_pins == installed_pins:
        return current
    # A guidance-only update preserves the saved claim's role hash and frozen brief.
    # Only the official native callbacks can continue on the exact released successor.
    if selection["source"] == SOURCE and snapshot == current[0] and selection["policy_sha256"] == current[1]:
        try:
            successors = json.loads(PROJECT_ROLE_PINS.read_text(encoding="utf-8"))["native_guidance_successors"]
            successor = successors[ids.sha256_of_obj(saved_pins)]
        except (OSError, ValueError, TypeError, KeyError):
            successor = None
        if successor == ids.sha256_of_obj(installed_pins):
            return current
    raise ContentHashMismatch("the Codex role intent pins changed after this run started")


def for_new_run(ctx: Any, record: Mapping[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Select the native policy only from the App Server's bound callback thread."""
    if getattr(ctx, "native_call", None) is not None:
        from .native_context import policy_for_new_run
        return policy_for_new_run(ctx, record)
    declared = record.get("declared") or {}
    for host in (declared.get("kind"), record.get("kind")):
        if host is not None and not contracts.is_label("host_kind", host):
            raise SchemaInvalid("unknown Host kind")
    document, digest = policy.agent_policy()
    return document, digest, dict(record)
