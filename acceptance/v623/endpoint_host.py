"""D1/D2 on the pinned official Codex App Server with a loopback-only scripted model."""
import argparse
from contextlib import closing
import hashlib
import json
import subprocess
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

ROOT = Path.cwd() if "--baseline" in sys.argv else Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]

from acceptance.v622.public import batch, funding, offline
from acceptance.v62c import pilot_peer
from acceptance.v622.test_close import close_decision
from gx_core.domain import ids

CONFLICT = "估計九成。 The estimate is ninety percent."


def run(work, *, baseline=False, limit=512, select=False):
    if work.is_relative_to(batch.ROOT):
        raise ValueError("Use a new external directory for the official host session")
    work.mkdir(exist_ok=False, parents=True)
    ready = Event()
    ready.set()
    ledger = funding.BatchFunding(work/"funding.sqlite", series="V623-D-ENDPOINT-HOST", slots=[work.name])
    evidence = {}
    def step(brief, replies, *, behavior):
        if brief.get("module"):
            if not brief.get("nodes"):
                source = brief["materials"][0]["artifact_ref"]
                op = pilot_peer.measure(brief["module"]["branch_id"], source, "coverage", mask="ordered_backbone")
                return pilot_peer.decision([op],"Measure the ordered deposited positions.", event="job_settled", subject=op["node"])
            output = brief["nodes"][0]["result_ref"]
            if not output:
                return pilot_peer.decision([], "Await the Job.", event="job_settled", subject=brief["nodes"][0]["node_id"])
            return "gx_submit_module_report", dict(operation_key="publish", status="COMPLETE",
                 findings=[{"text":"Observed {{metric:coverage}}." if select else CONFLICT,"source_refs":[output]}], artifact_refs=[output], gaps=[],next_steps=[])
        if not brief.get("module_reports"):
            opened = replies.get(("gx_open_module",None))
            if not opened and not brief.get("previous_decision") and not brief["branches"]:
                return "gx_open_module", dict(module_id="data_inventory",module_version="2",focus="data",
                     expected_delivery="Inspect the deposited structure.",round=1,
                     input_refs=[brief["materials"][0]["artifact_ref"]],capabilities=[pilot_peer.METHOD],
                     sub_budget=dict(api_calls=6,tokens=150000,job_submissions=1,cpu_seconds=60,worker_tasks=5),
                     operation_key="inventory")
            return pilot_peer.decision([], "Await the released module evidence.", event="module_report",
                                       subject=(opened or {}).get("module_instance_id", "run"))
        report, = brief["module_reports"]
        ref = report["report_ref"]
        evidence["official_model_request_report"] = report
        evidence["delivered_brief_sha256"] = ids.sha256_of_obj(brief)
        close = close_decision([ref])
        if not baseline:
            read = replies.get(("gx_read_artifact", ref))
            if not read:
                return "gx_read_artifact", {"ref":ref,"endpoint_offset":0}
            evidence["official_model_request_tool_result"] = read
            row, = read["endpoint_rows"]
            close["metric_refs"] = [ref+"#"+row["key"]]
        return "gx_decide_revision", {"decision":close}

    grant_path = batch.ROOT/"examples/v62a-data-inventory/grant.json"
    grant=json.loads(grant_path.read_bytes())
    for item in grant["input_files"]:
        item["path"]=str((grant_path.parent/item["path"]).resolve())
    grant.update(capabilities=[pilot_peer.METHOD], plugins=["org.gx.protein@0.3.0"],
                 research_effort="deep",research_modules=["data_inventory@2"], module_policy={"report_bytes":limit})
    batch.save(work/"grant.json",grant)
    binary = batch.old.BINARY
    with (work/"lock").open("w") as lock, patch.object(offline.scripted,"peer_step",step), offline.session(
            work,binary,"standard",ready,ledger) as (env,wire,peer):
        cli=batch.native_cli(work,env,lock.fileno())
        rid=cli("start","--run-key","d-projection","--goal","Inspect deposited chain coverage.","--goal-config",work/"grant.json")["run_id"]
        with closing(batch.old.db_open(work)) as db:
            parent, = db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=?",
                              (rid,batch.materials.PARENT_SHA)).fetchone()
        batch.parent_observation(work,rid,parent,cli)
        result = cli("run","--run-id",rid,"--timeout",45,timeout=65)
        evidence.update(final_status=result["goal_status"], errors=[r["error"] for r in peer.rows if r.get("error")])
    with closing(batch.old.db_open(work)) as db:
        evidence["goal_run"]=rid
        evidence["native_decisions"]=[json.loads(r[0]) for r in db.execute("SELECT receipt_json FROM v6_decisions ORDER BY rowid")]
        evidence["endpoint_events"]=[dict(event_id=r[0],receipt=json.loads(r[1])) for r in db.execute(
                           "SELECT event_id,payload_json FROM events WHERE kind='v623.endpoints.received' ORDER BY event_id")]
        evidence["jobs"]=[dict(r) for r in db.execute("SELECT job_id,status,capability_id FROM jobs")]
        from gx_core.artifacts import ArtifactService
        from gx_core.native_release import native_state
        with native_state(work/"state"):
            assets = ArtifactService(db)
            report_ref, = db.execute("SELECT report_ref FROM v62_module_reports").fetchone()
            evidence["report"] = assets.read_json(report_ref)
            output, = db.execute("SELECT result_ref FROM v6_nodes WHERE result_ref IS NOT NULL").fetchone()
            record = assets.read_json(output)
            evidence["raw_counts"] = [record["selected_residues"],record["observed_union_residues"]]
            evidence["named_output"] = {"artifact_ref":output, "sha256":assets.describe(output)["sha256"]}
        evidence["source_commit"] = subprocess.check_output(["git","rev-parse","HEAD"],cwd=batch.ROOT,text=True).strip()
        with binary.open("rb") as binary_file:
            evidence["official_binary_sha256"] = hashlib.file_digest(binary_file,"sha256").hexdigest()
    evidence.update(transport="pinned official Codex App Server / loopback scripted Responses", actual_paid_calls=0,
                    budget_bytes=limit, confirmed_provider_tokens=None, model_assertion="Observed {{metric:coverage}}." if select else CONFLICT)
    batch.save(work/"endpoint-host.json",evidence)
    assert evidence["final_status"]=="CLOSED" and len(evidence["jobs"])==1 and not evidence["errors"], evidence["errors"]
    delivered = evidence["official_model_request_report"]
    assert evidence["raw_counts"]==[49,164]
    if baseline:
        assert bool(evidence["report"]["verified_metrics"])==select
        assert not delivered.get("verified_metrics") and "49/164" not in json.dumps(delivered)
    else:
        row, = delivered["endpoint_rows"]
        assert row["id"]=="coverage" and row["value"]=="49/164 (0.299)"
        recorded = {r["receipt"]["delivery_kind"] for r in evidence["endpoint_events"]}
        assert recorded == {"brief","artifact_read"}, recorded
        assert evidence["native_decisions"][-1]["endpoint_use"]["used_metric_refs"]
    print(work/"endpoint-host.json")


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--work",type=Path,required=True)
    args.add_argument("--baseline",action="store_true")
    args.add_argument("--selected",action="store_true",help="Exercise a correctly selected endpoint in a clipped report")
    args.add_argument("--report-bytes",type=int,default=512)
    config=args.parse_args()
    run(config.work.resolve(),baseline=config.baseline,limit=config.report_bytes,select=config.selected)
