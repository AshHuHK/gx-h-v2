"""Use the upgraded original installation to revisit a sealed PI message and export both studies.

Use the original deployment's absolute source directory for --release. Native states
retain that source root across in-place version upgrades.
"""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--run-key", required=True)
    args = parser.parse_args()
    release, work = args.release.resolve(), args.work.resolve()
    # Import the selected deployment's host settings and ordinary CLI wrapper.
    sys.path.insert(0,str(release))
    from acceptance.unified_multiturn.live import db, environment, parent_id, run_gx, write
    from gx_core.p0 import conversation, research
    os.umask(0o077)
    env = environment(work)
    with db(work) as conn:
        parent = parent_id(work,args.run_key)
        child = json.loads((work/"followup.json").read_text())["child_run_id"]
        for rid in (parent,child):
            if conn.execute("SELECT status FROM v6_goals WHERE run_id=?",(rid,)).fetchone()[0] != "CLOSED":
                raise ValueError("Warm readback requires the two original studies to be sealed")
        before = research.counts(conn,parent)
    challenge = json.loads((work/"pi.json").read_text())["challenge"]["payload"]["client_message_id"]
    mid = args.run_key+"-sealed-anchored-why"
    text = ("请只回看这条 PI 消息及封存证据。哪条路线的研究者据此改了哪项判断，另一条路线为何继续？"
            "请给出当时的报告或决定引用；无法从记录判断的部分请说明。请用中文简答。")
    build = subprocess.check_output(["git","rev-parse","HEAD"],cwd=release,text=True).strip()
    _,reply = run_gx(env,"message","--state",work/"state","--run-id",parent,
                     "--message-id",mid,"--reply-to",challenge,"--text",text)
    with db(work) as conn:
        after = research.counts(conn,parent)
        item = next(m for m in conversation.timeline(conn,parent)["messages"] if m["message_id"]==mid)
        scope = list(conn.execute("SELECT scope,p.report_ref FROM v62_modules m JOIN v62_module_reports p "
                                  "USING(run_id,module_instance_id) WHERE m.run_id=?", (parent,)))
        decision_refs = [x[0] for x in conn.execute("SELECT decision_ref FROM v6_decisions WHERE run_id=?", (parent,))]
        old_events = [dict(x) for x in conn.execute("""SELECT event_id,kind,subject_id FROM events
            WHERE run_id=? AND kind IN ('research.pi.disposition','v62.module.reported',
            'v6.decision.committed','v6.run.closed','v62.module.opened')""",(parent,))]
    exports = {}
    for rid in (parent,child):
        _, res = run_gx(env,"research","--state",work/"state","--run-id",rid)
        exports[rid] = res["invocation"]
    answer = item["reply"]["text"] if item["reply"] else ""
    event_groups = re.findall(r"(?:事件|events?)\s*[#＃]?\s*(\d+(?:\s*[、,，和及]\s*\d+)*)",answer,flags=re.I)
    event_ids = {int(i) for group in event_groups for i in re.findall(r"\d+",group)}
    receipt = {"build":build, "parent":parent, "child":child, "message_id":mid,
               "reply_to":challenge,"text":text,"response":item["reply"],"native_status":reply["status"],
               "cited_evidence_events":[e for e in old_events if e["event_id"] in event_ids],
               "cited_reports":[ref for _,ref in scope if ref in answer],
               "cited_decisions":[ref for ref in decision_refs if ref in answer],
               "before":before, "after":after,"warm_exports":exports}
    write(work/"release-readback.json",receipt)
    if not answer or item["reply_to"]!=challenge or before["jobs"]!=after["jobs"]:
        raise ValueError("The sealed anchored reply is missing or started an unexpected Job")
    if not any(receipt[k] for k in ("cited_evidence_events","cited_decisions","cited_reports")):
        raise ValueError("The sealed response lacks an identifiable historical evidence or decision citation")
    if any(v["action"]!="export_closed" or v["added"]!={"host_turns":0,"jobs":0} for v in exports.values()):
        raise ValueError("A warm export stepped a scientific Run")
    print(json.dumps({"build":build,"parent":parent,"child":child,"anchor":challenge,"turn_id":item["reply"]["turn_id"],
                      "cited_reports":receipt["cited_reports"],"cited_decisions":receipt["cited_decisions"],
                      "cited_evidence_events":receipt["cited_evidence_events"],
                      "warm_added":[v["added"] for v in exports.values()]},ensure_ascii=False))


if __name__ == "__main__":
    main()
