"""Open-loop real-OAuth acceptance workload. Launch only against a new private staging directory."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import time

WAVE_OFFSETS = tuple(range(0, 7201, 600))
LANES = ("west", "east")
MODEL = "gpt-6.2-astra"
EFFORT = "xhigh"
OFFICIAL_BINARY_SHA256 = "b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b"


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def append(path, data):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, sort_keys=True) + "\n")


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def check_proxy():
    expected = {"http_proxy":"http://127.0.0.1:7890", "https_proxy":"http://127.0.0.1:7890",
                "all_proxy":"socks5h://127.0.0.1:7891"}
    for k,v in expected.items():
        if any(os.getenv(case) != v for case in (k,k.upper())):
            raise ValueError("Required Clash setting missing: " + k)
    for k in ("NO_PROXY","no_proxy"):
        if any(item not in {"localhost","127.0.0.1","::1"} for item in os.environ[k].split(",")):
            raise ValueError("Nonlocal proxy bypass")
    for port in (7890,7891):
        with socket.create_connection(("127.0.0.1",port), timeout=2):
            pass


def specification(base, wave, lane):
    spec = json.loads((base / "examples/paired-grid.json").read_bytes())
    idx = LANES.index(lane)
    # Distinct frozen grids with a positive and negative control in every Run.
    threshold = (-0.25, -0.125, -0.375)[idx]
    shift = (idx - 1) / 8 + wave / 128
    spec.update(project="v3-multiroot-finite-grid", workflow="science_grid", workflow_version="2",
        objective="Use measured CPU grid outputs to compare two finite sets of coordinates for score(x)=-(x-2)^2. Review both sets against each run's frozen cutoff and close the single-round comparison.",
        max_rounds=1, max_parallel_roots=2, workspace={"max_parallel_roots":2}, turn_token_budget=180000,
        workspace_budget={"tokens":900000,"api_calls":6,"worker_tasks":12,"job_submissions":4,
                          "cpu_seconds":300,"wall_hours":6,"gpu_jobs":0,"gpu_minutes":0},
        run_budget={"tokens":440000,"api_calls":3,"worker_tasks":6,"job_submissions":2,
                    "cpu_seconds":120,"wall_hours":2,"gpu_jobs":0,"gpu_minutes":0},
        teams=[{"team_id":lane, "name":lane.title()+" grid",
                "max_parallel_roots":1, "roles":["planner","evidence_reviewer"],
                "limits":{"tokens":435000,"api_calls":3,"worker_tasks":5}}])
    spec["proposal"]["threshold"] = threshold
    for ix, route in enumerate(spec["proposal"]["paths"]):
        route["id"] = lane + ("-near" if ix==0 else "-offset")
        route["prediction"] = f"A coordinate in this {lane} grid has measured score at or above {threshold}."
        for p in route["grid"]["candidates"]:
            p["candidate_id"] = f'{wave:02d}-{lane}-{p["candidate_id"]}'
            p["x"] += shift
    return spec


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--private", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    source, runtime, work = args.source.resolve(), args.runtime.resolve(), args.private.resolve()
    out = args.evidence.resolve()
    work.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True,exist_ok=True)
    if (out/"environment.json").exists():
        raise FileExistsError("Evidence directory already belongs to an acquisition")
    check_proxy()
    stage = work/"install"
    stage.mkdir()
    def git(*argv):
        return subprocess.check_output(["git","-C",source,*argv],text=True).strip()
    commit = git("rev-parse", "--verify", args.revision + "^{commit}")
    paths = [name for name in ("bin","codex_adapter","examples","gx-h","plugins") if git("ls-tree","--name-only",commit,"--",name)]
    with tempfile.TemporaryFile(dir=work) as f:
        subprocess.run(["git","-C",source,"archive",commit,"--",*paths],stdout=f,check=True)
        f.seek(0)
        with tarfile.open(fileobj=f) as t:
            t.extractall(stage, filter="data")
    official = runtime/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    python = runtime/".venv/bin/python"
    if sha(official) != OFFICIAL_BINARY_SHA256:
        raise RuntimeError("Official binary digest mismatch")
    private_home = work/"codex-home"
    private_home.mkdir()
    operator_home = Path(os.environ.get("CODEX_HOME",str(Path.home()/".codex")))
    for name in ("auth.json","models_cache.json"):
        src = operator_home/name
        if src.is_file():
            shutil.copyfile(src, private_home/name)
    profile = work/"profile.json"
    save(profile,{"default":{"provider":"openai","model":MODEL,"effort":EFFORT}})
    env = {**os.environ,"CODEX_HOME":str(private_home),"PYTHONDONTWRITEBYTECODE":"1",
           "GX_P0_CODEX":str(official),"GX_P0_PROFILE":str(profile),
           "GX_METRICS_FILE":str(work/"metrics.jsonl"),"PATH":str(runtime/".venv/bin")+":"+os.environ["PATH"]}
    if any(k in env for k in ("GX_P0_TEST","GX_P0_TEST_PROVIDER","GX_P0_PROVIDER","OPENAI_BASE_URL","OPENAI_API_BASE","CODEX_INSECURE_ALLOW_NO_SANDBOX","OPENAI_API_KEY")):
        raise ValueError("Unexpected model provider/test/API-key override")
    prefix = [str(python),str(stage/"bin/gx-p0")]
    state = work/"state"
    def launch(label, args_):
        check_proxy()
        command = prefix + args_
        row={"command":command,"cwd":str(stage),"label":label,"at":utc()}
        with (work/(label+".out")).open("w") as stdout, (work/(label+".err")).open("w") as stderr:
            proc = subprocess.Popen(command,cwd=stage,env=env,stdout=stdout,stderr=stderr)
        row["pid"] = proc.pid
        append(out/"commands.jsonl",row)
        return proc,row
    def receipt(proc, row):
        label=row["label"]
        append(out/"commands.jsonl",{"label":label,"pid":proc.pid,"exit_code":proc.returncode,
               "ended":utc(),"stdout_sha256":sha(work/(label+".out")),"stderr_sha256":sha(work/(label+".err"))})
    osmeta = dict(platform.freedesktop_os_release())
    start_env = {"at":utc(),"source_commit":commit,"source_status":git("status","--porcelain","--untracked-files=no"),
        "source_origin":"immutable git archive; excluded other paths are not runtime inputs",
        "binary_sha256":sha(official),"codex":subprocess.check_output([official,"--version"],env=env,text=True).strip(),
        "model":MODEL,"provider":"openai","auth":"official ChatGPT OAuth, private Codex home","effort":EFFORT,
        "gx_model_test_mode":False,"python":subprocess.check_output([python,"--version"],text=True).strip(),
        "sqlite":sqlite3.sqlite_version,"os":osmeta.get("PRETTY_NAME"),"kernel":platform.release(),"machine":platform.machine(),
        "cpu":next(l.split(":",1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines() if l.startswith("model name")),
        "logical_cpus":os.cpu_count(),"cpu_affinity":sorted(os.sched_getaffinity(0)),
        "memory_kib":int(next(l.split()[1] for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal:"))),
        "clash_endpoints":{"http":"http://127.0.0.1:7890","https":"http://127.0.0.1:7890","socks":"socks5h://127.0.0.1:7891","no_proxy":env["NO_PROXY"]},
        "oauth_transport_flags":["--enable","respect_system_proxy","--disable","system_proxy_fallback"],
        "wave_offsets_seconds":WAVE_OFFSETS, "lanes":LANES, "workspace_root_cap":2, "team_root_cap":1,
        "runner_sha256":sha(Path(__file__)),"stage_paths":paths,"predeclared_comparison": {
        "safety":"no duplicate job operation keys or invalid artifact receipts, no database integrity errors",
        "percentiles":"nearest rank p50, p95, p99; reported individually",
        "volume":"same 26 run specs, 52 initial numerical jobs, 13 x 900k workspace tokens and 13 x 6 native-turn ceilings",
        "capacity":"at least 2 simultaneous real workers; first admitted to last completed real turn spans at least 7200s; report actual occupied time separately",
        "latency_review":"investigate p95 increase over both 25% and 5 ms for GX dispatch, callbacks and SQLite transactions",
        "quality_review":"investigate greater than 10% decrease in completed turns/hour or accepted runs/hour, added job failures or discrepancies in accounting"}}
    save(out/"environment.json", start_env)
    pre, pre_row=launch("oauth-readiness", ["doctor"])
    pre.wait(timeout=125)
    receipt(pre,pre_row)
    readiness=json.loads((work/"oauth-readiness.out").read_bytes())
    save(out/"oauth-readiness.json",{k:readiness.get(k) for k in ("codex_version","expected_version","provider","model","ready","validation","account_type")})
    if pre.returncode or not readiness.get("ready") or readiness.get("account_type")!="chatgpt":
        raise RuntimeError("OAuth/model readiness did not pass")
    specdir=work/"specifications"
    specdir.mkdir()
    released = utc()
    base_time = time.monotonic()
    save(out/"start.json",{"released":released,"clock":"UTC plus local monotonic elapsed","private_staging":str(work)})
    drivers = []
    def collect_drivers():
        for row in list(drivers):
            if row[0].poll() is not None:
                receipt(*row)
                drivers.remove(row)
    for wave,offset in enumerate(WAVE_OFFSETS):
        while (remain:=base_time+offset-time.monotonic()) > 0:
            collect_drivers()
            time.sleep(min(5,remain))
        collect_drivers()
        state = work / f"state-{wave:02d}"
        for lane in LANES:
            key=f"{wave:02d}-{lane}"
            spec_file = specdir/(key+".json")
            save(spec_file, specification(stage,wave,lane))
            r, info=launch("start-"+key,["start","--state",str(state),"--run-key",key,"--spec",str(spec_file)])
            r.wait(timeout=180)
            receipt(r,info)
            if r.returncode:
                raise RuntimeError("Staging Start failed: "+key)
        driver=launch(f"drive-{wave:02d}",["run","--state",str(state),"--timeout","870"])
        drivers.append(driver)
        append(out/"arrivals.jsonl",{"wave":wave,"utc":utc(),"seconds_from_first_release":round(time.monotonic()-base_time,3),
               "scheduled_offset_seconds":offset,"driver_pid":driver[0].pid})
    while drivers:
        collect_drivers()
        if drivers:
            time.sleep(2)
    save(out/"finished.json",{"start":released,"ended":utc(),"elapsed_monotonic_s":time.monotonic()-base_time,
        "arrivals":len(WAVE_OFFSETS),"attempted_runs":len(WAVE_OFFSETS)*len(LANES),"private_staging":str(work)})
    print("Acquisition finished. Audit the journals and receipts before classifying the gate.",flush=True)


if __name__=="__main__":
    main()
