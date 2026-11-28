"""Zero-inference deployed-host and exact public-series funding preflight."""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
from types import MappingProxyType, ModuleType
from urllib.parse import urlsplit
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT)]
from gx_core.hostobs.app_server_client import AppServerClient, AppServerError
from gx_core.p0 import profiles
from acceptance.v622.public.funding import FundingBlocked

# A system/deployment operator owns these paths; applicant-controlled paths never select an issuer.
PI_DIRECTORY = Path("/etc/gx-h/approved-series")
PI_SIGNERS = Path("/etc/gx-h/live-pi.allowed_signers")
METER_DIRECTORY = Path("/etc/gx-h/live")
SCHEME = "gx.series-spending/1"
PUBLIC = "GX-V62C-PUBLIC-COVERAGE-LIVE-01"
CAPS = (1_000_000, 4_000_000)


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _production_origin(value):
    if not isinstance(value, str):
        return False
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment
            or p.path not in {"", "/"} or "." not in p.hostname
            or p.hostname.rstrip(".").lower().endswith((".localhost", ".local", ".test", ".invalid"))):
        return False
    try:
        return ipaddress.ip_address(p.hostname).is_global
    except ValueError:
        return True


def _installed_binding():
    """Load only code pinned and installed by the deployment administrator, separately from any applicant grant."""
    path = METER_DIRECTORY / "owner.py"
    manifest = METER_DIRECTORY / "binding.json"
    contract = METER_DIRECTORY / "provider-contract"
    if not all(f.is_file() and _owned(f) for f in (path, manifest, contract)):
        raise FundingBlocked("HARD_CAP_UNAVAILABLE: stock Codex has no installed and verified priced-send binding")
    raw = path.read_bytes()
    spec = json.loads(manifest.read_bytes())
    if (not isinstance(spec, dict) or spec.get("schema") != "gx.priced-responses/1"
            or spec.get("adapter_sha256") != hashlib.sha256(raw).hexdigest()
            or spec.get("contract_sha256") != _sha(contract)
            or not all(isinstance(spec.get(k), str) and spec[k] for k in ("deployment_id", "contract_id", "account_ref"))
            or not _production_origin(spec.get("provider_origin"))):
        raise FundingBlocked("HARD_CAP_UNAVAILABLE: deployed adapter, billing identity or contract pin mismatch")
    # This is installed deployment code (like a provisioned provider plugin). No applicant data is executable.
    module = ModuleType("gx_operator_priced_responses")
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module.open_owner, spec


class VerifiedOwner:
    """An installed transport and its authenticated, nonbillable deployment receipt."""
    def __init__(self, owner, proof):
        self._owner = owner
        self.proof = proof

    def __getattr__(self, name):
        return getattr(self._owner, name)


def open_deployment():
    """Resolve the operator-installed provider owner and authenticate its current billing contract for this session."""
    try:
        factory, spec = _installed_binding()
        env = profiles.clash_env(native_http=True)
        proxies = {k: env[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        if (proxies["HTTP_PROXY"], proxies["HTTPS_PROXY"], proxies["ALL_PROXY"]) != (
                "http://127.0.0.1:7890", "http://127.0.0.1:7890", "socks5h://127.0.0.1:7891"):
            raise FundingBlocked("HARD_CAP_UNAVAILABLE: required provider network route unavailable")
        owner = factory(proxies=proxies)
        if (owner.source != "PRODUCTION" or (owner.deployment_id, owner.contract_id) !=
                (spec["deployment_id"], spec["contract_id"]) or not all(callable(getattr(owner, name, None))
                for name in ("deployment_receipt", "model_catalog", "quote", "send", "lookup"))):
            raise FundingBlocked("HARD_CAP_UNAVAILABLE: installed owner is not the registered production metered transport")
        # This is a free authenticated control-plane read by the same trusted adapter that sends/looks up
        # each paid request. OAuth logins, caller-supplied metadata and local quote-only files cannot supply it.
        proof = owner.deployment_receipt()
        required = ("provider_origin", "contract_sha256", "deployment_id", "contract_id", "account_ref")
        if (not isinstance(proof, dict) or proof.get("verified") is not True or
                any(proof.get(k) != spec[k] for k in required)
                or proof.get("all_billable_tokens_bounded") is not True
                or proof.get("free_lookup_by_original_request_id") is not True
                or not isinstance(proof.get("provider_evidence_ref"), str) or not proof["provider_evidence_ref"]):
            raise FundingBlocked("HARD_CAP_UNAVAILABLE: provider did not attest the installed exact account and contract")
        return VerifiedOwner(owner, proof)
    except FundingBlocked:
        raise
    except Exception as exc:
        # Provider errors and response bodies may contain credentials; publish only the error type.
        raise FundingBlocked("HARD_CAP_UNAVAILABLE: provider binding " + type(exc).__name__) from None


def _owned(path):
    if not path.is_absolute() or path.resolve() != path:
        return False
    for part in (path, *path.parents):
        st = part.stat()
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
    return path.is_file()


class VerifiedApproval(dict):
    """JSON-exportable public receipt. Only an in-process result of issuer verification enables a new send."""
    def __init__(self, fields):
        super().__init__(fields)
        self.checked = MappingProxyType(dict(fields))


def _approval(*, series, candidate, protocol_sha256, model, effort, approval=None, authority=None):
    result = {"status": "AUTHORIZATION_MISSING", "issuer_verified": False, "covers_series": False}
    if series != PUBLIC or not re.fullmatch("[0-9a-f]{40}", str(candidate)) or not re.fullmatch("[0-9a-f]{64}", str(protocol_sha256)):
        return {**result, "detail": "Series, candidate and frozen protocol must be pinned before funding"}
    installed = PI_DIRECTORY / (series + ".json")
    path = Path(approval).absolute() if isinstance(approval, (str, Path)) else installed
    if approval is not None and not isinstance(approval, (str, Path)):
        return {**result, "detail": "Operator source or detached signature required"}
    if not path.is_file():
        return {**result, "detail": "No external exact-series approval found"}
    try:
        raw = path.read_bytes()
        if path == installed and _owned(installed):
            issuer = "deployment-operator approved-series store"
        else:
            if (authority is not None and Path(authority).absolute() != PI_SIGNERS) or not PI_SIGNERS.is_file() or not _owned(PI_SIGNERS):
                return {**result, "detail": "No installed external issuer for this approval"}
            signature = path.with_name(path.name + ".sig")
            verify = ["ssh-keygen", "-Y", "verify", "-f", str(PI_SIGNERS), "-I", "gx-live-pi",
                      "-n", "gx-series-spending", "-s", str(signature)]
            if not signature.is_file() or subprocess.run(verify, input=raw, capture_output=True, timeout=10).returncode:
                return {**result, "detail": "Unverified operator signature"}
            issuer = "registered external PI signer"
        doc = json.loads(raw)
        wanted = dict(schema=SCHEME, execution_authorized=True, series=series, candidate=candidate,
                      protocol_sha256=protocol_sha256, model=model, effort=effort, cost_mode="HARD_TOKEN_CAP",
                      tokens_per_slot=CAPS[0], tokens_whole_batch=CAPS[1], all_physical_requests=True,
                      paid_judges=False)
        if not isinstance(doc, dict) or any(type(doc.get(k)) is not type(v) or doc[k] != v for k, v in wanted.items()):
            return {**result, "issuer_verified": True, "detail": "Grant does not cover the exact series, scope, pricing mode and limits"}
        for key in ("approval_ref", "deployment_id", "contract_id", "account_ref"):
            if not isinstance(doc.get(key), str) or not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,180}", doc[key]):
                return {**result, "issuer_verified": True, "detail": "Missing approval, billing deployment, account or cost contract reference"}
        expiry = datetime.fromisoformat(doc["expires_utc"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            return {**result, "issuer_verified": True, "detail": "Approval is expired or lacks explicit time zone"}
        return VerifiedApproval({"status": "VERIFIED", "issuer_verified": True, "covers_series": True, "issuer": issuer,
                "approval_sha256": hashlib.sha256(raw).hexdigest(), **{k: doc[k] for k in (
                    "series", "candidate", "protocol_sha256", "model", "effort", "cost_mode",
                    "tokens_per_slot", "tokens_whole_batch", "approval_ref", "deployment_id", "contract_id",
                    "account_ref", "expires_utc")}})
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return {**result, "detail": "Unreadable or invalid external approval"}


def contract_evidence():
    pin = json.loads((ROOT / "release-assets/lock.json").read_text())
    base = ROOT / "vendor/codex/codex-rs"
    files = {"turn": "app-server-protocol/schema/json/v2/TurnStartParams.json",
             "responses": "codex-api/src/common.rs", "provider": "model-provider-info/src/lib.rs",
             "sse": "codex-api/src/sse/responses.rs", "retry": "core/src/responses_retry.rs"}
    result = {"source_commit": pin["source"]["commit"], "release_archive_sha256": pin["source"]["sha256"],
              "known_gaps": ["No total billable token cap in the pinned native turn or Responses request",
                             "A turn may cause multiple physical sends including retries and tool follow-ups",
                             "Reported cache/input/output/reasoning usage is retrospective",
                             "No verified official request-bound quote or free lookup by original GX request ID"]}
    if any(not (base / f).is_file() for f in files.values()):
        return {**result, "source_files": {}, "inspection": "PINNED_SOURCE_UNAVAILABLE"}
    archive = ROOT / "release-assets" / pin["source"]["file"]
    if not archive.is_file() or _sha(archive) != pin["source"]["sha256"]:
        return {**result, "source_files": {}, "inspection": "SOURCE_ARCHIVE_UNVERIFIED"}
    wanted = {"codex-rs/" + v: k for k, v in files.items()}
    archived = {}
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            if member.name in wanted and member.isfile():
                archived[wanted[member.name]] = hashlib.sha256(bundle.extractfile(member).read()).hexdigest()
    if archived != {k: _sha(base / v) for k, v in files.items()}:
        return {**result, "source_files": {}, "inspection": "CHECKOUT_AND_ARCHIVE_DIFFER"}
    wire = (base / files["responses"]).read_text()
    wire_fields = re.findall(r"pub (\w+):", wire.split("pub struct ResponsesApiRequest {", 1)[1].split("\n}", 1)[0])
    turn = json.loads((base / files["turn"]).read_text())
    return {**result, "inspection": "PINNED_LOCAL_SCHEMA_AND_RUST_SOURCE",
            "source_files": {k: {"path": v, "sha256": _sha(base / v)} for k, v in files.items()},
            "turn_fields": sorted(turn["properties"]), "responses_fields": wire_fields}


def _allowed_config(config):
    allowed = {"https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/codex"}
    # A named provider can be redirected with top-level or per-provider URL/auth/catalog options.
    info = (config.get("model_providers") or {}).get("openai") or {}
    urls = [config.get(k) for k in ("openai_base_url", "chatgpt_base_url")]
    urls += [info.get(k) for k in ("base_url", "chatgpt_base_url")]
    return (config.get("model_provider", "openai") in {None, "openai"} and
            not any(config.get(k) for k in ("model_catalog_json", "experimental_realtime_ws_base_url")) and
            not any(info.get(k) for k in ("auth", "env_key", "experimental_bearer_token", "http_headers", "env_http_headers")) and
            all(not url or isinstance(url, str) and url.rstrip("/") in allowed for url in urls))


def inspect_account(binary, model, effort):
    # Only the official CLI sees stored authentication. Never serialize the raw account, config or catalog.
    binary = Path(binary)
    pin = json.loads((ROOT / "release-assets/lock.json").read_text())
    proof = {"requested_provider": "openai", "model": model, "effort": effort, "inference": "NOT_RUN",
             "rpc_methods": [], "thread_starts": 0, "turn_starts": 0, "provider_inference_requests": 0,
             "authenticated_inference_identity": None, "actual_provider_usd": None}
    if not binary.is_file() or _sha(binary) != pin["linux_x64"]["binary_sha256"]:
        return {**proof, "status": "OFFICIAL_BINARY_UNVERIFIED"}
    proof["binary_sha256"] = pin["linux_x64"]["binary_sha256"]
    try:
        proxies = profiles.proxy_settings()
        expected = {"HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890",
                    "ALL_PROXY": "socks5h://127.0.0.1:7891"}
        if any(proxies.get(k) != v for k, v in expected.items()):
            return {**proof, "status": "DEPLOYMENT_PROXY_UNAVAILABLE"}
        env = profiles.clash_env(native_http=True)
        if any(env.get(k) for k in ("CODEX_APP_SERVER_CHATGPT_BASE_URL", "OPENAI_BASE_URL",
                                    "OPENAI_API_BASE", "CODEX_HOME_OVERRIDE")) or not _allowed_config(profiles.codex_config()):
            return {**proof, "status": "PROVIDER_ROUTE_UNVERIFIED"}
        args = [str(binary), *profiles.app_server_argv(provider="openai", test_mode=False)[1:]]
        with TemporaryDirectory(prefix="gx-live-metadata-") as cwd, AppServerClient(args, cwd=cwd, env=env, default_timeout=15) as client:
            def read(method, params):
                proof["rpc_methods"].append(method)
                return client.request(method, params)
            identity = read("initialize", {"clientInfo": {"name": "gx_live_metadata", "version": "1"},
                                           "capabilities": {"experimentalApi": True}})
            client.notify("initialized")
            proof["rpc_methods"].append("initialized")
            proof["user_agent"] = identity["userAgent"].split(" (")[0]
            config = read("config/read", {"cwd": cwd, "includeLayers": False})["config"]
            if not _allowed_config(config):
                return {**proof, "status": "PROVIDER_ROUTE_UNVERIFIED"}
            account = read("account/read", {"refreshToken": False}).get("account") or {}
            proof["login_mode"] = account.get("type") if account.get("type") in {"chatgpt", "apiKey"} else None
            if proof["login_mode"] != "chatgpt":
                return {**proof, "status": "OFFICIAL_OAUTH_UNAVAILABLE"}
            # model/list is read-only, uses a native cache or the configured Clash proxy for catalog metadata.
            class CatalogReader:
                request = staticmethod(read)
            proof["model_effort_advertised"] = effort in profiles.model_catalog(CatalogReader, "openai").get(model, ())
        return {**proof, "status": "OFFICIAL_LOGIN_AND_CATALOG" if proof["model_effort_advertised"] else "MODEL_UNAVAILABLE",
                "configured_endpoint": "https://chatgpt.com/backend-api/codex", "proxy": "Clash",
                "evidence_scope": "native local OAuth mode and provider catalog; catalog may be cached"}
    except (OSError, AppServerError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        return {**proof, "status": "PROVIDER_METADATA_UNAVAILABLE", "error_type": type(exc).__name__}


def preflight(*, series, candidate, protocol_sha256, binary, model="gpt-6.2-astra", effort="max",
              approval=None, authority=None, probe_host=True):
    grant = _approval(series=series, candidate=candidate, protocol_sha256=protocol_sha256, model=model,
                      effort=effort, approval=approval, authority=authority)
    provider = inspect_account(binary, model, effort) if probe_host else {"status": "METADATA_NOT_PROBED"}
    enforcement = {"status": "HARD_CAP_UNAVAILABLE", "all_inclusive_maximum": None,
                   "evidence": contract_evidence(), "deployment_id": None, "contract_id": None}
    try:
        owner = open_deployment()
    except FundingBlocked:
        owner = None
    reasons = [] if grant["covers_series"] else ["AUTHORIZATION_MISSING"]
    if owner is None:
        reasons.append("HARD_CAP_UNAVAILABLE")
        if provider["status"] not in {"OFFICIAL_LOGIN_AND_CATALOG", "METADATA_NOT_PROBED"}:
            reasons.append(provider["status"])
    else:
        # Only an installed priced-send integration can supply these. It authenticates its billing account
        # and its server-enforced bound on the same connection used for all quote/send/lookup operations.
        evidence = owner.proof
        if (owner.source != "PRODUCTION" or not evidence.get("verified") or
                (evidence.get("deployment_id"), evidence.get("contract_id")) != (owner.deployment_id, owner.contract_id)
                or not evidence.get("account_ref") or evidence.get("model") != model or effort not in evidence.get("efforts", ())):
            reasons.append("PROVIDER_DEPLOYMENT_UNVERIFIED")
        elif grant["covers_series"] and any(grant[k] != evidence[k] for k in ("deployment_id", "contract_id", "account_ref")):
            reasons.append("AUTHORIZATION_DEPLOYMENT_MISMATCH")
        else:
            enforcement = {"status": "VERIFIED_DEPLOYMENT", **{k: evidence[k] for k in
                           ("deployment_id", "contract_id", "account_ref", "provider_origin", "contract_sha256", "provider_evidence_ref")}}
    return {"ready": not reasons, "status": "READY" if not reasons else "BLOCKED", "reason_codes": reasons,
            "series": series, "candidate": candidate, "protocol_sha256": protocol_sha256, "provider": provider,
            "authorization": grant, "enforcement": enforcement, "paid_requests": 0, "actual_provider_usd": None}
