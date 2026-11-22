"""Run two public grid projects through one GX workspace, a real App Server and bounded local CPU Jobs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import tarfile
import time

import e2e_responses
from e2e_grid import fixture_env
from gx_core.p0.cli import demo_spec

BASE = Path(__file__).resolve().parents[1]
CODEX = BASE / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
PYTHON = BASE / ".venv/bin/python"
ROLES = ["planner", "evidence_reviewer"]


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def check(ok, detail):
    if not ok:
        raise AssertionError(detail)


def rows(conn, sql, args=()):
    return [dict(row) for row in conn.execute(sql, args)]


def make_install(work, args):
    stage = work / "install"
    source_dirs = ("gx-h", "bin", "codex_adapter")
    if revision := getattr(args,"revision",None):
        commit = subprocess.check_output(["git","rev-parse","--verify",revision+"^{commit}"],cwd=BASE,text=True).strip()
        stage.mkdir()
        with tempfile.TemporaryFile(dir=work) as archive:
            subprocess.run(["git","archive",commit,"--",*source_dirs],cwd=BASE,stdout=archive,check=True)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as contents:
                contents.extractall(stage,filter="data")
    else:
        for sub in source_dirs:
            shutil.copytree(BASE / sub, stage / sub, ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "node_modules", "*.pyc"))
    limits = stage / "gx-h/config/limits.json"
    cfg = json.loads(limits.read_text())
    cfg["task_budget"]["lease_seconds"] = args.lease
    cfg["research_budget"]["max_literature_replanning_rounds"] = args.rounds
    cfg.setdefault("native_provider", {}).setdefault("parallel_turns", {})["gx_loopback"] = 2
    save(limits, cfg)
    digest = hashlib.sha256()
    for f in sorted(stage.rglob("*")):
        if f.is_file():
            digest.update(str(f.relative_to(stage)).encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    return stage, digest.hexdigest()


def project_spec(key, rounds):
    spec = demo_spec()
    cutoff = -.25 if key == "A" else -.125
    name = "west" if key == "A" else "east"
    spec.update(project="v21-staging-grid", max_rounds=rounds, max_parallel_roots=2,
                objective="Test two finite-grid search strategies on score(x)=-(x-2)^2 against their own frozen cutoffs.",
                workspace={"max_parallel_roots":2},
                turn_token_budget=2048,
                run_budget={"tokens":400000,"api_calls":60,"worker_tasks":55,"job_submissions":50,
                            "cpu_seconds":4000,"wall_hours":6},
                teams=[{"team_id":name, "name":name.title()+" numerical research",
                        "limits":{"tokens":260000,"api_calls":50,"worker_tasks":50},
                        "max_parallel_roots":1,"roles":ROLES}])
    spec["proposal"]["threshold"] = cutoff
    for index, route in enumerate(spec["proposal"]["paths"]):
        region = "near" if index == 0 else "offset"
        route["id"] = name + "-" + region
        route["prediction"] = f"The {name} {region} finite grid contains a measured score at or above {cutoff}."
        for point in route["grid"]["candidates"]:
            point["candidate_id"] = name + "-" + point["candidate_id"]
            if key == "B":
                point["x"] += .125
    return spec


def timed_provider(latency):
    original = e2e_responses.fixture_action
    def action(body, ident):
        item, info = original(body, ident)
        if "task_id" in info:
            # Simulated model latency applies only to actual requests for this numerical investigation.
            if latency:
                time.sleep(latency)
            if info.get("tool") == "gx_submit_product" and info["output_kind"] == "science_decision" and info["round"] == 0:
                # Keep the original job/pid visible long enough to inject a deterministic coordinator crash.
                args = json.loads(item["arguments"])
                for grid in args["product"]["next_grids"].values():
                    grid["delay_seconds"] = 5
                item["arguments"] = json.dumps(args)
        return item, info
    return action


class Staging:
    def __init__(self, work, stage, server, env, deadline):
        self.work, self.stage, self.server, self.env = work, stage, server, env
        self.state = work / "state"
        self.deadline = deadline
        self.commands = []
        self.owned = []

    def command(self, label, action, *extra, background=False):
        argv = [str(PYTHON), str(self.stage / "bin/gx-p0"), action, "--state", str(self.state), *map(str, extra)]
        start = time.monotonic()
        record = {"label":label, "command":[action, *map(str,extra)], "at":utc(), "requests_before":self.server.request_count}
        self.commands.append(record)
        out = self.work / (label + ".out")
        err = self.work / (label + ".err")
        with out.open("w") as stdout, err.open("w") as stderr:
            proc = subprocess.Popen(argv, env=self.env, cwd=self.stage, stdout=stdout, stderr=stderr)
        self.owned.append(proc)
        record["pid"] = proc.pid
        if background:
            return proc, record
        code = proc.wait(timeout=min(60, max(1,self.deadline-time.monotonic())))
        record.update(exit=code, seconds=round(time.monotonic()-start,3), requests_after=self.server.request_count)
        check(code==0, f"{label} exited {code}: {err.read_text()[-700:]}")
        return json.loads(out.read_text())

    def db(self):
        conn = sqlite3.connect((self.state / "gx.db").as_uri()+"?mode=ro",uri=True,timeout=4)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        return conn

    def snapshot(self):
        with closing(self.db()) as conn:
            snapshots = {}
            for rid,status in conn.execute("SELECT run_id,status FROM runs ORDER BY rowid"):
                snapshots[rid] = {
                    "status": status,
                    "rounds": conn.execute("SELECT count(*) FROM science_decisions WHERE run_id=?",(rid,)).fetchone()[0],
                    "jobs": rows(conn,"SELECT job_id,status,idempotency_key,scheduler_ref,job_attempt FROM jobs WHERE run_id=? ORDER BY rowid",(rid,)),
                    "submissions": conn.execute("SELECT count(*) FROM submissions JOIN tasks USING(task_id) WHERE run_id=? AND result='ACCEPTED'",(rid,)).fetchone()[0],
                    "worker_turns": conn.execute("SELECT count(*) FROM p0_root_deliveries d JOIN p0_roots r USING(thread_id) WHERE d.run_id=? AND r.task_id IS NOT NULL AND method='turn/start'",(rid,)).fetchone()[0],
                    "held": conn.execute("SELECT count(*) FROM p0_meter_native_turns WHERE run_id=? AND budget_status='HELD'",(rid,)).fetchone()[0],
                    "accepted_awaiting_final": conn.execute("SELECT count(*) FROM p0_meter_native_turns n JOIN p0_roots r USING(thread_id) JOIN tasks t USING(task_id) WHERE n.run_id=? AND n.budget_status='HELD' AND t.status='ACCEPTED'",(rid,)).fetchone()[0]
                }
            return snapshots

    def until(self, proc, predicate, phase):
        deadline = self.deadline
        while time.monotonic() < deadline:
            shot = self.snapshot()
            if predicate(shot):
                return shot
            if proc.poll() is not None:
                record = next(r for r in self.commands if r["pid"]==proc.pid)
                label = record["label"]
                raise AssertionError(f"{label} ended {proc.returncode} before {phase}: " +
                    (self.work / (label+".err")).read_text()[-400:] + " " + str({r:(v["status"],v["rounds"],v["held"]) for r,v in shot.items()}))
            time.sleep(.2)
        raise TimeoutError(phase)

    def reap(self, proc, entry, expected):
        code = proc.wait(timeout=max(1,self.deadline-time.monotonic()))
        entry.update(exit=code, ended=utc(), requests_after=self.server.request_count)
        check(code==expected, f'{entry["label"]} exited {code}: ' + (self.work/(entry["label"]+".err")).read_text()[-700:])

    def close(self):
        for proc in self.owned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def hot_job_and_clear(s):
    return s["rounds"]==1 and not s["held"] and any(j["status"]=="RUNNING" and j["scheduler_ref"] for j in s["jobs"])


def run(work, args):
    stage, digest = make_install(work,args)
    for sub in ("tmp",):
        (work/sub).mkdir()
    started = time.monotonic()
    info = {"started_utc":utc(), "stage_sha256":digest, "product_head":subprocess.check_output(
        ["git", "rev-parse", args.revision or "HEAD"], cwd=BASE,text=True).strip(),
        "product_source":"git archive" if args.revision else "isolated working-tree copy", "rounds_per_run":args.rounds,
        "worker_response_delay_seconds":args.latency,"lease_seconds":args.lease,"result":"FAIL"}
    for key in ("A","B"):
        save(work / f"{key}.json",project_spec(key,args.rounds))
    e2e_responses.fixture_action = timed_provider(args.latency)
    try:
        with e2e_responses.local_responses(work/"requests.jsonl") as server:
            env = fixture_env(work,server,str(CODEX))
            env.update(PATH=str(BASE/".venv/bin")+":"+env["PATH"], TMPDIR=str(work/"tmp"),
                       GX_METRICS_FILE=str(work/"metrics.jsonl"))
            info["codex"] = subprocess.check_output([str(CODEX),"--version"],env=env,text=True).strip()
            check(info["codex"]=="codex-cli 0.169.0","official package version")
            site = Staging(work,stage,server,env,started+args.timeout)
            info["commands"] = site.commands
            try:
                for key in ("A","B"):
                    site.command("start-"+key,"start","--run-key",key,"--spec",work/(key+".json"))
                before = site.snapshot()
                a,b = before
                check(len(before[a]["jobs"])==len(before[b]["jobs"])==2, "both starts must launch both local jobs")
                info["run_ids"] = {"A":a,"B":b}
                site.command("west-pause","team","--run-id",a,"--team-id","west","--operation","pause")
                faulted, entry = site.command("workspace-first","run","--timeout",args.timeout,background=True)
                pre = site.until(faulted, lambda x:hot_job_and_clear(x[b]), "B first successor Job, after receipt")
                check(pre[a]["worker_turns"]==0 and pre[b]["submissions"]==2,"paused team issued work or peer failed to advance")
                kill_time = time.monotonic()
                faulted.kill()
                site.reap(faulted,entry,-9)
                after_kill = site.snapshot()
                info["coordinator_crash"]={"signal":"SIGKILL","at":utc(),"scope":"only owned workspace CLI PID",
                    "before":pre,"after_process_exit":after_kill,"seconds_to_exit":round(time.monotonic()-kill_time,4)}
                save(work/"crash.json",info["coordinator_crash"])
                site.command("west-resume","team","--run-id",a,"--team-id","west","--operation","resume")
                fresh, entry = site.command("workspace-cold","run","--timeout",args.timeout,background=True)
                recovered = site.until(fresh,lambda x:x[b]["rounds"]>=2, "first post-crash decision")
                info["coordinator_crash"]["seconds_to_next_decision"] = round(time.monotonic()-kill_time,3)
                old_jobs = {(j["job_id"],j["scheduler_ref"],j["idempotency_key"]) for s in pre.values() for j in s["jobs"]}
                rec_jobs = {(j["job_id"],j["scheduler_ref"],j["idempotency_key"]) for s in recovered.values() for j in s["jobs"]}
                check(old_jobs <= rec_jobs,"recovery replaced a prior job, operation key or scheduler receipt")
                # Stop after GX accepted the lagging team's product, while its official receipt remains in flight.
                terminal = {"SUCCEEDED","COMPLETED"}
                rid = a
                boundary = site.until(fresh,lambda x:(x[rid]["rounds"] >= args.rounds-2 and
                    x[rid]["status"] not in terminal and x[rid]["accepted_awaiting_final"] == x[rid]["held"] == 1),
                    "A accepted a product and its official final receipt is still in flight")
                t0 = time.monotonic()
                site.command("target-stop","stop","--run-id",rid)
                acknowledged = site.snapshot()
                info["pause_ack"] = {"seconds":round(time.monotonic()-t0,3),"status":acknowledged[rid]["status"],"held":acknowledged[rid]["held"]}
                site.reap(fresh,entry,0)
                stopped = site.snapshot()
                check(stopped[rid]["status"]=="PAUSED" and not stopped[rid]["held"],"public stop did not drain admitted turn")
                info["operator_stop"] = {"target":rid,"at":utc(),"before":boundary, "state":stopped,"seconds_to_workspace_exit":round(time.monotonic()-t0,3)}
                last, entry = site.command("target-resume","resume","--run-id",rid,"--timeout",args.timeout,background=True)
                site.reap(last,entry,0)
                finished = site.snapshot()
                check(all(x["status"] in terminal and x["rounds"]==args.rounds for x in finished.values()),"did not reach both round budgets")
                from v21_long_audit import audit
                info["audit"] = audit(site,server.rows,args.rounds)
                if args.rounds >= 18:
                    check(all(r["longest_owned_submission_seconds"] > args.lease and r["official_thread_renewals"] > 0
                              for r in info["audit"]["runs"].values()),"short test lease must be exceeded by both live teams")
                before_replay = site.snapshot()
                count = server.request_count
                site.command("terminal-cold","run","--timeout",30)
                check(site.snapshot()==before_replay and server.request_count==count,"terminal replay changed paid calls, jobs or submissions")
                from v21_long_audit import business_hashes
                with closing(site.db()) as conn:
                    check(business_hashes(conn)==info["audit"]["original_business_hashes"],"terminal replay rewrote durable business state")
                info["terminal_replay"] = {"new_model_requests":server.request_count-count,"workspace_scientific_snapshot_equal":True,
                                           "all_business_hashes_equal":True}
                info["result"]="PASS"
            finally:
                site.close()
    except Exception as exc:
        info["error"]=f"{type(exc).__name__}: {exc}"
        raise
    finally:
        info.update(ended_utc=utc(),wall_seconds=round(time.monotonic()-started,3))
        save(work/"summary.json",info)
    print(json.dumps({"result":info["result"],"work":str(work),"wall_seconds":info["wall_seconds"]}),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Stage this exact committed product revision with git archive")
    parser.add_argument("--rounds",type=int,default=18)
    parser.add_argument("--latency",type=float,default=3.4)
    parser.add_argument("--lease",type=int,default=16)
    parser.add_argument("--timeout",type=float,default=1500)
    args=parser.parse_args()
    check(3<=args.rounds<=23 and 0<=args.latency<=8 and 3<=args.lease<=3600 and 0<args.timeout<=7200,
          "use 3–23 rounds, latency 0–8 seconds, a 3–3600 second lease, timeout at most 2 hours")
    parent=BASE/"var/v21-long-run"
    parent.mkdir(exist_ok=True,parents=True)
    work=Path(tempfile.mkdtemp(dir=parent,prefix="cohort-"))
    print(str(work),flush=True)
    run(work,args)
