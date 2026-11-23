"""Codex model selection and the Linux deployment's outbound proxy settings."""

import json
import os
from pathlib import Path
import re
import socket
import tomllib
from urllib.parse import urlsplit

from .. import config


DEFAULT_MODEL = "gpt-6.2-astra"
CODEX_VERSION = "0.169.0"
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_LEADS = {"main", "lead", "team_lead", "group_lead", "coordinator", "decision", "ultra"}
_ROUTINE = {"simple", "routine", "execution", "executor", "test", "tester", "testing", "smoke", "xhigh"}


def _testing(test_mode: bool | None) -> bool:
    return os.environ.get("GX_P0_TEST") == "1" if test_mode is None else test_mode


def effort_for(role_or_class: str, *, test_mode: bool | None = None) -> str:
    """Choose the GX effort for a role or task class; scientific work defaults to max."""
    role = role_or_class.strip().lower().removeprefix("role.").replace("-", "_")
    if _testing(test_mode) or role in _ROUTINE:
        return "xhigh"
    return "ultra" if role in _LEADS else "max"


def role_profile(kind: str, *, model: str | None = None, test_mode: bool | None = None) -> dict[str, str]:
    return {"model": model or os.environ.get("GX_P0_MODEL") or DEFAULT_MODEL,
            "effort": effort_for(kind, test_mode=test_mode)}


def provider_name(*, test_mode: bool | None = None, provider: str | None = None) -> str:
    if provider is None:
        fixture = os.environ.get("GX_P0_TEST_PROVIDER")
        if fixture and fixture != "openai" and not _testing(test_mode):
            raise ValueError("GX_P0_TEST_PROVIDER requires GX_P0_TEST=1")
        provider = fixture or os.environ.get("GX_P0_PROVIDER") or "openai"
    if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
        raise ValueError("Use an official Codex provider ID in the model profile")
    return provider


def read_overrides():
    filename = os.environ.get("GX_P0_PROFILE")
    if not filename:
        return {}
    try:
        raw = json.loads(Path(filename).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("GX_P0_PROFILE must name a readable JSON file") from exc
    if not isinstance(raw, dict) or set(raw) - {"default", "roles", "task_classes"}:
        raise ValueError("The model profile accepts default, roles and task_classes")
    return raw


def _route_fields(value):
    if not isinstance(value, dict) or set(value) - {"provider", "model", "effort", "tools", "multi_agent"}:
        raise ValueError("Profile entries accept provider, model, effort, tools and multi_agent")
    if "multi_agent" in value and type(value["multi_agent"]) is not bool:
        raise ValueError("multi_agent must be a boolean")
    if "provider" in value:
        if value["provider"] is None:
            raise ValueError("Supply a Codex provider ID")
        provider_name(provider=value["provider"])
    if "model" in value and (not isinstance(value["model"], str) or
                             not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]*", value["model"])):
        raise ValueError("Supply a provider model ID")
    if "effort" in value and (not isinstance(value["effort"], str) or not value["effort"].strip()
                              or value["effort"] != value["effort"].strip()):
        raise ValueError("Supply a nonempty reasoning effort from the provider's official model catalog")
    names = value.get("tools", [])
    if (not isinstance(names, list) or
            not all(isinstance(name, str) and name and name.strip() == name for name in names)
            or len(names) != len(set(names))):
        raise ValueError("tools must list distinct GX dynamic tool names")
    return value


def project_routes(document, overrides):
    """Resolve the operator selection once; store the result with the run's policy."""
    default = {**_route_fields({"model": role_profile("main")["model"], "provider": provider_name()}),
               **_route_fields(overrides.get("default", {}))}
    classes = overrides.get("task_classes", {})
    roles = overrides.get("roles", {})
    known = {"main": document["roles"]["coordinator"], **document["roles"]}
    if not isinstance(classes, dict) or set(classes) - set(document["task_classes"]):
        raise ValueError("Model profile contains an unknown GX task class")
    if not isinstance(roles, dict) or set(roles) - set(known):
        raise ValueError("Model profile contains an unknown GX role")
    for value in (*roles.values(), *classes.values()):
        _route_fields(value)
    result = {}
    # The main and planner coordinate decisions. Task classes supply the complexity
    # baseline for each other role, including simple execution and code authoring.
    for role, role_spec in known.items():
        task_class = role_spec["task_class"]
        level = effort_for("lead" if role in {"main", "coordinator", "planner"} else task_class)
        route = {**default, **classes.get(task_class, {}), **roles.get(role, {})}
        selected = route.get("effort", level)
        if role != "main" and "tools" in route and "gx_submit_product" not in route["tools"]:
            raise ValueError(f"The {role} task requires gx_submit_product to deliver its result")
        result[role] = {**route, "effort": selected}
    return result


def codex_config():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        with (home / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError("Codex config.toml is unreadable") from exc
    if not isinstance(config.get("model_providers", {}), dict):
        raise ValueError("Codex model_providers must be a TOML table")
    return config


def provider_endpoint(provider, config, *, test_mode, environ=None):
    """Validate the Codex-owned transport; never copy credentials into project state."""
    if provider == "openai":
        return None
    info = (config.get("model_providers") or {}).get(provider)
    if not isinstance(info, dict):
        raise ValueError(f"Configure model_providers.{provider} in the official Codex config.toml")
    if info.get("wire_api", "responses") != "responses":
        raise ValueError(f"{provider} requires wire_api=responses for Codex {CODEX_VERSION}")
    url = info.get("base_url")
    target = urlsplit(url) if isinstance(url, str) else None
    if (not target or not target.hostname or target.scheme not in {"http", "https"} or
            target.username is not None or target.password is not None or target.query or target.fragment):
        raise ValueError(f"Configure a service base_url without credentials for {provider}")
    if test_mode and target.hostname not in _LOOPBACK:
        raise ValueError(f"Configure a loopback base_url for the selected P0 test provider {provider}")
    if not test_mode and info.get("requires_openai_auth"):
        raise ValueError(f"Configure {provider}'s own credentials in Codex")
    key = info.get("env_key")
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise ValueError(f"Set env_key to the credential environment variable name for {provider}")
    if key and not (os.environ if environ is None else environ).get(key):
        raise ValueError(f"The credential environment variable configured for {provider} is missing")
    return url.rstrip("/")


def selected_tools(available, names):
    if names is None:
        return available
    by_name = {tool["name"]: tool for tool in available}
    missing = set(names) - set(by_name)
    if missing:
        raise ValueError("GX tools unavailable for this root: " + ", ".join(sorted(missing)))
    return [by_name[name] for name in names]


_DELIVER = {"gx_task_context", "gx_read_artifact", "gx_submit_product"}
_GRID_REVIEW = _DELIVER | {"gx_grid_measurement"}
_COMPUTE = _DELIVER | {"gx_capability_inspect", "gx_job_submit", "gx_job_status", "gx_wait_job"}
_ROLE_TOOLS = {
    "evidence_reviewer": _GRID_REVIEW,
    "planner": _GRID_REVIEW,
    "researcher": _COMPUTE | {"gx_job_cancel"},
    "executor": _COMPUTE,
}


def available_for_role(available, role, profile, *, scoped):
    """A frozen role override can assign a run plugin or independent computation."""
    if "tools" in profile or not scoped or role == "main":
        return selected_tools(available, profile.get("tools"))
    allowed = _ROLE_TOOLS.get(role, _DELIVER)
    return [tool for tool in available if tool["name"] in allowed]


def worker_instructions(names, *, delegate):
    guidance = ["Base your product on the cited evidence, including observations and evaluations."]
    if "gx_read_artifact" in names:
        guidance.append("Read missing cited originals with gx_read_artifact.")
    if "gx_task_context" in names:
        guidance.append("Refresh gx_task_context after a change.")
    if "gx_wait_job" in names:
        guidance.append("End the turn when gx_wait_job parks the attempt.")
    if delegate and "gx_read_artifact" in names:
        guidance.append("Give direct native children the exact references for their analysis. "
                        "Children may read cited artifacts and retry a read during verification.")
    guidance.append("Submit your own product with gx_submit_product.")
    return " ".join(guidance)


def model_catalog(client, provider):
    from ..hostobs.app_server_client import AppServerError
    params = {"modelProvider": provider, "includeHidden": False, "limit": 100}
    result, seen = {}, set()
    while True:
        page = client.request("model/list", params)
        for row in page["data"]:
            if not row.get("hidden"):
                result[row["model"]] = [v["reasoningEffort"] for v in row["supportedReasoningEfforts"]]
        cursor = page.get("nextCursor")
        if not cursor:
            return result
        if cursor in seen:
            raise AppServerError("Codex model/list repeated a pagination cursor")
        seen.add(cursor)
        params = {**params, "cursor": cursor}


def require_catalog_entry(catalog, provider, model, effort):
    if effort not in catalog.get(model, ()):
        raise ValueError(f"Codex model/list for {provider} does not advertise {model} with {effort} effort")


def proxy_settings() -> dict[str, str]:
    """Set both casings and preserve a caller's more restrictive bypass list."""
    allowed = set(_LOOPBACK)
    for key in ("NO_PROXY", "no_proxy"):
        if key in os.environ:
            allowed.intersection_update(value.strip() for value in os.environ[key].split(","))
    bypass = ",".join(address for address in _LOOPBACK if address in allowed)
    network = json.loads((config.CONFIG_DIR / "p0_network.json").read_text())
    values = {"NO_PROXY": bypass}
    if network["mode"] == "proxy":
        for key in ("http", "https", "all"):
            url = network.get(key, "")
            endpoint = urlsplit(url)
            if endpoint.scheme not in ("http", "https", "socks5h") or not endpoint.hostname or not endpoint.port:
                raise ValueError(f"Set a complete {key} proxy endpoint in p0_network.json")
            values[key.upper() + "_PROXY"] = url
    elif network["mode"] == "environment":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            inherited = os.environ.get(key.lower()) or os.environ.get(key)
            if inherited:
                values[key] = inherited
    else:
        raise ValueError("p0_network.json mode must be proxy or environment")
    return {**values, **{key.lower(): value for key, value in values.items()}}


def clash_env(*, check: bool = True) -> dict[str, str]:
    """Return the child environment and fail before network use if Clash is unavailable."""
    selected = proxy_settings()
    if check:
        endpoints = dict.fromkeys(selected[k] for k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY") if k in selected)
        for endpoint in endpoints:
            parsed = urlsplit(endpoint)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            try:
                with socket.create_connection((parsed.hostname, port), timeout=2):
                    pass
            except OSError as exc:
                raise ConnectionError(f"Configured proxy is unavailable at {parsed.hostname}:{port}") from exc
    return {**os.environ, **selected}


def codex_argv(*, test_mode: bool | None = None, provider: str | None = None) -> list[str]:
    chosen_provider = provider_name(test_mode=test_mode, provider=provider)
    packaged = Path(__file__).resolve().parents[3] / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    return [
        os.environ.get("GX_P0_CODEX") or (str(packaged) if packaged.is_file() else "codex"),
        "--enable", "respect_system_proxy",
        "--disable", "system_proxy_fallback",
        "-c", f"model_provider={json.dumps(chosen_provider)}",
    ]


def app_server_argv(*, test_mode: bool | None = None, provider: str | None = None) -> list[str]:
    args = [*codex_argv(test_mode=test_mode, provider=provider), "--enable", "goals"]
    if _testing(test_mode):
        args.extend([
            "-c", 'model_reasoning_effort="xhigh"',
            "-c", 'agents.default_subagent_reasoning_effort="xhigh"',
            "-c", "features.multi_agent_v2={enabled=true,expose_spawn_agent_model_overrides=false}",
        ])
    return [*args, "-c", 'experimental_thread_store={type="local"}', "app-server", "--listen", "stdio://"]
