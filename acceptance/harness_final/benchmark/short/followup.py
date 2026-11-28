"""Single paired follow-up for the watch-format feedback observed during the original short trial."""

import argparse
import json
from pathlib import Path
from run import HERE, read, run, save, sha, trial


def check(work, release, candidate, out):
    registration = HERE / "followup-preregistered.json"
    protocol = read(registration)
    baseline = read(HERE / "field/results.json")
    if sha(HERE / "field/results.json") != protocol["original_field_sha256"]:
        raise ValueError("The original short field receipt changed")
    if (
        sha(HERE / "preregistered.json") != protocol["parent_protocol_sha256"]
        or candidate == protocol["original_candidate"]
    ):
        raise ValueError("Expected the patched candidate and registered trial")
    save(work / "followup-slot.json", {"candidate": candidate, "protocol_sha256": sha(registration)})
    out.mkdir(parents=True, exist_ok=True)
    result = run(work, release, candidate, out)

    def watch(fields):
        return [
            x["call"]
            for x in fields["calls"]
            if (x["refusal"].get("details") or {}).get("field", "").startswith("reconsider_when")
        ]

    new = result["friction"]
    comparable = {
        "first_candidate": protocol["original_candidate"],
        "candidate": candidate,
        "original_close": baseline["friction"]["first_close"],
        "close": new["first_close"],
        "original_matching_watch_refusal_ids": watch(baseline["friction"]),
        "matching_watch_refusal_ids": watch(new),
        "all_calls": new["calls"],
        "original_score": [baseline["blind_science"]["passed"], 7],
        "score": [result["blind_science"]["passed"], 7] if result.get("blind_science") else None,
    }
    calls = trial.sql(
        work / "revised",
        """SELECT n.call_id,n.thread_id,n.turn_id,
                json_extract(n.request_json,'$.arguments.decision') AS body_json FROM p0_v6_native_calls n
                WHERE json_extract(n.request_json,'$.tool')='gx_decide_revision' ORDER BY n.rowid""",
    )
    watch_repairs = []
    for call_id in comparable["matching_watch_refusal_ids"]:
        original = next(i for i, row in enumerate(calls) if row["call_id"] == call_id)
        initial = calls[original]
        repair = None
        attempts = 0
        for later in calls[original + 1 :]:
            if later["turn_id"] != initial["turn_id"] or later["thread_id"] != initial["thread_id"]:
                continue
            attempts += 1
            body = json.loads(later["body_json"])
            if "reconsider_when" not in body:
                repair = {
                    "call": later["call_id"],
                    "action": "omitted optional watch",
                    "attempt": attempts,
                }
                break
            watches = body["reconsider_when"]
            allowed = {
                "goal_changed",
                "evidence_added",
                "claim_changed",
                "job_settled",
                "budget_changed",
                "operator_reopened",
                "module_report",
            }
            if (
                isinstance(watches, list)
                and watches
                and all(
                    isinstance(w, dict)
                    and w.get("event") in allowed
                    and isinstance(w.get("subject_ref"), str)
                    and bool(w["subject_ref"].strip())
                    and set(w) == {"event", "subject_ref"}
                    for w in watches
                )
            ):
                repair = {
                    "call": later["call_id"],
                    "action": "supplied watch vocabulary and shape",
                    "attempt": attempts,
                }
                break
        watch_repairs.append({"refused_call": call_id, "first_observed_field_repair": repair})
    comparable["within_same_turn_watch_repairs"] = watch_repairs
    save(out / "format-followup.json", comparable)
    return comparable


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("release", type=Path)
    p.add_argument("candidate")
    p.add_argument("--out", type=Path, default=HERE / "followup")
    a = p.parse_args()
    x = check(a.work.resolve(), a.release.resolve(), a.candidate, a.out)
    print(x["close"], x["matching_watch_refusal_ids"], x["score"])
