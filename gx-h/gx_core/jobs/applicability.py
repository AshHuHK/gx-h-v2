"""Would a job of this capability accept these inputs and parameters: asked without a job.

Selection must not choose a path whose first step the job service will refuse. This asks
the same questions ``JobService.submit`` asks before it starts anything, in the same order,
and starts nothing: the capability is ACTIVE; its environment is ready as observed on this
host; the input manifest's bytes are what it says (re-hashed); the roles are the ones the
manifest takes; the parameters are declared and of a type that can reach the command line;
the needs are inside this machine's limits (and, for a server of the earlier platform,
inside its switch: the predicate of :mod:`.limits`, which admission reads too); and the
adapter's own ``validate_inputs`` accepts the staged inputs, run in a subprocess under the
adapter rule, exactly as before a launch.

What is not asked: the run's budget, the run's and the path's state, the machine's load.
Those change while a plan waits and are the planner's and the scheduler's to read
(``gx_core.budgets``, ``capacity.fits``).

    check(conn, capability_version="id@ver", input_manifest_ref="gxa_…", parameters={},
          probe=True)
      -> {"result": "ACCEPTED" | "REFUSED" | "NOT_CHECKABLE", "problems": [str],
          "by_kind": {"input": [...], "parameters": [...], "limits": [...], "environment": [...],
                      "adapter": [...]},
          "not_checkable": {part: reason},   # the parts nobody could answer, and why
          "why": str | None,                 # those reasons in one sentence
          "environment": {...} | None,       # with probe=False: environments.readiness_view
          "capability_sha256", "manifest_sha256" (the same: the capability's content sha256),
          "input_manifest_sha256", "cached": bool}

``probe``. With True (the default) the environment is checked as the job service checks it,
and a record made on another host is probed again (a write, and processes started). With
False nothing is probed: the stored record is read through ``readiness_view``; a record
that is not healthy on this host is a refusal, and a record that is not fresh here (never
probed, or probed on another host) makes the environment part NOT_CHECKABLE. A selection
wave asks with False (SCHEDULING_DESIGN r2 §5.1, hard filter 8).

NOT_CHECKABLE: nothing was refused, and some part could not be answered: the capability
binds no adapter, or its adapter defines no ``validate_inputs``, or (probe=False) the
environment's record is not fresh on this host. ``not_checkable`` and ``why`` say which.
Raises only for what cannot be asked at all: an unknown or not-ACTIVE capability version
(CAPABILITY_NOT_APPROVED) and an input manifest that does not exist (ARTIFACT_NOT_FOUND).

What rests on the content alone — the input and parameter checks and the adapter's answer —
is kept for the life of the process, keyed by (capability content sha256, input manifest
sha256, parameters sha256, the environment's paths): the same facts asked again launch
nothing. The environment and the limits are read again on every call, because they change
while the content does not.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..capabilities import environments, registry, storage
from ..capabilities import manifest as manifest_mod
from ..domain import ids
from ..errors import ArtifactNotFound, EnvNotReady, GxError
from . import adapter as adapter_mod
from . import limits as limits_mod

ACCEPTED, REFUSED, NOT_CHECKABLE = "ACCEPTED", "REFUSED", "NOT_CHECKABLE"
KINDS = ("input", "parameters", "limits", "environment", "adapter")
CONTENT_KINDS = ("input", "parameters", "adapter")

_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}
_CACHE_MAX = 4096


def _env_view(env: Mapping[str, Any]) -> manifest_mod.EnvView:
    return manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                binaries=dict(env["binary_paths"]), prefix=env["prefix"])


def _content(capability: Mapping[str, Any], artifacts: Any, input_manifest_ref: str,
             parameters: Mapping[str, Any], env: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """The checks that rest on the content: the input manifest, the parameters (through the
    launch recipe, with the environment's stored paths), the adapter's ``validate_inputs``.
    Returns the findings and whether they may be kept."""
    manifest = capability["manifest"]
    found: dict[str, Any] = {kind: [] for kind in CONTENT_KINDS}
    found["adapter_unknown"] = None
    keep = True
    entries: dict[str, Any] = {}
    try:
        artifacts.verify_input_manifest(input_manifest_ref)          # re-hashes every entry
        document = artifacts.read_input_manifest(input_manifest_ref)
        listed = list(document.get("entries", []))
        entries = {entry["role"]: entry for entry in listed}
        needed = manifest_mod.input_roles(manifest)
        if sorted(entries) != needed or len(entries) != len(listed):
            found["input"].append(f"the capability takes input role(s) {needed}; the manifest supplies "
                                  f"{sorted(entry['role'] for entry in listed)}")
    except GxError as exc:
        found["input"].append(f"{exc.code}: {exc.message}")
        keep = False                     # bytes that did not re-hash may be restored

    staging = Path(tempfile.mkdtemp(prefix="gx-applicability-"))
    try:
        staged: dict[str, str] = {}
        if not found["input"]:
            for role, entry in entries.items():
                suffix = Path(entry.get("rel_path") or "").suffix[:16]
                target = staging / f"{role}{suffix}"
                target.write_bytes(artifacts.read_bytes(entry["artifact_ref"]))      # re-hashed read
                staged[role] = str(target)
        if env is not None and not found["input"]:
            try:
                manifest_mod.launch_plan(capability["bundle"], _env_view(env), inputs=staged,
                                         out_dir=str(staging / "out"), parameters=dict(parameters),
                                         home_dir=str(staging / "home"), tmp_dir=str(staging / "tmp"),
                                         cpu_threads=1)
            except GxError as exc:
                found["parameters"].append(f"{exc.code}: {exc.message}")
        parser = capability["bundle"].get("parser")
        if parser is None:
            found["adapter_unknown"] = ("the capability binds no adapter module, so nothing can check its "
                                        "inputs before a launch")
        elif not found["input"]:
            work = staging / "work"
            work.mkdir()
            reply = adapter_mod.call(parser, "validate_inputs", [staged, dict(parameters)], work_dir=work)
            if reply.missing_function:
                found["adapter_unknown"] = f"the adapter {parser['path']} defines no validate_inputs"
            elif not reply.ok:
                found["adapter"].append(f"the adapter could not check the inputs: {reply.error_type}: {reply.error}")
                keep = False             # a crash or a timeout under load is not a property of the bytes
            else:
                found["adapter"].extend(str(problem)[:300] for problem in (reply.result or [])[:20])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return found, keep


def check(conn: sqlite3.Connection, *, capability_version: str, input_manifest_ref: str,
          parameters: Mapping[str, Any] | None = None, probe: bool = True) -> dict[str, Any]:
    parameters = dict(parameters or {})
    capability = registry.resolve(conn, capability_version)          # raises unless ACTIVE
    manifest = capability["manifest"]
    artifacts = storage.artifact_service(conn)
    try:
        described = artifacts.describe(input_manifest_ref)
    except GxError as exc:
        raise ArtifactNotFound(f"no input manifest {input_manifest_ref!r}: {exc.message}",
                               details={"input_manifest_ref": input_manifest_ref}) from exc
    input_sha = str(described.get("sha256") or "")

    found: dict[str, list[str]] = {kind: [] for kind in KINDS}
    unknown: dict[str, str] = {}
    answer: dict[str, Any] = {"capability_version": capability["capability_version"],
                              "capability_sha256": capability["manifest_sha256"],
                              "manifest_sha256": capability["manifest_sha256"],
                              "input_manifest_sha256": input_sha, "probe": bool(probe),
                              "environment": None, "cached": False}

    # The environment: as the job service checks it, or (probe=False) as its record stands.
    env_id = capability["env_id"]
    if probe:
        try:
            environments.require_ready(conn, env_id, actor="applicability")
        except GxError as exc:
            found["environment"].append(f"{exc.code}: {exc.message}")
    else:
        view = environments.readiness_view(conn, env_id)
        answer["environment"] = view
        if not view["ready"]:
            if view["registered"] and view["fresh"]:      # observed here, and not healthy
                found["environment"].append(f"ENV_NOT_READY: {view['why']}")
            else:
                unknown["environment"] = view["why"]
    try:
        env: dict[str, Any] | None = environments.get(conn, env_id)    # its paths, for the recipe
    except EnvNotReady:
        env = None

    # The limits: the predicate admission raises from.
    resources = manifest_mod.effective_resources(manifest, None)
    found["limits"].extend(f"{finding.code}: {finding.message}" for finding in
                           limits_mod.limit_findings(resources, kind=manifest["kind"]))

    # What rests on the content.
    paths = ids.sha256_of_obj({"python": env["python_executable"], "binaries": env["binary_paths"],
                               "prefix": env["prefix"]}) if env is not None else ""
    key = (capability["manifest_sha256"], input_sha, ids.sha256_of_obj(parameters), paths)
    content = _CACHE.get(key)
    if content is not None:
        answer["cached"] = True
    else:
        content, keep = _content(capability, artifacts, input_manifest_ref, parameters, env)
        if keep:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[key] = content
    for kind in CONTENT_KINDS:
        found[kind] = list(content[kind])
    if content["adapter_unknown"]:
        unknown["adapter"] = content["adapter_unknown"]

    problems = [problem for kind in KINDS for problem in found[kind]]
    answer.update(problems=problems, by_kind=found, not_checkable=dict(unknown),
                  why="; ".join(unknown.values()) or None)
    answer["result"] = REFUSED if problems else NOT_CHECKABLE if unknown else ACCEPTED
    return answer


__all__ = ["ACCEPTED", "KINDS", "NOT_CHECKABLE", "REFUSED", "check"]
