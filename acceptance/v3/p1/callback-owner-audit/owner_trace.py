"""One offline process for forming or tracing an exact native callback snapshot."""
from collections import Counter
from contextlib import closing
import cProfile
import hashlib
import json
import os
from pathlib import Path
import pstats
import re
import shutil
import sqlite3
import sys
import time

mode, release, work = sys.argv[1:4]
release, work = Path(release), Path(work)
sys.path.insert(0, str(release / "gx-h"))

from examples.science_judgment import judge
from gx_core.artifacts import ArtifactService
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import service, reactor
from gx_core.p0 import cli, roots
from gx_core.persistence import store
from gx_core.teams.service import backend_ctx


def payload(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def owner(conn, rid):
    tools = service.hooks_for_run(conn, rid).build_worker_tools(conn, rid)
    kw = {"worker_prepare": tools.prepare} if hasattr(tools, "prepare") else {}
    return roots.Roots(conn, run_id=rid, cwd=work, worker_tools=tools.specs, worker_call=tools.call,
                       token_budget=180000, **kw)


def call(root, message):
    reply = root._callback(ReceivedMessage(1, message))
    assert reply["success"], reply
    return json.loads(reply["contentItems"][0]["text"])


def prepare():
    spec = json.loads((work/"spec.json").read_text())
    spec["plugins"] = ["org.gx.science-grid@2.0.0"]
    with native_state(work/"state") as state, closing(store.open_store(state.db_path)) as db:
        rid = cli.start(db, state.directory, spec, run_key="callback-trace")["run_id"]
        js = JobService(db)
        for (jid,) in db.execute("SELECT job_id FROM jobs").fetchall():
            assert js.wait(jid, 30)["terminal"]
        info = {"run_id": rid}
        for role in ("evidence_reviewer", "planner"):
            ctx = backend_ctx(db)
            reactor.lazy(ctx, rid)
            reactor.drive_science(ctx, rid)
            task = db.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY' AND role=?", (rid,role)).fetchone()
            assert task, role
            # The same saved Root must be executable by both releases. Give its
            # assignment the common native tools. Its P1 workflow context, exact
            # product validator, Run receipt and surrounding evidence remain live.
            root = roots.Roots(db, run_id=rid, cwd=work, token_budget=spec["turn_token_budget"])
            row = root._claim(task["task_id"])
            tid = "trace-" + role
            root._bind(row, {"id":tid,"sessionId":tid,"cwd":row["cwd"],"source":"appServer"})
            turn = "trace-turn-" + role
            root.active_turns[tid] = turn
            inputs = json.loads(row["brief"].split("\n",1)[1])["input"]
            product = judge(ArtifactService(db), inputs)
            messages = {}
            for tool, args in (("gx_submit_product",{"product":product}), ("gx_read_artifact",{"ref":inputs["evidence_refs"][0]})):
                messages[tool] = {"method":"item/tool/call","params":{"threadId":tid,"turnId":turn,
                    "callId":"trace-call-" + tool, "tool":tool,"arguments":args}}
            image = work/role
            image.mkdir()
            shutil.copytree(state.directory/"artifacts", image/"artifacts")
            with closing(sqlite3.connect(image/"gx.db")) as snapshot:
                db.backup(snapshot)
            (work/(role+".json")).write_bytes(payload(messages))
            info[role] = {"task":task["task_id"], "run":rid, "role":role, "lease":row["lease_id"],
                          "root":row["root_key"], "input_kind":inputs["output_kind"], "thread":tid,"turn":turn,
                          "initial_brief_bytes":len(row["brief"].encode()), "tool_names":json.loads(row["tool_names_json"]),
                          "db_sha256":digest((image/"gx.db").read_bytes()),
                          "envelopes":{t:{"bytes":len(payload(m)),"sha256":digest(payload(m))} for t,m in messages.items()},
                          "artifact_set_sha256":digest(payload(sorted((str(f.relative_to(image)),digest(f.read_bytes()))
                            for f in (image/"artifacts").rglob("*") if f.is_file())))}
            assert call(root,messages["gx_submit_product"])["task_status"] == "ACCEPTED"
        info["jobs"] = [dict(r) for r in db.execute("SELECT status,job_attempt FROM jobs")]
        assert len(info["jobs"]) == 2 and all(row["status"] == "SUCCEEDED" for row in info["jobs"])
        assert db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0] == 0
        (work/"fixtures.json").write_bytes(payload(info))


SQL_STRING = re.compile(r"'(?:''|[^'])*'")
NUMBERS = re.compile(r'\b\d+(?:\.\d+)?\b')


def sql_shape(statement):
    return " ".join(NUMBERS.sub("?", SQL_STRING.sub("?", statement)).split())


def source_name(name):
    if name.startswith(str(release) + "/"):
        return name[len(str(release))+1:]
    return name


def trace_group(path, flags):
    if not isinstance(path, (str,bytes)):
        return "file descriptor"
    name = os.fsdecode(path)
    if name.startswith(str(release) + "/"):
        return source_name(name)
    if "/artifacts/" in name:
        if flags & os.O_DIRECTORY:
            return "artifact directory"
        return "artifact/staging" if ".staging" in name else "artifact/file"
    if name.startswith(str(work)):
        return "fixture directory" if flags & os.O_DIRECTORY else "fixture file"
    return "runtime/" + Path(name).name


def profile_data(prof, opens, sql, fsync):
    stats = pstats.Stats(prof).stats
    key = lambda loc: source_name(loc[0]) + "::" + loc[2]
    selected_names = {"hooks_for_run","for_saved","_gateway","_bind","restore","module_sources","digest",
        "_callback","_call","_submit","submit_workflow_product","_product_workflow","_prepare_judgment","validate",
        "_read_artifact","_read_task_artifact","_allow_refs","is_analysis_data","_publish_verdict","_conclude","put",
        "publish_bytes","publish","read_json","verify","sha256_of_file","sync_directory_chain","_grant_job_outputs",
        "validate_evidence","check_capability","participates","_key","sha256_of_obj"}
    selected = {}
    for loc, values in stats.items():
        name = key(loc)
        if ((loc[2] in selected_names and loc[0].startswith(str(release) + "/")) or "sqlite3.Connection" in loc[2]
                or "posix.fsync" in loc[2]):
            nc, tottime, cumtime = values[1:4]
            selected[name] = {"calls":nc, "self_us":round(tottime*1e6,2), "cumulative_us":round(cumtime*1e6,2)}
    counts = Counter(sql_shape(q) for q in sql)
    ordered = [sql_shape(q) for q in sql]
    return {"calls":selected, "sql_counts":dict(counts), "sql_order_sha256":digest(payload(ordered)),
            "sql_total":len(sql), "python_open_counts":dict(opens), "python_fsync_count":len(fsync),
            "python_fsync_wall_us":sum(fsync)/1e3}


def sample():
    fixtures = json.loads((work/"fixtures.json").read_text())
    current = work/("trial-" + sys.argv[4])
    os.sched_setaffinity(0, {int(sys.argv[5])})
    active_opens = None
    def audit(event,args):
        if active_opens is not None and event == "open":
            active_opens[trace_group(args[0], args[2])] += 1
    sys.addaudithook(audit)
    emit({"ready": True, "gx_source": roots.__file__, "pid": os.getpid()})
    for line in sys.stdin:
        request = json.loads(line)
        rows = []
        for role, tool, traced in request:
            f = fixtures[role]
            template = work/role
            if current.exists():
                shutil.rmtree(current)
            shutil.copytree(template, current)
            with closing(store.open_store(current/"gx.db")) as db:
                root = owner(db, f["run"])
                # A live coordinator already observed these two unchanged tables.
                # Make the warm condition independent of CPython connection-ID reuse.
                for table in ("tasks", "outbox"):
                    store._columns(db, table)
                root.active_turns[f["thread"]] = f["turn"]
                msg = json.loads((work/(role+".json")).read_text())[tool]
                assert digest(payload(msg)) == f["envelopes"][tool]["sha256"]
                received = ReceivedMessage(1, msg)
                start_writes = db.total_changes
                sql, opens, fsync = [], Counter(), []
                if traced:
                    prof = cProfile.Profile()
                    original_fsync = os.fsync
                    def timed_fsync(fd):
                        at = time.perf_counter_ns()
                        try:
                            return original_fsync(fd)
                        finally:
                            fsync.append(time.perf_counter_ns()-at)
                    db.set_trace_callback(sql.append)
                    os.fsync = timed_fsync
                    active_opens = opens
                start_cpu, start = time.process_time_ns(), time.perf_counter_ns()
                if traced:
                    prof.enable()
                try:
                    reply = root._callback(received)
                finally:
                    if traced:
                        prof.disable()
                    wall, cpu = time.perf_counter_ns()-start,time.process_time_ns()-start_cpu
                    if traced:
                        active_opens=None
                        os.fsync=original_fsync
                        db.set_trace_callback(None)
                assert reply["success"], reply
                answer = json.loads(reply["contentItems"][0]["text"])
                if tool == "gx_submit_product":
                    assert answer["task_status"] == "ACCEPTED" and db.execute(
                        "SELECT status FROM tasks WHERE task_id=?", (f["task"],)).fetchone()[0] == "ACCEPTED"
                else:
                    assert len(answer["routes"]) == 2 and db.execute(
                        "SELECT status FROM tasks WHERE task_id=?", (f["task"],)).fetchone()[0] == "CLAIMED"
                assert db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0] == 0
                record = {"role":role, "tool":tool, "traced":traced, "wall_ns":wall, "cpu_ns":cpu,
                          "writes":db.total_changes-start_writes, "request_sha256":f["envelopes"][tool]["sha256"],
                          "result":"ACCEPTED" if tool=="gx_submit_product" else "read two grid routes"}
                if traced:
                    record["profile"] = profile_data(prof,opens,sql,fsync)
                rows.append(record)
        emit(rows)


if __name__ == "__main__":
    if mode == "prepare":
        prepare()
    else:
        sample()
