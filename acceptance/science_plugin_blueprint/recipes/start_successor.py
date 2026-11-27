"""Use a finished study's frozen workspace workflow on the sparse public control."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--private", type=Path, required=True)
    args = parser.parse_args()
    release, private, state = (p.resolve() for p in (args.deployment, args.private, args.state))
    private.mkdir(exist_ok=True, mode=0o700, parents=True)
    with sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True) as conn:
        saved = [json.loads(row[0]) for row in conn.execute(
            "SELECT definition_json FROM v6_saved_recipes WHERE source_run=? ORDER BY reference", (args.parent,))]
        completed = conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (args.parent,)).fetchone()
    methods = {"archived-fold-calibration@1", "mapped-structure-agreement@1"}
    recipes = [item for item in saved if {step["capability"] for step in item["steps"]} == methods
               and item.get("decision_point", {}).get("output") == "agreement.json"]
    if completed != ("CLOSED",) or not recipes:
        parser.exit(2, "The parent must have closed with its published prediction, comparison and Lead question.\n")
    study = HERE / "sparse-reference"
    config = json.loads((study / "goal.json").read_bytes())
    config_path = private / "successor.json"
    options = {"research_effort":"deep","parent_method_options":sorted(methods),
               "input_files":[dict(item,path=str((study/item["path"]).resolve())) for item in config["input_files"]]}
    config_path.write_text(json.dumps(options, indent=2) + "\n")
    env = os.environ.copy()
    env["GX_P0_CODEX"] = str(release / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
    oauth = [sys.executable, "-B", str(release / "acceptance/science_capabilities/gx_oauth.py"), str(private / "host")]
    message = subprocess.run([*oauth, "message", "--state", str(state), "--run-id", args.parent,
                              "--message-id", "archived-partial-reference-v1", "--successor",
                              "--successor-config", str(config_path), "--text", (study / "question.md").read_text().strip()],
                             env=env, capture_output=True, text=True)
    (private / "entry.json").write_text(message.stdout)
    if message.returncode:
        (private / "entry-error.log").write_text(message.stderr)
        sys.stderr.write(message.stderr or message.stdout)
        return message.returncode
    with sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True) as conn:
        successor = conn.execute("""SELECT run_id FROM runs WHERE
            json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=?
            AND json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')=?""",
            (args.parent, "archived-partial-reference-v1")).fetchone()
    if not successor:
        parser.exit(2, "Successor request is recorded. Resume this same message after the parent driver releases the workspace.\n")
    print("Successor", successor[0], "Parent", args.parent, flush=True)
    return subprocess.run([*oauth, "research", "--state", str(state), "--run-id", successor[0],
                           "--timeout", "1800"], env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
