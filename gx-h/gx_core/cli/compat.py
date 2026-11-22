"""Entrypoints for existing GX inspection, native-project and localhost web commands.

Each command imports its original owner on use. P0 starts through gx_core.p0.cli.
"""
from __future__ import annotations

import argparse
import json
from typing import Sequence


def inspect_installation(argv: Sequence[str] | None = None) -> int:
    from .. import api, contracts
    from ..context import principal_from_env
    from ..orchestration import seams, service

    parser = argparse.ArgumentParser(description="Inspect the native harness installation without opening project state.")
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


def native(argv: Sequence[str] | None = None) -> int:
    from ..native_cli import main
    return main(argv)


def project(argv: Sequence[str] | None = None) -> int:
    from ..project_continuation import main
    return main(argv)


def web(argv: Sequence[str] | None = None) -> int:
    from ..web.http import main
    return main(argv)
