"""Run and grade the separately registered original attempt on the explicit repaired revision."""

import argparse
import json
from pathlib import Path
import secrets
from assessment import arm_result, blind_judge, final_report
from archive_outputs import archive
from run import run
from support import HERE, read, save, sha


def followup(work, candidate, out):
    registration = HERE / "followup-preregistered.json"
    protocol = read(registration)
    if candidate == protocol["original_candidate"]:
        raise ValueError("The follow-up requires the patched revision")
    if sha(HERE / "preregistered.json") != protocol["parent_protocol_sha256"]:
        raise ValueError("The registered original protocol changed")
    save(work / "followup-slot.json", {"candidate": candidate, "protocol_sha256": sha(registration)})
    outcome = run(work, "candidate", candidate)
    answer = final_report(work / "candidate", "candidate")
    label_path = work / "anonymous-label.json"
    if not label_path.exists():
        save(label_path, {"label": secrets.token_hex(10)})
    label = read(label_path)["label"]
    score = blind_judge(work / "anonymous" / label, answer) if answer else None
    entry = {"label":label,"blind_grade":score,**arm_result(work,"candidate")}
    out.mkdir(parents=True,exist_ok=True)
    if answer:
        (out / "candidate.md").write_text(answer + "\n")
    save(out / "results.json", {"study":"post-fix original follow-up","series":read(work/"series.json"),
            "followup_protocol_sha256":sha(registration),"arms":[entry],"n":1,"general_effect_size":None})
    archive(work,out)
    return {"outcome":outcome["outcome"],"score":score and score.get("score"),
            "in_flight_recovery":entry["end_to_end_takeover_verified"]}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("work",type=Path)
    p.add_argument("candidate")
    p.add_argument("--out",type=Path,default=HERE/"followup")
    args=p.parse_args()
    print(json.dumps(followup(args.work.resolve(),args.candidate,args.out)))
