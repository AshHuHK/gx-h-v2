"""Codex model selection and the Linux deployment's outbound proxy settings."""

import json
import os
import socket


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
    provider = provider if provider is not None else os.environ.get("GX_P0_TEST_PROVIDER")
    if provider and provider != "openai" and not _testing(test_mode):
        raise ValueError("GX_P0_TEST_PROVIDER requires GX_P0_TEST=1")
    return provider or "openai"


def proxy_settings() -> dict[str, str]:
    """Set both casings and preserve a caller's more restrictive bypass list."""
    allowed = set(_LOOPBACK)
    for key in ("NO_PROXY", "no_proxy"):
        if key in os.environ:
            allowed.intersection_update(value.strip() for value in os.environ[key].split(","))
    bypass = ",".join(address for address in _LOOPBACK if address in allowed)
    values = {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "ALL_PROXY": "socks5h://127.0.0.1:7891",
        "NO_PROXY": bypass,
    }
    return {**values, **{key.lower(): value for key, value in values.items()}}


def clash_env(*, check: bool = True) -> dict[str, str]:
    """Return the child environment and fail before network use if Clash is unavailable."""
    if check:
        for port in (7890, 7891):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    pass
            except OSError as exc:
                raise ConnectionError(f"Clash is unavailable at 127.0.0.1:{port}") from exc
    return {**os.environ, **proxy_settings()}


def codex_argv(*, test_mode: bool | None = None, provider: str | None = None) -> list[str]:
    chosen_provider = provider_name(test_mode=test_mode, provider=provider)
    return [
        os.environ.get("GX_P0_CODEX") or "codex",
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
