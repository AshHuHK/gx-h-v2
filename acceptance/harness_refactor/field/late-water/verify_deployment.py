"""Compare the actual field deployment bytes with the requested candidate without touching either."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[4]
PIN="5904a4fdac3b0267a80fc7b59d3c00427a4871d4"
COMPONENTS={
    "route":["v6_routes.py","v6_graph.py","v6_goal.py","v6_decisions.py","v62_rounds.py"],
    "subscription":["research_topics.py","research_handoff.py"],
    "dispatch":["driver.py","roots.py","v62_modules.py","v62_reports.py","v6_jobs.py"],
    "brief":["v62_lead.py","v6_native.py","v62_native.py","module_methods.py","catalog_survey.py"],
}
def compare(release):
    live=release/"gx-h/gx_core/p0"
    def sha(data): return hashlib.sha256(data).hexdigest()
    rows=[]
    for group,names in COMPONENTS.items():
        for name in names:
            rel="gx-h/gx_core/p0/"+name
            candidate=subprocess.check_output(["git","show",PIN+":"+rel],cwd=ROOT)
            actual=sha((live/name).read_bytes())
            rows.append({"component":group,"file":rel,"field_sha256":actual,
                         "candidate_sha256":sha(candidate),"identical":actual==sha(candidate)})
    return {"candidate":PIN,"actual_deployment":json.loads((release/"field-deployment.json").read_text()),
            "checked":rows,"different":[row["file"] for row in rows if not row["identical"]]}
if __name__=="__main__":
    print(json.dumps(compare(Path(sys.argv[1]).resolve()),indent=2))
