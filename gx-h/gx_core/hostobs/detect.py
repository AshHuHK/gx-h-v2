"""The host capability record a run stores when it starts (``runs.host_json``).

Two things can say which host is driving the backend: the caller (``gx_run_start`` takes a
``host`` object) and the environment the backend process was launched in. Both are recorded,
separately, with where each came from. Neither is authentication: on one machine a shell can
set a variable and a client can send a field.

``kind`` decides how strictly a worker's model must be shown before its work is taken
(``role_policy.observation.by_host``), so it is settled by a rule and not by the caller alone:

* a declared kind is used as declared;
* with nothing declared, the kind detected from the launch environment is used - a model's
  shell inside Claude Code carries the host's own variables, and a run started from it is a
  Claude Code run whether or not the model said so;
* with neither, the run is ``other_verified``: a script or a person, with no model to observe.

A script that drives the backend from inside a host's shell therefore says what it is
(``host: {"kind": "other_verified"}``), and the record shows both what it said and what the
environment looked like.

What is known about each host's environment is what was observed. Claude Code 2.1.322 sets
``CLAUDECODE``, ``CLAUDE_CODE_ENTRYPOINT``, ``CLAUDE_CODE_SESSION_ID`` and ``CLAUDE_EFFORT`` in
the shell of every tool call. No Codex session has been observed, so no Codex marker is listed
and a Codex run is recognised only when it is declared.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Mapping

from .. import contracts
from ..domain import clock
from ..errors import SchemaInvalid
from . import claude_code, policy

HOST_CLAUDE_CODE = claude_code.HOST
HOST_DEFAULT = "other_verified"
for _value in (HOST_CLAUDE_CODE, HOST_DEFAULT, "codex"):
    contracts.require_label("host_kind", _value)
del _value

#: Variables Claude Code was observed to set for a tool call's shell (2.1.322). Names only
#: are recorded, except the session id and the effort level, which are identifiers/settings.
CLAUDE_CODE_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
ENV_SESSION = "CLAUDE_CODE_SESSION_ID"
ENV_EFFORT = "CLAUDE_EFFORT"


def detected(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What the launch environment shows about the host. Never a guess."""
    env = os.environ if env is None else env
    markers = [name for name in CLAUDE_CODE_MARKERS if (env.get(name) or "").strip()]
    if (env.get("CLAUDECODE") or "").strip() == "1" or (env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip():
        session = (env.get(ENV_SESSION) or "").strip()
        return {
            "kind": HOST_CLAUDE_CODE,
            "markers": markers,
            "session_ref": session if claude_code.valid_session_ref(session) else None,
            "effort": (env.get(ENV_EFFORT) or "").strip() or None,
            "entrypoint": (env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip() or None,
        }
    return {"kind": None, "markers": markers, "session_ref": None, "effort": None, "entrypoint": None}


def host_record(
    declared: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The record for ``runs.host_json``: the kind that will be judged by, and how it was settled."""
    declared = dict(declared or {})
    seen = detected(env)
    declared_kind = declared.get("kind")
    if declared_kind is not None and declared_kind not in contracts.label_values("host_kind"):
        raise SchemaInvalid(
            f"{declared_kind!r} is not a host kind; allowed: {contracts.label_values('host_kind')}",
            details={"host_kind": str(declared_kind)[:64], "allowed": contracts.label_values("host_kind")},
        )
    if declared_kind is not None:
        kind, basis = declared_kind, "declared"
    elif seen["kind"] is not None:
        kind, basis = seen["kind"], "launch_environment"
    else:
        kind, basis = HOST_DEFAULT, "default"

    declared_session = declared.get("session_ref")
    session_ref = declared_session if isinstance(declared_session, str) and declared_session else seen["session_ref"]
    policy_doc, policy_sha256 = policy.agent_policy()
    mode = policy.host_mode(kind, policy_doc)
    reader = policy.reader_spec(kind, policy_doc)
    return {
        "kind": kind,
        "kind_basis": basis,
        "declared": {key: declared[key] for key in ("kind", "session_ref", "version") if declared.get(key) is not None},
        "detected": seen,
        # True when the caller declared a more lenient kind than the environment shows: a
        # fact for whoever audits the run, never a reason the backend overrides the declaration.
        "understated": bool(seen["kind"]) and len(policy.host_mode(seen["kind"], policy_doc)["accepts"]) < len(mode["accepts"]),
        "session_ref": session_ref,
        "observation_mode": mode.get("mode"),
        "accepts": list(mode["accepts"]),
        "reader": {
            "available": reader is not None,
            "verified_on_versions": list(reader["verified_on_versions"]) if reader else [],
            "record_found": bool(
                kind == HOST_CLAUDE_CODE and session_ref and claude_code.locate_session(session_ref) is not None
            ),
        },
        "delegation": "unknown",
        "hooks": "unknown",
        "policy_sha256": policy_sha256,
        "recorded_at": clock.now_iso(),
    }


def probe_version(command: str, *, timeout: int = 20) -> str | None:
    """The first line a host CLI prints for ``--version``, or None. For documents and ``doctor``."""
    binary = shutil.which(command)
    if binary is None:
        return None
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (done.stdout or "").strip().splitlines()
    return lines[0].strip() if done.returncode == 0 and lines else None
