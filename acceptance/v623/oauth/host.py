"""Stock Codex app-server, existing ChatGPT subscription, no Responses/API-key gateway.

The rate read is an account snapshot. It never serves as an invoice. Native role usage
is reconciled separately against the original Run. Account metadata operations do not
start a thread or a turn.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT)]
from acceptance.v623.live import deployment
from gx_core.hostobs.app_server_client import AppServerClient, AppServerError
from gx_core.p0 import profiles


class HostBlocked(RuntimeError):
    """A sanitized admission error, safe to record without credentials or server text."""


def _private(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise HostBlocked("PRIVATE_HOST_DIRECTORY_UNAVAILABLE")
    return path


def _state():
    return _private(Path.home() / ".local/state/gx-h-oauth")


def _account_store():
    # Stock Codex resolves an auth.json symlink for both reads and atomic refresh.
    # Do not read or serialize this file. Keep the link OUTSIDE any project, receipt or archive.
    source = (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json").resolve()
    if not source.is_file():
        raise HostBlocked("EXISTING_CHATGPT_AUTH_STORE_UNAVAILABLE")
    return source


def _proxy():
    env = profiles.clash_env()
    expected = {"HTTP_PROXY":"http://127.0.0.1:7890", "HTTPS_PROXY":"http://127.0.0.1:7890",
                "ALL_PROXY":"socks5h://127.0.0.1:7891"}
    if any(env.get(k) != v or env.get(k.lower()) != v for k, v in expected.items()):
        raise HostBlocked("CLASH_ROUTE_UNAVAILABLE")
    for key in ("NO_PROXY", "no_proxy"):
        if {x for x in env.get(key, "").split(",") if x} - {"localhost","127.0.0.1","::1"}:
            raise HostBlocked("CLASH_BYPASS_OUTSIDE_LOOPBACK")
    return {k: env[k] for k in (*expected, "NO_PROXY", *(name.lower() for name in expected), "no_proxy")}


def _pin(binary):
    binary = Path(binary).resolve(strict=True)
    locked = json.loads((ROOT / "release-assets/lock.json").read_text())["linux_x64"]["binary_sha256"]
    if not binary.is_file() or deployment._sha(binary) != locked:
        raise HostBlocked("OFFICIAL_BINARY_UNVERIFIED")
    return binary, locked


def _write(path, value):
    tmp = path.with_name("." + path.name + "." + secrets.token_hex(8))
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _env(home, binary, model, effort, *, sessions=None):
    _private(home)
    link = home / "auth.json"
    original = _account_store()
    if not link.is_symlink():
        if link.exists():
            raise HostBlocked("CONFLICTING_AUTH_STORE")
        link.symlink_to(original)
    if link.resolve() != original:
        raise HostBlocked("CONFLICTING_AUTH_STORE")
    if sessions is not None:
        sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = home / "sessions"
        if not target.is_symlink():
            if target.exists():
                raise HostBlocked("CONFLICTING_SESSION_STORE")
            target.symlink_to(sessions, target_is_directory=True)
        if target.resolve() != sessions.resolve():
            raise HostBlocked("CONFLICTING_SESSION_STORE")

    # Exclude inherited model/provider endpoints, API keys, Codex origins, model catalogs,
    # credit routes, project config, imported MCP providers and certificate overrides.
    config = (f'model = {json.dumps(model)}\nmodel_reasoning_effort = {json.dumps(effort)}\n'
              'model_provider = "openai"\nforced_login_method = "chatgpt"\n'
              'cli_auth_credentials_store = "file"\ncheck_for_update_on_startup = false\n'
              '[features]\nrespect_system_proxy = true\nsystem_proxy_fallback = false\n'
              '[analytics]\nenabled = false\n')
    config_path = home / "config.toml"
    if config_path.exists() and config_path.read_text() != config:
        raise HostBlocked("HOST_CONFIGURATION_MISMATCH")
    if not config_path.exists():
        config_path.write_text(config)
    profile = home / "gx-profile.json"
    _write(profile, {"default":{"provider":"openai","model":model,"effort":effort,"multi_agent":False}})
    env = _proxy()
    env.update(PATH=str(Path(sys.executable).parent) + os.pathsep + os.defpath,
               LANG="C.UTF-8", HOME=str(home), CODEX_HOME=str(home), GX_P0_CODEX=str(binary),
               GX_P0_PROFILE=str(profile), GX_P0_PROVIDER="openai", PYTHONDONTWRITEBYTECODE="1")
    return env


def _account_tag(identity):
    keyfile = _state() / "account-ref.key"
    if not keyfile.is_file():
        # Publish the whole secret atomically when two roles enroll simultaneously.
        tmp = keyfile.with_name(".account-ref-" + secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
            try:
                os.link(tmp, keyfile)
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
    key = keyfile.read_bytes()
    if len(key) != 32:
        raise HostBlocked("ACCOUNT_REFERENCE_UNAVAILABLE")
    return hmac.digest(key, identity.encode(), "sha256").hex()


def _rates(raw, model):
    """Inspect the stock account endpoint. Never call a purchase, reserve or upsell RPC."""
    reasons = []
    primary = raw.get("rateLimits") or {}
    grouped = raw.get("rateLimitsByLimitId") or {}
    buckets = [primary] if primary else []
    buckets.extend(b for name, b in grouped.items() if b and b not in buckets and
                   (name in ("codex", model) or b.get("normalModelSlug") == model))
    included = raw.get("ordinaryUsageAllowed")
    if not buckets:
        reasons.append("RATE_SNAPSHOT_UNAVAILABLE")
    if included is False:
        reasons.append("INCLUDED_USAGE_DISABLED")
    elif included is not True:
        # The native contract defines null as unavailable, never infer permission from window resets.
        reasons.append("INCLUDED_USAGE_UNKNOWN")
    if raw.get("rateLimitUpsell") and included is not True:
        reasons.append("SUBSCRIPTION_UPSELL_PRESENT")
    snapshots = []
    for item in buckets:
        windows = {}
        for name in ("primary","secondary"):
            window = item.get(name)
            if window:
                percent = window.get("usedPercent")
                if type(percent) not in (int,float) or not 0 <= percent <= 100:
                    reasons.append("RATE_SNAPSHOT_UNAVAILABLE")
                elif percent >= 100:
                    reasons.append("INCLUDED_RATE_LIMIT_REACHED")
                windows[name] = {"used_percent":percent, "resets_at":window.get("resetsAt")}
        if not windows and included is not True:
            reasons.append("INCLUDED_USAGE_UNKNOWN")
        limit_type = item.get("rateLimitReachedType")
        if isinstance(limit_type, str) and "creditsdepleted" in limit_type.lower():
            reasons.append("ADDITIONAL_CREDITS_REQUIRED")
        if item.get("spendControlReached") or limit_type:
            reasons.append("PROVIDER_LIMIT_REACHED")
        credits = item.get("credits")
        spend = item.get("individualLimit")
        credit_available = False
        if credits:
            try:
                balance = Decimal(credits["balance"]) if credits.get("balance") is not None else Decimal(0)
                if not balance.is_finite():
                    raise InvalidOperation()
            except (InvalidOperation, TypeError, ValueError):
                reasons.append("CREDIT_ROUTE_UNVERIFIED")
            else:
                credit_available = bool(credits.get("hasCredits") or credits.get("unlimited") or balance > 0)
        if credit_available:
            reasons.append("ADDITIONAL_CREDIT_ROUTE_AVAILABLE")
        if spend and spend.get("remainingPercent") == 0:
            reasons.append("PROVIDER_LIMIT_REACHED")
        snapshots.append({"windows":windows, "credits_reported":credits is not None,
                          "additional_credits_available":credit_available, "spend_control_reached":item.get("spendControlReached"),
                          "limit_type":item.get("rateLimitReachedType")})
    return sorted(set(reasons)), {"ordinary_usage_allowed":included, "upsell_present":bool(raw.get("rateLimitUpsell")), "limits":snapshots}


def _inspect(binary, pin, model, effort, env, cwd):
    result = dict(schema="gx.oauth.account/1", provider="openai", origin="https://chatgpt.com/backend-api/codex",
                  model=model, effort=effort, binary_sha256=pin, proxy="Clash", inference_calls=0,
                  actual_provider_usd=None, turn_starts=0, thread_starts=0, rpc_methods=[], checked_at=datetime.now(timezone.utc).isoformat())
    argv = [str(binary), *profiles.app_server_argv(provider="openai", test_mode=False)[1:]]
    codes = []
    try:
        with AppServerClient(argv, cwd=cwd, env=env, default_timeout=30) as client:
            def read(method, params):
                result["rpc_methods"].append(method)
                return client.request(method, params)
            init = read("initialize", {"clientInfo":{"name":"gx_oauth_account","version":"1"},
                                       "capabilities":{"experimentalApi":True}})
            client.notify("initialized")
            result["host"] = init["userAgent"].split(" (")[0]
            cfg = read("config/read", {"cwd":str(cwd),"includeLayers":False})["config"]
            if not deployment._allowed_config(cfg) or cfg.get("forced_login_method") != "chatgpt":
                raise HostBlocked("OFFICIAL_OAUTH_ROUTE_UNVERIFIED")
            if cfg.get("model") != model or cfg.get("model_reasoning_effort") != effort:
                raise HostBlocked("MODEL_CONFIGURATION_MISMATCH")
            acc = read("account/read", {"refreshToken":False}).get("account") or {}
            if acc.get("type") != "chatgpt":
                raise HostBlocked("CHATGPT_OAUTH_REQUIRED")
            result["auth_type"], result["plan"] = "chatgpt", acc.get("planType")
            rates = read("account/rateLimits/read", {})
            identity = rates.get("accountId") or acc.get("email")
            if not identity:
                raise HostBlocked("ACCOUNT_REFERENCE_UNAVAILABLE")
            result["account_ref"] = _account_tag(identity)
            codes, result["rate_snapshot"] = _rates(rates, model)
            class Catalog:
                request = staticmethod(read)
            if effort not in profiles.model_catalog(Catalog, "openai").get(model, ()):
                codes.append("MODEL_EFFORT_UNAVAILABLE")
    except HostBlocked as error:
        codes.append(str(error))
    except (AppServerError, OSError, TimeoutError, KeyError, TypeError, ValueError) as error:
        # Do not serialize provider diagnostics (may contain personal identifiers or bearer material).
        codes.append("ACCOUNT_METADATA_UNAVAILABLE")
        result["error_type"] = type(error).__name__
    state = "INCLUDED_AVAILABLE"
    if codes:
        if any(x in codes for x in ("INCLUDED_USAGE_DISABLED","ADDITIONAL_CREDIT_ROUTE_AVAILABLE","ADDITIONAL_CREDITS_REQUIRED","SUBSCRIPTION_UPSELL_PRESENT")):
            state = "ADDITIONAL_CHARGE"
        elif any(x in codes for x in ("INCLUDED_RATE_LIMIT_REACHED","PROVIDER_LIMIT_REACHED")):
            state = "RATE_LIMITED"
        else:
            state = "UNKNOWN"
    return {**result, "subscription_usage_state":state, "status":"BLOCKED" if codes else "READY",
            "reason_codes":sorted(set(codes))}


def preflight(binary, model, effort):
    """Free, isolated official control-plane reads. No attempt to create a slot or Run."""
    try:
        binary, pin = _pin(binary)
        with TemporaryDirectory(prefix="oauth-preflight-", dir=_state()) as tmp:
            tmp = Path(tmp)
            home = tmp / "home"
            return _inspect(binary, pin, model, effort, _env(home,binary,model,effort), tmp)
    except (OSError, ValueError, HostBlocked) as exc:
        return {"status":"BLOCKED", "reason_codes":[str(exc) if isinstance(exc,HostBlocked) else type(exc).__name__],
                "subscription_usage_state":"UNKNOWN", "inference_calls":0, "actual_provider_usd":None}


@contextmanager
def session(slot, binary, reg):
    slot = Path(slot).resolve()
    binary, pin = _pin(binary)
    model, effort = reg["model"], reg.get("effort") or reg["model_effort"]
    private = _private(_state() / ("session-" + hashlib.sha256(str(slot).encode()).hexdigest()))
    env = _env(private,binary,model,effort,sessions=slot/"codex/sessions")
    control = _private(private / "control")
    path = slot / "oauth-account.json"
    before = _inspect(binary,pin,model,effort,env,control)
    prior = json.loads(path.read_text()) if path.is_file() else {}
    bindings = {ref for ref in (reg.get("account_ref"), prior.get("account_ref")) if ref}
    if bindings and bindings != {before.get("account_ref")}:
        before.update(status="BLOCKED", reason_codes=["SUBSCRIPTION_ACCOUNT_CHANGED"])
    if before["status"] != "READY":
        # Keep an earlier account binding intact through transient account-read failures.
        _write(slot/"oauth-account-blocked.json",before)
        # Usage reconciliation reads the latest admission result even when no new turn was sent.
        # Retain the original account binding through a failed or inconsistent read.
        recorded = {**before, "account_ref": prior.get("account_ref") or reg.get("account_ref") or before.get("account_ref")}
        _write(path,recorded)
        raise HostBlocked(",".join(before["reason_codes"]))
    _write(path,before)
    try:
        yield env
    finally:
        after = _inspect(binary,pin,model,effort,env,control)
        if before["account_ref"] != after.get("account_ref"):
            after.update(status="BLOCKED",reason_codes=sorted(set(after["reason_codes"] + ["SUBSCRIPTION_ACCOUNT_CHANGED"])))
        _write(path,{**after, "account_ref":before["account_ref"], "admission": before})
        if after["status"] != "READY" and sys.exc_info()[0] is None:
            raise HostBlocked(",".join(after["reason_codes"]))


def reconcile(slot, run_id):
    from acceptance.v623.oauth.usage import reconcile as original_usage
    return original_usage(slot, run_id)
