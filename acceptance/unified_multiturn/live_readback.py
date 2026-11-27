"""Use a second installed release to read the original PI anchor, then warmly export both studies."""
import argparse
import json
import os
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
    exports = {}
    for rid in (parent,child):
        _, res = run_gx(env,"research","--state",work/"state","--run-id",rid)
        exports[rid] = res["invocation"]
    answer = item["reply"]["text"] if item["reply"] else ""
    receipt = {"build":build, "parent":parent, "child":child, "message_id":mid,
               "reply_to":challenge,"text":text,"response":item["reply"],"native_status":reply["status"],
               "cited_reports":[ref for _,ref in scope if ref in answer],
               "cited_decisions":[ref for ref in decision_refs if ref in answer],
               "before":before, "after":after,"warm_exports":exports}
    write(work/"release-readback.json",receipt)
    if not answer or item["reply_to"]!=challenge or before["jobs"]!=after["jobs"]:
        raise ValueError("The sealed anchored reply is missing or started an unexpected Job")
    if any(v["action"]!="export_closed" or v["added"]!={"host_turns":0,"jobs":0} for v in exports.values()):
        raise ValueError("A warm export stepped a scientific Run")
    print(json.dumps({"build":build,"parent":parent,"child":child,"anchor":challenge,"turn_id":item["reply"]["turn_id"],
                      "cited_reports":receipt["cited_reports"],"cited_decisions":receipt["cited_decisions"],
                      "warm_added":[v["added"] for v in exports.values()]},ensure_ascii=False))


if __name__ == "__main__":
    main()
