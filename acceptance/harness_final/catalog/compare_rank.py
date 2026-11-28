"""Create an ordinary Goal with one release and cold-query its saved contract with another."""
from contextlib import redirect_stdout
import argparse
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch


def invoke(repo, target, action):
    sys.path[:0] = [str(repo), str(repo/"gx-h"), str(repo/"gx-h/tests"), str(repo/"acceptance/v62a")]
    from gx_core.p0 import cli
    if action == "start":
        target.mkdir(parents=True, exist_ok=True)
        with patch.object(cli, "run_until_stopped", side_effect=lambda db, _, rid, timeout: cli.snapshot(db,rid)), redirect_stdout(io.StringIO()):
            if cli.main(["research", "--state", str(target/"state"), "--run-key","catalog-replay",
                         "--goal","Which reading methods fit the available public documents?"]) != 0:
                raise RuntimeError("Could not admit the ordinary Goal")
        return
    from acceptance.v622.test_close import CloseStudy
    with CloseStudy(target, reopen=True) as science:
        lead = science.next_actor("run")
        control = science.brief(lead)
        args = {"kind":"all"}
        if control.get("pi_method_scope",{}).get("status") == "pending":
            args["pi_method_scope"] = []
        science.call(lead, "gx_query_catalog", args, "open")
        rows = []
        for query in ("文獻","文献","Crossref","JATS","乳糖","cyclic peptide"):
            result = science.call(lead, "gx_query_catalog", {"kind":"research" if query in ("文獻","文献") else "all",
                                  "limit":15,"query":query}, query)
            rows.append({"query":query, "top15":[{k:i[k] for k in ("reference","kind","provider","status","match_reason")
                                                  if k in i} for i in result["items"]]})
        policy, = science.conn.execute("SELECT json_extract(inputs_json,'$.inputs.harness_policy') FROM runs").fetchone()
        return {"policy":policy,"queries":rows}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("start","read"))
    p.add_argument("repo", type=Path)
    p.add_argument("target", type=Path)
    p.add_argument("--output",type=Path)
    args=p.parse_args()
    result=invoke(args.repo.resolve(),args.target.resolve(),args.action)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
