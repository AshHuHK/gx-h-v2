"""Probe the direct GX submit surface and the yieldable nested-call control on official Codex.

The paid model's locally cached catalog entry supplies tool rendering and mode. All
Responses requests go to a loopback fixture and use no account or model credentials.
"""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest.mock import patch

from terminal_protocol_probe import (NO_TOOL_RESPONSE, ArtifactService, Peer, Probe, Roots, Wire, _proxy_preflight,
    budgets, default_registry, identity, initial_store, meter, records, schema, store, submissions)
from native_probe_peer import wire_catalog
from gx_core.p0 import intent
from workflows.science_grid_v3 import products

MODEL = "gpt-6.2-astra"
MODE_SOURCES = (
    "codex-rs/core/src/tools/spec_plan.rs",
    "codex-rs/core/src/tools/spec_plan_tests.rs",
    "codex-rs/core/src/tools/spec_plan_strict_third_party_tests.rs",
)
SCENARIOS = (
    ("legacy_yield_control", "code_mode_only", "ACCEPTED"),
    ("paid_catalog_accept", "code_mode_only", "ACCEPTED"),
    ("paid_catalog_reject", "code_mode_only", "REJECTED"),
    ("code_mode_accept", "code_mode", "ACCEPTED"),
    ("direct_accept", "direct", "ACCEPTED"),
)


def tool_catalog(body):
    top = body.get("tools", [])
    native = any(item.get("type") == "additional_tools" and item["tools"] for item in body["input"])
    tools, _ = wire_catalog(body)
    groups = [t for t in tools if t.get("type") == "namespace" and t.get("name") == "gx"]
    return {"location": [key for key, found in (("tools", top), ("input.additional_tools", native)) if found],
            "direct_product_groups": [[s["name"] for s in t["tools"]] for t in groups],
            "nested_code_available": any(t.get("name") == "exec" or
                  any(n.get("name") == "exec" for n in t.get("tools", [])) for t in tools)}


class Model(Peer):
    def __init__(self, case, mode):
        super().__init__()
        self.case, self.mode = case, mode
        self.gate.clear()
        self.committed = threading.Event()
        self.release_extra = threading.Event()

    def record(self, body):
        r = {"id": "wire-response-" + str(len(self.rows) + 1), "t": time.monotonic(),
             "model": body["model"], "catalog": tool_catalog(body),
             "usage": {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}}
        self.rows.append(r)
        return r, len(self.rows)

    def item(self, row, n):
        assert self.gate.wait(12)
        args = json.dumps({"product": self.product})
        if self.case == "legacy_yield_control":
            if n == 1:
                source = '// @exec: {"yield_time_ms": 25}\ntext(await tools.gx_submit_product(' + args + '));'
                return {"type": "custom_tool_call", "namespace": "functions", "name": "exec",
                        "call_id": "yielded-wrapper", "input": source}
            row["held_until_turn_interrupted"] = self.release_extra.wait(8)
            return self.message(row["id"], "The extra request reached the local provider")
        if n == 1 and self.mode != "direct":
            source = ('text({matches:ALL_TOOLS.filter(t=>t.name.includes("gx_submit_product")).map(t=>t.name),'
                      'plain:typeof tools.gx_submit_product,grouped:typeof tools.gx_gx_submit_product,'
                      'reader:typeof tools.gx_read_artifact});'
                      'try{await tools.gx_gx_submit_product(' + args + ');}catch(e){text(e.name);}')
            return {"type": "custom_tool_call", "namespace": "functions", "name": "exec",
                    "call_id": "inspect-and-attempt-nested", "input": source}
        if n <= 2:
            return {"type": "function_call", "namespace": "gx", "name": "gx_submit_product",
                    "call_id": "direct-submit", "arguments": args}
        return self.message(row["id"], "A later request reached the local provider")


def v3_store(conn, url, probe, expected, metering_grace=None):
    schema.install(conn)
    workflow = default_registry().get("science_grid", "3")
    with store.transaction(conn):
        budget = budgets.create_root(conn, run_id=None, actor="protocol-fixture", profile_name="p0")
        run = records.create_run(conn, objective="Check one failed synthetic measurement", project_ref="direct-product",
            actor="fixture", budget_ref=budget, workflow_id="science_grid", workflow_version="3",
            inputs=None if metering_grace is None else {"inputs": {"p0_spec": {"metering": {"grace_seconds": metering_grace}}}},
            pins={"workflow_sha256": workflow.digest()}, host={"policy_selection": {
                "test_mode": True, "model_provider": probe.provider, "test_base_url": url}})
        for state in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(conn, machine="run", subject_id=run, to_state=state, actor="fixture")
        intent.bind_main(conn, run, "protocol-coordinator")
        note = {"kind": "supplement", "text": "The source describes a different synthetic sample."}
        intent.stage_input(conn, run, "sample-note", note)
        receipt = intent.accept_input(conn, run, "protocol-coordinator", "note-turn", "sample-note")
        source = receipt["source_id"]
        assets = ArtifactService(conn)
        def publish(kind, value):
            return assets.publish_bytes(namespace=run, rel_path=kind + ".json", run_id=run, kind=kind,
                                        media_type="application/json", data=json.dumps(value)).artifact_ref
        grid = publish("grid_input", {"synthetic": True, "candidates": [{"candidate_id": "sample", "x": 0}]})
        proposal = publish("research_proposal", {"author_principal": "fixture-proposer", "threshold": 1,
            "method": "fixture-grid@1", "paths": [{"id": "sample", "prediction": "The score reaches one.",
            "input_ref": grid, "expected_reaches_threshold": True, "claim_scope": "interpretation"}]})
        evidence = publish("research_evidence", {"proposal_ref": proposal, "routes": [{"path_id": "sample",
             "job_id": "fixture-failed-measurement", "input_ref": grid, "status": "FAILED",
             "outcome_kind": "measurement_failure"}]})
        inputs, _ = products.for_review(conn, run, {"round": 0, "max_rounds": 1,
                       "proposal_ref": proposal, "evidence_refs": [evidence]})
        product = {"findings_by_path": {"sample": {"reason": "The recording failed.",
                   "risk": "The instrument returned no observation."}},
                   "source_reviews_by_id": {source: {"disposition": "rejected", "reason": "Different sample.",
                   "compared_paths": ["sample"], "qualifies_paths": ["sample"] if expected == "REJECTED" else []}}}
    return run, inputs, product


def trial(directory, binary, entry, case, mode, expected, *, model=None, post_commit_delay=0.25, missing_final=False,
          metering_grace=None):
    legacy = case == "legacy_yield_control"
    model = model or Model(case, mode)
    with ExitStack() as stack:
        threading.Thread(target=model.serve_forever, daemon=True).start()
        stack.callback(model.server_close)
        stack.callback(model.shutdown)
        stack.callback(model.release_extra.set)
        stack.callback(model.gate.set)
        probe = Probe(directory, binary, model)
        # For alternate modes use the identical entry with this one explicit test override.
        selected = {**entry, "tool_mode": mode}
        catalog_path = directory / "catalog.json"
        catalog_path.write_text(json.dumps({"models": [selected]}))
        config = probe.home / "config.toml"
        config.write_text("model_catalog_json=" + json.dumps(str(catalog_path)) + "\n" + config.read_text().replace(
            "[features]\n", "[features]\ncode_mode={enabled=true,direct_only_tool_namespaces=['existing_group']}\n"
            "code_mode_only_strict_3p_tools=true\n"))
        db = directory / "gx.db"
        conn = stack.enter_context(closing(store.open_store(db)))
        reader = stack.enter_context(closing(sqlite3.connect(db)))
        reader.row_factory = sqlite3.Row
        url = f"http://127.0.0.1:{model.server_port}/v1"
        if legacy:
            run, proposal, evidence = initial_store(conn, url, probe.provider)
            inputs = {"output_kind": "science_assessment", "round": 0, "proposal_ref": proposal, "evidence_refs": [evidence]}
            model.product = {"proposal_ref": proposal, "evidence_refs": [evidence], "findings": [{"path_id": "sample",
                             "reason": "The sample failed.", "evaluation_ref": None, "supported": None}]}
        else:
            run, inputs, model.product = v3_store(conn, url, probe, expected, metering_grace)
        knobs = {"GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": probe.provider,
                 "GX_P0_MODEL": MODEL, "GX_P0_CODEX": str(binary), "CODEX_HOME": str(probe.home)}
        stack.enter_context(patch.dict(os.environ, knobs))
        roots = stack.enter_context(Roots(conn, run_id=run, cwd=probe.cwd, argv=[str(binary), "app-server",
                                   "--listen", "stdio://"], env=probe.env, token_budget=1000))
        task = records.create_task(conn, run_id=run, kind="research_round", role="evidence_reviewer",
            actor="protocol-fixture", required_artifacts=["science_assessment"], task_input=inputs,
            acceptance={"validate_workflow_product": True})
        store.transition(conn, machine="task", subject_id=task, to_state="READY", actor="fixture")
        client = roots.client
        rec = {"case": case, "mode": mode, "catalog_mode_override": mode != entry["tool_mode"], "fault": "none",
               "run": run, "task": task, "expected": expected, "interrupt_writes": [], "tool_responses_written": [],
               "server_interrupt_replies": [], "observed_calls": [], "events": [], "requests": model.rows}
        stack.enter_context(patch.object(client, "_transport", Wire(client.transport, rec)))
        original_observe, original_callback = client.on_notification, client.on_server_request
        def observe(frame):
            p = frame.message.get("params", {})
            method, item = frame.message["method"], p.get("item") or {}
            if method == "rawResponseItem/completed" and item.get("type") in (
                    "custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output"):
                rec["events"].append({"seq": frame.receive_seq, "type": item["type"],
                    **{k: item[k] for k in ("call_id", "name", "namespace", "output") if k in item}})
            if method == "item/completed" and item.get("type") == "dynamicToolCall":
                rec["events"].append({"seq": frame.receive_seq, "type": "dynamicToolCall", "id": item["id"],
                                      "namespace": item.get("namespace"), "status": item["status"]})
            if method == "rawResponse/completed":
                rec["events"].append({"seq": frame.receive_seq, "type": method, "id": p["responseId"], "t": time.monotonic(),
                                      "usage": p.get("usage")})
            original_observe(frame)
        def callback(frame):
            p = frame.message["params"]
            rec["observed_calls"].append({"seq": frame.receive_seq, "namespace": p.get("namespace"),
                                          "call": p["callId"], "tool": p["tool"]})
            if legacy:
                roots._call(p)
                result = NO_TOOL_RESPONSE
            else:
                result = original_callback(frame)
            committed = submissions(reader, task)
            assert result is NO_TOOL_RESPONSE and len(committed) == 1 and committed[0]["result"] == expected, (
                rec["observed_calls"], committed, result)
            rec.update(committed_at=time.monotonic(), submission=committed[0])
            model.committed.set()
            return result
        client.on_notification, client.on_server_request = observe, callback
        work = roots.dispatch(task)
        thread, turn = work["thread_id"], work["turn_id"]
        rec.update(thread=thread, turn_id=turn, encoded_worker_override=roots._product_tool_config)
        model.gate.set()
        client.run_until(lambda f: f.message.get("method") == "item/tool/call", timeout=10)
        if legacy:
            client.request("thread/goal/set", {"threadId": thread, "status": "complete", "origin": "automatic"})
        if post_commit_delay:
            time.sleep(post_commit_delay)
        if legacy:
            client.request("turn/interrupt", {"threadId": thread, "turnId": turn})
            client.run_until(lambda f: f.message.get("method") == "turn/completed", timeout=5)
            model.release_extra.set()
        else:
            end = time.monotonic() + 8
            while meter(reader, thread, turn)["terminal_status"] is None and time.monotonic() < end:
                roots.poll(0.01)
            rec["terminal_receipt"] = dict(reader.execute("SELECT * FROM p0_root_terminals WHERE thread_id=?",
                                                         (thread,)).fetchone())
            assert rec["terminal_receipt"]["reason"] == ("GX_ACCEPTED" if expected == "ACCEPTED" else "GX_TERMINAL_REJECTED")
        history = client.request("thread/turns/list", {"threadId": thread, "limit": 1, "itemsView": "full"})["data"][0]
        assert history["id"] == turn and history["status"] == "interrupted"
        rec["history_terminal"] = history["status"]
        rec["final_meter"] = meter(reader, thread, turn)
        client.run_until(lambda _: False, timeout=1, poll_interval=0.15)
        assert len(rec["observed_calls"]) == 1 and not rec["tool_responses_written"]
        rec["post_commit_requests"] = [r["id"] for r in model.rows if r["t"] > rec["committed_at"]]
        observed_usage = {evt["id"]: evt["usage"]["totalTokens"] for evt in rec["events"]
                          if evt["type"] == "rawResponse/completed" and evt["usage"] is not None}
        rec["requests_without_native_usage_receipt"] = [r["id"] for r in model.rows if r["id"] not in observed_usage]
        assert any(evt.get("type") == "dynamicToolCall" and evt["status"] == "failed" for evt in rec["events"])
        if legacy:
            assert len(rec["post_commit_requests"]) == 1, rec
            assert rec["requests_without_native_usage_receipt"] == rec["post_commit_requests"], rec
            assert model.rows[-1]["held_until_turn_interrupted"] and sum(observed_usage.values()) == 35
        else:
            assert rec["observed_calls"][0]["namespace"] == "gx" and not rec["post_commit_requests"], rec
            if missing_final:
                assert rec["requests_without_native_usage_receipt"] == [model.rows[-1]["id"]], rec
                assert rec["final_meter"]["budget_status"] == "HELD" and rec["final_meter"]["settled_tokens"] is None, rec
            else:
                assert rec["final_meter"]["confidence"] == "PROVIDER_CONFIRMED", rec
                assert observed_usage == {r["id"]: r["usage"]["total_tokens"] for r in model.rows}, rec
                assert rec["final_meter"]["settled_tokens"] == sum(observed_usage.values())
            assert model.rows[0]["catalog"]["direct_product_groups"] == [["gx_submit_product"]], model.rows
            assert "existing_group" in roots._product_tool_config["features.code_mode.direct_only_tool_namespaces"]
            assert len(model.rows) == (1 if mode == "direct" else 2)
            if mode != "direct":
                output, = [evt["output"] for evt in rec["events"] if evt.get("type") == "custom_tool_call_output"]
                texts = [item["text"] for item in output] if isinstance(output, list) else output.splitlines()
                bindings = next(json.loads(s) for s in texts if s.startswith("{"))
                assert bindings == {"matches": [], "plain": "undefined", "grouped": "undefined", "reader": "function"}, bindings
                assert "TypeError" in texts, texts
        return rec


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json")
    p.add_argument("--output", type=Path, default=Path(__file__).with_name("terminal-code-mode.json"))
    args = p.parse_args()
    _proxy_preflight()
    binary, source = identity(MODE_SOURCES)
    model = next(m for m in json.loads(args.catalog.read_bytes())["models"] if m["slug"] == MODEL)
    assert model["tool_mode"] == "code_mode_only" and model["supports_search_tool"]
    with tempfile.TemporaryDirectory(prefix="gx-terminal-surface-") as tmp, patch(
            "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
        rows = [trial(Path(tmp) / case, binary, model, case, mode, expected) for case, mode, expected in SCENARIOS]
    result = {"completed_utc": datetime.now(timezone.utc).isoformat(), "host": source, "model": MODEL,
              "catalog_mode": model["tool_mode"], "catalog_search_supported": model["supports_search_tool"],
              "catalog_entry_sha256": hashlib.sha256(json.dumps(model, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "model_service": "loopback scripted Responses; actual cached entry used without credentials",
              "trials": rows, "passed": True}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps([(r["case"], r["expected"], len(r["post_commit_requests"])) for r in rows]))
