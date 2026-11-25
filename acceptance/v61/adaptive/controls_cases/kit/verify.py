#!/usr/bin/env python3
"""Check an offline V6 evidence export. Standard library, Python 3.9+."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TYPES = {"open_branch", "add_node", "cancel_node", "stop_branch", "close_run"}
FINISHED = {"completed", "failed", "cancelled"}
PDB_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def no_duplicates(pairs):
    obj = dict(pairs)
    need(len(obj) == len(pairs), "duplicate JSON key")
    return obj


def parse(data):
    return json.loads(data, object_pairs_hook=no_duplicates,
                      parse_constant=lambda value: need(False, "non-finite JSON number"))


def stable(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(obj):
    return sha(stable(obj))


def name(value):
    need(isinstance(value, str) and value.strip(), "missing nonempty ID or name")
    return value


def count(value):
    need(type(value) is int and value >= 0, "expected nonnegative integer")
    return value


def index(rows, field):
    need(isinstance(rows, list), "expected list of rows")
    keyed = {name(row[field]): row for row in rows}
    need(len(keyed) == len(rows), "duplicate " + field)
    return keyed


def blob(root, ref):
    rel = Path(name(ref["file"]))
    path = (root / rel).resolve()
    need(not rel.is_absolute() and ".." not in rel.parts and root in path.parents,
         "source file outside export")
    data = path.read_bytes()
    need(sha(data) == ref["sha256"], "source digest mismatch for " + str(rel))
    return data


def pointer(obj, path):
    """RFC 6901 object keys and array positions; empty string selects the root."""
    if path == "":
        return obj
    need(isinstance(path, str) and path.startswith("/"), "invalid JSON pointer")
    for part in path[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(obj, list):
            need(key.isascii() and key.isdecimal() and str(int(key)) == key, "invalid array index")
            obj = obj[int(key)]
        else:
            obj = obj[key]
    return obj


def document(root, ref):
    return pointer(parse(blob(root, ref)), ref.get("pointer", ""))


def state(s):
    """A read-only projection during a quiescent submission window."""
    jobs = index(s["jobs"], "job_id")
    index(s["jobs"], "operation_key")
    charges = index(s["submission_ledger"], "ledger_id")
    by_job = index(s["submission_ledger"], "job_id")
    need(set(by_job) == set(jobs), "need one submission charge per accepted Job")
    for jid, charge in by_job.items():
        need(charge["operation_key"] == jobs[jid]["operation_key"] and
             count(charge["units"]) == 1, "receipt / ledger operation mismatch")
    spent = count(s["spent_job_submissions"])
    need(spent == len(charges) and spent <= count(s["job_submission_limit"]), "budget mismatch")
    name(s["head"])
    need(isinstance(s["science"], dict) and bool(s["science"]), "missing science projection")
    return jobs, charges


def retained(old, new):
    need(old.keys() <= new.keys(), "historical job or ledger row disappeared")
    for key in old:
        need(old[key] == new[key], "immutable ledger entry changed")


def valid_observed(root, packet, before, cited):
    need(packet["kind"] == "observed_structure" and
         sha(blob(root, packet["coordinates"])) == PDB_SHA, "expected measured 2RE9 source bytes")
    jobs = index(before["jobs"], "job_id")
    for key in cited:
        obs = index(packet["observations"], "id")[key]
        produced = jobs[name(obs["source_job_id"])]
        need(produced["status"] == "completed" and produced["input_sha256"] == PDB_SHA,
             "observation source is not a finished 2RE9 Job")
        file = produced["result"]
        need(obs["result_sha256"] == file["sha256"], "observation cites another Job result")
        measured = pointer(parse(blob(root, file)), obs["pointer"])
        need(isinstance(measured, (dict, list)) and bool(measured) and
             stable(measured) == stable(obs["value"]), "observation differs from raw result")


def requested_pairs(request, paths):
    values = set()
    need(isinstance(paths, list), "expected list of request pointers")
    for path in paths:
        value = pointer(request, path)
        if isinstance(value, list) and len(value) == 2 and all(v in {"A", "B", "C"} for v in value):
            value = "/".join(sorted(value))
        need(isinstance(value, str) and value in {"A/B", "A/C", "B/C"}, "unknown target in native request")
        values.add(value)
    return values


def realized_stops(root, intent, start, end, reopened, decision_id):
    requested = {}
    for op in intent["ops"]:
        if op["op"] == "stop_branch":
            key = ("branch", name(op["branch"]))
        elif op["op"] == "close_run":
            key = ("run", end["run_id"])
        else:
            continue
        need(key not in requested, "duplicate stop target")
        requested[key] = digest(op)

    recorded = set()
    for ref in end.get("stop_receipts", []):
        event = document(root, ref)
        key = (event["subject_kind"], event["subject_id"])
        need(event["run_id"] == end["run_id"] and key in requested and key not in recorded,
             "wrong run or subject in stop event")
        need(type(event["event_id"]) in (int, str) and str(event["event_id"]).strip(), "missing stop event ID")
        cause = event["payload_json"]
        if isinstance(cause, str):
            cause = parse(cause)
        need(cause["decision_id"] == decision_id and cause["op_digest"] == requested[key],
             "stop event belongs to another decision or operation")
        tag = key[0] + ":" + key[1]
        before = name(event["prev_state"]).lower()
        after = name(event["new_state"]).lower()
        need(before in {"active", "open", "running", "waiting", "pending", "draining"}
             and after in {"stopped", "closed", "cancelled", "completed"}
             and start["scope_states"][tag] == before and end["scope_states"][tag] == after
             and reopened["scope_states"][tag] == after,
             "stop was not realized and durable across reopen")
        recorded.add(key)
    need(recorded == requested.keys(), "missing durable stop event")
    return recorded


def verify_run(root, row, suite, mode):
    run_id = name(row["run_id"])
    start, turn, commit, end, reopened = [
        document(root, row[k]) for k in ("before", "turn", "commit", "settled", "reopened")]
    records = [start, turn, commit, end, reopened]
    orders = [count(r["capture_order"]) for r in records]
    need(all(r["run_id"] == run_id for r in records) and orders == sorted(set(orders)),
         "cross-run or unordered capture")
    packet = document(root, row["packet"])
    pid = name(packet["packet_id"])
    initial = document(root, row["initial"])
    goal = document(root, row["goal"])
    need(initial["goal_digest"] == digest(goal) and initial["goal_id"] == goal["id"]
         and start["logical_head"] == initial["graph_version"] and start["base_digest"] == digest(initial)
         and packet["base_digest"] == digest(initial), "goal, base or head mismatch")
    if mode == "controls":
        need(suite["goal_digest"] == digest(goal) and suite["base_digest"] == digest(initial)
             and suite["packet_digests"].get(pid) == digest(packet), "unknown or edited matched packet")

    # The native tool arguments are separately hash-pinned bytes, not a summary
    # typed by a reviewer. A pointer may select a serialized JSON argument.
    intent = document(root, turn["tool_arguments"])
    if isinstance(intent, str):
        intent = parse(intent)
    need(intent["mode"] in {"amend", "resolve"} and isinstance(intent["ops"], list)
         and all(op["op"] in TYPES for op in intent["ops"]), "unrecognized decision operations")
    need((intent["mode"] == "amend") == bool(intent["ops"]), "mode / operations mismatch")
    view = document(root, turn["presented_evidence"])
    need(view["packet_digest"] == digest(packet) and view["goal_digest"] == digest(goal)
         and view["base_digest"] == digest(initial), "wrong evidence supplied to turn")
    alias = view["evidence_map"]
    observed = index(packet["observations"], "id")
    need(isinstance(alias, dict) and len(set(alias.values())) == len(alias)
         and set(alias.values()) == observed.keys(), "incomplete or ambiguous native evidence mapping")
    visible = index(view["visible_observations"], "evidence_ref")
    need(visible.keys() == alias.keys(), "visible evidence refs differ from packet")
    for native_ref, obs_id in alias.items():
        source = observed[obs_id]
        expected = source["measurement"] if mode == "controls" else source["value"]
        need(stable(visible[native_ref]["value"]) == stable(expected), "model-visible measurement differs")
    cites = intent["evidence_refs"]
    need(isinstance(cites, list) and cites and all(isinstance(c, str) for c in cites)
         and len(set(cites)) == len(cites) and set(cites) <= (set(alias) | set(start.get("prior_evidence_refs", [])))
         and any(c in alias for c in cites),
         "invalid evidence citation")
    name(intent["rationale"])
    need(isinstance(intent["reconsider_when"], list) and intent["reconsider_when"], "missing re-evaluation condition")
    did = name(turn["decision_id"])
    need(turn["packet_digest"] == digest(packet) and turn["base_head"] == start["head"] and
         commit["decision_id"] == did and commit["turn_id"] == name(turn["turn_id"]) and
         commit["intent_digest"] == digest(intent) and commit["base_head"] == start["head"] and
         end["head"] == name(commit["result_head"]) and end["decision_id"] == did,
         "turn, commit, head or outcome lineage mismatch")
    if intent["mode"] == "resolve":
        need(commit["base_head"] == commit["result_head"], "resolve changed execution head")
    else:
        need(commit["base_head"] != commit["result_head"], "amend did not advance execution head")

    old, old_ledger = state(start)
    new, new_ledger = state(end)
    retained(old_ledger, new_ledger)
    need(start["job_submission_limit"] == end["job_submission_limit"] and old.keys() <= new.keys(),
         "hard limit changed or historical job missing")
    for jid in old:
        for key in ("operation_key", "branch", "request_sha256"):
            need(new[jid][key] == old[jid][key], "existing Job identity changed")
    if mode == "controls":
        protected = {jid for jid, job in old.items() if job["branch"] == "citation-index"}
        need(protected and protected == {jid for jid, job in new.items() if job["branch"] == "citation-index"},
             "control branch silently replaced")
        for jid in protected:
            old_status, new_status = old[jid]["status"], new[jid]["status"]
            allowed = {"queued": {"queued", "running", "completed"}, "running": {"running", "completed"},
                       "completed": {"completed"}}
            need(new_status in allowed.get(old_status, set()), "control branch cancelled or rewound")
    else:
        valid_observed(root, packet, start, [alias[c] for c in cites if c in alias])

    effects = index(commit["dispatches"], "operation_key")
    added = {job["operation_key"]: job for jid, job in new.items() if jid not in old}
    need(effects.keys() == added.keys(), "commissioned and settled operation sets differ")
    introduced = {op["node"]: digest(op) for op in intent["ops"] if op["op"] == "add_node"}
    for key, job in added.items():
        plan = effects[key]
        need(plan["node"] in introduced and plan["node_digest"] == introduced[plan["node"]],
             "dispatch does not correspond to a node in the native model call")
        need(job["decision_id"] == did and job["request_sha256"] == plan["request_sha256"]
             and job["branch"] == plan["branch"] and job["status"] in FINISHED,
             "operation did not settle under this decision")
        raw_request = blob(root, job["request"])
        need(sha(raw_request) == job["request_sha256"], "request bytes differ")
        if mode == "controls" and plan["branch"] != "provenance":
            need(requested_pairs(parse(raw_request), plan["pair_pointers"]) == set(plan["chain_pairs"]),
                 "scored target differs from actual request")
        need(bool(blob(root, job["result"])), "empty terminal receipt")
    if start["science"] != end["science"]:
        claim = document(root, end["science_receipt"])
        need(claim["decision_id"] == did and name(claim["claim_revision_id"]) and
             claim["science"] == end["science"], "scientific effect has no matching claim revision")
    stops = realized_stops(root, intent, start, end, reopened, did)
    if mode == "observed":
        need(bool(stops) or start["science"] != end["science"] or any(
            j["status"] == "completed" for j in added.values()),
            "observed witness has no realized stop, scientific change or completed follow-up")
    if mode == "controls" and pid == "p63":
        need(start["science"] == end["science"] and stops <= {("branch", "provenance")} and all(e["branch"] == "provenance"
             for e in effects.values()), "neutral metadata event changed science or commissioned scientific work")

    # Same read-only projection after restart, new process boot ID. Resuming a
    # fully settled window should not trigger another submission or charge.
    need(name(end["boot_id"]) != name(reopened["boot_id"]), "same boot, cold reopen untested")
    for key in ("head", "decision_id", "science", "job_submission_limit", "spent_job_submissions"):
        need(end[key] == reopened[key], "changed across reopen: " + key)
    reopen_jobs, reopen_ledger = state(reopened)
    need(new.keys() == reopen_jobs.keys() and new_ledger == reopen_ledger, "jobs or charges changed across reopen")
    for jid, old_job in new.items():
        resumed = reopen_jobs[jid]
        if old_job["status"] in FINISHED:
            need(old_job == resumed, "terminal job changed across reopen")
        else:
            for field in ("job_id", "operation_key", "request_sha256", "branch"):
                need(old_job[field] == resumed[field], "in-flight job identity changed across reopen")
            allowed = {"queued": {"queued", "running", "completed"}, "running": {"running", "completed"}}
            need(resumed["status"] in allowed.get(old_job["status"], set()), "unexpected in-flight state")
    settings = document(root, row["profile"])
    need(isinstance(settings, dict) and bool(settings), "empty fixed model/tool/budget profile")
    shape = (start["science"], start["job_submission_limit"], start["spent_job_submissions"],
             sorted((j["branch"], j["status"]) for j in old.values()), settings)
    effect = None
    if mode == "controls":
        # Ignore prose, case labels, job IDs and the model's command verbs.
        # A shared verb may yield distinct scopes or different measured targets.
        science = end["science"]
        level = science["level"]
        need(level in {"open", "limited_support", "contested", "insufficient", "mixed", "refuted"},
             "unknown reviewer projection for claim standing")
        pairs = set(science["chain_pairs"])
        targets = {t for plan in effects.values() if plan["branch"] != "provenance" for t in plan["chain_pairs"]}
        need(pairs | targets <= {"A/B", "A/C", "B/C"}, "unknown chain pair in effect projection")
        effect = (level, sorted(pairs), sorted(targets), sorted(
            "run" if kind == "run" else subject for kind, subject in stops if subject != "provenance"))
    return pid, run_id, name(turn["thread_id"]), did, name(turn["turn_id"]), digest(shape), effect


def verify(root, anchor):
    root, anchor = Path(root).resolve(), Path(anchor).resolve()
    need(root not in anchor.parents, "provide the reviewer's external anchor")
    archive = (root / "bundle.json").read_bytes()
    trusted = parse(anchor.read_bytes())
    need(sha(archive) == trusted["bundle_sha256"], "bundle differs from detached anchor")
    data = parse(archive)
    need(data["version"] == 6 and data["mode"] in {"controls", "observed"}, "unknown export format")
    mode = data["mode"]
    suite = parse((HERE / "reviewer/suite.json").read_bytes())
    rows = [verify_run(root, row, suite, mode) for row in data["runs"]]
    if mode == "controls":
        need({x[0] for x in rows} == set(suite["packet_digests"]) and len(rows) == 3,
             "expected three matched controls")
        need(len({x[5] for x in rows}) == 1, "different initial conditions or model/tool/budget profiles")
        effects = {x[0]: x[6] for x in rows}
        need(effects["p17"] != effects["p42"], "opposed observations produced equivalent measured effects")
    else:
        need(len(rows) == 1, "expected one observed protein witness")
    for n in range(5):
        need(len({x[n] for x in rows}) == len(rows), "reused packet, run, session, decision or turn")
    return mode, len(rows)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("source_dir", type=Path)
    cli.add_argument("--anchor", type=Path, required=True)
    args = cli.parse_args()
    try:
        mode, n = verify(args.source_dir, args.anchor)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"EXPORT_CONSISTENT mode={mode} runs={n}; provider, isolation and scientific review require independent evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
