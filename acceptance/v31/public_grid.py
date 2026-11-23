"""Run one numerical grid through the public CLI and official loopback Codex runtime."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "acceptance"), str(ROOT / "acceptance/v3/performance")]
import e2e_responses as peer
from e2e_grid import fixture_env
from run_workload import specification, OFFICIAL_BINARY_SHA256
from v21_staging import stage_source


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class ReviewerPeer:
    """Only see the official provider request and prior tool results visible to its model."""

    def __init__(self, *, repair=False):
        self.original_action = peer.fixture_action
        self.repair = repair
        self.invalid = None
        self.invalid_error = None
        self.products = []
        self.http = threading.local()
        self.preloads = {}

    def __call__(self, body, request_id):
        event, info = self.action(body, request_id)
        if info.get("task_id"):
            catalogs = [body.get("tools", [])] + [item["tools"] for item in body["input"]
                       if item.get("type") == "additional_tools" and isinstance(item.get("tools"), list)]
            scopes = [group.get("name") if group.get("type") == "namespace" else None
                      for catalog in catalogs for group in catalog for leaf in group.get("tools", [group])
                      if leaf.get("name") == "gx_submit_product"]
            assert scopes == ["gx"], f"submit tool not exposed in its single direct namespace: {scopes}"
            info["submit_namespace"] = scopes[0]
            if event.get("name") == "gx_submit_product":
                event["namespace"] = scopes[0]
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        marker = "GX task brief\n"
        info.update(json_payload_bytes=len(encoded), http_body_bytes=getattr(self.http, "body_size", None),
                    brief_in_instructions=marker in (body.get("instructions") or ""),
                    brief_in_model_input=any(marker in text for item in body["input"]
                                             if item.get("role") in ("developer", "user")
                                             for text in peer.message_texts(item)))
        return event, info

    def action(self, body, request_id):
        try:
            brief = peer.brief_from(body)
        except ValueError:
            return self.original_action(body, request_id)
        task = brief["input"]
        details = {"task_id": brief["task_id"], "output_kind": task["output_kind"], "round": task["round"]}
        prior = body["input"]
        if self.invalid:
            for item in prior:
                if item.get("type") == "function_call_output" and item.get("call_id") == self.invalid:
                    reply = peer.output_json(item["output"])
                    assert isinstance(reply, dict) and reply.get("error"), reply
                    self.invalid_error = reply
            prior = [item for item in prior if item.get("call_id") != self.invalid]
        artifacts, submitted = peer.tool_history(prior)
        if submitted:
            return self.original_action({**body, "input": prior}, request_id)
        for ref, item in brief.get("preloaded_artifacts", {}).items():
            content_hash = hashlib.sha256(json.dumps(item["content"],sort_keys=True).encode()).hexdigest()
            entry = (item["sha256"], content_hash)
            assert self.preloads.setdefault(ref, entry) == entry, "an immutable preload changed between requests"
            artifacts.replies.setdefault(ref, item["content"])
        try:
            product = peer.judge(artifacts, task)
        except peer.ReadArtifact as missing:
            ref = missing.args[0]
            return {"type": "function_call", "call_id": "call-" + request_id, "name": "gx_read_artifact",
                    "arguments": json.dumps({"ref": ref})}, {**details, "tool": "gx_read_artifact", "ref": ref}
        event = {"type": "function_call", "name": "gx_submit_product", "call_id": "call-" + request_id,
                 "arguments": json.dumps({"product": product})}
        details["tool"] = event["name"]
        if details["output_kind"] == "science_assessment":
            findings = {f["path_id"]: {"reason": f["reason"],
                        "risk": "This finite coordinate set does not establish behavior outside the measured points."}
                        for f in product["findings"]}
            if self.repair and self.invalid is None:
                self.invalid = event["call_id"]
                findings.pop(next(iter(findings)))
            product = {"findings_by_path": findings}
            event = {**event, "arguments": json.dumps({"product": product})}
        self.products.append({"call_id": event["call_id"], "task_id": details["task_id"],
                              "kind": details["output_kind"], "product": product})
        return event, details


def inspect(state, spec, preloads):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        def records(sql):
            return [dict(row) for row in db.execute(sql)]
        artifacts = {}
        for row in db.execute("SELECT artifact_ref,namespace,rel_path,bytes,sha256,kind FROM artifacts WHERE published=1"):
            file = state / "artifacts" / row["namespace"] / row["rel_path"]
            assert file.stat().st_size == row["bytes"] and sha(file) == row["sha256"], row["artifact_ref"]
            artifacts[row["artifact_ref"]] = {**dict(row), "file": file}
        def read(ref):
            return json.loads(artifacts[ref]["file"].read_bytes())
        for ref, (stated_hash, content_hash) in preloads.items():
            assert artifacts[ref]["sha256"] == stated_hash and hashlib.sha256(
                json.dumps(read(ref),sort_keys=True).encode()).hexdigest() == content_hash, "stale artifact preload"
        run, = records("SELECT run_id,workflow_id,workflow_version,workflow_sha256,status FROM runs")
        assert run["workflow_version"] == "3" and run["status"] == "COMPLETED"
        report_ref, = [ref for ref, row in artifacts.items() if row["kind"] == "research_report"]
        report = read(report_ref)
        round_, = report["rounds"]
        proposal = read(round_["proposal_ref"])
        assessment = read(round_["assessment_ref"])
        decision = read(round_["artifact_ref"])
        assert decision["proposal_ref"] == round_["proposal_ref"] and decision["assessment_ref"] == round_["assessment_ref"]
        assert decision["stop_reason"] and not decision["continue_paths"] and not decision.get("pivots")
        expected = {r["id"]: r for r in spec["proposal"]["paths"]}
        routes = {}
        for route in read(round_["evidence_ref"])["routes"]:
            path = route["path_id"]
            assert path not in routes
            grid = read(route["input_ref"])
            observed = read(route["observations_ref"])
            points = observed["observations"]
            ev = read(route["evaluation_ref"])
            assert observed["input_sha256"] == artifacts[route["input_ref"]]["sha256"]
            assert grid["candidates"] == expected[path]["grid"]["candidates"]
            assert [(p["candidate_id"], p["x"]) for p in points] == [(p["candidate_id"], p["x"]) for p in grid["candidates"]]
            assert all(p["score"] == -(p["x"] - 2)**2 for p in points)
            assert ev["input_ref"] == route["observations_ref"] and ev["threshold"] == proposal["threshold"] == spec["proposal"]["threshold"]
            passed = sum(p["score"] >= ev["threshold"] for p in points)
            assert ev["count_at_or_above"] == passed and ev["max_score"] == max(p["score"] for p in points)
            finding, = [f for f in assessment["findings"] if f["path_id"] == path]
            assert finding["supported"] is (passed > 0) and finding["evaluation_ref"] == route["evaluation_ref"]
            routes[path] = {"job": route["job_id"], "evaluation": route["evaluation_ref"], "supported": finding["supported"],
                            "points": points, "threshold": ev["threshold"]}
        assert set(expected) == set(routes) and {r["supported"] for r in routes.values()} == {True, False}
        jobs = records("SELECT job_id,status,idempotency_key FROM jobs ORDER BY rowid")
        assert len(jobs) == len({r["idempotency_key"] for r in jobs}) == 2 and all(r["status"] == "SUCCEEDED" for r in jobs)
        submissions = records("SELECT t.task_id,t.role,s.attempt,s.result,s.submission_id,s.artifact_refs_json,l.principal "
                              "FROM tasks t JOIN submissions s USING(task_id) JOIN leases l ON l.lease_id=s.lease_id ORDER BY t.rowid")
        assert len({(s["task_id"], s["attempt"]) for s in submissions}) == len(submissions)
        assert all(s["result"] == "ACCEPTED" and s["attempt"] == 1 for s in submissions)
        roles = {s["role"] for s in submissions}
        if spec["proposal"].get("requires_interpretation"):
            assert roles == {"planner", "evidence_reviewer"} and len({s["principal"] for s in submissions}) == 2
        else:
            assert roles == {"planner"}
        valid = records("SELECT path_id,job_id,kind,direction,eval_id,evidence_id,assessment_ref FROM science_settlements "
                        "WHERE kind='valid_scientific_outcome' ORDER BY path_id")
        assert {r["path_id"] for r in valid} == set(expected)
        for settlement in valid:
            entry = routes[settlement["path_id"]]
            assert settlement["evidence_id"] and settlement["eval_id"] and settlement["job_id"] == entry["job"]
            assert settlement["direction"] == ("supports" if entry["supported"] else "refutes")
        # Check that each model-authored product had one successful callback and terminal record on the same Turn.
        receipts = records("SELECT r.task_id,r.attempt,t.thread_id,t.turn_id,t.call_id,t.reason,c.result_json "
                           "FROM p0_root_terminals t JOIN p0_roots r USING(thread_id) "
                           "JOIN p0_root_calls c USING(thread_id,turn_id,call_id) ORDER BY r.task_id")
        assert {(r["task_id"],json.loads(r["result_json"])["submission_id"]) for r in receipts} == {
            (s["task_id"],s["submission_id"]) for s in submissions}
        assert len(receipts) == len(submissions) and all(r["reason"] == "GX_ACCEPTED" for r in receipts)
        for row in receipts:
            row["submission_id"] = json.loads(row.pop("result_json"))["submission_id"]
        repairs = records("SELECT r.task_id,r.attempt,t.call_id,t.category,t.contract_digest,t.response_json "
                          "FROM p0_product_repairs t JOIN p0_roots r USING(thread_id) ORDER BY r.task_id,t.call_id")
        for kind in ("science_assessment", "science_decision", "research_report"):
            assert sum(a["kind"] == kind for a in artifacts.values()) == 1, "more than one per-round " + kind
        published_files = {a["file"] for a in artifacts.values()}
        extra_files = [p for p in (state / "artifacts").rglob("*") if p.is_file() and p not in published_files]
        assert not extra_files, f"unpublished files under project artifacts: {extra_files[:4]}"
        artifact_count = len(artifacts)
        hashes = {ref: a["sha256"] for ref, a in artifacts.items()}
        return dict(run=run, report_ref=report_ref, decision_ref=round_["artifact_ref"], assessment_ref=round_["assessment_ref"],
                    decision=decision, jobs=jobs, routes=routes, submissions=submissions,
                    settlements=valid, repair_receipts=repairs, terminal_receipts=receipts,
                    checked_preloaded_refs=len(preloads), stale_preloads=0, orphan_artifact_files=0, duplicate_product_artifacts=0, published_artifacts=artifact_count, artifact_index_sha256=hashlib.sha256(
                        json.dumps(hashes,sort_keys=True).encode()).hexdigest(),
                    native_turns=records("SELECT thread_id,turn_id,budget_status,confidence,settled_tokens FROM p0_meter_native_turns ORDER BY thread_id,turn_id"))


def run(args, result, work):
    install = work / "install"
    commit = args.commit
    if commit != "WORKTREE":
        commit = subprocess.check_output(["git", "rev-parse", "--verify", commit + "^{commit}"], cwd=ROOT, text=True).strip()
    result["build"] = stage_source(work, install, commit)
    spec = specification(install, args.wave, args.lane)
    spec.update(workflow_version="3", plugins=["org.gx.science-grid@3.0.0"])
    if args.mode == "deterministic":
        for route in spec["proposal"]["paths"]:
            route["claim_scope"] = "finite_grid_threshold"
    else:
        spec["proposal"]["requires_interpretation"] = True
    spec_file = work / "spec.json"
    spec_file.write_text(json.dumps(spec))
    binary = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    assert sha(binary) == OFFICIAL_BINARY_SHA256
    result.update(mode=args.mode, binary_sha256=OFFICIAL_BINARY_SHA256, official_version=subprocess.check_output(
        [binary, "--version"],text=True).strip(), fixture="scripted Responses at 127.0.0.1, 35 supplied tokens per response",
        spec=spec, commands=[])
    state = work / "state"
    model = ReviewerPeer(repair=args.mode == "repair")
    paths = []
    post = peer.Peer.do_POST
    def observed_post(handler):
        model.http.body_size = int(handler.headers.get("Content-Length") or 0)
        paths.append(handler.path)
        return post(handler)
    with (peer.local_responses(work / "requests.jsonl") as server, patch.object(peer, "fixture_action", model),
          patch.object(peer.Peer, "do_POST", observed_post)):
        env = fixture_env(work, server, str(binary))
        def cli(action, *extra):
            selector = ["--run-key", args.lane] if action == "start" else []
            before = server.request_count
            started = time.monotonic()
            command = [sys.executable, str(install / "bin/gx-p0"), action, "--state", str(state), *selector, *extra]
            proc = subprocess.run(command, cwd=install, env=env, capture_output=True, text=True, timeout=155)
            result["commands"].append(dict(argv=[action, *selector, *extra], exit=proc.returncode,
                                        requests=server.request_count - before, elapsed_s=round(time.monotonic()-started, 4)))
            assert proc.returncode == 0, (proc.stderr[-2000:],proc.stdout[-2000:],server.rows[-1:])
            return json.loads(proc.stdout)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        opened = cli("start", "--spec", str(spec_file))
        bound = opened["plugin_binding"]
        assert (bound["workflow"]["id"], bound["workflow"]["version"]) == ("science_grid", "3")
        assert [(entry["id"], entry["version"]) for entry in bound["packages"]] == [("org.gx.science-grid", "3.0.0")]
        assert any(entry["capability_version"] == "fixture-grid@1" for entry in bound["capabilities"])
        result["saved_binding"] = bound
        final = cli("run", "--timeout", "120")
        assert final["status"] == "COMPLETED", (final["status"], server.rows[-1:])
        result["observed"] = inspect(state, spec, model.preloads)
        projected = [r["evidence_id"] for r in final["current_evidence"]]
        assert sorted(projected) == sorted(row["evidence_id"] for row in result["observed"]["settlements"])
        result["current_evidence_ids"] = projected
        replay = cli("run", "--timeout", "5")
        assert replay["report_ref"] == final["report_ref"] and result["commands"][-1]["requests"] == 0
        assert inspect(state, spec, model.preloads) == result["observed"], "reopening repeated work or changed the original facts"
        assert all("error" not in row for row in server.rows)
        turns = result["observed"]["native_turns"]
        assert all(row["budget_status"] == "SETTLED" and row["confidence"] == "PROVIDER_CONFIRMED" for row in turns), turns
        assert sum(row["settled_tokens"] for row in turns) == server.request_count * 35, turns
        successful_calls = {row["call_id"]:row for row in result["observed"]["terminal_receipts"]}
        by_task = {}
        for row in server.rows:
            if row.get("task_id"):
                by_task.setdefault(row["task_id"], []).append(row)
        for product in model.products:
            if product["call_id"] in successful_calls:
                assert by_task[product["task_id"]][-1]["id"] == product["call_id"].removeprefix("call-"), "post-terminal request"
        repairs = result["observed"]["repair_receipts"]
        assert len(repairs) == int(args.mode == "repair")
        if args.mode == "repair":
            assert repairs[0]["call_id"] == model.invalid and json.loads(repairs[0]["response_json"]) == model.invalid_error
            assert model.invalid_error, "malformed submission did not return a recoverable wire error"
        result.update(responses=server.rows, visible_submissions=model.products, invalid_call_id=model.invalid,
                      invalid_error=model.invalid_error, post_terminal_requests=0,
                      compaction_http_requests=sum("compact" in path for path in paths))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", default="WORKTREE")
    p.add_argument("--mode", choices=["review", "repair", "deterministic"], required=True)
    p.add_argument("--wave", type=int, default=0)
    p.add_argument("--lane", choices=["west", "east"], default="west")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True, help="New private scratch directory")
    args = p.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    result = {"passed": False}
    try:
        run(args, result, work)
        result["passed"] = True
    except Exception as exc:
        result["error"] = repr(exc)
        raise
    finally:
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
