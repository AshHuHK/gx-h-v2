"""Reconstruct the published V6.2.1 scores from the public SQLite/rollout archive with this installation."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[3]
BASE = "e5b7ee977ec33854362bc2440d8f9249464aed20"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    work = parser.parse_args().work.resolve()
    if work.is_relative_to(ROOT):
        parser.error("Supply a fresh directory outside the repository")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    published = ROOT / "acceptance/v62c/receipts"
    files = {}
    for name in ("public-raw-replay.tar.gz", "scores.json", "registration.json"):
        path = published / name
        expected = subprocess.check_output(["git","-C", ROOT,"show",f"{BASE}:{path.relative_to(ROOT)}"])
        if path.read_bytes() != expected:
            raise ValueError("Released scientific receipt changed: " + name)
        files[name] = hashlib.sha256(expected).hexdigest()
    with tarfile.open(published / "public-raw-replay.tar.gz", "r:gz") as bundle:
        bundle.extractall(work,filter="data")
    cmd = [sys.executable, "-B", str(ROOT / "acceptance/v62c/rate.py"), "--work", str(work)]
    subprocess.run(cmd, check=True)
    actual = hashlib.sha256((work / "scores.json").read_bytes()).hexdigest()
    if actual != files["scores.json"]:
        raise ValueError("The original score could not be reproduced from its stored scientific records")
    result = json.loads((work / "scores.json").read_bytes())
    receipt = {"status":"PASS", "published_on":BASE, "verified_on":subprocess.check_output(
                    ["git","-C",ROOT,"rev-parse","HEAD"],text=True).strip(),
               "registered_arms":result["registered_slots"], "science_passes":result["scripted_science_passes"],
               "demonstrated_delegation":result["scripted_delegation_demonstrated"],
               "run_ids":{k:v["run_id"] for k,v in result["cases"].items()}, "original_files":files,
               "reproduced_scores_sha256":actual,"paid_provider_calls":0,"argv":cmd}
    (work/"archive-receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
    print("Original and reconstructed public scores", actual)


if __name__ == "__main__":
    main()
