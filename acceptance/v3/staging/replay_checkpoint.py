"""Rehearse a candidate checkout against the retained, paused v2.1 checkpoint."""
import argparse
from contextlib import contextmanager, ExitStack
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
from urllib.parse import urlsplit

from cold_fixture import ROOT, FROZEN, check, digest, fixture_env, job_sidecars, migrations, now
from cold_fixture import original_facts, save, sha, stage_source
from e2e_responses import FixtureServer
from v21_staging import assert_preserved


@contextmanager
def at_original_path(path, replacement, audit, name):
    previous = audit / ("parked-" + name)
    path.rename(previous)
    try:
        replacement.rename(path)
        try:
            yield
        finally:
            path.rename(replacement)
    finally:
        previous.rename(path)


def reopen(report, base, revision, audit):
    checkpoint = base["checkpoint"]
    work = ROOT / base["restore"]["retained_private_root"]
    check(work.resolve().is_relative_to(ROOT / "var"), "Invalid private fixture path")
    snapshot = work / "snapshot"
    check(sha(snapshot / "project/gx.db") == checkpoint["db_sha256"], "Source snapshot DB changed")
    expected = original_facts(snapshot / "project")
    check(digest(expected) == checkpoint["original_facts_sha256"], "Source snapshot facts changed")
    for name, checksum in checkpoint["rollouts"].items():
        check(sha(snapshot / "codex" / name) == checksum, "Source native rollout changed")
    state, native, install = (work / name for name in ("project", "codex", "install"))
    check(digest(original_facts(state)) == checkpoint["original_facts_sha256"],
          "Current private fixture differs from the paused baseline")
    source = audit / "candidate"
    report["candidate"] = stage_source(audit, source, revision, full_release=True)
    lock = json.loads((ROOT / "release-assets/lock.json").read_text())["linux_x64"]
    binary = ROOT / "vendor/codex-runtime/package" / lock["binary"]
    check(sha(binary) == lock["binary_sha256"], "Official Codex differs")
    report["binary_sha256"] = lock["binary_sha256"]
    report["source_snapshot"] = {"db_sha256": checkpoint["db_sha256"], "run": checkpoint["run"],
                                 "original_facts_sha256": checkpoint["original_facts_sha256"]}
    config = tomllib.loads((snapshot / "codex/config.toml").read_text())
    endpoint = urlsplit(config["model_providers"]["gx_loopback"]["base_url"])
    check(endpoint.hostname == "127.0.0.1" and endpoint.port, "Expected a source loopback endpoint")

    class SourcePeer(FixtureServer):
        def server_bind(self):
            self.server_address = ("127.0.0.1", endpoint.port)
            super().server_bind()

    with ExitStack() as resources:
        peer = resources.enter_context(SourcePeer(audit / "requests.jsonl"))
        peer.request_count = checkpoint["source_model_responses"]
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        resources.callback(thread.join)
        resources.callback(peer.shutdown)
        conf = audit / "environment"
        conf.mkdir()
        env = fixture_env(conf, peer, str(binary))
        env.update(HOME=str(work), CODEX_HOME=str(native), PATH=str(ROOT / ".venv/bin") + os.pathsep + env["PATH"])

        def invoke(phase, action, *args):
            argv = [str(install / "bin/gx-p0"), action, "--state", str(state), *args]
            previous = peer.request_count
            proc = subprocess.run(argv, cwd=install, env=env, text=True, capture_output=True, timeout=130)
            row = {"stage": phase, "command": argv, "exit_code": proc.returncode,
                   "new_fixture_responses": peer.request_count - previous}
            report["commands"].append(row)
            check(proc.returncode == 0, f"{action} failed: {proc.stderr[-650:]}")
            result = json.loads(proc.stdout)
            row["status"] = result["status"]
            check(result["run_id"] == expected["run"]["run_id"], "Wrong persisted run")
            return result

        def restore_copies(stack, phase):
            for name, target in (("project", state), ("codex", native)):
                source_copy = audit / (phase + "-" + name)
                shutil.copytree(snapshot / name, source_copy, symlinks=True)
                stack.enter_context(at_original_path(target, source_copy, audit, phase + "-" + name))
            check(sha(state / "gx.db") == checkpoint["db_sha256"], "Restored GX DB differs")

        def source_receipts():
            check(job_sidecars(state, expected["jobs"]) == checkpoint["job_sidecars"], "Old Job receipt changed")

        with ExitStack() as opened:
            restore_copies(opened, "pre-rollback")
            opened.enter_context(at_original_path(install, source, audit, "first-install"))
            check(invoke("candidate-write", "stop")["status"] == "PAUSED", "Writable open resumed run")
            opened_view = original_facts(state)
            assert_preserved(expected, opened_view)
            check(opened_view["original_budget_lines"] == expected["original_budget_lines"],
                  "Writable open changed historical budget balances")
            source_receipts()
            report["candidate_p0_migrations"] = migrations(state)
        with ExitStack() as restored:
            restore_copies(restored, "rollback")
            check(invoke("restored-v2.1", "status")["status"] == "PAUSED", "Rollback status failed")
            check(invoke("restored-v2.1", "run", "--timeout", "10")["status"] == "PAUSED", "Rollback changed run")
            check(migrations(state) == checkpoint["p0_migrations"] and original_facts(state) == expected,
                  "Rollback changed original schema, ownership, accounting, or receipts")
            source_receipts()
            check(peer.request_count == checkpoint["source_model_responses"], "Rollback made an extra model call")
            report["rollback_verified"] = True
            with at_original_path(install, source, audit, "second-install"):
                resumed = invoke("candidate-cold", "resume", "--timeout", "100")
                check(resumed["status"] == "COMPLETED" and resumed["report_ref"] and not resumed["unsettled_turns"],
                      "Candidate did not finish restored legacy run")
                final = original_facts(state)
                assert_preserved(expected, final)
                source_receipts()
                check(len(final["jobs"]) == len({r["idempotency_key"] for r in final["jobs"]}) == 4
                      and all(r["status"] == "SUCCEEDED" for r in final["jobs"]), "Unexpected Job replay or failure")
                check(len(final["submissions"]) == 6 and len(final["decisions"]) == 3, "Incomplete continuation")
                budget = expected["original_budget_lines"][0]["budget_ref"]
                balance, = (row for row in final["original_budget_lines"] if row["budget_ref"] == budget)
                spent = json.loads(balance["spent_json"])
                reserved = json.loads(balance["reserved_json"])
                totals = {}
                for usage in final["usage"]:
                    thread_id = usage["thread_id"]
                    totals[thread_id] = max(totals.get(thread_id, 0), usage["total_tokens"])
                check(spent["tokens"] == sum(totals.values()) == peer.request_count * 35
                      and spent["api_calls"] == len(final["usage"])
                      and spent["job_submissions"] == 4 and not any(reserved.values()),
                      "Source budget did not reconcile to the native and Job receipts")
                before_replay = peer.request_count
                invoke("candidate-terminal", "run", "--timeout", "10")
                check(peer.request_count == before_replay and original_facts(state) == final, "Terminal replay changed state")
                report["completion"] = {"report_ref": resumed["report_ref"], "cpu_jobs": len(final["jobs"]),
                                        "native_turns": len(final["usage"]), "submissions": len(final["submissions"]),
                                        "accepted_decisions": len(final["decisions"]), "final_facts_sha256": digest(final),
                                        "total_fixture_responses_including_source": before_replay,
                                        "settled_budget": spent, "remaining_reservations": reserved,
                                        "original_job_receipts_equal": True, "terminal_replay_extra_calls": 0}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", required=True, help="A committed candidate Git revision")
    p.add_argument("--audit", type=Path, required=True, help="A new private directory under var/")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    audit = args.audit.resolve()
    if not audit.is_relative_to(ROOT / "var"):
        p.error("keep private replay data under var/")
    audit.mkdir(parents=True, exist_ok=False)
    base = json.loads(args.baseline.read_text())
    check(base["result"] == "PASS" and base["source_install"]["git_commit"] == FROZEN, "Wrong source release")
    revision = subprocess.check_output(["git", "rev-parse", "--verify", args.candidate + "^{commit}"],
                                       cwd=ROOT, text=True).strip()
    result = {"test": "writable candidate open, rollback and legacy run cold continuation",
              "mode": "official Codex CLI; synthetic localhost Responses; detached CPU Jobs",
              "command": [str(Path(sys.executable).relative_to(ROOT)), "-B", *sys.argv],
              "source": "acceptance/v3/staging/replay_checkpoint.py", "source_commit": FROZEN,
              "started_utc": now(), "commands": [], "result": "FAIL"}
    try:
        reopen(result, base, revision, audit)
        result.update(result="PASS", exit_code=0)
    except Exception as exc:
        result.update(error=str(exc), exit_code=1)
        raise
    finally:
        result["ended_utc"] = now()
        save(args.output, result)
