"""Exercise the release bin/gx-p0 CLI, the official binary and local numerical Jobs.

Run in a fresh result directory. Only summaries from this credential-free project
are exported. CODEX_HOME, rollout files and local GX state live in a temporary dir.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from e2e_fault import interrupt, observed_state
from e2e_audit import audit, check_native_charges, require
from e2e_responses import local_responses

PACKAGE = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def fixture_env(work, server, codex):
    home = work / "codex"
    home.mkdir()
    url = f"http://127.0.0.1:{server.server_port}"
    (home / "config.toml").write_text(f'''model_provider = "gx_loopback"
chatgpt_base_url = "{url}"
openai_base_url = "{url}/v1"
check_for_update_on_startup = false
[model_providers.gx_loopback]
name = "GX synthetic Responses fixture"
base_url = "{url}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
[features]
respect_system_proxy = true
system_proxy_fallback = false
[analytics]
enabled = false
[feedback]
enabled = false
''')
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if k in os.environ}
    from gx_core.resource_profile import child_resource_environment
    env.update(child_resource_environment())
    env.update(CODEX_HOME=str(home), HOME=str(work), GX_P0_CODEX=codex, GX_P0_TEST="1", GX_P0_TEST_PROVIDER="gx_loopback",
               PYTHONDONTWRITEBYTECODE="1", CODEX_APP_SERVER_CHATGPT_BASE_URL=url)
    from gx_core.p0.profiles import clash_env
    env.update({k: v for k, v in clash_env().items()
                if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")})
    return env


def execute(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {"mode": "official Codex app-server, scripted loopback Responses, real local synthetic grid Jobs",
              "started": datetime.now(timezone.utc).isoformat(), "manual_advancement": False,
              "commands": [], "result": "FAIL"}
    try:
        with ExitStack() as resources:
            if args.work:
                work = args.work.resolve()
                work.mkdir(parents=True, exist_ok=False)
            else:
                work = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="gx-p0-e2e-")))
            state = work / "state"
            server = resources.enter_context(local_responses(out / "requests.jsonl"))
            codex = shutil.which(args.codex)
            if not codex:
                raise ValueError("pinned Codex was not found")
            env = fixture_env(work, server, codex)
            version = subprocess.check_output([codex, "--version"], text=True, env=env).strip()
            require(version == "codex-cli 0.169.0", f"unexpected Codex version {version!r}")
            result["codex"] = version

            def cli(label, action, extra=()):
                command = ["bin/gx-p0", action, "--state", str(state), *extra]
                started = time.monotonic()
                record = {"name": label, "argv": [x if x != str(state) else "$STATE" for x in command],
                          "response_count_before": server.request_count}
                result["commands"].append(record)
                completed = subprocess.run(command, cwd=PACKAGE, env=env, capture_output=True,
                                           text=True, timeout=args.timeout + 30)
                record.update(exit_code=completed.returncode, wall_seconds=round(time.monotonic() - started, 3),
                              response_count_after=server.request_count)
                (out / f"{label}.stdout").write_text(completed.stdout)
                (out / f"{label}.stderr").write_text(completed.stderr)
                require(completed.returncode == 0, f"{label} exited {completed.returncode}, see command stderr")
                return record

            cli("01-start", "start", ["--demo-grid"])
            initial = observed_state(state)
            save(out / "01-start-state.json", initial)
            require(len(initial["jobs"]) == 2, "start did not launch both initial Jobs")
            if not args.interrupt_worker:
                paused = cli("02-pause", "stop")
                cli("02-status", "status")
                paused_state = observed_state(state)
                save(out / "02-paused-state.json", paused_state)
                require([r["status"] for r in paused_state["runs"]] == ["PAUSED"]
                        and paused["response_count_before"] == paused["response_count_after"],
                        "operator stop failed to pause without a new model request")
            else:
                cli("02-status", "status")
            if args.interrupt_worker:
                result["fault"] = interrupt(args, command=["bin/gx-p0", "run", "--state", str(state),
                                           "--timeout", str(args.timeout)], state=state, server=server,
                                           env=env, package=PACKAGE, out=out)
            cli("03-run", "run" if args.interrupt_worker else "resume", ["--timeout", str(args.timeout)])
            before = audit(state)
            save(out / "04-before-replay.json", before)
            require({(j["job_id"], j["scheduler_ref"]) for j in initial["jobs"]} <=
                    {(j["job_id"], j["scheduler_ref"]) for j in before["jobs"]},
                    "start/pause/recovery lost original Job or backend identity")
            if args.interrupt_worker:
                prior = result["fault"]["before"]
                original = {(r["thread_id"], r["task_id"], r["attempt"]) for r in prior["bindings"]}
                require(original <= {(r["thread_id"], r["task_id"], r["attempt"]) for r in before["roots"]},
                        "recovery replaced the original attempting worker")
                require({(j["job_id"], j["scheduler_ref"]) for j in prior["jobs"]} <=
                        {(j["job_id"], j["scheduler_ref"]) for j in before["jobs"]},
                        "recovery lost original backend jobs")
                fault = result["fault"]
                require(fault["cli_returncode"] == 0
                        and len(fault["after_cli_exit"]["submissions"]) == len(before["submissions"]),
                        "the live gx-p0 run did not recover from an App Server exit without operator restart")
                fault["recovery_to_scientific_stop_seconds"] = fault["seconds_from_injection_to_cli_exit"]
            replay = cli("05-resume-stopped", "run", ["--timeout", str(args.timeout)])
            after = audit(state)
            save(out / "06-after-replay.json", after)
            require(replay["response_count_before"] == replay["response_count_after"],
                    "reopening a stopped experiment invoked the model")
            for key in ("run_id", "report_ref", "rounds", "stop_reason", "jobs", "submissions", "budgets",
                        "artifact_hashes", "decisions", "settlements", "manifests"):
                require(before[key] == after[key], f"STOP replay changed {key}")
            require(all(r.get("reasoning_effort") == "xhigh" for r in server.rows) and server.rows,
                    "test profile did not send xhigh on every actual Responses request")
            require(not any("error" in r for r in server.rows), "test peer received malformed worker input")
            require(len({r["task_id"] for r in server.rows if r.get("tool") == "gx_submit_product"}) == 6,
                    "six distinct role-owned submissions must cross the official server")
            job_windows = [(datetime.fromisoformat(j["submitted_at"].replace("Z", "+00:00")),
                            datetime.fromisoformat(j["finished_at"].replace("Z", "+00:00"))) for j in before["jobs"]]
            overlap = [r for r in server.rows if any(
                start <= datetime.fromisoformat(r["at_utc"]) <= end for start, end in job_windows)]
            # A preceding worker may finish acknowledging a submitted product while GX
            # starts the next Job. Count that separately from requests for work or results.
            waiting_calls = [r["id"] for r in overlap if r.get("tool")]
            require(not waiting_calls, f"worker requested tools while grid Job was running: {waiting_calls}")
            # At the SIGKILL boundary the fixture had an open request and withheld its
            # response. No usage receipt existed for that request.
            delivered = server.request_count - int(args.interrupt_worker)
            accounting = check_native_charges(before, delivered * 35)
            result.update(result="PASS", reconciled_usage=accounting, responses_during_grid_jobs=overlap, job_polling_calls=waiting_calls, rounds=len(before["rounds"]), jobs=len(before["jobs"]),
                          unique_worker_principals=len({s["principal"] for s in before["submissions"]}),
                          artifacts_rehashed=len(before["artifact_hashes"]), responses_requests=server.request_count,
                          cost={"external_model_requests": 0, "external_model_cost_usd": 0, "gpu_seconds": 0,
                                "synthetic_usage_per_response": {"input_tokens": 30, "output_tokens": 5},
                                "synthetic_reported_tokens": 35*delivered,
                                "gx_budget_balances": before["budgets"],
                                "job_cpu_measurements": [json.loads(j["measured_json"]) for j in before["jobs"]]},
                          replay_new_model_calls=0, replay_new_jobs=0, replay_new_submissions=0)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["ended"] = datetime.now(timezone.utc).isoformat()
        save(out / "summary.json", result)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True, help="New evidence directory")
    p.add_argument("--work", type=Path, help="New directory in which to preserve isolated state for diagnosis")
    p.add_argument("--interrupt-worker", action="store_true",
                   help="Kill the active app-server at R0 review, then resume the same state")
    p.add_argument("--codex", default=os.environ.get("GX_P0_CODEX", "codex"))
    p.add_argument("--timeout", type=int, default=600)
    execute(p.parse_args())
