"""Bounded official-Codex acquisition of the historical paired numerical grid."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import time

RELEASE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RELEASE / "acceptance/v3/performance"))
from run_workload import EFFORT, LANES, MODEL, OFFICIAL_BINARY_SHA256, append, save, sha, specification, utc

PATHS = ("bin", "codex_adapter", "examples", "gx-h")
HTTP = "http://127.0.0.1:7890"
SOCKS = "socks5h://127.0.0.1:7891"
BYPASS = "localhost,127.0.0.1,::1"
OVERRIDES = ("GX_P0_TEST", "GX_P0_TEST_PROVIDER", "GX_P0_PROVIDER", "OPENAI_BASE_URL",
             "OPENAI_API_BASE", "CODEX_INSECURE_ALLOW_NO_SANDBOX", "OPENAI_API_KEY")


def proxy_env():
    """Check both mandatory local routes before starting any network-capable process."""
    overrides = [name for name in OVERRIDES if name in os.environ]
    if overrides:
        raise RuntimeError("Unexpected provider or test environment settings: " + ", ".join(overrides))
    env = os.environ.copy()
    allowed = set(BYPASS.split(","))
    for casing in ("no_proxy", "NO_PROXY"):
        if casing in env:
            allowed.intersection_update(item.strip() for item in env[casing].split(","))
    bypass = ",".join(host for host in BYPASS.split(",") if host in allowed)
    for key, value in dict(HTTP_PROXY=HTTP, HTTPS_PROXY=HTTP, ALL_PROXY=SOCKS, NO_PROXY=bypass).items():
        env[key] = env[key.lower()] = value
    for port in (7890, 7891):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    return env


def runtime_env(work, runtime, profile=None, *, resume=False):
    """Private official OAuth home and inherited mandatory proxy for every descendant."""
    os.umask(0o077)
    env = proxy_env()
    official = runtime / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    python = runtime / ".venv/bin/python"
    if sha(official) != OFFICIAL_BINARY_SHA256 or not python.is_file():
        raise RuntimeError("Official runtime identity mismatch")
    auth_home = work / "codex-home"
    profile_path = work / "profile.json"
    if not resume:
        auth_home.mkdir(mode=0o700)
        operator = Path(env.get("CODEX_HOME", Path.home() / ".codex"))
        if not (operator / "auth.json").is_file():
            raise RuntimeError("Established OAuth credentials unavailable")
        for name in ("auth.json", "models_cache.json"):
            if (operator / name).is_file():
                shutil.copyfile(operator / name, auth_home / name)
        save(profile_path, profile or {"default": {"provider": "openai", "model": MODEL, "effort": EFFORT}})
    elif not (auth_home / "auth.json").is_file() or not profile_path.is_file():
        raise RuntimeError("Original scientific Run's private OAuth home or profile missing")
    env.update(CODEX_HOME=str(auth_home), GX_P0_CODEX=str(official), GX_P0_PROFILE=str(profile_path),
               GX_METRICS_FILE=str(work / "metrics.jsonl"), PYTHONDONTWRITEBYTECODE="1",
               PATH=str(runtime / ".venv/bin") + os.pathsep + env["PATH"])
    return env, python, official


def stage_source(source, revision, stage, patches):
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "--verify",
                                        revision + "^{commit}"], text=True).strip()
    stage.mkdir()
    with tempfile.TemporaryFile(dir=stage.parent) as archive:
        subprocess.run(["git", "-C", source, "archive", revision, "--", *PATHS], stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tree:
            tree.extractall(stage, filter="data")
    for patch in patches:
        subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch.resolve())], cwd=stage,
                       env={**os.environ, "GIT_CEILING_DIRECTORIES": str(stage.parent.resolve())}, check=True)
    file_hashes = {}
    for file in sorted(stage.rglob("*")):
        if file.is_symlink():
            file_hashes[str(file.relative_to(stage))] = "link:" + os.readlink(file)
        elif file.is_file():
            file_hashes[str(file.relative_to(stage))] = sha(file)
    return {"source_commit": revision, "paths": list(PATHS), "files": len(file_hashes),
            "stage_sha256": hashlib.sha256(json.dumps(file_hashes, sort_keys=True).encode()).hexdigest(),
            "overlays": [{"file": p.name, "sha256": sha(p)} for p in patches]}


def sockets(pid, official):
    counts = Counter()
    pending, visited = [pid], set()
    while pending:
        child = pending.pop()
        if child in visited:
            continue
        visited.add(child)
        process = Path("/proc") / str(child)
        try:
            for task in (process / "task").iterdir():
                try:
                    pending.extend(int(value) for value in (task / "children").read_text().split())
                except FileNotFoundError:
                    continue
            if (process / "exe").resolve() != official.resolve():
                continue
            descriptors = set()
            for fd in (process / "fd").iterdir():
                try:
                    descriptors.add(os.readlink(fd))
                except FileNotFoundError:
                    continue
            for filename, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
                for line in (process / "net" / filename).read_text().splitlines()[1:]:
                    fields = line.split()
                    if fields[3] != "01" or "socket:[" + fields[9] + "]" not in descriptors:
                        continue
                    encoded, port = fields[2].split(":")
                    packed = b"".join(struct.pack("=I", int(encoded[i:i+8], 16))
                                      for i in range(0, len(encoded), 8))
                    host, port = socket.inet_ntop(family, packed), int(port, 16)
                    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                        raise RuntimeError("Official App Server has a non-loopback internet peer")
                    if port in (7890, 7891):
                        counts[f"{host}:{port}"] += 1
        except FileNotFoundError:
            continue
    return counts


def command(argv, *, work, evidence, stage, env, label, deadline, monitor=False):
    proxy_env()
    started = time.monotonic()
    usage = Counter()
    with (work / (label + ".out")).open("wb") as stdout, (work / (label + ".err")).open("wb") as stderr:
        proc = subprocess.Popen(argv, cwd=stage, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Acquisition wall-time ceiling reached")
                try:
                    proc.wait(timeout=min(2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    proxy_env()
                    if monitor:
                        usage.update(sockets(proc.pid, Path(env["GX_P0_CODEX"])))
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(5)
            append(evidence / "commands.jsonl",
                   {"at": utc(), "label": label, "argv": [str(v) for v in argv], "pid": proc.pid,
                    "exit_code": proc.returncode, "wall_seconds": round(time.monotonic() - started, 3),
                    "clash_established_socket_samples": dict(usage), **{
                        name + "_sha256": sha(work / (label + "." + suffix))
                        for name, suffix in (("stdout", "out"), ("stderr", "err"))}})
    return proc.returncode


def statuses(state):
    with sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True) as conn:
        return dict(conn.execute("SELECT w.run_key,r.status FROM runs r JOIN p0_workspace_runs w USING(run_id)"))


def resume_started_wave(work, out, runtime, wave):
    """Resume the same public state and official home after an interrupted single-wave acquisition."""
    plan = json.loads((out / "environment.json").read_bytes())
    if plan["waves_planned"] != 1 or wave != 0 or (out / "finished.json").exists():
        raise ValueError("This continuation expects an unfinished single wave")
    state = work / f"state-{wave:02d}"
    if set(statuses(state)) != {f"{wave:02d}-{lane}" for lane in plan["lanes"]}:
        raise ValueError("Existing workspace does not match the original admission")
    env, python, _ = runtime_env(work, runtime, resume=True)
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(plan["began_utc"])).total_seconds()
    remaining = math.floor(plan["wall_ceiling_seconds"] - elapsed)
    if remaining <= 0:
        raise TimeoutError("Original single-wave wall ceiling exhausted")
    stage = work / "install"
    started = time.monotonic()
    cmd = [str(python), str(stage / "bin/gx-p0"), "run", "--state", str(state), "--timeout", str(remaining)]
    code = command(cmd, work=work, evidence=out, stage=stage, env=env, label="drive-00-resume",
                   deadline=started + remaining, monitor=True)
    results = statuses(state)
    append(out / "waves.jsonl", {"wave": wave, "exit_code": code, "run_status": results,
           "driver_wall_seconds": round(time.monotonic() - started, 3), "ended": utc(),
           "continued_original_state": True, "recovery_runner_sha256": sha(Path(__file__))})
    if code or set(results.values()) != {"COMPLETED"}:
        raise RuntimeError("Original workspace did not complete within its original ceiling")
    save(out / "finished.json", {"waves_completed": 1, "ended_utc": utc(),
        "total_wall_seconds": round(elapsed + time.monotonic() - started, 3), "continued_original_state": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=RELEASE)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--patch", type=Path, action="append", default=[])
    parser.add_argument("--workflow-version", choices=("2", "3"), required=True)
    parser.add_argument("--waves", type=int, choices=range(14), required=True)
    parser.add_argument("--lanes", nargs="+", choices=LANES, default=list(LANES))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--wall-seconds", type=int, default=7200)
    options = parser.parse_args()
    if len(set(options.lanes)) != len(options.lanes) or not 1 <= options.wall_seconds <= 7200:
        parser.error("Lanes must be distinct and wall time in 1..7200 seconds")
    os.umask(0o077)
    work, out = options.private.resolve(), options.evidence.resolve()
    work.mkdir(parents=True, mode=0o700, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    origin = time.monotonic()
    deadline = origin + options.wall_seconds
    stage = work / "install"
    source = stage_source(options.source.resolve(), options.revision, stage, options.patch)
    env, python, official = runtime_env(work, options.runtime.resolve(), (
        json.loads(options.profile.read_bytes()) if options.profile else None))
    prefix = [str(python), str(stage / "bin/gx-p0")]
    params = dict(work=work, evidence=out, stage=stage, env=env, deadline=deadline)
    plugin = "org.gx.science-grid@" + options.workflow_version + ".0.0"
    save(out / "environment.json", {**source, "began_utc": utc(), "private": str(work),
        "provider": "openai", "model": MODEL, "reasoning_effort": EFFORT,
        "auth": "existing official ChatGPT OAuth, isolated private home",
        "profile_sha256": sha(work / "profile.json"), "binary_sha256": sha(official),
        "binary_version": subprocess.check_output([official, "--version"], env=env, text=True).strip(),
        "http_https_proxy": HTTP, "socks5_proxy": SOCKS, "no_proxy": env["NO_PROXY"],
        "codex_flags": ["--enable", "respect_system_proxy", "--disable", "system_proxy_fallback"],
        "plugin": plugin, "waves_planned": options.waves, "lanes": options.lanes,
        "wave_policy": "serial paired workspaces, next wave opens when the preceding driver exits",
        "wall_ceiling_seconds": options.wall_seconds, "max_concurrent_roots": len(options.lanes),
        "budget": {"workspace_tokens_per_wave": 900000, "run_tokens": 440000,
                   "run_api_calls": 3, "run_jobs": 2, "turn_reservation_tokens": 180000,
                   "maximum_run_tokens_this_acquisition": options.waves * len(options.lanes) * 440000},
        "historical_input_generator_sha256": sha(RELEASE / "acceptance/v3/performance/run_workload.py"),
        "acquisition_runner_sha256": sha(Path(__file__))})
    code = command([*prefix, "doctor"], label="doctor", **params)
    checked = json.loads((work / "doctor.out").read_bytes())
    save(out / "readiness.json", {k: checked.get(k) for k in (
        "ready", "validation", "account_type", "codex_version", "expected_version", "model", "provider")})
    if code or checked.get("account_type") != "chatgpt" or not checked.get("ready"):
        raise RuntimeError("Official OAuth and model-catalog readiness failed before inference")
    code = command([*prefix, "plugin", "verify", plugin], label="plugin-verify", **params)
    verify = json.loads((work / "plugin-verify.out").read_bytes())
    if code or verify.get("failures"):
        raise RuntimeError("Public plugin verification failed")
    package = next(p for p in verify["plugins"] if p["id"] + "@" + p["version"] == plugin)
    save(out / "plugin.json", package)
    expected_workflow = "science_grid@" + options.workflow_version
    if expected_workflow not in package["contributions"]["workflows"]:
        raise RuntimeError("Public plugin does not supply requested Workflow")
    completed_waves = 0
    for wave in range(options.waves):
        state = work / f"state-{wave:02d}"
        for lane in options.lanes:
            key = f"{wave:02d}-{lane}"
            spec = specification(stage, wave, lane)
            spec.update(workflow_version=options.workflow_version, plugins=[plugin])
            spec_file = out / ("spec-" + key + ".json")
            save(spec_file, spec)
            cmd = [*prefix, "start", "--state", str(state), "--run-key", key, "--spec", str(spec_file)]
            if command(cmd, label="start-" + key, **params):
                raise RuntimeError("Public Project start failed for " + key)
            opened = json.loads((work / ("start-" + key + ".out")).read_bytes())["plugin_binding"]
            pins = {p["id"] + "@" + p["version"]: p["sha256"] for p in opened["packages"]}
            if pins.get(plugin) != package["sha256"] or (
                opened["workflow"]["id"], opened["workflow"]["version"]) != ("science_grid", options.workflow_version):
                raise RuntimeError("Run opened with unexpected plugin or Workflow")
        run_timeout = min(600, math.floor(deadline - time.monotonic()))
        if run_timeout < 1:
            raise TimeoutError("No remaining time for a new wave")
        started = time.monotonic()
        cmd = [*prefix, "run", "--state", str(state), "--timeout", str(run_timeout)]
        code = command(cmd, label=f"drive-{wave:02d}", monitor=True, **params)
        results = statuses(state)
        append(out / "waves.jsonl", {"wave": wave, "exit_code": code, "run_status": results,
               "driver_wall_seconds": round(time.monotonic() - started, 3), "ended": utc()})
        if code or set(results.values()) != {"COMPLETED"}:
            raise RuntimeError(f"Wave {wave} did not complete; preserve state and reconcile before another acquisition")
        completed_waves += 1
    save(out / "finished.json", {"waves_completed": completed_waves, "ended_utc": utc(),
         "total_wall_seconds": round(time.monotonic() - origin, 3)})


if __name__ == "__main__":
    main()
