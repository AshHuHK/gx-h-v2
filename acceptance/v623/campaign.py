"""Run the local V6.2.3 engineering campaign on a clean candidate and preserve native command receipts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from acceptance.closure.profile import test_environment

BASE = "e6520f53ca19ef8eae3ec0915af74e9bfc6dcdca"
PY = ROOT/".venv/bin/python"
BINARY = ROOT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"


def git(repo, *args):
    return subprocess.check_output(["git","-C",repo,*args],text=True).strip()


def save(path, value):
    with path.open("x") as stream:
        json.dump(value,stream,indent=2,ensure_ascii=False)
        stream.write("\n")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def run(name, argv, work, cwd, env=None):
    log = work/(name+".log")
    cmd = list(map(str,argv))
    start = datetime.now(timezone.utc).isoformat()
    with log.open("xb") as output:
        code = subprocess.run(cmd,cwd=cwd,env=env,stdout=output,stderr=subprocess.STDOUT).returncode
    result = {"argv":cmd,"cwd":str(cwd),"source_commit":git(cwd,"rev-parse","HEAD"),
              "exit_code":code,"started":start,"log":log.name,"log_sha256":sha(log)}
    save(work/(name+".command.json"),result)
    if code:
        raise RuntimeError(f"{name} exited {code}: {log}")
    return result


def frozen(repo, commit):
    if git(repo,"rev-parse","HEAD")!=commit or git(repo,"status","--porcelain"):
        raise ValueError(f"Expected clean checkout {commit} in {repo}")


def prepare_install(install):
    subprocess.run(["git","clone","--quiet","--shared","--no-checkout",ROOT,install], check=True)
    subprocess.run(["git","-C",install,"checkout","--quiet","--detach",BASE], check=True)
    for path in (".venv","vendor","gx-h/runtime/plugin-host/node_modules"):
        subprocess.run(["cp","-al",ROOT/path,install/path],check=True)


def execute(opts):
    work = opts.work.resolve()
    clash = opts.clash.resolve(strict=True)
    candidate = git(ROOT,"rev-parse","HEAD")
    if work.is_relative_to(ROOT): raise ValueError("Use a fresh external results directory")
    work.mkdir(mode=0o700,parents=True,exist_ok=False)
    env = test_environment(work/"environment",python=PY,binary=BINARY)
    env.update(PYTHONPATH=str(ROOT)+":"+str(ROOT/"gx-h"), GX_CODEX_OFFLINE="1", GX_CLASH=str(clash))
    def isolated(name, cwd, *argv, extra=None):
        local_env = env | {"PYTHONPATH":str(cwd)+":"+str(cwd/"gx-h")} | (extra or {})
        return run(name,[cwd/".venv/bin/python","-B",cwd/"acceptance/phase2a/regression/loopback.py","--clash",clash,"--receipt",work/(name+".network.json"),
                         "--",cwd/".venv/bin/python","-B",*argv],work,cwd,local_env)
    if opts.action == "baseline":
        original=opts.old.resolve()
        frozen(original,BASE)
        isolated("baseline",original,"acceptance/v622/verify.py","verify","--require-complete")
        for cohort in ("standard","formal"):
            isolated(cohort,original,"acceptance/v622/public_archive.py","replay","--receipts",
                     original/"acceptance/v622/receipts"/cohort)
        return
    if opts.action == "seed":
        install=work/"install"
        prepare_install(install)
        frozen(install,BASE)
        isolated("seed",install,"acceptance/v622/public/batch.py","--work",work/"old-run","--suite","standard",
                 "--host","offline","--diagnostic-slot","02")
        isolated("capture",install,ROOT/"acceptance/v623/upgrade.py","capture","--work",work,"--install",install,
                 "--project",work/"old-run/02")
        return
    frozen(ROOT,candidate)
    if opts.action == "upgrade":
        seed=opts.seed.resolve(strict=True)
        install=seed/"install"
        for phase,rev in (("check",candidate),("rollback",BASE)):
            subprocess.run(["git","-C",install,"checkout","--quiet","--detach",rev],check=True)
            isolated(phase,install,ROOT/"acceptance/v623/upgrade.py",phase,"--work",seed,
                     "--candidate",candidate,"--install",install,"--project",seed/"old-run/02")
        for name in ("capture","check","rollback"):
            shutil.copyfile(seed/(name+".json"),work/(name+".json"))
        return
    evidence=work/"product"
    evidence.mkdir()
    flags={"GX_V623_E1_RECEIPT":"E1.json","GX_V623_E4_JOB_RECEIPT":"E4-job.json",
           "GX_V623_CHRONOLOGY_DIR":"chronology","GX623_SOURCE_RECEIPTS":"revisions",
           "GX623_INVENTORY_RECEIPTS":"inventory","GX_D2_TRACE":"delivery"}
    def tests():
        isolated("product", ROOT,"-m","unittest","discover","-s","acceptance/v623","-p","test_*.py","-v",
                 extra={k:str(evidence/v) for k,v in flags.items()})
    def host():
        isolated("endpoint-host",ROOT,"acceptance/v623/endpoint_host.py","--work",work/"endpoint-host")
    def legacy():
        run("inherited",[PY,"-B","acceptance/v622/compat/regression.py","--work",work/"inherited","--clash",clash],
            work,ROOT,env)
    def repairs():
        isolated("v622",ROOT,"acceptance/v622/repairs.py","--work",work/"v622")
    with ThreadPoolExecutor(4) as executor:
        futures=[executor.submit(fn) for fn in (tests, legacy, repairs, host)]
        for f in futures: f.result()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("baseline","seed","test","upgrade"))
    p.add_argument("--work",type=Path,required=True)
    p.add_argument("--clash",type=Path,required=True)
    p.add_argument("--seed",type=Path)
    p.add_argument("--old",type=Path)
    opt=p.parse_args()
    if opt.action=="baseline" and not opt.old: p.error("baseline needs --old at "+BASE)
    if opt.action=="upgrade" and not opt.seed: p.error("upgrade needs an original --seed directory")
    execute(opt)
