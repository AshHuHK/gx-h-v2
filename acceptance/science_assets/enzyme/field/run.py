"""Start or resume the two ordinary GX research Goals in one distinct workspace."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[3]
sys.path[:0] = [str(PRODUCT), str(PRODUCT / "gx-h")]
from gx_core.p0 import capability_views, v62_outputs

KEY = "yeast-natural-records-maltose-first"
MESSAGE = "yeast-natural-records-isomaltose-first"


def open_state(work):
    conn = sqlite3.connect((work / "state/gx.db").as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def material(name):
    path = HERE / name
    return {"role": "query", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def run(work, *args):
    executable = PRODUCT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    env = dict(os.environ, GX_P0_CODEX=str(executable))
    return subprocess.run([sys.executable, "-B", str(PRODUCT / "acceptance/science_capabilities/gx_oauth.py"),
                           str(work / "host"), *args], env=env).returncode


def first(work):
    options = {"project": "food-yeast-archive-audit", "budget_profile": "oauth_research",
               "research_effort": "deep", "capability_collections": ["public_databases", "cpu_analysis"],
               "input_files": [material("query-a.json")]}
    scope = capability_views.freeze_scope(options)
    allowed = {entry["method"] for entry in scope["capability_options"]}
    expected = {"yeast-archive-lookup@1", "yeast-archive-compare@1"}
    if expected - allowed:
        raise SystemExit(f"Deployment is missing field-study capabilities: {sorted(expected - allowed)}")
    path = work / "goal-a.json"
    path.write_text(json.dumps(options, ensure_ascii=False, indent=2) + "\n")
    return run(work, "research", "--state", str(work / "state"), "--run-key", KEY,
               "--goal-file", str(HERE / "question-a.md"), "--goal-config", str(path), "--timeout", "1500")


def second(work, parent):
    with closing(open_state(work)) as conn:
        record = conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (parent,)).fetchone()
        if not record or record["status"] != "CLOSED":
            raise SystemExit("The source research is still open.")
        source = []
        rows = conn.execute("SELECT job_id,spec_json FROM v6_nodes WHERE run_id=? AND status='SUCCEEDED' "
                            "ORDER BY rowid", (parent,))
        for row in rows:
            if json.loads(row["spec_json"])["capability"] != "yeast-archive-lookup@1":
                continue
            for out in v62_outputs.published_outputs(conn, parent, row["job_id"]):
                if out["output_name"] == "parent-records.json":
                    source.append({k: out[k] for k in ("output_name", "artifact_ref", "sha256")} |
                                  {"job_id": row["job_id"], "role": "prior_records"})
        if len(source) != 1:
            raise SystemExit(f"Must identify the unique maltose archive deliverable, got {len(source)}.")
    options = {"research_effort": "deep", "input_files": [material("query-c.json")],
               "parent_outputs": source}
    path = work / "successor-options.json"
    path.write_text(json.dumps(options, ensure_ascii=False, indent=2) + "\n")
    status = run(work, "message", "--state", str(work / "state"), "--run-id", parent, "--message-id", MESSAGE,
                 "--successor", "--successor-config", str(path), "--text", (HERE/"question-c.md").read_text().strip())
    if status:
        return status
    with closing(open_state(work)) as conn:
        child = conn.execute("""SELECT run_id FROM runs WHERE
            json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=? AND
            json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?""", (parent,MESSAGE)).fetchone()
        if child is None:
            raise SystemExit("Message saved. Resume this same successor request after the parent driver releases the workspace.")
    return run(work, "research", "--state", str(work / "state"), "--run-id", child["run_id"], "--timeout", "1500")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--successor-of")
    args = parser.parse_args()
    private = args.workspace.resolve()
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    sys.exit(second(private, args.successor_of) if args.successor_of else first(private))
