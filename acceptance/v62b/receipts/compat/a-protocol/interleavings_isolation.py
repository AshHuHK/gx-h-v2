"""A7: an original quick Goal, scoped artifact isolation, and replay of accepted operations."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASELINE = "c4c5b66a55739b38b34684360810d05f18f9a21a"
RELEASE_FILES = (
    "bin/gx-p0", "gx-h", "codex_adapter", "examples/paired-grid.json", "release-assets/lock.json",
)


def db_facts(state):
    with closing(sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.row_factory = sqlite3.Row
        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]
        return {
            "contract": conn.execute("SELECT value FROM schema_meta WHERE key='contract_sha256'").fetchone()[0],
            "run": rows("SELECT run_id,status,contract_sha256,inputs_json FROM runs")[0],
            "tasks": rows("SELECT task_id,scope FROM v6_decision_tasks ORDER BY rowid"),
            "jobs": rows("SELECT job_id,status,idempotency_key FROM jobs ORDER BY rowid"),
            "heads": rows("SELECT version,sha256 FROM v6_graph_versions ORDER BY version"),
            "native_calls": rows("SELECT thread_id,turn_id,json_extract(request_json,'$.tool') AS tool FROM p0_v6_native_calls ORDER BY rowid"),
            "module_count": (
                conn.execute("SELECT count(*) FROM v62_modules").fetchone()[0]
                if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='v62_modules'").fetchone()
                else None
            ),
        }


def materialize_old(repo, deploy):
    deploy.mkdir()
    with tempfile.TemporaryFile() as stream:
        subprocess.run(["git", "-C", repo, "archive", BASELINE, *RELEASE_FILES], stdout=stream, check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as contents:
            contents.extractall(deploy, filter="data")


def materialize_candidate(deploy):
    deploy.mkdir()
    for part in RELEASE_FILES:
        src, dst = ROOT / part, deploy / part
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc", ".venv"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def quick_compatibility(work):
    """Upgrade an opened c4c5b66 Goal, keep its native path and finish its original assignment."""
    engineering = ROOT / "acceptance/v6/engineering"
    if str(engineering) not in sys.path:
        sys.path.insert(0, str(engineering))
    # The released verifier imports public_cli by module name. Hook its CLI path to the
    # stable deployment directory, retaining its decision script and transport checks.
    import official
    import public_cli as official_cli

    work.mkdir(parents=True)
    deploy, state = work / "release", work / "state"
    materialize_old(ROOT, deploy)
    material = work / "public-2RE9.cif"
    shutil.copy2(official.STRUCTURE, material)
    out = work / "loopback"
    out.mkdir()
    opened = {}
    public_call = official.cli

    def call_across_upgrade(target, env, action, *args, **kwargs):
        result = public_call(target, env, action, *args, **kwargs)
        if action == "start" and not opened:
            opened.update(before=db_facts(state), marker=(state / ".gx-native-state.json").read_bytes(),
                          run_id=result["body"]["run_id"])
            deploy.rename(work / "v61-release")
            materialize_candidate(deploy)
        return result

    binary = os.environ.get("GX_V62_CODEX_BINARY") or os.environ.get("GX_P0_CODEX")
    if not binary:
        candidate = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
        binary = candidate if candidate.is_file() else ROOT.parent / "product-v6.1" / candidate.relative_to(ROOT)
    with (patch.object(official_cli, "ROOT", deploy), patch.object(official, "STRUCTURE", material),
          patch.object(official, "cli", call_across_upgrade)):
        q = official.run(work, out, Path(binary).resolve())
    before, marker, rid = opened["before"], opened["marker"], opened["run_id"]
    old_task = before["tasks"][0]["task_id"]
    assert before["run"]["run_id"] == rid and not before["jobs"] and not before["heads"]
    assert len(before["tasks"]) == 1 and before["module_count"] is None
    assert "research_effort" not in json.loads(before["run"]["inputs_json"]).get("inputs", {})
    after = db_facts(state)
    assert marker == (state / ".gx-native-state.json").read_bytes()
    assert after["run"]["run_id"] == q["run_id"] == rid
    assert before["run"]["inputs_json"] == after["run"]["inputs_json"]
    assert after["tasks"][0]["task_id"] == old_task and {r["scope"] for r in after["tasks"]} == {"run"}
    assert after["run"]["contract_sha256"] == before["run"]["contract_sha256"]
    assert after["jobs"][0]["status"] == "SUCCEEDED" and len(after["jobs"]) == 1
    assert len(after["heads"]) == 2 and q["cold_resume_additional_jobs"] == 0
    assert not (after["module_count"] or any("module" in r["tool"] for r in after["native_calls"]))
    turns = []
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        seen = json.loads((out / "model-inputs.json").read_text())
        for call, observed in zip(q["decisions"], seen, strict=True):
            root = db.execute("SELECT task_id,lease_id,brief,tool_names_json FROM p0_roots WHERE thread_id=?",
                              (call["thread_id"],)).fetchone()
            assert root["task_id"] == call["task_id"]
            turns.append({"scope": "run", "thread_id": call["thread_id"], "turn_id": call["turn_id"],
                          "call_id": call["call_id"], "lease_id": root["lease_id"], "task_id": root["task_id"],
                          "delivered_brief_bytes": len(root["brief"].encode()),
                          "delivered_brief_sha256": hashlib.sha256(root["brief"].encode()).hexdigest(),
                          "brief_only_token_count": None, "native_request_sha256": observed["request_sha256"],
                          "tool_names": json.loads(root["tool_names_json"]),
                          "tool_summary_count": len(json.loads(root["tool_names_json"]))})
    return {
        "old_revision": BASELINE, "run_id": rid, "original_task_id": old_task,
        "source_marker_sha256": hashlib.sha256(marker).hexdigest(), "initial_contract": before["contract"],
        "candidate_contract": after["contract"], "retained_run_contract": after["run"]["contract_sha256"],
        "turns": turns, "native_requests_to_local_peer": len(turns), "jobs": q["jobs"], "heads": q["versions"], "raw_records_dir": "quick/loopback",
        "original_scope": before["tasks"][0]["scope"], "resumed_task_scopes": [r["scope"] for r in after["tasks"]],
        "module_instances": after["module_count"], "decision_source": "V6-LOOPBACK fixed peer on official Codex",
        "official_host_version": q["official_host_version"], "official_binary_sha256": q["lock_binary_sha256"],
        "paid_model_calls": 0, "provider_tokens": None, "scripted_turn_usage": q["local_fixture_usage"],
        "cold_extra_jobs": q["cold_resume_additional_jobs"], "cold_extra_native_calls": q["cold_resume_additional_requests"],
    }


def denied(action, codes):
    from gx_core.errors import GxError

    try:
        action()
    except GxError as exc:
        assert exc.code in codes, (exc.code, codes, str(exc))
        return {"code": exc.code, "reason": str(exc)}
    raise AssertionError("The operation outside its grant was accepted")


def scoped_replays(directory):
    from acceptance.v62a.interleavings_fixture import Scenario, draft
    from gx_core.p0 import v62_reports
    from gx_core.persistence import store

    directory = Path(directory).resolve()

    with Scenario(directory / "scope") as s:
        allowed, marker = s.materials[0], s.materials[2]
        lead = s.lead
        marker_bytes = (s.directory / "public-scope-marker.json").read_bytes()
        marker_text = json.loads(marker_bytes)["marker"]
        lead_read = s.call(lead, "gx_read_artifact", {"ref": marker}, "a7-lead-marker")
        assert marker_text in json.dumps(lead_read), lead_read
        args = s.open_args("a7-open-one", inputs=[allowed])
        receipt = s.call(lead, "gx_open_module", args, "a7-delegate")
        instance_id = receipt["module_instance_id"]
        assert s.call(lead, "gx_open_module", args, "a7-delegate") == receipt
        assert s.call(lead, "gx_open_module", args, "a7-new-transport-id") == receipt
        refusals = {}
        refusals["changed_open"] = denied(
            lambda: s.call(lead, "gx_open_module", {**args, "expected_delivery": "A different report"},
                           "a7-conflicting-content"),
            {"IDEMPOTENCY_CONFLICT"},
        )
        module = s.bind(receipt["task_id"])
        snapshot = s.brief(module)
        context = s.call(module, "gx_task_context", {}, "a7-context")
        module_visible = json.dumps([snapshot, context, s.rows("SELECT brief FROM p0_roots WHERE thread_id=?", module.thread)])
        assert marker not in module_visible and marker_text not in module_visible and allowed in module_visible
        refusals["same_run_outside_input"] = denied(
            lambda: s.call(module, "gx_read_artifact", {"ref": marker}, "a7-marker-guess"), {"SCOPE_DENIED"})
        assert marker_text not in json.dumps(refusals)
        refusals["nested_open"] = denied(
            lambda: s.call(module, "gx_open_module", args, "a7-nested"), {"SCOPE_DENIED"})
        s.call(module, "gx_read_artifact", {"ref": allowed, "offset": 0}, "a7-authorized-read")
        refusals["unread_source_in_report"] = denied(
            lambda: s.report(module, marker, key="a7-outside-finding"), {"SCOPE_DENIED"})

        s.call(lead, "gx_decide_revision", {"decision": draft()}, "a7-wait-for-inventory")
        lead_base = s.head()
        first = s.report(module, allowed, key="a7-publish")
        assert s.report(module, allowed, key="a7-publish") == first
        accepted = s.conn.execute("SELECT request_json FROM p0_v6_native_calls WHERE thread_id=? AND call_id=?",
                                  (module.thread, "a7-publish")).fetchone()
        report_args = json.loads(accepted[0])["arguments"]
        assert s.call(module, "gx_submit_module_report", report_args, "a7-report-redelivery") == first
        changed_report = {**report_args, "gaps": ["A new gap after the submitted report."]}
        refusals["changed_report"] = denied(
            lambda: s.call(module, "gx_submit_module_report", changed_report, "a7-changed-report"),
            {"IDEMPOTENCY_CONFLICT"})
        # A newly received call with a changed operation key is new work on a released assignment.
        refusals["released_module_read"] = denied(
            lambda: s.call(module, "gx_read_artifact", {"ref": allowed, "offset": 10}, "a7-after-report"),
            {"SCOPE_DENIED", "STALE_REVISION"})
        wake_sql = ("SELECT w.round,w.wake_key,w.event_id,e.payload_json FROM v62_module_wakes w "
                    "JOIN events e USING(event_id) WHERE w.run_id=? ORDER BY w.event_id")
        original_wakes = s.rows(wake_sql, s.run_id)
        assert len(original_wakes) == 1 and original_wakes[0]["wake_key"] == "round:1"
        wake = json.loads(original_wakes[0]["payload_json"])
        assert wake["module_instance_ids"] == [instance_id] and wake["report_refs"] == [first["report_ref"]]
        before_replay = s.rows("SELECT task_id FROM v6_decision_tasks WHERE run_id=? AND scope='run'", s.run_id)
        for _ in range(2):
            with store.transaction(s.conn, transaction_owner="V62AAcceptance", operation="redeliver_round_seal", run_id=s.run_id):
                row = s.conn.execute("SELECT * FROM v62_modules WHERE module_instance_id=?", (instance_id,)).fetchone()
                v62_reports.release_round(s.conn, row)
            s.advance()
        after_replay = s.rows("SELECT task_id FROM v6_decision_tasks WHERE run_id=? AND scope='run'", s.run_id)
        assert s.rows(wake_sql, s.run_id) == original_wakes
        assert len(after_replay) == 2 and len(before_replay) in {1, 2}
        assert len(s.rows("SELECT report_ref FROM v62_module_reports WHERE module_instance_id=?", instance_id)) == 1
        assert len(s.rows("SELECT module_instance_id FROM v62_modules WHERE run_id=?", s.run_id)) == 1
        resumed_lead = s.next_actor("run")
        delivered = s.brief(resumed_lead)
        assert first["report_ref"] in json.dumps(delivered)
        assert not s.rows("SELECT job_id FROM jobs WHERE run_id=?", s.run_id)
        trace = s.trace()
        return {"case": "A7", "status": "PASS", "paid_model_calls": 0, "provider_tokens": None,
                "authorized_lead_marker": {"ref": marker, "sha256": hashlib.sha256(marker_bytes).hexdigest()},
                "authorized_module_input": allowed, "delegation_receipt": receipt, "report_receipt": first,
                "wake_event": {**original_wakes[0], "payload_json": wake},
                "controlled_interleavings": ["open", "transport replay", "operation replay", "module scoped reads",
                                             "lead waits", "report", "transport replay", "two wake redeliveries",
                                             "lead receives report"],
                "refusals": refusals, "report_graph_head": lead_base, "lead_tasks_after_redelivery": after_replay,
                "next_lead_context_ref": delivered.get("context_ref"), "trace": trace}


def run(directory):
    directory = Path(directory).resolve()
    scope = scoped_replays(directory)
    network = directory / "quick-network.json"
    clash = os.environ.get("GX_CLASH") or shutil.which("mihomo") or ROOT.parents[2] / "clash/clash.new"
    # Two public GX processes share the pinned local peer in an isolated Linux network
    # namespace. The harness accepts only loopback traffic and supplies no OAuth login.
    command = [sys.executable, "-B", str(ROOT / "acceptance/phase2a/regression/loopback.py"),
               "--clash", str(clash), "--receipt", str(network), "--",
               sys.executable, "-B", str(Path(__file__).resolve()), "--quick-worker", str(directory / "quick")]
    with (directory / "quick-worker.log").open("w") as logfile:
        completed = subprocess.run(command, stdout=logfile, stderr=subprocess.STDOUT, timeout=160)
    if completed.returncode:
        raise AssertionError(f"Quick-upgrade process exited {completed.returncode}. See {directory / 'quick-worker.log'}")
    boundary = json.loads(network.read_text())
    if boundary["exit_code"] != 0:
        raise AssertionError("The local-only host verification did not complete")
    quick = json.loads((directory / "quick/quick.json").read_text())
    quick["network_receipt"] = {"path": network.name, "sha256": hashlib.sha256(network.read_bytes()).hexdigest(),
                                "exit_code": boundary["exit_code"], "ipv4_routes": boundary["ipv4_routes"],
                                "ipv6_route_interfaces": boundary["ipv6_route_interfaces"],
                                "resource_profile": boundary["resource_profile"]}
    assert quick["module_instances"] == 0 and quick["initial_contract"] != quick["candidate_contract"]
    scope["quick"] = quick
    return scope


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick-worker", type=Path, required=True)
    args = parser.parse_args()
    result = quick_compatibility(args.quick_worker.resolve())
    (args.quick_worker / "quick.json").write_text(json.dumps(result, indent=2) + "\n")
