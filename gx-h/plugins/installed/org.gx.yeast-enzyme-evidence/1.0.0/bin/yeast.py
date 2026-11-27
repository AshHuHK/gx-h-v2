"""Execute a yeast database Goal method as a detached GX CPU Job."""
import argparse
import json
from pathlib import Path
import runpy
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("index", "uniprot", "mcp"))
    parser.add_argument("--implementation", required=True)
    parser.add_argument("--mount", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--selection", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reader")
    args = parser.parse_args()
    method = runpy.run_path(args.implementation)
    mount = json.loads(args.mount.read_text())
    document = json.loads(args.input.read_bytes())
    if args.action == "index":
        candidates, sequences = method["run_lookup"](document, args.selection, mount)
        homologs, raw = method["homology"](sequences, mount)
        (args.out / "blast.tsv").write_text(raw)
        documents = {"candidates.json":candidates, "homology.json":homologs}
    elif args.action == "uniprot":
        raw, descriptor, record = method["fetch"](document, args.selection, runpy.run_path(args.reader)["retrieve"])
        (args.out / "uniprot.json").write_bytes(raw)
        documents = {"acquisition.json":descriptor, "record-summary.json":record}
    else:
        method["check_selection"](document, args.selection)
        result = subprocess.run([mount["mcp_python"], "-I", "-B", args.reader, str(args.mount), args.selection],
                                capture_output=True, text=True, timeout=50)
        if result.returncode:
            parser.exit(1, "UniProt MCP failed: " + result.stderr[-1200:])
        reply = json.loads(result.stdout)
        doc = method["check_record"](json.dumps(reply["record"]), args.selection)
        version = doc["entryAudit"]["entryVersion"]
        reply.update({"retrieved_at":method["now"](), "record_version":version, "evidence_level":"database_annotation",
                      "source":f"UniProtKB:{args.selection}/entry/{version}",
                      "limitation":"This tool reads the same UniProtKB entry as REST; it supplies no independent experimental evidence."})
        documents = {"mcp-readback.json":reply}
    for filename, content in documents.items():
        (args.out / filename).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
