"""Start/resume the frozen studies through the normal official GX research CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[2]
BINARY = SOURCE / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
AUTH = SOURCE / "acceptance/science_assets/deployment/start_native.py"
KEY_A, KEY_YEAST, MESSAGE = "discharge-field-april-first", "yeast-source-attribution-first", "june-gauges-first"


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")


def product_cli(release, *args):
    candidate = release/".venv/bin/python"
    exe = str(candidate) if candidate.is_file() else sys.executable
    answer = subprocess.run([exe, "-B", str(release/"bin/gx-p0"), *map(str, args)], cwd=release,
                            text=True, capture_output=True, check=True)
    return json.loads(answer.stdout)


def hydro_release(base):
    release = base/"release"
    if release.is_dir():
        return release
    with TemporaryDirectory(dir=base) as tmp:
        stage=Path(tmp)/"release"
        for tree in ("bin","gx-h"):
            shutil.copytree(SOURCE/tree,stage/tree,symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache","var","tests"))
        runtime = SOURCE/".venv"
        if (runtime/"bin/python").is_file():
            (stage/".venv").symlink_to(runtime, target_is_directory=True)
        else:
            interpreter = stage/".venv/bin"
            interpreter.mkdir(parents=True)
            for name in ("python", "python3"):
                (interpreter/name).symlink_to(sys.executable)
        for name in ("vendor", "release-assets"):
            if (SOURCE/name).is_dir():
                (stage/name).symlink_to(SOURCE/name, target_is_directory=True)
        policy_file=stage/"gx-h/config/science_site_access.json"
        policy=json.loads(policy_file.read_bytes())
        receipts=[]
        for label in ("concurrent","marginal"):
            pkg=SOURCE/"examples/plugins"/("hydro-"+label)
            package_id=json.loads((pkg/"gx_plugin.json").read_bytes())["id"]+"@1.0.0"
            cap=json.loads((pkg/"method.json").read_bytes())["manifest"]
            pin=product_cli(stage,"plugin","digest",pkg)["sha256"]
            product_cli(stage,"plugin","install",pkg,"--sha256",pin)
            receipt=product_cli(stage,"plugin","verify",package_id)
            if receipt["failures"] or receipt["plugins"][0]["sha256"] != pin:
                raise ValueError(f"Cannot run {package_id}")
            policy["cpu_analysis"][package_id]=[cap["capability_id"]+"@"+cap["version"]]
            receipts.append({"provider":package_id,"sha256":pin,"methods":policy["cpu_analysis"][package_id]})
        write(policy_file,policy)
        code = hashlib.sha256()
        for path in sorted((stage/"gx-h/gx_core").rglob("*.py")):
            code.update(str(path.relative_to(stage)).encode()+b"\0"+hashlib.sha256(path.read_bytes()).digest())
        upstream = None
        if (SOURCE/".git").exists() and shutil.which("git"):
            git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True, capture_output=True)
            if git.returncode == 0:
                upstream = git.stdout.strip()
        write(stage/"field-deployment.json",{"installed":receipts,"gx_core_sha256":code.hexdigest(),
              "upstream_commit_at_copy":upstream,
              "site_policy_sha256":hashlib.sha256(policy_file.read_bytes()).hexdigest()})
        stage.rename(release)
    return release


def start(base, case, timeout):
    registration = json.loads((HERE/"preregistered.json").read_bytes())
    group = "yeast/" if case == "yeast" else "hydro/" + case + "/"
    for filename, expected in registration["inputs"].items():
        if filename.startswith(group) and hashlib.sha256((HERE/filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Registered study material changed: {filename}")
    hydro = case != "yeast"
    work=base/("hydro" if hydro else "yeast")
    work.mkdir(mode=0o700,parents=True,exist_ok=True)
    release=hydro_release(work) if hydro else SOURCE
    state=work/"state"
    sys.path[:0] = [str(SOURCE), str(SOURCE/"gx-h")]
    from acceptance.v623.oauth import host
    binary, _ = host._pin(os.environ.get("GX_P0_CODEX") or BINARY)
    args=[sys.executable,"-B",str(AUTH),str(release),str(binary),str(work)]
    opts=["research","--state",str(state),"--timeout",str(timeout)]
    if case == "yeast":
        return subprocess.run([*args,*opts,"--run-key",KEY_YEAST,"--goal-file",str(HERE/"yeast/question.md"),
                              "--goal-config",str(HERE/"yeast/goal.json")]).returncode
    here=HERE/"hydro"/case
    if case=="a":
        return subprocess.run([*args,*opts,"--run-key",KEY_A,"--goal-file",str(here/"question.md"),
                               "--goal-config",str(here/"goal.json")]).returncode
    with sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True) as db:
        parent=db.execute("SELECT run_id,status FROM v6_goals ORDER BY rowid LIMIT 1").fetchone()
        if not parent or parent[1]!="CLOSED":
            raise ValueError("Source study is still open; use 'a' to resume it.")
    successor={"input_files":json.loads((here/"goal.json").read_bytes())["input_files"],"research_effort":"deep"}
    for item in successor["input_files"]:
        item["path"]=str(here/item["path"])
    config=work/"successor.json"
    write(config,successor)
    message=[*args,"message","--state",str(state),"--run-id",parent[0],"--message-id",MESSAGE,
             "--successor","--successor-config",str(config),"--text",(here/"question.md").read_text().strip()]
    result=subprocess.run(message)
    if result.returncode:
        return result.returncode
    with sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True) as db:
        run=db.execute("SELECT run_id FROM runs WHERE json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=? "
                       "AND json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?",(parent[0],MESSAGE)).fetchone()
    if not run:
        raise ValueError("Successor is queued. Resume using the same message after the writer returns.")
    return subprocess.run([*args,*opts,"--run-id",run[0]]).returncode


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("case",choices=("yeast","a","b"))
    p.add_argument("work",type=Path)
    p.add_argument("--timeout",type=int,default=1200)
    a=p.parse_args()
    sys.exit(start(a.work.resolve(),a.case,a.timeout))
