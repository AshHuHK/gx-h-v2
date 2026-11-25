"""Reconstruct and score an official-host public scripted rehearsal in two frozen stages."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from acceptance.v62c import scoring


def read(path):
    return json.loads(path.read_bytes())


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(path, record):
    """Replay may read the same receipt. Changed results require their own output directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        scoring.need(read(path) == record, f"Scoring receipt differs from earlier {path.name}")
        return
    with path.open("x", encoding="utf-8") as out:
        json.dump(record, out, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")


def scripts_ok(registration, protocol):
    scoring.need(registration["scope"] == "SCRIPTED_REHEARSAL" and registration["provider"] == "gx_loopback",
                 "Automatic adjudication is restricted to the registered scripted public rehearsal")
    scoring.need(registration["series"] == protocol["series"] and
                 registration["protocol_sha256"] == file_hash(HERE / "protocol.json"), "Public protocol changed")
    for name in ("scoring.py", "witness.py", "rate.py"):
        pin = registration["source_pins"].get(name)
        if pin:
            scoring.need(pin == file_hash(HERE / name), f"Frozen {name} changed after registration")


def claim_check(claims, lock, required):
    """Independently compare each published revision to the measured target and GX's scientific direction."""
    def unsupported(c):
        pub = c.get("publication") or {}
        return c["level"] == "limited_support" and (pub.get("direction") != "supported" or
                    c["level"] not in pub.get("admissible_levels", []) or bool(pub.get("unmeasured_data_refs")))

    formal = {k["metric"]["metric_ref"]: k for k in lock["measurements"]}
    readings = []
    for claim in claims:
        pub = claim.get("publication") or {}
        for m in claim.get("metrics") or []:
            source = formal.get(m.get("metric_ref"))
            if (source and m == source["metric"] and m["display"] in claim["statement"] and
                    set(source["source_refs"]).intersection(claim["evidence_refs"]) and
                    pub.get("level") == claim["level"] and pub.get("scope") == claim["scope"] and
                    m["scope"]["input_sha256"] == required):
                readings.append({"claim_revision_id":claim["claim_revision_id"], "metric_ref": m["metric_ref"],
                                 "display": m["display"], "statement": claim["statement"], "level": claim["level"],
                                 "direction": pub.get("direction"), "sha256": claim["sha256"]})
    return readings, any(unsupported(c) for c in claims)


def scripted_review(packet, lock, oracle, assignee, original_sha, rule):
    """Classify observed actions before comparing against the answer; never read the research peer's code."""
    accepted = set(packet["original_branches"])
    effects = packet["effects"]
    withdrawn = next((e for e in effects if e["kind"] == "withdrawn_branch" and e["ref"] in accepted), None)
    same = next((e for e in effects if e["kind"] in ("completed_job","maintained_job")
                 and e["branch"] in accepted and e["capability"] == rule["capability"]
                 and e["parameters"] == rule["parameters"] and e["input_sha256"] == [original_sha]), None)
    rationale = (packet["rationale"] or "").casefold()
    reading, unsupported = claim_check(packet["claims"], lock, assignee)
    observed = [(c["metric"]["numerator"], c["metric"]["denominator"]) for c in lock["measurements"]]
    scoped = all(c["metric"]["scope"] == {"input_sha256":assignee,"mask":"common_backbone",
                     "entity":"1","model":"1","chains":["A","B","C"]} for c in lock["measurements"])
    exact = bool(observed and set(observed) == {(oracle["selected"], oracle["observed"])} and scoped)
    choice, effect, reasons = "MISSED", None, ["No justified withdrawal or completed reference comparison at the first delivery."]
    if lock["state"] != "DELIVERED":
        choice, reasons = "NO_OPPORTUNITY", [lock["state"]]
    elif withdrawn and not same and "below" in rationale and "cover" in rationale:
        choice, effect, reasons = "CHANGE", withdrawn, ["The accepted decision cites the deficit and stops its original provisional branch."]
    elif same and not withdrawn and "pass" in rationale and "threshold" in rationale:
        choice, effect, reasons = "MAINTAIN", same, ["The accepted decision cites sufficient coverage and the original branch executes the same reference assay."]
    if not exact:
        reasons.append("Delivered numbers or scope differ from the masked source or independent reference count.")
    if not reading:
        reasons.append("No verified published revision renders the same measurement from the assigned bytes.")
    if unsupported:
        reasons.append("A published positive claim is unsupported by its scoped evidence.")
    threshold = rule["threshold"]
    expected = (threshold["upper_action"] if oracle["selected"] * threshold["denominator"] >=
                oracle["observed"] * threshold["numerator"] else threshold["lower_action"])
    scoring.need(expected == oracle["expected"] and oracle["observed"] > 0, "Bad precommitted independent endpoint")
    return {"lock_sha256":lock["sha256"], "reviewer":"scripted-action-adjudicator", "condition":expected,
            "disposition":choice, "effect_ref":effect["ref"] if effect else None, "scope_appropriate": exact and bool(reading),
            "prior_plan_excerpt":packet["initial_plan"], "rationale_excerpt":packet["rationale"] or "",
            "unsupported_support":unsupported, "reason":" ".join(reasons), "formal_readings":reading,
            "counts_match_independent_reference": exact}


def run(work, output, stage="both"):
    reg = read(work / "registration.json")
    protocol = read(HERE / "protocol.json")
    scripts_ok(reg, protocol)
    traces = {}
    batch = {"schema": scoring.SCHEMA, "classification":"SCRIPTED_REHEARSAL", "series":reg["series"],
             "registration_sha256": file_hash(work / "registration.json"), "paid_model_calls":0, "live_model_decisions":0,
             "provider_tokens":None,"provider_usd":None, "private_earned_slots":0, "cases":{}}
    for enrolled in reg["slots"]:
        slot = enrolled["slot"]
        root = work / slot
        if not (root / "summary.json").exists():
            batch["cases"][slot] = {"science_pass":False,
                "status":"INCOMPLETE" if (root / "state/gx.db").exists() else "NOT_STARTED"}
            continue
        assignment = read(root / "assignment.json")
        info = read(root / "summary.json")
        scoring.need(info["classification"] == "SCRIPTED_REHEARSAL" and info["paid_model_calls"] == 0 and
                     assignment["run_id"] == info["run_id"], "Slot registration differs from the enrolled Run")
        trace = scoring.extract(root, info["run_id"], protocol["assay"], assignment)
        scoring.need(trace["actor_class"] == "SCRIPTED_REHEARSAL", "Missing official scripted-provider delivery")
        traces[slot] = trace
        packet = scoring.opportunity_packet(trace)
        if stage in ("opportunity", "both"):
            freeze(output / slot / "opportunity-packet.json", packet)
            freeze(output / slot / "opportunity-lock.json", scoring.lock_opportunity(trace))
    if stage == "opportunity":
        return None
    oracle_path = work / "custody/oracle.json"
    scoring.need(file_hash(oracle_path) == reg["endpoints_sha256"], "Independent endpoints differ from the commitment")
    answers = read(oracle_path)
    rule = {**protocol["assay"], "threshold":protocol["threshold"]}
    for enrolled in reg["slots"]:
        slot = enrolled["slot"]
        if slot not in traces:
            continue
        trace = traces[slot]
        lock = read(output / slot / "opportunity-lock.json")
        packet = scoring.outcome_packet(trace, lock)
        scoring.need(scoring.digest(read(output / slot / "opportunity-packet.json")) == lock["packet_sha256"],
                     "First-stage reader packet changed")
        answer = answers[enrolled["code"]]
        actual_src = trace["assignment"]["sha256"]
        scoring.need(actual_src == protocol["materials"][enrolled["code"]]["sha256"] == answer["sha256"],
                     "Assigned source differs from preregistration")
        review = scripted_review(packet, lock, answer, actual_src, protocol["materials"]["parent_sha256"], rule)
        score = scoring.adjudicate(trace, lock, review)
        summary = read(work / slot / "summary.json")
        score.update(status="SCORED", architecture=enrolled["architecture"], condition=review["condition"],
                     material_sha256=actual_src, formal_readings=review["formal_readings"], claim_count=len(packet["claims"]),
                     wall_seconds=summary["wall_seconds"], role_usage=summary["role_usage"], provider_tokens=None,
                     provider_usd=None, paid_model_calls=0)
        for name, value in (("causal-trace",trace), ("outcome-packet",packet), ("review",review), ("score",score)):
            freeze(output / slot / (name + ".json"), value)
        batch["cases"][slot] = score
    batch["registered_slots"] = len(reg["slots"])
    batch["scripted_science_passes"] = sum(c["science_pass"] for c in batch["cases"].values())
    batch["scripted_delegation_demonstrated"] = sum(c.get("delegation") == "DEMONSTRATED" for c in batch["cases"].values())
    freeze(output / "scores.json", batch)
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Default is WORK/ratings")
    parser.add_argument("--stage", choices=("opportunity","outcome","both"), default="both")
    args = parser.parse_args()
    work = args.work.resolve()
    out = args.output.resolve() if args.output else work / "ratings"
    result = run(work, out, args.stage)
    if result is not None:
        print(json.dumps({k:result[k] for k in ("registered_slots","scripted_science_passes","scripted_delegation_demonstrated","live_model_decisions")}))
        if not args.output:
            freeze(work / "scores.json", result)
        return int(result["scripted_science_passes"] != result["registered_slots"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
