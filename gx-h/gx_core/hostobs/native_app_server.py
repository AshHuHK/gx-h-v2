"""Thin official App Server -> existing GX dispatcher router.

Codex owns model turns and native agents. This router starts the requested Root
turn, serves its callbacks and optionally continues from the GX project's next
actions on the same connection. GX business owners retain the project state.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import time
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from .. import api, config, contracts, metrics, project_messages
from ..context import Principal, SCOPE_OPERATOR
from ..domain import ids
from ..errors import GxError, ModelPolicyViolation, ScopeDenied
from ..native_release import NativeState, RELEASE_ID, write_native_bytes
from . import host_policy, native_context as native, native_spawn
from .public_output import PublicOutputCollector
from .session_receipts import (CALLBACK_LIMIT_MESSAGE, FACADE_EVENTS, WORKER_CLOSE_EFFECT_MESSAGE,
    host_process_group, known_effect_receipts, recoverable_facade_crash)
from .app_server_client import AppServerClient, AppServerDeliveryPaused, AppServerError, AppServerResponseError, AppServerTimeout, ReceivedMessage

# These business tools run through the local GX dispatcher on native callbacks.
BUSINESS_TOOLS = frozenset({
    "gx_run_start", "gx_run_status", "gx_next_actions", "gx_run_resume", "gx_run_control",
    "gx_coordinator_claim", "gx_shared_board", "gx_task_claim", "gx_task_bind", "gx_task_submit",
    "gx_bundle_check", "gx_artifact_put", "gx_artifact_get", "gx_team_manage", "gx_team_message",
    "gx_team_status", "gx_memory_search", "gx_memory_read",
})
TOOL_PROFILES = {
    "native_core": BUSINESS_TOOLS,
    "native_v2": (BUSINESS_TOOLS - {"gx_task_claim", "gx_task_bind", "gx_task_submit"})
                 | {"gx_task_acquire", "gx_task_finish", "gx_model_activity"},
}
REQUIRED_TOOLS = frozenset({"gx_run_start", "gx_run_status", "gx_next_actions", "gx_run_resume",
    "gx_task_claim", "gx_task_bind", "gx_task_submit", "gx_bundle_check", "gx_artifact_put",
    "gx_artifact_get", "gx_team_manage", "gx_team_message", "gx_team_status"})
SESSION_FILE = "native-session.json"
INBOX_POLL_SECONDS = 2
ROOT_SKILLS = ("native/gx-native-run", "native/gx-native-review")


def _root_skill_inputs(*, tool_profile: str = "native_core") -> tuple[list[dict[str, Any]], str]:
    """Deliver the installed canonical procedures before constructing a Host.

    These paths are not model/caller options. Record actual delivered identity;
    ordinary guidance needs no separate prose hash pin or activation certificate.
    """
    root = contracts.HARNESS_ROOT
    metadata, procedures = [], []
    for package in ROOT_SKILLS:
        path = root / ".agents/skills" / package / "SKILL.md"
        name = path.parent.name
        try:
            for parent in path.parents[:4]:
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ValueError("non-directory or symlink")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 64 * 1024:
                    raise ValueError("missing or unbounded procedure")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    body = stream.read(64 * 1024 + 1)
            finally:
                os.close(fd)
            text = body.decode("utf-8")
            frontmatter = text.split("---", 2)
            if (len(body) > 64 * 1024
                    or len(frontmatter) != 3 or frontmatter[0] != ""
                    or ("name: " + name) not in frontmatter[1].splitlines()
                    or not frontmatter[2].strip()):
                raise ValueError("procedure source is incomplete or misidentified")
        except (OSError, ValueError, UnicodeError) as exc:
            raise ScopeDenied("a required installed Root procedure is missing, unsafe or incomplete") from exc
        metadata.append({"name": name, "path": str(path), "release_id": RELEASE_ID,
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        procedures.append(f"Canonical procedure {name} ({path}):\n{text}")
    guidance = ("Complete shipped procedures follow as ordinary task guidance. This text is not "
        "Core Skill activation and does not replace the complete frozen attempt brief.\n\n"
        + "\n\n".join(procedures))
    return metadata, guidance


_OUTPUT_URL = re.compile(r"[a-z][a-z0-9+.-]{0,31}(?:://|%(?:25)*3a%(?:25)*2f%(?:25)*2f)[^\s<>\"']+", re.IGNORECASE)
_CREDENTIAL_QUERY_KEYS = frozenset({"key", "api_key", "apikey", "token", "access_token", "refresh_token",
    "authorization", "auth", "password", "passwd", "secret", "client_secret", "signature", "sig"})


def _screen_root_output(text: str) -> tuple[str, int, int]:
    """Plain user output; preserve public citations, never fetch or execute URLs."""
    from ..research import screening
    withheld = 0
    def safe_url(match):
        nonlocal withheld
        token = match.group(0)
        decoded = token
        # A bounded decode also catches percent-encoded credential-bearing URLs.
        for _ in range(3):
            updated = unquote(decoded)
            if updated == decoded:
                break
            decoded = updated
        try:
            parsed = urlsplit(decoded)
            query_parts = (parsed.query, parsed.fragment.partition("?")[2] or parsed.fragment)
            unsafe = (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or any(ord(char) < 32 for char in decoded)
                or any(key.lower() in _CREDENTIAL_QUERY_KEYS for query in query_parts
                       for key, _ in parse_qsl(query, keep_blank_values=True))
                or bool(screening.screen_matches(decoded, lists=("credentials",))))
            parsed.port  # Malformed ports are not published as safe citations.
        except (ValueError, UnicodeError):
            unsafe = True
        if unsafe:
            withheld += 1
            return "[withheld: credential URL]"
        return token
    text = _OUTPUT_URL.sub(safe_url, text)
    screened, counts = screening.redact(text, lists=("credentials",))
    return screened, sum(counts.values()), withheld


def _session_descriptor(state: NativeState, project_ref: str, policy_sha: str,
                        catalog_sha: str, native_execution: Mapping | None = None, *,
                        tool_profile: str = "native_core", catalog_without_files: str | None = None) -> dict[str, Any] | None:
    path = state.directory / SESSION_FILE
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ScopeDenied("the owned native session descriptor is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(64 * 1024 + 1)
        doc = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ScopeDenied("the owned native session descriptor is unreadable") from exc
    finally:
        os.close(fd)
    expected = {"schema": "gx.native_session/1", "source": native.SOURCE,
        "release_id": state.release_id,
        "project_ref": project_ref, "policy_sha256": policy_sha,
        "host_version": "0.169.0",
        "tool_profile": tool_profile}
    if not isinstance(doc, dict) or any(doc.get(k) != v for k, v in expected.items()):
        raise ScopeDenied("the owned native session pins/project/source changed; no automatic rebind")
    if doc.get("native_execution") != native_execution:
        raise ScopeDenied("the owned native session execution profile changed; no automatic permission change")
    if (not isinstance(doc.get("root_thread_id"), str) or not doc["root_thread_id"]
            or doc.get("session_id") != doc["root_thread_id"]
            or type(doc.get("uncertain")) is not bool
            or type(doc.get("cleanup_pending")) is not bool
            or not isinstance(doc.get("pending_effects"), list) or len(doc["pending_effects"]) > 128
            or any(not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k]
                for k in ("thread_id", "call_id", "tool", "arguments_sha256")) for item in doc["pending_effects"])):
        raise ScopeDenied("the owned native session identity/outcome descriptor is invalid")
    if doc["cleanup_pending"] and not recoverable_facade_crash(state.directory, doc):
        raise ScopeDenied("the previous owned Host process cleanup is unconfirmed; do not start another process")
    installed_catalog = doc.get("installed_catalog_sha256", doc.get("catalog_sha256"))
    if any(not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in (doc.get("contract_sha256"), doc.get("catalog_sha256"), installed_catalog)):
        raise ScopeDenied("the owned native session API identity is invalid")
    if ((doc["contract_sha256"] != state.contract_sha256 or installed_catalog not in (catalog_sha, catalog_without_files))
            and doc["pending_effects"]):
        raise ScopeDenied("settle the previous UNKNOWN effects before changing its native API contract/catalog")
    return doc


def _save_descriptor(state: NativeState, doc: Mapping[str, Any]) -> None:
    path = state.directory / SESSION_FILE
    if path.is_symlink():
        raise ScopeDenied("the owned native session descriptor cannot be a symlink")
    encoded = (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(encoded) > 64 * 1024:
        raise ScopeDenied("the bounded native outcome descriptor is exhausted")
    write_native_bytes(path, encoded)


def dynamic_tools(allowed_tools=None, *, profile: str = "native_core") -> list[dict[str, Any]]:
    """Descriptors come from the one existing API/contract, not a new tool DSL."""
    if not isinstance(profile, str) or profile not in TOOL_PROFILES:
        raise ScopeDenied("the native launch tool profile is unknown")
    business_tools = TOOL_PROFILES[profile]
    required = REQUIRED_TOOLS if profile == "native_core" else business_tools
    if allowed_tools is not None:
        required = frozenset(allowed_tools)
    selected = business_tools if allowed_tools is None else business_tools & required
    catalog = api.list_tools(Principal(subject="native-catalog", scope=SCOPE_OPERATOR,
                                      project_refs=(), source=native.OBSERVATION_SOURCE), names=selected)
    result = [{"type": "function", "name": tool["name"], "description": tool["description"],
               "inputSchema": tool["inputSchema"]} for tool in catalog
              if tool["bound"] and not contracts.any_tool_spec(tool["name"]).get("requires_user_interaction")]
    missing = required.difference(tool["name"] for tool in result)
    if missing:
        raise ScopeDenied("required GX business tools are unavailable for this native launch",
                          details={"missing_tools": sorted(missing)})
    return result


def _tool_policy(value: Mapping | None) -> dict[str, Any]:
    # Internal launch configuration only; never read from model tool arguments.
    if value is None:
        raise ScopeDenied("the native launch requires an explicit bounded tool policy")
    if not isinstance(value, Mapping) or set(value) - {"allowed_tools", "max_callbacks", "allow_mutations", "tool_profile"}:
        raise ScopeDenied("the native launch tool policy has an invalid shape")
    profile = value.get("tool_profile", "native_core")
    if not isinstance(profile, str) or profile not in TOOL_PROFILES:
        raise ScopeDenied("the native launch tool profile is unknown")
    if type(value.get("max_callbacks")) is not int or value["max_callbacks"] < 1 or type(value.get("allow_mutations")) is not bool:
        raise ScopeDenied("the native launch needs an exact positive callback bound and mutation permission")
    allowed = value.get("allowed_tools")
    if "allowed_tools" in value and (not isinstance(allowed, list) or not allowed
            or any(not isinstance(name, str) or not name for name in allowed) or len(set(allowed)) != len(allowed)):
        raise ScopeDenied("the native launch tool whitelist must be a nonempty unique list of names")
    return {"allowed_tools": frozenset(allowed) if allowed is not None else None, "tool_profile": profile,
            "max_callbacks": value["max_callbacks"], "allow_mutations": value["allow_mutations"]}


def _disabled_mcp_servers(response: Mapping) -> dict[str, dict[str, bool]]:
    """Project only literal server names from the same process's config/read.

    Never log/forward raw effective configuration, commands, headers or auth
    values. Empty is meaningful only when the official table is present.
    JSON nested object keys preserve dots/quotes in a server's actual name.
    """
    effective = response.get("config") if isinstance(response, Mapping) else None
    if not isinstance(effective, Mapping) or "mcp_servers" not in effective:
        raise ScopeDenied("official effective configuration did not expose its MCP server table")
    servers = effective["mcp_servers"]
    if not isinstance(servers, Mapping) or len(servers) > 128:
        raise ScopeDenied("official effective MCP configuration is unreadable or exceeds its bounded inventory")
    if any(not isinstance(name, str) or not name or len(name) > 1000 or not isinstance(value, Mapping)
           for name, value in servers.items()):
        raise ScopeDenied("official effective MCP server identity/configuration is invalid")
    return {name: {"enabled": False} for name in servers}


_ROOT_UNSTABLE_ADVISORY = (
    "Under-development features enabled: multi_agent_v2_dynamic_tools. "
    "Under-development features are incomplete and may behave unpredictably. "
    "To suppress this warning, set `suppress_unstable_features_warning = true` "
    f"in {Path.home() / '.codex/config.toml'}."
)
_ROOT_CATALOG_BUDGET_WARNING = re.compile(
    r"Exceeded skills context budget\. All skill descriptions were removed and "
    r"[1-9][0-9]{0,5} additional skills were not included in the model-visible skills list\."
)
_WARNING_PRIVATE_MARKER = re.compile(
    r"secret|token|password|passwd|credential|authorization|api[_ -]?key|do[_ -]?not[_ -]?echo|software_",
    re.IGNORECASE,
)


def _root_warning_diagnostic(params: Mapping) -> dict[str, Any]:
    """Bounded screened warning text; never retain arbitrary raw Host content."""
    capsule = {"message": "[withheld]", "message_sha256": None, "bytes": None,
        "truncated": False, "message_disposition": "WITHHELD_INVALID_TEXT"}
    message = params.get("message")
    if not isinstance(message, str):
        return capsule
    try:
        encoded = message.encode("utf-8")
        capsule.update(message_sha256=hashlib.sha256(encoded).hexdigest(), bytes=len(encoded))
        if len(encoded) > 4096:
            capsule["message_disposition"] = "WITHHELD_OVERSIZE"
            return capsule
        from ..research import screening
        screened, counts = screening.redact(message, lists=("credentials",))
        if counts or _WARNING_PRIVATE_MARKER.search(message):
            capsule["message_disposition"] = "WITHHELD_PRIVATE_TEXT"
        elif "//" in message or "%" in message or any(ord(char) < 32 for char in message):
            capsule["message_disposition"] = "WITHHELD_URL_OR_ENCODED_TEXT"
        else:
            safe = screened.encode("utf-8")
            capsule.update(message=safe[:1024].decode("utf-8", errors="ignore"),
                truncated=len(safe) > 1024, message_disposition="SCREENED")
    except (UnicodeError, ValueError, TypeError, OSError, GxError, RuntimeError):
        capsule["message_disposition"] = "WITHHELD_SCREEN_ERROR"
    return capsule


def _metadata_rpc_error(exc: BaseException, thread_id: str | None) -> dict[str, Any] | None:
    """Bounded selected RPC diagnostics, never raw data/config/chat frames."""
    if not isinstance(exc, AppServerResponseError):
        return None
    capsule = {"code": exc.code, "request_id": exc.request_id,
        "response_seq": exc.received.receive_seq, "thread_id": thread_id,
        "message_sha256": None, "message": "[withheld]", "truncated": False,
        "message_disposition": "WITHHELD_INVALID_TEXT"}
    try:
        encoded = exc.message.encode("utf-8")
        capsule["message_sha256"] = hashlib.sha256(encoded).hexdigest()
        if len(encoded) > 4096:
            capsule["message_disposition"] = "WITHHELD_OVERSIZE"
            return capsule
        from ..research import screening
        screened, counts = screening.redact(exc.message, lists=("credentials",))
        if counts:
            capsule["message_disposition"] = "WITHHELD_CREDENTIALS"
        elif "//" in exc.message or "%" in exc.message:
            # Conservatively withhold URI/percent-bearing text in full; do not
            # grow a URL/encoding parser or risk echoing query credentials.
            capsule["message_disposition"] = "WITHHELD_URL_OR_ENCODED_TEXT"
        else:
            safe = screened.encode("utf-8")
            capsule.update(message=safe[:1024].decode("utf-8", errors="ignore"),
                truncated=len(safe) > 1024, message_disposition="SCREENED")
    except Exception:
        # A diagnostic/screen failure must not mask the original RPC failure.
        capsule["message_disposition"] = "WITHHELD_SCREEN_ERROR"
    return capsule


def _failure_text(message: str) -> dict[str, Any]:
    """Screen a complete bounded diagnostic before publishing any prefix."""
    capsule = {"message": "[withheld]", "message_sha256": None, "bytes": None,
        "truncated": False, "message_disposition": "WITHHELD_INVALID_TEXT"}
    try:
        encoded = message.encode("utf-8")
        capsule.update(message_sha256=hashlib.sha256(encoded).hexdigest(), bytes=len(encoded))
        if len(encoded) > 4096:
            capsule["message_disposition"] = "WITHHELD_OVERSIZE"
            return capsule
        screened, credentials, credential_urls = _screen_root_output(message)
        safe = screened.encode("utf-8")
        capsule.update(message=safe[:1024].decode("utf-8", errors="ignore"),
            truncated=len(safe) > 1024, message_disposition="SCREENED",
            redacted_credentials=credentials, withheld_credential_urls=credential_urls)
    except Exception:
        capsule["message_disposition"] = "WITHHELD_SCREEN_ERROR"
    return capsule


def _failure_diagnostic(exc: BaseException) -> dict[str, Any]:
    """Retain causes and code locations, never frames, source lines or locals."""
    try:
        chain, seen = [], set()
        current = exc
        while current is not None and id(current) not in seen and len(chain) < 4:
            seen.add(id(current))
            locations = []
            trace = current.__traceback__
            while trace is not None:
                code = trace.tb_frame.f_code
                try:
                    path = str(Path(code.co_filename).relative_to(contracts.HARNESS_ROOT))
                except ValueError:
                    path = Path(code.co_filename).name
                locations.append({"file": _failure_text(path)["message"],
                    "function": _failure_text(code.co_name)["message"], "line": trace.tb_lineno})
                locations = locations[-6:]
                trace = trace.tb_next
            # Selected RPC diagnostics retain their existing stricter whole-URL
            # withholding policy; never republish a previously withheld message
            # through the generic cause-chain slot.
            message = (_metadata_rpc_error(current, None) if isinstance(current, AppServerResponseError)
                       else _failure_text(str(current)))
            item = {"type": type(current).__name__, "message": message,
                    "locations": locations}
            if getattr(current, "callback_method", None) is not None:
                item["callback"] = {"method": _failure_text(current.callback_method),
                    "receive_seq": current.receive_seq, "kind": current.callback_kind,
                    "stage": current.stage}
            chain.append(item)
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        return {"chain": chain, "chain_truncated": current is not None}
    except Exception:
        # Diagnostics never change the primary failure or recovery decision.
        return {"disposition": "WITHHELD_DIAGNOSTIC_ERROR"}


def _response_effect_status(response: Any) -> str:
    """The existing GX envelope settles business effects, not Host delivery."""
    if (not isinstance(response, Mapping) or not isinstance(response.get("request_id"), str)
            or not response["request_id"] or any(key not in response for key in ("error", "data", "run_id"))):
        return "UNKNOWN"
    if response.get("ok") is True and response["error"] is None and isinstance(response["data"], Mapping):
        return "APPLIED"
    error = response.get("error")
    if (response.get("ok") is False and isinstance(error, Mapping)
            and error.get("code") in contracts.error_codes() and error.get("code") != "OUTCOME_UNKNOWN"
            and error.get("side_effect_status") in ("NONE", "APPLIED", "ROLLED_BACK", "PARTIAL", "UNKNOWN")):
        return error["side_effect_status"]
    return "UNKNOWN"


def _proxy_preflight() -> None:
    # Pinned Codex default_client.rs uses a direct HTTP constructor under this
    # macOS sandbox marker. Do not enter that exception with a Clash-only rule.
    if os.environ.get("CODEX_SANDBOX") == "seatbelt":
        raise ScopeDenied("this Host sandbox mode bypasses HTTP proxy discovery; the native client will not start")
    expected = {name: "http://127.0.0.1:7890" for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    expected.update({name: "socks5h://127.0.0.1:7891" for name in ("ALL_PROXY", "all_proxy")})
    if any(os.environ.get(name) != value for name, value in expected.items()):
        raise ScopeDenied("the six inherited Clash variables must match their fixed HTTP(S)/SOCKS routes")
    for name in ("NO_PROXY", "no_proxy"):
        bypass = {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}
        if not bypass or not bypass <= {"localhost", "127.0.0.1", "::1"}:
            raise ScopeDenied("NO_PROXY/no_proxy must contain only the approved loopback hosts")
    for port in (7890, 7891):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError as exc:
            raise ScopeDenied("Clash is unavailable; the native client will not start or connect directly") from exc


def _project_role_overrides(state: NativeState, document: Mapping) -> list[str]:
    """Use the project's role TOMLs in this Codex process, including on resume."""
    from . import codex_roles
    directory = state.directory / "codex-roles"
    directory.mkdir(exist_ok=True)
    args = []
    for role in codex_roles.shipped_files(document):
        path = directory / Path(role["path"]).name
        if not path.is_file() or path.read_bytes() != role["content"]:
            write_native_bytes(path, role["content"])
        args.extend(["-c", f"agents.{path.stem}.config_file={json.dumps(str(path))}"])
    return args


class NativeRouter:
    """Metadata normalization and direct business dispatch on one trusted stream."""
    def __init__(self, conn: sqlite3.Connection, *, project_ref: str, max_turns: int,
                 max_workers: int, event_sink=None, tool_policy: Mapping | None = None,
                 public_output_sink=None, codex_policy: Mapping | None = None):
        self.conn = conn
        self.codex_policy = codex_policy if codex_policy is not None else host_policy.native_codex_policy()[0]
        self.authority = native.NativeAuthority(project_ref=project_ref)
        self.max_turns, self.max_workers = max_turns, max_workers
        self.event_sink = event_sink
        launch = _tool_policy(tool_policy)
        self.tool_profile = launch["tool_profile"]
        self.authority.tool_profile = self.tool_profile
        self.tool_catalog = dynamic_tools(launch["allowed_tools"], profile=self.tool_profile)
        self.allowed = {tool["name"] for tool in self.tool_catalog}
        self.max_callbacks, self.allow_mutations = launch["max_callbacks"], launch["allow_mutations"]
        self.settled_project_feedback = False
        self.callback_attempts = 0
        self.peak_concurrent_workers = 0
        self.turns = 0
        self.calls = 0
        self.root_finished = False
        self.last_root_turn_id: str | None = None
        self.guidance_in_context = False
        self.unknown = False
        self.events = 0
        self.pending_effects: dict[tuple[str, str], dict[str, Any]] = {}
        self._before_root: list[ReceivedMessage] = []
        self.client = None
        self.discoveries: dict[str, dict[str, Any]] = {}
        self.metadata_refusals: dict[str, dict[str, Any]] = {}
        self.metadata_floors: dict[str, int] = {}
        self.replaying_metadata = False
        self.initial_local_store = False
        self.resumed_root = False
        self.materialization_attempts: set[str] = set()
        self.public_output_collector = (PublicOutputCollector(public_output_sink, screen=_screen_root_output)
            if public_output_sink is not None else None)
        self.activity_cwd = str(contracts.HARNESS_ROOT)
        self.reset_root_output()

    def reset_root_output(self) -> None:
        """Only the latest requested Root turn supplies the returned user answer."""
        self.root_final_output: dict[str, Any] = {
            "scope": "ROOT_USER_OUTPUT_NOT_IDENTITY_OR_VERDICT",
            "source": "official_item_completed_final_answer", "capture_status": "NOT_OBSERVED",
            "text": None, "content_sha256": None}
        self._final_item_ids: set[str] = set()
        self._final_scan_bytes = 0
        self._final_halted = False

    def emit(self, event: Mapping[str, Any]) -> None:
        self.events += 1
        if self.event_sink:
            self.event_sink(dict(event))

    def _carry_policy_epoch(self, thread_id, sequence):
        from .native_observation import record_adverse_epoch
        if thread_id not in self.authority.active_turns:
            return True
        try:
            record_adverse_epoch(self.conn, self.authority, thread_id, sequence=sequence)
        except Exception as exc:
            self.emit({"kind": "native_adverse_epoch_carry_failed", "event_seq": sequence,
                "thread_id": thread_id, "error_type": type(exc).__name__,
                "disposition": "TERMINAL_SELECTED_ADVERSE_FACT_NOT_PERSISTED"})
            return False
        return True

    def _native_activity(self, params: Mapping, method: str, sequence: int) -> None:
        """Selected official item metadata; no command/reasoning/output text.

        The timestamp is this callback's local monotonic observation time, not
        a reconstructed Host execution time or an identity/business assertion.
        """
        item = params.get("item")
        if not isinstance(item, Mapping) or item.get("type") not in ("commandExecution", "reasoning"):
            return
        identities = {name: params.get(key) for name, key in (("thread_id", "threadId"), ("turn_id", "turnId"))}
        identities["item_id"] = item.get("id")
        if any(not isinstance(value, str) or not value or len(value) > 1000
               or any(ord(char) < 32 for char in value) for value in identities.values()):
            return
        event = {"kind": "native_activity", "event_seq": sequence,
            "monotonic_ns": time.monotonic_ns(), "phase": method.rsplit("/", 1)[1],
            "item_type": item["type"], **identities,
            "scope": "OFFICIAL_ITEM_METADATA_NOT_IDENTITY_OR_VERDICT"}
        if item["type"] == "commandExecution":
            def digest(value):
                try:
                    return hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None
                except UnicodeError:
                    return None
            cwd = item.get("cwd")
            event.update(command_sha256=digest(item.get("command")), cwd_sha256=digest(cwd),
                cwd=cwd if cwd == self.activity_cwd else None,
                cwd_disposition="EXACT_LAUNCH_WORKSPACE" if cwd == self.activity_cwd else "WITHHELD_NONLAUNCH_PATH",
                exit_code=item.get("exitCode") if type(item.get("exitCode")) is int else None,
                output_sha256=digest(item.get("aggregatedOutput")))
        self.emit(event)

    def check_configuration(self, model, effort, *, root=False, allow_unknown=False) -> None:
        from . import policy
        document = self.codex_policy
        allowed = (document["native_coordinator_observation"]["accepted_efforts"] if root else
            {level for task_class in document["task_classes"]
             for level in policy.allowed_efforts(task_class, document)})
        if (not (model is None and allow_unknown) and (not model or not policy.is_pinned_model(model, document))
                or not (effort is None and allow_unknown) and effort not in allowed):
            raise ScopeDenied("official native configuration is unknown or outside the admitted model/effort")

    def _capture_root_final(self, params: Mapping, sequence: int) -> None:
        item = params.get("item") or {}
        root, turn = self.authority.root_thread_id, params.get("turnId")
        if (params.get("threadId") != root or not root or not isinstance(turn, str)
                or self.authority.active_turns.get(root) != turn
                or item.get("phase") != "final_answer" or self._final_halted):
            return
        item_id, text = item.get("id"), item.get("text")
        if isinstance(item_id, str) and item_id in self._final_item_ids:
            return
        output = {"scope": "ROOT_USER_OUTPUT_NOT_IDENTITY_OR_VERDICT",
            "source": "official_item_completed_final_answer", "thread_id": root,
            "turn_id": turn, "event_seq": sequence, "capture_status": "WITHHELD",
            "text": None, "content_sha256": None, "truncated": False,
            "redaction_count": 0, "withheld_url_count": 0, "original_bytes": None}
        self.root_final_output = output  # Never fall back to a stale earlier answer.
        if (not isinstance(item_id, str) or not item_id or len(item_id) > 1000
                or not isinstance(text, str)):
            self._final_halted = True
            output["disposition"] = "WITHHELD_INVALID_ITEM"
            return
        if len(self._final_item_ids) >= 4 or len(text) > 65536 - self._final_scan_bytes:
            self._final_halted = True
            output["disposition"] = "WITHHELD_OUTPUT_BUDGET"
            return
        self._final_item_ids.add(item_id)
        output["item_id"] = item_id
        try:
            encoded = text.encode("utf-8")
            output["original_bytes"] = len(encoded)
            if len(encoded) > 65536 - self._final_scan_bytes:
                self._final_halted = True
                output["disposition"] = "WITHHELD_OUTPUT_BUDGET"
                return
            self._final_scan_bytes += len(encoded)
            screened, redactions, urls = _screen_root_output(text)
            safe = screened.encode("utf-8")
            published = safe[:8192].decode("utf-8", errors="ignore")
            output.update(capture_status="CAPTURED", text=published,
                content_sha256=hashlib.sha256(published.encode("utf-8")).hexdigest(),
                truncated=len(safe) > 8192, redaction_count=redactions,
                withheld_url_count=urls, disposition="SCREENED_USER_OUTPUT")
        except Exception:
            # A user-output diagnostic failure changes no protocol verdict and
            # never publishes an unscreened prefix or masks a Host failure.
            self._final_halted = True
            output["disposition"] = "WITHHELD_SCREEN_ERROR"

    def _check_worker_capacity(self, thread_id: str) -> None:
        root = self.authority.root_thread_id
        if thread_id == root:
            return
        # Codex owns resident threads and can evict or wake idle workers.
        # Count current work, including observations awaiting worker metadata.
        workers = self.authority.active_turns.keys() | self.discoveries.keys() | {thread_id}
        workers.discard(root)
        workers.difference_update(self.metadata_refusals)
        if len(workers) > self.max_workers:
            raise ScopeDenied("the concurrent native worker budget is exhausted")
        self.peak_concurrent_workers = max(self.peak_concurrent_workers, len(workers))

    def _worker_closed(self, thread_id: str, sequence: int) -> None:
        pending = self.discoveries.get(thread_id, {})
        if "materialization" in pending:
            raise ScopeDenied("the native thread closed during its materialization observation")
        # A worker eviction cannot settle another actor's write. Leave those
        # effects with their original actors and the session recovery owner.
        if any(worker == thread_id for worker, _ in self.pending_effects):
            raise ScopeDenied(WORKER_CLOSE_EFFECT_MESSAGE)
        self.discoveries.pop(thread_id, None)
        turn = self.authority.active_turns.get(thread_id)
        if turn is not None:
            self.authority.finish_turn(thread_id, turn, sequence=sequence)
        if thread_id in self.authority.threads:
            # The identity and its prior turn epochs survive eviction. A reloaded
            # runtime must supply its current settings through the usual path.
            self.authority.apply_settings(thread_id, {}, sequence=sequence)
        if turn or pending.get("active"):
            self.emit({"kind": "worker_closed", "event_seq": sequence, "thread_id": thread_id,
                       "turn_id": turn or pending["turn_id"], "disposition": "NO_TURN_COMPLETION_OBSERVED"})

    def _pending_notification(self, received: ReceivedMessage, thread_id: str) -> None:
        """Preserve original observations, never assign later model facts to them."""
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 1000:
            raise ScopeDenied("unknown native event has no bounded thread identity")
        if thread_id not in self.discoveries:
            self._check_worker_capacity(thread_id)
            self.discoveries[thread_id] = {"first_wire_seq": received.receive_seq,
                "events": 0, "turn_id": None, "turn_start_seq": None, "active": False, "terminal": False}
        pending = self.discoveries[thread_id]
        pending["events"] += 1
        if pending["events"] > 256:
            raise ScopeDenied("the bounded pending native metadata observations are exhausted")
        method, params = received.message["method"], received.message.get("params") or {}
        if method == "turn/started":
            turn_id = (params.get("turn") or {}).get("id")
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 1000:
                raise ScopeDenied("unknown native turn has no bounded actual turn identity")
            if pending["active"]:
                raise ScopeDenied("unknown native turn starts are duplicate or ambiguous")
            if thread_id in self.authority.threads:
                if thread_id in self.authority.active_turns:
                    raise ScopeDenied("a known native thread already has an observed active turn")
                # A later real turn belongs to the already admitted identity.
                # Keep its actual start/history; metadata cannot replace it.
                self.authority.start_turn(thread_id, turn_id, sequence=received.receive_seq)
            self.turns += 1
            if self.turns > self.max_turns:
                raise ScopeDenied("the explicit native turn budget is exhausted")
            pending.update(turn_id=turn_id, turn_start_seq=received.receive_seq, active=True, terminal=False)
        elif method == "turn/completed":
            turn_id = (params.get("turn") or {}).get("id")
            if not pending["active"] or pending["turn_id"] != turn_id:
                raise ScopeDenied("unknown native turn completion has no matching original start")
            if self.authority.active_turns.get(thread_id) == turn_id:
                self.authority.finish_turn(thread_id, turn_id, sequence=received.receive_seq)
            pending.update(active=False, terminal=True)
        elif method == "thread/settings/updated":
            settings = params.get("threadSettings") or {}
            self.check_configuration(settings.get("model"), settings.get("effort"), allow_unknown=True)
        elif method == "thread/started":
            facts = params.get("thread") or {}
            self.check_configuration(facts.get("model"), facts.get("reasoningEffort"), allow_unknown=True)
            if (facts.get("id") != thread_id or facts.get("sessionId") != self.authority.session_id
                    or not isinstance(facts.get("parentThreadId"), str) or not facts["parentThreadId"]
                    or facts["parentThreadId"] == thread_id
                    or facts.get("cliVersion") != self.authority.host_version):
                if thread_id != self.authority.root_thread_id and not self.unknown and not self.pending_effects:
                    self.metadata_refusal_diagnostic(thread_id, facts, received.receive_seq, "PENDING_THREAD_IDENTITY")
                    self.refuse_metadata_actor(thread_id, received.receive_seq)
                    return
                raise ScopeDenied("observed pending-thread identity contradicts the admitted native session")
            try:
                self._pending_immutable(thread_id, self._worker_facts(facts), sequence=received.receive_seq, record=True)
            except ScopeDenied:
                if thread_id == self.authority.root_thread_id or self.unknown or self.pending_effects:
                    raise
                self.metadata_refusal_diagnostic(thread_id, facts, received.receive_seq, "PENDING_IMMUTABLE_IDENTITY")
                self.refuse_metadata_actor(thread_id, received.receive_seq)
                return
        elif method == "thread/status/changed":
            status = params.get("status") or {}
            if status.get("type") == "systemError" or pending["active"] and status.get("type") == "notLoaded":
                self.emit({"kind": "metadata_validation_refused", "thread_id": thread_id,
                    "original_wire_seq": received.receive_seq, "turn_id": pending["turn_id"],
                    "disposition": "OBSERVED_ACTIVE_TURN_RUNTIME_GAP" if pending["active"]
                        else "OBSERVED_PRE_TURN_SYSTEM_ERROR"})
                raise ScopeDenied("later metadata cannot revive an observed unloaded or errored active native turn")
        elif method == "error":
            self._host_error(received)
        elif method == "model/rerouted":
            raise ScopeDenied("an observed unknown-child error/reroute cannot be repaired by later metadata")
        self.emit({"kind": "metadata_discovery_pending", "thread_id": thread_id,
            "turn_id": pending["turn_id"], "original_wire_seq": received.receive_seq,
            "original_wire_method": method, "disposition": "NO_IDENTITY_OR_BUSINESS_ADMISSION"})
        # Allocation/loaded/active status is not an actual turn. Await the
        # official turn id before querying current model/identity metadata.
        if pending["turn_start_seq"] is not None and not self.replaying_metadata:
            if self.client is None:
                raise ScopeDenied("native metadata lookup has no owned official connection")
            self.client.pause_callbacks()

    def _pending_immutable(self, thread_id: str, facts: Mapping, *, sequence: int, record=False) -> None:
        pending = self.discoveries[thread_id]
        for wire_key, stored_key in (("parentThreadId", "observed_parent"), ("forkedFromId", "observed_fork")):
            if wire_key not in facts:
                continue
            if stored_key in pending and pending[stored_key] != facts[wire_key]:
                self.emit({"kind": "metadata_validation_refused", "thread_id": thread_id,
                    "original_wire_seq": sequence, "disposition": "IMMUTABLE_" + wire_key + "_DISAGREES"})
                raise ScopeDenied("pending native immutable identity changed: " + wire_key)
            if record:
                pending[stored_key] = facts[wire_key]

    def metadata_needed(self, thread_id: str) -> bool:
        if thread_id == self.authority.root_thread_id:
            return False
        facts = self.authority.threads.get(thread_id)
        return (facts is None or facts.get("model") is None or facts.get("effort") is None
                or not facts.get("fork_origin_known") and thread_id not in self.metadata_floors)

    def _worker_facts(self, thread: Mapping, *, current_local_read=False) -> dict[str, Any]:
        """Normalize selected facts without certifying a fallback public null.

        Fixed LocalThreadStore read(false) has a source-proved discriminator:
        nonempty preview plus an admitted nonempty parent excludes both the
        synthetic fallback and a stored view lacking SessionMeta. Only its
        boolean is used here; preview content is never copied or retained.
        """
        facts = {key: thread.get(key) for key in ("id", "sessionId", "parentThreadId",
                 "cliVersion", "model", "reasoningEffort")}
        if "forkedFromId" in thread:
            fork = thread["forkedFromId"]
            if fork is not None and (not isinstance(fork, str) or not fork or len(fork) > 1000):
                raise ScopeDenied("the official worker fork value is invalid")
            parent = thread.get("parentThreadId")
            proven_null = (current_local_read and self.initial_local_store
                and self.authority.host_version == thread.get("cliVersion") == "0.169.0"
                and isinstance(parent, str) and bool(parent) and parent in self.authority.threads
                and isinstance(thread.get("preview"), str) and thread["preview"] != "")
            if fork is not None or proven_null:
                facts["forkedFromId"] = fork
        path = native_spawn.thread_path(thread)
        if path:
            facts["agent_path"] = path
        return facts

    def _pending_lifecycle(self, thread_id: str, status: Mapping) -> None:
        pending = self.discoveries[thread_id]
        active = status["type"] == "active"
        if (pending["active"] and not active) or (pending["terminal"] and active):
            raise ScopeDenied("current native metadata conflicts with the observed turn terminal boundary")

    def begin_materialization(self, thread_id: str, response: Mapping, facts: Mapping, sequence: int) -> bool:
        """Choose one source-proved persist path; no caller-supplied receipt."""
        thread = response["thread"]  # Already checked by stage_metadata.
        known = self.authority.threads.get(thread_id) or {}
        pending = self.discoveries[thread_id]
        if (thread_id == self.authority.root_thread_id or not self.initial_local_store
                or thread.get("parentThreadId") not in self.authority.threads
                or thread.get("cliVersion") != "0.169.0"
                or thread.get("historyMode") != "paginated" or thread.get("ephemeral") is not False
                or thread.get("forkedFromId") is not None or "forkedFromId" in facts
                or known.get("fork_origin_known") or "observed_fork" in pending
                or thread_id in self.materialization_attempts):
            return False
        self._pending_lifecycle(thread_id, facts["status"])
        self.materialization_attempts.add(thread_id)
        self._pending_immutable(thread_id, facts, sequence=sequence, record=True)
        pending["materialization"] = {"initial_seq": sequence, "ack_seq": None,
            "identity": {key: thread[key] for key in
                ("id", "sessionId", "parentThreadId", "cliVersion", "historyMode", "ephemeral")}}
        return True

    def _materialization_view(self, thread_id: str, response: Mapping) -> Mapping:
        record = self.discoveries[thread_id]["materialization"]
        thread = response.get("thread") if isinstance(response, Mapping) else None
        if (thread_id == self.authority.root_thread_id or not isinstance(thread, Mapping)
                or any(thread.get(key) != value for key, value in record["identity"].items())
                or thread.get("ephemeral") is not False
                or "forkedFromId" not in thread):
            raise ScopeDenied("the native materialization response changed its exact thread/source tuple")
        status = thread.get("status")
        if not isinstance(status, Mapping) or status.get("type") not in ("active", "idle"):
            raise ScopeDenied("the materialized native thread is no longer loaded/current")
        self._pending_lifecycle(thread_id, status)
        self.check_configuration(thread.get("model"), thread.get("reasoningEffort"))
        self._worker_facts(thread)  # Validate fork shape without trusting a null.
        return thread

    def observe_materialization(self, thread_id: str, response: Mapping, sequence: int) -> None:
        record = self.discoveries[thread_id]["materialization"]
        if type(sequence) is not int or sequence <= record["initial_seq"] or record["ack_seq"] is not None:
            raise ScopeDenied("native materialization has no unique ordered success response")
        thread = self._materialization_view(thread_id, response)
        self._pending_immutable(thread_id, self._worker_facts(thread), sequence=sequence, record=True)
        # Inspect only negative metadata for the already observed actual turn.
        # Never inspect/copy historical item or message bodies or infer a turn.
        turns = thread.get("turns", [])
        if not isinstance(turns, list):
            raise ScopeDenied("native materialization returned an invalid turns container")
        current = self.discoveries[thread_id]["turn_id"]
        matches = [turn.get("status") for turn in turns if isinstance(turn, Mapping) and turn.get("id") == current]
        if len(matches) > 1 or any(status != "inProgress" for status in matches):
            raise ScopeDenied("materialization observed an ended or ambiguous actual turn")
        record["ack_seq"] = sequence

    def metadata_refusal_diagnostic(self, thread_id: str, thread: Mapping | None, metadata_seq: int, disposition: str) -> None:
        fields = {}
        for name in ("id", "sessionId", "parentThreadId", "cliVersion", "forkedFromId"):
            present = isinstance(thread, Mapping) and name in thread
            value = thread.get(name) if present else None
            entry = {"present": present, "type": type(value).__name__ if present else None}
            if present and value is None:
                entry["value"] = None
            elif isinstance(value, str):
                entry["sha256"] = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
                pattern = (r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}"
                           if name == "cliVersion" else r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
                if re.fullmatch(pattern, value):
                    entry["value"] = value
                else:
                    entry["value_withheld"] = True
            fields[name] = entry
        selected = thread if isinstance(thread, Mapping) else {}
        checks = {"thread_id_matches": selected.get("id") == thread_id,
            "session_matches": selected.get("sessionId") == self.authority.session_id,
            "parent_shape_valid": isinstance(selected.get("parentThreadId"), str)
                and 0 < len(selected["parentThreadId"]) <= 1000 and selected["parentThreadId"] != thread_id,
            "creator_cli_matches_admitted_version": selected.get("cliVersion") == self.authority.host_version,
            "fork_field_present": "forkedFromId" in selected,
            "fork_shape_valid": "forkedFromId" in selected and (selected["forkedFromId"] is None or
                isinstance(selected["forkedFromId"], str) and 0 < len(selected["forkedFromId"]) <= 1000)}
        self.emit({"kind": "metadata_validation_refused", "thread_id": thread_id,
            "metadata_seq": metadata_seq, "disposition": disposition,
            "identity_fields": fields, "identity_checks": checks,
            "scope": "OFFICIAL_METADATA_DIAGNOSTIC_NOT_HISTORICAL_BACKFILL_OR_FRESHNESS"})

    def stage_metadata(self, thread_id: str, response: Mapping, metadata_seq: int) -> dict[str, Any]:
        """Read current official facts, but do not advance authority ahead of old rows."""
        thread = response.get("thread") if isinstance(response, Mapping) else None
        def refuse(disposition, *, error_type=ScopeDenied):
            self.metadata_refusal_diagnostic(thread_id, thread, metadata_seq, disposition)
            raise error_type("official current child metadata refused: " + disposition)
        if type(metadata_seq) is not int or metadata_seq <= self.discoveries[thread_id]["first_wire_seq"]:
            refuse("RESPONSE_SEQUENCE")
        if not isinstance(thread, Mapping) or thread.get("id") != thread_id:
            refuse("THREAD_ID")
        status = thread.get("status")
        if not isinstance(status, Mapping) or status.get("type") not in ("active", "idle"):
            refuse("LOADED_CURRENT_VIEW")
        try:
            self.check_configuration(thread.get("model"), thread.get("reasoningEffort"), allow_unknown=True)
        except ScopeDenied:
            refuse("CURRENT_MODEL_EFFORT", error_type=ModelPolicyViolation)
        if (thread.get("sessionId") != self.authority.session_id
                or not isinstance(thread.get("parentThreadId"), str) or not thread["parentThreadId"]
                or len(thread["parentThreadId"]) > 1000 or thread["parentThreadId"] == thread_id
                or thread.get("cliVersion") != self.authority.host_version
                or "forkedFromId" not in thread):
            refuse("PARENT_SESSION_VERSION_FORK_PRESENCE")
        if thread["forkedFromId"] is not None and (not isinstance(thread["forkedFromId"], str) or not thread["forkedFromId"]):
            refuse("FORK_VALUE")
        facts = self._worker_facts(thread, current_local_read=True)
        if self.resumed_root and thread.get("parentThreadId") in self.authority.threads:
            from .native_observation import previously_observed_fork
            prior = previously_observed_fork(self.conn, self.authority.project_ref, self.authority.session_id, thread_id,
                                             thread["parentThreadId"], thread["cliVersion"])
            if "forkedFromId" in facts and prior and prior["forkedFromId"] != facts["forkedFromId"]:
                raise ScopeDenied("the official fork origin differs from this worker's earlier native claim")
            facts.update(prior)
        materialization = self.discoveries[thread_id].get("materialization")
        if materialization and materialization["ack_seq"] is not None:
            if metadata_seq <= materialization["ack_seq"]:
                refuse("MATERIALIZATION_RESPONSE_SEQUENCE")
            self._materialization_view(thread_id, response)
            # Only the post-ack false-read is canonical fork authority. The
            # true-read's null/history never grants freshness.
            if thread["forkedFromId"] is None:
                facts["forkedFromId"] = None
        self._pending_immutable(thread_id, facts, sequence=metadata_seq)
        try:
            self.check_configuration(thread.get("model"), thread.get("reasoningEffort"))
        except ScopeDenied:
            refuse("CURRENT_MODEL_EFFORT")
        facts["status"] = {"type": status["type"]}
        self.emit({"kind": "metadata_snapshot_staged", "thread_id": thread_id,
            "metadata_seq": metadata_seq, "original_wire_seq": self.discoveries[thread_id]["first_wire_seq"],
            "disposition": "CURRENT_FACTS_NOT_HISTORICAL_AUTHORITY"})
        return facts

    def admit_metadata(self, thread_id: str, facts: Mapping, metadata_seq: int) -> None:
        pending = self.discoveries[thread_id]
        facts = dict(facts)
        if "forkedFromId" not in facts and "observed_fork" in pending:
            # An unproven null cannot erase an actually observed nonnull fork.
            facts["forkedFromId"] = pending["observed_fork"]
        # Deferred rows were delivered after stage_metadata. Recheck the whole
        # immutable observed tuple here, before any authority/epoch is created.
        try:
            self._pending_immutable(thread_id, facts, sequence=metadata_seq)
        except ScopeDenied:
            if thread_id == self.authority.root_thread_id or self.unknown or self.pending_effects:
                raise
            self.refuse_metadata_actor(thread_id, metadata_seq)
            return
        self._pending_lifecycle(thread_id, facts["status"])
        try:
            self.authority.register_thread(facts, sequence=metadata_seq)
        except GxError:
            if thread_id == self.authority.root_thread_id or self.unknown or self.pending_effects:
                raise
            self.refuse_metadata_actor(thread_id, metadata_seq)
            return
        native_spawn.remember_path(self.conn, self.authority, thread_id, facts["parentThreadId"], facts.get("agent_path"))
        self.metadata_floors[thread_id] = metadata_seq
        if pending["active"] and self.authority.active_turns.get(thread_id) != pending["turn_id"]:
            self.authority.admit_existing_turn(thread_id, pending["turn_id"], metadata_seq=metadata_seq,
                original_wire_start_seq=pending["turn_start_seq"])
        del self.discoveries[thread_id]
        self.emit({"kind": "worker_registered", "event_seq": metadata_seq, "metadata_seq": metadata_seq,
            "thread_id": thread_id, "session_id": facts["sessionId"], "parent_thread_id": facts["parentThreadId"],
            "forked_from_present": "forkedFromId" in facts, "forked_from_id": facts.get("forkedFromId"),
            "fork_origin_disposition": ("KNOWN_FORK" if facts.get("forkedFromId") is not None else
                "SOURCE_VERIFIED_UNFORKED" if "forkedFromId" in facts else "UNKNOWN_OR_UNPROVEN"),
            "subject": self.authority.principal_for(thread_id).subject,
            "model": facts["model"], "effort": facts["reasoningEffort"],
            "original_wire_start_seq": pending["turn_start_seq"],
            "disposition": "OFFICIAL_CURRENT_METADATA_ADMITTED_NO_PRE_METADATA_HISTORY"})

    def finished(self) -> bool:
        return (self.root_finished and not any(identity not in self.metadata_refusals
                    for identity in self.authority.active_turns)
                and not self.discoveries)

    def refuse_metadata_actor(self, thread_id: str, metadata_seq: int) -> None:
        """Resolve this failed lookup without registering or retrying the actor."""
        pending = self.discoveries.pop(thread_id, {})
        self.metadata_refusals[thread_id] = {"thread_id": thread_id, "turn_id": pending.get("turn_id"),
            "metadata_seq": metadata_seq, "disposition": "CHILD_UNREGISTERED_GX_API0_ROOT_CONTINUES"}
        self.emit({"kind": "metadata_actor_refused", **self.metadata_refusals[thread_id],
            "scope": "RESOLVED_METADATA_REFUSAL_NOT_BUSINESS_EFFECT_OR_FRESHNESS"})

    def metadata_callback_denial(self, thread_id: str, turn_id: str, sequence: int) -> dict[str, Any]:
        """A pre-dispatch refusal is a normal tool failure, never a Root call."""
        self.emit({"kind": "metadata_callback_refused", "thread_id": thread_id,
            "turn_id": turn_id, "original_wire_seq": sequence,
            "disposition": "NO_PRIOR_CURRENT_METADATA_API0", "side_effect_status": "NONE"})
        response = {"ok": False, "api_calls": 0, "error": {"code": "SCOPE_DENIED",
            "side_effect_status": "NONE", "message": "This actor has no admitted current metadata; GX was not called. "
            "Root and admitted workers may continue; this actor's original task/lease owner handles recovery."}}
        return {"contentItems": [{"type": "inputText", "text": json.dumps(response, ensure_ascii=False)}],
                "success": False}

    def _root_warning(self, received: ReceivedMessage, *, before_input: bool) -> None:
        params = received.message.get("params") or {}
        message = params.get("message")
        # Warning prose is diagnostic, never a second admission/approval owner.
        # Required guidance/tools, identity/model policy and UNKNOWN effects are
        # checked by their existing owners independently of notification text.
        category = "HOST_NOTICE"
        if message == _ROOT_UNSTABLE_ADVISORY:
            category = "NATIVE_UNDER_DEVELOPMENT"
        elif isinstance(message, str) and _ROOT_CATALOG_BUDGET_WARNING.fullmatch(message):
            category = "SKILL_CATALOG_BUDGET"
        self.emit({"kind": "root_warning_diagnostic", "event_seq": received.receive_seq,
            "thread_id": self.authority.root_thread_id, "before_input": before_input,
            "category": category, "disposition": "RECORDED_CONTINUE",
            "warning": _root_warning_diagnostic(params)})

    def _host_error(self, received: ReceivedMessage) -> None:
        """Official StreamError may retry inside the same current native turn."""
        raw = received.message.get("params")
        params = raw if isinstance(raw, Mapping) else {}
        error = params.get("error")
        error = error if isinstance(error, Mapping) else {}
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        valid_id = lambda value: isinstance(value, str) and 0 < len(value) <= 1000
        pending = self.discoveries.get(thread_id, {}) if isinstance(thread_id, str) else {}
        current = (valid_id(thread_id) and valid_id(turn_id) and
            (self.authority.active_turns.get(thread_id) == turn_id
             or pending.get("active") is True and pending.get("turn_id") == turn_id))
        valid = (isinstance(raw, Mapping) and current and type(params.get("willRetry")) is bool
            and isinstance(error.get("message"), str)
            and all(key in error for key in ("codexErrorInfo", "additionalDetails", "misalignment"))
            and (error["codexErrorInfo"] is None or isinstance(error["codexErrorInfo"], (str, Mapping)))
            and (error["additionalDetails"] is None or isinstance(error["additionalDetails"], str))
            and (error["misalignment"] is None or isinstance(error["misalignment"], Mapping)))
        def structured(value):
            try:
                return _failure_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
            except (ValueError, TypeError, RecursionError):
                return _failure_text(None)
        retrying = valid and params["willRetry"] is True
        self.emit({"kind": "native_error_diagnostic", "event_seq": received.receive_seq,
            "thread_id": _failure_text(thread_id)["message"], "turn_id": _failure_text(turn_id)["message"],
            "will_retry": params.get("willRetry") if type(params.get("willRetry")) is bool else None,
            "schema_and_current_turn_valid": bool(valid),
            "error": {"message": _failure_text(error.get("message", params.get("message"))),
                "codex_error_info": structured(error.get("codexErrorInfo")),
                "additional_details": _failure_text(error.get("additionalDetails")),
                "misalignment": structured(error.get("misalignment"))},
            "disposition": "NATIVE_SAME_TURN_RETRY_CONTINUE" if retrying else "TERMINAL_OR_INVALID_STOP"})
        if not retrying:
            raise ScopeDenied("the official Host reported a terminal or invalid execution error; inspect its retained diagnostic")

    def register_root(self, response: Mapping[str, Any], sequence: int) -> None:
        thread = response.get("thread")
        if not isinstance(thread, Mapping):
            raise ScopeDenied("official thread/start returned no thread metadata")
        facts = dict(thread)
        # These values come from the Host response, never the request or model JSON.
        for outer, inner in (("model", "model"), ("reasoningEffort", "reasoningEffort")):
            if facts.get(inner) is None and response.get(outer) is not None:
                facts[inner] = response[outer]
        self.check_configuration(facts.get("model"), facts.get("reasoningEffort"), root=True)
        self.authority.register_thread(facts, sequence=sequence, root=True)
        for received in self._before_root:
            message = received.message
            if message.get("method") == "thread/started":
                observed = (message.get("params") or {}).get("thread") or {}
                if observed.get("id") != facts["id"]:
                    raise ScopeDenied("unexpected unrelated thread arrived before root registration")
            elif message.get("method") == "warning" and (message.get("params") or {}).get("threadId") == facts["id"]:
                self._root_warning(received, before_input=True)
            elif message.get("method") == "error":
                self._host_error(received)
            elif (message.get("method") in ("thread/compacted", "thread/reverted")
                    and (message.get("params") or {}).get("threadId") == facts["id"]):
                self.guidance_in_context = False
            elif message.get("method", "").startswith(("turn/", "item/", "hook/", "mcpServer/")):
                raise ScopeDenied("unexpected runtime activity before the root was registered")
        self._before_root.clear()
        self.emit({"kind": "root_registered", "event_seq": sequence,
                   "thread_id": facts["id"], "session_id": facts["sessionId"],
                   "forked_from_present": "forkedFromId" in facts,
                   "forked_from_id": facts.get("forkedFromId"),
                   "model": facts.get("model"), "effort": facts.get("reasoningEffort"),
                   "host_version": facts.get("cliVersion"), "model_authority": native.MODEL_AUTHORITY})

    def on_notification(self, received: ReceivedMessage) -> None:
        message, seq = received.message, received.receive_seq
        method, params = message.get("method", ""), message.get("params") or {}
        if method == "error":
            if self.authority.root_thread_id is not None:
                self.authority.advance(seq)
            self._host_error(received)
            return
        if self.authority.root_thread_id is None:
            if len(self._before_root) >= 256:
                raise ScopeDenied("too many startup events before official root registration")
            self._before_root.append(received)
            return
        self.authority.advance(seq)
        if method == "thread/tokenUsage/updated":
            metrics.codex_usage(params, seq, self.authority.instance_id)
        if self.public_output_collector is not None:
            self.public_output_collector.observe(received, root_thread_id=self.authority.root_thread_id)
        if method in ("item/started", "item/completed"):
            self._native_activity(params, method, seq)
        thread_id = params.get("threadId")
        if thread_id == self.authority.root_thread_id and (
                method in ("thread/compacted", "thread/reverted") or
                method in ("item/started", "item/completed") and
                (params.get("item") or {}).get("type") == "contextCompaction"):
            self.guidance_in_context = False
        if method == "thread/started":
            thread_id = (params.get("thread") or {}).get("id")
        if thread_id and thread_id != self.authority.root_thread_id:
            if method == "thread/closed":
                self._worker_closed(thread_id, seq)
                return
            if (method == "thread/status/changed" and (params.get("status") or {}).get("type") in ("idle", "notLoaded")
                    and thread_id not in self.authority.active_turns and thread_id not in self.discoveries):
                if params["status"]["type"] == "notLoaded" and thread_id in self.authority.threads:
                    self._worker_closed(thread_id, seq)
                return  # A residency notification alone does not start work.
        if thread_id in self.metadata_refusals and method == "thread/settings/updated":
            settings = params.get("threadSettings") or {}
            self.check_configuration(settings.get("model"), settings.get("effort"), allow_unknown=True)
            return
        if thread_id in self.metadata_refusals and method == "thread/started":
            facts = params.get("thread") or {}
            self.check_configuration(facts.get("model"), facts.get("reasoningEffort"), allow_unknown=True)
            return
        if thread_id in self.metadata_refusals and method != "model/rerouted":
            # This actor is resolved as unadmitted. Its ordinary native output,
            # status and completion cannot recreate a pending GX discovery.
            if method == "turn/started" and (params.get("turn") or {}).get("id") != self.metadata_refusals[thread_id]["turn_id"]:
                self.turns += 1
                if self.turns > self.max_turns:
                    raise ScopeDenied("the explicit native turn budget is exhausted")
                self.metadata_refusals[thread_id]["turn_id"] = (params.get("turn") or {}).get("id")
            return
        pending_methods = {"thread/status/changed", "turn/started", "turn/completed", "thread/settings/updated", "model/rerouted", "error"}
        known = self.authority.threads.get(thread_id)
        refresh_unknown_fork = (method == "turn/started" and thread_id in self.metadata_floors
            and known is not None and not known.get("fork_origin_known"))
        trigger = (thread_id not in self.authority.threads and method in pending_methods
            or self.metadata_needed(thread_id) and method in {"thread/status/changed", "turn/started", "turn/completed"}
            or refresh_unknown_fork)
        if thread_id in self.discoveries or trigger:
            self._pending_notification(received, thread_id)
            return
        if (method in ("item/started", "item/completed") and thread_id in self.authority.threads
                and self.authority.active_turns.get(thread_id) == params.get("turnId")):
            native_spawn.observe(self.conn, project_ref=self.authority.project_ref,
                                 session_id=self.authority.session_id, parent_thread_id=thread_id,
                                 turn_id=params.get("turnId"), item=params.get("item"),
                                 phase=method.rsplit("/", 1)[1])
        if method == "thread/started":
            thread = params.get("thread") or {}
            native_id = thread.get("id")
            if native_id == self.authority.root_thread_id:
                # The start response already established this same identity.
                if thread.get("sessionId") != self.authority.session_id:
                    raise ScopeDenied("the root notification changed session identity")
            else:
                # Official Thread fields are optional. Identity may arrive
                # before its configuration; preserve unknown rather than
                # guessing from parent/request. NativeCall still requires both.
                self.check_configuration(thread.get("model"), thread.get("reasoningEffort"), allow_unknown=True)
                try:
                    normalized = self._worker_facts(thread)
                    self.authority.register_thread(normalized, sequence=seq)
                except GxError:
                    if self.unknown or self.pending_effects:
                        raise
                    self.metadata_refusal_diagnostic(native_id, thread, seq, "STARTED_THREAD_IDENTITY")
                    self.refuse_metadata_actor(native_id, seq)
                    return
                native_spawn.remember_path(self.conn, self.authority, native_id,
                                           normalized["parentThreadId"], normalized.get("agent_path"))
                self.emit({"kind": "worker_registered", "event_seq": seq, "thread_id": native_id,
                    "parent_thread_id": thread.get("parentThreadId"), "session_id": thread.get("sessionId"),
                    "forked_from_present": "forkedFromId" in normalized,
                    "forked_from_id": normalized.get("forkedFromId"),
                    "fork_origin_disposition": ("KNOWN_FORK" if normalized.get("forkedFromId") is not None
                        else "UNKNOWN_OR_UNPROVEN"),
                    "subject": self.authority.principal_for(native_id).subject,
                    "model": thread.get("model"), "effort": thread.get("reasoningEffort")})
        elif method == "thread/settings/updated":
            settings = params.get("threadSettings") or {}
            self.authority.apply_settings(params.get("threadId"), settings, sequence=seq)
            carried = self._carry_policy_epoch(params.get("threadId"), seq)
            self.check_configuration(settings.get("model"), settings.get("effort"),
                                     root=params.get("threadId") == self.authority.root_thread_id)
            if not carried:
                raise ScopeDenied("the official settings epoch could not be retained; inspect its failure diagnostic")
        elif method == "thread/status/changed":
            status = params.get("status") or {}
            if status.get("type") == "systemError" or (status.get("type") == "notLoaded"
                    and thread_id in self.authority.active_turns):
                raise ScopeDenied("the official native runtime lost an active turn or reported a system error")
        elif method == "turn/started":
            turn = params.get("turn") or {}
            self._check_worker_capacity(thread_id)
            self.turns += 1
            if self.turns > self.max_turns:
                raise ScopeDenied("the explicit native turn budget is exhausted")
            self.authority.start_turn(params.get("threadId"), turn.get("id"), sequence=seq)
        elif method == "turn/completed":
            thread, turn = params.get("threadId"), params.get("turn") or {}
            self.authority.finish_turn(thread, turn.get("id"), sequence=seq)
            if thread == self.authority.root_thread_id:
                self.root_finished = True
                self.last_root_turn_id = turn.get("id") if turn.get("status") == "completed" else None
            self.emit({"kind": "turn_completed", "event_seq": seq, "thread_id": thread,
                       "turn_id": turn.get("id"), "status": turn.get("status")})
        elif method == "model/rerouted":
            self.authority.model_rerouted(params.get("threadId"), params.get("turnId"), params.get("toModel"), sequence=seq)
            self._carry_policy_epoch(params.get("threadId"), seq)
            raise ScopeDenied("the official Host rerouted a model; stop instead of continuing on a fallback")
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "dynamicToolCall":
                key = (params.get("threadId"), item.get("id"))
                pending = self.pending_effects.get(key)
                if pending is not None:
                    pending["delivered"] = True
                self.emit({"kind": "tool_result_delivered", "event_seq": seq,
                           "thread_id": params.get("threadId"), "call_id": item.get("id")})
            elif item.get("type") == "agentMessage":
                self._capture_root_final(params, seq)
        elif method == "warning" and thread_id == self.authority.root_thread_id:
            self._root_warning(received, before_input=False)
        elif method.startswith(("mcpServer/", "hook/")):
            raise ScopeDenied("unexpected extra MCP/hook runtime in the dynamic-tool-only connection")
        elif method == "error":
            self._host_error(received)
        # Other conversation content is not retained. Only the explicitly
        # selected Root final answer uses the separate user-output result slot.

    def on_server_request(self, received: ReceivedMessage) -> dict[str, Any]:
        message = received.message
        if message.get("method") != "item/tool/call":
            raise ScopeDenied("this client never automatically grants permissions/auth/user decisions")
        params = message.get("params")
        raw = params if isinstance(params, Mapping) else {}
        safe_text = lambda value: value if isinstance(value, str) and len(value) <= 1000 else None
        try:
            args_sha = ids.sha256_of_obj(dict(raw["arguments"])) if isinstance(raw.get("arguments"), Mapping) else None
        except (TypeError, ValueError, RecursionError):
            args_sha = None
        self.emit({"kind": "tool_callback_received", "event_seq": received.receive_seq,
            "admission": "NOT_YET_CHECKED", "thread_id": safe_text(raw.get("threadId")),
            "turn_id": safe_text(raw.get("turnId")), "call_id": safe_text(raw.get("callId")),
            "tool": safe_text(raw.get("tool")), "namespace": safe_text(raw.get("namespace")),
            "arguments_sha256": args_sha})
        self.callback_attempts += 1
        if self.callback_attempts > self.max_callbacks:
            raise ScopeDenied(CALLBACK_LIMIT_MESSAGE)
        if not isinstance(params, Mapping) or params.get("tool") not in self.allowed:
            raise ScopeDenied("the dynamic callback is not an existing enabled GX tool")
        if not self.allow_mutations and contracts.any_tool_spec(params["tool"]).get("mutating"):
            raise ScopeDenied("this approved native launch permits only read-only GX callbacks")
        thread_id = params.get("threadId")
        if (self.metadata_needed(thread_id) or thread_id in self.discoveries
                or thread_id in self.metadata_refusals
                or received.receive_seq <= self.metadata_floors.get(thread_id, 0)):
            if self.authority.root_thread_id is None or thread_id == self.authority.root_thread_id or self.unknown or self.pending_effects:
                raise ScopeDenied("native callback has no strictly earlier admitted current metadata")
            return self.metadata_callback_denial(safe_text(thread_id), safe_text(params.get("turnId")), received.receive_seq)
        call = self.authority.make_call(params, sequence=received.receive_seq)
        try:
            mutating = bool(contracts.any_tool_spec(call.tool_name).get("mutating"))
            if mutating and self.settled_project_feedback:
                error = {"code": "SCOPE_DENIED", "side_effect_status": "NONE", "message":
                         "This project is settled. Answer the user from its existing records. A new experiment needs a separate commission."}
                return {"contentItems": [{"type": "inputText", "text": json.dumps({"ok": False, "error": error})}],
                        "success": False}
            if mutating and not self.authority.recovery_only:
                self.pending_effects[(call.thread_id, call.call_id)] = {
                    "thread_id": call.thread_id, "call_id": call.call_id, "tool": call.tool_name,
                    "arguments_sha256": call.arguments_sha256, "request_id": None}
                if call.tool_name in FACADE_EVENTS:
                    self.pending_effects[(call.thread_id, call.call_id)].update(
                        native_call_sha256=call.evidence_sha256, task_id=params["arguments"].get("task_id"))
                self.emit({"kind": "tool_callback_pending", "event_seq": received.receive_seq,
                           "thread_id": call.thread_id, "call_id": call.call_id, "tool": call.tool_name})
            arguments = params["arguments"]
            with metrics.identity(run_id=arguments.get("run_id"), task_id=arguments.get("task_id"),
                    attempt=arguments.get("attempt"), thread_id=call.thread_id, turn_id=call.turn_id,
                    operation_id=arguments.get("operation_id") or arguments.get("idempotency_key")):
                dispatch_started = time.monotonic_ns()
                response = api.dispatch(call.tool_name, arguments, principal=call.principal,
                    conn=self.conn, transport=native.TRANSPORT, native_call=call)
                metrics.record("gx_tool_dispatch", started=dispatch_started, tool=call.tool_name,
                               run_id=response.get("run_id") or arguments.get("run_id"), call_id=call.call_id,
                               error_code=(response.get("error") or {}).get("code"),
                               ok=response.get("ok"))
            # A read-only recovery denial is not a new result for an earlier
            # uncertain callback, even if the Host presents the same call ID.
            pending = None if self.authority.recovery_only else self.pending_effects.get((call.thread_id, call.call_id))
            effect_status = _response_effect_status(response)
            if pending is not None:
                pending["request_id"] = response.get("request_id")
                pending["side_effect_status"] = effect_status
                if effect_status in ("NONE", "APPLIED", "ROLLED_BACK"):
                    self.pending_effects.pop((call.thread_id, call.call_id), None)
                elif effect_status == "UNKNOWN":
                    pending["outcome_unknown"] = True
            if effect_status == "UNKNOWN":
                self.unknown = True
            self.calls += 1
            self.emit({"kind": "GX_call", "event_seq": received.receive_seq,
                       "thread_id": call.thread_id, "turn_id": call.turn_id, "call_id": call.call_id,
                       "tool": call.tool_name, "arguments_sha256": call.arguments_sha256,
                       "request_id": response.get("request_id"), "run_id": response.get("run_id"),
                       "ok": response.get("ok"), "error_code": (response.get("error") or {}).get("code"),
                       "side_effect_status": effect_status,
                       "native_call": call.as_evidence(),
                       "evidence_sha256": call.evidence_sha256})
            if call.tool_name in {"gx_team_status", "gx_team_manage"} and self.tool_profile == "native_v2":
                workers = native_spawn.worker_directory(self.conn, self.authority)
                response["native_context"] = {"caller_subject": call.principal.subject,
                    "registered_workers": native_spawn.visible_workers(workers, call)[:32]}
            content = [{"type": "inputText", "text": json.dumps(response, ensure_ascii=False, separators=(",", ":"))}]
            success = bool(response.get("ok"))
            if call.tool_name == "gx_team_status":
                is_root = call.thread_id == call.root_thread_id
                threads = self.authority.threads.items() if is_root else [(call.thread_id, self.authority.threads[call.thread_id])]
                roster = [{"thread_id": thread_id, "backend_subject": self.authority.principal_for(thread_id).subject,
                           "parent_thread_id": facts["parent_thread_id"], "session_id": facts["session_id"],
                           "fork_origin_known": facts["fork_origin_known"], "forked_from_id": facts["forked_from_id"],
                           "fresh_context": (facts["forked_from_id"] is None) if facts["fork_origin_known"] else None,
                           "model": facts.get("model"), "effort": facts.get("effort")}
                          for thread_id, facts in threads if thread_id not in self.metadata_refusals]
                projection = {"native_context": {"purpose": "CORRELATION_ONLY",
                    "source": native.SOURCE, "observation_source": native.OBSERVATION_SOURCE,
                    "model_authority": native.MODEL_AUTHORITY, "upstream_tier_independently_verified": False,
                    "project_ref": self.authority.project_ref,
                    "caller_subject": call.principal.subject, "caller_thread_id": call.thread_id,
                    "registered_workers": roster,
                    "metadata_refusals": list(self.metadata_refusals.values())[:32] if is_root else [],
                    "note": "IDs name registered identities for ordinary enrollment/assignment; request fields never grant identity or HUMAN authority."}}
                content.append({"type": "inputText", "text": json.dumps(projection, ensure_ascii=False, separators=(",", ":"))})
            return {"contentItems": content, "success": success}
        finally:
            self.authority.complete(call)


def _definite_steer_refusal(error: AppServerResponseError) -> bool:
    # Official invalid request/method/parameters, or a rejected active-turn
    # precondition. An internal error alone gives no evidence of nondelivery.
    if error.code in (-32600, -32601, -32602):
        return True
    data = error.data if isinstance(error.data, Mapping) else {}
    info = data.get("codexErrorInfo", data)
    return (isinstance(info, Mapping) and "activeTurnNotSteerable" in info
            or error.message == "no active turn to steer"
            or error.message.startswith("expected active turn id ") and " but found " in error.message)


def _project_instruction(work: Mapping[str, Any]) -> str:
    # The durable owner retains the complete queue. Keep individual turn inputs
    # small even for projects with many waiting branches.
    if work["status"] == "TERMINAL":
        return ("The original project has settled with GX run outcomes "
                + json.dumps(work["run_statuses"]) + ". Answer the queued user additions from the existing "
                "evidence. Keep these outcomes and runs closed. New experiments require a separate commission.")
    if work["status"] != "READY":
        return (f"The original GX work is {work['status']}. Review the queued user additions on this same "
                "project. Preserve held tasks, approvals and outstanding Jobs. Report what the new information "
                "resolves and any remaining blocker. " + "\n".join(work.get("blockers", [])[:20]))
    return ("Continue the assigned project using GX's current work. Read the relevant "
            "run status, advance the existing tasks and reuse their results. Choose "
            "the order and workers according to the project objective. "
            "The first 20 available actions are shown below.\n" + "\n".join(work["actions"][:20]))


def _collaboration_instruction(mode: str, max_workers: int) -> str:
    if mode == "auto":
        return "The user selected automatic collaboration. Choose staffing and delegation for the current work."
    from ..orchestration.packages import native_guidance
    return (
        "The user selected module-leads collaboration. As Root, choose the natural modules, their scope, "
        "inputs and expected deliverables. Reuse the existing work and GX teams. Designate a native lead for "
        "each module, give its GX team the question, and delegate management with gx_team_manage leader and "
        "grant for members, assign and message. Include the relevant task IDs as they become available. "
        "Each lead uses native spawn_agent or followup_task to dispatch its own workers and assigns READY GX "
        "tasks to their actual subjects. Workers submit their results through GX and send references to their "
        "lead. Each lead examines the results, reports agreement, conflicts and gaps, and hands Root its "
        "synthesis with original GX result references. Count active leads and workers together within the "
        f"{max_workers}-worker launch limit. Leave room for workers and use waves when needed.\n\n"
        "Root decides what to follow up with the original leads, when independent review is needed, and "
        "when the objective is settled. "
        + native_guidance(mode)
    )


def run_session(prompt: str, *, state: NativeState, conn: sqlite3.Connection, project_ref: str,
                max_wall_seconds: int, max_turns: int, max_workers: int = 2,
                client_factory=None, event_sink=None, tool_policy: Mapping | None = None,
                native_execution: Mapping | None = None, public_output_sink=None,
                project_continuation: bool = False, project_run_id: str | None = None,
                collaboration: str | None = None, runtime_backend: str | None = None) -> dict[str, Any]:
    """Called only after the entry's one state/launch guard and store open.

    The factory is an internal software-fixture seam, not a user CLI/wire option.
    Optional follow-ups start only after a settled successful turn and new work.
    """
    if not isinstance(state, NativeState) or state.release_id != RELEASE_ID:
        raise ScopeDenied("the native session requires this release's guarded state")
    from ..native_cli import COLLABORATION_MODES, validate_native_execution
    if collaboration is not None and (collaboration not in COLLABORATION_MODES or not project_continuation):
        raise ScopeDenied("a collaboration mode must be selected for project continuation")
    native_execution = validate_native_execution(native_execution, state)
    execution_cwd = native_execution["workspace"] if native_execution else str(contracts.HARNESS_ROOT)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 256 * 1024:
        raise ScopeDenied("the native prompt is missing or exceeds its input bound")
    project_result = None
    if project_continuation:
        from ..project_work import next_project_work, project_objective_key
        work = next_project_work(conn, project_ref, project_run_id,
                                 after_root_turn=(state.directory / SESSION_FILE).is_file())
        if project_run_id is not None and project_run_id not in work["run_ids"]:
            raise ScopeDenied("the selected project run does not exist in this project")
        if work["status"] == "EMPTY":
            prompt += ("\n\nWhen creating the GX run that owns this entire research question, "
                       "use idempotency_key=" + project_objective_key(project_ref) +
                       ". Select a workflow whose completion evidence answers the question. "
                       "Keep distinct keys for individual methods and reuse existing runs and results.")
        elif work["status"] == "READY":
            prompt += "\n\n" + _project_instruction(work)
    for name, value in (("max_wall_seconds", max_wall_seconds), ("max_turns", max_turns), ("max_workers", max_workers)):
        if type(value) is not int or value < 1:
            raise ScopeDenied(f"{name} must be an explicit positive configured bound")
    config.check_holds("orchestration")
    document, policy_sha = host_policy.native_codex_policy(state.directory)
    # Request the exact model and Root effort from the current Host-scoped policy.
    from . import policy
    model = policy.pinned_model(document)
    effort = document["native_coordinator_observation"]["default_effort"]
    if effort not in document["native_coordinator_observation"]["accepted_efforts"]:
        raise ScopeDenied("the installed Codex policy does not admit the root effort")
    router = NativeRouter(conn, project_ref=project_ref, max_turns=max_turns, max_workers=max_workers,
                          tool_policy=tool_policy, public_output_sink=public_output_sink, codex_policy=document)
    catalog_without_files = None
    if native_execution:
        from .. import schema
        catalog_without_files = ids.sha256_of_obj(router.tool_catalog)
        for tool in router.tool_catalog:
            if tool["name"] == "gx_artifact_get":
                tool["inputSchema"] = schema.tool_input_schema("gx_artifact_get", native_workspace=True)
    skill_metadata, canonical_guidance = _root_skill_inputs(tool_profile=router.tool_profile)
    if native_execution:
        prompt += ("\n\nTrusted local test execution is explicitly enabled for this launch. "
            "Author local working files and run local commands/tests in " + execution_cwd + ". "
            "GX publication also permits staging exact bytes of prepared inputs "
            "into the actual run inbox returned by GX, then publishing through the existing GX tools; "
            "this needs no new GO or approval. Existing GX owners manage their Job outputs. "
            "Do not edit source/shared trees or databases manually. "
            "Give every worker this workspace and GX staging guidance. This is an instruction constraint; "
            "danger-full-access supplies no OS filesystem isolation. Keep the inherited Clash proxy routes.")
    catalog_sha = ids.sha256_of_obj(router.tool_catalog)
    previous = _session_descriptor(state, project_ref, policy_sha, catalog_sha, native_execution,
                                   tool_profile=router.tool_profile, catalog_without_files=catalog_without_files)
    from . import source_host_adapter
    backend = source_host_adapter.select_backend(previous, runtime_backend)
    source_host = backend == source_host_adapter.NAME
    if source_host:
        runtime_store = str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve())
        if previous and previous.get("runtime_store") != runtime_store:
            raise ScopeDenied("source host resume requires the original Codex thread store")
        source_binary = source_host_adapter.binary_path()
    # A resumed Root keeps the dynamic schema it received from thread/start, even
    # when unrelated tools in the installed dispatcher change in a later release.
    if (catalog_without_files is not None and catalog_sha != catalog_without_files
            and (previous is None or previous.get("workspace_artifact_delivery") is True
                 or previous["catalog_sha256"] == catalog_sha)):
        router.authority.artifact_workspace = Path(execution_cwd)
    warm_turn = None
    if (previous and not previous["uncertain"]
            and not previous["pending_effects"] and previous.get("guidance_procedures") == skill_metadata):
        warm_turn = previous.get("guidance_turn")
    descriptor = {"schema": "gx.native_session/1", "source": native.SOURCE,
        "release_id": state.release_id, "contract_sha256": state.contract_sha256,
        "project_ref": project_ref, "policy_sha256": policy_sha,
        # Codex 0.169.0 cannot install dynamicTools during thread/resume.
        # Preserve the catalog actually delivered to this Root separately from
        # the current installed dispatcher/API; do not claim a reinstallation.
        "catalog_sha256": previous["catalog_sha256"] if previous else catalog_sha,
        "installed_catalog_sha256": catalog_sha, "host_version": "0.169.0",
        "tool_profile": router.tool_profile}
    descriptor["runtime_backend"] = backend
    if source_host:
        descriptor["runtime_store"] = runtime_store
    if native_execution:
        descriptor["native_execution"] = native_execution
    if router.authority.artifact_workspace is not None:
        descriptor["workspace_artifact_delivery"] = True
    last_saved = None

    def save_session_state(*, cleanup_pending=True, uncertain=True):
        nonlocal last_saved
        # Pending responses acquire their request ID and outcome in place. Keep
        # a snapshot of the last durable values under the native session lock.
        current = {**descriptor,
            "root_thread_id": router.authority.root_thread_id or (previous or {}).get("root_thread_id"),
            "session_id": router.authority.session_id or (previous or {}).get("session_id"),
            "uncertain": uncertain, "cleanup_pending": cleanup_pending,
            "pending_effects": [effect.copy() for effect in router.pending_effects.values()]}
        if current != last_saved:
            last_saved = None  # A failed directory sync may follow a successful rename.
            _save_descriptor(state, current)
            last_saved = current

    def checkpoint(event):
        if event_sink:
            event_sink(event)
        if router.authority.root_thread_id:
            save_session_state()

    router.event_sink = checkpoint
    def root_turn_input(message):
        if collaboration is not None and work["status"] != "TERMINAL":
            message += "\n\n" + _collaboration_instruction(collaboration, max_workers)
        if floor := document.get("worker_effort_floor"):
            message += (f"\n\nUse the project's configured {model} GX role for every native spawn, including "
                        f"module leads and their workers. Keep its configured effort of {floor} or higher. "
                        "Include this requirement in each lead's dispatch instructions.")
        if router.guidance_in_context:
            guidance = ("Continue using the shipped GX procedures already supplied to this Root. "
                        "Read the installed originals as needed:\n" + "\n".join(row["path"] for row in skill_metadata))
        else:
            guidance = canonical_guidance
        router.guidance_in_context = True
        return [{"type": "text", "text": message + "\n\n" + guidance}]

    router.activity_cwd = execution_cwd
    if previous and (previous["contract_sha256"] != state.contract_sha256
            or previous.get("installed_catalog_sha256", previous["catalog_sha256"]) != catalog_sha):
        router.emit({"kind": "native_api_maintenance", "root_thread_id": previous["root_thread_id"],
            "previous_contract_sha256": previous["contract_sha256"],
            "contract_sha256": state.contract_sha256,
            "previous_installed_catalog_sha256": previous.get("installed_catalog_sha256", previous["catalog_sha256"]),
            "installed_catalog_sha256": catalog_sha, "delivered_catalog_sha256": previous["catalog_sha256"],
            "disposition": ("ORIGINAL_HOST_CATALOG_AND_PENDING_EFFECTS_RETAINED" if previous["pending_effects"]
                            else "CLEAN_OWNED_ROOT_RESUME_ORIGINAL_HOST_CATALOG_RETAINED")})
    # A protocol failure is historical diagnostics, not an unresolved GX write.
    # Actual pending effects retain their original read/reconcile-only barrier.
    # No callback can run before fresh official same-Root metadata is admitted.
    router.authority.recovery_only = bool(previous and previous["pending_effects"])
    if previous and router.authority.recovery_only:
        for item in previous["pending_effects"]:
            router.pending_effects[(item["thread_id"], item["call_id"])] = dict(item)

    def reconcile_effects(source):
        for key, receipt in known_effect_receipts(state.directory, source, model).items():
            item = router.pending_effects.pop(key)
            router.emit({"kind": "native_business_completion_reconciled", **receipt,
                "thread_id": item["thread_id"], "call_id": item["call_id"], "tool": item["tool"],
                "arguments_sha256": item["arguments_sha256"]})

    if client_factory is None:
        _proxy_preflight()
    argv = ["codex", "--disable", "apps", "--disable", "hooks", "--disable", "plugins",
            "--enable", "multi_agent_v2", "--enable", "multi_agent_v2_dynamic_tools",
            "-c", f"model={json.dumps(model)}",
            "-c", f"model_reasoning_effort={json.dumps(effort)}",
            *_project_role_overrides(state, document),
            "-c", 'experimental_thread_store={type="local"}',
            "-c", "suppress_unstable_features_warning=true",
            "-c", "skills.include_instructions=false",
            "-c", f"agents.max_concurrent_threads_per_session={max_workers + 1}", "app-server", "--listen", "stdio://"]
    if source_host:
        argv = source_host_adapter.launch_argv(source_binary, argv, state.directory / "codex-outcomes")
    factory = client_factory or AppServerClient
    # The same descriptor is the durable UNKNOWN barrier. An absent start
    # response or subsequent disk failure must not look like fresh state.
    # Failure here precedes even factory construction, let alone a Popen/RPC.
    save_session_state()
    client = factory(argv=argv, cwd=execution_cwd if native_execution else str(state.directory), env=os.environ.copy(),
        on_notification=router.on_notification, on_server_request=router.on_server_request,
        max_inbound_message_bytes=64 * 1024 * 1024,
        default_timeout=min(30, max_wall_seconds))
    router.client = client
    started = time.monotonic()
    deadline = started + max_wall_seconds
    status, failure, cleanup_failure = "OUTCOME_UNKNOWN", None, None
    def remaining(*, whole_session=False):
        value = deadline - time.monotonic()
        if value <= 0:
            raise AppServerTimeout("the configured native session wall budget expired")
        return value if whole_session else min(30, value)

    def resolve_metadata():
        while True:
            thread_id = next((identity for identity, pending in router.discoveries.items()
                              if pending["turn_start_seq"] is not None), None)
            if thread_id is None:
                return
            pending = router.discoveries[thread_id]
            client.pause_callbacks()
            metadata_id = client.send("thread/read", {"threadId": thread_id, "includeTurns": False}, timeout=remaining())
            router.emit({"kind": "metadata_lookup_requested", "thread_id": thread_id,
                "request_id": metadata_id, "original_wire_seq": router.discoveries[thread_id]["first_wire_seq"],
                "disposition": "ONE_PUBLIC_LOOKUP_NO_BUSINESS_ADMISSION"})
            try:
                response = client.wait(metadata_id, timeout=remaining())
                received = client.response_received(metadata_id)
                metadata_seq = received.receive_seq
                try:
                    facts = router.stage_metadata(thread_id, response, metadata_seq)
                except ScopeDenied:
                    if thread_id == router.authority.root_thread_id or router.unknown or router.pending_effects:
                        raise
                    router.refuse_metadata_actor(thread_id, metadata_seq)
                    response = None
                    router.replaying_metadata = True
                    try:
                        client.resume_callbacks(timeout=remaining())
                    finally:
                        router.replaying_metadata = False
                    continue
                if router.begin_materialization(thread_id, response, facts, metadata_seq):
                    response = None
                    metadata_id = client.send("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=remaining())
                    router.emit({"kind": "metadata_materialization_requested", "thread_id": thread_id,
                        "request_id": metadata_id, "initial_metadata_seq": metadata_seq})
                    response = client.wait(metadata_id, timeout=remaining())
                    persisted = client.response_received(metadata_id).receive_seq
                    router.observe_materialization(thread_id, response, persisted)
                    response = None
                    router.emit({"kind": "metadata_materialization_acknowledged", "thread_id": thread_id,
                        "request_id": metadata_id, "response_seq": persisted,
                        "scope": "NATIVE_OWNED_PERSISTENCE_NOT_HISTORY_AUTHORITY"})
                    metadata_id = client.send("thread/read", {"threadId": thread_id, "includeTurns": False}, timeout=remaining())
                    router.emit({"kind": "metadata_current_after_materialization_requested", "thread_id": thread_id,
                        "request_id": metadata_id, "persistence_seq": persisted})
                    response = client.wait(metadata_id, timeout=remaining())
                    metadata_seq = client.response_received(metadata_id).receive_seq
                    facts = router.stage_metadata(thread_id, response, metadata_seq)
                response = None
                # Do not advance authority to metadata_seq before replaying the
                # older Root rows. Their actual wire sequences stay unchanged.
                router.replaying_metadata = True
                try:
                    client.resume_callbacks(timeout=remaining())
                finally:
                    router.replaying_metadata = False
                if router.discoveries.get(thread_id) is not pending:
                    # Closure or refusal retired the lookup while its response was in flight.
                    continue
                router.admit_metadata(thread_id, facts, metadata_seq)
            except (AppServerError, GxError, OSError) as exc:
                refusal = {"kind": "metadata_lookup_refused", "thread_id": thread_id,
                    "request_id": metadata_id, "failure_type": type(exc).__name__,
                    "disposition": "UNKNOWN_NO_RETRY_NO_HISTORICAL_BACKFILL"}
                capsule = _metadata_rpc_error(exc, thread_id)
                if capsule is not None:
                    refusal["rpc_error"] = capsule
                router.emit(refusal)
                raise

    def await_response(request_id):
        while True:
            try:
                return client.wait(request_id, timeout=remaining())
            except AppServerDeliveryPaused:
                resolve_metadata()

    def request(method, params):
        request_id = client.send(method, params, timeout=remaining())
        return await_response(request_id)

    try:
        client.start()
        descriptor["host_process_group"] = host_process_group(getattr(client, "transport", None))
        request("initialize", {"clientInfo": {"name": "gx-native", "version": "1"},
            "capabilities": {"experimentalApi": True}})
        client.notify("initialized", {})
        # Empty -c tables recursively merge and cannot disable lower layers.
        # Read the owned process's effective config before creating any thread,
        # then pass literal nested false values through its official override.
        config_id = client.send("config/read", {"cwd": execution_cwd, "includeLayers": False}, timeout=remaining())
        effective = await_response(config_id)
        disabled_mcp = _disabled_mcp_servers(effective)
        mode = effective["config"].get("experimental_thread_store")
        router.initial_local_store = isinstance(mode, Mapping) and dict(mode) == {"type": "local"}
        effective = None  # no raw config is emitted to model, event sink or disk
        router.emit({"kind": "mcp_disable_plan", "event_seq": client.response_received(config_id).receive_seq,
                     "server_names": sorted(disabled_mcp), "literal_override_count": len(disabled_mcp),
                     "initial_thread_store": "local" if router.initial_local_store else "unknown",
                     "scope": "same_process_effective_config_before_thread_creation"})
        # Complete, exact procedures are ordinary Root input. Catalog discovery
        # and typed Skill activation are optional and add no business authority.
        router.emit({"kind": "root_skill_catalog_status", "required": False,
            "disposition": "NOT_QUERIED_CANONICAL_GUIDANCE_SUFFICIENT"})
        method = "thread/resume" if previous else "thread/start"
        params = {"model": model, "cwd": execution_cwd,
            "sandbox": "danger-full-access" if native_execution else "read-only",
            "approvalPolicy": "never", "allowProviderModelFallback": False,
            "config": {"model_reasoning_effort": effort, "mcp_servers": disabled_mcp}}
        environments = [{"environmentId": "local", "cwd": execution_cwd,
                         "runtimeWorkspaceRoots": [execution_cwd]}]
        # ThreadResumeParams has no environments field. The read-only
        # environment is refreshed through the supported turn/start override.
        if not previous:
            params["environments"] = environments
        if native_execution:
            params["runtimeWorkspaceRoots"] = [execution_cwd]
            router.emit({"kind": "native_execution_profile", "profile": native_execution["profile"],
                "cwd": execution_cwd, "sandbox": "danger-full-access", "approval_policy": "never",
                "filesystem_isolation": False, "workspace_scope": "INSTRUCTION_ONLY"})
        else:
            router.emit({"kind": "native_readonly_environment_profile", "profile": "core_local_readonly",
                "environment_id": "local", "cwd": execution_cwd, "sandbox": "read-only",
                "approval_policy": "never", "read_scope_is_os_allowlist": False,
                "budget_isolation": False, "disposition": "REQUESTED_NOT_SOURCE_READ_PROOF"})
        if previous:
            params.update(threadId=previous["root_thread_id"], excludeTurns=True)
            if warm_turn:
                # Ask for only the latest turn ID/status on the existing resume.
                # A missing or different turn gets the complete procedures.
                params["initialTurnsPage"] = {"limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"}
        else:
            params.update(ephemeral=False, dynamicTools=router.tool_catalog)
        connection_started = time.monotonic_ns()
        request_id = client.send(method, params, timeout=remaining())
        response = await_response(request_id)
        received = client.response_received(request_id)
        metrics.record("session_resume" if previous else "session_create", started=connection_started,
                       run_id=project_run_id, thread_id=(response.get("thread") or {}).get("id"))
        if previous and ((response.get("thread") or {}).get("id") != previous["root_thread_id"]
                         or (response.get("thread") or {}).get("sessionId") != previous["session_id"]):
            raise ScopeDenied("official resume returned another root/session; do not create a replacement")
        if warm_turn:
            page = response.get("initialTurnsPage")
            turns = page.get("data") if isinstance(page, Mapping) else None
            router.guidance_in_context = (isinstance(turns, list) and len(turns) == 1
                and isinstance(turns[0], Mapping) and turns[0].get("status") == "completed"
                and turns[0].get("id") == warm_turn)
        router.register_root(response, received.receive_seq)
        router.resumed_root = bool(previous)
        if response.get("model") != model or response.get("reasoningEffort") != effort:
            raise ScopeDenied("the Host-applied root configuration differs from the admitted policy")
        if previous:
            spawn_receipts = native_spawn.reconcile(conn, project_ref=project_ref,
                session_id=router.authority.session_id,
                list_items=lambda page: request("thread/items/list", page))
            native_spawn.recover_legacy_paths(conn, router.authority,
                lambda thread_id: request("thread/read", {"threadId": thread_id, "includeTurns": False}))
            if spawn_receipts:
                prompt += ("\n\nNative spawn recovery. Reuse the recovered children. Retain unknown operations "
                           "for reconciliation and continue independent work. Do not repeat these spawns.\n"
                           + json.dumps([{key: row[key] for key in ("operation_id", "parent_thread_id", "turn_id",
                                                                     "item_id", "child_thread_id", "status")}
                                         for row in spawn_receipts]))
        if previous and router.authority.recovery_only:
            reconcile_effects(previous)
            router.authority.recovery_only = bool(router.pending_effects)
        if previous and previous["uncertain"] and not router.authority.recovery_only:
            router.emit({"kind": "native_protocol_recovery", "event_seq": received.receive_seq,
                "root_thread_id": router.authority.root_thread_id, "session_id": router.authority.session_id,
                "previous_protocol_status": "OUTCOME_UNKNOWN", "pending_effects": 0,
                "disposition": "SAME_ROOT_CURRENT_CONFIGURATION_VERIFIED_CONTINUE"})
        guidance_delivery = ("UNCHANGED_IN_RESUMED_ROOT" if router.guidance_in_context
                             else "FULL_VALIDATED_ORDINARY_TEXT_INPUT")
        turn_params = {"threadId": router.authority.root_thread_id,
            "input": root_turn_input(prompt), "model": model, "effort": effort}
        turn_params.update(cwd=execution_cwd, runtimeWorkspaceRoots=[execution_cwd],
                           approvalPolicy="never", environments=environments)
        root_turns = 1
        previous_counts = (0, 0, time.monotonic())
        def reconcile_additions():
            try:
                project_messages.reconcile(state.directory, router.authority.root_thread_id,
                    lambda page: request("thread/turns/list", page),
                    lambda page: request("thread/items/list", page))
            except (AppServerResponseError, sqlite3.Error, OSError):
                router.emit({"kind": "web_message_receipt_unavailable"})

        if previous and project_continuation and not router.authority.recovery_only:
            reconcile_additions()

        def take_additions(*, steer_turn=None):
            if project_continuation and not router.authority.recovery_only:
                try:
                    return project_messages.take_for_turn(state.directory, router.authority.root_thread_id,
                                                          steer_turn=steer_turn)
                except (OSError, sqlite3.Error):
                    router.emit({"kind": "web_message_inbox_temporarily_unavailable"})
                    return None
            return []

        def start_root_turn(additions):
            inputs = turn_params["input"]
            router.settled_project_feedback = project_continuation and work["status"] == "TERMINAL"
            if router.settled_project_feedback or (additions and work["status"] not in ("READY", "EMPTY")):
                inputs = [*inputs, {"type": "text", "text": _project_instruction(work)}]
            params = {**turn_params, "input": project_messages.attach_to_input(inputs, additions)}
            request_id = client.send("turn/start", params, timeout=remaining())
            metrics.record("root_turn_requested", run_id=project_run_id, thread_id=router.authority.root_thread_id,
                           work_status=work["status"] if project_continuation else None,
                           has_feedback=bool(additions), request_id=request_id)
            return request_id

        def record_delivery(rows, phase, turn):
            if rows and turn:
                try:
                    project_messages.update_delivery(state.directory, rows, phase, turn)
                except (OSError, sqlite3.Error):
                    router.emit({"kind": "web_message_receipt_unavailable", "turn_id": turn})

        def wait_for_turn(turn, delivered):
            root = router.authority.root_thread_id
            can_steer = bool(project_continuation and turn and not router.authority.recovery_only)
            pending = None
            uncertain = False
            while not router.finished() or pending:
                if pending and (router.finished() or client.response_received(pending[0])):
                    request_id, row = pending
                    pending = None
                    try:
                        reply = await_response(request_id)
                    except AppServerResponseError as exc:
                        if _definite_steer_refusal(exc):
                            try:
                                project_messages.return_refused_steer(state.directory, row["id"], root, turn)
                            except (OSError, sqlite3.Error):
                                router.emit({"kind": "web_message_receipt_unavailable", "turn_id": turn})
                        else:
                            uncertain = True
                        can_steer = False
                    else:
                        if isinstance(reply, Mapping) and reply.get("turnId") == turn:
                            record_delivery([row], "received", turn)
                            delivered.append(row)
                        else:
                            uncertain, can_steer = True, False
                    continue
                try:
                    event = client.run_until(lambda _: router.finished() or
                        (pending is not None and client.response_received(pending[0]) is not None),
                        timeout=remaining(whole_session=True),
                        poll_interval=INBOX_POLL_SECONDS if can_steer else None)
                except AppServerDeliveryPaused:
                    resolve_metadata()
                    continue
                if event is not None or pending or router.authority.active_turns.get(root) != turn:
                    continue
                if router.unknown or router.authority.recovery_only:
                    can_steer = False
                    continue
                try:
                    if not project_messages.has_queued(state.directory):
                        continue
                    # Inputs which raced with settlement get the existing
                    # read-only clarification turn after this one finishes.
                    if next_project_work(conn, project_ref, project_run_id, after_root_turn=True)["status"] == "TERMINAL":
                        can_steer = False
                        continue
                except (OSError, sqlite3.Error):
                    router.emit({"kind": "web_message_inbox_temporarily_unavailable"})
                    can_steer = False
                    continue
                rows = take_additions(steer_turn=turn)
                if rows is None:
                    can_steer = False
                elif rows:
                    params = {"threadId": root, "expectedTurnId": turn, "clientUserMessageId": rows[0]["id"],
                              "input": project_messages.attach_to_input([], rows)}
                    pending = (client.send("turn/steer", params, timeout=remaining()), rows[0])
            if uncertain:
                reconcile_additions()

        def project_handoff(current, reason, count):
            return {"reason": reason, "actions": current["actions"], "run_ids": current["run_ids"],
                    "run_statuses": current["run_statuses"], "root_turns": count}

        additions = take_additions()
        if additions is None and project_continuation and work["status"] == "TERMINAL":
            project_result = project_handoff(work, "INBOX_UNAVAILABLE", 0)
            status, turn_request = "COMPLETED", None
        else:
            turn_request = start_root_turn(additions or [])
            router.emit({"kind": "root_skill_inputs_requested", "request_id": turn_request,
                "thread_id": router.authority.root_thread_id, "skills": skill_metadata,
                "disposition": "REQUESTED_ORDINARY_GUIDANCE",
                "canonical_guidance": {"delivery": guidance_delivery,
                    "core_skill_activation": "NOT_REQUIRED_NOT_PROVEN", "procedures": skill_metadata},
                "automatic_skill_catalog_instructions": "SUPPRESSION_REQUESTED_OWN_PROCESS_ONLY"})
        while turn_request is not None:
            response = await_response(turn_request)
            acknowledged = response.get("turn") if isinstance(response, Mapping) else None
            expected_turn = acknowledged.get("id") if isinstance(acknowledged, Mapping) else None
            delivered = list(additions or [])
            record_delivery(delivered, "received", expected_turn)
            wait_for_turn(expected_turn, delivered)
            if (router.pending_effects and not router.authority.recovery_only and not router.unknown
                    and expected_turn and router.last_root_turn_id == expected_turn):
                # A later exact replay can settle a partial start on its original
                # run during this same turn, before the native handoff.
                reconcile_effects({**descriptor, "root_thread_id": router.authority.root_thread_id,
                                   "session_id": router.authority.session_id,
                                   "pending_effects": list(router.pending_effects.values())})
            settled_status = ("BLOCKED_RECOVERY_REQUIRED" if router.authority.recovery_only else
                              "OUTCOME_UNKNOWN" if router.unknown or router.pending_effects else "COMPLETED")
            if not project_continuation or settled_status != "COMPLETED":
                status = settled_status
                break
            if not router.last_root_turn_id or expected_turn != router.last_root_turn_id:
                raise ScopeDenied("project continuation needs the requested Root turn to complete successfully")
            record_delivery(delivered, "turn_completed", router.last_root_turn_id)

            updated = next_project_work(conn, project_ref, project_run_id, after_root_turn=True)
            reason = updated["status"]
            now = time.monotonic()
            if reason == "READY" and updated["actions"] == work["actions"] and updated["run_statuses"] == work["run_statuses"]:
                reason = "NO_PROGRESS"
            metrics.record("root_work_after_turn", run_id=project_run_id, thread_id=router.authority.root_thread_id,
                           turn_id=expected_turn, work_status=reason)
            # A user addition accepted during the last turn is new input even
            # when the GX actions are unchanged or the scientific run settled.
            feedback_only = False
            if reason != "READY":
                try:
                    if project_messages.has_queued(state.directory):
                        feedback_only = True
                except (OSError, sqlite3.Error):
                    router.emit({"kind": "web_message_inbox_temporarily_unavailable"})
            if reason == "READY" or feedback_only:
                if (max_turns - router.turns < max(1, router.turns - previous_counts[0])
                        or router.max_callbacks - router.callback_attempts < max(1, router.callback_attempts - previous_counts[1])
                        or deadline - now <= now - previous_counts[2]):
                    reason = "HANDOFF"
                else:
                    additions = take_additions()
                    if feedback_only and additions is None:
                        reason = "INBOX_UNAVAILABLE"
                    elif additions:
                        reason = "READY"
            if reason != "READY":
                project_result = project_handoff(updated, reason, root_turns)
                status = "COMPLETED"
                break
            work = updated
            root_turns += 1
            previous_counts = (router.turns, router.callback_attempts, now)
            router.root_finished = False
            router.reset_root_output()
            turn_params["input"] = root_turn_input(_project_instruction(work) if work["status"] == "READY"
                                                   else "Review the pending additions on this same Root.")
            turn_request = start_root_turn(additions or [])
    except KeyboardInterrupt:
        # Interrupt only the observed Root turn. The durable project owns its
        # Jobs independently and normal transport cleanup still runs below.
        root = router.authority.root_thread_id
        turn = router.authority.active_turns.get(root)
        if turn:
            try:
                source_host_adapter.cancel_activity(client, root, turn)
            except (AppServerError, GxError, OSError):
                router.emit({"kind": "root_interrupt_delivery_unknown", "thread_id": root, "turn_id": turn})
        raise
    except (AppServerError, GxError, OSError) as exc:
        failure = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                   "failure_code": getattr(exc, "failure_code", None),
                   "request_id": getattr(exc, "request_id", None),
                   "pending_requests": list(getattr(exc, "pending_ids", ())),
                   "reason": "official connection or business source refused/ended; inspect exact retained evidence",
                   "diagnostic": _failure_diagnostic(exc)}
        status = "OUTCOME_UNKNOWN"
    finally:
        # Persist uncertainty before cleanup. Only confirmed cleanup may clear it;
        # neither a descriptor IO error nor close failure may hide the primary.
        try:
            save_session_state()
        except (OSError, GxError) as exc:
            status = "OUTCOME_UNKNOWN"
            if failure is None:
                failure = {"type": type(exc).__name__, "reason": "native outcome descriptor could not be durably recorded"}
        finally:
            router.authority.close()
            try:
                client.close()
                if getattr(client, "cleanup_pending", False):
                    cleanup_failure = {"type": "CleanupPending", "pending": True}
            except AppServerError as exc:
                cleanup_failure = {"type": type(exc).__name__, "pending": True}
            if cleanup_failure is not None:
                status = "OUTCOME_UNKNOWN"
            else:
                try:
                    if (status == "COMPLETED"
                            and router.guidance_in_context and router.last_root_turn_id):
                        descriptor.update(guidance_turn=router.last_root_turn_id, guidance_procedures=skill_metadata)
                    save_session_state(cleanup_pending=False, uncertain=status != "COMPLETED")
                except (OSError, GxError) as exc:
                    status = "OUTCOME_UNKNOWN"
                    if failure is None:
                        failure = {"type": type(exc).__name__, "reason": "confirmed cleanup outcome could not be durably recorded"}
    return {"status": status, "scope": "official_protocol_session_only_not_product_acceptance",
            **({"project_continuation": project_result} if project_result and status == "COMPLETED" else {}),
            "root_thread_id": router.authority.root_thread_id, "session_id": router.authority.session_id,
            "turns": router.turns, "workers": sum(identity != router.authority.root_thread_id
                and identity not in router.metadata_refusals for identity in router.authority.threads),
            "peak_concurrent_workers": router.peak_concurrent_workers,
            "GX_calls": router.calls, "callback_attempts": router.callback_attempts,
            "events": router.events, "elapsed_seconds": time.monotonic() - started,
            "pending_requests": list(client.pending_ids), "failure": failure,
            "cleanup_failure": cleanup_failure,
            "metadata_refusals": list(router.metadata_refusals.values())[:32],
            "metadata_refusals_total": len(router.metadata_refusals),
            "root_final_output": router.root_final_output,
            "public_output": router.public_output_collector.summary() if router.public_output_collector is not None else {"enabled": False},
            "resumed": bool(previous), "recovery_only": router.authority.recovery_only,
            "unresolved_previous_effects": previous["pending_effects"] if previous and router.authority.recovery_only else [],
            "model_authority": native.MODEL_AUTHORITY, "upstream_tier_independently_verified": False,
            "experimental_feature_advisory": {"acknowledged": True, "suppression": "REQUESTED_OWN_PROCESS_ONLY"},
            "automatic_skill_catalog_instructions": "SUPPRESSION_REQUESTED_OWN_PROCESS_ONLY",
            "experimental_features": ["dynamicTools", "multi_agent_v2_dynamic_tools"]}
