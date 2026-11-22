"""The capability manifest: what a version *is*, and the launch it resolves to.

A capability version is one immutable document — the ``capability_manifest_bundle`` of
``contracts/bundles.json``: a manifest, and the two scripts bound to it by sha256 (an
entry script that runs inside the tool's own interpreter, and an adapter module that
runs in the control plane and knows the tool's dialect). Everything the backend later
needs to know about the version is read from that document, and from nothing a caller
sends with a request:

* **how it launches** — ``entry``: an argv template whose first element comes from the
  environment registry, a working directory, a module search path and fixed environment
  values;
* **what it is** — ``method_card``: family, mechanism, objective, estimator and
  training-data lineage. A planner's own description of its method decides nothing;
* **what it needs** — ``resources``: threads, wall time, and a GPU need with its scope.
  A request can tighten these and can neither widen them nor leave them out;
* **what it loads** — ``weights``, each with a digest and a load mode.

Placeholders
------------
One template token is exactly one argv element; nothing is split, joined or handed to a
shell. A token may contain placeholders from these namespaces:

==================  ==========================================================
``{env.python}``    the environment's interpreter
``{env.bin.NAME}``  a binary the environment registry holds
``{env.prefix}``    the environment's prefix directory
``{workspace}``     this checkout's root
``{harness}``       this tree
``{entry_script}``  the bundle's entry script, after its sha256 was checked
``{weights.N}``     the Nth weight file    ``{data.N}``  the Nth required data path
``{in.ROLE}``       an input artifact of the job, by the role its manifest names
``{out.dir}``       the job's output directory
``{param.NAME}``    a job parameter; undeclared parameters are refused
==================  ==========================================================

``argv[0]`` is ``{env.python}`` or ``{env.bin.NAME}`` and nothing else: a program name
that could arrive from a request is a capability that runs whatever the request names.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import config, contracts
from ..domain import ids
from ..errors import ContentHashMismatch, EnvNotReady, SchemaInvalid
from . import schema

BUNDLE_KIND = "capability_manifest_bundle"
ASSET_KIND = "TOOL_ADAPTER"

HARNESS_ROOT = Path(__file__).resolve().parents[2]

CAPABILITY_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
BINARY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}")
OUTPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ARTIFACT_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

#: Repository-local code accepted by the local Job runner.
SCRIPT_ROOTS = ("adapters/local/",)
SCRIPT_ROOT = SCRIPT_ROOTS[0]

#: LOCAL vocabulary, not from contracts/: the two controls a manifest may declare.
KNOWN_ANSWER_KINDS = ("IDENTITY", "CROSS_IMPLEMENTATION")

#: LOCAL vocabulary: output kinds whose bytes are a file the tool wrote into ``{out.dir}``.
#: Every other kind is a document the bound adapter module produces from the tool's output.
RAW_OUTPUT_PREFIX = "file"
#: LOCAL vocabulary: an output that is a directory under ``{out.dir}``; each regular file in
#: it is published as its own artifact, named ``<output name>/<path inside it>``.
DIR_OUTPUT_PREFIX = "dir"

#: Reserved contract vocabulary for an optional external runtime.
STDIO_KIND = "MCP_STDIO_TOOL"
STDIO_SWITCH = "launch_gx_stdio_backends"
STDIO_SWITCH_SCOPE = "per_call_subprocess_only"
NO_PROXY_HOSTS = "localhost,127.0.0.1,::1"

#: Fixed environment values a manifest may set. Data and runtime switches only.
LAUNCH_ENV_ALLOWED = frozenset({
    "LD_LIBRARY_PATH", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
    "HF_HOME", "TORCH_HOME", "WANDB_MODE", "PYTORCH_CUDA_ALLOC_CONF",
    "XLA_PYTHON_CLIENT_PREALLOCATE", "XLA_PYTHON_CLIENT_MEM_FRACTION", "MPLBACKEND",
    "MPLCONFIGDIR", "TF_CPP_MIN_LOG_LEVEL", "PYTHONUNBUFFERED", "PYTHONHASHSEED",
    "CUBLAS_WORKSPACE_CONFIG", "LAYERNORM_TYPE", "KMP_DUPLICATE_LIB_OK",
})

#: Values the backend sets itself for every job, from the manifest's resources and the
#: job's own directories. A manifest naming one of these is refused: two sources for the
#: thread count or the visible GPU is how a declared limit stops being the real one.
BACKEND_OWNED_ENV = frozenset({
    "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONDONTWRITEBYTECODE", "HOME",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PYTHONPYCACHEPREFIX",
    "TMPDIR", "LANG", "LC_ALL", "LD_PRELOAD", "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS",
    "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
})

_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_ARGV_NAMESPACES = frozenset({
    "env.python", "env.bin", "env.prefix", "workspace", "harness", "entry_script",
    "weights", "data", "in", "out.dir", "param",
})
_PATH_NAMESPACES = frozenset({"workspace", "harness", "env.prefix"})
_CWD_NAMESPACES = _PATH_NAMESPACES | {"out.dir"}
_ENV_VALUE_NAMESPACES = _PATH_NAMESPACES | {"out.dir"}

#: What a request may lower. Keys of ``gx_job_submit``'s ``tighten``.
TIGHTENABLE = ("wall_seconds", "cpu_threads", "gpu_memory_mib")


# --------------------------------------------------------------------------- locations

def workspace_root() -> Path:
    """Resolve workspace-relative assets against this installed checkout."""
    return harness_root()


def harness_root() -> Path:
    return HARNESS_ROOT


def _base_values() -> dict[str, str]:
    return {"workspace": str(workspace_root()), "harness": str(harness_root())}



# -------------------------------------------------------------------------- placeholders

_PREFIXED = (("env.bin.", "env.bin"), ("in.", "in"), ("param.", "param"),
             ("weights.", "weights"), ("data.", "data"))


def _split(name: str) -> tuple[str | None, str]:
    """``"in.sequences"`` -> ``("in", "sequences")``; an unknown name -> ``(None, name)``."""
    if name in ("env.python", "env.prefix", "workspace", "harness", "entry_script", "out.dir"):
        return name, ""
    for prefix, kind in _PREFIXED:
        if name.startswith(prefix) and name[len(prefix):]:
            return kind, name[len(prefix):]
    return None, name


def _namespace(name: str) -> str | None:
    return _split(name)[0]


def placeholders(token: str) -> list[str]:
    return _PLACEHOLDER.findall(token)


def _token_problems(token: Any, *, where: str, allowed: frozenset[str]) -> list[str]:
    if not isinstance(token, str):
        return [f"{where}: must be a string, got {type(token).__name__}; a list would "
                "expand into several arguments"]
    found: list[str] = []
    if "\x00" in token:
        found.append(f"{where}: contains a NUL byte")
    for name in placeholders(token):
        kind = _namespace(name)
        if kind is None:
            found.append(f"{where}: unknown placeholder {{{name}}}")
        elif kind not in allowed:
            found.append(f"{where}: placeholder {{{name}}} is not allowed here "
                         f"(allowed namespaces: {sorted(allowed)})")
        elif kind in ("in", "param") and not NAME.fullmatch(_split(name)[1]):
            found.append(f"{where}: {{{name}}} names nothing usable; names match {NAME.pattern}")
        elif kind == "env.bin" and not BINARY_NAME.fullmatch(_split(name)[1]):
            found.append(f"{where}: {{{name}}} names no binary; names match {BINARY_NAME.pattern}")
        elif kind in ("weights", "data") and not _split(name)[1].isdigit():
            found.append(f"{where}: {{{name}}} must index by position, e.g. {{{kind}.0}}")
    stripped = _PLACEHOLDER.sub("", token)
    if "{" in stripped or "}" in stripped:
        found.append(f"{where}: unbalanced brace; a literal brace would reach the program")
    return found


def _rooted_path_problems(value: Any, *, where: str) -> list[str]:
    """A path field of the manifest: rooted at a placeholder, never at a bare absolute path."""
    found = _token_problems(value, where=where, allowed=_PATH_NAMESPACES)
    if found or not isinstance(value, str):
        return found
    if not any(value.startswith("{" + root + "}/") for root in _PATH_NAMESPACES):
        found.append(
            f"{where}: {value!r} must start with one of "
            f"{sorted('{' + root + '}/' for root in _PATH_NAMESPACES)}; this workspace is "
            "reachable under two prefixes and a literal one joins against nothing"
        )
    if ".." in Path(_PLACEHOLDER.sub("x", value)).parts:
        found.append(f"{where}: contains '..'")
    return found


# ---------------------------------------------------------------------------- validation

def bundle_problems(bundle: Any) -> list[str]:
    """Every reason this bundle is not an acceptable capability version. Empty = acceptable.

    Schema first (the contract's own table, unknown fields refused), then the rules the
    schema cannot express.
    """
    found = schema.bundle_problems(BUNDLE_KIND, bundle)
    if found:
        return found
    manifest = bundle["manifest"]

    if not CAPABILITY_ID.fullmatch(manifest["capability_id"]):
        found.append(f"capability_id {manifest['capability_id']!r} must match {CAPABILITY_ID.pattern}")
    if not VERSION.fullmatch(manifest["version"]):
        found.append(f"version {manifest['version']!r} must match {VERSION.pattern}")
    if not manifest["env_id"].strip():
        found.append("env_id is empty: a capability with no environment is not callable")

    found.extend(_entry_problems(manifest, bundle))
    found.extend(_weights_problems(manifest))
    for index, item in enumerate(manifest["required_data"]):
        found.extend(_rooted_path_problems(item, where=f"required_data[{index}]"))
    found.extend(_method_card_problems(manifest["method_card"]))
    found.extend(_resources_problems(manifest["resources"]))
    found.extend(_outputs_problems(manifest["outputs"]))
    found.extend(_rooted_path_problems(manifest["source"]["path"], where="source.path"))
    digest = manifest["source"].get("sha256")
    if digest is not None and not SHA256.fullmatch(digest):
        found.append("source.sha256 is not a sha256 hex digest")
    for name in ("licence", "licence_basis", "limitations"):
        if not manifest[name].strip():
            found.append(f"{name} is empty; say what is known, including that it is not known")
    if manifest.get("known_answer") is not None:
        found.append("known-answer controls require an evaluation provider, which is unavailable in this build")
    for name in ("entry_script", "parser"):
        ref = bundle.get(name)
        if ref is not None:
            found.extend(_script_ref_problems(ref, where=name))
    if manifest["kind"] == STDIO_KIND:
        found.append("the installed Job runner supports local commands only")
    return found


def _option_values(template: Sequence[Any], option: str) -> list[Any]:
    """The tokens that follow each occurrence of ``option`` in an argv template."""
    return [template[index + 1] for index, token in enumerate(template[:-1]) if token == option]


def readonly_roots(manifest: Mapping[str, Any]) -> list[str]:
    """The directories an MCP_STDIO_TOOL's server must leave as they were, as the manifest
    names them (``--readonly-root`` tokens of its argv template). The job runner checks them
    before and after the server runs; the backend adds the environment's own prefix."""
    return [str(token) for token in _option_values(manifest["entry"]["argv_template"], "--readonly-root")]


def stdio_tool_name(manifest: Mapping[str, Any]) -> str | None:
    names = _option_values(manifest["entry"]["argv_template"], "--tool")
    return str(names[0]) if len(names) == 1 else None


def _entry_problems(manifest: Mapping[str, Any], bundle: Mapping[str, Any]) -> list[str]:
    entry = manifest["entry"]
    template = entry["argv_template"]
    found: list[str] = []
    if not template:
        return ["entry.argv_template is empty: there is nothing to run"]
    for index, token in enumerate(template):
        found.extend(_token_problems(token, where=f"entry.argv_template[{index}]",
                                     allowed=_ARGV_NAMESPACES))
    head = template[0]
    if isinstance(head, str):
        names = placeholders(head)
        whole = len(names) == 1 and head == "{" + names[0] + "}"
        if not (whole and _namespace(names[0]) in ("env.python", "env.bin")):
            found.append(
                "entry.argv_template[0] must be exactly {env.python} or {env.bin.NAME}: "
                "the program comes from the environment registry, never from a literal "
                f"path or a request; got {head!r}"
            )
    used = {name for token in template if isinstance(token, str) for name in placeholders(token)}
    if "entry_script" in used and bundle.get("entry_script") is None:
        found.append("the argv template uses {entry_script} but the bundle carries no entry_script")
    for name in sorted(used):
        kind = _namespace(name)
        if kind == "weights" and _split(name)[1].isdigit():
            if int(_split(name)[1]) >= len(manifest["weights"]):
                found.append(f"{{{name}}} is beyond the {len(manifest['weights'])} declared weight file(s)")
        if kind == "data" and _split(name)[1].isdigit():
            if int(_split(name)[1]) >= len(manifest["required_data"]):
                found.append(f"{{{name}}} is beyond the {len(manifest['required_data'])} required data path(s)")

    cwd = entry.get("cwd")
    if cwd is not None:
        found.extend(_token_problems(cwd, where="entry.cwd", allowed=_CWD_NAMESPACES))
        if isinstance(cwd, str) and not any(cwd.startswith("{" + root + "}") for root in _CWD_NAMESPACES):
            found.append("entry.cwd must start with a placeholder root such as {workspace} or {out.dir}")
    for index, item in enumerate(entry.get("pythonpath") or []):
        found.extend(_rooted_path_problems(item, where=f"entry.pythonpath[{index}]"))
    for key, value in (entry.get("launch_env") or {}).items():
        where = f"entry.launch_env[{key!r}]"
        if key in BACKEND_OWNED_ENV or key.startswith("GX_") or "proxy" in key.lower():
            found.append(
                f"{where}: the backend sets this value itself (threads, visible GPU, search "
                "paths, home and network routing are not a manifest's to choose)"
            )
        elif key not in LAUNCH_ENV_ALLOWED:
            found.append(f"{where}: not an allowed fixed environment value; allowed: "
                         f"{sorted(LAUNCH_ENV_ALLOWED)}")
        if not isinstance(value, str):
            found.append(f"{where}: value must be a string")
        else:
            found.extend(_token_problems(value, where=where, allowed=_ENV_VALUE_NAMESPACES))
    return found


def _weights_problems(manifest: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for index, weight in enumerate(manifest["weights"]):
        where = f"weights[{index}]"
        found.extend(_rooted_path_problems(weight["path"], where=f"{where}.path"))
        if not SHA256.fullmatch(weight["sha256"]):
            found.append(f"{where}.sha256 is not a sha256 hex digest")
        if weight["bytes"] <= 0:
            found.append(f"{where}.bytes must be positive")
        if weight["load_mode"] == "NONE":
            found.append(f"{where}.load_mode is NONE: a file that is never loaded is not a weight file")
        if weight["load_mode"] == "FULL_PICKLE" and not (weight.get("upstream_digest") or "").strip():
            found.append(
                f"{where}: FULL_PICKLE executes code on load and needs an upstream_digest "
                "recorded before the first load"
            )
    return found


def _method_card_problems(card: Mapping[str, Any]) -> list[str]:
    return [
        f"method_card.{name} is empty; the card is the only source of method identity"
        for name in ("family", "mechanism", "objective", "estimator", "training_data_lineage")
        if not card[name].strip()
    ]


def _resources_problems(resources: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    if resources["cpu_threads"] < 1:
        found.append("resources.cpu_threads must be at least 1")
    if resources["wall_seconds"] < 1:
        found.append("resources.wall_seconds must be at least 1")
    memory = resources.get("memory_mib")
    if memory is not None and memory < 1:
        found.append("resources.memory_mib must be positive when given")
    gpu = resources.get("gpu")
    if gpu is not None:
        if gpu["memory_mib"] < 1:
            found.append("resources.gpu.memory_mib must be positive")
        if not gpu["scope"].strip():
            found.append("resources.gpu.scope is empty; the gpu_jobs switch is scoped")
    return found


def _outputs_problems(outputs: Sequence[Mapping[str, Any]]) -> list[str]:
    found: list[str] = []
    if not outputs:
        found.append("outputs is empty: a capability that declares no output produces nothing "
                     "a job could publish")
    seen: set[str] = set()
    for index, output in enumerate(outputs):
        name = output["name"]
        if not OUTPUT_NAME.fullmatch(name) or name in ("stdout", "stderr"):
            found.append(f"outputs[{index}].name {name!r} must match {OUTPUT_NAME.pattern} and "
                         "is not stdout/stderr, which are always published as logs")
        if name in seen:
            found.append(f"outputs[{index}].name {name!r} is declared twice")
        seen.add(name)
        kind = output["kind"]
        prefixed = kind.startswith((RAW_OUTPUT_PREFIX + ":", DIR_OUTPUT_PREFIX + ":"))
        published_as = kind.split(":", 1)[1] if prefixed else kind
        if kind not in (RAW_OUTPUT_PREFIX, DIR_OUTPUT_PREFIX) and not ARTIFACT_KIND.fullmatch(published_as):
            found.append(f"outputs[{index}].kind {kind!r} must be 'file', 'file:<kind>', 'dir', "
                         f"'dir:<kind>' or '<kind>' with <kind> matching {ARTIFACT_KIND.pattern}")
    return found


def probe_argv_problems(manifest: Mapping[str, Any], argv: Any) -> list[str]:
    """Why an adapter's ``ENTRYPOINT_PROBE`` argv is not a probe of this capability.

    It must start the same program the manifest starts, and may name only what exists
    without a job: no input, no output directory, no parameter.
    """
    if not isinstance(argv, list) or not argv:
        return ["ENTRYPOINT_PROBE.argv must be a non-empty list of strings"]
    found: list[str] = []
    allowed = _ARGV_NAMESPACES - {"in", "out.dir", "param"}
    for index, token in enumerate(argv):
        found.extend(_token_problems(token, where=f"ENTRYPOINT_PROBE.argv[{index}]", allowed=allowed))
    if not found and argv[0] != manifest["entry"]["argv_template"][0]:
        found.append(
            f"ENTRYPOINT_PROBE.argv[0] is {argv[0]!r}; it must be the program the manifest starts, "
            f"{manifest['entry']['argv_template'][0]!r}"
        )
    return found


def allowed_script_path(path: str) -> bool:
    """Directory roots use prefixes; a reviewed single entry uses exact equality."""
    return any(path.startswith(root) if root.endswith("/") else path == root
               for root in SCRIPT_ROOTS)


def _script_ref_problems(ref: Mapping[str, Any], *, where: str) -> list[str]:
    found: list[str] = []
    path = ref["path"]
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts or not allowed_script_path(path):
        found.append(f"{where}.path {path!r} must be a path of this tree inside one of "
                     f"{list(SCRIPT_ROOTS)}")
    if not path.endswith(".py"):
        found.append(f"{where}.path must name a Python file")
    if not SHA256.fullmatch(ref["sha256"]):
        found.append(f"{where}.sha256 is not a sha256 hex digest")
    if not ref["artifact_ref"].strip():
        found.append(f"{where}.artifact_ref is empty")
    return found


def require_valid(bundle: Any) -> dict[str, Any]:
    """The canonical form of an acceptable bundle, or ``SCHEMA_INVALID`` with every problem."""
    found = bundle_problems(bundle)
    if found:
        raise SchemaInvalid(
            f"capability manifest bundle has {len(found)} problem(s): {found[0]}",
            details={"problems": found},
        )
    return canonical_bundle(bundle)


# ------------------------------------------------------------------------- canonical form

def canonical_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    return schema.prune_bundle(BUNDLE_KIND, bundle)


def content_bytes(bundle: Mapping[str, Any]) -> bytes:
    """The bytes a capability version is addressed by."""
    return ids.canonical_json(canonical_bundle(bundle)).encode("utf-8")


def content_sha256(bundle: Mapping[str, Any]) -> str:
    return hashlib.sha256(content_bytes(bundle)).hexdigest()


def capability_version(manifest: Mapping[str, Any]) -> str:
    return f"{manifest['capability_id']}@{manifest['version']}"


def parse_capability_version(value: Any) -> tuple[str, str]:
    """``"selftest@1"`` -> ``("selftest", "1")``."""
    if not isinstance(value, str) or value.count("@") != 1:
        raise SchemaInvalid(
            f"a capability version looks like '<capability_id>@<version>', got {value!r}",
            details={"capability_version": value},
        )
    capability_id, version = value.split("@")
    if not CAPABILITY_ID.fullmatch(capability_id) or not VERSION.fullmatch(version):
        raise SchemaInvalid(
            f"{value!r} is not a well-formed capability version",
            details={"capability_version": value},
        )
    return capability_id, version


def input_roles(manifest: Mapping[str, Any]) -> list[str]:
    """The input roles a job must supply: every ``{in.ROLE}`` the argv template names."""
    return sorted({_split(name)[1] for token in manifest["entry"]["argv_template"]
                   for name in placeholders(token) if _namespace(name) == "in"})


def parameter_names(manifest: Mapping[str, Any]) -> list[str]:
    return sorted({_split(name)[1] for token in manifest["entry"]["argv_template"]
                   for name in placeholders(token) if _namespace(name) == "param"})


def is_raw_output(output: Mapping[str, Any]) -> bool:
    kind = output["kind"]
    return kind == RAW_OUTPUT_PREFIX or kind.startswith(RAW_OUTPUT_PREFIX + ":")


def is_dir_output(output: Mapping[str, Any]) -> bool:
    kind = output["kind"]
    return kind == DIR_OUTPUT_PREFIX or kind.startswith(DIR_OUTPUT_PREFIX + ":")


# ---------------------------------------------------------------------------- resolution

@dataclass(frozen=True)
class EnvView:
    """What the launch needs from an environment row."""

    env_id: str
    python: str | None
    binaries: Mapping[str, str]
    prefix: str | None


def resolve_path(template: str, *, env: EnvView | None = None, out_dir: str | None = None) -> Path:
    """A rooted path field of the manifest as a real path on this host."""
    values: dict[str, str] = _base_values()
    if env is not None and env.prefix:
        values["env.prefix"] = env.prefix
    if out_dir is not None:
        values["out.dir"] = out_dir
    return Path(_substitute(template, values, where="path"))


def script_path(ref: Mapping[str, Any]) -> Path:
    """The file a ScriptRef names, after checking that its bytes are the bound ones."""
    path = harness_root() / ref["path"]
    try:
        observed = ids.sha256_of_file(path)
    except FileNotFoundError as exc:
        error = ContentHashMismatch(
            f"bound script {ref['path']} is missing from the GX release",
            details={"path": ref["path"], "expected_sha256": ref["sha256"],
                     "reason": "release_file_missing"},
        )
        error.suggested_recovery = (
            "Release engineering can install the file at the reported path. "
            "Retry this binding if the installed bytes match the expected SHA-256, "
            "or register the revised capability version with its actual scripts."
        )
        raise error from exc
    except OSError as exc:
        raise ContentHashMismatch(
            f"bound script {ref['path']} cannot be read: {exc}",
            details={"path": ref["path"], "expected_sha256": ref["sha256"]},
        ) from exc
    if observed != ref["sha256"]:
        raise ContentHashMismatch(
            f"bound script {ref['path']} hashes to {observed[:16]}…, the capability version "
            f"was registered with {ref['sha256'][:16]}…; changed code is a new version",
            details={"path": ref["path"], "expected_sha256": ref["sha256"],
                     "observed_sha256": observed},
        )
    return path


def _substitute(token: str, values: Mapping[str, str], *, where: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in values:
            raise SchemaInvalid(
                f"{where}: placeholder {{{name}}} has no value; a literal {{{name}}} must "
                "never reach a program",
                details={"placeholder": name, "where": where, "known": sorted(values)},
            )
        return values[name]

    return _PLACEHOLDER.sub(replace, token)


def coerce_parameter(name: str, value: Any, *, whole_token: bool) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SchemaInvalid(
            f"parameter {name!r} must be a string or a number, got {type(value).__name__}",
            details={"parameter": name},
        )
    text = str(value)
    if "\x00" in text or "\n" in text:
        raise SchemaInvalid(f"parameter {name!r} contains a NUL or a newline",
                            details={"parameter": name})
    if whole_token and text.startswith("-"):
        raise SchemaInvalid(
            f"parameter {name!r} would begin an argument with '-', which the program would "
            "read as an option the manifest did not declare",
            details={"parameter": name},
        )
    return text


def effective_resources(manifest: Mapping[str, Any], tighten: Mapping[str, Any] | None) -> dict[str, Any]:
    """The manifest's resources with a request's tightening applied.

    A request lowers; it never raises and never removes. An unknown key is refused
    rather than ignored, so a field cannot be slipped past as if it had been honoured.
    """
    resources = manifest["resources"]
    gpu = resources.get("gpu")
    out: dict[str, Any] = {
        "cpu_threads": int(resources["cpu_threads"]),
        "wall_seconds": int(resources["wall_seconds"]),
        "memory_mib": resources.get("memory_mib"),
        "gpu": {"memory_mib": int(gpu["memory_mib"]), "scope": gpu["scope"]} if gpu else None,
        "tightened": {},
    }
    for key, value in dict(tighten or {}).items():
        if key not in TIGHTENABLE:
            raise SchemaInvalid(
                f"tighten has no key {key!r}; a request may lower {list(TIGHTENABLE)} and "
                "nothing else",
                details={"key": key, "allowed": list(TIGHTENABLE)},
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SchemaInvalid(f"tighten.{key} must be a positive integer",
                                details={"key": key, "value": repr(value)})
        if key == "gpu_memory_mib":
            if out["gpu"] is None:
                raise SchemaInvalid(
                    "tighten.gpu_memory_mib was given for a capability that declares no GPU "
                    "need; a request cannot add one",
                    details={"key": key},
                )
            ceiling = out["gpu"]["memory_mib"]
        else:
            ceiling = out[key]
        if value > ceiling:
            raise SchemaInvalid(
                f"tighten.{key}={value} is above the manifest's {ceiling}; a request may only "
                "lower a limit",
                details={"key": key, "requested": value, "manifest": ceiling},
            )
        if key == "gpu_memory_mib":
            out["gpu"]["memory_mib"] = value
        else:
            out[key] = value
        out["tightened"][key] = value
    return out


@dataclass
class LaunchPlan:
    """An argv array, a working directory and an environment. Nothing else starts a tool."""

    argv: list[str]
    cwd: str
    env: dict[str, str]
    entry_script: str | None = None
    notes: list[str] = field(default_factory=list)


def launch_plan(
    bundle: Mapping[str, Any],
    env: EnvView,
    *,
    inputs: Mapping[str, str] | None = None,
    out_dir: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    home_dir: str | None = None,
    tmp_dir: str | None = None,
    cpu_threads: int | None = None,
    gpu_device: str | None = None,
    gpu_memory_mib: int | None = None,
    argv_template: Sequence[str] | None = None,
    resource_limits: Mapping[str, Any] | None = None,
) -> LaunchPlan:
    """Resolve a bundle's launch recipe against an environment and one job's values.

    ``argv_template`` lets a probe run the same recipe with a different argument list
    (an entry point asked to answer, with no inputs); everything else — interpreter,
    search path, working directory, fixed values — is the recipe the job will use.
    """
    manifest = bundle["manifest"]
    entry = manifest["entry"]
    template = list(argv_template if argv_template is not None else entry["argv_template"])
    supplied = dict(parameters or {})
    declared = set(parameter_names(manifest))
    undeclared = sorted(set(supplied) - declared)
    if undeclared and argv_template is None:
        raise SchemaInvalid(
            f"parameter(s) {undeclared} are not declared by {capability_version(manifest)}; "
            f"its template declares {sorted(declared)}",
            details={"undeclared": undeclared, "declared": sorted(declared)},
        )

    values: dict[str, str] = _base_values()
    if env.prefix:
        values["env.prefix"] = env.prefix
    if out_dir is not None:
        values["out.dir"] = out_dir
    if env.python:
        values["env.python"] = env.python
    for name, path in env.binaries.items():
        values[f"env.bin.{name}"] = path

    entry_script: str | None = None
    if bundle.get("entry_script") is not None:
        entry_script = str(script_path(bundle["entry_script"]))
        values["entry_script"] = entry_script
    for index, weight in enumerate(manifest["weights"]):
        values[f"weights.{index}"] = str(resolve_path(weight["path"], env=env))
    for index, item in enumerate(manifest["required_data"]):
        values[f"data.{index}"] = str(resolve_path(item, env=env))
    for role, path in dict(inputs or {}).items():
        values[f"in.{role}"] = str(path)

    argv: list[str] = []
    for index, token in enumerate(template):
        names = placeholders(token)
        local = dict(values)
        for name in names:
            if _namespace(name) == "param":
                key = name[len("param."):]
                if key not in supplied:
                    raise SchemaInvalid(
                        f"parameter {key!r} is required by {capability_version(manifest)} and "
                        "was not supplied; there are no defaults, so two runs that differ in "
                        "a flag differ in what was recorded",
                        details={"parameter": key, "declared": sorted(declared)},
                    )
                local[name] = coerce_parameter(key, supplied[key],
                                               whole_token=token == "{" + name + "}")
            if _namespace(name) in ("env.python", "env.bin") and name not in local:
                raise EnvNotReady(
                    f"argv needs {{{name}}} and environment {env.env_id!r} records no such "
                    "executable",
                    details={"env_id": env.env_id, "placeholder": name,
                             "known_binaries": sorted(env.binaries)},
                )
        argv.append(_substitute(token, local, where=f"argv[{index}]"))

    cwd_template = entry.get("cwd")
    if cwd_template:
        cwd = _substitute(cwd_template, values, where="entry.cwd")
    elif out_dir is not None:
        cwd = out_dir
    else:
        cwd = "/"

    threads = int(cpu_threads if cpu_threads is not None else manifest["resources"]["cpu_threads"])
    path_parts = [str(Path(env.prefix) / "bin")] if env.prefix else []
    process_env: dict[str, str] = {
        "PATH": os.pathsep.join(path_parts + [_SYSTEM_PATH]),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": str(threads),
        "MKL_NUM_THREADS": str(threads),
        "OPENBLAS_NUM_THREADS": str(threads),
        "NUMEXPR_NUM_THREADS": str(threads),
        "GX_CPU_THREADS": str(threads),
        # A capability that declares no GPU need is not shown a GPU. The need is read from
        # the manifest, so leaving a field out of a request cannot reach the device.
        "CUDA_VISIBLE_DEVICES": gpu_device if gpu_device is not None else "",
    }
    if home_dir is not None:
        # Whatever a tool caches or configures lands in the job's own directory: importing a
        # module otherwise writes __pycache__ beside it, in a tree that is not ours to write.
        process_env["HOME"] = home_dir
        process_env["XDG_CACHE_HOME"] = os.path.join(home_dir, ".cache")
        process_env["XDG_CONFIG_HOME"] = os.path.join(home_dir, ".config")
        process_env["XDG_DATA_HOME"] = os.path.join(home_dir, ".local", "share")
        process_env["PYTHONPYCACHEPREFIX"] = os.path.join(home_dir, ".pycache")
    if tmp_dir is not None:
        process_env["TMPDIR"] = tmp_dir
    if out_dir is not None:
        process_env["GX_OUT_DIR"] = out_dir
    if gpu_device is not None and gpu_memory_mib is not None:
        process_env["GX_GPU_MEMORY_MIB"] = str(int(gpu_memory_mib))
    search = [_substitute(item, values, where="entry.pythonpath") for item in entry.get("pythonpath") or []]
    if search:
        process_env["PYTHONPATH"] = os.pathsep.join(search)
    for key, value in (entry.get("launch_env") or {}).items():
        process_env[key] = _substitute(value, values, where=f"entry.launch_env[{key!r}]")
    # Keep proxy routing even for a command whose inputs are entirely local.
    process_env.update(proxy_environment())

    return LaunchPlan(argv=argv, cwd=cwd, env=process_env, entry_script=entry_script)


#: The names a client reads its proxy from.
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
#: Historical closed-route terms retained for existing specs/evidence readers.
CLOSED_PROXY_HOST, CLOSED_PROXY_PORT = "127.0.0.1", 9
NETWORK_CLOSED, NETWORK_PROXY = "closed", "clash_proxy"


def proxy_environment() -> dict[str, str]:
    """The proxy variables, from ``config/limits.json`` ``network``: the one route out."""
    network = config.limits()["network"]
    proxy = str(network["http_proxy"])
    return {**{name: proxy for name in PROXY_NAMES}, "NO_PROXY": NO_PROXY_HOSTS,
            "no_proxy": NO_PROXY_HOSTS, "GX_ENFORCE_CLASH": "1"}


def closed_network_environment() -> dict[str, str]:
    """Historical helper; current launch planning uses proxy_environment()."""
    closed = f"http://{CLOSED_PROXY_HOST}:{CLOSED_PROXY_PORT}"
    return {**{name: closed for name in PROXY_NAMES}, "NO_PROXY": NO_PROXY_HOSTS, "no_proxy": NO_PROXY_HOSTS}


def network_terms(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Record the configured proxy. Operators can require a pre-launch listener check."""
    network = config.limits()["network"]
    proxy = str(network["http_proxy"])
    terms = {"mode": NETWORK_PROXY, "proxy": proxy}
    if network.get("require_proxy_probe"):
        from urllib.parse import urlsplit
        address = urlsplit(proxy)
        if address.hostname is None or address.port is None:
            raise SchemaInvalid("the proxy listener check needs a hostname and an explicit port")
        terms["must_answer"] = [address.hostname, address.port]
    return terms


__all__ = [
    "ASSET_KIND", "BACKEND_OWNED_ENV", "BUNDLE_KIND", "DIR_OUTPUT_PREFIX", "EnvView", "KNOWN_ANSWER_KINDS",
    "LAUNCH_ENV_ALLOWED", "LaunchPlan", "SCRIPT_ROOTS",
    "STDIO_KIND", "STDIO_SWITCH", "STDIO_SWITCH_SCOPE", "TIGHTENABLE", "bundle_problems",
    "canonical_bundle",
    "capability_version", "content_bytes", "content_sha256", "effective_resources",
    "NETWORK_CLOSED", "NETWORK_PROXY", "PROXY_NAMES", "closed_network_environment", "network_terms",
    "harness_root", "input_roles", "is_dir_output", "is_raw_output", "launch_plan",
    "parameter_names", "parse_capability_version", "placeholders", "probe_argv_problems", "proxy_environment",
    "readonly_roots", "require_valid", "resolve_path", "script_path", "stdio_tool_name", "workspace_root",
]
