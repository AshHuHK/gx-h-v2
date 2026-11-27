"""What the backend itself can see of the process that called ``gx_host_event``.

``gx_host_event`` is guarded by a scope, and on one machine any shell can name any scope. So
the backend looks at its caller and writes down what it sees with every event, and it refuses
a call that reached it through the command-line client or the MCP server and does not look
like a hook the host ran (design r2.2a section 8.2).

What a hook the host runs looks like was measured, not assumed (probe F, Claude Code 2.1.322,
22 hook runs and 3 commands of the shell tool, in the main thread, an Agent-tool agent and a
Workflow agent):

==========================  ====================  =========================
                            a hook the host runs  a command of the Bash tool
==========================  ====================  =========================
standard input              a socket              a character device
``CLAUDE_CODE_EXECPATH``    not set               set
``CLAUDE_PROJECT_DIR``      set                   not set
==========================  ====================  =========================

An agent's tool shell did not differ from the main thread's, so the backend can say "a tool
shell" and never "an agent's". Which shell runs a hook and which runs the shell tool, and
the names of the processes above the caller, are this machine's and decide nothing: they are
recorded as facts and are not a reason to refuse. The refusal rests on the host's own
signals: the variable it sets for its shell tool, the socket it hands a hook, and the caller
being this tree's hook script.

**This records and discourages. It does not stop a determined caller.** What stops a forged
event from having an effect is that an event moves state only where the host's own record
agrees with it (``events``, ``observation.tie_of_subject``). Two ways round this check are
known and are left open on purpose, because closing them would only move the line:

* ``exec`` the hook's own command from a tool shell, with standard input a socket and the
  variable removed;
* call the dispatcher in-process (``python -c`` importing ``gx_core``): a call that does not
  arrive through the client or the MCP server is recorded as in-process and is not refused,
  because nothing a process can see tells a test or a script from a forger there. (The hook
  on the shell tool denies a command that names ``gx_core``, as advice.)
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

from .. import contracts

#: The hook client this tree ships: the one caller of ``gx_host_event`` over the client.
HOOK_SCRIPT = contracts.HARNESS_ROOT / "adapters" / "hosts" / "claude_code" / "hooks" / "gx_hook.py"
#: Set by the host for a command of its shell tool, not for a hook (probe F).
ENV_TOOL_SHELL = "CLAUDE_CODE_EXECPATH"
#: Set by the host for a hook, not for a command of its shell tool (probe F). Recorded only.
ENV_HOOK = "CLAUDE_PROJECT_DIR"
ENTRY_CLIENT, ENTRY_MCP, ENTRY_IN_PROCESS = "client", "mcp_server", "in_process"
MAX_CHAIN = 16
_KINDS = (("sock", stat.S_ISSOCK), ("fifo", stat.S_ISFIFO), ("chr", stat.S_ISCHR), ("reg", stat.S_ISREG),
          ("dir", stat.S_ISDIR))


def entry() -> str:
    """How this process was started: as the client, as the MCP server, or as something that imported the backend."""
    main = sys.modules.get("__main__")
    name = getattr(getattr(main, "__spec__", None), "name", None) or ""
    if name.startswith("gx_core.cli"):
        return ENTRY_CLIENT
    if name.startswith("gx_core.mcp"):
        return ENTRY_MCP
    return ENTRY_IN_PROCESS


def _stat_of(pid: int, proc: Path) -> tuple[str | None, int | None]:
    """``(command name, parent pid)`` of a process, from ``/proc/<pid>/stat``."""
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        head, _, tail = text.rpartition(")")
        return head.split("(", 1)[1][:32], int(tail.split()[1])
    except (OSError, IndexError, ValueError):
        return None, None


def _stdin_kind(pid: int | None, proc: Path) -> str | None:
    """What kind of file a process reads its standard input from, or None when that cannot be seen."""
    if pid is None:
        return None
    try:
        mode = os.stat(proc / str(pid) / "fd" / "0").st_mode
    except OSError:
        return None
    return next((name for name, test in _KINDS if test(mode)), "other")


def _is_the_hook_script(pid: int | None, proc: Path) -> bool:
    """Whether a process is an interpreter running this tree's hook script, by the file it names."""
    if pid is None:
        return False
    try:
        words = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")[:12]
        wanted = os.path.realpath(HOOK_SCRIPT)
    except OSError:
        return False
    for word in words:
        if word.endswith(HOOK_SCRIPT.name.encode()):
            try:
                if os.path.realpath(word.decode("utf-8", "replace")) == wanted:
                    return True
            except OSError:
                continue
    return False


def facts(*, pid: int | None = None, env: Mapping[str, str] | None = None, proc: Path = Path("/proc")) -> dict[str, Any]:
    """What the backend sees of its caller. Names and kinds only: no argument, no variable's value.

    ``entry``                       how this process was started (:func:`entry`);
    ``chain``                       the command names of this process and its ancestors, nearest first;
    ``caller_stdin``                what the calling process reads its standard input from;
    ``called_by_hook_script``       whether the calling process runs this tree's hook script;
    ``tool_shell_variable_set``     whether the host's variable for its shell tool is in the environment;
    ``hook_variable_set``           whether the host's variable for a hook is.
    """
    env = os.environ if env is None else env
    current = os.getpid() if pid is None else pid
    chain: list[str] = []
    parent: int | None = None
    seen: set[int] = set()
    while current and current not in seen and len(chain) < MAX_CHAIN:
        seen.add(current)
        name, above = _stat_of(current, proc)
        if name is None:
            break
        chain.append(name)
        if len(chain) == 1:
            parent = above
        current = above or 0
    return {
        "entry": entry(), "chain": chain,
        "caller_stdin": _stdin_kind(parent, proc),
        "called_by_hook_script": _is_the_hook_script(parent, proc),
        "tool_shell_variable_set": bool((env.get(ENV_TOOL_SHELL) or "").strip()),
        "hook_variable_set": bool((env.get(ENV_HOOK) or "").strip()),
    }


def not_a_hook(found: Mapping[str, Any]) -> list[str]:
    """Why a call is not a hook the host ran, by the host's own signals; empty when it is one.

    Empty also for a call made in-process: it did not arrive through the client or the MCP
    server, so it is neither a hook nor a shell command, and it is recorded as what it is.
    """
    if found.get("entry") == ENTRY_IN_PROCESS:
        return []
    reasons: list[str] = []
    if found.get("tool_shell_variable_set"):
        reasons.append(f"{ENV_TOOL_SHELL} is set, which the host sets for a command of its shell tool and not for a hook")
    if found.get("caller_stdin") != "sock":
        reasons.append("the calling process does not read a socket, which is what the host hands a hook")
    if not found.get("called_by_hook_script"):
        reasons.append("the calling process is not this tree's hook script")
    return reasons


__all__ = ["ENTRY_CLIENT", "ENTRY_IN_PROCESS", "ENTRY_MCP", "ENV_HOOK", "ENV_TOOL_SHELL", "HOOK_SCRIPT", "entry",
           "facts", "not_a_hook"]
