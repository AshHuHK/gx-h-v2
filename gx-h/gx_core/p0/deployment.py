"""Operator commands for the package's official Codex CLI."""

import json
import re
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from ..hostobs.app_server_client import (
    AppServerClient, AppServerDisconnected, AppServerError, AppServerResponseError, AppServerTimeout,
)
from ..hostobs import host_policy
from . import profiles
from .profiles import CODEX_VERSION, app_server_argv, clash_env, codex_argv


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _version(env):
    cmd = subprocess.run([codex_argv(provider="openai")[0], "--version"], env=env, capture_output=True, text=True, timeout=15)
    match = re.fullmatch(r"codex-cli ([^\s]+)", cmd.stdout.strip())
    return match.group(1) if cmd.returncode == 0 and match else None


def _login_state(env):
    result = subprocess.run([*codex_argv(provider="openai"), "login", "status"], env=env, capture_output=True, text=True, timeout=30)
    lines = (result.stdout + result.stderr).splitlines()
    if result.returncode == 0:
        return "chatgpt" if "Logged in using ChatGPT" in lines else "other_auth"
    return "signed_out" if "Not logged in" in lines else "unavailable"


def _login(args):
    env = clash_env()
    if _version(env) != CODEX_VERSION:
        _print({"error": f"Select codex-cli {CODEX_VERSION} with GX_P0_CODEX."})
        return 1
    state = _login_state(env)
    if args.command == "login-status" or state in {"chatgpt", "unavailable"}:
        _print({"login": state, "chatgpt_oauth": state == "chatgpt"})
        return int(state != "chatgpt")
    # The operator receives OpenAI's one-time device instructions on their terminal.
    # All subsequent credential exchanges and storage belong to the official CLI.
    return subprocess.run([*codex_argv(provider="openai"), "login", "--device-auth"], env=env).returncode


def _local_profiles():
    routes = host_policy.p0_policy()[0]["p0_role_profiles"]
    config = profiles.codex_config()
    providers = {}
    for name in sorted({route["provider"] for route in routes.values()}):
        try:
            profiles.provider_endpoint(name, config, test_mode=profiles._testing(None))
            providers[name] = {"configured": True, "wire_api": "responses"}
        except ValueError as exc:
            providers[name] = {"configured": False, "error": str(exc)}
    return {"roles": routes, "providers": providers, "configured": all(p["configured"] for p in providers.values()),
            "custom_provider_ids": sorted(set(config.get("model_providers", {})) - {"openai"}),
            "inference": "not_run", "mode": "local_configuration"}


def _saved_profiles(directory, run_id=None):
    from .cli import existing_db
    from .driver import project
    with closing(existing_db(directory.expanduser().resolve(), readonly=True)) as conn:
        run = project(conn, run_id)
        if run is None:
            raise ValueError("There is no GX project in this state directory")
        document, _ = host_policy.current_for_run(run)
        host = json.loads(run["host_json"] or "{}").get("policy_selection", {})
        roots = []
        for record in conn.execute("SELECT * FROM p0_roots WHERE run_id=? ORDER BY rowid", (run["run_id"],)):
            row = dict(record)
            roots.append({"role": row["role"], "task_id": row["task_id"], "attempt": row["attempt"],
                          "thread_id": row["thread_id"],
                          "requested": {"provider": row.get("provider") or host.get("model_provider"),
                                        "model": row["model"], "effort": row["effort"]},
                          "gx_tools": json.loads(row.get("tool_names_json") or "null"),
                          "codex_effective": json.loads(row.get("effective_profile_json") or "null")})
        return {"mode": "saved_project", "run_id": run["run_id"],
                "default_provider": host.get("model_provider"), "roles": document.get("p0_role_profiles"), "roots": roots}


def _smoke(client, cwd, provider, model, effort="xhigh"):
    opened = client.request("thread/start", {
        "cwd": cwd, "model": model, "modelProvider": provider, "ephemeral": True,
        "approvalPolicy": "never", "sandbox": "read-only",
        "baseInstructions": "Follow the user request concisely.", "developerInstructions": "Use no tools.",
        "config": {"model_reasoning_effort": effort, "features.goals": False},
    })
    thread_id = opened["thread"]["id"]
    result = {"thread_id": thread_id, "model": opened["model"], "provider": opened["modelProvider"],
              "effort": opened["reasoningEffort"], "requested": {"model":model, "provider":provider, "effort":effort},
              "matched": False}
    if (result["model"], result["provider"], result["effort"]) != (model, provider, effort):
        result["error"] = "Thread configuration differs from the requested model/provider/effort."
        return result
    started = client.request("turn/start", {"threadId": thread_id, "effort": effort, "input": [
        {"type": "text", "text": "Reply with exactly GX_P0_OAUTH_OK. Use no tools."}]})
    turn_id = result["turn_id"] = started["turn"]["id"]
    final_text = []

    def completed(event):
        message = event.message
        method, params = message.get("method"), message.get("params", {})
        if params.get("threadId") != thread_id:
            return False
        if method == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
            result["tokens"] = params["tokenUsage"]["last"]
        if method == "item/completed" and params.get("turnId") == turn_id:
            item = params["item"]
            if item.get("type") == "agentMessage":
                final_text.append(item["text"].strip())
        if method == "turn/completed" and params["turn"]["id"] == turn_id:
            result["status"] = params["turn"]["status"]
            return True
        return False

    try:
        client.run_until(completed, timeout=120)
    except (AppServerDisconnected, AppServerTimeout) as exc:
        result.update(status="unknown", error=type(exc).__name__)
        return result
    result["matched"] = result.get("status") == "completed" and final_text[-1:] == ["GX_P0_OAUTH_OK"]
    return result


def _doctor(args):
    local = _local_profiles()
    routes = local["roles"]
    role = getattr(args, "role", None)
    if role and role not in routes:
        raise ValueError("Unknown GX model-profile role")
    selected = routes[role] if role else {**routes["main"], "effort": "xhigh"}
    provider, model = selected["provider"], selected["model"]
    check = [selected] if role else list(routes.values())
    if args.smoke:
        check.append(selected)
    providers = sorted({r["provider"] for r in check})
    if any(not local["providers"][name]["configured"] for name in providers):
        _print({**local, "ready": False})
        return 1
    env = clash_env()
    version = _version(env)
    report = {"codex_version": version, "expected_version": CODEX_VERSION, "provider": provider, "model": model,
              "http_proxy": env.get("HTTP_PROXY"), "socks_proxy": env.get("ALL_PROXY"), "no_proxy": env["NO_PROXY"]}
    if version != CODEX_VERSION:
        _print({**report, "ready": False, "error": "Select the pinned official Codex CLI with GX_P0_CODEX."})
        return 1
    with tempfile.TemporaryDirectory(prefix="gx-p0-doctor-") as cwd:
        with AppServerClient(app_server_argv(provider=provider), cwd=cwd, env=env, default_timeout=45) as client:
            client.request("initialize", {"clientInfo": {"name": "gx_p0_doctor", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
            client.notify("initialized")
            if "openai" in providers:
                account = client.request("account/read", {"refreshToken": False}).get("account")
                report["account_type"] = (account or {}).get("type")
                if report["account_type"] != "chatgpt":
                    _print({**report, "ready": False, "error": "Run gx-p0 login to sign in with ChatGPT."})
                    return 1
            if any(name != "openai" for name in providers):
                effective = client.request("config/read", {"cwd": cwd, "includeLayers": False})["config"]
                for name in providers:
                    profiles.provider_endpoint(name, effective, test_mode=profiles._testing(None), environ=env)
            catalogs = {name: profiles.model_catalog(client, name) for name in providers}
            report.update(models=catalogs[provider], required_efforts=sorted({r["effort"] for r in check}),
                          roles={role: routes[role]} if role else routes)
            missing = [dict(provider=p, model=m, effort=e) for p,m,e in sorted({
                (r["provider"], r["model"], r["effort"]) for r in check
                if r["effort"] not in catalogs[r["provider"]].get(r["model"], ())
            })]
            if missing:
                _print({**report, "ready": False, "missing_efforts": missing})
                return 1
            if args.smoke:
                report["inference"] = _smoke(client, cwd, provider, model, selected["effort"])
    report["ready"] = report.get("inference", {}).get("matched", True)
    report["validation"] = "inference" if args.smoke else "account_and_model_catalog"
    _print(report)
    return int(not report["ready"])


def _run(args):
    try:
        if args.command == "profiles":
            if getattr(args, "state", None) is not None:
                _print(_saved_profiles(args.state, getattr(args, "run_id", None)))
                return 0
            report = _local_profiles()
            _print(report)
            return int(not report["configured"])
        return _doctor(args) if args.command == "doctor" else _login(args)
    except (ConnectionError, ValueError) as exc:
        _print({"ready": False, "error": str(exc)})
    except FileNotFoundError:
        _print({"ready": False, "error": "Codex executable missing. Set GX_P0_CODEX to codex-cli 0.169.0."})
    except AppServerResponseError as exc:
        _print({"ready": False, "error": "Codex RPC failed", "rpc_code": exc.code})
    except (AppServerError, OSError, subprocess.TimeoutExpired) as exc:
        _print({"ready": False, "error": type(exc).__name__})
    return 1


def add_commands(subparsers):
    commands = {
        "login": "Use the existing ChatGPT login or start official device authorization",
        "login-status": "Show the local Codex login mode",
        "doctor": "Check Clash, Codex version, account type and the current model catalog",
        "profiles": "Show local provider configuration and role selections without starting Codex",
    }
    for name, help_text in commands.items():
        parser = subparsers.add_parser(name, help=help_text)
        parser.set_defaults(command=name, deployment_handler=_run)
        if name == "profiles":
            parser.add_argument("--run-id", help="Select a saved workflow run")
            parser.add_argument("--state", type=Path, help="Read pinned and observed profiles from a saved project")
        if name == "doctor":
            parser.add_argument("--role", help="Check one GX role's selected provider, model and effort")
            parser.add_argument("--smoke", action="store_true", help="Send one short inference request (xhigh by default)")
