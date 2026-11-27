"""Role intents: the host-neutral definition of a role, and the role file each host gets from it.

A **role intent** says what a role is without saying how any host spells it::

    {"manifest": {"role_id": "role.researcher", "version": "...", "role": "researcher",
                  "task_class": "PLANNING", "title": "...", "description": "what the role does",
                  "hosts": ["claude_code", "codex"]},
     "instructions": "role-specific text, the same words for every host",
     "may_not_use": ["WebFetch", "WebSearch"],
     "may_use_only": [...]}          # optional; absent means the role inherits its session's tools

It does not name a model or an effort. Those are the user's rule, held in
``config/role_policy.json`` for the role's class of work; the projection reads them from the
policy, so an intent cannot ask for a cheaper model and a role file cannot carry one the
policy does not give.

A **role file** is the projection of one intent for one host: ``.claude/agents/gx-<role>.md``
or ``.codex/agents/gx_<role>.toml``. It is the frame (``role_frame.md``: how a worker
reaches the backend and what it never does) filled from the policy and the host, followed by
the intent's instructions verbatim, and it names the intent it came from by id, version and
sha256.

Two kinds of intent go through this one code path:

* the intents this tree **ships**, one per role of the policy. They are product code,
  admitted at the sha256 ``config/shipped.json`` pins for each (``contracts/gates.json``,
  ``bootstrap.SHIPPED``). That sha256 covers the intent, the model and efforts the policy
  gives its class, and the two files of the projector (this module and the frame), so a
  change to any of them moves the pin;
* an intent **authored at run time**, which is an asset of kind ``ROLE_INTENT`` and takes
  the gate. Its role file carries the asset's own id, version and sha256.

v1 kept two hand-written sets of role files with two vocabularies; the models they named
were whatever the author typed, and the Claude ones listed their tools in a way that
removed the backend they told the worker to call.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .. import contracts
from . import policy as role_policy

KIND = contracts.require_label("asset_kind", "ROLE_INTENT")
HARNESS_ROOT = Path(__file__).resolve().parents[2]
FRAME = Path(__file__).with_name("role_frame.md")
#: The projector: the files that turn an intent into a role file.
PROJECTOR_FILES = (Path(__file__).resolve(), FRAME.resolve())
SHIPPED_PINS = HARNESS_ROOT / "config" / "shipped.json"

#: host -> the directory of its project tree that holds role files.
HOST_ROLE_DIRS = {"claude_code": ".claude/agents", "codex": ".codex/agents"}
for _host in HOST_ROLE_DIRS:
    contracts.require_label("host_kind", _host)

COORDINATOR = contracts.require_label("worker_role", "coordinator")
VERSION_SHIPPED = "shipped"
ID_PREFIX = "role."
#: The absolute path of this tree's client, as a worker's shell is given it.
GX_BIN = str(HARNESS_ROOT / "bin" / "gx")
#: What stands in its place in the staged files of the tree, which are the same on every
#: machine; the installer puts ``GX_BIN`` there.
GX_BIN_TOKEN = "@GX_BIN@"
#: Tools every role is denied: they reach the network without the proxy.
ALWAYS_DENIED = ("WebFetch", "WebSearch")
#: A worker reaches the backend through the client, which it runs with the shell tool. An
#: allowlist that leaves the shell out describes a role that cannot hand anything in.
SHELL_TOOL = "Bash"
MAX_INSTRUCTIONS = 16_000
MAX_DESCRIPTION = 600

#: The content of a ROLE_INTENT asset in the contract's notation (declared to governance by
#: ``projection`` until ``contracts/bundles.json`` holds it).
FIELDS = {
    "manifest": "object",
    "instructions": "str",
    "may_not_use": "array<str>",
    "may_use_only": "array<str>?",
}
MANIFEST_FIELDS = {
    "role_id": "str",
    "version": "str",
    "role": "enum:worker_role",
    "task_class": "enum:task_class",
    "title": "str",
    "description": "str",
    "hosts": "array<enum:host_kind>",
    "supersedes": "AssetRef?",
    "licence": "str?",
    "licence_basis": "str?",
    "limitations": "str?",
}

STAMP_KEYS = ("gx_asset_id", "gx_asset_version", "gx_asset_sha256")
_STAMP_LINE = re.compile(r"^\s*gx_asset_(?:id|version|sha256)\s*[:=]", re.MULTILINE)
_FRAME_RE = re.compile(r"<!--gx:frame\s+roles=(?P<roles>\w+)-->\n(?P<body>.*?)<!--gx:end-->", re.DOTALL)
_SHIPPED_RE = re.compile(r"<!--gx:shipped\s+roles=(?P<roles>[\w,]+)-->\n(?P<body>.*?)<!--gx:end-->", re.DOTALL)
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


# ------------------------------------------------------------------------------ naming

def intent_id(role: str) -> str:
    """The one id a role's definition has; its versions are versions of that role."""
    return ID_PREFIX + role


def role_name(host: str, role: str) -> str:
    """The name a host knows the role by: ``gx-skill-author`` / ``gx_skill_author``."""
    return "gx-" + role.replace("_", "-") if host == "claude_code" else "gx_" + role


def role_path(host: str, role: str) -> str:
    """Where the role file goes, relative to the host's project tree."""
    suffix = ".md" if host == "claude_code" else ".toml"
    return f"{HOST_ROLE_DIRS[host]}/{role_name(host, role)}{suffix}"


# ------------------------------------------------------------------------------- frame

def _frame_text() -> str:
    return FRAME.read_text(encoding="utf-8")


def frames(text: str | None = None) -> dict[str, str]:
    """``{"worker": ..., "coordinator": ...}``: what every role file of that kind says."""
    text = _frame_text() if text is None else text
    found = {match.group("roles"): match.group("body").strip("\n") for match in _FRAME_RE.finditer(text)}
    missing = {"worker", COORDINATOR} - set(found)
    if missing:
        raise ValueError(f"{FRAME.name} has no frame for {sorted(missing)}")
    return found


def shipped_instructions(role: str, text: str | None = None) -> str:
    """The instructions of the shipped intent of one role, in the order the frame file has them."""
    text = _frame_text() if text is None else text
    parts = [match.group("body").strip("\n") for match in _SHIPPED_RE.finditer(text)
             if role in match.group("roles").split(",")]
    return "\n\n".join(parts)


def host_words(host: str, policy: Mapping[str, Any], gx: str) -> dict[str, str]:
    """What the frame says differently on each host."""
    if host == "claude_code":
        # The Agent tool takes a model by its short name; a dispatch row names it by id.
        short_names = ", ".join(f"\"{name}\" for {model}" for model, name in worker_models(policy).items())
        return {
            "gx": gx,
            "subject_form": "<first 8 characters of your session id>:<the nonce of this launch>:<task id>",
            "bind_hint": (
                "With the host's GX hooks installed the backend already knows which of the host's "
                "agents you are: the host said so when you ran the claim. Pass `host` `claude_code`, "
                "the id of the model you run on and your effort; add `agent_ref` and "
                "`host_session_ref` only from a line beginning \"GX host adapter facts\" in your "
                "context, never from a shell variable. What you say is a report: the verdict is read "
                "from the host's record of your turns, and a worker that record cannot show is held "
                "at submission."
            ),
            "web_rule": "`WebFetch` and `WebSearch` are not available to this role.",
            "transport_why": (
                "The host's hook ties your subject to you only when it sees the client run; a call "
                "it cannot see leaves the backend unable to show which agent did the work."
            ),
            "spawn_hint": (
                "A round is dispatched with the saved `gx-dispatch` workflow, under a nonce you make "
                "for that launch and never use again in this session; it starts one `gx-<role>` agent "
                "per row at the model and effort the row names, returns at once, and its result comes "
                "back as a task notification. The host adapter records the rows it was handed and "
                "refuses a launch that repeats them while it runs. A single task can be started with "
                "the Agent tool the same way: the model and effort of its row, the worker's subject as "
                "the spawn's `description` and nothing else in it (the host writes it into the agent's "
                "own record, which is what ties the subject to that agent), and the task id written "
                f"into the prompt as it is. The Agent tool takes a model by its short name ({short_names}). "
                "The `gx-<role>` agent types exist only in a session that started after the host "
                "adapter was installed."
            ),
        }
    if host == "codex":
        return {
            "gx": gx,
            "subject_form": "codex:<thread id>:<worker name>",
            "bind_hint": (
                "Pass `host` `codex`, your thread id as `host_session_ref`, and the model and "
                "reasoning effort you run at. The backend has no reader for a Codex session's "
                "record, so what you say is recorded as a report and labelled as one."
            ),
            "web_rule": "Do not use the host's own web search or fetch tools.",
            "transport_why": (
                "A call over MCP carries the session's identity, not your thread's, so the backend "
                "could not tell your work from another thread's."
            ),
            "spawn_hint": (
                "Start it as the `gx_<role>` agent; if this session has no subagents, do the tasks "
                "one after another yourself and bind each as `single_agent_sequential`."
            ),
        }
    raise KeyError(f"no role file is defined for host {host!r}")


# ------------------------------------------------------------------------------ intents

def shipped_intent(role: str, policy: Mapping[str, Any]) -> dict[str, Any]:
    """The intent this tree ships for one role of the policy."""
    spec = policy["roles"][role]
    return {
        "manifest": {
            "role_id": intent_id(role),
            "version": VERSION_SHIPPED,
            "role": role,
            "task_class": spec["task_class"],
            "title": f"GX role {role}",
            "description": spec["does"],
            "hosts": sorted(HOST_ROLE_DIRS),
        },
        "instructions": shipped_instructions(role),
        "may_not_use": list(ALWAYS_DENIED),
    }


def worker_models(policy: Mapping[str, Any]) -> dict[str, str]:
    """``model id -> short name`` for every model some class of work runs on under this policy."""
    return {model: role_policy.short_name(model, policy) for model in role_policy.models_in_use(policy)}


def requirement_of(content: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """What the policy gives a role file: the model and efforts of the class of work of the intent's role.

    The coordinating role runs on the pin, and its file also tells it the short name of every
    model its workers run on (``worker_models``): that table is part of what decides the
    file's bytes, so it is part of what the shipped sha256 covers.
    """
    role = content["manifest"]["role"]
    required = role_policy.required_for_role(role, policy)
    out = {"model": required["model"], "default_effort": required["default_effort"],
           "allowed_efforts": list(required["allowed_efforts"])}
    if role == COORDINATOR:
        out["worker_models"] = worker_models(policy)
    return out


def problems_in(content: Any, policy: Mapping[str, Any]) -> list[str]:
    """Every way an intent that has the right shape still may not be projected.

    The shape itself (unknown fields, enums) is the schema check's; this is what the fields
    mean: the id is the role's, the class is the policy's, nothing that reaches the network
    around the proxy is allowed back, and the instructions cannot pass for a stamp or break
    out of the file they are written into.
    """
    if not isinstance(content, Mapping) or not isinstance(content.get("manifest"), Mapping):
        return ["a role intent is an object with a manifest"]
    manifest = content["manifest"]
    problems: list[str] = []
    role = manifest.get("role")
    roles = policy.get("roles") or {}
    if role not in roles:
        return [f"role {role!r} is not a role of the agent policy"]
    if manifest.get("role_id") != intent_id(role):
        problems.append(f"manifest.role_id must be {intent_id(role)!r}: one id per role, and a new "
                        "definition of the role is a new version of it")
    if manifest.get("task_class") != roles[role]["task_class"]:
        problems.append(f"manifest.task_class is {manifest.get('task_class')!r}; the agent policy gives "
                        f"{role} the class {roles[role]['task_class']!r}, and the model and effort follow from it")
    hosts = manifest.get("hosts")
    if not isinstance(hosts, list) or not hosts or len(set(hosts)) != len(hosts):
        problems.append("manifest.hosts lists each host the role is projected to, once")
    else:
        unknown = sorted(set(hosts) - set(HOST_ROLE_DIRS))
        if unknown:
            problems.append(f"manifest.hosts names {unknown}; role files exist for {sorted(HOST_ROLE_DIRS)}")
    for name in ("title", "description"):
        value = manifest.get(name)
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            problems.append(f"manifest.{name} is one non-empty line")
        elif len(value) > MAX_DESCRIPTION:
            problems.append(f"manifest.{name} is longer than {MAX_DESCRIPTION} characters")
        elif "'''" in value or GX_BIN_TOKEN in value:
            problems.append(f"manifest.{name} holds a sequence that would break out of a role file")

    denied = content.get("may_not_use")
    if not isinstance(denied, list) or not all(isinstance(item, str) and _TOOL_NAME.fullmatch(item) for item in denied):
        problems.append("may_not_use is a list of tool names")
        denied = []
    missing = [name for name in ALWAYS_DENIED if name not in denied]
    if missing:
        problems.append(f"may_not_use must name {missing}: they reach the network without the proxy")
    allowed = content.get("may_use_only")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed \
                or not all(isinstance(item, str) and _TOOL_NAME.fullmatch(item) for item in allowed):
            problems.append("may_use_only, when present, is a non-empty list of tool names")
        else:
            if SHELL_TOOL not in allowed:
                problems.append(f"may_use_only leaves out {SHELL_TOOL}: a worker reaches the backend through "
                                "the client, so such a role could hand nothing in")
            both = sorted(set(allowed) & set(denied))
            if both:
                problems.append(f"{both} are listed both as allowed and as denied")

    instructions = content.get("instructions")
    if not isinstance(instructions, str):
        problems.append("instructions is text")
    else:
        if len(instructions) > MAX_INSTRUCTIONS:
            problems.append(f"instructions are longer than {MAX_INSTRUCTIONS} characters; what belongs to "
                            "one task goes in its brief")
        if "'''" in instructions or GX_BIN_TOKEN in instructions:
            problems.append("instructions hold a sequence that would break out of a role file")
        if _STAMP_LINE.search(instructions):
            problems.append("instructions hold a line that reads as the stamp of a generated file")
    return problems


# ---------------------------------------------------------------------------- rendering

def _description(manifest: Mapping[str, Any]) -> str:
    """When a host should pick this role: what it does, and that it is a GX role."""
    role = manifest["role"]
    if role == COORDINATOR:
        return f"{manifest['description']} GX role `coordinator`: coordinates one GX run."
    return (f"{manifest['description']} GX role `{role}` ({manifest['task_class'].lower()} work): "
            "started for one task of a run, named by run id, task id and subject.")


def body(host: str, content: Mapping[str, Any], policy: Mapping[str, Any], *, gx: str = GX_BIN) -> str:
    """The text a worker in the role starts from: the frame, then the intent's own instructions."""
    manifest = content["manifest"]
    required = requirement_of(content, policy)
    frame = frames()[COORDINATOR if manifest["role"] == COORDINATOR else "worker"]
    # A Claude Code role file names the model of the role's class. A Codex one names none: the
    # policy names no Codex model until the user names one (design r2.2 §8.10).
    model_words = (f"`{required['model']}`" if host == "claude_code" else
                   "the model of the Codex session that starts it (the agent policy names no Codex model "
                   "until the user names one)")
    text = frame.format(
        role=manifest["role"], does=manifest["description"], model_words=model_words,
        allowed_efforts=" or ".join(f"`{level}`" for level in required["allowed_efforts"]),
        **host_words(host, policy, gx),
    ).strip("\n")
    instructions = content["instructions"].strip("\n")
    return text + ("\n\n" + instructions if instructions.strip() else "") + "\n"


def stamp(asset_id: str, version: str, sha256: str) -> dict[str, str]:
    return {"gx_asset_id": asset_id, "gx_asset_version": version, "gx_asset_sha256": sha256}


def render(host: str, content: Mapping[str, Any], *, asset_id: str, version: str, sha256: str,
           policy: Mapping[str, Any], gx: str = GX_BIN) -> str:
    """One role file: the projection of an intent for one host. The same inputs give the same bytes."""
    manifest = content["manifest"]
    required = requirement_of(content, policy)
    text = body(host, content, policy, gx=gx)
    marks = stamp(asset_id, version, sha256)
    note = ("generated from this role intent by gx_core/hostobs/roles.py; do not edit: a change is a new "
            "version of the intent")
    if host == "claude_code":
        # No `tools:` line unless the intent has an allowlist. An allowlist of built-in tools
        # removes every MCP tool, which is how v1's roles lost the backend they were told to call.
        allow = f"tools: {', '.join(content['may_use_only'])}\n" if content.get("may_use_only") else ""
        return (
            "---\n"
            f"name: {role_name(host, manifest['role'])}\n"
            f"description: {json.dumps(_description(manifest), ensure_ascii=False)}\n"
            f"model: {required['model']}\n"
            f"effort: {required['default_effort']}\n"
            f"{allow}"
            f"disallowedTools: {', '.join(content['may_not_use'])}\n"
            + "".join(f"{key}: {value}\n" for key, value in marks.items())
            + f"# {note}\n"
            "---\n\n" + text
        )
    if host == "codex":
        # The reference is written inside the instructions: whether Codex accepts a key it does
        # not know in a role file has not been observed, and text it always accepts.
        return (
            f"# {note}\n"
            "#\n"
            "# NOT OBSERVED WITH A HOST. No Codex session has run under this role. Whether the effort\n"
            "# level named here is honoured is unknown until one has.\n"
            "#\n"
            "# No model is named: config/role_policy.json names no Codex model until the user names one\n"
            "# (design r2.2 section 8.10), so the role runs on the model of the Codex session that starts\n"
            "# it. The worker reports that model when it binds; a report the policy does not allow waits\n"
            "# for the user.\n\n"
            f"name = {json.dumps(role_name(host, manifest['role']), ensure_ascii=False)}\n"
            f"description = {json.dumps(_description(manifest), ensure_ascii=False)}\n"
            f"model_reasoning_effort = {json.dumps(required['default_effort'])}\n"
            "developer_instructions = '''\n" + text
            + "\n---\n" + "".join(f"{key}: {value}\n" for key, value in marks.items())
            + "'''\n"
        )
    raise KeyError(f"no role file is defined for host {host!r}")


def files(content: Mapping[str, Any], *, asset_id: str, version: str, sha256: str,
          policy: Mapping[str, Any], gx: str = GX_BIN) -> list[dict[str, Any]]:
    """The host projections of an intent, in the shape a projector returns.

    One entry per host that has role files: a file (``path`` relative to the host's project
    tree, ``content`` as bytes, ``embeds`` naming the parts of the intent the file holds
    verbatim) or the reason the host gets none.
    """
    manifest = content["manifest"]
    embeds = ["/manifest/description"] + (["/instructions"] if content["instructions"].strip() else [])
    out: list[dict[str, Any]] = []
    for host in sorted(HOST_ROLE_DIRS):
        if host not in manifest["hosts"]:
            out.append({"host": host, "skipped": "the intent does not list this host"})
            continue
        text = render(host, content, asset_id=asset_id, version=version, sha256=sha256, policy=policy, gx=gx)
        out.append({"host": host, "path": role_path(host, manifest["role"]),
                    "content": text.encode("utf-8"), "embeds": list(embeds)})
    return out


# ------------------------------------------------------------------------------ shipped

def projector_sha256() -> dict[str, str]:
    """The sha256 of each file of the projector, by its path in the tree."""
    return {str(path.relative_to(HARNESS_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in PROJECTOR_FILES}


def shipped_sha256(content: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    """The sha256 a shipped intent is admitted at.

    It covers what decides the bytes of its role files: the intent, the model and efforts the
    policy gives its class, and the projector. A change to the reader's field lists in the
    policy file moves nothing here; a change to the frame moves every pin.
    """
    canonical = json.dumps(
        {"intent": content, "requirement": requirement_of(content, policy), "projector": projector_sha256()},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def shipped(policy: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Every intent this tree ships: ``role_id -> {"sha256", "intent", "requirement"}``."""
    policy = policy if policy is not None else role_policy.agent_policy()[0]
    out: dict[str, dict[str, Any]] = {}
    for role in policy["roles"]:
        content = shipped_intent(role, policy)
        found = problems_in(content, policy)
        if found:
            raise ValueError(f"the shipped intent of {role} may not be projected: {'; '.join(found)}")
        out[intent_id(role)] = {"sha256": shipped_sha256(content, policy), "intent": content,
                                "requirement": requirement_of(content, policy)}
    return out


def shipped_files(policy: Mapping[str, Any] | None = None, *, gx: str = GX_BIN) -> list[dict[str, Any]]:
    """The role files of every shipped intent, each entry also naming its ``role_id``."""
    policy = policy if policy is not None else role_policy.agent_policy()[0]
    out: list[dict[str, Any]] = []
    for role_id, entry in shipped(policy).items():
        for item in files(entry["intent"], asset_id=role_id, version=VERSION_SHIPPED, sha256=entry["sha256"],
                          policy=policy, gx=gx):
            out.append({**item, "role_id": role_id})
    return out


def pins(path: str | Path | None = None) -> dict[str, str]:
    """What ``config/shipped.json`` admits as shipped role intents: ``role_id -> sha256``."""
    try:
        document = json.loads(Path(path if path is not None else SHIPPED_PINS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = document.get("role_intents") if isinstance(document, dict) else None
    return {str(name): str(value) for name, value in found.items()} if isinstance(found, dict) else {}


def pin_problems(policy: Mapping[str, Any] | None = None, *, pinned: Mapping[str, str] | None = None) -> list[str]:
    """Every shipped intent that is not the one admitted, and every pin of an intent that is not shipped."""
    pinned = pins() if pinned is None else pinned
    computed = {role_id: entry["sha256"] for role_id, entry in shipped(policy).items()}
    problems = []
    for role_id, sha256 in sorted(computed.items()):
        if role_id not in pinned:
            problems.append(f"UNPINNED {role_id}: config/shipped.json admits no such role intent "
                            f"(computed sha256={sha256})")
        elif pinned[role_id] != sha256:
            problems.append(f"PIN MISMATCH {role_id}: computed sha256={sha256}, config/shipped.json has "
                            f"{pinned[role_id]}")
    for role_id in sorted(set(pinned) - set(computed)):
        problems.append(f"STALE PIN {role_id}: config/shipped.json pins a role intent the policy does not define")
    return problems


def admission(role: str, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """How the shipped intent of one role stands against its pin.

    ``{"role_id", "sha256", "pinned", "admitted"}``: ``admitted`` is true only when the
    sha256 computed now is the one ``config/shipped.json`` holds. What a run does with a role
    whose intent is not admitted is its caller's decision; this function only says so.
    """
    policy = policy if policy is not None else role_policy.agent_policy()[0]
    content = shipped_intent(role, policy)
    sha256 = shipped_sha256(content, policy)
    pinned = pins().get(intent_id(role))
    return {"role_id": intent_id(role), "sha256": sha256, "pinned": pinned, "admitted": pinned == sha256}


def read_stamp(text: str) -> dict[str, str | None]:
    """The asset reference a role file carries, key by key (None where it carries none)."""
    out: dict[str, str | None] = {}
    for key in STAMP_KEYS:
        match = re.search(rf"^\s*{key}\s*[:=]\s*[\"']?([^\"'\s]+)[\"']?\s*$", text, re.MULTILINE)
        out[key] = match.group(1) if match else None
    return out
