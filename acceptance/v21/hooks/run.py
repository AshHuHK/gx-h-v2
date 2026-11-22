"""Install an independent workflow into a clean GX deployment, execute it over the public CLI, audit its receipts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[2]
sys.path.insert(0, str(PACKAGE / "acceptance"))
sys.path.insert(0, str(PACKAGE / "gx-h"))
from e2e_grid import fixture_env
from e2e_responses import brief_from, local_responses, output_json


def put(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def code_sha(directory):
    sha = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        sha.update(str(path.relative_to(directory)).encode() + b"\0" + path.read_bytes())
    return sha.hexdigest()


def action(body, ident):
    brief = brief_from(body)
    info = {"task_id": brief["task_id"], "offered": brief["gx_tools"], "minimum": brief["input"]["minimum"]}
    calls = {x["call_id"]: x["name"] for x in body["input"] if x.get("type") == "function_call"}
    answers = {calls[x["call_id"]]: output_json(x["output"]) for x in body["input"]
               if x.get("type") == "function_call_output" and x["call_id"] in calls}
    if "gx_task_context" not in answers:
        name, args = "gx_task_context", {}
    elif "gx_sample_count" not in answers:
        assert answers["gx_task_context"]["instrument_units"] == brief["workflow_context"]["instrument_units"] == "readings"
        name, args = "gx_sample_count", {}
    elif "gx_submit_product" not in answers:
        name, args = "gx_submit_product", {"product": answers["gx_sample_count"]}
    else:
        return {"type": "message", "id":"m-"+ident, "role":"assistant",
                "content":[{"type":"output_text","text":"The measured sample count was submitted."}]}, info
    return {"type":"function_call","call_id":"call-"+ident,"name":name,"arguments":json.dumps(args)}, {**info,"tool":name}


def verify(old_workspace, out, work):
    deploy = work / "deploy"
    harness = deploy / "gx-h"
    shutil.copytree(PACKAGE / "gx-h", harness, ignore=shutil.ignore_patterns("__pycache__", "var", "*.pyc"))
    (deploy / "bin").mkdir()
    shutil.copy(PACKAGE / "bin/gx-p0", deploy / "bin/gx-p0")
    before = code_sha(harness / "gx_core")
    example = harness / "external_calibration.py"
    shutil.copy(HERE / "calibration_package.py", example)
    config = harness / "config"
    installed = json.loads((config / "workflow_packages.json").read_text())
    installed["packages"].append({"module": "external_calibration"})
    put(config / "workflow_packages.json", installed)
    script = "from gx_core.orchestration.service import default_registry; print(default_registry().get('calibration','1').digest())"
    pin = subprocess.check_output([sys.executable, "-B", "-c", script], cwd=harness, text=True).strip()
    default_pin = subprocess.check_output([sys.executable, "-B", "-c", script.replace("'calibration'", "'calibration_note'")],
                                          cwd=harness, text=True).strip()
    recorded_child = subprocess.check_output([sys.executable, "-B", "-c", script.replace(".digest()", ".document['calibration_note']['sha256']")],
                                             cwd=harness, text=True).strip()
    assert recorded_child == default_pin
    pinned = json.loads((config / "shipped.json").read_text())
    pinned["workflows"]["calibration@1"] = pin
    pinned["workflows"]["calibration_note@1"] = default_pin
    put(config / "shipped.json", pinned)
    after = code_sha(harness / "gx_core")
    assert before == after
    commands = []
    with patch("e2e_responses.fixture_action", action), local_responses(work / "requests.jsonl") as server:
        codex = os.environ.get("GX_P0_CODEX") or str(PACKAGE / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
        env = fixture_env(work, server, codex)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        state = work / "state"

        def cli(command, *args, okay=True, directory=state):
            p = subprocess.run([sys.executable,str(deploy / "bin/gx-p0"),command,"--state",str(directory),*args],
                               cwd=deploy, env=env, capture_output=True, timeout=100, text=True)
            commands.append({"command":command, "exit":p.returncode})
            if bool(p.returncode) == okay:
                raise AssertionError((command,p.returncode,p.stdout[-1600:],p.stderr[-1600:]))
            try:
                return json.loads(p.stdout or p.stderr)
            except ValueError:
                if okay:
                    raise
                return {"error": (p.stdout or p.stderr).strip()}

        def edit(name, kind, version, value):
            return {"item_id":name,"item_type":kind,"expected_version":version,"value":value}

        def message(data, okay=True):
            file = work / "action.json"
            put(file, data)
            return cli("message", "--action", str(file), okay=okay)

        spec = {"workflow":"calibration","workflow_version":"1","project":"hook-calibration",
                "objective":"How many synthetic reference readings were observed?", "max_rounds":1,
                "intent_items":[edit("evaluation","evaluation_spec",0,{"threshold":-0.25}),
                                edit("sampling","priority",0,{"minimum":3})]}
        specfile = work / "project.json"
        put(specfile, spec)
        started = cli("start","--run-key","calibration","--spec",str(specfile))
        assert len(started["jobs"]) == 1 and started["jobs"][0]["status"] == "SUCCEEDED", started
        cli("stop", "--run-id", started["run_id"])
        wrong = message({"kind":"change","items":[edit("sampling","priority",1,{"minimum":0})]}, okay=False)
        criterion = message({"kind":"change","items":[edit("evaluation","evaluation_spec",1,{"threshold":1.0})]})["result"]
        if criterion["status"] == "awaiting_confirmation":
            confirmation = {"kind":"confirm","change_id":criterion["change_id"],"preview_sha256":criterion["preview_sha256"]}
            if criterion.get("required_acknowledgement"):
                confirmation["acknowledge_consequences"] = criterion["required_acknowledgement"]
            assert message(confirmation)["result"]["status"] == "committed"
        final = cli("resume","--run-id",started["run_id"],"--timeout","70")
        if final["status"] != "COMPLETED":
            raise AssertionError((final["status"],final.get("pause"),final.get("pending_deliveries"),final["tasks"]))
        with sqlite3.connect(state / "gx.db") as conn:
            conn.row_factory = sqlite3.Row
            row = lambda sql: [dict(item) for item in conn.execute(sql)]
            requests = [json.loads(line) for line in (work / "requests.jsonl").read_text().splitlines()]
            calls = {"call-"+request["id"]: request["tool"] for request in requests if "tool" in request}
            receipts = [{**entry, "tool": calls[entry["call_id"]]} for entry in
                        row("SELECT call_id,thread_id,turn_id,result_json FROM p0_root_calls")]
            jobs = row("SELECT job_id,status,capability_version FROM jobs")
            evaluations = row("SELECT eval_id,verdict,metrics_json,provenance_json FROM evaluations WHERE purpose='CANDIDATE'")
            replans = row("SELECT event_id,payload_json FROM events WHERE kind='calibration.resume'")
            tasks = row("SELECT task_id,status,input_json FROM tasks")
            assert replans and len(jobs) == 1 and len(evaluations) == 2 and evaluations[0]["verdict"] != evaluations[1]["verdict"]
            assert len(tasks) == 1 and tasks[0]["status"] == "ACCEPTED" and json.loads(tasks[0]["input_json"])["minimum"] == 3
            assert any(x["tool"] == "gx_sample_count" and json.loads(x["result_json"])["count"] == 3 for x in receipts)
            assert any(x["tool"] == "gx_task_context" and json.loads(x["result_json"])["instrument_units"] == "readings" for x in receipts)
            assert "positive minimum" in json.dumps(wrong)
            assert "p0.intent.reevaluate" in {r[0] for r in conn.execute("SELECT topic FROM outbox WHERE consumed_at IS NOT NULL")}
        put(out / "run.json", {"workflow":"calibration@1","workflow_digest":pin,"core_before_install":before,
            "core_after_install":after, "commands":commands, "final":final["status"], "run_id":started["run_id"],
            "tasks":tasks,"jobs":jobs,"evaluations":evaluations,"replan":replans,"root_calls":receipts,"invalid_intent":wrong})

        defaults_spec = {**spec, "workflow":"calibration_note","project":"no-hooks", "intent_items":[
            edit("evaluation", "evaluation_spec", 0, {"units":"unregistered evaluation"})]}
        put(specfile, defaults_spec)
        default_refusal = cli("start","--run-key","defaults","--spec",str(specfile),okay=False,directory=work/"no-evaluator")
        assert "frozen evaluation reference or a numeric threshold" in json.dumps(default_refusal), default_refusal
        put(specfile, {**defaults_spec,"intent_items":[edit("evaluation","evaluation_spec",0,{"threshold":0.0})]})
        default_ok = cli("start","--run-key","defaults","--spec",str(specfile),directory=work/"default-ok")
        assert default_ok["status"] == "COMPLETED" and not default_ok["jobs"]
        # A new request on the same original inputs is denied by the installed prepare_job method.
        tested = subprocess.check_output([sys.executable,"-B","-c", """
import json, sys
from gx_core.persistence import store
from gx_core.jobs.service import JobService
from gx_core.errors import SchemaInvalid
db = store.open_store(sys.argv[1])
first = db.execute("SELECT * FROM jobs").fetchone()
proposal = json.loads(first["submit_intent_json"])["request"]
request = {key:proposal[key] for key in ("run_id","capability_version","purpose","input_manifest_ref","parameters","binding")}
service = JobService(db)
previous = service.submit(**request,idempotency_key="grid:calibration")
assert previous["replayed"] and previous["job_id"]==first["job_id"]
try:
    service.submit(**{**request,"parameters":{"operation":"unrequested"}},idempotency_key="other-measurement")
except SchemaInvalid as exc:
    assert "Only this calibration measurement" in str(exc)
    print(json.dumps({"replayed": previous["job_id"],"refused":str(exc),"jobs":db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]}))
else:
    raise AssertionError("The unrequested measurement was accepted")
""",str(state/"gx.db")],cwd=harness,env=env,text=True)
        # Editing only a package implementation changes its graph digest even at the identical ID/version.
        example.write_text(example.read_text()+"\n# Modified installation verification.\n")
        changed = cli("start","--run-key","changed","--spec",str(specfile),okay=False,directory=work/"modified")
        assert "changed since it was pinned" in json.dumps(changed), changed
        shutil.copy(work / "requests.jsonl", out / "requests.jsonl")
        put(out/"defaults-and-refusals.json",{"default_workflow":"calibration_note@1","default_digest":default_pin,
            "child_digest_recorded_by_parent":recorded_child,
            "completed":default_ok["run_id"], "unknown_evaluation":default_refusal,
            "job":json.loads(tested),"changed_implementation":changed})

    # D08 inspects a real v2 workspace through a private database copy. The source can be read-only.
    if old_workspace:
        src = old_workspace / "gx.db"
        old_hash = hashlib.sha256(src.read_bytes()).hexdigest()
        copy = work / "copied-v2.db"
        with sqlite3.connect(src.resolve().as_uri()+"?mode=ro", uri=True) as original, sqlite3.connect(copy) as target:
            original.backup(target)
        with sqlite3.connect(copy) as target:
            target.row_factory = sqlite3.Row
            from gx_core.orchestration import service
            from gx_core.persistence import store
            from gx_core.p0 import schema
            old = target.execute("SELECT * FROM runs WHERE json_extract(inputs_json,'$.inputs.p0')=1 LIMIT 1").fetchone()
            def evidence():
                return {name: [tuple(r) for r in target.execute(sql,(old["run_id"],))] for name,sql in {
                    "jobs":"SELECT job_id,status FROM jobs WHERE run_id=? ORDER BY rowid",
                    "decisions":"SELECT round_index,decision_ref FROM science_decisions WHERE run_id=? ORDER BY round_index",
                    "charges":"SELECT reservation_ref,status,amounts_json FROM budget_reservations WHERE budget_ref IN (SELECT budget_ref FROM runs WHERE run_id=?) ORDER BY rowid"}.items()}
            prior = evidence()
            schema.install(target)
            flow = service._pinned_workflow(target, old)
            service.require_pins(old, flow)
            assert service.hooks_for_run(target, old["run_id"]) == flow.hooks
            assert evidence() == prior and old_hash == hashlib.sha256(src.read_bytes()).hexdigest()
            ckpt = store.latest_checkpoint(target, old["run_id"])
            put(out/"old-pin.json",{"workspace":old_workspace.name,"source_database_sha256":old_hash,"run_id":old["run_id"],
                "status":old["status"],"workflow_id":old["workflow_id"],"workflow_version":old["workflow_version"],
                "saved_pin":old["workflow_sha256"],"installed_pin":flow.digest(),"checkpoint":ckpt["checkpoint_id"],
                "node":ckpt["node"],"identical_rows":{k:len(v) for k,v in prior.items()},"read_only_source":True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-workspace", type=Path)
    parser.add_argument("--output",type=Path,default=HERE)
    parser.add_argument("--work",type=Path)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=True,parents=True)
    if args.work:
        args.work.mkdir(exist_ok=False,parents=True)
        verify(args.old_workspace,args.output,args.work)
    else:
        with tempfile.TemporaryDirectory(prefix="gx-hooks-") as temporary:
            verify(args.old_workspace,args.output,Path(temporary))
