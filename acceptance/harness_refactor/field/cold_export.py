"""Reopen an existing closed study with the normal official CLI and check sealed files and counts."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryFile

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observe(state, run):
    reports = state / "research" / run
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT run_id,status FROM v6_goals ORDER BY rowid").fetchall()
        if dict(rows).get(run) != "CLOSED":
            raise ValueError("Can only re-export an already closed study")
        jobs = db.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0]
        turns = db.execute("""SELECT COUNT(*) FROM (SELECT DISTINCT thread_id,turn_id FROM p0_root_deliveries
                         WHERE run_id=? AND method='turn/start' AND state='acknowledged' AND turn_id IS NOT NULL)""",
                           (run,)).fetchone()[0]
        result = json.loads((reports/"result.json").read_bytes())
        result.pop("invocation")
        scientific_json = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {"all_goals":dict(rows), "jobs":jobs, "turns":turns,
                "scientific_result_sha256":hashlib.sha256(scientific_json).hexdigest(),
                "sealed":{name:digest(reports/name) for name in ("REPORT.md", "DECISION.md")},
                "result_json_sha256_including_cli_invocation":digest(reports/"result.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("yeast", "a", "b"))
    parser.add_argument("base", type=Path)
    parser.add_argument("run")
    opts = parser.parse_args()
    base = opts.base.resolve()
    state = base/("yeast" if opts.case=="yeast" else "hydro")/"state"
    before = observe(state, opts.run)
    with TemporaryFile() as log:
        subprocess.run([sys.executable,"-B",str(HERE/"run.py"),opts.case,str(base),"--timeout","1"],
                       stdout=log, check=True)
    after = observe(state, opts.run)
    operational = "result_json_sha256_including_cli_invocation"
    same_science = all(value == after[key] for key, value in before.items() if key != operational)
    print(json.dumps({"run_id":opts.run,"before":before,"after":after,
                      "scientific_contents_and_execution_counts_unchanged":same_science,
                      "complete_result_bytes_unchanged":before[operational] == after[operational]},indent=2))
    if not same_science:
        raise ValueError("Cold re-export changed study count or sealed results")


if __name__ == "__main__":
    main()
