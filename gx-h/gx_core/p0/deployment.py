"""Operator commands for the package's official Codex CLI."""

import json
import re
import subprocess
import tempfile

from ..hostobs.app_server_client import (
    AppServerClient, AppServerDisconnected, AppServerError, AppServerResponseError, AppServerTimeout,
)
from .profiles import CODEX_VERSION, app_server_argv, clash_env, codex_argv, effort_for, provider_name, role_profile


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _version(env):
    cmd = subprocess.run([codex_argv()[0], "--version"], env=env, capture_output=True, text=True, timeout=15)
    match = re.fullmatch(r"codex-cli ([^\s]+)", cmd.stdout.strip())
    return match.group(1) if cmd.returncode == 0 and match else None


def _login_state(env):
    result = subprocess.run([*codex_argv(), "login", "status"], env=env, capture_output=True, text=True, timeout=30)
    lines = (result.stdout + result.stderr).splitlines()
    if result.returncode == 0:
        return "chatgpt" if "Logged in using ChatGPT" in lines else "other_auth"
    return "signed_out" if "Not logged in" in lines else "unavailable"


def _login(args):
    env = clash_env()
    if provider_name() != "openai":
        _print({"error": "Select the OpenAI provider for ChatGPT login."})
        return 1
    if _version(env) != CODEX_VERSION:
        _print({"error": f"Select codex-cli {CODEX_VERSION} with GX_P0_CODEX."})
        return 1
    state = _login_state(env)
    if args.command == "login-status" or state in {"chatgpt", "unavailable"}:
        _print({"login": state, "chatgpt_oauth": state == "chatgpt"})
        return int(state != "chatgpt")
    # The operator receives OpenAI's one-time device instructions on their terminal.
    # All subsequent credential exchanges and storage belong to the official CLI.
    return subprocess.run([*codex_argv(), "login", "--device-auth"], env=env).returncode


def _models(client, provider):
    params = {"modelProvider": provider, "includeHidden": False, "limit": 100}
    result = {}
    while True:
        page = client.request("model/list", params)
        for model in page["data"]:
            if not model.get("hidden"):
                result[model["model"]] = [entry["reasoningEffort"] for entry in model["supportedReasoningEfforts"]]
        cursor = page.get("nextCursor")
        if not cursor:
            return result
        if cursor == params.get("cursor"):
            raise AppServerError("Model listing returned the same pagination cursor twice")
        params["cursor"] = cursor


def _smoke(client, cwd, provider, model):
    opened = client.request("thread/start", {
        "cwd": cwd, "model": model, "modelProvider": provider, "ephemeral": True,
        "approvalPolicy": "never", "sandbox": "read-only",
        "baseInstructions": "Follow the user request concisely.", "developerInstructions": "Use no tools.",
        "config": {"model_reasoning_effort": "xhigh", "features.goals": False},
    })
    thread_id = opened["thread"]["id"]
    result = {"thread_id": thread_id, "model": opened["model"], "provider": opened["modelProvider"],
              "effort": opened["reasoningEffort"], "matched": False}
    if (result["model"], result["provider"], result["effort"]) != (model, provider, "xhigh"):
        result["error"] = "Thread configuration differs from the requested model/provider/effort."
        return result
    started = client.request("turn/start", {"threadId": thread_id, "effort": "xhigh", "input": [
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
    env = clash_env()
    provider, model = provider_name(), role_profile("test")["model"]
    version = _version(env)
    report = {"codex_version": version, "expected_version": CODEX_VERSION, "provider": provider, "model": model,
              "http_proxy": env["HTTP_PROXY"], "socks_proxy": env["ALL_PROXY"], "no_proxy": env["NO_PROXY"]}
    if version != CODEX_VERSION:
        _print({**report, "ready": False, "error": "Select the pinned official Codex CLI with GX_P0_CODEX."})
        return 1
    with tempfile.TemporaryDirectory(prefix="gx-p0-doctor-") as cwd:
        with AppServerClient(app_server_argv(), cwd=cwd, env=env, default_timeout=45) as client:
            client.request("initialize", {"clientInfo": {"name": "gx_p0_doctor", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
            client.notify("initialized")
            account = client.request("account/read", {"refreshToken": False}).get("account")
            report["account_type"] = (account or {}).get("type")
            if provider == "openai" and report["account_type"] != "chatgpt":
                _print({**report, "ready": False, "error": "Run gx-p0 login to sign in with ChatGPT."})
                return 1
            report["models"] = _models(client, provider)
            required = {effort_for(role) for role in ("lead", "planning", "test")}
            report["required_efforts"] = sorted(required)
            missing = sorted(required - set(report["models"].get(model, [])))
            if missing:
                _print({**report, "ready": False, "missing_efforts": missing})
                return 1
            if args.smoke:
                report["inference"] = _smoke(client, cwd, provider, model)
    report["ready"] = report.get("inference", {}).get("matched", True)
    _print(report)
    return int(not report["ready"])


def _run(args):
    try:
        return _doctor(args) if args.command == "doctor" else _login(args)
    except ConnectionError as exc:
        _print({"ready": False, "error": str(exc)})
    except FileNotFoundError:
        _print({"ready": False, "error": "Codex executable missing. Set GX_P0_CODEX to codex-cli 0.169.0."})
    except AppServerResponseError as exc:
        _print({"ready": False, "error": "Codex RPC failed", "rpc_code": exc.code})
    except (AppServerError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _print({"ready": False, "error": type(exc).__name__})
    return 1


def add_commands(subparsers):
    commands = {
        "login": "Use the existing ChatGPT login or start official device authorization",
        "login-status": "Show the local Codex login mode",
        "doctor": "Check Clash, Codex version, account type and the current model catalog",
    }
    for name, help_text in commands.items():
        parser = subparsers.add_parser(name, help=help_text)
        parser.set_defaults(command=name, deployment_handler=_run)
        if name == "doctor":
            parser.add_argument("--smoke", action="store_true", help="Send one short xhigh inference request")
