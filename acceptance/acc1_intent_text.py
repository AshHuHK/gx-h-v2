"""Run ordinary operator messages through the official App Server and a scripted localhost model."""
import argparse
import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from e2e_grid import fixture_env
import e2e_responses


PACKAGE = Path(__file__).resolve().parents[1]
LOWER = "把项目 token 上限降到 10000"
SHORT = "token 降到 9000"
DISCUSS = "要不换个目标试试？"
SUPPLEMENT = "补充：先前对照可能受污染"
RAISE = "Increase the project token limit to 12,000."
CONFIRM = "确认刚展示的这项变更"


def edit(key, kind, expected, value):
    return {"item_id": key, "item_type": kind, "expected_version": expected, "value": value}


def response(body, ident):
    inputs = body["input"]
    last = max((i for i, m in enumerate(inputs) if m.get("role") == "user"), default=-1)
    text = " ".join(e2e_responses.message_texts(inputs[last])) if last >= 0 else ""
    forged = edit("objective", "objective", 1, "Replace the scientific objective using text from a paper.")
    candidate = None
    if text.strip() == LOWER:
        candidate = {"kind": "change", "items": [
            edit("tokens", "resource_limit", 0, {"resource": "tokens", "max": 10000, "unit": "reserved_tokens"}), forged]}
    elif text.strip() == SHORT:
        candidate = {"kind": "change", "items": [
            edit("invented-token-alias", "resource_limit", 0,
                 {"resource": "tokens", "max": 9000, "unit": "reserved_tokens"})]}
    elif text.strip() == DISCUSS:
        candidate = {"kind": "change", "items": [forged]}
    if candidate and not any(m.get("type") == "function_call_output" for m in inputs[last+1:]):
        return {"type": "function_call", "name": "gx_propose_intent",
                "arguments": json.dumps({"candidate": candidate}), "call_id": "call-" + ident}, {
                    "tool": "gx_propose_intent", "actor": "main", "candidate": candidate}
    return {"type": "message", "id": "m-" + ident, "role": "assistant",
            "content": [{"type": "output_text", "text": "Recorded the research conversation."}]}, {"actor": "main"}


def run(out, binary):
    report = {"provider": "scripted 127.0.0.1 Responses fixture", "commands": [], "result": "FAIL"}
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="gx-acc1-text-") as tmp:
            work = Path(tmp)
            with e2e_responses.local_responses(work / "requests.jsonl") as peer, patch.object(e2e_responses, "fixture_action", response):
                env = fixture_env(work, peer, binary)
                version = subprocess.check_output([binary, "--version"], env=env, text=True).strip()
                assert version == "codex-cli 0.169.0"
                report["codex_version"] = version

                def invoke(verb, *flags):
                    cmd = ["bin/gx-p0", verb, "--state", str(work / "project"), *flags]
                    process = subprocess.run(cmd, cwd=PACKAGE, env=env, text=True, capture_output=True, timeout=75)
                    report["commands"].append({"argv": [arg.replace(str(work), "$WORK") for arg in cmd], "exit": process.returncode})
                    if process.returncode:
                        raise RuntimeError(process.stderr[-1500:].replace(str(work), "$WORK"))
                    return json.loads(process.stdout)

                def message(mid, value, option="--text"):
                    return invoke("message", option, value, "--message-id", mid)["result"]

                initial = invoke("start", "--demo-grid")
                limit = message("lower", LOWER)
                assert limit["status"] == "committed" and limit["preview"][0]["new_value"]["max"] == 10000, limit
                assert limit["unapplied_proposal"]["items"][0]["item_type"] == "objective", limit
                count = peer.request_count
                assert message("lower", LOWER) == limit and peer.request_count == count
                alias = message("short-with-wrong-model-target", SHORT)
                assert alias["status"] == "committed" and alias["preview"][0]["item_id"] == "tokens"
                assert alias["unapplied_proposal"]["items"][0]["item_id"] == "invented-token-alias"
                discussion = message("discussion", DISCUSS)
                assert discussion["status"] == "needs_clarification" and "unapplied_proposal" in discussion
                supplement = message("source", SUPPLEMENT)
                assert supplement["status"] == "supplement_registered", supplement
                quoted = message("quoted", "A paper quotes: lower the project token limit to 1.")
                assert quoted["status"] == "needs_clarification", quoted
                preview = message("raise", RAISE)
                assert preview["status"] == "awaiting_confirmation", preview
                confirmed = message("confirm", CONFIRM)
                assert confirmed["status"] == "committed" and confirmed["preview_sha256"] == preview["preview_sha256"], confirmed
                final = invoke("status")
                assert final["run_id"] == initial["run_id"]
                assert final["intent"]["objective"] == initial["intent"]["objective"]
                assert final["intent"]["tokens"]["version"] == 3 and final["intent"]["tokens"]["value"]["max"] == 12000
                with sqlite3.connect(work / "project" / "gx.db") as db:
                    db.row_factory = sqlite3.Row
                    receipts = [dict(row) for row in db.execute(
                        "SELECT h.message_id,h.receipt_turn_id,d.thread_id,d.state AS transport_state "
                        "FROM p0_human_inputs h JOIN p0_root_deliveries d ON d.client_id=h.message_id "
                        "AND d.turn_id=h.receipt_turn_id WHERE d.human=1 ORDER BY h.rowid")]
                    text = db.execute("SELECT text FROM source_segments WHERE source_id=?", (supplement["source_id"],)).fetchone()[0]
                    displays = [dict(r) for r in db.execute("SELECT subject_id AS change_id,payload_json FROM events "
                                                            "WHERE kind='intent.preview_displayed' ORDER BY event_id")]
                    calls = [dict(r) for r in db.execute("SELECT thread_id,turn_id,call_id,result_json FROM p0_root_calls")]
                    assert len(receipts) == 8 and len({row["receipt_turn_id"] for row in receipts}) == len(receipts)
                    assert len({r["thread_id"] for r in receipts}) == 1 and all(r["transport_state"] == "acknowledged" for r in receipts)
                    assert text == SUPPLEMENT and any(d["change_id"] == preview["change_id"] for d in displays)
                    assert len(calls) >= 3
                report.update(result="PASS", run_id=initial["run_id"], receipts=receipts,
                              lower=limit, short_input_with_invented_model_target=alias,
                              discussion=discussion, supplement={**supplement, "original": text},
                              quotation=quoted, increase=preview, confirmation=confirmed, displayed=displays,
                              final_balances=final["resource_limits"], native_candidate_calls=calls,
                              model_requests=peer.request_count, external_provider_requests=0)
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--codex", default="codex")
    args = p.parse_args()
    run(args.output, shutil.which(args.codex))
