"""Project the slim smoke study's scientific trace from its SQLite state and official rollouts."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

PRODUCT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(PRODUCT), str(PRODUCT / "gx-h")]
from acceptance.harness_refactor.field.evidence import bytes_for, pick
from gx_core.p0 import research_report, research_routes


def completed_reads(path, thread, calls):
    """Match successful client-side deliveries to exact GX arguments and returned content."""
    matched = {}
    if not path:
        return matched
    with path.open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith(b"\n"):
                    break
                raise
            p = record.get("payload", {})
            if record.get("type") != "event_msg" or p.get("type") != "item_completed" or p.get("thread_id") != thread:
                continue
            item = p.get("item", {})
            key = (p.get("turn_id"), item.get("id"))
            if item.get("type") != "DynamicToolCall" or item.get("success") is not True or key not in calls:
                continue
            req, out = calls[key]
            parts = item.get("content_items", [])
            if (item.get("arguments") != req["arguments"] or item.get("tool") != req["tool"]
                    or len(parts) != 1 or parts[0].get("type") != "inputText"):
                continue
            try:
                received = json.loads(parts[0]["text"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if received == out:
                matched[key] = line_no
    return matched


def original_read(raw, req, answer, sha):
    tool, args = req["tool"], req["arguments"]
    if tool == "gx_read_source":
        text = answer.get("text")
        if (answer.get("source_sha256") == sha and isinstance(text, str) and text
                and hashlib.sha256(text.encode()).hexdigest() == answer.get("text_sha256")
                and args.get("segment_id") == answer.get("segment_id")):
            return {"type": "source_segment", **pick(answer, "receipt_id segment_id text_sha256")}
    else:
        try:
            if json.loads(raw) == answer:
                return {"type": "complete_json"}
        except (UnicodeDecodeError, ValueError):
            pass
        if isinstance(answer, dict) and answer.get("sha256") == sha:
            meta = answer.get("metadata", {})
            offset, count = meta.get("offset"), meta.get("returned_bytes")
            if (isinstance(offset, int) and isinstance(count, int) and count > 0 and
                    raw[offset:offset + count].decode("utf-8", errors="replace") == answer.get("content")):
                return {"type": "byte_page", "offset": offset, "bytes": count}
    return None


def receipt(db, state, sessions, rid):
    goal = db.execute("SELECT * FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
    if not goal:
        raise ValueError(f"No Goal {rid}")
    inputs = json.loads(goal["materials_json"])
    lead_turns, reads, adopted = [], [], []
    native_seq = {}
    rendered_readings = {}
    lead_roots = db.execute("""SELECT r.* FROM p0_roots r JOIN v6_decision_tasks t USING(run_id,task_id)
                              WHERE r.run_id=? AND t.scope='run' AND r.thread_id IS NOT NULL ORDER BY r.rowid""", (rid,))
    for root in lead_roots:
        thread = root["thread_id"]
        paths = list(sessions.rglob(f"rollout-*-{thread}.jsonl"))
        path = paths[0] if len(paths) == 1 else None
        spans = research_routes._history_turns(path, root) if path else {}
        genuine = {}
        for tid, in db.execute("""SELECT DISTINCT turn_id FROM p0_root_deliveries WHERE run_id=? AND thread_id=?
                                 AND state='acknowledged' AND method='turn/start' AND turn_id IS NOT NULL""", (rid, thread)):
            span = spans.get(tid, {})
            verified = root["provider"] == "openai" and span.get("model") == root["model"] and bool(span.get("response"))
            if verified:
                genuine[tid] = span
            lead_turns.append({"thread_id": thread, "turn_id": tid, "official_model_observed": verified,
                               "provider": root["provider"], "model": span.get("model"),
                               "rollout": path.name if path else None, **pick(span, "start end")})
        pairs = {}
        for row in db.execute("""SELECT n.rowid AS seq,n.turn_id,n.call_id,n.request_json,c.result_json
                FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
                WHERE n.thread_id=? AND json_extract(n.request_json,'$.tool') IN ('gx_read_artifact','gx_read_source')
                ORDER BY n.rowid""", (thread,)):
            native_seq[thread, row["turn_id"], row["call_id"]] = row["seq"]
            pairs[row["turn_id"], row["call_id"]] = (json.loads(row["request_json"]), json.loads(row["result_json"]))
        for (tid, call_id), line in completed_reads(path, thread, pairs).items():
            if tid not in genuine:
                continue
            req, answer = pairs[tid, call_id]
            ref = req["arguments"].get("ref") or req["arguments"].get("source_ref")
            art = db.execute("SELECT sha256,job_id FROM artifacts WHERE run_id=? AND artifact_ref=?", (rid, ref)).fetchone()
            if not art:
                continue
            raw = bytes_for(db, state, ref, art["sha256"])
            content = original_read(raw, req, answer, art["sha256"]) if raw is not None else None
            if content:
                reads.append({"thread_id": thread, "turn_id": tid, "call_id": call_id, "rollout_line": line,
                              "artifact_ref": ref, "sha256": art["sha256"], "input_material": ref in inputs,
                              "job_id": art["job_id"], **content})
        for row in db.execute("""SELECT d.*,n.rowid AS seq FROM v6_decisions d LEFT JOIN p0_v6_native_calls n
                USING(thread_id,turn_id,call_id) WHERE d.run_id=? AND d.thread_id=? ORDER BY d.rowid""", (rid, thread)):
            tid, cid = row["turn_id"], row["call_id"]
            enact = json.loads(row["receipt_json"])
            proposal = json.loads(row["request_json"])["decision"]
            if enact.get("outcome") != "adopted":
                continue
            native_seq[thread, tid, cid] = row["seq"]
            line = genuine.get(tid, {}).get("calls", {}).get(cid)
            cited = proposal.get("evidence_refs", [])
            matched = [r["call_id"] for r in reads if r["artifact_ref"] in cited and r["thread_id"] == thread
                       and r["turn_id"] == tid and line and r["rollout_line"] < line]
            adopted.append({"decision_ref": row["decision_ref"], "event_id": enact["event_id"], "thread_id": thread,
                            "turn_id": tid, "call_id": cid, "rollout_line": line,
                            "evidence_refs": cited, "verified_same_turn_read_calls": matched,
                            "rationale": research_report.render_decision(db, rid, rendered_readings, row["decision_ref"],
                                                                          proposal["rationale"]),
                            "operations": [op["spec"]["op"] for op in enact.get("resolved_ops", [])]})
    for decision in adopted:
        end = native_seq.get((decision["thread_id"], decision["turn_id"], decision["call_id"]))
        decision["prior_lead_turn_read_calls"] = [
            r["call_id"] for r in reads if end and decision["rollout_line"]
            and (r["thread_id"], r["turn_id"]) != (decision["thread_id"], decision["turn_id"])
            and r["artifact_ref"] in decision["evidence_refs"] and native_seq[r["thread_id"], r["turn_id"], r["call_id"]] < end]
    closed, final = None, None
    if goal["status"] == "CLOSED":
        close = research_report.completed_close(db, rid)
        final = research_report.closing_decision(db, rid, close)
        closed = pick(close, "decision_ref requested_event committed_event closed_event closed_utc")
        final = {"evidence_refs": final["evidence_refs"], **research_report.render_close(db, rid, final, {})}
    jobs = [dict(row) for row in db.execute(
        "SELECT job_id,capability_id,capability_version,status FROM jobs WHERE run_id=? ORDER BY rowid", (rid,))]
    return {"schema": "gx.slim-study-receipt/1", "run_id": rid, "question": goal["goal"], "goal_version": goal["goal_version"],
            "status": goal["status"], "material_refs": inputs, "lead_turns": lead_turns,
            "official_lead_turns_observed": sum(t["official_model_observed"] for t in lead_turns),
            "lead_reads_verified": reads, "adopted_lead_decisions": sorted(adopted, key=lambda d: d["event_id"]),
            "closure": closed, "final_result": final, "job_count": len(jobs), "jobs": jobs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--sessions", type=Path, help="defaults to WORK/codex/sessions")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    work = args.work.resolve()
    state = work / "state"
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        runs = [r[0] for r in db.execute("SELECT run_id FROM v6_goals")]
        if not args.run_id and len(runs) != 1:
            parser.error("Choose --run-id in a state with multiple Goals")
        result = receipt(db, state, args.sessions or work / "codex/sessions", args.run_id or runs[0])
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
