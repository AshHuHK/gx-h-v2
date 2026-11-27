"""Exercise an additive update on a private backup of closed Runs from the previous deployment."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.native_release import native_state


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("archive", type=Path)
    args = cli.parse_args()
    source = args.archive.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp, "state")
        # Establish a fresh owner at the test path; retain the archived database's own schema pin.
        with native_state(state):
            pass
        with closing(sqlite3.connect((source/"gx.db").as_uri()+"?mode=ro", uri=True)) as original:
            before = original.execute("SELECT value FROM schema_meta WHERE key='contract_sha256'").fetchone()[0]
            studies = list(original.execute("SELECT run_id,stop_decision_ref FROM v6_goals WHERE status='CLOSED'"))
            if not studies:
                raise ValueError("The supplied archive has no sealed scientific Goal")
            with closing(sqlite3.connect(state/"gx.db")) as backup:
                original.backup(backup)
        for name in ("artifacts", "threads", "runs"):
            if (source/name).is_dir():
                shutil.copytree(source/name,state/name,dirs_exist_ok=True,symlinks=True)
        receipts = []
        for rid, decision in studies:
            argv = [str(ROOT/"bin/gx-p0"),"research","--state",str(state),"--run-id",rid]
            response = subprocess.run(argv,check=True,capture_output=True,text=True,
                                      env=dict(os.environ,GX_CODEX_OFFLINE="1"))
            export = json.loads(response.stdout)
            result = json.loads(Path(export["result"]).read_bytes())
            old = source/"research"/rid/"REPORT.md"
            if result["closure"]["decision_ref"] != decision or export["invocation"]["added"] != {"jobs":0,"host_turns":0}:
                raise ValueError("Reopening changed the evidence trail or commissioned work")
            row = dict(run_id=rid,decision_ref=decision,added=export["invocation"]["added"],
                       report_sha256=digest(Path(export["report"])))
            if old.exists():
                row["archived_report_sha256"]=digest(old)
                row["report_matches_archive"] = row["report_sha256"] == row["archived_report_sha256"]
            receipts.append(row)
        with closing(sqlite3.connect(state/"gx.db")) as reopened:
            after = reopened.execute("SELECT value FROM schema_meta WHERE key='contract_sha256'").fetchone()[0]
        print(json.dumps(dict(source_contract=before,updated_contract=after,studies=receipts),indent=2))


if __name__=="__main__":
    main()
