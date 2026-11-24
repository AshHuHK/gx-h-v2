"""Read-only cost attribution from scoped receipts, independently of budget settlement."""
from collections import Counter
import json


def _turn_cost(ticket, grant, scoped_lower, confirmed, child_in_flight, disputed, exceeds_receipt):
    estimated = ticket["estimated_at"] is not None
    gaps = []
    if confirmed is None:
        if not ticket["terminal_status"]:
            gaps.append("ROOT_TERMINAL_UNOBSERVED")
        if child_in_flight:
            gaps.append("CHILD_TERMINAL_UNOBSERVED")
        if disputed:
            gaps.append("DISPUTED_RECEIPTS")
        if exceeds_receipt:
            gaps.append("SCOPED_USAGE_EXCEEDS_CONFIRMED_TOTAL")
        gaps.append("INCOMPLETE_END_USAGE")
    risk = "PROVIDER_CONFIRMED" if confirmed is not None else (
        "ESTIMATE_POSSIBLY_UNDERSTATED" if estimated else "UNRESOLVED")
    return {
        "estimate_policy": "ESTIMATED_CONSERVATIVE" if estimated else None,
        "risk": risk,
        "turn_grant_tokens": grant,
        "scoped_observed_lower_bound_tokens": scoped_lower,
        "observed_lower_bound_at_estimate_tokens": ticket["observed_lower_bound"] if estimated else None,
        "original_estimate_tokens": ticket["settled_tokens"] if estimated else None,
        "provider_confirmed_total_tokens": confirmed,
        # A GX grant does not impose a maximum on upstream requests in this Turn.
        "unobserved_tokens_upper_bound": 0 if confirmed is not None else None,
        "evidence_gaps": gaps,
    }


def classified_cost(conn, run_id, threads, reservations, adjustments):
    # Receipts without a snapshot still belong to their verified thread. A native child
    # has no GX reservation of its own. Its owner is solely the funding relationship.
    owners = {r["thread_id"]: r["thread_id"] for r in reservations}
    owners.update((r["thread_id"], r["root_id"]) for r in threads)
    owners.update((r[0], r[0]) for r in conn.execute(
        "SELECT thread_id FROM p0_roots WHERE run_id=? AND thread_id IS NOT NULL", (run_id,)))
    child_in_flight = set()
    for child in conn.execute("SELECT child_id,root_id,owner_turn,metadata_verified,awaiting_turn "
                              "FROM p0_native_child_meters WHERE run_id=?", (run_id,)):
        if child["metadata_verified"]:
            owners[child["child_id"]] = child["root_id"]
        if child["awaiting_turn"] or not child["metadata_verified"]:
            child_in_flight.add((child["root_id"], child["owner_turn"]))
    snapshots = {r["thread_id"]: r["observed_total"] for r in threads}
    raw = {}
    known = {}
    funded_turn = {}
    disputes = set()
    if owners:
        # The native receipt primary key is (thread_id,response_id). Resending a response
        # or using the same response ID in a different thread does not duplicate a cost.
        scope = json.dumps(list(owners))
        for r in conn.execute(
            "SELECT thread_id,turn_id,count(*) AS n,count(CASE WHEN conflicting=0 THEN tokens END) AS received,"
            "sum(CASE WHEN conflicting=0 THEN tokens END) AS tokens,sum(conflicting) AS conflicts "
            "FROM p0_meter_responses WHERE thread_id IN (SELECT value FROM json_each(?)) GROUP BY thread_id,turn_id",
            (scope,)):
            raw[r["thread_id"], r["turn_id"]] = r
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_meter_receipt_intervals'").fetchone():
            disputes = {tuple(r) for r in conn.execute(
                "SELECT thread_id,turn_id FROM p0_meter_receipt_intervals WHERE disputed=1 "
                "AND thread_id IN (SELECT value FROM json_each(?))", (scope,))}
        for r in conn.execute(
            "SELECT thread_id,turn_id,measured_tokens,turn_id,NULL AS child_terminal FROM p0_root_turn_usage "
            "WHERE thread_id IN (SELECT value FROM json_each(?)) UNION ALL "
            "SELECT child_id,turn_id,measured_tokens,owner_turn,terminal FROM p0_native_child_usage "
            "WHERE child_id IN (SELECT value FROM json_each(?))", (scope, scope)):
            known[r[0], r[1]] = r[2]
            funded_turn[r[0], r[1]] = (owners[r[0]], r[3])
            if r["child_terminal"] == 0:
                child_in_flight.add(funded_turn[r[0], r[1]])

    by_turn = []
    totals = Counter()
    unreserved = Counter()
    scoped = Counter()
    paid_turns = {(r["thread_id"], r["turn_id"]) for r in reservations}
    conflicting_turns = set()
    for thread, turn in sorted(raw.keys() | known.keys() | disputes):
        receipt = raw.get((thread, turn))
        conflicts = receipt["conflicts"] if receipt else 0
        disputed = bool(conflicts or (thread, turn) in disputes)
        if disputed:
            conflicting_turns.add(funded_turn.get((thread, turn), (thread, turn)))
        # An official recovered history can cover more requests than the live transport
        # observed. Both totals describe the same Turn and must never be added together.
        sources = [v for v in (receipt["tokens"] if receipt else None,
                               None if disputed else known.get((thread, turn))) if v is not None]
        tokens = max(sources) if sources else None
        if tokens is not None:
            totals[thread] += tokens
            funded = funded_turn.get((thread, turn), (thread, turn))
            if funded in paid_turns:
                scoped[funded] += tokens
            else:
                unreserved[owners[thread]] += tokens
        by_turn.append({"thread_id":thread, "turn_id":turn, "provider_confirmed_tokens":tokens,
                        "response_count":receipt["n"] if receipt else 0,
                        "receipts_with_usage":receipt["received"] if receipt else 0,
                        "disputed":disputed, "conflicting_response_count":conflicts})

    accounts = []
    known_per_root = Counter()
    for thread, root in sorted(owners.items()):
        total = snapshots.get(thread)
        attributed = totals[thread]
        delta = total - attributed if total is not None and total >= attributed else None
        accounts.append({"thread_id":thread, "root_id":root, "thread_confirmed_cumulative":total,
                         "turn_attributed_total":attributed, "unattributed_delta":delta})
        known_per_root[root] += max(total or 0, attributed)

    original = Counter()
    settled = Counter()
    signed = Counter()
    confirmed_late = {a["reservation_ref"]:a["confirmed_tokens"] for a in adjustments if a["basis"] == "TURN_RECEIPT"}
    for item in adjustments:
        signed[item["reservation_ref"]] += item["delta"]
    unresolved = []
    possibly_understated = []
    # Imported deliveries can lack token_budget while the original reservation
    # retains the grant used by the estimator.
    missing_grants = [t["reservation_ref"] for t in reservations if t["token_budget"] is None]
    grants = dict(conn.execute("SELECT reservation_ref,json_extract(amounts_json,'$.tokens') "
                              "FROM budget_reservations WHERE reservation_ref IN (SELECT value FROM json_each(?))",
                              (json.dumps(missing_grants),))) if missing_grants else {}
    for ticket in reservations:
        ref, root = ticket["reservation_ref"], ticket["thread_id"]
        if ticket["settled_tokens"] is not None:
            paid = ticket["settled_tokens"] + signed[ref]
            settled[root] += paid
            if ticket["estimated_at"] and ref not in confirmed_late:
                original[root] += paid
                ticket["usage_class"] = "ESTIMATED"
            elif ticket["confidence"] == "PROVIDER_CONFIRMED" or ref in confirmed_late:
                ticket["usage_class"] = "PROVIDER_CONFIRMED"
                # Receipt-covered tokens in a common funding pool cease to be provisional,
                # including funds that were originally booked on another same-fund Turn.
                if ref in confirmed_late:
                    original[root] += paid - confirmed_late[ref]
            else:
                ticket["usage_class"] = "UNRESOLVED"
        else:
            ticket["usage_class"] = "UNRESOLVED"
        key = root, ticket["turn_id"]
        confirmed = (confirmed_late.get(ref, ticket["settled_tokens"])
                     if ticket["usage_class"] == "PROVIDER_CONFIRMED" else None)
        lower = scoped.get(key)
        exceeds_receipt = confirmed is not None and lower is not None and lower > confirmed
        if key in conflicting_turns or (confirmed is not None and (exceeds_receipt or key in child_in_flight)):
            ticket["usage_class"] = "UNRESOLVED"
            confirmed = None
        if ticket["usage_class"] == "UNRESOLVED":
            unresolved.append(ref)
        ticket["cost_assessment"] = _turn_cost(
            ticket, grants.get(ref, ticket["token_budget"]), confirmed if confirmed is not None else lower,
            confirmed, key in child_in_flight, key in conflicting_turns, exceeds_receipt)
        if ticket["cost_assessment"]["risk"] == "ESTIMATE_POSSIBLY_UNDERSTATED":
            possibly_understated.append(ref)

    # Report only the provisional exposure above the evidence on the same funding root.
    # A snapshot residual has no Turn owner and is never distributed to resize Turn costs.
    original = +original
    estimated = sum(min(amount, max(0, settled[root] - (known_per_root[root] - unreserved[root]))) for root, amount in original.items())
    return {"PROVIDER_CONFIRMED":{"tokens":sum(totals.values())},
            "ESTIMATED":{"tokens":estimated, "provisional_budget_tokens":sum(original.values())},
            "UNRESOLVED":{"tokens":None, "reservation_refs":unresolved},
            "UNATTRIBUTED_DELTA":{"tokens":sum(item["unattributed_delta"] or 0 for item in accounts)},
            "possibly_understated_estimate_refs":possibly_understated,
            "threads":accounts, "turn_receipts":by_turn}
