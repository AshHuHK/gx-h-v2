"""Inspect actual role catalogs on the wire and run a delegated, cold-resumed read."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "acceptance"), str(ROOT / "gx-h")]
from native_child_access import run as child_read
from native_probe import INIT, Peer, Probe, _proxy_preflight
from native_probe_peer import wire_catalog
from gx_core.hostobs import host_policy
from gx_core.hostobs.app_server_client import AppServerClient
from gx_core.p0 import profiles, schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from gx_core.plugins.loader import bootstrap


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def paid_calls():
    source = Path("acceptance/v3/performance/callback-samples.jsonl.gz")
    counts, waves = defaultdict(lambda: defaultdict(Counter)), defaultdict(set)
    with gzip.open(ROOT / source, "rt") as stream:
        for call in map(json.loads, stream):
            arm = call["arm"]
            counts[arm][call["role"]][call["tool"]] += 1
            waves[arm].add(call["wave"])
    return {"path": str(source), "sha256": hash_file(ROOT / source), "calls": counts,
            "waves": {arm: sorted(items) for arm, items in waves.items()}}


class WirePeer(Peer):
    def record(self, body):
        row, index = super().record(body)
        specs, names = wire_catalog(body)
        row["tool_names"] = sorted(names)
        row["spawn_in_catalog"] = "spawn_agent" in names
        row["tool_bytes"] = len(json.dumps(specs, ensure_ascii=False, separators=(",", ":")).encode())
        base = body.get("instructions") or next(part["text"] for item in body["input"]
            if item.get("role") == "developer" for part in item.get("content", ())
            if part.get("text", "").startswith("You are Codex, "))
        row["base_instructions_sha256"] = hashlib.sha256(base.encode()).hexdigest()
        return row, index


def catalogs(work, binary):
    peer = WirePeer()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    probe = Probe(work / "catalog-host", binary, peer)
    knobs = {"GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": probe.provider, "GX_P0_MODEL": "gpt-6.1-sol",
             "GX_P0_CODEX": str(binary), "CODEX_HOME": str(probe.home), "GX_P0_PROFILE": ""}
    results = []
    try:
        with patch.dict(os.environ, knobs), closing(store.open_store(work / "catalogs.sqlite")) as conn:
            schema.install(conn)
            document, digest, host = host_policy.p0_host()
            # This is the same process-level multi-agent feature override used for GX local model tests.
            probe.client = AppServerClient(profiles.app_server_argv(test_mode=True, provider=probe.provider),
                cwd=probe.cwd, env=probe.env, on_notification=probe.notification,
                on_server_request=probe.callback, default_timeout=20).start()
            probe.rpc("initialize", INIT)
            probe.client.notify("initialized", {})
            for version in ("2", "3"):
                rid = records.create_run(conn, objective="Inspect a frozen grid", project_ref="catalog", actor="probe",
                    workflow_id="science_grid", workflow_version=version, agent_policy=document, host=host,
                    pins={"agent_policy_sha256": digest})
                plugin_ref = ("org.gx.science-grid", version + ".0.0")
                plugin = bootstrap(selected=[plugin_ref]).get(*plugin_ref)
                tool = plugin.contributions.tools["gx_grid_measurement"].as_spec()
                roots = Roots(conn, run_id=rid, cwd=probe.cwd, worker_tools=(tool,))
                for role in ("evidence_reviewer", "planner", "researcher", "executor"):
                    task = records.create_task(conn, run_id=rid, actor="probe", role=role, kind="research_round")
                    record = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
                    # This sample creates a brief and an official thread; it does not commission a scientific Job.
                    row = roots._new_row(task=record, attempt=1)
                    settings = roots._settings(row, new=True)
                    assert "baseInstructions" not in settings
                    tid = probe.rpc("thread/start", settings)["thread"]["id"]
                    probe.turn(tid, "Return the catalog observation.")
                    observed = peer.rows[-1]
                    names = sorted({name for name in observed["tool_names"] if name.startswith("gx_")})
                    assert names == sorted(json.loads(row["tool_names_json"])), (version, role, names)
                    assert observed["spawn_in_catalog"] == (version == "2"), (version, role, observed["tool_names"])
                    results.append({"workflow": "science_grid@" + version, "role": role, "thread": tid,
                        "model": observed["wire_model"], "gx_tools": names, "spawn_agent": observed["spawn_in_catalog"],
                        "tool_schema_bytes": observed["tool_bytes"],
                        "base_instructions_sha256": observed["base_instructions_sha256"]})
            assert len({row["base_instructions_sha256"] for row in results}) == 1
            return results
    finally:
        probe.close()
        peer.shutdown()
        peer.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=ROOT / "vendor/codex-runtime/package/vendor/"
                        "x86_64-unknown-linux-musl/bin/codex")
    args = parser.parse_args()
    _proxy_preflight()
    binary = args.binary.resolve()
    locked = json.loads((ROOT / "release-assets/lock.json").read_text())["linux_x64"]["binary_sha256"]
    assert hash_file(binary) == locked
    assigned = {"tools": ["gx_read_artifact", "gx_wait_job", "gx_submit_product"], "multi_agent": True}
    with tempfile.TemporaryDirectory(prefix="gx-v31-role-catalogs-") as temporary:
        work = Path(temporary)
        with patch("gx_core.p0.roots.tempfile.gettempdir", return_value=work):
            wire = catalogs(work, binary)
            delegated = child_read(work, binary, role_profile=assigned)
    history = paid_calls()
    for row in wire:
        if row["workflow"] == "science_grid@3":
            assert set(history["calls"]["candidate"].get(row["role"], ())) <= set(row["gx_tools"])
    result = {"binary_sha256": locked, "fixture": "official stdio App Server with loopback scripted Responses",
              "wire_catalogs": wire, "historical_callbacks": history, "explicit_review_assignment": assigned,
              "native_child": delegated}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    if not delegated["pass"]:
        raise SystemExit(str(delegated["checks"]))
    print("Verified eight official wire catalogs and scoped native child access across restart.")


if __name__ == "__main__":
    main()
