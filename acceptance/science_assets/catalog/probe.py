"""Read a deployed inventory through a normal Goal and persist native lookup receipts."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import branch, draft
from gx_core import config
from gx_core.errors import ScopeDenied
from gx_core.p0 import cli


def probe(directory):
    if (directory / "state").exists():
        raise ValueError("Use a new private study directory to retain previous evidence")
    directory.mkdir(parents=True, exist_ok=True)
    with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), \
            redirect_stdout(io.StringIO()):
        status = cli.main(["research", "--state", str(directory / "state"), "--run-key", "asset-catalog",
                           "--goal", "Which archived records support interpreting a deposited structure?",
                           "--research-effort", "deep"])
        if status:
            raise RuntimeError(f"research admission failed with exit status {status}")
    calls = []
    with CloseStudy(directory, reopen=True) as study:
        lead = study.next_actor("run")
        def query(key, **args):
            reply = study.call(lead, "gx_query_catalog", args, key)
            calls.append({"call_id": key, "arguments": args, "reply": reply})
            return reply

        legacy = "protenix-model@legacy"
        for kind in ("all", "method"):
            listing = query(f"word-{kind}", kind=kind, query="Protenix", required_outputs=["model summary"])
            assert legacy in {r["reference"] for r in listing["items"]}
            source = query(f"detail-{kind}", kind=kind, reference=legacy)
            assert not source["execution_granted"] and source["provider"] is None and source["spec"]["source"]
        chinese = query("ordinary-question", kind="method", query="可以找到這份結構模型的舊資產嗎")
        assert legacy in {r["reference"] for r in chinese["items"]}
        database = query("two-providers", kind="database", query="pdb-archive")
        by_provider = [r for r in database["items"] if r.get("canonical_method_id") == "pdb-archive"]
        assert len(by_provider) >= 2
        bindings = [query(f"pdb-binding-{i}", kind="database", reference=r["reference"])["spec"]["provider_binding"]
                    for i, r in enumerate(by_provider)]
        assert len({b["id"] for b in bindings}) >= 2
        attempts = [
            ("gx_select_capability", {"method": legacy, "action": "enable", "revision": 0}),
            ("gx_decide_revision", {"decision": draft(branch("archive"), {"op": "add_node", "node": "local:read",
                "branch": "local:archive", "inputs": [], "capability": legacy, "parameters": {},
                "depends_on": [], "question": "Can the historical record execute here?"})})]
        refused = []
        for tool, args in attempts:
            try:
                study.call(lead, tool, args, "refusal-" + tool)
            except ScopeDenied as error:
                refused.append({"tool": tool, "code": error.code, "message": error.message})
            else:
                raise AssertionError("A historical record was treated as runnable")
        assert not study.rows("SELECT job_id FROM jobs")
        return {"transport": "Controlled native callbacks after gx-p0 research admission", "model_calls": 0,
                "jobs": 0, "run_id": study.run_id, "state_directory": directory.name + "/state",
                "inventory_sha256": hashlib.sha256((config.CONFIG_DIR/"science_asset_inventory.json").read_bytes()).hexdigest(),
                "calls": calls, "refusals": refused}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = probe(args.directory)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
