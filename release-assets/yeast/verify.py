"""Check the optional release through both read-only indexes, BLASTp and local MCP stdio."""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client

PLUGIN = "gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1/methods"


async def check_mcp(bundle, expected_digest):
    sys.path.insert(0, str(bundle / "GX-harness/integrations/macromnex"))
    from runtime import ROOT, launch_settings, resolve_server

    server = resolve_server("uniprot")
    entrypoint = ROOT / server["path"] / server["entrypoint"]
    if hashlib.sha256(entrypoint.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("UniProt MCP differs from the build pinned by the plugin")
    async with stdio_client(launch_settings(server)) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            identity = await session.initialize()
            methods = await session.list_tools()
            if "get_protein_info" not in {tool.name for tool in methods.tools}:
                raise ValueError("UniProt MCP did not list the plugin's operation")
            return {"name": identity.serverInfo.name, "tools_listed": len(methods.tools)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    release, bundle = args.release.resolve(), args.bundle.resolve()
    sys.path.insert(0, str(release / PLUGIN))
    from evidence import homology, run_lookup

    scope = json.loads((release / "examples/yeast-sugar-evidence/scope.json").read_text())
    mount = {"enzyme_assets": str(bundle / "enzyme-assets"), "blastp": str(bundle / "blast/bin/blastp")}
    counts = {}
    for substrate in ("sucrose", "trehalose"):
        _, sequences = run_lookup(scope, substrate, mount)
        result, _ = homology(sequences, mount)
        if not sequences or result["raw_hsps"] <= 0:
            raise ValueError(f"No indexed candidates or BLAST matches for {substrate}")
        counts[substrate] = {"distinct_accessions": len(sequences), "raw_hsps": result["raw_hsps"]}
    pins = json.loads((release / PLUGIN / "mounts.json").read_text())
    counts["stdio"] = asyncio.run(check_mcp(bundle, pins["mcp_script_sha256"]))
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
