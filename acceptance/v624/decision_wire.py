"""Capture the pinned official host's decision declaration and one correction on loopback."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]

from acceptance.v622.public import batch, funding, offline
from gx_core.domain import ids
from gx_core.p0 import v621_policy


def run(work, *, before_run=None):
    if work.is_relative_to(ROOT):
        raise ValueError("Select a fresh workspace outside the product")
    work.mkdir(parents=True, exist_ok=False)
    ready = Event()
    ready.set()
    binary = batch.old.BINARY
    with binary.open("rb") as source:
        binary_hash = hashlib.file_digest(source, "sha256").hexdigest()
    ledger = funding.BatchFunding(work / "funding.sqlite", series="V624-DECISION-WIRE", slots=[work.name])
    form, replies = {}, []
    calls = []
    def respond(wire, path, body, ident, assignment_complete, **kwargs):
        brief, _ = offline.delivered(body)
        assert brief["evidence_policy"] == v621_policy.VERSION and brief["scope"] == "run"
        specs, _ = batch.old.wire_catalog(body)
        declaration, = [leaf for group in specs if group.get("name") == "gx"
                        for leaf in group.get("tools", []) if leaf["name"] == "gx_decide_revision"]
        batch.save(work / "declared-tool.json", declaration)
        schema = declaration["parameters"]["properties"]["decision"]
        variants = schema["properties"]["ops"]["items"]["oneOf"]
        required = {v["properties"]["op"]["enum"][0]: v["required"] for v in variants}
        observed = dict(required=schema["required"], add_node_required=required["add_node"],
                        description=declaration["description"],
                        schema_sha256=ids.sha256_of_obj(declaration["parameters"]),
                        instructions_received="Use gx_decide_revision with rationale and ops" in json.dumps(body))
        if form:
            assert form == observed
        form.update(observed)
        call_ids = {row["call_id"] for row in calls}
        for item in body["input"]:
            if item.get("type") == "function_call_output" and item.get("call_id") in call_ids:
                result = batch.old.responses.output_json(item["output"])
                replies.append({key:result[key] for key in ("code", "reason", "field")})
        if not calls:
            args = {"unexpected": "private rejection payload marker"}
        elif len(calls) == 1 and replies[-1].get("field") == "decision":
            args = {"decision": {"rationale": "The protocol probe has received the corrected field.",
                    "ops": [{"op":"close_run", "reason":"The protocol observation is complete.",
                             "claim_refs": [], "unresolved": ["No scientific outcome was tested."]}]}}
        else:
            raise ValueError("Unexpected extra wire callback")
        call_id = "call-" + ident
        calls.append({"call_id":call_id, "payload_sha256":ids.sha256_of_obj(args)})
        call = {"type":"function_call", "name":"gx_decide_revision", "namespace":"gx",
                "arguments":json.dumps(args), "call_id":call_id}
        return call, {"scope":"run", "tool":"gx_decide_revision"}

    source = ROOT / "examples/v62a-data-inventory/grant.json"
    grant = json.loads(source.read_bytes())
    for row in grant["input_files"]:
        row["path"] = str((source.parent / row["path"]).resolve())
    grant.update(research_effort="deep", research_modules=[], plugins=["org.gx.protein@0.3.0"],
                 capabilities=["deposited-chain-geometry@2"])
    batch.save(work / "grant.json", grant)
    with (work / "lock").open("w") as lock, patch.object(offline, "action", respond), offline.session(
            work, binary, "standard", ready, ledger) as (env, wire, peer):
        cli = batch.native_cli(work, env, lock.fileno())
        rid = cli("start", "--goal", "Inspect the decision tool contract on the official host.",
                  "--run-key", "wire-retry", "--goal-config", work / "grant.json")["run_id"]
        if before_run is not None:
            before_run(work, rid)
        final = cli("run", "--run-id", rid, "--timeout", 28, timeout=47)
        faults = [row["error"] for row in peer.rows if "error" in row]
    with closing(batch.old.db_open(work)) as conn:
        events = [dict(kind=r[0], sequence=r[1], subject=r[2], payload=json.loads(r[3]))
                  for r in conn.execute("""SELECT kind,event_id,subject_id,payload_json FROM events
                              WHERE kind LIKE 'v624.decision.%' AND run_id=? ORDER BY event_id""", (rid,))]
        decisions = [json.loads(r[0]) for r in conn.execute("SELECT receipt_json FROM v6_decisions WHERE run_id=?", (rid,))]
    receipt = dict(transport="pinned official Codex App Server / scripted localhost Responses",
                   actual_paid_calls=0, official_binary_sha256=binary_hash, goal_run=rid,
                   declared=form, rejection_replies=replies, events=events, requests=len(calls),
                   adopted_events=[r["event_id"] for r in decisions], status=final["goal_status"], errors=faults)
    batch.save(work / "decision-wire.json", receipt)
    assert not faults and final["goal_status"] == "CLOSED", receipt
    assert form["required"] == ["rationale", "ops"] and form["instructions_received"]
    assert form["add_node_required"] == ["op", "capability", "inputs", "question"]
    assert '"metric_refs"' in form["description"] and '"op":"add_node"' in form["description"]
    assert replies == [{"code":"SCHEMA_INVALID","reason":"DECISION_OBJECT_REQUIRED","field":"decision"}]
    assert len(calls) == 2 and len(decisions) == 1
    rejected, corrected = events
    assert rejected["kind"] == "v624.decision.rejected" and corrected["kind"] == "v624.decision.corrected"
    assert rejected["subject"] == corrected["subject"]
    assert rejected["payload"]["attempt"] == 1 and corrected["payload"]["previous_rejections"] == 1
    assert corrected["payload"]["decision_event"] == decisions[0]["event_id"]
    assert "private rejection payload marker" not in json.dumps(events)
    return work / "decision-wire.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    print(run(parser.parse_args().work.resolve()))
