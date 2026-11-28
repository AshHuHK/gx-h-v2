"""Run one prospectively registered arm. Only live processes owned by this launch may be interrupted."""

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from support import (
    BASE,
    BINARY,
    EFFORT,
    HERE,
    MODEL,
    P,
    deploy,
    environment,
    git,
    goal_config,
    read,
    registered,
    save,
    sha,
)
from common import oauth_host
from trial import family, gx_facts, start
from gx_core.capabilities import procs


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def physical(folder):
    for path in (folder / "state/jobs").glob("*/status.json"):
        if (path.parent / "outcome.json").exists():
            continue
        try:
            status = read(path)
        except (OSError, ValueError):
            continue
        if status.get("state") != "running":
            continue
        if all(
            procs.is_alive(status.get(k + "_pid"), status.get(k + "_started"))
            for k in ("runner", "tool")
        ):
            return {
                key: status[key]
                for key in ("job_id", "runner_pid", "runner_started", "tool_pid", "tool_started")
            }
    return None


def stop_controller(p, job=None):
    owned = family(p.pid)
    hosts = []
    for pid in owned - {p.pid}:
        try:
            name = Path(os.readlink("/proc/" + str(pid) + "/exe")).name
        except FileNotFoundError:
            continue
        if name.startswith("codex"):
            hosts.append(pid)
    when = time.time()
    for pid in [p.pid, *hosts]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    physical_after = None
    if job is not None:
        physical_after = {name: procs.process_start_ticks(job[name + "_pid"]) for name in ("runner", "tool")}
    p.wait(timeout=10)
    return {
        "at": when,
        "physical_start_ticks_after_signal": physical_after,
        "controller_pid": p.pid,
        "host_pids": hosts,
        "controller_returncode": p.returncode,
    }


def late(folder, arm, release, env):
    if (folder / "late-delivery.json").exists():
        return
    receipt = {"at": time.time(), "source_sha256": sha(P / "sites.rdb")}
    if arm == "stock":
        receipt["route"] = "same native thread; inventory supplied in resumed or follow-up turn"
    else:
        g = gx_facts(folder)["goal"]
        if g["status"] == "CLOSED":
            return
        arg = [
            sys.executable,
            "-B",
            str(release / "bin/gx-p0"),
            "message",
            "--state",
            str(folder / "state"),
            "--run-id",
            g["run_id"],
            "--message-id",
            "original-station-inventory",
            "--queue-only",
            "--text",
            "The USGS station inventory arrived.\n" + (P / "sites.rdb").read_text(),
        ]
        cp = subprocess.run(
            arg, cwd=release, env=env, check=True, capture_output=True, text=True, timeout=30
        )
        receipt["receipt"] = json.loads(cp.stdout)
    save(folder / "late-delivery.json", receipt)


def drive(folder, arm, release, env, phase, deadline):
    if (folder / (phase + "-launch.json")).exists():
        raise RuntimeError(
            "The original phase already launched; inspect its existing Run or thread before continuation"
        )
    if arm == "stock":
        args = [sys.executable, "-B", str(HERE / "native_arm.py"), str(folder), phase, str(release)]
    else:
        args = [
            sys.executable,
            "-B",
            str(release / "bin/gx-p0"),
            "research",
            "--state",
            str(folder / "state"),
            "--timeout",
            str(max(1, int(deadline - time.time()))),
        ]
        if phase == "first":
            args += [
                "--run-key",
                "monthly-water-first",
                "--goal-file",
                str(P / "question.txt"),
                "--goal-config",
                str(folder / "goal.json"),
            ]
        else:
            args += ["--run-id", gx_facts(folder)["goal"]["run_id"]]
    start_time = time.time()
    save(folder / (phase + "-launch.json"), {"at": start_time})
    p = start(folder, phase, args, release, env if arm != "stock" else None)
    try:
        while p.poll() is None:
            if time.time() > deadline:
                return "deadline"
            if phase == "first" and not (folder / "late-delivery.json").exists():
                if job := physical(folder):
                    # A still-live worker after SIGKILL distinguishes execution from delayed status publication.
                    killed = stop_controller(p, job)
                    terminal = folder / "state/jobs" / job["job_id"] / "outcome.json"
                    after = killed["physical_start_ticks_after_signal"]
                    alive_after_signal = all(after[k] == job[k + "_started"] for k in ("runner", "tool"))
                    eligible = alive_after_signal and p.returncode == -signal.SIGKILL and (
                        not terminal.exists() or instant(read(terminal)["finished_at"]) > killed["at"]
                    )
                    save(
                        folder / "interruption.json",
                        {"original_job": job, **killed, "qualified_at_cut": eligible},
                    )
                    return "restart_original"
                if time.time() - start_time >= 240:
                    late(folder, arm, release, env)
            time.sleep(0.005 if phase == "first" else 0.12)
        if p.returncode:
            return "driver_exit_" + str(p.returncode)
        if arm == "stock":
            term = read(folder / (phase + "-terminal.json"))
            if term["status"] != "completed":
                return "native_" + term["status"]
            if phase == "first" and (folder / "late-delivery.json").exists() and not (folder / "native-steer-acknowledged.json").exists():
                return "original_followup"
            return "finished" if term["answer"] else "no_report"
        g = gx_facts(folder, required=False)["goal"]
        return "finished" if g and g["status"] == "CLOSED" else "research_open"
    finally:
        if p.poll() is None:
            stop_controller(p)


def run(work, arm, candidate):
    if len(candidate) != 40 or git("rev-parse", candidate).decode().strip() != candidate:
        raise ValueError("Exact frozen candidate commit required")
    spec = registered()
    folder = work / arm
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".arm.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (folder / "outcome.json").exists():
            return read(folder / "outcome.json")
        save(
            work / "series.json",
            {
                "registration_sha256": sha(HERE / "preregistered.json"),
                "candidate": candidate,
                "baseline": BASE,
                "native_binary_sha256": sha(BINARY),
            },
        )
        release = deploy(folder, candidate if arm == "candidate" else BASE)
        if not (folder / "account.json").exists():
            pf = oauth_host().preflight(BINARY, MODEL, EFFORT)
            if pf["status"] != "READY":
                raise RuntimeError(pf.get("reason_codes"))
            save(
                folder / "account.json",
                {k: pf[k] for k in ("status", "auth_type", "subscription_usage_state")},
            )
        env = environment(folder)
        save(folder / "goal.json", goal_config())
        if not (folder / "began.json").exists():
            save(
                folder / "began.json",
                {"at": time.time(), "arm": arm, "release": read(release / "pin.json")},
            )
        t = read(folder / "began.json")["at"]
        deadline = t + spec["seconds_per_arm"]
        if (folder / "interruption.json").exists() and not (folder / "second-launch.json").exists():
            state = "restart_original"
        else:
            state = drive(folder, arm, release, env, "first", deadline)
        if state in ("restart_original", "original_followup"):
            late(folder, arm, release, env)
            state = drive(folder, arm, release, env, "second", deadline)
        result = {
            "arm": arm,
            "outcome": state,
            "elapsed_seconds": round(time.time() - t, 3),
            "facts": {} if arm == "stock" else gx_facts(folder, required=False),
        }
        save(folder / "outcome.json", result)
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("arm", choices=["stock", "baseline", "candidate"])
    p.add_argument("candidate")
    a = p.parse_args()
    print(json.dumps(run(a.work.resolve(), a.arm, a.candidate)))
