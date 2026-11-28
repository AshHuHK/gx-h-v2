"""Build an isolated deployment of a nominated repair and repeat the registered disconnect."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import drive

sys.path.insert(0, str(drive.HERE.parent))
import run as deploy


def match_commit(src, release, commit):
    prefix = "gx-h/gx_core/"
    tree = subprocess.check_output(["git","archive",commit,prefix],cwd=src)
    with tarfile.open(fileobj=io.BytesIO(tree)) as archive:
        expected = {f.name: hashlib.sha256(archive.extractfile(f).read()).hexdigest()
                    for f in archive if f.isfile() and f.name.endswith(".py")}
    actual = {str(f.relative_to(release)): hashlib.sha256(f.read_bytes()).hexdigest()
              for f in (release/prefix).rglob("*.py")}
    if expected != actual:
        raise ValueError("Installed core differs from nominated commit")
    return {"verified_python_files": len(actual), "commit":commit}


def prepare(source, commit):
    if not source.is_dir():
        raise ValueError("Provide the source checkout containing the nominated candidate")
    commit = subprocess.check_output(["git","rev-parse","--verify",commit+"^{commit}"], cwd=source, text=True).strip()
    work = drive.LIVE / "late-water-repair"
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    deploy.SOURCE = source
    release = deploy.hydro_release(work)
    deployment = json.loads((release / "field-deployment.json").read_bytes())
    proof = match_commit(source,release,commit)
    first = drive.REG
    if deployment["installed"] != first["deployment"]["installed"] or deployment["site_policy_sha256"] != first["deployment"]["site_policy_sha256"]:
        raise ValueError("The repaired run must use the same hydro plug-ins and policy as the original")
    registration = {**first,"deployment":deployment, "repeat_of":"gxr_01m8fs36kqvx147ysh",
                    "purpose":"engineering recheck only; first scientific trial stays as observed",
                    "registered_core": proof, "original_registration_sha256":hashlib.sha256((drive.HERE/"preregistered.json").read_bytes()).hexdigest(),
                    "positive_delivery":"An official receiving scope sees the new artifact ref and human words in its native tool context or acknowledged brief after recovery. Check separately that it inspects actual second-station rows and acts on them."}
    path = work / "preregistered-repair.json"
    if path.is_file() and json.loads(path.read_bytes()) != registration:
        raise ValueError("A previous isolated repair deployment is already registered here")
    if not path.exists() and (work/"state/gx.db").exists():
        raise ValueError("A repair Run already exists without its original registration")
    if not path.exists():
        path.write_text(json.dumps(registration,ensure_ascii=False,indent=2)+"\n")
    return work,release,registration,path


def start(work,release,registration,path):
    drive.WORK, drive.RELEASE, drive.STATE, drive.REG = work, release, work/"state", registration
    drive.REGISTRATION = path
    official = drive.PRODUCT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    binary,drive.PIN = drive.host._pin(official)
    drive.CALL = [sys.executable, "-B", str(drive.PRODUCT/"acceptance/science_assets/deployment/start_native.py"),
                  str(release),str(binary),str(work)]
    drive.main()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("commit")
    parser.add_argument("--prepare-only",action="store_true")
    args = parser.parse_args()
    prepared = prepare(args.source.resolve(),args.commit)
    if args.prepare_only:
        print(prepared[3])
    else:
        start(*prepared)
