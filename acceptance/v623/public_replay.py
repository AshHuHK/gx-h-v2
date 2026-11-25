"""Independently reconstruct the frozen public loopback decisions from the original four GX databases and native records."""
import argparse
from pathlib import Path
import json
import hashlib
import shutil
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]


def read(path):
    return json.loads(path.read_bytes())

NAME="public-loopback.tar.gz"


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def pack(work,dst):
    from acceptance.v622.public_archive import snapshot
    from acceptance.v623.live import rehearsal_check
    reg=read(work/"registration.json")
    if reg["host"]!="offline" or reg["slots"]!=rehearsal_check.P["slots"]:
        raise ValueError("Only registered public loopback material can enter this archive")
    dst.mkdir(parents=True,exist_ok=False)
    with TemporaryDirectory() as tmp, tarfile.open(dst/NAME,"x:gz",compresslevel=6) as tar:
        def add(p,alias=None):
            if not p.is_file() or p.is_symlink():
                raise ValueError("Non-regular evidence "+str(p))
            tar.add(p,arcname=alias or str(p.relative_to(work)),recursive=False)
        for name in ("registration.json","result.json","source-pins.json","funding.json"):
            add(work/name)
        with TemporaryDirectory() as dbs:
            backup=Path(dbs)/"ledger";snapshot(work/"funding.sqlite",backup);add(backup,"funding.sqlite")
        for tree,pat in (("custody","*"),("adjudication","*.json")):
            for p in sorted((work/tree).rglob(pat)):
                if p.is_file():add(p)
        for row in reg["slots"]:
            num=row["slot"];slot=work/num
            for n in ("assignment.json","summary.json","first-attempt.json","native-host.json","accounting.json",
                      "parent.json","grant.json","raw/commands.json","raw/physical-http.jsonl","raw/wire.json"):
                add(slot/n)
            backup=Path(tmp)/(num+".db");snapshot(slot/"state/gx.db",backup);add(backup,num+"/state/gx.db")
            for tree,pat in (("state/artifacts","*"),("codex/sessions","*.jsonl"),("raw/scripted-provider","*.json")):
                for p in sorted((slot/tree).rglob(pat)):
                    if p.is_file():add(p)
    shutil.copyfile(work.parent/"verification.json",dst/"verification.json")


def replay(dst):
    from acceptance.v623.live import adjudication, rehearsal_check
    with TemporaryDirectory() as tmp,tarfile.open(dst/NAME) as tar:
        work=Path(tmp)/"work";work.mkdir()
        for m in tar:
            p=work/m.name
            if not m.isreg() or not p.resolve().is_relative_to(work):
                raise ValueError("Unexpected member in public archive")
            p.parent.mkdir(exist_ok=True,parents=True)
            with tar.extractfile(m) as src,p.open("xb") as out:shutil.copyfileobj(src,out)
        reg=read(work/"registration.json")
        registered=work/"adjudication"
        expected=Path(tmp)/"original-rating"
        registered.rename(expected)
        registered.mkdir()
        shutil.copyfile(expected/"registration.json",registered/"registration.json")
        for s in reg["slots"]:
            adjudication.lock(work,s["slot"])
        for s in reg["slots"]:
            adjudication.assess_offline_slot(work,s["slot"])
        calculated=rehearsal_check.verify(work)
        if calculated!=read(dst/"verification.json"):
            raise ValueError("Reconstructed public trajectories differ")
        original={p.relative_to(expected).as_posix():sha(p) for p in expected.rglob("*.json")}
        output={p.relative_to(registered).as_posix():sha(p) for p in registered.rglob("*.json")}
        if original!=output:
            raise ValueError("Re-reading original Runs changed their locked votes or effects")
        return {"source_commit":reg["source_commit"],"status":"PASS","original_runs":len(reg["slots"]),
                "packets_reproduced":len(output)-1,"archive_sha256":sha(dst/NAME),
                "verification_sha256":sha(dst/"verification.json"),"external_paid_calls":0}


if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("action",choices=("pack","verify"))
    ap.add_argument("--source",type=Path,help="Use this original checkout explicitly")
    ap.add_argument("--work",type=Path);ap.add_argument("--receipts",type=Path,required=True);a=ap.parse_args()
    d=a.receipts.resolve()
    identity=(read(a.work/"registration.json") if a.action=="pack" else read(d/"replay.json"))["source_commit"]
    if not re.fullmatch("[0-9a-f]{40}",identity):
        raise ValueError("Missing immutable source revision")
    with TemporaryDirectory() as tmp:
        source=a.source.resolve() if a.source else ROOT
        if not a.source and subprocess.check_output(["git","-C",source,"rev-parse","HEAD"],text=True).strip()!=identity:
            source=Path(tmp)/"source"
            subprocess.run(["git","clone","--quiet","--local","--shared","--no-checkout",ROOT,source],check=True)
            subprocess.run(["git","-C",source,"checkout","--quiet","--detach",identity],check=True)
        sys.path[:0]=[str(source),str(source/"gx-h")]
        if a.action=="pack":pack(a.work.resolve(),d)
        result=replay(d)
    if a.action=="pack":(d/"replay.json").write_text(json.dumps(result,indent=2)+"\n")
    elif result!=read(d/"replay.json"):raise ValueError("Reconstruction differs from recorded hashes")
    print(json.dumps(result))
