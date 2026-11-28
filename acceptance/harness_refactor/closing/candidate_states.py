"""Exercise counterfactual nominations against immutable publications and real transfer receipts of the PM2.5 study."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.p0 import research_closeout


def replay(state):
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as db, \
            patch.object(config,"RUNTIME_VAR_DIR",state):
        db.row_factory = sqlite3.Row
        task = db.execute("""SELECT t.* FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id)
            JOIN v6_goals g ON g.stop_decision_ref=d.decision_ref""").fetchone()
        visible = set(json.loads(task["visible_refs_json"]))
        rid = task["run_id"]
        samples = {}
        for (ref,) in db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND kind='scholarly_candidates'",(rid,)):
            if ref not in visible:
                continue
            for paper in ArtifactService(db).read_json(ref)["candidates"]:
                aliases = paper["identifiers"]
                ids = aliases.get("doi") or aliases.get("pmcid") or aliases.get("pmid")
                if not ids:
                    continue
                args = {"ref":ref,"candidate_id":ids[0],"reason":"Inspect this candidate in this counterfactual replay."}
                item = research_closeout.prepare_nomination(db,task,args)
                state_result = research_closeout._state(db,task,item,visible)
                label = state_result[0] if state_result else "located_original_already_read"
                samples.setdefault(label,{"candidate_id":ids[0],"list_ref":ref,"fetch_job_states":[
                    dict(job_id=job,status=status) for job,status in research_closeout._fetch_jobs(db,rid,ref,item["identifiers"])]})
        expected = {"located_original_already_read","candidate_listed","upstream_claims_fetchable","fetch_failed"}
        assert samples.keys() == expected, samples
        assert not db.total_changes
        return {"run_id":rid,"counterfactual_nominations":samples,"new_events":0,"new_jobs":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state",type=Path)
    print(json.dumps(replay(parser.parse_args().state.resolve()),indent=2))
