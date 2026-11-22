"""Inspect the native harness installation without opening project state."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Sequence

from .. import api, contracts
from ..context import principal_from_env
from ..orchestration import seams, service


def _confirm_as_person(tool: str, arguments: dict[str, Any]) -> bool:
    """Ask the person at the controlling terminal. False when there is none.

    The terminal is opened by name rather than taken from stdin, so arguments piped in
    do not stand in for an answer and an answer cannot be piped in with them.

    It is opened as a bare descriptor. A buffered read-write text stream cannot be built
    on a device that does not seek, so ``open("/dev/tty", "r+")`` raised whenever there
    *was* a terminal, and the handler written for "there is none" turned that into
    "no": nobody could ever confirm. This is a procedural boundary like a scope: a
    process can give its child a terminal, and what is recorded is that a confirmation
    was read from one.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    try:
        if not os.isatty(fd):
            return False
        prompt = (
            f"\n{tool} records a decision as yours.\n"
            + json.dumps(arguments, ensure_ascii=False, indent=2)[:4000]
            + "\nType yes to make it: "
        )
        os.write(fd, prompt.encode("utf-8"))
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 256:
            chunk = os.read(fd, 64)
            if not chunk:
                break
            answer += chunk
        return answer.decode("utf-8", "replace").strip().lower() == "yes"
    finally:
        os.close(fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    names = commands.add_parser("tools", help="list installed tools for this caller")
    names.add_argument("--names", action="store_true", help="print only tool names")
    schema = commands.add_parser("schema", help="display the schema of an installed tool")
    schema.add_argument("tool")
    commands.add_parser("doctor", help="check providers and installed workflow, strategy and role pins")
    args = parser.parse_args(argv)
    if args.command == "tools":
        entries = api.list_tools(principal_from_env())
        result = {"ok": True, "tools": [entry["name"] for entry in entries] if args.names else entries}
    elif args.command == "schema":
        try:
            entry = api.describe_tool(args.tool)
        except api.UnknownTool as exc:
            result = {"ok": False, "problem": str(exc)}
        else:
            spec = contracts.any_tool_spec(args.tool)
            result = {"ok": True, **entry, "output": spec["output"], "errors": spec.get("errors", [])}
    else:
        report = api.doctor()
        owners = seams.report()
        shipped = service.shipped_report()
        failed = [name for name, error in report["registry"]["providers"].items() if error]
        pins_match = all(
            item["matches"]
            for group in ("workflows", "strategies", "role_intents")
            for item in shipped[group].values()
        )
        pin = json.loads((contracts.CONTRACTS_DIR / "CONTRACT_SHA").read_text(encoding="utf-8"))
        result = {
            "ok": not failed and not owners["on_fallback"] and not shipped["packages_not_loaded"]
                  and pins_match and pin == contracts.contract_digest(),
            "providers_not_loaded": failed, "shipped": shipped, "seams": owners, **report,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1
