"""Run a released scripted peer with explicit operator selection of its original evidence policy."""
import argparse
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def run(cohort, work, binary):
    c4 = cohort == "c4"
    policy = "gx.evidence/6.2.1" if c4 else "gx.evidence/6.2"
    full = ROOT / ("acceptance/v62c/rehearse.py" if c4 else "acceptance/v621/compat/released_peers.py")
    original_run = subprocess.run
    starts = []

    def historical_start(args, *other, **options):
        call = list(map(str, args))
        if str(ROOT / "bin/gx-p0") in call and call[call.index(str(ROOT / "bin/gx-p0")) + 1] == "start":
            config = json.loads(Path(call[call.index("--goal-config") + 1]).read_bytes())
            if config.get("evidence_policy") != policy:
                raise ValueError("Historical adapter needs the original explicit policy pin")
            call.append("--historical-replay")
            starts.append(call)
            return original_run(call, *other, **options)
        return original_run(args, *other, **options)

    argv = [str(full), *([] if c4 else [cohort]), "--work", str(work), "--binary", str(binary)]
    with patch.object(sys, "argv", argv), patch.object(subprocess, "run", historical_start):
        runpy.run_path(str(full), run_name="__main__")
    arms = {}
    for db in sorted(work.glob("*/state/gx.db")):
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            rid, saved = conn.execute("SELECT run_id,inputs_json FROM runs").fetchone()
            saved = json.loads(saved)["inputs"]
            if saved["evidence_policy"] != policy or saved.get("study_class") != "historical_replay":
                raise ValueError("A released peer used a different persisted policy")
            arms[db.parents[1].name] = {"run_id": rid, "evidence_policy": policy, "study_class": saved["study_class"],
                "jobs": conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0]}
    expected = {"a1": 2, "b4": 4, "c4": 4}[cohort]
    if len(arms) != len(starts) or len(arms) != expected:
        raise ValueError("Original peers did not exercise every historical admission")
    receipt = {"classification": "HISTORICAL_REPLAY", "cohort": cohort, "arms": arms,
               "start_commands": starts,"paid_provider_calls": 0}
    (work / "historical-policy.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", choices=("a1","b4","c4"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    opt = parser.parse_args()
    run(opt.cohort, opt.work.resolve(), opt.binary.resolve())
