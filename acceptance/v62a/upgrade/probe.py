"""Restore a public CSV Goal and two paused static Runs at the original physical paths."""
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
import time

ROOT = Path(__file__).resolve().parents[3]
V6 = "47fc728d32bc10ba478df6a063529a8d98f98828"
V61 = "c4c5b66a55739b38b34684360810d05f18f9a21a"
PUBLIC_PACKAGE = "org.example.gx.sensor-calibration@1.0.0"


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_output(argv, **options):
    return subprocess.check_output(list(map(str, argv)), text=True, **options).strip()


def source_commit(repo):
    return check_output(["git", "-C", repo, "rev-parse", "HEAD"])


def install(source, dest):
    dest.mkdir()
    for name in ("gx-h", "bin"):
        shutil.copytree(source / name, dest / name,
                        ignore=shutil.ignore_patterns("tests", "var", "__pycache__", "*.pyc", ".venv"))
    assert not (dest / ".git").exists()


def released(repo, dest, commit):
    dest.mkdir()
    with tempfile.TemporaryFile() as tmp:
        subprocess.run(["git", "-C", repo, "archive", commit, "bin", "gx-h"], check=True, stdout=tmp)
        tmp.seek(0)
        with tarfile.open(fileobj=tmp) as tar:
            tar.extractall(dest, filter="data")
    modules = Path("gx-h/runtime/plugin-host/node_modules")
    shutil.copytree(repo / modules, dest / modules)


def cli(deploy, work, state, action, *args, expected=(0,)):
    local_env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if k in os.environ}
    for key, value in {"http_proxy":"http://127.0.0.1:7890", "https_proxy":"http://127.0.0.1:7890",
                       "all_proxy":"socks5h://127.0.0.1:7891", "no_proxy":"localhost,127.0.0.1,::1"}.items():
        local_env[key] = local_env[key.upper()] = value
    local_env.update(HOME=str(work), CODEX_HOME=str(work/"codex"), GX_CODEX_OFFLINE="1",
                     PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-B", str(deploy / "bin/gx-p0"), action]
    if state is not None:
        argv.extend(["--state", str(state)])
    argv.extend(map(str, args))
    proc = subprocess.run(argv, cwd=work, env=local_env, capture_output=True, text=True, timeout=60)
    if proc.returncode not in expected:
        raise RuntimeError(f"{action} returned {proc.returncode}: {(proc.stderr or proc.stdout)[-1400:]}")
    with (work/"commands.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv":argv,"exit":proc.returncode,
                                 "output_sha256":hashlib.sha256(proc.stdout.encode()).hexdigest()})+"\n")
    return json.loads(proc.stdout)


def snapshot(state):
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        def query(sql):
            return [dict(row) for row in db.execute(sql)]
        artifacts = query("SELECT artifact_ref,sha256,namespace,rel_path,job_id FROM artifacts ORDER BY artifact_ref")
        for a in artifacts:
            assert digest(state/"artifacts"/a["namespace"]/a["rel_path"]) == a["sha256"]
        run, = query("SELECT run_id,status,inputs_json,contract_sha256,workflow_id,workflow_version,workflow_sha256,"
                     "agent_policy_sha256 FROM runs")
        inputs = json.loads(run.pop("inputs_json")).get("inputs", {})
        run["capability_pins"] = inputs.get("capability_catalog")
        return {"schema":dict(db.execute("SELECT key,value FROM schema_meta WHERE key IN "
                                        "('db_schema_sha256','contract_sha256')")),
                "run":run,"marker_sha256":digest(state/".gx-native-state.json"),
                "graph":query("SELECT version,sha256,decision_ref FROM v6_graph_versions ORDER BY version"),
                "tasks":query("SELECT task_id,kind,status,attempts_consumed FROM tasks ORDER BY task_id"),
                "jobs":query("SELECT job_id,capability_id,idempotency_key,reservation_ref,status,measured_json "
                             "FROM jobs ORDER BY job_id"),
                "outputs":[{"ref":a["artifact_ref"],"sha256":a["sha256"],"job_id":a["job_id"]} for a in artifacts],
                "turns":query("SELECT thread_id,turn_id,settled_tokens,confidence,budget_status "
                              "FROM p0_meter_native_turns ORDER BY thread_id,turn_id"),
                "budgets":query("SELECT budget_ref,spent_json,reserved_json FROM budgets ORDER BY budget_ref"),
                "receipts":query("SELECT task_id,thread_id,turn_id,call_id,decision_ref FROM v6_decisions ORDER BY rowid"),
                "resync":[json.loads(row[0]) for row in db.execute("SELECT payload_json FROM events WHERE "
                                                                  "kind='store.contract_resynced' ORDER BY event_id")],
                "executions":{p.parent.name:digest(p) for p in sorted((state/"jobs").glob("*/outcome.json"))}}


def backup(state, target):
    shutil.copytree(state, target, ignore=shutil.ignore_patterns("gx.db*", ".native.lock", ".gx-driver-wake"))
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro", uri=True)) as src, \
         closing(sqlite3.connect(target/"gx.db")) as dst:
        src.backup(dst)


def static_seed(deploy, work, state, spec):
    opened = cli(deploy, work, state, "start", "--spec", spec, "--run-key", state.name)
    cli(deploy, work, state, "stop", "--run-id", opened["run_id"])
    assert len(opened["jobs"]) == 2
    until = time.monotonic()+30
    while not all((state/"jobs"/r["job_id"]/"outcome.json").is_file() for r in opened["jobs"]):
        if time.monotonic() > until:
            raise TimeoutError("old public CPU Jobs did not terminate")
        time.sleep(.05)


def quick_fixture(source, peer_checkout, binary, work, out):
    work.mkdir()
    out.mkdir()
    # Use the V6.1 scripted peer for both releases. A new tool or an injected
    # measurement in a quick brief fails the released assertions.
    runner = """from pathlib import Path
import json, sys
peer, source, work, out, binary = map(Path, sys.argv[1:])
sys.path.insert(0, str(peer / 'acceptance/v61'))
import csv_loopback as fixture
fixture.ROOT = source
fixture.PACKAGE = source / 'examples/plugins/sensor_calibration'
fixture.EXAMPLE = source / 'examples/v61-sensor-calibration'
result = fixture.run(work, out, binary)
(out / 'result.json').write_text(json.dumps(result, indent=2) + chr(10))
"""
    subprocess.run([sys.executable, "-B", "-c", runner, str(peer_checkout), str(source), str(work),
                    str(out), str(binary)], cwd=peer_checkout, check=True)
    return load(out/"result.json")


def seed(baseline, binary, work):
    assert source_commit(baseline) == V61
    stable_paths = ["gx-h", "bin", "examples", "acceptance/v61/csv_loopback.py"]
    assert not check_output(["git", "-C", baseline, "status", "--porcelain", "--", *stable_paths])
    work.mkdir(mode=0o700, parents=True)
    old_csv, csv_out = work/"quick-fixture", work/"csv-loopback"
    assert quick_fixture(baseline, baseline, binary, old_csv, csv_out)["paid_requests"] == 0
    spec = load(baseline/"examples/paired-grid.json")
    spec.update(workflow="science_grid",workflow_version="3",plugins=["org.gx.science-grid@3.0.0"])
    save(work/"grid.json",spec)
    deploy = old_csv/"release"
    static_seed(deploy, work, work/"static", work/"grid.json")
    # Reproduce the two real migration transactions at identical filesystem paths.
    deploy.rename(work/"released61")
    released(baseline,deploy,V6)
    static_seed(deploy, work, work/"multi-hop", work/"grid.json")
    deploy.rename(work/"released60")
    (work/"released61").rename(deploy)
    upgraded = cli(deploy,work,work/"multi-hop","resume","--timeout","1", expected=(75,))
    assert upgraded["status"] in ("ACTIVE","WAITING_CLIENT")
    cli(deploy,work,work/"multi-hop","stop")
    seed_records = {}
    for name,state in states(work).items():
        backup(state,work/("before-"+name))
        seed_records[name] = snapshot(state)
        assert seed_records[name] == snapshot(work/("before-"+name))
    shutil.copytree(old_csv/"codex",work/"before-loopback-home")
    save(work/"seed.json",{"source":V61,"historical_static_source":V6,"states":seed_records,
                           "binary_sha256":digest(binary),"old_csv":load(csv_out/"result.json")["independent_check"]})


def states(work):
    return {"quick":work/"quick-fixture/state","static":work/"static","multi-hop":work/"multi-hop"}


def module_count(state):
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro", uri=True)) as db:
        return db.execute("SELECT count(*) FROM v62_modules").fetchone()[0]


def pinned_facts(record):
    return {k:v for k,v in record["run"].items() if k != "status"}


def closed_science(record):
    return {k:v for k,v in record.items() if k not in {"schema","resync"}}


def check(candidate, baseline, binary, work, out):
    seed_record = load(work/"seed.json")
    deploy = work/"quick-fixture/release"
    trial = Path(tempfile.mkdtemp(prefix="trial-", dir=work))
    deployed = trial/"candidate-install"
    old = trial/"v61-install"
    original_stamp = seed_record["states"]["quick"]["schema"]["contract_sha256"]
    target_stamp = load(candidate/"gx-h/contracts/CONTRACT_SHA")["__combined__"]
    assert original_stamp != target_stamp, "This probe requires a candidate with its new formal contract"
    new_quick = quick_fixture(candidate, baseline, binary, trial/"new-quick", trial/"new-quick-raw")
    old_catalogs = [r["catalog"] for r in load(work/"csv-loopback/model-inputs.json")]
    new_catalogs = [r["catalog"] for r in load(trial/"new-quick-raw/model-inputs.json")]
    assert old_catalogs == new_catalogs and all("gx_open_module" not in tools for tools in new_catalogs)
    assert module_count(trial/"new-quick/state") == 0
    install(candidate, deployed)
    package = candidate / "examples/plugins/sensor_calibration"
    pin = load(candidate/"examples/v61-sensor-calibration/package-pin.json")
    cli(deployed, work, None, "plugin", "install", package, "--sha256", pin["sha256"])
    deploy.rename(old)
    deployed.rename(deploy)
    cases = {}
    try:
        for name,state in states(work).items():
            before = seed_record["states"][name]
            assert snapshot(state) == before
            quick = name == "quick"
            snapshots = []
            for _ in range(2):
                cli(deploy,work,state,"resume","--timeout","1",expected=(0,) if quick else (75,))
                cli(deploy,work,state,"status")
                after = snapshot(state)
                assert after["schema"]["contract_sha256"] == target_stamp and pinned_facts(after) == pinned_facts(before)
                assert before["marker_sha256"] == after["marker_sha256"]
                assert after["executions"] == before["executions"]
                assert after["turns"] == before["turns"] and after["receipts"] == before["receipts"]
                if quick:
                    assert closed_science(after) == closed_science(before)
                    assert len(after["jobs"])==1 and len(after["turns"])==2
                else:
                    assert all(j["status"] == "SUCCEEDED" for j in after["jobs"])
                    assert [{k:j[k] for k in ("job_id","idempotency_key","reservation_ref")} for j in after["jobs"]] == [
                        {k:j[k] for k in ("job_id","idempotency_key","reservation_ref")} for j in before["jobs"]]
                    assert any(t["kind"]=="research_round" and t["status"]=="READY" for t in after["tasks"])
                assert module_count(state) == 0
                snapshots.append(after)
            assert snapshots[0] == snapshots[1]
            assert any(e.get("from_sha256")==original_stamp and e.get("to_sha256")==target_stamp for e in after["resync"])
            cases[name] = {"before":before, "after":snapshots[1],"equal_cold_open_snapshots":True,
                           "additional_jobs":0,"additional_native_turns":0,"additional_token_charges":0}
        verify = cli(deploy,work,None,"plugin","verify",PUBLIC_PACKAGE)
        assert verify["failures"] == {} and verify["plugins"][0]["sha256"] == pin["sha256"]
    finally:
        deploy.rename(deployed)
        old.rename(deploy)
        for name,state in states(work).items():
            state.rename(trial/("after-"+name))
            backup(work/("before-"+name),state)
        operator = work/"quick-fixture/codex"
        operator.rename(trial/"after-loopback-home")
        shutil.copytree(work/"before-loopback-home",operator)
    for name,state in states(work).items():
        cli(deploy,work,state,"status")
        assert snapshot(state) == cases[name]["before"]
        cases[name]["full_rollback_matches"]=True
    tracked = check_output(["git","-C",candidate,"status","--porcelain","--","gx-h","bin"])
    result = {"case":"A7-upgrade", "passed":True, "baseline":V61, "prebaseline":V6,
              "candidate_git_head":source_commit(candidate), "candidate_code_changes_at_execution":tracked.splitlines(),
              "source":"public synthetic science_grid and paired CSV", "old_contract":original_stamp,
              "new_contract":target_stamp, "plugin":pin, "host_binary_sha256":seed_record["binary_sha256"],
              "paid_model_calls":0, "provider_tokens":None,"old_quick_loopback_tokens":70,
              "new_quick":{"run_id":new_quick["run_id"],"graph":new_quick["graph_head"],"jobs":len(new_quick["jobs"]),
                           "decisions":len(new_quick["decisions"]),"real_cpu":new_quick["resources"]["measured_cpu"],
                           "cold_extra_jobs":new_quick["cold_extra_jobs"],"cold_extra_requests":new_quick["cold_extra_requests"],
                           "paid_model_calls":new_quick["paid_requests"],"loopback_fixture_tokens":70,
                           "wire_tools_by_turn":new_catalogs,"module_instances":0},
              "module_instances_on_each_migrated_run":0, "csv_independent_check":seed_record["old_csv"], "cases":cases}
    out.parent.mkdir(exist_ok=True,parents=True)
    save(out,result)
    return result


if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase",choices=["seed","check","all"])
    ap.add_argument("--baseline",type=Path,required=True)
    ap.add_argument("--binary",type=Path,required=True)
    ap.add_argument("--work",type=Path,required=True)
    ap.add_argument("--receipt",type=Path,required=True)
    args=ap.parse_args()
    work=args.work.resolve()
    base=args.baseline.resolve()
    if work.is_relative_to(ROOT) or work.is_relative_to(base):
        ap.error("use a fresh external work directory for test installs and stores")
    if args.phase in {"seed","all"}:
        seed(base,args.binary.resolve(),work)
    if args.phase in {"check","all"}:
        check(ROOT,base,args.binary.resolve(),work,args.receipt.resolve())
