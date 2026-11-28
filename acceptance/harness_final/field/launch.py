"""Pin the deployed GX, then open/resume either frozen archival question via the ordinary CLI."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[2]
KEY = "public-food-yeast-curation-first"
MESSAGE = "public-food-yeast-trehalase-followup-first"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(*args):
    return subprocess.check_output(["git", "-C", str(SOURCE), *args], text=True).strip()


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def pin(private, candidate):
    manifest = json.loads((HERE / "preregistered.json").read_bytes())
    for name, digest in manifest["frozen_files"].items():
        if sha(HERE / name) != digest:
            raise ValueError(f"Study input changed: {name}")
    sha_id = git("rev-parse", "--verify", candidate + "^{commit}")
    staged = private / "deployment"
    if staged.exists():
        saved = json.loads((staged / "study-deployment.json").read_bytes())
        if saved["product_commit"] != sha_id:
            raise ValueError("Resume using the registered product SHA and the same study directory")
        return staged
    private.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=private) as prefix:
        release = Path(prefix) / "product"
        release.mkdir()
        with tempfile.TemporaryFile() as snapshot:
            subprocess.run(["git", "-C", str(SOURCE), "archive", sha_id, "bin", "gx-h", "release-assets"],
                           check=True, stdout=snapshot)
            snapshot.seek(0)
            with tarfile.open(fileobj=snapshot) as archive:
                archive.extractall(release, filter="data")
        (release / ".venv").symlink_to(SOURCE / ".venv", target_is_directory=True)
        (release / "vendor").symlink_to(SOURCE / "vendor", target_is_directory=True)
        mount = release / "gx-h/var/science-assets"
        mount.mkdir(parents=True, exist_ok=True)
        (mount / "yeast-archive").symlink_to((SOURCE / "gx-h/var/science-assets/yeast-archive").resolve(strict=True),
                                                target_is_directory=True)
        recorded = {
            "product_commit": sha_id, "frozen_at": datetime.now(timezone.utc).isoformat(),
            "product_trees": {n:git("rev-parse", sha_id + ":" + n) for n in ("gx-h","bin","release-assets")},
            "station_policy_sha256":sha(release/"gx-h/config/science_site_access.json"),
            "plugin_registry_sha256":sha(release/"gx-h/config/workflow_packages.json"),
            "budget_config_sha256":sha(release/"gx-h/config/limits.json"),
            "limits":json.loads((release/"gx-h/config/limits.json").read_bytes())["budget_profiles"]["oauth_research"],
            "preregistration_sha256":sha(HERE/"preregistered.json"),
            "official_host_sha256":sha(release / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
        }
        if recorded["limits"] != manifest["profile_at_baseline"]:
            raise ValueError("The registered hard budget profile changed")
        write(release / "study-deployment.json", recorded)
        release.rename(staged)
    return staged


def config(case, private):
    info = json.loads((HERE/case/"goal.json").read_bytes())
    materials = private/"incoming"/case
    materials.mkdir(parents=True, mode=0o700, exist_ok=True)
    for item in info["input_files"]:
        name = item["path"]
        source = (private/"archive"/(Path(name).stem+".pmchtml") if name.endswith(".html")
                  else HERE/case/name)
        if sha(source) != item["sha256"]:
            raise ValueError(f"Original study material changed: {name}")
        dest = materials/name
        if not dest.exists():
            shutil.copyfile(source,dest)
        if sha(dest) != item["sha256"]:
            raise ValueError("Staged input differs")
        item["path"] = str(dest)
    return info


def run(args):
    private = args.private.resolve()
    release = pin(private,args.candidate)
    study = private/"study"
    study.mkdir(mode=0o700,parents=True,exist_ok=True)
    state = study/"state"
    goal = config(args.case,private)
    binary = release/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    start = [sys.executable,"-B",str(SOURCE/"acceptance/science_assets/deployment/start_native.py"),
             str(release),str(binary),str(study)]
    identity = None
    case_key = KEY if args.case == "a" else MESSAGE + "-independent"
    if (state/"gx.db").exists():
        with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as conn:
            parent = conn.execute("SELECT run_id,status FROM v6_goals WHERE trim(goal,char(9,10,13,32))=?",
                                  ((HERE/"a/question.md").read_text().strip(),)).fetchall()
            if not parent and args.case == "a" and conn.execute("SELECT count(*) FROM v6_goals").fetchone()[0] == 0:
                parent_id, parent_status = None, None
            elif len(parent) == 1:
                parent_id, parent_status = parent[0]
            else:
                raise ValueError("Existing study must have exactly one original A specimen")
            if args.case == "a":
                identity = parent_id
            else:
                successor = conn.execute("""SELECT run_id FROM runs WHERE
                    json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=? AND
                    json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?""",
                    (parent_id,MESSAGE)).fetchall()
                independent = conn.execute("SELECT run_id FROM v6_goals WHERE trim(goal,char(9,10,13,32))=?",
                    ((HERE/"aprime/question.md").read_text().strip(),)).fetchall()
                if len(successor) > 1 or len(independent) > 1:
                    raise ValueError("Follow-up has more than one Run identity")
                if successor:
                    identity = successor[0][0]
                elif independent:
                    identity = independent[0][0]
                elif parent_status == "CLOSED":
                    cfg = private/"incoming/aprime/successor.json"
                    write(cfg,{"input_files":goal["input_files"],"research_effort":goal["research_effort"]})
                    rc = subprocess.run([*start,"message","--state",str(state),"--run-id",parent_id,
                                         "--message-id",MESSAGE,"--successor","--successor-config",str(cfg),
                                         "--text",(HERE/"aprime/question.md").read_text().strip()]).returncode
                    if rc:
                        return rc
                    found = conn.execute("""SELECT run_id FROM runs WHERE
                        json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=? AND
                        json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?""",
                        (parent_id,MESSAGE)).fetchone()
                    if not found:
                        raise RuntimeError("Successor request pending in original workspace")
                    identity = found[0]
                elif not args.independent_after_hold:
                    raise ValueError("A remains open. Use the same A Run until close or documented genuine hold.")
    elif args.case == "aprime":
        raise ValueError("Parent has not run")
    opts=["research","--state",str(state),"--timeout",str(args.timeout)]
    cfg=private/"incoming"/args.case/"goal.json"
    if not identity:
        write(cfg,goal)
        opts += ["--run-key",case_key,"--goal-file",str(HERE/args.case/"question.md"),"--goal-config",str(cfg)]
    else:
        opts += ["--run-id",identity]
    return subprocess.run([*start,*opts],cwd=release).returncode


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("case",choices=("a","aprime"))
    p.add_argument("private",type=Path)
    p.add_argument("candidate")
    p.add_argument("--timeout",type=int,default=1800)
    p.add_argument("--independent-after-hold",action="store_true",help="Run the frozen follow-up independently after a documented genuine hold on A")
    sys.exit(run(p.parse_args()))
