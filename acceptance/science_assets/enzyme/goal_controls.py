"""Run a real CPU Job for a negative substrate using the normal CLI and controlled Lead."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from unittest.mock import patch

from acceptance.v622.test_close import SurveyedStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import GxError
from gx_core.p0 import cli, manifest
from tests.test_v6_decisions import branch

CAP = "yeast-archive-lookup@1"
PACKAGE = "org.gx.yeast-enzyme-archive@1.0.0"


def run(work):
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    for value in ("outside-panel", "lactose"):
        (work / (value + ".json")).write_text(json.dumps({"taxid":559292, "schema":"gx.yeast-archive-query/1", "substrate":value}))
    grant = {"project":"archive-panel-control","research_effort":"deep","research_modules":[],
             "plugins":[PACKAGE],"capabilities":[CAP],"input_files":[{"role":r,"path":str(work / (n+".json"))}
                  for r,n in (("wrong","outside-panel"), ("query","lactose"))]}
    (work/"grant.json").write_text(json.dumps(grant))
    status = io.StringIO()
    with redirect_stderr(status), redirect_stdout(status), patch.object(cli,"run_until_stopped",side_effect=lambda c,_,rid,timeout:cli.snapshot(c,rid)):
        ok=cli.main(["research","--state",str(work/"state"),"--run-key","substrate-observations","--goal",
                     "What records exist for the archived sugar in this strain?","--goal-config",str(work/"grant.json")])
    if ok:
        raise RuntimeError(status.getvalue())
    with SurveyedStudy(work, reopen=True) as study:
        lead=study.next_actor("run")
        chosen=branch("archive", jobs=2)
        chosen["budget_limits"]["cpu_seconds"]=145
        materials = {ArtifactService(study.conn).read_json(ref)["substrate"]:ref for ref in study.materials}
        def decision(substrate):
            node = study.node(lead,"look",materials[substrate],capability=CAP,branch_id=chosen["branch"])
            return {"decision":draft(chosen,node,watches=[{"event":"job_settled","subject_ref":node["node"]}])}
        try:
            study.call(lead,"gx_decide_revision",decision("outside-panel"),"outside")
        except (GxError, ValueError) as exc:
            rejected=str(exc)
        else:
            raise AssertionError("An out-of-panel method was admitted")
        if study.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?",(study.run_id,)).fetchone()[0]!=0:
            raise AssertionError("Invalid input started a Job")
        submitted=study.call(lead,"gx_decide_revision",decision("lactose"),"inside")
        settled=study.settle(submitted["aliases"]["local:look"])
        job=manifest.read_manifest(study.conn,settled["job_id"])
        output=job["completion"]["outputs"][0]
        result=ArtifactService(study.conn).read_json(output["artifact_ref"])
        assert result["counts"]=={"distinct_accessions":0,"exact_accessions":0,"class_accessions":0}
        return {"entrypoint":"bin/gx-p0 research; controlled native Lead, actual detached Job","run_id":study.run_id,
                "outside_query":rejected,"outside_jobs":0,"inside_job_id":settled["job_id"],"outcome":"observed_zero",
                "file":output,"counts":result["counts"],"query":result["query"]}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace",type=Path)
    print(json.dumps(run(parser.parse_args().workspace.resolve()),indent=2))
