"""Single paired follow-up for the watch-format feedback observed during the original short trial."""

import argparse
import json
from pathlib import Path
from run import HERE, read, run, save, sha


def check(work, release, candidate, out):
    registration = HERE / "followup-preregistered.json"
    protocol = read(registration)
    baseline = read(HERE / "field/results.json")
    if sha(HERE/"field/results.json") != protocol["original_field_sha256"]:
        raise ValueError("The original short field receipt changed")
    if sha(HERE/"preregistered.json") != protocol["parent_protocol_sha256"] or candidate == protocol["original_candidate"]:
        raise ValueError("Expected the patched candidate and registered trial")
    save(work/"followup-slot.json", {"candidate":candidate,"protocol_sha256":sha(registration)})
    out.mkdir(parents=True,exist_ok=True)
    result = run(work, release, candidate, out)
    def watch(fields):
        return [x["call"] for x in fields["calls"] if (x["refusal"].get("details") or {}).get("field","").startswith("reconsider_when")]
    new = result["friction"]
    comparable = {"first_candidate":protocol["original_candidate"],"candidate":candidate,
                  "original_close":baseline["friction"]["first_close"],"close":new["first_close"],
                  "original_matching_watch_refusal_ids":watch(baseline["friction"]),
                  "matching_watch_refusal_ids":watch(new),"all_calls":new["calls"],
                  "original_score":[baseline["blind_science"]["passed"],7],
                  "score":[result["blind_science"]["passed"],7] if result.get("blind_science") else None}
    save(out/"format-followup.json",comparable)
    return comparable

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("work",type=Path);p.add_argument("release",type=Path);p.add_argument("candidate")
    p.add_argument("--out",type=Path,default=HERE/"followup");a=p.parse_args()
    x=check(a.work.resolve(),a.release.resolve(),a.candidate,a.out);print(x["close"],x["matching_watch_refusal_ids"],x["score"])
