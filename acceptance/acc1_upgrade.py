"""Upgrade the installation under one paused science_grid@1 project and finish it via GX P0."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from unittest.mock import patch

import e2e_responses
from e2e_audit import audit, check_native_charges
from e2e_grid import fixture_env

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "gx-h"))
from gx_core.p0.profiles import codex_argv
BASE = "47165af6befa919a5fc444a7c823ea9160cfb22f"
NOTE = "Supplement: The earlier control may have been contaminated."
PREFERENCE = "Set the report preference to include the evaluation references."


def inspect(state):
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]
        return {
            "run":rows("SELECT run_id,status,workflow_id,workflow_version,workflow_sha256,agent_policy_sha256 FROM runs")[0],
            "jobs":rows("SELECT job_id,status,scheduler_ref,idempotency_key,input_manifest_ref,reservation_ref "
                        "FROM jobs ORDER BY rowid"),
            "submissions":rows("SELECT task_id,attempt,submission_id,result,content_sha256 FROM submissions ORDER BY rowid"),
            "charges":rows("SELECT event_id,payload_json FROM events WHERE kind='budget.settled' ORDER BY event_id"),
            "paid":rows("SELECT subject_id,reservation_ref,amounts_json,status FROM budget_reservations "
                        "WHERE subject_kind='native_turn' ORDER BY rowid"),
            "artifacts":dict(conn.execute("SELECT artifact_ref,sha256 FROM artifacts ORDER BY rowid")),
            "decisions":rows("SELECT round_index,submission_id,reviewer_submission_id,decision_ref FROM science_decisions ORDER BY round_index"),
            "settlements":rows("SELECT settlement_id,round_index,eval_id,artifact_ref FROM science_settlements ORDER BY rowid"),
            "roots":rows("SELECT task_id,attempt,lease_id,thread_id FROM p0_roots ORDER BY rowid"),
            "budget":rows("SELECT spent_json,reserved_json FROM budgets ORDER BY rowid"),
        }


def run(work, report):
    install = work/"install"
    install.mkdir()
    pin = json.loads((PACKAGE/"release-assets/lock.json").read_text())["previous_p0"]
    archive = PACKAGE/"release-assets"/pin["file"]
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin["sha256"]:
            raise ValueError("Prior P0 runtime archive checksum differs")
    with tarfile.open(archive) as tar:
        if pin["commit"] != BASE or tar.pax_headers.get("comment") != BASE:
            raise ValueError("Prior P0 runtime archive has a different commit")
        tar.extractall(install, filter="data")
    report["baseline_runtime_archive_sha256"] = pin["sha256"]
    state = work/"project"
    runner = install/"bin/gx-p0"
    action = e2e_responses.fixture_action
    def paced_next_job(body, ident):
        result, facts = action(body, ident)
        if facts.get("round") == 0 and facts.get("output_kind") == "science_decision" and facts.get("tool") == "gx_submit_product":
            args = json.loads(result["arguments"])
            for grid in args["product"]["next_grids"].values():
                grid["delay_seconds"] = 5
            result = {**result, "arguments":json.dumps(args)}
        return result, facts

    with e2e_responses.local_responses(work/"requests.jsonl") as peer, patch.object(e2e_responses,"fixture_action",paced_next_job):
        binary = shutil.which(codex_argv()[0])
        if not binary:
            raise FileNotFoundError("Install the pinned Codex runtime using scripts/release_assets.py codex")
        env = fixture_env(work, peer, binary)
        report["codex"] = subprocess.check_output([binary,"--version"], env=env, text=True).strip()
        assert report["codex"] == "codex-cli 0.169.0"
        def command(verb, extra):
            return [str(runner),verb,"--state",str(state),*extra]
        def invoke(generation, verb, *extra):
            count = peer.request_count
            proc = subprocess.run(command(verb, extra), cwd=install, env=env, capture_output=True, text=True, timeout=105)
            report["commands"].append({"generation":generation,"action":verb,"exit":proc.returncode,
                                       "new_requests":peer.request_count-count})
            if proc.returncode:
                raise RuntimeError(f"{generation} {verb}: {proc.stderr}")
            return json.loads(proc.stdout)

        start = invoke("before","start","--demo-grid")
        prior_count = peer.request_count
        with subprocess.Popen(command("run",["--timeout","75"]),cwd=install,env=env,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True) as old:
            until = time.monotonic()+45
            while True:
                active = inspect(state)
                if (len(active["decisions"]) == 1 and len(active["jobs"]) == 3
                        and active["jobs"][-1]["status"] in ("RUNNING","SUBMITTED")
                        and all(r["status"] == "SETTLED" for r in active["paid"])):
                    break
                if old.poll() is not None or time.monotonic() >= until:
                    raise RuntimeError("Old CLI did not reach round 1 with settled turn receipts: "+str(active["run"]))
                time.sleep(0.025)
            paused = invoke("before","stop")
            out, err = old.communicate(timeout=20)
            assert old.returncode == 0 and json.loads(out)["status"] == "PAUSED", err
            report["commands"].append({"generation":"before","action":"active run exited at stop","exit":old.returncode,
                                       "new_requests":peer.request_count-prior_count})
        saved = inspect(state)
        report["before"] = saved
        assert saved["run"]["status"] == paused["status"] == "PAUSED" and saved["run"]["workflow_version"] == "1"
        assert len(saved["submissions"]) == 2 and len(saved["settlements"]) == 2 and len(saved["jobs"]) == 3
        assert len(saved["paid"]) == 3 and all(row["status"] == "SETTLED" for row in saved["paid"])
        db = state/"gx.db"
        db_identity = (db.stat().st_dev,db.stat().st_ino)
        # Replace only the temporary installation. GX database, artifacts, Codex history,
        # active Job runner files and the selected provider endpoint stay in their original paths.
        for path in ("gx-h","bin"):
            shutil.copytree(PACKAGE/path, install/path, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "var"))
        status = invoke("after","status")
        assert status["status"] == "PAUSED" and inspect(state) == saved
        assert not status["unsettled_turns"], status["unsettled_turns"]
        supplement = invoke("after","message","--message-id","upgrade-supplement","--text",NOTE)["result"]
        preference = invoke("after","message","--message-id","upgrade-preference","--text",PREFERENCE)["result"]
        report["natural_input"] = {"supplement":supplement,"preference":preference}
        assert supplement["status"] == "supplement_registered" and supplement["reason"] == "pinned_version", supplement
        assert preference["status"] == "unsupported" and preference["item_type"] == "soft_preference", preference
        finished = invoke("after","resume","--timeout","75")
        assert finished["status"] == "COMPLETED" and finished["run_id"] == start["run_id"], finished
        whole = audit(state)
        report["accounting"] = check_native_charges(whole, peer.request_count*35)
        assert not finished["unsettled_turns"] and sum(int(r["status"] != "SETTLED") for r in whole["native_turn_reservations"]) == 0
        final = inspect(state)
        assert db_identity == (db.stat().st_dev,db.stat().st_ino)
        assert {**saved["run"],"status":"COMPLETED"} == final["run"]
        assert len(final["jobs"]) == len({j["idempotency_key"] for j in final["jobs"]}) == 4
        assert all(j["status"] == "SUCCEEDED" for j in final["jobs"])
        assert [{**j,"status":"SUCCEEDED"} for j in saved["jobs"]] == final["jobs"][:3]
        for key in ("submissions","decisions","settlements","charges","paid","roots"):
            assert final[key][:len(saved[key])] == saved[key], key
        assert all(whole["artifact_hashes"][ref] == digest for ref,digest in saved["artifacts"].items())
        assert supplement["source_id"] in {s["source_id"] for s in finished["supplements"] if s["disposition"] == "pending"}
        with closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as conn:
            original, = conn.execute("SELECT text FROM source_segments WHERE source_id=?",(supplement["source_id"],)).fetchone()
        assert original == NOTE
        count = peer.request_count
        invoke("after","run","--timeout","20")
        assert inspect(state) == final and peer.request_count == count
        report.update(result="PASS",run_id=start["run_id"],same_db_inode=True,final_run=final["run"],
                final_jobs=final["jobs"],final_budget=final["budget"],final_rounds=len(whole["rounds"]),
                preserved_artifact_hashes=len(saved["artifacts"]),preserved_charge_events=len(saved["charges"]),
                preserved_accepted_submissions=len(saved["submissions"]),preserved_settlements=len(saved["settlements"]),
                final_report_ref=whole["report_ref"],final_stop_reason=whole["stop_reason"],
                final_unsettled_turns=finished["unsettled_turns"],supplement_text=original,
                supplement_disposition="pending",model_requests=peer.request_count,
                replay_new_model_requests=peer.request_count-count,replay_state_identical=True)
        artifact_hashes = report["before"].pop("artifacts")
        report["before"]["artifact_index_sha256"] = hashlib.sha256(
            json.dumps(artifact_hashes, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = {"baseline":BASE,"mode":"0.169.0 official App Server; scripted localhost Responses; real local CPU Jobs",
              "installation":"in-place software replacement in the temporary release directory",
              "start_utc":datetime.now(timezone.utc).isoformat(),"commands":[],"result":"FAIL"}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-acc1-upgrade-") as temporary:
            run(Path(temporary),report)
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        report["end_utc"] = datetime.now(timezone.utc).isoformat()
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
