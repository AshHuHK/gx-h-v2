"""Measure root/child accounting through production Roots with an official binary and loopback Responses."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch

from native_probe import Peer, Probe, find_binary, _proxy_preflight
from gx_core import budgets
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


def measure(directory, binary):
    peer = Peer()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    probe = Probe(directory / "official", binary, peer)
    knobs = {"GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": probe.provider, "GX_P0_MODEL": "gpt-6.1-sol"}
    try:
        with patch.dict(os.environ, knobs), closing(store.open_store(directory / "gx.sqlite")) as conn:
            schema.install(conn)
            budget = budgets.create_root(conn, run_id=None, profile_name="p0", actor="meter-probe")
            run = records.create_run(conn, objective="Measure delegated arithmetic reasoning", project_ref="meter",
                                     actor="meter-probe", budget_ref=budget)
            peer.scenario("spawn")
            argv = [str(binary), "--enable", "respect_system_proxy", "--disable", "system_proxy_fallback",
                    "app-server", "--listen", "stdio://"]
            with Roots(conn, run_id=run, cwd=probe.cwd, argv=argv, env=probe.env, token_budget=8000) as roots:
                tid = roots.ensure_main()
                turn = roots.human_turn("Delegate this synthetic arithmetic check to a temporary native child.")
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    roots.poll(.1)
                    ledger = budgets.state(conn, budget)
                    if ledger["spent"].get("tokens") and not ledger["reserved"]:
                        break
                child = conn.execute("SELECT * FROM p0_native_child_meters").fetchone()
                observations = [dict(r) for r in conn.execute("SELECT * FROM p0_native_child_usage")]
                original = roots._request("thread/read", {"threadId":child["child_id"]})["thread"] if child else {}
                raw = conn.execute("SELECT * FROM p0_root_turn_usage WHERE thread_id=? AND turn_id=?", (tid,turn)).fetchone()
                ledger = budgets.state(conn, budget)
                parent_tokens = sum(r["usage"]["total_tokens"] for r in peer.rows if not r["child"])
                child_tokens = sum(r["usage"]["total_tokens"] for r in peer.rows if r["child"])
                result = {"provider":"official Codex with loopback scripted Responses","run_id":run,
                          "root_thread_id":tid,"root_turn":turn,"root_usage":dict(raw) if raw else None,
                          "official_child_metadata":{k:original.get(k) for k in ("id","parentThreadId","sessionId","source")},
                          "child_meter":dict(child) if child else None,"child_usage":observations,
                          "responses_fixture":{"root_tokens":parent_tokens,"child_tokens":child_tokens,
                          "provider_requests":len(peer.rows)},"ledger":ledger,
                          "gx_tasks":conn.execute("SELECT count(*) FROM tasks").fetchone()[0],
                          "gx_leases":conn.execute("SELECT count(*) FROM leases").fetchone()[0]}
                result["pass"] = bool(child_tokens and child and child["metadata_verified"] and raw and
                                      raw["total_tokens"] == parent_tokens and not result["gx_tasks"] and
                                      not result["gx_leases"] and not ledger["reserved"] and
                                      ledger["spent"] == {"tokens":parent_tokens+child_tokens,"api_calls":1})
                return result
    finally:
        peer.gate.set()
        peer.shutdown()
        peer.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--binary",type=Path)
    args = parser.parse_args()
    _proxy_preflight()
    with TemporaryDirectory(prefix="gx-child-meter-") as tmp:
        verdict = measure(Path(tmp), args.binary or find_binary())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"pass": verdict["pass"],"usage":verdict["responses_fixture"],"spent":verdict["ledger"]["spent"]}))
    raise SystemExit(0 if verdict["pass"] else 1)
