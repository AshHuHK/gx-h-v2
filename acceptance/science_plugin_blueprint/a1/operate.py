"""Prepare and run each frozen A1 problem using ordinary GX plugin install and research."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[2]
AUTH_HELPER = Path("acceptance/science_capabilities/gx_oauth.py")
BINARY = Path("vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
CASES = {"negative": ("marginal",), "positive": ("marginal", "concurrent"), "multiturn": ("marginal", "concurrent")}


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def dump(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def cli(deployment, *args):
    result = subprocess.run([str(deployment/"bin/gx-p0"), *map(str, args)], check=True,
                            cwd=deployment, capture_output=True, text=True)
    return json.loads(result.stdout)


def prepare(parent, case):
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deployment = parent/"release"
    if deployment.exists():
        return json.loads((deployment/"a1-deployment.json").read_text())
    packet = HERE/"multiturn" if case == "multiturn" else HERE
    preregistration = packet/"preregistration.json"
    checks = json.loads(preregistration.read_text())["sha256"]
    for path, sha in checks.items():
        if digest(packet / path) != sha:
            raise ValueError(f"Frozen file differs: {path}")
    with TemporaryDirectory(dir=parent) as temporary:
        dest = Path(temporary)/"release"
        for tree in ("bin", "gx-h"):
            shutil.copytree(PRODUCT/tree, dest/tree, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "var", "tests"))
        files = [AUTH_HELPER, "acceptance/v623/oauth/host.py", "acceptance/v623/live/deployment.py",
                 "acceptance/v622/public/funding.py"]
        for path in map(Path, files):
            (dest/path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PRODUCT/path, dest/path)
        for directory in ("vendor", "release-assets"):
            (dest/directory).symlink_to(PRODUCT/directory, target_is_directory=True)
        for path in ("question.md", "material.json", "goal.json"):
            (dest/"input").mkdir(exist_ok=True)
            shutil.copy2(HERE/case/path, dest/"input"/path)
        note = dest/"gx-h"/"docs"/"gauge-observation-note.md"
        shutil.copy2(HERE/"OBSERVATION_NOTE.md", note)
        cards = dest/"gx-h"/"config"/"research_asset_cards.json"
        index = json.loads(cards.read_text())
        index["cards"].append({
            "kind":"skill", "reference":"gauge-logbook@reference", "category":"hydrometry",
            "title":"Discharge field logbook", "purpose":"Background on archival gauge discharge log terminology.",
            "digest":digest(note), "source":{"workspace":"release", "path":"gx-h/docs/"+note.name,
            "sha256":digest(note)}, "inputs":["observation note"], "outputs":["archival reading note"]})
        dump(cards, index)
        pins = []
        for name in CASES[case]:
            source = PRODUCT/"examples"/"plugins"/("hydro-"+name)
            sha = cli(dest, "plugin", "digest", source)["sha256"]
            cli(dest, "plugin", "install", source, "--sha256", sha)
            result = cli(dest, "plugin", "verify", "org.gx.hydro-"+name+"@1.0.0")
            if result["failures"] or result["plugins"][0]["sha256"] != sha:
                raise ValueError("Ordinary plugin verification failed")
            pins.extend(result["plugins"])
        source_hash = hashlib.sha256()
        for path in sorted((dest/"gx-h"/"gx_core").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                source_hash.update(str(path.relative_to(dest)).encode() + bytes.fromhex(digest(path)))
        record = {"case":case,"preregistration_sha256":digest(preregistration),
                  "source_tree_sha256":source_hash.hexdigest(), "installed_plugins":pins,
                  "skill_reference":index["cards"][-1], "goal_config_sha256":digest(dest/"input"/"goal.json")}
        dump(dest/"a1-deployment.json", record)
        dest.rename(deployment)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare","run"))
    parser.add_argument("case", choices=CASES)
    parser.add_argument("work", type=Path, help="Private parent of the two original case directories")
    args=parser.parse_args()
    workspace = args.work.resolve()/args.case
    receipt = prepare(workspace, args.case)
    if args.action == "prepare":
        print(json.dumps(receipt, indent=2))
        return
    release=workspace/"release"
    material=release/"input"
    binary=release/BINARY
    pin=json.loads((release/"release-assets"/"lock.json").read_text())["linux_x64"]["binary_sha256"]
    if digest(binary)!=pin:
        raise ValueError("Release Codex binary does not match its lock")
    env=dict(os.environ, GX_P0_CODEX=str(binary))
    os.execve(sys.executable,[sys.executable, str(release/AUTH_HELPER), str(workspace/"official"), "research",
              "--state",str(workspace/"state"),"--run-key","a1-rill-"+args.case+"-first",
              "--goal-file",str(material/"question.md"),"--goal-config",str(material/"goal.json")],env)


if __name__ == "__main__":
    main()
