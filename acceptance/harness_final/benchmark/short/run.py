"""Repeat the original archive question once on a pinned candidate using its original fault and scoring protocol."""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
ORIGINAL = REPO / "acceptance/harness_refactor/blind"
sys.path.insert(0, str(ORIGINAL))
from common import BINARY, EFFORT, MODEL, PACKET, blind_text, lock, oauth_host, read, save, sha
import score
import trial
from native import operate


def classify(call, refusal):
    details = refusal.get("details") or {}
    reason = details.get("reason", "")
    field = details.get("field", "")
    code = refusal.get("code", "")
    if code in ("SCOPE_DENIED", "BUDGET_EXCEEDED") or reason in (
        "unseen_module_report",
        "outside_scope",
    ):
        return "permission_or_evidence_visibility"
    if reason in ("comparison_action_ambiguous", "branch_selection_required") or field.endswith(
        ".outcome"
    ):
        return "scientific_or_PI_disposition_choice"
    if len(call.get("ops", [])) == 1:
        op = call["ops"][0]
        if field == "ops[0].intent" and op.get("op") == "open_intent" and "intent" not in op:
            return "uniquely_mechanical"
        if (
            field == "ops[0].reason"
            and op.get("op") == "close_run"
            and "reason" not in op
            and call.get("rationale")
        ):
            if "unresolved" in refusal.get("message", "") and "unresolved" not in op:
                return "mixed_format_and_unspecified_scientific_field"
            return "uniquely_mechanical"
    return "other_or_indeterminate"


def friction(folder):
    q = """SELECT n.call_id,n.turn_id,n.request_json,c.result_json FROM p0_v6_native_calls n
         JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
         WHERE json_extract(n.request_json,'$.tool')='gx_decide_revision' ORDER BY n.rowid"""
    rows = []
    for r in trial.sql(folder, q):
        body = json.loads(r["request_json"])["arguments"]["decision"]
        result = json.loads(r["result_json"])
        refused = result.get("decision_refusal", {})
        is_close = any(o.get("op") == "close_run" for o in body.get("ops", []) if isinstance(o, dict))
        rows.append(
            {
                "call": r["call_id"],
                "turn": r["turn_id"],
                "close": is_close,
                "accepted": bool(result.get("decision_ref")),
                "refusal": refused,
                "classification": classify(body, refused) if refused else None,
            }
        )
    close = [x for x in rows if x["close"]]
    return {
        "first_close": {
            "denominator": int(bool(close)),
            "accepted": int(close[0]["accepted"]) if close else None,
        },
        "close_submissions": len(close),
        "rejections": sum(bool(x["refusal"]) for x in rows),
        "by_class": dict(Counter(x["classification"] for x in rows if x["classification"])),
        "calls": rows,
    }


def judge(folder):
    orig = read(ORIGINAL / "rubric.json")["criteria"]
    item = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in ("source_locator", "report_support", "reason")},
        "additionalProperties": False,
    }
    item["properties"]["mark"] = {"type": "string", "enum": ["pass", "fail", "not demonstrated"]}
    item["required"] = list(item["properties"])
    schema = {
        "type": "object",
        "properties": {k: item for k in orig},
        "required": list(orig),
        "additionalProperties": False,
    }
    report = blind_text(score.report_for(folder, "revised"))
    pkt = {
        "question": (PACKET / "question.txt").read_text(),
        "initial": read(PACKET / "initial.json"),
        "late": read(PACKET / "late.json"),
        "rubric": orig,
        "report": report,
        "instruction": "Independently grade each criterion and identify the precise report and source evidence.",
    }
    judge_dir = folder.parent / "independent-review"
    save(judge_dir / "packet.json", pkt)
    if not (judge_dir / "judge-terminal.json").exists():
        if (judge_dir / "judge-started.json").exists():
            raise RuntimeError("Reconcile unfinished original review")
        save(judge_dir / "judge-started.json", {"at": time.time()})
        operate(judge_dir, "judge", json.dumps(pkt, ensure_ascii=False), judging=True, schema=schema)
    term = read(judge_dir / "judge-terminal.json")
    marks = json.loads(term["answer"])
    assert marks.keys() == orig.keys() and term["status"] == "completed"
    return {
        "marks": marks,
        "passed": sum(m["mark"] == "pass" for m in marks.values()),
        "denominator": len(orig),
        "turn": term["turn_id"],
        "session": score.turns(judge_dir),
        "packet_sha256": sha(judge_dir / "packet.json"),
    }


def run(work, release, candidate, out):
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock(work)
    reg = read(HERE / "preregistered.json")
    save(
        work / "this-registration.json",
        {"candidate": candidate, "registration": reg, "sha256": sha(HERE / "preregistered.json")},
    )
    if not (work / "account.json").exists():
        pf = oauth_host().preflight(BINARY, MODEL, EFFORT)
        if pf["status"] != "READY":
            raise RuntimeError(pf["reason_codes"])
        save(
            work / "account.json",
            {k: pf[k] for k in ("status", "auth_type", "subscription_usage_state")},
        )
    trial.ROOT = release
    result = trial.execute(work, "revised", release, candidate)
    f = work / "revised"
    old = next(v for v in read(ORIGINAL / "field/results.json")["arms"] if v["arm"] == "revised")
    summary = {
        "run": result,
        "previous": {
            "candidate": old["commit"],
            "seconds": old["wall_seconds"],
            "close_submissions": 11,
            "rejections": 14,
            "first_close": {"accepted": 0, "denominator": 1},
            "score": [7, 7],
            "actual_turns": len(old["field_turns"]),
        },
        "frozen_candidate": candidate,
        "registration_sha256": sha(HERE / "preregistered.json"),
        "official_turns": score.turns(f),
        "friction": friction(f),
        "official_fault": read(f / "interruption.json") if (f / "interruption.json").exists() else None,
        "late_delivery": score.lead_delivery(f, "revised"),
    }
    summary["turns_delta_vs_previous_GX"] = len(summary["official_turns"]) - len(old["field_turns"])
    native = next(a for a in read(ORIGINAL / "field/results.json")["arms"] if a["arm"] == "stock")
    summary["turns_delta_vs_original_native"] = len(summary["official_turns"]) - len(
        native["field_turns"]
    )
    if result["outcome"] == "finished":
        summary["blind_science"] = judge(f)
        (out / "report.md").write_text(score.report_for(f, "revised") + "\n")
    save(out / "results.json", summary)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("release", type=Path)
    p.add_argument("commit")
    p.add_argument("--out", type=Path, default=HERE / "field")
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    d = run(a.work.resolve(), a.release.resolve(), a.commit, a.out)
    print(d["run"]["outcome"], d["friction"]["first_close"])
