"""Independently blind-grade the original field reports and expose physical and timing receipts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import time
from support import HERE, P, read, save, sha
from run import instant
from common import blind_text
from native import operate
from trial import sql


def source_in(value, source):
    if isinstance(value, str):
        if source in value:
            return True
        if source.splitlines()[-1].split()[-1] in value:
            for prefix in ("GX research brief", ""):
                try:
                    obj = json.loads(value.removeprefix(prefix).lstrip())
                except (TypeError, ValueError):
                    continue
                return source_in(obj, source)
    if isinstance(value, list):
        return any(source_in(v, source) for v in value)
    if isinstance(value, dict):
        return any(source_in(v, source) for v in value.values())
    return False


def sessions(folder, lead_threads):
    late = folder / "late-delivery.json"
    when = read(late)["at"] if late.exists() else None
    source = (P / "sites.rdb").read_text().rstrip("\n")
    feed = []
    totals = {}
    ledger = {}
    unique_turns = {}
    for path in sorted((folder / "sessions").rglob("*.jsonl")):
        thread = ""
        for line_no, line in enumerate(path.open(), 1):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = r.get("payload", {})
            event = m.get("type")
            kind = r.get("type")
            ts = r["timestamp"]
            if kind == "session_meta":
                thread = m.get("id", "")
            if kind == "turn_context" and m.get("turn_id"):
                t = m["turn_id"]
                unique_turns.setdefault(
                    t,
                    {
                        "thread": thread,
                        "turn": t,
                        "model": m.get("model"),
                        "effort": m.get("effort"),
                        "first_context": ts,
                    },
                )
            if kind != "event_msg":
                continue
            t = m.get("turn_id")
            if t and event in ("task_started", "task_complete", "turn_aborted"):
                ledger.setdefault(t, {})[event] = ts
            if event == "token_count":
                val = ((m.get("info") or {}).get("total_token_usage") or {}).get("total_tokens")
                if thread and isinstance(val, int):
                    totals[thread] = max(val, totals.get(thread, 0))
            if event == "item_completed" and when and thread in lead_threads and instant(ts) >= when - 1:
                item = m.get("item", {})
                if item.get("type") == "UserMessage":
                    body = item.get("content", [])
                elif item.get("type") == "DynamicToolCall" and item.get("success") is True:
                    body = item.get("content_items", [])
                else:
                    continue
                if source_in(body, source):
                    feed.append(
                        {
                            "at": ts,
                            "thread": thread,
                            "turn": t,
                            "line": line_no,
                            "session": str(path.relative_to(folder)),
                            "kind": item["type"],
                        }
                    )
    intervals = []
    for t, u in unique_turns.items():
        tlog = ledger.get(t, {})
        a = tlog.get("task_started")
        b = tlog.get("task_complete") or tlog.get("turn_aborted")
        u.update(start=a, end=b)
        if a and b and instant(b) >= instant(a):
            intervals.append((instant(a), instant(b)))
    return {
        "turns": list(unique_turns.values()),
        "observed_token_subtotals_by_thread": totals,
        "observed_tokens_sum": sum(totals.values()) if totals else None,
        "accounting": "largest observed official cumulative value per thread; approximate. Interrupted tails can be missing. No additional payment.",
        "lead_received_inventory": min(feed, key=lambda x: x["at"], default=None),
        "model_turn_active_union_seconds_observed": union(intervals),
        "complete_turn_intervals": len(intervals),
        "turn_intervals_expected": len(unique_turns),
    }, intervals


def union(intervals):
    if not intervals:
        return 0.0
    joined = []
    for a, b in sorted(intervals):
        if joined and a <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], b))
        else:
            joined.append((a, b))
    return round(sum(b - a for a, b in joined), 3)


def physical_cost(folder):
    jobs = []
    intervals = []
    for path in sorted((folder / "state/jobs").glob("*/runner.json")):
        directory = path.parent
        provider = read(path)
        job_id = directory.name
        spec = read(directory / "spec.json")
        ctx = spec["gx_context"]
        claim = read(directory / "launch.claim")
        out = read(directory / "outcome.json") if (directory / "outcome.json").exists() else {}
        a = out.get("started_at")
        b = out.get("finished_at")
        if a and b:
            intervals.append((instant(a), instant(b)))
        jobs.append(
            {
                "job_id": job_id,
                "method": ctx.get("capability_version"),
                "input_sha256": ctx.get("input_sha256"),
                "request_sha256": ctx.get("request_sha256"),
                "provider": provider,
                "launch_at": claim["at"],
                "provider_receipt": {
                    k: out.get(k)
                    for k in (
                        "started_at",
                        "finished_at",
                        "runner_pid",
                        "runner_started",
                        "tool_pid",
                        "tool_started",
                        "result",
                        "exit_code",
                        "signal",
                        "cpu_user_seconds",
                        "cpu_system_seconds",
                        "wall_seconds",
                        "files",
                    )
                },
                "launch_to_execution_seconds": (
                    round(instant(a) - instant(claim["at"]), 3) if a else None
                ),
                "tool_output_paths": [f["path"] for f in out.get("files", [])],
            }
        )
    return jobs, intervals


def final_report(folder, arm):
    if read(folder / "outcome.json")["outcome"] != "finished":
        return None
    if arm == "stock":
        term = next(
            (
                folder / k
                for k in ("second-terminal.json", "first-terminal.json")
                if (folder / k).exists()
            ),
            None,
        )
        return read(term)["answer"] if term else None
    rid = read(folder / "outcome.json")["facts"]["goal"]["run_id"]
    item = read(folder / "state/research" / rid / "result.json")["final_decision"]
    fin = item.get("rendered") or item
    return "\n\n".join(
        str(x) for x in (fin.get("reason"), fin.get("rationale"), *fin.get("unresolved", [])) if x
    )


def blind_judge(d, answer):
    rubric = read(HERE / "rubric.json")
    item = {
        "type": "object",
        "properties": {
            "mark": {"type": "string", "enum": ["pass", "fail", "not demonstrated"]},
            "source_locator": {"type": "string"},
            "report_support": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["mark", "source_locator", "report_support", "reason"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": dict.fromkeys(rubric, item),
        "required": list(rubric),
        "additionalProperties": False,
    }
    pkt = {
        "question": (P / "question.txt").read_text(),
        "original_daily": read(P / "daily.json"),
        "original_area": (P / "sites.rdb").read_text(),
        "independent_decimal_reference": read(P / "reference.json"),
        "criteria": rubric,
        "anonymous_report": blind_text(answer),
    }
    save(d / "packet.json", pkt)
    if not (d / "judge-terminal.json").exists():
        if (d / "judge-started.json").exists():
            raise RuntimeError("Recover the existing judge thread first")
        save(d / "judge-started.json", {"at": time.time()})
        operate(d, "judge", json.dumps(pkt), judging=True, schema=schema)
    j = read(d / "judge-terminal.json")
    if j["status"] != "completed":
        return {"state": j["status"], "score": None}
    m = json.loads(j["answer"])
    if set(m) != set(rubric):
        raise ValueError("Incomplete blinded assessment")
    return {
        "marks": m,
        "score": [sum(x["mark"] == "pass" for x in m.values()), len(m)],
        "thread": read(d / "thread.json")["id"],
        "turn": j["turn_id"],
        "packet_sha256": sha(d / "packet.json"),
    }


def reattachment(folder, arm, original_job, when, leads):
    if arm != "stock":
        rows = sql(
            folder,
            "SELECT outbox_id,created_at,consumed_at,topic,idempotency_key FROM outbox "
            "WHERE idempotency_key=? AND topic='job.terminal'",
            ("job.terminal:" + original_job["job_id"],),
        )
        return [
            {"type": "GX Job terminal outbox", "original_job_id": original_job["job_id"], **r}
            for r in rows
            if instant(r["created_at"]) >= when
        ]
    evidence = []
    digests = [item["sha256"] for item in original_job["provider_receipt"].get("files") or []]
    for p in (folder / "sessions").rglob("*.jsonl"):
        for i, line in enumerate(p.open(), 1):
            if "observe_measurements" not in line or original_job["job_id"] not in line:
                continue
            try:
                r = json.loads(line)
                m = r.get("payload", {})
                it = m.get("item", {})
            except ValueError:
                continue
            if (
                r.get("type") == "event_msg"
                and m.get("type") == "item_completed"
                and it.get("type") == "DynamicToolCall"
                and it.get("success") is True
                and it.get("tool") == "observe_measurements"
                and instant(r["timestamp"]) >= when
                and any(h in json.dumps(it.get("content_items", [])) for h in digests)
            ):
                evidence.append(
                    {
                        "type": "official successful physical-output read",
                        "thread": m.get("thread_id"),
                        "lead": m.get("thread_id") in leads,
                        "turn": m.get("turn_id"),
                        "call": it.get("id"),
                        "session": str(p.relative_to(folder)),
                        "line": i,
                        "at": r["timestamp"],
                    }
                )
    return evidence


def arm_result(work, arm):
    folder = work / arm
    o = read(folder / "outcome.json")
    jobs, job_times = physical_cost(folder)
    if arm == "stock":
        thread = read(folder / "thread.json")["id"] if (folder / "thread.json").exists() else None
        runs = [thread]
        read_calls = [read(x) for x in (folder / "scientific-calls").glob("*.json")]
        logical = [r for r in read_calls if r["tool"] == "submit_measurement"]
        # A transport can die between physical acceptance and the tool reply. Count the attempted call from the official session.
        offered = {}
        for path in (folder / "sessions").rglob("*.jsonl"):
            for line in path.open():
                if "submit_measurement" not in line:
                    continue
                try:
                    r = json.loads(line)
                    m = r["payload"]
                    it = m.get("item", {})
                except (KeyError, ValueError):
                    continue
                if it.get("type") == "DynamicToolCall" and it.get("tool") == "submit_measurement":
                    offered[it["id"]] = {
                        "thread": m.get("thread_id"),
                        "turn": m.get("turn_id"),
                        "method": it.get("arguments"),
                    }
        logical = {
            "identity": "official native tool-call IDs",
            "requests": list(offered.items()),
            "replies_at_controller": len(logical),
        }
    else:
        thread = (o["facts"].get("goal") or {}).get("run_id")
        runs = [
            r["thread_id"]
            for r in sql(
                folder,
                "SELECT DISTINCT r.thread_id FROM p0_roots r JOIN v6_decision_tasks t ON r.task_id=t.task_id AND t.scope='run'",
            )
        ]
        logical = {
            "identity": "GX admission rows; shared subscriptions separately shown where present",
            "requests": sql(
                folder,
                "SELECT job_id,task_id,idempotency_key,capability_id,capability_version,created_at,submitted_at FROM jobs",
            ),
            "node_requests": sql(folder, """SELECT n.node_id,n.branch_id,n.operation_key,n.status,n.job_id,
                   n.result_ref,a.exec_key,a.reused_from FROM v6_job_attempts a
                   JOIN v6_nodes n USING(node_id) ORDER BY n.rowid"""),
            "subscribe_and_settle_events": sql(folder,"""SELECT event_id,ts,kind,subject_id,
                   json_extract(payload_json,'$.job_id') AS job_id,
                   json_extract(payload_json,'$.physical_owner') AS physical_owner
                   FROM events WHERE kind IN ('v6.node.subscribed','v6.node.settled') ORDER BY event_id"""),
            "in_flight_sharing_implemented":arm == "candidate",
        }
    counts, model_times = sessions(folder, runs)
    interruption = (
        read(folder / "interruption.json") if (folder / "interruption.json").exists() else None
    )
    match = None
    observed = False
    new_exact = None
    adopted = []
    if interruption:
        original = interruption["original_job"]
        match = next((x for x in jobs if x["job_id"] == original["job_id"]), None)
        if match:
            out = match["provider_receipt"]
            observed = (
                interruption["qualified_at_cut"]
                and out.get("finished_at")
                and instant(out["finished_at"]) > interruption["at"]
                and all(
                    out.get(k) == original[k]
                    for k in ("runner_pid", "runner_started", "tool_pid", "tool_started")
                )
            )
            if (folder / "second-launch.json").exists():
                adopted = reattachment(
                    folder, arm, match, read(folder / "second-launch.json")["at"], runs
                )
            # Count only exact request digests (or native method/input physical identity), not unrelated post-restart Jobs.
            new_exact = sum(
                j["job_id"] != original["job_id"]
                and instant(j["launch_at"]) >= interruption["at"]
                and (
                    (match["request_sha256"] and j["request_sha256"] == match["request_sha256"])
                    or (
                        arm == "stock"
                        and not match["request_sha256"]
                        and j["input_sha256"] == match["input_sha256"]
                        and j["method"] == match["method"]
                    )
                )
                for j in jobs
            )
    return {
        "arm": arm,
        "arm_id": thread,
        "outcome": o,
        "pin": read(folder / "began.json")["release"],
        "official": counts,
        "logical": logical,
        "physical": jobs,
        "physical_launches": len(jobs),
        "interruption": interruption,
        "physical_continuity_verified": bool(observed) if interruption else None,
        "resumed_controller_adoption_evidence": adopted,
        "end_to_end_takeover_verified": (
            bool(observed and adopted and new_exact == 0) if interruption else None
        ),
        "extra_launches_for_same_provider_request": new_exact,
        "measurement_execution_union_seconds": union(job_times),
        "control_or_other_observed_gap_seconds": (
            round(o["elapsed_seconds"] - union([*job_times, *model_times]), 3)
            if len(model_times) == counts["turn_intervals_expected"]
            else None
        ),
        "timing_definition": "Measured role intervals may overlap. Turn duration includes synchronous tools; residual is controller, queue and transport jointly, not a pure controller CPU estimate.",
    }


def grade(work, out):
    arms = ["stock", "baseline", "candidate"]
    for arm in arms:
        if not (work / arm / "outcome.json").exists():
            raise RuntimeError("Record each original outcome before blinding")
    key = work / "anonymous-map.json"
    if not key.exists():
        secrets.SystemRandom().shuffle(arms)
        save(key, {"report-" + str(i): a for i, a in enumerate(arms, 1)})
    records = []
    mapping = read(key)

    def grade_one(pair):
        label, arm = pair
        d = work / "anonymous" / label
        txt = final_report(work / arm, arm)
        if not txt:
            return label, arm, None, None
        return label, arm, txt, blind_judge(d, txt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for label, arm, txt, marks in pool.map(grade_one, mapping.items()):
            if txt:
                (out / (arm + ".md")).write_text(txt + "\n")
            records.append({"label": label, "blind_grade": marks, **arm_result(work, arm)})
    save(
        out / "results.json",
        {"series": read(work / "series.json"), "arms": records, "n": 1, "general_effect_size": None},
    )
    return records


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("--out", type=Path, default=HERE / "field")
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    print([(x["arm"], x["blind_grade"] and x["blind_grade"]["score"]) for x in grade(a.work, a.out)])
