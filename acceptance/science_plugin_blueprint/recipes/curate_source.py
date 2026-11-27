"""Publish the platform team's reusable open question from the two successful public B0 Jobs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--deployment", required=True, type=Path)
    args = parser.parse_args()
    import sys
    sys.path.insert(0,str(args.deployment.resolve() / "gx-h"))
    from gx_core.persistence import store
    from gx_core.p0 import research_report, v6_recipes

    with closing(store.open_store(args.state.resolve() / "gx.db")) as conn:
        closed=research_report.completed_close(conn,args.parent)
        existing=conn.execute("SELECT reference,definition_json FROM v6_saved_recipes WHERE source_run=?", (args.parent,)).fetchall()
        for row in existing:
            definition=json.loads(row["definition_json"])
            if {step["capability"] for step in definition["steps"]} == {"archived-fold-calibration@1","mapped-structure-agreement@1"} and definition.get("decision_point",{}).get("output")=="agreement.json":
                return {"reference":row["reference"],"status":"already_available"}
        expected={"archived-fold-calibration@1":"predict","mapped-structure-agreement@1":"compare"}
        nodes=[]
        for node in conn.execute("SELECT node_id,spec_json FROM v6_nodes WHERE run_id=? AND status='SUCCEEDED'",(args.parent,)):
            cap=json.loads(node["spec_json"])["capability"]
            if cap in expected:
                nodes.append({"slot":expected[cap],"node_id":node["node_id"]})
        if sorted(s["slot"] for s in nodes)!=["compare","predict"]:
            raise ValueError("Expected exactly one successful source prediction and comparison")
        source=json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?",(args.parent,)).fetchone()[0])
        args_save={"recipe_id":"archived-coordinate-agreement","version":"1","title":"Compare a fresh archived-specimen prediction with mapped deposited coordinates",
                   "units":"Equal-weight C-alpha RMSD in angstrom; polymer positions counted separately.",
                   "id_alignment":"Use the supplied one-to-one polymer position mapping and declared chain IDs.",
                   "missing_values":"Keep mapped but unavailable positions in the coverage denominator.",
                   "nodes":nodes,"inputs":[{"name":f["role"],"artifact_ref":f["artifact_ref"]} for f in source["files"] if f["role"] in ("specimen","mapping","reference")],
                   "decision_point":{"step":"compare","output":"agreement.json",
                      "question":"Over what positions is comparison supported by this Goal's available coordinates?",
                      "actions":["Report agreement within the justified scope","Limit the claim and request additional coverage","Stop when evidence is inconclusive"],
                      "stop_condition":"The available mapped coverage and measurement support the stated scope, or the remaining gap requires additional data."}}
        card=v6_recipes.save(conn,args.parent,args_save)
        return {"status":"published_by_platform_team","source_run":args.parent,"source_closing_decision":closed["decision_ref"],
                "source_lead_saved_recipe":False, "owner":"gx_core.p0.v6_recipes.save",
                "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
                "source_jobs":json.loads(conn.execute("SELECT sources_json FROM v6_saved_recipes WHERE reference=?",
                                                     (card["reference"],)).fetchone()[0]), "card":card}


if __name__=="__main__":
    print(json.dumps(main(),indent=2,ensure_ascii=False))
