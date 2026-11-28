"""Compare the two installed runtimes while preserving each site's existing plugin registrations."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def git(root, *args):
    return subprocess.check_output(["git","-C",str(root),*args],text=True).strip()


def contract(root):
    probe = """import json,sys
from pathlib import Path
sys.path.insert(0,'gx-h')
from gx_core.contracts import contract_digest
actual=contract_digest()
assert actual==json.loads(Path('gx-h/contracts/CONTRACT_SHA').read_bytes())
print(json.dumps({'contract':actual['__combined__'],'ddl':actual['db_schema.sql'],'python':sys.version.split()[0]}))"""
    return json.loads(subprocess.check_output([str(root/".venv/bin/python"),"-B","-c",probe],cwd=root))


def compare(full, slim, commit):
    baseline="50d31f5792a178529a817ffd93f66ef9cbbb0be4"
    product=["gx-h/gx_core","gx-h/config","gx-h/contracts","gx-h/plugins"]
    changed=git(full,"diff","--name-only",baseline,commit,"--",*product).splitlines()
    registry="gx-h/config/workflow_packages.json"
    assert changed
    hashes={}
    for name in changed:
        if name == registry:
            continue
        frozen=subprocess.check_output(["git","-C",str(full),"show",commit+":"+name])
        if frozen!=(full/name).read_bytes() or frozen!=(slim/name).read_bytes():
            raise RuntimeError("Runtime differs at " + name)
        hashes[name]=hashlib.sha256(frozen).hexdigest()
    def entries(raw):
        return {p["path"]:p for p in json.loads(raw)["plugins"]}
    old=entries(git(full,"show",baseline+":"+registry))
    published=entries(git(full,"show",commit+":"+registry))
    slim_old=entries(git(slim,"show","c744aa9:"+registry))
    present=entries((slim/registry).read_bytes())
    expected=dict(slim_old)
    for name in old.keys() | published.keys():
        if old.get(name)!=published.get(name):
            if name in published:
                expected[name]=published[name]
            else:
                expected.pop(name,None)
    if expected!=present:
        raise RuntimeError("Slim plugin registry lost a local entry or a reviewed update")
    a,b=contract(full),contract(slim)
    assert a["contract"]==b["contract"] and a["ddl"]==b["ddl"]
    return {"full_runtime_commit":commit,"shared_files_count":len(hashes),
            "shared_files_sha256":hashes,"slim_registry_preserved":True,
            "full_installation":a,"slim_installation":b}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("full",type=Path)
    p.add_argument("slim",type=Path)
    p.add_argument("candidate")
    args=p.parse_args()
    print(json.dumps(compare(args.full.resolve(),args.slim.resolve(),args.candidate),indent=2))
