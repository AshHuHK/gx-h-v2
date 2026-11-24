"""Compare frozen builds through the same public grid fixture and a live late source."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gx-h"))
import public_grid as public
import acc1_source_cli as source


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def brief_sizes(state):
    result = {}
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as conn:
        for task, role, brief in conn.execute("SELECT task_id,role,brief FROM p0_roots WHERE task_id IS NOT NULL"):
            digest = hashlib.sha256(compact(brief).encode()).hexdigest()
            expected, = conn.execute("SELECT brief_sha256 FROM submissions WHERE task_id=? AND result='ACCEPTED'",
                                     (task,)).fetchone()
            assert expected == digest
            packet = json.loads(brief.split("\n", 1)[1])
            inline = packet.get("preloaded_artifacts", {})
            without = {k: v for k, v in packet.items() if k not in ("preloaded_artifacts", "lazy_artifacts")}
            result[task] = {"role": role, "submission_brief_sha256": digest, "brief_utf8_bytes": len(brief.encode()),
                            "preloads": len(inline), "lazy_refs": len(packet.get("lazy_artifacts", {})),
                            "preloaded_payload_bytes": len(compact(inline).encode()) if inline else 0,
                            "preload_and_lazy_fields_bytes": len(compact(packet).encode()) - len(compact(without).encode()),
                            "pinned_source_bytes": sum(conn.execute("SELECT bytes FROM artifacts WHERE artifact_ref=?",
                                                                   (ref,)).fetchone()[0] for ref in inline)}
    return result


def compare_case(revision, mode, work):
    work.mkdir()
    result = {}
    args = SimpleNamespace(commit=revision, mode=mode, lane="west", wave=0)
    public.run(args, result, work)
    observed = result["observed"]
    outcomes = {path: {k: route[k] for k in ("points", "threshold", "supported")}
                for path, route in observed["routes"].items()}
    facts = {"routes": outcomes, "continue_paths": observed["decision"]["continue_paths"],
             "stop_reason": observed["decision"]["stop_reason"], "job_statuses": [j["status"] for j in observed["jobs"]]}
    requests = result["responses"]
    assert all(type(r["http_body_bytes"]) is int and r["http_body_bytes"] > 0 for r in requests)
    packets = brief_sizes(work / "state")
    for task, packet in packets.items():
        role_requests = [r for r in requests if r.get("task_id") == task]
        first = role_requests[0]
        packet.update(requests=len(role_requests), first_request_http_bytes=first["http_body_bytes"],
                      first_request_brief_copies=first["copies_by_role"],
                      total_http_bytes=sum(r["http_body_bytes"] for r in role_requests),
                      worker_tools=dict(Counter(r["tool"] for r in role_requests if r.get("tool"))))
    return {"build": result["build"], "binary_sha256": result["binary_sha256"],
            "science_sha256": hashlib.sha256(compact(facts).encode()).hexdigest(),
            "spec_sha256": hashlib.sha256(compact(result["spec"]).encode()).hexdigest(),
            "job_count": len(observed["jobs"]), "submitted_products": len(observed["submissions"]),
            "support_by_path": {p: data["supported"] for p, data in outcomes.items()},
            "checked_preloaded_refs": observed["checked_preloaded_refs"], "stale_preloads": observed["stale_preloads"],
            "http_requests": len(requests), "compaction_http_requests": result["compaction_http_requests"],
            "post_terminal_requests": result["post_terminal_requests"],
            "total_http_request_bytes": sum(r["http_body_bytes"] for r in requests),
            "scripted_provider_tokens": result["scripted_provider_tokens"],
            "native_turns": observed["native_turns"],
            "rollout_diagnostics": result["spec"]["metering"]["rollout_diagnostics"],
            "start_and_run_seconds": round(sum(c["elapsed_s"] for c in result["commands"][:2]), 4),
            "terminal_cold_replay_requests": result["commands"][-1]["requests"], "workers": packets}


class SourcePeer(public.ReviewerPeer):
    def action(self, body, ident):
        event, facts = super().action(body, ident)
        if event.get("name") != "gx_submit_product":
            return event, facts
        brief = public.peer.brief_from(body)
        task = brief["input"]
        if not task.get("supplement_sources"):
            return event, facts
        artifacts, _ = public.peer.tool_history(body["input"])
        artifacts.replies.update({ref: v["content"] for ref, v in brief.get("preloaded_artifacts", {}).items()})
        product = json.loads(event["arguments"])["product"]
        try:
            docs = [artifacts.read_json(s["artifact_ref"]) for s in task["supplement_sources"]]
            routes = [r for ref in task["evidence_refs"] for r in artifacts.read_json(ref)["routes"]]
            originals = [artifacts.read_json(r["observations_ref"]) for r in routes]
            assert all(doc["text"] == source.OPERATOR_TEXT for doc in docs)
            assert len(originals) == 2 and all(raw["method_id"] == "fixture-grid" for raw in originals)
            assert all(not raw.get("control_id") for raw in originals)
            if task["output_kind"] == "science_assessment":
                product["source_reviews_by_id"] = {
                    doc["source_id"]: {"disposition": "rejected", "compared_paths": [r["path_id"] for r in routes],
                    "qualifies_paths": [], "reason": "The two outputs are computed from coordinates. Neither "
                    "identifies an experimental control to which the reported contamination would apply."} for doc in docs}
            else:
                independent = artifacts.read_json(task["assessment_ref"])["source_reviews"]
                assert {row["source_id"] for row in independent} == {doc["source_id"] for doc in docs}
                assert all(row["disposition"] == "rejected" for row in independent)
                product["source_decisions"] = independent
                product["reason"] += " The control concern does not apply to these coordinate measurements."
        except public.peer.ReadArtifact as missing:
            ref, = missing.args
            return {"type": "function_call", "name": "gx_read_artifact", "call_id": "call-"+ident,
                    "arguments": json.dumps({"ref": ref})}, {**facts, "tool": "gx_read_artifact", "ref": ref}
        return {**event, "arguments": json.dumps({"product": product})}, facts


def source_change(install, work):
    work.mkdir()
    spec = source.demo_spec()
    spec.update(workflow_version="3", plugins=["org.gx.science-grid@3.0.0"])
    spec["proposal"]["requires_interpretation"] = True
    spec["max_rounds"] = 1
    spec_file = work / "spec.json"
    spec_file.write_text(compact(spec))
    binary = public.ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    model = SourcePeer()
    state = work / "state"
    with public.peer.local_responses(work / "requests.jsonl") as server, patch.object(public.peer, "fixture_action", model):
        env = public.fixture_env(work, server, str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        def cli(*args, background=False):
            command = [sys.executable, str(install / "bin/gx-p0"), args[0], "--state", str(state), *args[1:]]
            options = dict(cwd=install, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if background:
                return subprocess.Popen(command, **options)
            result = subprocess.run(command, timeout=90, **options)
            assert not result.returncode, result.stderr[-1200:]
            return json.loads(result.stdout)
        cli("start", "--spec", str(spec_file))
        before = source.snapshot(state)
        server.fault_enabled = True
        running = cli("run", "--timeout", "110", background=True)
        try:
            assert server.fault_ready.wait(45), "no review hand-in at loopback peer"
            assert not source.snapshot(state)["submitted"]
            pending = cli("message", "--text", source.OPERATOR_TEXT, "--message-id", source.MESSAGE, "--queue-only")
            assert pending["status"] == "queued"
        finally:
            server.fault_release.set()
            try:
                result, error = running.communicate(timeout=125)
            except subprocess.TimeoutExpired:
                running.terminate()
                running.communicate(timeout=10)
                raise
        assert not running.returncode and json.loads(result)["status"] == "COMPLETED", error[-1200:]
        previous = server.request_count
        same = cli("message", "--text", source.OPERATOR_TEXT, "--message-id", source.MESSAGE)
        final = cli("run", "--timeout", "5")
        assert final["status"] == "COMPLETED" and server.request_count == previous
        trace = list(server.rows)
    rows = source.snapshot(state)
    manifest = {a["artifact_ref"]: a for a in rows["artifacts"]}
    current, = (ref for ref, a in manifest.items() if a["kind"] == "operator_source")
    note, = rows["source"]
    assert note["text"] == source.OPERATOR_TEXT and same["result"]["source_id"] == note["source_id"]
    assert {j["job_id"] for j in before["jobs"]} == {j["job_id"] for j in rows["jobs"]} and len(rows["jobs"]) == 2
    assert all(j["status"] == "SUCCEEDED" for j in rows["jobs"])
    assert len(rows["submitted"]) == 3 and len({s["principal"] for s in rows["submitted"]}) == 3
    assert rows["versions"] == before["versions"]
    reviews = [a for a in manifest.values() if a["kind"] == "science_assessment"]
    first_review, = (a for a in reviews if not a["body"]["source_reviews"])
    second_review, = (a for a in reviews if a["body"]["source_reviews"])
    decision, = (a for a in manifest.values() if a["kind"] == "science_decision")
    assert second_review["body"]["source_reviews"][0]["artifact_ref"] == current
    assert decision["body"]["assessment_ref"] == second_review["artifact_ref"]
    assert decision["body"]["source_decisions"] == second_review["body"]["source_reviews"]
    assert first_review["body"]["evidence_refs"] == second_review["body"]["evidence_refs"]
    assert final["supplements"][0]["disposition"] == "rejected"
    assert rows["decisions"][0]["decision_ref"] == decision["artifact_ref"]
    assert current not in model.preloads
    original_readers = {r["task_id"] for r in trace if r.get("tool") == "gx_read_artifact" and r.get("ref") == current}
    assert original_readers == {second_review["task_id"], decision["task_id"]}
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as conn:
        queued, consumed = conn.execute("SELECT o.created_at,i.consumed_at FROM p0_operator_messages o "
                                       "JOIN p0_human_inputs i USING(message_id) WHERE message_id=?", (source.MESSAGE,)).fetchone()
        accepted = dict(conn.execute("SELECT task_id,created_at FROM submissions WHERE result='ACCEPTED'"))
        assert queued <= accepted[first_review["task_id"]] and consumed <= accepted[second_review["task_id"]]
        old, = conn.execute("SELECT brief FROM p0_roots WHERE task_id=?", (first_review["task_id"],)).fetchone()
        assert current not in old
        for task_id in original_readers:
            brief, = conn.execute("SELECT brief FROM p0_roots WHERE task_id=?", (task_id,)).fetchone()
            assert source.OPERATOR_TEXT not in brief and current in brief
        for ref, (digest, content_hash) in model.preloads.items():
            registered, namespace, rel_path = conn.execute("SELECT sha256,namespace,rel_path FROM artifacts "
                                                          "WHERE artifact_ref=?", (ref,)).fetchone()
            raw = (state / "artifacts" / namespace / rel_path).read_bytes()
            assert digest == registered == hashlib.sha256(raw).hexdigest()
            assert hashlib.sha256(json.dumps(json.loads(raw), sort_keys=True).encode()).hexdigest() == content_hash
        assert not conn.execute("SELECT count(*) FROM outbox WHERE topic='p0.supplement.review' "
                                "AND consumed_at IS NULL").fetchone()[0]
    return {"old_assessment": first_review["artifact_ref"], "new_assessment": second_review["artifact_ref"],
        "decision": decision["artifact_ref"], "source_ref": current, "disposition": "rejected",
        "jobs": len(rows["jobs"]), "independent_worker_roots": 3, "replay_requests": 0,
        "source_queued": queued, "source_accepted": consumed, "original_review_accepted": accepted[first_review["task_id"]],
        "updated_review_accepted": accepted[second_review["task_id"]],
        "source_sha256": manifest[current]["sha256"], "source_preloaded": False,
        "request_count": len(trace), "source_read_by_tasks": sorted(original_readers),
        "same_frozen_measurements": True, "distinct_preloads_verified": len(model.preloads),
        "stale_preloads": 0, "unconsumed_supplement_events": 0}


def main(args):
    args.work.mkdir(parents=True, exist_ok=False)
    result = {"method": "public bin/gx-p0, official Codex, real CPU grid Jobs, scripted loopback Responses",
              "token_accounting": "fixed 35 provider tokens per fixture response", "cases": {}}
    original = public.ReviewerPeer.__call__
    def observed(peer, body, request_id):
        event, info = original(peer, body, request_id)
        copies = Counter()
        marker = "GX task brief\n"
        copies["instructions"] = (body.get("instructions") or "").count(marker)
        for item in body["input"]:
            if item.get("role") in ("user", "developer"):
                copies[item["role"]] += sum(text.count(marker) for text in public.peer.message_texts(item))
        info["copies_by_role"] = dict(copies)
        return event, info

    with patch.object(public.ReviewerPeer, "__call__", observed):
        for mode in ("review", "deterministic"):
            arms = {}
            for name in (("b2", "b3") if mode == "review" else ("b3", "b2")):
                arms[name] = compare_case(getattr(args, name), mode, args.work / (mode+"-"+name))
            for field in ("spec_sha256", "science_sha256", "binary_sha256", "submitted_products", "job_count"):
                assert arms["b2"][field] == arms["b3"][field], (mode, field)
            assert arms["b2"]["checked_preloaded_refs"] == 0 and arms["b3"]["checked_preloaded_refs"] > 0
            for packet in arms["b3"]["workers"].values():
                assert packet["first_request_brief_copies"].get("user") == 1
                assert sum(packet["first_request_brief_copies"].values()) == 1
                assert max(packet["preloaded_payload_bytes"], packet["pinned_source_bytes"]) <= 16384
            result["cases"][mode] = arms
        result["late_source"] = source_change(args.work / "review-b3/install", args.work / "late-source")
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b2", required=True)
    parser.add_argument("--b3", required=True)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    main(parser.parse_args())
