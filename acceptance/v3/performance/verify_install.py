"""Verify the isolated benchmark installation against its immutable Git source and official binary lock."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from run_workload import save, sha, utc


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",required=True,type=Path)
    p.add_argument("--runtime",required=True,type=Path)
    p.add_argument("--private",required=True,type=Path)
    p.add_argument("--evidence",required=True,type=Path)
    args=p.parse_args()
    env=json.loads((args.evidence/"environment.json").read_bytes())
    rev=env["source_commit"]
    stage=args.private/"install"
    raw=subprocess.check_output(["git","-C",args.source,"ls-tree","-rz",rev,"--",*env["stage_paths"]])
    expected={}
    changed=[]
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        mode,kind,rest=entry.split(b" ",2)
        oid,name=rest.split(b"\t",1)
        name=os.fsdecode(name)
        expected[name]=oid.decode("ascii")
        f=stage/name
        if not f.exists() and not f.is_symlink():
            changed.append({"path":name,"why":"missing"})
            continue
        if kind!=b"blob":
            changed.append({"path":name,"why":"not a Git blob"})
            continue
        data=os.fsencode(os.readlink(f)) if f.is_symlink() else f.read_bytes()
        got=hashlib.sha1(b"blob "+str(len(data)).encode("ascii")+b"\0"+data).hexdigest()
        x=bool(f.lstat().st_mode & 0o111)
        if got!=expected[name] or (mode==b"120000")!=f.is_symlink() or (mode!=b"120000" and (mode==b"100755")!=x):
            changed.append({"path":name,"expected":expected[name],"actual":got,"mode":oct(f.lstat().st_mode & 0o777)})
    present={str(f.relative_to(stage)) for f in stage.rglob("*") if f.is_file() or f.is_symlink()}
    lockfile=args.runtime/"release-assets/lock.json"
    lock=json.loads(lockfile.read_bytes())
    binary=args.runtime/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    observed=sha(binary)
    fs=subprocess.run(["findmnt","-T",str(args.private.resolve()),"-n","-o","FSTYPE"],capture_output=True,text=True)
    result={"captured":utc(),"commit":rev,
            "commit_tree":subprocess.check_output(["git","-C",args.source,"rev-parse",rev+"^{tree}"],text=True).strip(),
            "stage_git_blobs":len(expected),"staged_path_changes":changed,"additional_generated_paths":sorted(present-set(expected)),
            "official_codex":{"version":lock["codex_version"],"source_tag":lock["source"]["tag"],"source_commit":lock["source"]["commit"],
                "source_tree":lock["source"]["tree"],"locked_binary_sha256":lock["linux_x64"]["binary_sha256"],
                "observed_binary_sha256":observed,"release_assets_lock_sha256":sha(lockfile)},
            "staging_filesystem":fs.stdout.strip() or None,
            "release_binary_matches":observed==lock["linux_x64"]["binary_sha256"]==env["binary_sha256"]}
    save(args.evidence/"runtime-details.json",result)
    print(json.dumps({k:result[k] for k in ("commit","stage_git_blobs","staged_path_changes","release_binary_matches")},indent=2))
    if changed or not result["release_binary_matches"]:
        raise SystemExit(1)


if __name__=="__main__":
    main()
