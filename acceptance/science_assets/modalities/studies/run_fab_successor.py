"""Send one stable ordinary successor request for the next published Fab data set."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[3]
MESSAGE_ID = "second-archival-fab-deposition-v1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path, help="Private workspace used for the first Fab study")
    parser.add_argument("parent", help="Closed ordinary Goal")
    args = parser.parse_args()
    work = args.work.resolve(strict=True)
    state = work / "state"
    config_file = HERE / "fab-successor/goal.json"
    goal = json.loads(config_file.read_bytes())
    config = work / "successor.json"
    # Replaying the durable message must retain its original envelope across releases.
    if not config.exists():
        inputs = [dict(f) for f in goal["input_files"]]
        for material in inputs:
            for field in ("path", "acquisition"):
                if field in material:
                    material[field] = str((config_file.parent / material[field]).resolve(strict=True))
        config.write_text(json.dumps({"parent_method_options":["archival-fab-coverage@1"],
                                     "research_effort":"deep", "input_files":inputs}, indent=2) + "\n")
    host = [sys.executable, "-B", str(PRODUCT/"acceptance/science_capabilities/gx_oauth.py"), str(work)]
    env = os.environ.copy()
    env.setdefault("GX_P0_CODEX", str(PRODUCT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"))
    uri = (state/"gx.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        if db.execute("SELECT status FROM v6_goals WHERE run_id=?", (args.parent,)).fetchone() != ("CLOSED",):
            parser.error("The first Fab study must have a recorded closure before opening this successor.")
    reply = subprocess.run([*host, "message", "--state", str(state), "--run-id", args.parent,
                            "--successor", "--successor-config", str(config), "--message-id", MESSAGE_ID,
                            "--text", (config_file.parent/"question.md").read_text().strip()],
                           capture_output=True, text=True, env=env)
    (work/"second-admission.json").write_text(reply.stdout)
    if reply.returncode:
        (work/"second-admission-error.log").write_text(reply.stderr)
        sys.stderr.write(reply.stderr or reply.stdout)
        return reply.returncode
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("""SELECT run_id FROM runs WHERE
               json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=?
               AND json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?""",
                         (args.parent, MESSAGE_ID)).fetchone()
    if not row:
        parser.exit(2, "Request is recorded. Resume with the same message after the active workspace driver exits.\n")
    print("Successor", row[0], flush=True)
    return subprocess.run([*host, "research", "--state", str(state), "--run-id", row[0], "--timeout", "1080"],
                          env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
