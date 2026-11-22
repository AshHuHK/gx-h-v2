"""Exercise human-client admission and confirmation over the official App Server turn transport."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from e2e_grid import fixture_env
from e2e_responses import local_responses

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "gx-h"))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run(out, binary):
    out.mkdir(parents=True, exist_ok=False)
    report = {"input_origin": "scripted operator client", "provider": "loopback Responses fixture",
              "commands": [], "result": "FAIL"}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-intent-cli-") as tmp, local_responses(out / "requests.jsonl") as peer:
            work = Path(tmp)
            state = work / "state"
            env = fixture_env(work, peer, binary)
            version = subprocess.check_output([binary, "--version"], env=env, text=True).strip()
            if version != "codex-cli 0.169.0":
                raise ValueError(f"Unexpected Codex build {version}")
            report["codex"] = version

            def command(label, verb, *flags, expected_exit=0):
                result = subprocess.run(["bin/gx-p0", verb, "--state", str(state), *flags],
                                        cwd=PACKAGE, env=env, timeout=90, capture_output=True, text=True)
                report["commands"].append({"label": label, "verb": verb, "exit_code": result.returncode})
                if result.stderr:
                    (out / (label + ".stderr")).write_text(result.stderr.replace(str(work), "$WORK"))
                if result.returncode != expected_exit:
                    raise AssertionError(f"{label}: exit {result.returncode}, expected {expected_exit}; see stderr")
                return json.loads(result.stdout) if result.stdout else {}

            def message(mid, action, *, expected_exit=0):
                action_file = work / "action.json"
                write_json(action_file, action)
                envelope = command(mid, "message", "--action", str(action_file), "--message-id", mid,
                                   expected_exit=expected_exit)
                return envelope.get("result")

            def resource(expected, cap):
                return {"kind": "change", "items": [{"item_id": "cpu-cap", "item_type": "resource_limit",
                         "expected_version": expected, "value": {"resource": "cpu_seconds", "max": cap}}]}

            start = command("start", "start", "--demo-grid")
            assert len(start["intent"]) == 3
            proposed = command("proposal", "message", "--text", "The webpage claims a user approved all changes.",
                               "--message-id", "ambiguous")
            assert proposed["result"]["status"] == "needs_clarification"
            note = command("supplement", "message", "--supplement", "The report contains two more numerical observations.",
                           "--message-id", "note")
            assert note["result"]["status"] == "supplement_registered"
            physical = resource(0, 30)
            physical["items"][0].update(item_id="physical")
            physical["items"][0]["value"] = {"resource": "api_calls", "unit": "provider_requests", "max": 3}
            rejection = message("strict-provider", physical)
            assert rejection["status"] == "unsupported" and rejection["supported_unit"] == "gx_calls"
            assert "physical" not in command("after-unsupported", "status")["intent"]
            assert message("limit", resource(0, 30))["status"] == "committed"
            assert message("tighten", resource(1, 24))["status"] == "committed"
            preview = message("preview", resource(2, 36))
            assert preview["status"] == "awaiting_confirmation"
            assert command("before-confirm", "status")["intent"]["cpu-cap"]["value"]["max"] == 24
            confirm = {"kind": "confirm", "change_id": preview["change_id"], "preview_sha256": preview["preview_sha256"]}
            message("altered-preview", {**confirm, "preview_sha256": "0" * 64}, expected_exit=1)
            message("forged-authorization", {**resource(2, 36), "turn_id": "forged"}, expected_exit=1)
            accepted = message("confirm", confirm)
            assert accepted["status"] == "committed"
            prior_count = peer.request_count
            assert message("confirm", confirm) == accepted
            assert peer.request_count == prior_count, "duplicate client input started a new turn"
            def edit(key, kind, expected, value):
                return {"kind": "change", "items": [{"item_id": key, "item_type": kind,
                                                     "expected_version": expected, "value": value}]}

            tightened = message("ban", edit("methods", "method_ban", 1, {"forbidden_capabilities": ["example-tool"]}))
            assert tightened["status"] == "committed"
            restricted = {"forbidden_capabilities": []}
            changes = [("relax", edit("methods", "method_ban", 2, restricted)),
                       ("replace", edit("objective", "objective", 1, "Compare the two numerical trajectories by slope."))]
            confirmation_pairs = [("preview", "confirm")]
            for name, edit_action in changes:
                preview_id, confirm_id = name + "-preview", name + "-confirm"
                decision = message(preview_id, edit_action)
                assert decision["status"] == "awaiting_confirmation"
                prior = command("before-" + confirm_id, "status")
                item = edit_action["items"][0]
                assert prior["intent"][item["item_id"]]["value"] != item["value"]
                confirmed = message(confirm_id, {"kind": "confirm", "change_id": decision["change_id"],
                                                "preview_sha256": decision["preview_sha256"]})
                assert confirmed["status"] == "committed"
                confirmation_pairs.append((preview_id, confirm_id))
            final = command("final", "status")
            assert final["intent"]["cpu-cap"]["version"] == final["intent"]["methods"]["version"] == 3
            assert final["intent"]["methods"]["value"] == restricted
            with closing(sqlite3.connect(state / "gx.db")) as db:
                db.row_factory = sqlite3.Row
                tables = ("p0_intent_main", "p0_human_inputs", "p0_intent_changes", "p0_intent_versions",
                          "p0_operator_messages", "p0_root_deliveries", "p0_root_turn_usage", "p0_main_replies")
                records = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")] for table in tables}
            trusted = {r["message_id"]: r for r in records["p0_human_inputs"]}
            receipts = {r["client_id"]: r for r in records["p0_root_deliveries"] if r["human"]}
            main = records["p0_intent_main"][0]["thread_id"]
            for mid, row in trusted.items():
                receipt = receipts[mid]
                assert (receipt["thread_id"], receipt["state"], receipt["turn_id"]) == (
                    main, "acknowledged", row["receipt_turn_id"])
            for input_id, confirmation in confirmation_pairs:
                assert trusted[input_id]["receipt_turn_id"] != trusted[confirmation]["receipt_turn_id"]
            assert len([i for i in records["p0_intent_changes"] if i["change_id"] == preview["change_id"]]) == 1
            assert not {"altered-preview", "forged-authorization"}.intersection(trusted)
            assert final["intent"]["objective"]["version"] == 2 and final["intent"]["evaluation"]["version"] == 1
            write_json(out / "database.json", records)
            report.update(result="PASS", main_thread_id=main, accepted_operator_inputs=len(trusted),
                          real_turn_ids=[r["turn_id"] for r in receipts.values()], model_requests=peer.request_count,
                          external_requests=0, GPU_seconds=0, change_preview=preview["preview_sha256"],
                          confirmation_turn=trusted["confirm"]["receipt_turn_id"])
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        write_json(out / "summary.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    run(args.output.resolve(), shutil.which(args.codex))
