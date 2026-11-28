"""Check installed contracts, role projections, catalog registrations and optional archives."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PRODUCT_PATHS=("gx-h/gx_core","gx-h/config","gx-h/contracts","gx-h/plugins","gx-h/adapters","bin")
ARCHIVE_MAP={
    "reactions":"indexes/reaction-names/rhea139-142.sqlite",
    "rhea":"indexes/rhea142-sprot2026_03/lookup.sqlite",
    "enzyme":"indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite",
    "kinetics":"kinetics/brenda2026.1-sabio20260920-v1.sqlite",
}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def git(site,*args):
    return subprocess.check_output(["git","-C",str(site),*args],text=True).strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site",type=Path)
    args=parser.parse_args()
    root=args.site.resolve()
    # The product contract and role code are imported from the deployment under check.
    sys.path.insert(0,str(root/"gx-h"))
    from gx_core import contracts
    from gx_core.hostobs import host_policy
    from gx_core.plugins.catalog import package_digest
    hashes=contracts.contract_digest()
    assert hashes==json.loads((root/"gx-h/contracts/CONTRACT_SHA").read_bytes())
    roles={}
    for model in sorted(host_policy.PROJECT_MODELS):
        for floor in (None,*sorted(host_policy.PROJECT_WORKER_EFFORT_FLOORS)):
            doc,_=host_policy._native_policy_for_model(model,floor)
            pins=host_policy.native_role_pins(doc)
            roles[model+"@"+(floor or "default")]=dict(sorted(pins.items()))
    registry=root/"gx-h/config/workflow_packages.json"
    packages=json.loads(registry.read_bytes())["plugins"]
    registered = {row["path"]: row["sha256"] for row in packages}
    if len(registered) != len(packages):
        raise ValueError("Duplicate installed package registrations.")
    for name, expected_pin in registered.items():
        actual_pin = package_digest(root / "gx-h/plugins/installed" / name)
        if actual_pin != expected_pin:
            raise ValueError(f"Installed bytes for {name} give {actual_pin}, expected {expected_pin}.")
    data=root/"gx-h/var/science-assets/yeast-archive"
    expected=json.loads((root/"gx-h/plugins/installed/org.gx.yeast-enzyme-archive/1.0.0/snapshots.json").read_bytes())
    archives={}
    for name,item in ARCHIVE_MAP.items():
        path=data/item
        observed=digest(path) if path.is_file() else None
        archives[name]={"path":item,"expected":expected[name],"observed":observed,
                        "status":"ready" if observed==expected[name] else "missing" if observed is None else "wrong_snapshot"}
    out={"head":git(root,"rev-parse","HEAD"),"python":sys.version.split()[0],
        "trees":{path:git(root,"rev-parse","HEAD:"+path) for path in PRODUCT_PATHS},
        "config_files":{rel:digest(root/rel) for rel in (
              "gx-h/config/workflow_packages.json","gx-h/config/science_site_access.json",
              "gx-h/config/codex_project_role_pins.json","gx-h/contracts/CONTRACT_SHA")},
        "contract":hashes["__combined__"],"schema":hashes["db_schema.sql"],
        "native_role_pins_verified":roles,"registered_plugins":registered,"installed_package_digests_verified":len(registered),
        "archives":archives}
    print(json.dumps(out,indent=2))


if __name__=="__main__":
    main()
