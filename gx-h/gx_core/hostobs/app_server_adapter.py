"""Runtime contract of the GX adapter for the persistent Codex App Server."""
import json
from pathlib import Path

from . import native_app_server
from .native_app_server import run_session


def cancel_activity(client, thread_id, turn_id, *, timeout=5):
    """Deliver one interruption to a specific native turn on its owning connection."""
    return client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                          timeout=timeout)


def integration_evidence(backend):
    catalog = Path(__file__).resolve().parents[2] / "runtime/codex-host/integration.json"
    evidence = json.loads(catalog.read_text())[backend]
    return {"verification": "integration_verified", "verified_at": evidence["verified_at"],
            "evidence_ref": evidence["reference"]}


def capabilities(runtime_namespace):
    """The namespace identifies a persistent runtime store, independent of process boot."""
    return {
        "schema_version": "gx.v2.alpha1",
        "runtime": {"schema_version": "gx.v2.alpha1", "runtime_kind": "codex_app_server",
                    "runtime_namespace": runtime_namespace},
        "runtime_ownership": "controller",
        "operations": ["create_session", "resume_session", "start_activity", "append_input",
                       "inspect", "subscribe_events", "cancel"],
        "native_subagents": "internal_only",
        "context_fork": "unverified",
        "cold_resume": "same_session",
        "dynamic_tools_root": "supported",
        "dynamic_tools_child": "supported",
        "cancel_semantics": "delivery_only",
        "terminal_status_query": "unverified",
        "event_delivery": "live_only",
        "usage_reporting": "snapshot",
        "tool_schema_versions": sorted(native_app_server.TOOL_PROFILES),
        **integration_evidence("codex_app_server"),
    }
