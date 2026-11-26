"""Continue the same PI conversation after scientific close, including a new real study."""
import json
from pathlib import Path

from .live import CARDS, ROOT, SEALED, browser, db, parent_id, run_gx, wait_for, write


def followup(work, key, env):
    from gx_core.p0 import conversation, research, research_report, workspace
    parent = parent_id(work, key)
    file = work / "followup.json"
    log = json.loads(file.read_text()) if file.exists() else {}

    def save(label, value):
        log[label] = value
        write(file, log)

    def message(text, mid, *flags):
        return run_gx(env, "message", "--state", work / "state", "--run-id", parent,
                      "--text", text, "--message-id", mid, *flags)[1]

    def reply(mid):
        with db(work) as conn:
            entries = conversation.timeline(conn, parent)["messages"]
            item = next((m for m in entries if m["message_id"] == mid), None)
            if item and item["reply"] and item["reply"]["text"]:
                return item

    with db(work) as conn:
        close = research_report.completed_close(conn, parent)
        source = next(x for x in json.loads(workspace.find_run(conn, run_id=parent)["inputs_json"])["files"]
                      if Path(x["path"]).name == "1VYM.cif")
        before = research.counts(conn, parent)
    # Worker closure can precede Main's last conversational reply. Replay the same
    # anchored CLI message (without an extra scientific turn) to drain its queue.
    trace_id = key + "-trace"
    with db(work) as conn:
        trace = next(m for m in conversation.timeline(conn,parent)["messages"] if m["message_id"] == trace_id)
    if not trace.get("reply"):
        message(trace["text"], trace_id, "--reply-to", trace["reply_to"])
    with browser(work, env, parent) as page:
        if "sealed" not in log:
            save("sealed_before", before)
            save("sealed", page("send", text=SEALED))
        mid = log["sealed"]["payload"]["client_message_id"]
        if "sealed_response" not in log:
            if not reply(mid):
                message(SEALED, mid)
            save("sealed_response", wait_for(lambda:reply(mid), "native explanation of the sealed parent"))
            with db(work) as conn:
                save("sealed_after", research.counts(conn, parent))
        request = (CARDS / "successor.md").read_text().strip()
        material = ROOT / "examples/pcna/open-data/3TBL.cif"
        config = work / "successor-config.json"
        if not config.exists():
            write(config, {"parent_material_refs":[source["artifact_ref"]], "research_effort":"medium",
                  "input_files":[{"path":str(material), "role":"structure",
                    "sha256":"05a1488d313d9926f556f495ad155e3ebf7859b2fa3014bc68ae55465acdb776",
                    "acquisition":str(material.with_suffix(".json"))}]})
        child_mid = key + "-new-material"
        for label in ("successor", "successor_repeat"):
            if label not in log:
                save(label, message(request, child_mid, "--successor", "--successor-config", config))
        def child_entry():
            with db(work) as conn:
                entries = conversation.timeline(conn, parent)["messages"]
                result = next((m["result"] for m in entries if m["message_id"] == child_mid), None)
                if result and result.get("parent_decision_ref") == close["decision_ref"] and result.get("run_id"):
                    return result
        child = wait_for(child_entry, "child Run attached to this recorded request")["run_id"]
        if "switched" not in log:
            page("refresh", expected=SEALED)
            save("switched", page("select", runId=child))
        save("child_run_id", child)
    if "child_report" not in log:
        rc, result = run_gx(env, "research", "--state", work / "state", "--run-id", child)
        if rc or result.get("goal_status") != "CLOSED":
            raise ValueError("The child remains open; continue this same Run")
        save("child_report", result)
