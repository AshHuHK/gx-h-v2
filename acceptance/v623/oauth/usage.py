"""Read-only OAuth usage reconciliation for an enrolled GX Run and its official native threads."""
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from gx_core.p0 import metering


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _count(value):
    return type(value) is int and value >= 0


def _rollouts(home, owned):
    """Keep official identifiers, attribution, confirmed ends and counts. Discard all content."""
    by_thread = {}
    possible_children = {}
    names = [d for d in (home / "sessions", home / "archived_sessions") if d.is_dir()]
    for directory in names:
        for file in directory.rglob("rollout-*.jsonl"):
            if not any(file.stem.endswith("-" + t) for t in owned):
                # A process can die after a native spawn and before GX discovers the child.
                # Only inspect the slot-local header. Unrelated sessions do not enter the report.
                with file.open(encoding="utf-8") as stream:
                    try:
                        header = json.loads(stream.readline())
                    except (ValueError, UnicodeError):
                        continue
                if not isinstance(header, dict) or not isinstance(header.get("payload"), dict):
                    continue
                meta = header["payload"]
                src = meta.get("source")
                subagent = (src.get("subagent") or src.get("subAgent")) if isinstance(src, dict) else None
                spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
                if header.get("type") == "session_meta" and meta.get("id") and spawn.get("parent_thread_id"):
                    possible_children[meta["id"]] = spawn["parent_thread_id"]
                continue
            turns, current, thread, provider, session = {}, None, None, None, None
            errors = set()
            with file.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        errors.add("INCOMPLETE_ROLLOUT")
                        break
                    if not isinstance(record, dict):
                        errors.add("MALFORMED_NATIVE_RECORD")
                        break
                    p = record.get("payload") or {}
                    if not isinstance(p, dict):
                        continue
                    kind, event = record.get("type"), p.get("type")
                    if kind == "session_meta":
                        thread, provider = p.get("id"), p.get("model_provider")
                        session = p.get("session_id")
                        if thread not in owned:
                            break
                    if kind == "turn_context" or (kind == "event_msg" and event == "task_started"):
                        current = p.get("turn_id")
                        if not current:
                            errors.add("MISSING_NATIVE_TURN_ID")
                            continue
                        row = turns.setdefault(current, {})
                        for field in ("model", "effort", "root_turn_id"):
                            if p.get(field):
                                row[field] = p[field]
                    if current and kind == "response_item":
                        # A tool reply, user abort marker or rate-limit snapshot is not new model output.
                        if (event == "message" and p.get("role") == "assistant") or event in (
                                "reasoning", "function_call", "custom_tool_call", "web_search_call",
                                "local_shell_call", "image_generation_call"):
                            turns[current]["unpriced_output"] = True
                    if current and kind == "token_usage_record":
                        row = turns[current]
                        rid = p.get("response_id")
                        amount = (p.get("usage") or {}).get("total_tokens")
                        if (not isinstance(rid, str) or not rid or not _count(amount) or
                            (p.get("thread_id"), p.get("turn_id"), p.get("session_id"), p.get("root_turn_id")) !=
                            (thread, current, session, row.get("root_turn_id")) or not session or not row.get("root_turn_id")):
                            row["receipt_conflict"] = True
                        else:
                            key = _sha(rid)
                            receipts = row.setdefault("keyed_responses", {})
                            if key in receipts:
                                # Replaying a receipt for an earlier response cannot price a new emission.
                                if receipts[key] != amount:
                                    row["receipt_conflict"] = True
                            else:
                                receipts[key] = amount
                                if sum(receipts.values()) != (p.get("turn_token_usage") or {}).get("total_tokens"):
                                    row["receipt_conflict"] = True
                                row["unpriced_output"] = False
                                row["awaiting_token_count"] = True
                                row["last_response_amount"] = amount
                                row["last_response_total"] = (p.get("thread_token_usage") or {}).get("total_tokens")
                    if kind != "event_msg":
                        continue
                    if event in ("turn_aborted", "task_complete"):
                        identity = p.get("turn_id")
                        if identity:
                            turns.setdefault(identity, {})["end"] = event
                    elif event == "token_count" and current and isinstance(p.get("info"), dict):
                        n = (p["info"].get("total_token_usage") or {}).get("total_tokens")
                        if _count(n):
                            row = turns.setdefault(current, {})
                            if row.get("awaiting_token_count"):
                                if (row["last_response_total"] != n
                                        or row["last_response_amount"] != (p["info"].get("last_token_usage") or {}).get("total_tokens")):
                                    row["receipt_conflict"] = True
                                row["awaiting_token_count"] = False
                            elif row.get("keyed_responses") or row.get("unpriced_output"):
                                # No new keyed receipt backs this mirror, even if its cumulative total stayed flat.
                                row["unpaired_count"] = True
                            if "total" in row and n < row["total"]:
                                errors.add("NONMONOTONIC_NATIVE_COUNT")
                            row["total"] = n
            if thread in owned:
                projection = {"provider": provider, "turns": turns, "issues": sorted(errors)}
                if thread in by_thread and by_thread[thread] != projection:
                    projection["issues"].append("CONFLICTING_NATIVE_HISTORY")
                by_thread[thread] = projection
    # A provider response ID cannot belong to two roles/turns. Ambiguous receipts
    # stay out of the observed subtotal as well as the final total.
    seen = defaultdict(list)
    for session in by_thread.values():
        for turn in session["turns"].values():
            for receipt in turn.get("keyed_responses", {}):
                seen[receipt].append(turn)
    for key, owners in seen.items():
        if len(owners) > 1:
            for owner in owners:
                owner["receipt_conflict"] = True
                del owner["keyed_responses"][key]
    linked = set(owned)
    while discovered := {thread for thread, parent in possible_children.items() if parent in linked} - linked:
        linked.update(discovered)
    return by_thread, sorted(linked - owned)


def _account(slot):
    path = slot / "oauth-account.json"
    if path.is_file():
        try:
            source = json.loads(path.read_bytes())
            state = source.get("subscription_usage_state")
            if state in {"INCLUDED_AVAILABLE", "RATE_LIMITED", "ADDITIONAL_CHARGE", "UNKNOWN"}:
                return {"state": state, "ready": source.get("status") == "READY"}
        except (ValueError, AttributeError):
            pass
    return {"state": "UNKNOWN", "ready": False}


def _crosscheck(history, thread, turn, cumulative, *, owner=None, role=None):
    session = history.get(thread, {})
    official = session.get("turns", {}).get(turn, {})
    issues = list(session.get("issues", []))
    if official.get("unpriced_output") or official.get("unpaired_count"):
        issues.append("UNCONFIRMED_RESPONSE_TAIL")
    if official.get("receipt_conflict"):
        issues.append("CONFLICTING_PROVIDER_RESPONSE")
    if not official.get("end"):
        issues.append("NO_OFFICIAL_TERMINAL_RECEIPT")
    if not _count(cumulative) or official.get("total") != cumulative:
        issues.append("UNCONFIRMED_CUMULATIVE_COUNT")
    if role and (session.get("provider"), official.get("model"), official.get("effort")) != (
            role["provider"], role["model"], role["effort"]):
        issues.append("OFFICIAL_ROLE_MISMATCH")
    if owner and official.get("root_turn_id") != owner:
        issues.append("UNATTRIBUTED_DESCENDANT")
    return issues


def _covered(history, thread, turn):
    receipts = history.get(thread, {}).get("turns", {}).get(turn, {}).get("keyed_responses", {})
    return {"covered_response_count": len(receipts),
            "covered_response_tokens": sum(receipts.values()) if receipts else None}


def _audit(db, slot, run_id):
    roots = {r["thread_id"]: r for r in db.execute(
        "SELECT thread_id,provider,model,effort,role FROM p0_roots WHERE run_id=? AND thread_id IS NOT NULL",
        (run_id,))}
    child_meta = {r["child_id"]: r for r in db.execute("SELECT * FROM p0_native_child_meters WHERE run_id=?",
                                                       (run_id,))}
    history, undiscovered = _rollouts(slot / "codex", roots.keys() | child_meta.keys())
    ledger = list(db.execute("""SELECT d.*,a.confirmed_tokens FROM p0_meter_native_turns d
        LEFT JOIN p0_meter_receipt_adjustments a USING(reservation_ref)
        WHERE d.run_id=? ORDER BY d.sequence""", (run_id,)))
    unknown, pending, details, child_turns = [], [], [], defaultdict(list)
    unknown.extend({"reason": "UNDISCOVERED_OFFICIAL_CHILD", "thread": thread} for thread in undiscovered)
    keyed = {(r["thread_id"], r["turn_id"]) for r in ledger}
    for thread, meta in child_meta.items():
        if not meta["metadata_verified"] or meta["awaiting_turn"] or meta["root_id"] not in roots:
            unknown.append({"reason": "UNCONFIRMED_CHILD_BINDING", "thread": thread, "owner": meta["owner_turn"]})
        for child in db.execute("SELECT * FROM p0_native_child_usage WHERE child_id=? ORDER BY turn_id", (thread,)):
            key = (meta["root_id"], child["owner_turn"])
            if key not in keyed:
                unknown.append({"reason": "UNRESERVED_CHILD_TURN", "thread": thread, "turn": child["turn_id"]})
            child_turns[key].append(child)
            keyed.add((thread, child["turn_id"]))
    for thread, session in history.items():
        for turn in session["turns"]:
            if (thread, turn) not in keyed:
                unknown.append({"reason": "UNRESERVED_OFFICIAL_TURN", "thread": thread, "turn": turn})
    for r in db.execute("""SELECT u.thread_id,u.turn_id FROM p0_root_turn_usage u
            JOIN p0_roots p USING(thread_id) WHERE p.run_id=?""", (run_id,)):
        if tuple(r) not in keyed:
            unknown.append({"reason":"UNRESERVED_GX_TURN", "thread":r[0], "turn":r[1]})
    confirmed = 0
    for r in ledger:
        thread, turn = r["thread_id"], r["turn_id"]
        identity = {"thread": thread, "turn": turn, "reservation": r["reservation_ref"]}
        role = roots.get(thread)
        row = {**identity, **({key: role[key] for key in ("role", "model", "provider", "effort")} if role else {}),
               "confidence": r["confidence"], "ledger_state": r["budget_status"]}
        issues = []
        if r["state"] != "acknowledged" or not turn or thread not in roots:
            issues.append("UNATTRIBUTED_OR_UNACKNOWLEDGED_REQUEST")
        amount = r["confirmed_tokens"] if r["confirmed_tokens"] is not None else r["settled_tokens"]
        unsettled = r["budget_status"] == "HELD" and not r["terminal_status"]
        if unsettled:
            pending.append(identity)
            # GX may have gone down before its terminal notification. The original
            # official abort still proves a missing final response, while its hold stays intact.
            official = history.get(thread, {}).get("turns", {}).get(turn, {})
            if official.get("end") and (official.get("unpriced_output") or official.get("unpaired_count")):
                issues.append("UNCONFIRMED_RESPONSE_TAIL")
        elif r["confidence"] not in ("PROVIDER_CONFIRMED", "PROVIDER_RECONCILED") or (
                r["attribution"] != "TURN" or r["budget_status"] not in ("SETTLED", "RELEASED") or not _count(amount)):
            issues.append("UNRESOLVED_OR_ESTIMATED_USAGE")
        if not unsettled:
            issues.extend(_crosscheck(history, thread, turn, r["total_tokens"], role=roots.get(thread)))
            if not (_count(r["total_tokens"]) and _count(r["prior_tokens"]) and _count(r["measured_tokens"])
                    and r["total_tokens"] - r["prior_tokens"] == r["measured_tokens"]):
                issues.append("UNCONFIRMED_ROOT_INTERVAL")
            if not r["terminal_status"] or not r["children_known"] or metering.receipt_conflict(db, thread, turn):
                issues.append("INCOMPLETE_GX_RECEIPTS")
        children = []
        descendant_tokens = 0
        for c in child_turns[(thread, turn)]:
            child_issues = _crosscheck(history, c["child_id"], c["turn_id"], c["total_tokens"], owner=turn)
            if not c["terminal"] or not c["children_known"] or not _count(c["prior_tokens"]) or not (
                    _count(c["measured_tokens"]) and _count(c["total_tokens"])
                    and c["total_tokens"] - c["prior_tokens"] == c["measured_tokens"]):
                child_issues.append("INCOMPLETE_CHILD_USAGE")
            issues.extend(child_issues)
            children.append({"thread": c["child_id"], "turn": c["turn_id"],
                             "tokens": None if child_issues else c["measured_tokens"],
                             **_covered(history, c["child_id"], c["turn_id"]),
                             "receipt_sha256": _sha(history.get(c["child_id"], {}).get("turns", {}).get(c["turn_id"], {}))})
            if _count(c["measured_tokens"]):
                descendant_tokens += c["measured_tokens"]
        if not unsettled and (not _count(r["measured_tokens"]) or
                              r["measured_tokens"] + descendant_tokens != amount):
            issues.append("OWNER_AMOUNT_DISAGREES_WITH_ROLE_TOTALS")
        if issues:
            unknown.append({**identity, "reasons": sorted(set(issues))})
        proven = not unsettled and not issues
        if proven:
            confirmed += amount
        row.update(**_covered(history, thread, turn), tokens=amount if proven else None, descendants=children, reserved_tokens=r["token_budget"],
                   receipt_sha256=_sha(history.get(thread, {}).get("turns", {}).get(turn, {})))
        details.append(row)
    if not ledger:
        unknown.append({"reason": "NO_USAGE_RECEIPTS"})
    return details, confirmed, pending, unknown


def reconcile(slot, run_id):
    """Inspect a single native Run; never starts a host, reserves, settles or retries a request."""
    slot = Path(slot)
    account = _account(slot)
    records, confirmed, pending, unknown = [], None, [], []
    try:
        uri = (slot / "state/gx.db").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                unknown.append({"reason": "RUN_NOT_FOUND"})
            else:
                records, confirmed, pending, unknown = _audit(db, slot, run_id)
    except (OSError, UnicodeError, ValueError, sqlite3.Error):
        unknown.append({"reason": "RECEIPTS_UNAVAILABLE"})
    accounting = "UNKNOWN" if unknown else "PENDING" if pending else "SETTLED"
    exact = confirmed if accounting == "SETTLED" else None
    status = "ADDITIONAL_CHARGE" if account["state"] == "ADDITIONAL_CHARGE" else accounting
    safe = status == "SETTLED" and account == {"state": "INCLUDED_AVAILABLE", "ready": True}
    proof = {"run_id": run_id, "account": account, "turns": records, "pending_turns": pending,
             "unresolved": unknown}
    responses = [item for row in records for item in [row, *row["descendants"]]]
    covered = [r["covered_response_tokens"] for r in responses if r["covered_response_tokens"] is not None]
    return {**proof, "covered_response_count": sum(r["covered_response_count"] for r in responses),
            "covered_response_tokens": sum(covered) if covered else None, "schema": "gx.v623.oauth-usage/2", "status": status, "usage_status": accounting,
            "total_tokens": exact, "confirmed_tokens": confirmed if any(r["tokens"] is not None for r in records) else None,
            "can_continue": safe, "provider_usd": None, "cost_status": "UNKNOWN",
            "receipt_sha256": _sha(proof)}
