"""Read the original follow-up run: operator events, context, catalog query, and enacted response."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from export_first import export


def minute_check(file, reading):
    """Independent minute sum for these fixtures, which have integer interval boundaries."""
    original = json.loads(file.read_text(), parse_float=Decimal)
    channel_totals = {channel: Decimal(0) for channel in original["channels"]}
    slack = Decimal(0)
    minutes = 0
    for minute in range(*original["window_minutes"]):
        rates = {r["channel"]: r for r in original["intervals"] if r["valid"] and r["start"] <= minute < r["end"]}
        if len(rates) == 2:
            minutes += 1
            for channel, row in rates.items():
                channel_totals[channel] += 60 * row["q_L_s"]
                slack += 60 * row["err_L_s"]
    north, south = channel_totals.values()
    difference = north-south
    expected = {"common_minutes": minutes, "left_L": north, "right_L": south,
                "difference_L": difference, "difference_low_L": difference-slack,
                "difference_high_L": difference+slack}
    return {"source_file":file.name, "expected":{k:float(v) for k,v in expected.items()},
            "max_abs_error":float(max(abs(Decimal(str(reading[k]))-v) for k,v in expected.items()))}


def indexed(work):
    parent=work.parent
    data=export(parent,work.name)
    db=sqlite3.connect((work/"state/gx.db").as_uri()+"?mode=ro",uri=True)
    db.row_factory=sqlite3.Row
    try:
        first=db.execute("SELECT snapshot_json FROM research_contexts ORDER BY rowid LIMIT 1").fetchone()
        original=json.loads(first[0])
        data["first_science_brief"]={
            "capability_view":original.get("capability_view"),
            "method_names_in_snapshot":[x for x in ("discharge-concurrent","discharge-marginal") if x in first[0]],
            "initial_inputs":[{"role":r.get("role"),"name":r.get("name"),"artifact_ref":r.get("artifact_ref")}
                              for r in original.get("materials",[])]}
        stages=[]
        for i,case in enumerate(("opening","new trial logger","PI dispatch criterion","station custody observation")):
            if i==0:
                source=0
                trigger=None
            elif i in (1,3):
                filename=work/("update_"+str(i)+".json")
                if not filename.exists(): continue
                receipt=json.loads(filename.read_text())["receipt"]
                source=receipt["event_id"]
                trigger={"source":receipt.get("source"),"artifact_ref":receipt.get("artifact_ref")}
            else:
                row=db.execute("""SELECT event_id,payload_json FROM events WHERE kind IN
                           ('research.pi.answered', 'research.pi.feedback','v6.intent.changed')
                           AND event_id>? ORDER BY event_id LIMIT 1""",(stages[-1]["operator_event"],)).fetchone()
                if not row: continue
                source=row[0]
                trigger=json.loads(row[1])
            matching=[s for s in data["surveys"] if source==0 or source in s.get("trigger_events",[])]
            item={"change":case,"operator_event":source,"payload":trigger,"found_survey":bool(matching)}
            if matching:
                s=matching[0]
                ctx=json.loads(db.execute("SELECT snapshot_json FROM research_contexts WHERE context_ref=?",
                               (s["context_ref"],)).fetchone()[0])["catalog_discovery"]
                item.update(query_event=s["event_id"],query=s["arguments"],utc=s["utc"],turn=s["turn_id"],
                            latest_question=ctx["question"],latest_pi_update=ctx.get("pi_update"),
                            available_inputs=ctx["available_inputs"],required_outputs=ctx["required_outputs"])
                row=db.execute("SELECT request_json,receipt_json FROM v6_decisions WHERE context_ref=? ORDER BY rowid LIMIT 1",
                               (s["context_ref"],)).fetchone()
                if row:
                    proposal=json.loads(row[0])["decision"]
                    adopted=json.loads(row[1])
                    item.update(decision_event=adopted["event_id"],decision_ref=adopted["decision_ref"],outcome=adopted["outcome"],
                                rationale=proposal["rationale"],ops=proposal.get("ops",[]),
                                pi_dispositions=proposal.get("pi_dispositions",[]))
            stages.append(item)
        data["external_updates"]=stages
        for j in data["jobs"]:
            for out in j["outputs"]:
                reading = out["values"]
                name = {"POND-6":"material.json", "POND-7":"second.json"}.get(reading.get("trial"))
                if name and reading.get("schema") == "gx.hydrometry-paired/1":
                    out["independent_minute_sum"] = minute_check(HERE/name, reading)
        replay = work/"repeat_observation.json"
        if replay.is_file():
            data["repeated_observation"] = json.loads(replay.read_text())
        return data
    finally:
        db.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    args.output.write_text(json.dumps(indexed(args.study.resolve()),indent=2,ensure_ascii=False)+"\n")
