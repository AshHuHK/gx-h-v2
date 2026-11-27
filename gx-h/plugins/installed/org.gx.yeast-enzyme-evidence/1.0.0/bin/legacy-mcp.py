"""Run the legacy installed read-only get_protein_info operation with its own stdio client."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import socket
import sys

from mcp import ClientSession
from mcp.client.stdio import stdio_client


async def main():
    mount = json.loads(Path(sys.argv[1]).read_text())
    accession = sys.argv[2]
    sys.path.insert(0, mount["mcp_runtime"])
    from runtime import ROOT, launch_settings, resolve_server
    service = resolve_server("uniprot")
    script = ROOT / service["path"] / service["entrypoint"]
    digest = sha256(script.read_bytes()).hexdigest()
    if digest != mount["mcp_script_sha256"]:
        raise ValueError("Deployed UniProt MCP entrypoint differs from the integrated build")
    launch = launch_settings(service)
    if launch.env["https_proxy"] != "http://127.0.0.1:7890" or launch.env["no_proxy"] != "localhost,127.0.0.1,::1":
        raise ValueError("Legacy server is missing its Clash proxy settings")
    with socket.create_connection(("127.0.0.1", 7890), timeout=3):
        pass
    async with asyncio.timeout(40), stdio_client(launch) as streams, ClientSession(*streams) as session:
        initialized = await session.initialize()
        result = await session.call_tool("get_protein_info", {"accession": accession, "format": "json"})
        if result.isError or len(result.content) != 1 or result.content[0].type != "text":
            raise ValueError("get_protein_info failed: " + str(result.content)[:450])
        print(json.dumps({"server": initialized.serverInfo.model_dump(mode="json"), "tool": "get_protein_info",
                          "commit":service["commit"], "deployed_build_sha256":digest, "transport":"stdio",
                          "provider_route":"legacy Clash proxy preload via http://127.0.0.1:7890",
                          "arguments":{"accession":accession,"format":"json"}, "record":json.loads(result.content[0].text)}))


if __name__ == "__main__":
    asyncio.run(main())
