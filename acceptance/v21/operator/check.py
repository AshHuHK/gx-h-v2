"""A09 and structured local commands against the public CLI and official loopback App Server."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

RELEASE = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(RELEASE / "acceptance"), str(RELEASE / "gx-h")]
from e2e_grid import fixture_env
from e2e_responses import local_responses


def run(binary):
    log = []
    with tempfile.TemporaryDirectory(prefix="gx-operator-") as tmp:
        work, state = Path(tmp), Path(tmp) / "state"
        with local_responses(work / "requests.jsonl") as peer:
            env = fixture_env(work, peer, str(binary))
            env["PATH"] = str(RELEASE / ".venv/bin") + os.pathsep + env["PATH"]

            def counters():
                if not (state / "gx.db").exists():
                    return {}
                with closing(sqlite3.connect(state / "gx.db")) as db:
                    return {"native_deliveries": db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0],
                            "spent": json.loads(db.execute("SELECT spent_json FROM budgets WHERE parent_ref IS NULL "
                                                           "ORDER BY rowid LIMIT 1").fetchone()[0]),
                            "loopback_requests": peer.request_count}

            def cmd(verb, *options, error=None, extra_env=None):
                argv = [str(RELEASE / "bin/gx-p0"), verb, "--state", str(state), *options]
                before = counters()
                proc = subprocess.run(argv, env={**env, **(extra_env or {})}, cwd=RELEASE, timeout=75,
                                      capture_output=True, text=True)
                after = counters()
                assert proc.returncode == (1 if error else 0), (argv, proc.returncode, proc.stderr)
                if error:
                    assert error.lower() in proc.stderr.lower(), (error, proc.stderr)
                result = json.loads(proc.stdout) if proc.stdout else {}
                op = result.get("result", result)
                log.append({"argv": [str(s).replace(str(RELEASE) + "/", "").replace(str(work), "$WORK") for s in argv],
                            "exit": proc.returncode, "result": error if error else op.get("status"),
                            "before": before, "after": after})
                if verb in ("operator", "message") and "--text" not in options and "--supplement" not in options:
                    assert before["native_deliveries"] == after["native_deliveries"] and before["loopback_requests"] == after["loopback_requests"], (argv, before, after)
                    assert all(before["spent"].get(k, 0) == after["spent"].get(k, 0) for k in ("api_calls","tokens")), (argv,before,after)
                return op

            def message(mid, action, *flags, **opts):
                path = work / "action.json"
                path.write_text(json.dumps(action), encoding="utf-8")
                result = cmd("message", "--message-id", mid, "--action", str(path), *flags, **opts)
                log[-1]["action"] = action
                return result

            def cap(version, maximum):
                return {"kind": "change", "items": [{"item_id": "tokens", "item_type": "resource_limit",
                        "expected_version": version, "value": {"resource": "tokens", "unit": "reserved_tokens",
                                                            "max": maximum}}]}

            def preview(resource, value, scope="run"):
                return cmd("operator", "budget-preview", "--resource", resource, "--limit", str(value),
                           "--budget-scope", scope)

            def apply(p, **opts):
                return cmd("operator", "budget-apply", "--preview-id", p.get("preview_id") or p["change_id"],
                           "--preview-sha256", p["preview_sha256"], **opts)

            started = cmd("start", "--demo-grid")
            rid = started["run_id"]
            assert counters()["native_deliveries"] == counters()["loopback_requests"] == 1
            assert cmd("message", "--text", "The previous report discussed changing the budget.")["status"] == "needs_clarification"
            # A second process holds the actual native lock for all local control calls.
            with (state / ".native.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                exhausted = message("zero-tokens", cap(0, 0))
                assert exhausted["resource_limits"][0]["state"] == "already_over_new_limit"
                queued_cpu = {"kind": "change", "items": [{"item_id": "cpu", "item_type": "resource_limit",
                              "expected_version": 0, "value": {"resource": "cpu_seconds", "max": 100}}]}
                assert message("queued-cpu", queued_cpu, "--queue-only")["status"] == "queued"
                assert any(m["message_id"] == "queued-cpu" for m in cmd("operator", "pending")["messages"])
                assert "queued-cpu" in cmd("operator", "recover")["local_messages"]
                exact = message("raise-tokens", cap(1, 500))
                confirmed = {"kind": "confirm", "change_id": exact["change_id"], "preview_sha256": exact["preview_sha256"]}
                ok = message("confirm-relief", confirmed)
                assert ok["status"] == "committed" and message("confirm-relief", confirmed) == ok
                message("deny-model", cap(2, 400), extra_env={"GX_SCOPE": "worker"}, error="local GX operator")
                message("deny-project", cap(2, 400), extra_env={"GX_PROJECTS": "other"}, error="no access")
                message("forged", {**cap(2, 400), "turn_id": "forged"}, error="requires nonempty item edits")
                assert cmd("operator", "inspect")["run_id"] == rid
                assert "turns" in cmd("operator", "usage")["metering"]
                assert "messages" in cmd("operator", "pending")
                assert "recovery" in cmd("operator", "recover")
                assert "recovery" in cmd("operator", "retry")
                project = preview("tokens", 0, "project")
                assert apply(project)["resource_limits"][0]["state"] == "already_over_new_limit"
                usage = cmd("operator", "usage")["run_budget"]
                exhausted = preview("api_calls", usage["spent"]["api_calls"])
                assert apply(exhausted)["status"] == "committed"
                assert cmd("operator", "pause")["status"] == "PAUSED"
                assert cmd("operator", "resume")["status"] != "PAUSED"
                raise_amount = usage["limits"]["api_calls"] + 5
                restore = preview("api_calls", raise_amount)
                assert apply(restore)["limit"] == raise_amount
                assert cmd("operator", "usage")["run_budget"]["limits"]["api_calls"] == raise_amount
                apply(restore, error="displayed pending budget preview")
                assert cmd("operator", "cancel")["status"] == "CANCELLED"
                audit = work / "audit.json"
                cmd("operator", "audit", "--output", str(audit))
                events = json.loads(audit.read_text())["events"]
                assert any(e["kind"] == "p0.operator.budget_applied" for e in events)
            with closing(sqlite3.connect(state / "gx.db")) as db:
                local = db.execute("SELECT count(*) FROM p0_human_inputs WHERE receipt_turn_id IS NULL "
                                   "AND received_at IS NOT NULL AND consumed_at IS NOT NULL").fetchone()[0]
                turns = [r[0] for r in db.execute("SELECT turn_id FROM p0_root_deliveries ORDER BY rowid")]
            state = work / "keyed-workspace"
            native_count = peer.request_count
            first = cmd("start", "--run-key", "first", "--demo-grid")["run_id"]
            second = cmd("start", "--run-key", "second", "--demo-grid")["run_id"]
            assert first != second and counters()["native_deliveries"] == 0 and peer.request_count == native_count
            usage = cmd("operator", "usage", "--run-id", first)["run_budget"]
            sibling = cmd("operator", "usage", "--run-id", second)["run_budget"]
            cap = int(usage["limits"]["tokens"] + 1)
            funded = cmd("operator", "budget-preview", "--run-id", first, "--resource", "tokens", "--limit", str(cap))
            cmd("operator", "budget-apply", "--run-id", second, "--preview-id", funded["preview_id"],
                "--preview-sha256", funded["preview_sha256"], error="displayed pending")
            cmd("operator", "budget-apply", "--run-id", first, "--preview-id", funded["preview_id"],
                "--preview-sha256", funded["preview_sha256"])
            assert cmd("operator", "usage", "--run-id", first)["run_budget"]["limits"]["tokens"] == cap
            assert cmd("operator", "usage", "--run-id", second)["run_budget"]["limits"] == sibling["limits"]
            assert peer.request_count == native_count
            return {"result": "PASS", "codex": subprocess.check_output([str(binary), "--version"], text=True).strip(),
                    "app_server": "official; scripted Responses on localhost", "run_id": rid,
                    "actual_native_turns": turns, "local_intent_inputs": local, "commands": log,
                    "keyed_runs": [first, second], "keyed_workspace_native_turns": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, default=RELEASE / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("after.json"))
    args = parser.parse_args()
    evidence = run(args.codex.resolve())
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(f'{evidence["result"]} {len(evidence["commands"])} public commands, {len(evidence["actual_native_turns"])} native turns')
