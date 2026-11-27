"""Admit or recover the second archived QC Goal after the first one has closed."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MESSAGE = "archive-qc-next-independent-deposition-1"


def successor(private, parent_id):
    state = private / "state"
    database = (state / "gx.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database, uri=True) as db:
        row = db.execute("SELECT status FROM v6_goals WHERE run_id=?", (parent_id,)).fetchone()
        if row != ("CLOSED",):
            raise SystemExit("The original Goal must be closed before opening its successor.")
    public = HERE / "case-b"
    frozen = json.loads((public / "goal.json").read_bytes())
    files = []
    for item in frozen["input_files"]:
        material = dict(item, path=str((public / item["path"]).resolve()))
        if "acquisition" in material:
            material["acquisition"] = str((public / material["acquisition"]).resolve())
        files.append(material)
    payload = private / "request-b.json"
    payload.write_text(json.dumps({"input_files": files, "research_effort": "deep"}, indent=2) + "\n")
    cli = [sys.executable, "-B", str(ROOT / "acceptance/science_capabilities/gx_oauth.py"), str(private / "host")]
    env = dict(os.environ, GX_P0_CODEX=str(ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"))
    cmd = [*cli, "message", "--state", str(state), "--run-id", parent_id, "--message-id", MESSAGE,
           "--successor", "--successor-config", str(payload), "--text", (public / "question.md").read_text().strip()]
    with (private / "message-b.json").open("w") as output:
        subprocess.run(cmd, env=env, check=True, stdout=output)
    with sqlite3.connect(database, uri=True) as db:
        row = db.execute("""SELECT run_id FROM runs
              WHERE json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=?
              AND json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?""",
                         (parent_id, MESSAGE)).fetchone()
    if not row:
        raise SystemExit("GX accepted the successor request. Resume this same message after the coordinator settles it.")
    print("Successor", row[0], flush=True)
    return subprocess.run([*cli, "research", "--state", str(state), "--run-id", row[0],
                           "--timeout", "1200"], env=env).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private", type=Path)
    parser.add_argument("parent_id")
    args = parser.parse_args()
    sys.exit(successor(args.private.resolve(), args.parent_id))
