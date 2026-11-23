"""Run the numerical scientific-quality experiment in an isolated workspace through gx-p0."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
RELEASE = HERE.parents[2]
sys.path[:0] = [str(RELEASE / "acceptance"), str(RELEASE / "acceptance/v31/waves")]
import runner
from e2e_grid import fixture_env
import e2e_responses as responses
from fixture_peer import StudyPeer
from grade import capture, official_product_calls, score


def main(args):
    case = json.loads((HERE / "case.json").read_bytes())
    spec = case["project"]
    if args.v2_control:
        if args.mode != "scripted":
            raise ValueError("The version-two check uses the local scripted peer.")
        spec.update(workflow_version="2", plugins=["org.gx.science-grid@2.0.0"])
    work, out = args.private.resolve(), args.evidence.resolve()
    work.mkdir(parents=True, mode=0o700, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    install = work / "install"
    if args.revision == "WORKTREE":
        if args.mode == "live":
            raise ValueError("Use an exact frozen Git revision for the paid study")
        from v21_staging import stage_source
        build = stage_source(work, install, args.revision)
    else:
        build = runner.stage_source(RELEASE, args.revision, install, [])
    runtime = args.runtime.resolve()
    peer = None
    state = work / "state"
    with ExitStack() as stack:
        if args.mode == "scripted":
            official = runtime / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
            if runner.sha(official) != runner.OFFICIAL_BINARY_SHA256:
                raise ValueError("Official executable digest mismatch")
            peer = stack.enter_context(responses.local_responses(work / "requests.jsonl"))
            stack.enter_context(patch.object(responses, "fixture_action", StudyPeer(spec["workflow_version"])))
            env = fixture_env(work, peer, str(official))
            python = runtime / ".venv/bin/python"
            env["PATH"] = str(python.parent) + os.pathsep + env["PATH"]
        else:
            env, python, official = runner.runtime_env(work, runtime)
        runner.save(work / "spec.json", spec)
        runner.save(out / "freeze.json", {
            "frozen_utc": runner.utc(), "mode": args.mode, "build": build, "private_state": str(state),
            "model_visible_project": spec, "case_sha256": runner.sha(HERE / "case.json"),
            "grader_sha256": runner.sha(HERE / "grade.py"), "project_sha256": runner.sha(work / "spec.json"),
            "official_sha256": runner.sha(official), "turn_limit": spec["run_budget"]["api_calls"],
            "provider": "scripted localhost Responses" if peer else "OpenAI, official ChatGPT OAuth",
            "scope": "One synthetic response surface. Hypotheses and acquisition methods are supplied. "
                     "The scoring capability is public; only the new observations are held out until round one. "
                     "The benchmark tests conditional reasoning, source appraisal, selection and verification.",
            "grading_inputs_outside_staged_install": ["case.json rubric", "grade.py", "fixture_peer.py"],
        })
        deadline = time.monotonic() + (300 if peer else 1800)

        def cli(label, action, *flags):
            command = [str(python), str(install / "bin/gx-p0"), action, "--state", str(state), *map(str, flags)]
            status = runner.command(command, work=work, evidence=out, stage=install, env=env, label=label,
                                    deadline=deadline, monitor=not peer)
            if status:
                raise RuntimeError(f"{label} exited {status}; see private {work / (label+'.err')}")
            return json.loads((work / (label + ".out")).read_bytes())

        try:
            created = cli("01-start", "start", "--spec", work / "spec.json", "--run-key", "B5")
            if len(created["jobs"]) != 2:
                raise ValueError("Initial Job admission failed")
            rid = created["run_id"]
            for n, source in enumerate(case["supplements"]):
                action = work / (source["message_id"] + ".json")
                runner.save(action, {"kind": "supplement", "text": source["text"]})
                result = cli(f"02-source-{n}", "message", "--run-id", rid, "--action", action,
                             "--message-id", source["message_id"])
                if result["result"]["status"] != "supplement_registered":
                    raise ValueError("Mid-run supplemental evidence was not registered")
            final = cli("03-run", "run", "--timeout", "235" if peer else "1650")
            if final.get("runs"):
                final = next(r for r in final["runs"] if r["run_id"] == rid)
            if final.get("status") != "COMPLETED":
                raise ValueError("Workflow ended at " + str(final.get("status")))
        finally:
            if peer:
                runner.save(out / "scripted-requests.json", peer.rows)
            if (state / "gx.db").exists():
                evidence = capture(state)
                evidence["official_products"] = official_product_calls(Path(env["CODEX_HOME"]) / "sessions", evidence)
                runner.save(out / "evidence.json", evidence)
                grade = score(evidence, case)
                runner.save(out / "grade.json", grade)
        if not grade["all_mechanical_checks"]:
            raise ValueError("Scientific quality checks failed: " + str([k for k,v in grade["checks"].items() if not v]))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("scripted", "live"), required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--v2-control", action="store_true")
    p.add_argument("--private", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--runtime", type=Path, default=RELEASE)
    main(p.parse_args())
