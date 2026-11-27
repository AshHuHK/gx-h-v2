"""Verify the field-coverage export in an empty workspace backed by a clean, temporary source tree."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

EXPORT = Path(__file__).resolve().parent
ROOT = EXPORT.parents[2]
PROVIDER = "org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0"
BUILDER = "org.gx.record-method-builder@1.0.0"
PACKAGE = "record-coverage-8902880ed9d712bf6baa5899@1"


def verify(copy, workspace):
    sys.path[:0] = [str(copy), str(copy / "gx-h")]
    from acceptance.science_capabilities.recipe_reuse import lead
    from gx_core.artifacts import ArtifactService
    from gx_core.domain import ids
    from gx_core.errors import ScopeDenied
    from gx_core.native_release import native_state
    from gx_core.p0 import cli, manifest, v6_goal, v6_jobs
    from gx_core.persistence import store

    origin = json.loads((EXPORT / "source.json").read_bytes())
    archive = EXPORT / "accession-pmid-coverage@1.json"
    recipe = json.loads(archive.read_bytes())
    assert ids.sha256_of_obj(recipe) == hashlib.sha256(archive.read_bytes()).hexdigest() == origin["recipe_sha256"]
    runner = [sys.executable, "-B", str(copy / "bin/gx-p0")]
    def deployment(*args):
        return json.loads(subprocess.check_output([*runner, *map(str, args)], cwd=copy, text=True))
    absent = not (copy / "gx-h/plugins/installed" / PROVIDER.replace("@", "/")).exists()
    assert absent
    installed = deployment("plugin", "install", EXPORT / "plugin", "--sha256", origin["plugin_sha256"])
    checked = deployment("plugin", "verify", PROVIDER)
    assert installed["sha256"] == checked["plugins"][0]["sha256"] == origin["plugin_sha256"] and not checked["failures"]
    example = copy / "examples/record-method-authoring/sucrose"
    spec = json.loads((example / "goal.json").read_bytes())
    spec["mode"], spec["objective"] = "goal", (example / "question.md").read_text()
    spec["plugins"], spec["capabilities"] = [BUILDER, PROVIDER], ["derive-record-coverage@1", PACKAGE]
    for f in spec["input_files"]:
        f["path"] = str((example / f["path"]).resolve())
    methods = {s["slot"]: s for s in recipe["steps"]}
    expected_pins = {s["capability"]: s["method_digest"] for s in methods.values()}
    assert expected_pins[PACKAGE] == origin["method_sha256"]
    with native_state(workspace), closing(store.open_store(workspace / "gx.db")) as db:
        run = cli.start(db, workspace, spec, run_key="portable-coverage")["run_id"]
        pins = json.loads(db.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run,)).fetchone()[0])
        assert pins == expected_pins
        source_files = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])["files"]
        files = {f["role"]: f["artifact_ref"] for f in source_files}
        lead_call = lead(db, run, workspace)
        initial_count = db.execute("SELECT count(*) FROM v6_saved_recipes").fetchone()[0]
        assert initial_count == 0
        try:
            lead_call("gx_query_catalog", {"kind": "recipe", "reference": recipe["reference"]})
        except ScopeDenied:
            pass
        else:
            raise AssertionError("The archived recipe should be absent in the clean workspace")
        # Exercise just the two published read-only analytical steps. No recipe is imported into the DB.
        branch = {"op": "open_branch", "branch": "local:a", "hypothesis": "All candidate IDs have source pointers",
                  "alternative": "Some candidate IDs lack a pointer", "method_family": "Record coverage",
                  "discriminating_observation": "Join candidate and evidence IDs", "stop_condition": "All IDs counted",
                  "budget_limits": {"job_submissions": 2, "cpu_seconds": 120}}
        def job(slot, refs, depends):
            s = methods[slot]
            return {"op": "add_node", "node": "local:" + slot, "branch": "local:a", "depends_on": depends,
                    "question": s["question"], "capability": s["capability"], "parameters": s["parameters"],
                    "inputs": refs}
        assert methods["measure_coverage"]["depends_on"] == ["derive_method"]
        derive = job("derive_method", [files["method_source"]], [])
        measure = job("measure_coverage", [files["cohort"], files["evidence"]], ["local:derive_method"])
        accepted = lead_call("gx_decide_revision", {"decision": {"rationale": "Measure supplied record coverage.",
                    "ops": [branch, derive, measure]}})
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            v6_jobs.advance(db, run)
            nodes = list(db.execute("SELECT node_id,status,job_id FROM v6_nodes WHERE run_id=?", (run,)))
            if len(nodes) == 2 and all(n["status"] in v6_jobs.NODE_TERMINAL for n in nodes):
                break
            time.sleep(.05)
        if len(nodes) != 2 or any(n["status"] != "SUCCEEDED" for n in nodes):
            raise RuntimeError(str([dict(n) for n in nodes]))
        node_ids = {s: accepted["aliases"]["local:" + s] for s in methods}
        measure_id = next(n["job_id"] for n in nodes if n["node_id"] == node_ids["measure_coverage"])
        proof = manifest.read_manifest(db, measure_id)
        obs, = proof["completion"]["outputs"]
        result = json.loads(ArtifactService(db).read_bytes(obs["artifact_ref"], expected_sha256=obs["sha256"]))
        # Compare to a direct count over the supplied public record files.
        cohort = {r["accession"] for r in json.loads((example / "candidates.json").read_bytes())["candidates"]}
        linked = {r["entry"] for r in json.loads((example / "pointers.json").read_bytes())["pointers"] if r.get("pmid")}
        assert (result["numerator"], result["denominator"]) == (len(cohort & linked), len(cohort))
        v6_goal.ensure_task(db, run)
        follow = lead(db, run, workspace)
        recipe_id, version = recipe["reference"].split("@")
        metadata = {k: recipe[k] for k in ("title", "id_alignment", "units", "missing_values")}
        saved = follow("gx_save_recipe", {**metadata, "recipe_id": recipe_id, "version": version,
                "nodes": [{"slot": s, "node_id": node_ids[s]} for s in methods],
                "inputs": [{"name": k, "artifact_ref": files[k]} for k in recipe["inputs"]]})
        assert saved["digest"] == origin["recipe_sha256"]
        selection = follow("gx_query_catalog", {"kind": "recipe", "reference": recipe["reference"]})
        assert selection["spec"]["digest"] == saved["digest"]
        recreated = db.execute("SELECT definition_json,source_run FROM v6_saved_recipes WHERE reference=?",
                               (recipe["reference"],)).fetchone()
        assert recreated["definition_json"].encode() == archive.read_bytes() and recreated["source_run"] == run
    return {"source_checkout_initially_contains_plugin": not absent, "installed": installed,
            "native_callbacks": "controlled with real detached CPU Jobs",
            "opened_new_run": run, "source_run_in_export": origin["source_run"], "initial_recipe_count": initial_count,
            "goal_method_pins": expected_pins, "jobs": [dict(n) for n in nodes],
            "coverage": {"artifact_ref": obs["artifact_ref"], "numerator": result["numerator"],
                         "denominator": result["denominator"], "sha256": obs["sha256"]},
            "republished_recipe": saved["reference"], "definition_sha256": saved["digest"],
            "republished_definition_byte_identical": True, "historical_database_used": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--implementation", default="a517af40231cb47b5538685955d6999d66ca81b2")
    parser.add_argument("--in-copy", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.in_copy:
        print(json.dumps(verify(args.in_copy, args.in_copy.parent / "state"), ensure_ascii=False))
        return
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "source"
        tree.mkdir()
        with tempfile.TemporaryFile() as tar:
            subprocess.run(["git", "-C", str(ROOT), "archive", args.implementation, "bin", "gx-h",
                            "examples/record-method-authoring", "acceptance/science_capabilities/recipe_reuse.py"],
                           stdout=tar, check=True)
            tar.seek(0)
            subprocess.run(["tar", "-xf", "-", "-C", str(tree)], stdin=tar, check=True)
        result = json.loads(subprocess.check_output([sys.executable, "-B", str(__file__), "--in-copy", str(tree)],
                            cwd=tree, env=dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")))
        result["implementation"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", args.implementation],
                                                          text=True).strip()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.receipt:
        args.receipt.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
