"""A short official-OAuth PCNA study with a PI using the real browser and CLI.

Run 'study' and 'participate' in separate terminals against the same --work and
--run-key. Use 'continue-study' after the first driver returns EX_TEMPFAIL, and
'followup' once the parent is closed. 'verify' reads the genuine closed state.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v623.oauth import host, pilot
from gx_core.p0 import research_routes

CARDS = ROOT / "examples/pcna-conversation"
PROGRESS = "请简短说明当前课题进展，有哪些已实际发生的研究决定？"
PREFERENCE = "以后在这个项目对话中，请用中文呈现报告和答复。"
AFTER = "我刚才关于对照的那句话改变了哪一条判断？请区分已收到、已读和已经生效的科学决定，给出依据。"
SEALED = "只解释封存的父题，不做新计算。先前为何保留另一条路线？请引用当时负责人的决定和这份结题报告。"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp,path)


def environment(work):
    binary, _ = host._pin(ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
    private = host._state() / ("multiturn-" + hashlib.sha256(str(work).encode()).hexdigest())
    env = host._env(private, binary, "gpt-6.2-astra", "max", sessions=work / "codex/sessions")
    env["PATH"] = pilot.science_plugin_path(env)
    return env


def run_gx(env, *args, process_ids=None):
    cmd = [str(ROOT / "bin/gx-p0"), *map(str, args)]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process_ids is not None:
        process_ids.append(proc.pid)
    stdout, stderr = proc.communicate()
    if proc.returncode not in (0,75):
        raise RuntimeError(f"gx-p0 {cmd[1]} exited {proc.returncode}: {(stderr or stdout)[-1500:]}")
    return proc.returncode, json.loads(stdout)


@contextmanager
def db(work):
    with closing(sqlite3.connect((work/"state/gx.db").as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def wait_for(check, label, timeout=1800):
    until = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= until:
            raise TimeoutError(label)
        time.sleep(0.4)


def parent_id(work, key):
    if not (work/"state/gx.db").exists():
        return None
    with db(work) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_workspace_runs'").fetchone():
            return None
        r = conn.execute("SELECT run_id FROM p0_workspace_runs WHERE run_key=?", (key,)).fetchone()
        return r[0] if r else None


def module_turns(conn, work, rid):
    roots = conn.execute("""SELECT r.*,m.module_instance_id,m.branch_id,m.scope,m.expected_delivery,
            m.status AS module_status,t.status AS task_status FROM v62_modules m
            JOIN v6_decision_tasks d USING(run_id,scope) JOIN tasks t ON t.task_id=d.task_id
            JOIN p0_roots r ON r.task_id=t.task_id WHERE m.run_id=? ORDER BY r.rowid""", (rid,)).fetchall()
    out = []
    for r in roots:
        for t in research_routes._turns(conn,rid,work/"state",r):
            if t.get("start") and r["provider"]=="openai":
                out.append({**{k:r[k] for k in ("module_instance_id","branch_id","scope","task_id","expected_delivery","module_status","task_status")},
                            **{k:t[k] for k in ("thread_id","turn_id","start","end","classification")}})
    return out


def two_working(work,rid):
    with db(work) as conn:
        active = [t for t in module_turns(conn,work,rid) if not t["end"] and t["module_status"]=="OPEN" and t["task_status"] in ("CLAIMED","RUNNING")]
        for i,left in enumerate(active):
            for right in active[i+1:]:
                if left["scope"]!=right["scope"] and left["thread_id"]!=right["thread_id"]:
                    return left,right
        if conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0]!="OPEN":
            raise RuntimeError("Parent closed before a new in-flight pair was observed")


@contextmanager
def browser(work,env,rid):
    cmd = [str(ROOT/"gx-h/bin/gx-web"),"--state-dir",str(work/"state"),"--project","pcna-multiturn","--run-id",rid,"--port","0"]
    with (work/"web.stderr").open("a") as err:
        server = subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=err,text=True)
        node = None
        try:
            line=server.stdout.readline()
            url=re.search(r"http://127\.0\.0\.1:[0-9]+/",line)
            if not url:
                raise RuntimeError(f"gx-web did not start: {line!r}")
            browser_env = dict(env)
            for key in ("GX_CHROMIUM","GX_PLAYWRIGHT"):
                if os.environ.get(key):
                    browser_env[key]=os.environ[key]
            node=subprocess.Popen(["node",str(Path(__file__).with_name("live_page.cjs")),url[0]],
                cwd=ROOT,env=browser_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,bufsize=1)
            if not json.loads(node.stdout.readline())["ready"]:
                raise RuntimeError("Research browser unavailable")
            def act(action, **kw):
                node.stdin.write(json.dumps(dict(action=action,**kw),ensure_ascii=False)+"\n")
                node.stdin.flush()
                result=json.loads(node.stdout.readline())
                if result.get("error"):
                    raise RuntimeError(result["error"])
                return result
            yield act
        finally:
            if node and node.poll() is None:
                node.stdin.close()
                try:
                    node.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    node.terminate()
                    node.wait()
            server.terminate()
            server.wait()


def participate(work,key,env):
    rid=wait_for(lambda: parent_id(work,key),"the native parent Run")
    file=work/"pi.json"
    log=json.loads(file.read_text()) if file.exists() else {}
    def save(label,data):
        log[label]=data
        write(file,log)
    def message(text,id,*extra):
        return run_gx(env,"message","--state",work/"state","--run-id",rid,"--text",text,"--message-id",id,*extra)[1]
    with browser(work,env,rid) as page:
        if "progress" not in log:
            save("progress",page("send",text=PROGRESS))
        if "challenge" not in log:
            a,b=wait_for(lambda:two_working(work,rid),"two running scientific module turns")
            text=(f"补充：为什么保留路线 {b['branch_id']}？我怀疑路线 {a['branch_id']} 的对照不够独立，"
                  "请核对这些链对是否共享同一个结构模型，再决定原判断应保留还是收窄。"
                  f"路线 {b['branch_id']} 继续原任务。报告用中文。")
            save("at_send",dict(utc=stamp(),routes=[a,b]))
            save("challenge",page("send",text=text,target={"branchId":a["branch_id"]},
                                  routeRefs=[a["branch_id"],b["branch_id"]]))
        if "preference" not in log:
            save("preference",message(PREFERENCE,key+"-language","--queue-only"))
        challenge=log["challenge"]["payload"]
        if "replay" not in log:
            save("replay",message(challenge["text"],challenge["client_message_id"],
                                  "--branch-id",log["at_send"]["routes"][0]["branch_id"],"--queue-only"))
        if "refresh" not in log:
            save("refresh",page("refresh",expected=PREFERENCE))
        if "trace" not in log:
            save("trace",message(AFTER,key+"-trace","--reply-to",challenge["client_message_id"],"--queue-only"))


def run_study(work,key,env,action):
    cmd=["research","--state",work/"state","--run-key",key,"--goal-file",CARDS/"question.md",
         "--goal-config",CARDS/"goal.json","--research-effort","medium"]
    if action=="study":
        # A short first driver is also the ordinary restart test; the same key resumes the same live study.
        cmd+=["--timeout","0.001"]
    sha=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    logfile=work/(action+".json")
    if action=="study" and logfile.exists():
        raise ValueError("Use continue-study for this previously launched study")
    print(action,sha,stamp(),flush=True)
    pids = []
    while True:
        rc,data=run_gx(env,*cmd,process_ids=pids)
        if data.get("driver")!="another_native_client":
            break
        time.sleep(1)
    write(logfile,dict(build=sha,utc=stamp(),exit_code=rc,driver_pid=pids[-1],result=data))
    print(action,rc,data.get("run_id"),data.get("goal_status"),flush=True)


def all_steps(work,key,env):
    from acceptance.unified_multiturn.live_followup import followup
    from acceptance.unified_multiturn.live_receipts import verify

    if not (work/"study.json").exists():
        run_study(work,key,env,"study")
    with ThreadPoolExecutor(max_workers=2) as pool:
        driver = pool.submit(run_study,work,key,env,"continue-study")
        pi = pool.submit(participate,work,key,env)
        pi.result()
        driver.result()
    followup(work,key,env)
    run_study(work,key,env,"reopen")
    verify(work,key)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("all","study","continue-study","participate","followup","verify","reopen"))
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--run-key",required=True)
    args=parser.parse_args()
    work=args.work.resolve()
    os.umask(0o077)
    work.mkdir(parents=True,exist_ok=True)
    if args.action=="verify":
        from acceptance.unified_multiturn.live_receipts import verify
        verify(work,args.run_key)
        return
    env=environment(work)
    if args.action=="all":
        all_steps(work,args.run_key,env)
    elif args.action in ("study","continue-study","reopen"):
        run_study(work,args.run_key,env,args.action)
    elif args.action=="participate":
        participate(work,args.run_key,env)
    else:
        from acceptance.unified_multiturn.live_followup import followup
        followup(work,args.run_key,env)


if __name__=="__main__":
    main()
