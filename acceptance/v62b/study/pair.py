"""Custodian-side Goal execution and independent scoring for the private V6.2B pair."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from receipts import HERE, ROOT, cost_detail, database, digest, extract, need, parse, read, sha
from score import adjudicate, assignment_traces, brief_packet, lead_turns, lock_opportunities, matches_assay
import live

PROTOCOL = read(HERE / "protocol.json")
SLOTS = PROTOCOL["slots"]
PROFILE = {"default": {"model": PROTOCOL["model"], "provider": PROTOCOL["provider"],
                       "effort": PROTOCOL["model_effort"], "multi_agent": False}}


def save_new(path, doc):
    """Publish a complete file once, including under competing invocations."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
    try:
        live.save(tmp, doc)
        os.link(tmp, path)
    finally:
        tmp.unlink()


def external(path):
    path = path.resolve()
    need(not path.is_relative_to(ROOT), "Evaluation files belong in external custodian storage")
    return path


def source_pin():
    paths = ["bin/gx-p0", "gx-h/gx_core", "gx-h/contracts", "gx-h/config", "gx-h/plugins/installed",
             "gx-h/adapters", "release-assets/lock.json", "acceptance/v6/native_witness.py",
             "acceptance/v6/verify.py", "acceptance/v6/live.py", "acceptance/v61/adaptive/study_score.py",
             "acceptance/v611", "acceptance/v62b/study"]
    names = subprocess.check_output(["git", "-C", ROOT, "ls-files", "-z", "--cached", "--others",
                                     "--exclude-standard", "--", *paths]).decode().split("\0")
    hashes = {name: live.digest(ROOT / name) for name in names if name and (ROOT / name).is_file()}
    return {"checkout": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip(),
            "manifest_sha256": digest(hashes), "files": hashes}


def common(inputs):
    grant = read(inputs / "grant.json")
    need(grant["research_effort"] == PROTOCOL["research_effort"] and grant["research_modules"],
         "Use the registered deep research effort and declare module access")
    caps = grant["run_budget"]
    need(caps == grant["workspace_budget"] and all(type(v) is int and v >= 0 for v in caps.values())
         and all(caps.get(k, 0) > 0 for k in ("api_calls", "tokens", "job_submissions", "worker_tasks", "wall_hours"))
         and 0 < grant["turn_token_budget"] <= caps["tokens"], "Explicit equal Run and workspace limits required")
    materials = []
    for item in grant["input_files"]:
        path = (inputs / item["path"]).resolve(strict=True)
        need(live.digest(path) == item["sha256"], "Shared raw material digest mismatch")
        materials.append({"name": path.name, "role": item.get("role"), "sha256": item["sha256"]})
    goal = (inputs / "goal.md").read_text().strip()
    need(goal and materials, "Supply a common decision task and raw source pool")
    return dict(goal_sha256=sha(goal.encode()), grant_sha256=live.digest(inputs / "grant.json"),
                materials=materials, caps=caps)


def exposed_metadata():
    historical = read(ROOT / PROTOCOL["history"]["control_registration"])
    return set(historical["suite"]["packet_digests"].values()) | {
        historical["hashes"][f"acceptance/v61/adaptive/controls_cases/{name}.json"]
        for name in PROTOCOL["history"]["exposed_controls"]}


def case_file(inputs, slot):
    path = (inputs / "cases" / (slot + ".json")).resolve(strict=True)
    external(path)
    case = read(path)
    exposed = exposed_metadata()
    need(live.digest(path) not in exposed and digest(case) not in exposed, "Previously exposed control")
    need(set(case) == {"version", "intervention"} and case["version"].strip() and
         case["version"].lower() not in PROTOCOL["history"]["exposed_controls"], "Invalid or exposed case version")
    part = case["intervention"]
    need(isinstance(part, dict) and digest(part) not in exposed and part.get("kind") == "supplement" and
         part.keys() <= {"kind", "text", "source_ref"} and part["text"].strip(), "Expected a raw GX supplement")
    try:
        embedded = parse(part["text"])
    except ValueError:
        embedded = None
    need(digest(embedded) not in exposed and sha(part["text"].encode()) not in exposed,
         "A previously exposed packet was moved inside a supplement")
    return case, dict(version=case["version"], case_sha256=live.digest(path), intervention_sha256=digest(part))


def answer_file(path):
    body = read(external(path))
    need(body["condition"] in PROTOCOL["denominators"] and body["condition"] != "total" and
         isinstance(body["acceptable_actions"], list) and body["acceptable_actions"] and
         all(isinstance(a, str) and a.strip() for a in body["acceptable_actions"]) and
         body["scope_rule"].strip() and body["insufficient_support_rule"].strip(),
         "Specify a condition, prespecified alternatives, scope and unsupported-support rule")
    need(isinstance(body["eligible_measurements"], list) and body["eligible_measurements"] and all(
        rule.keys() == ({"kind", "capability", "parameters", "input_sha256", "output_name", "nature"}
                        if rule["kind"] == "job" else {"kind", "capability", "parameters", "input_sha256"}) and
        (rule["kind"] == "job" and rule["output_name"].strip() and rule["nature"] in ("observation", "analysis")
         or rule["kind"] == "source_read" and rule["parameters"].get("segment_id")) and isinstance(rule["parameters"], dict) and
        len(rule["input_sha256"]) == 64 and rule["capability"].strip() for rule in body["eligible_measurements"]),
        "Prespecify source and method selectors for the assigned inventory")
    return body


def opening(db, rid):
    from gx_core.domain.ids import canonical_json
    from gx_core.p0 import v6_native
    from gx_core.p0.roots import WORKER_TOOLS
    first, = db.execute("SELECT t.input_json FROM tasks t JOIN v6_decision_tasks d USING(task_id,run_id) "
                        "WHERE t.run_id=? AND d.scope='run' ORDER BY t.rowid LIMIT 1", (rid,)).fetchall()
    brief = parse(first[0])["brief"]
    need(brief["head"] is None and not any(brief[k] for k in ("nodes", "observations", "module_reports")),
         "Opening task already contains experimental results")
    actual = v6_native.PREFIX + canonical_json(brief)
    tools = v6_native.tool_specs(WORKER_TOOLS, brief, conn=db, run_id=rid)
    materials = [{k: m[k] for k in ("name", "role", "sha256")} for m in brief["materials"]]
    matching = {k: brief.get(k) for k in ("goal", "capabilities", "catalog_view", "module_catalog", "research_effort")}
    return dict(run_id=rid, brief_sha256=sha(actual.encode()), brief_utf8_bytes=len(actual.encode()),
                goal_sha256=sha(brief["goal"].encode()), materials=materials, tools_sha256=digest(tools),
                initial_conditions_sha256=digest({**matching, "materials": materials}))


def prepare_slot(folder, inputs, shared):
    from gx_core.p0.profiles import proxy_settings
    home = folder / "seed-home"
    home.mkdir(parents=True, mode=0o700)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if k in os.environ}
    save_new(home / "profile.json", PROFILE)
    env.update(HOME=str(home), GX_P0_PROFILE=str(home / "profile.json"), GX_CODEX_OFFLINE="1",
               PYTHONDONTWRITEBYTECODE="1", **proxy_settings())
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    result = live.invocation(env, ["start", "--state", folder / "state", "--run-key", "registered-pair",
        "--goal", (inputs / "goal.md").read_text().strip(), "--goal-config", inputs / "grant.json"], 90, folder / "start.json")
    need(result["exit_code"] == 0, "Offline public Goal start failed; retain its original receipt")
    rid = result["stdout"]["run_id"]
    initial = live.snapshot(folder / "state/gx.db", rid)
    need(not any(initial[k] for k in ("jobs", "decisions", "turns", "workflow_id")) and
         parse(initial["ledger"][0]["limits_json"]) == shared["caps"], "Starting Goal or limits differ")
    save_new(folder / "initial.json", initial)
    with closing(database(folder)) as db:
        pin = opening(db, rid)
    need((pin["goal_sha256"], pin["materials"]) == (shared["goal_sha256"], shared["materials"]),
         "Opening packet omits registered material or task information")
    return pin


def register(work, inputs, keys, custody_path, funding_path):
    inputs, keys = external(inputs), external(keys)
    need(not keys.is_relative_to(inputs) and not inputs.is_relative_to(keys),
         "Use separate execution and answer mounts")
    custody = read(external(custody_path))
    need(custody["series"] == PROTOCOL["series"] and custody["developer_access"] is False and
         custody["unexposed"] is True and custody["raw_assignment_contains_no_result_or_answer"] is True and
         set(custody["order"]) == set(SLOTS) and len(custody["order"]) == 2, "Independent custody or allocation missing")
    roles = {k: custody[k] for k in ("custodian", "opportunity_reader", "adjudicator")}
    need(all(isinstance(v, str) and v.strip() for v in roles.values()) and
         roles["custodian"] not in (roles["opportunity_reader"], roles["adjudicator"]),
         "Adjudication must be independent of case preparation")
    shared = common(inputs)
    records, conditions = {}, []
    for slot in SLOTS:
        _, record = case_file(inputs, slot)
        key_path = keys / (slot + ".json")
        answer = answer_file(key_path)
        records[slot] = {**record, "answer_sha256": live.digest(key_path)}
        note = read(inputs / "cases" / (slot + ".json"))["intervention"]["text"]
        need(not set(custody.get("prior_exposure_sha256", [])).intersection(
            [record["case_sha256"], record["intervention_sha256"], sha(note.encode()), records[slot]["answer_sha256"]]),
            "Case or answer matches a previously exposed receipt supplied by the custodian")
        conditions.append(answer["condition"])
    need(sorted(conditions) == ["change", "maintain"] and len({r["intervention_sha256"] for r in records.values()}) == 2
         and len({r["version"] for r in records.values()}) == 2, "A pair requires two distinct prospective cases")
    funding = None
    if funding_path:
        budget = read(external(funding_path))
        need(budget["series"] == PROTOCOL["series"] and budget["reference"].strip() and
             all(type(budget["caps"].get(k)) is int and budget["caps"][k] >= 2 * v for k, v in shared["caps"].items()),
             "Pair exceeds the independently authorized cumulative limits")
        funding = {k: budget[k] for k in ("reference", "caps")}
        funding["receipt_sha256"] = live.digest(funding_path)
    work.mkdir(exist_ok=False, mode=0o700, parents=True)
    for slot in SLOTS:
        records[slot]["opening"] = prepare_slot(work / slot, inputs, shared)
    need(len({(r["opening"]["initial_conditions_sha256"], r["opening"]["tools_sha256"]) for r in records.values()}) == 1,
         "Opening prompts, cards, tools or raw materials differ between slots")
    registration = dict(schema="gx.v62b.paired-decision-registration/1", registered_at=live.now(),
                        protocol_sha256=live.digest(HERE / "protocol.json"), source=source_pin(), roles=roles,
                        custody_sha256=live.digest(custody_path), order=custody["order"], slots=records,
                        common=shared, funding=funding, planned=PROTOCOL["denominators"])
    save_new(work / "registration.json", registration)
    return live.digest(work / "registration.json")


def registration(work, anchor):
    path = work / "registration.json"
    need(live.digest(path) == anchor, "Registration differs from the independently retained commitment")
    plan = read(path)
    need(plan["protocol_sha256"] == live.digest(HERE / "protocol.json") and plan["source"] == source_pin(),
         "Product, prompt, scorer, or protocol differs from preregistration")
    return plan


def gate(plan, work, slot, anchor, previous_sha):
    need(plan["funding"], "Registered pair has no spending authorization")
    for preceding in plan["order"][:plan["order"].index(slot)]:
        folder = work / preceding
        need((folder / "score.json").exists(), "Earlier slot requires independent review before another admission")
        result = verified_score(work, preceding, plan, anchor, previous_sha)
        need(not result["stop_unsupported"], "Batch paused for an unsupported assertion of support")
        need((folder / "terminal.json").exists(), "Reconcile the existing execution before another admission")
        rid = read(folder / "terminal.json")["run_id"]
        if rid:
            from gx_core.p0 import v6_costs
            with closing(database(folder)) as conn:
                costs = v6_costs.resource_report(conn, rid)
            for k, v in costs["lines"].items():
                need(v["booked"] + v["reserved"] + plan["common"]["caps"][k] <= plan["funding"]["caps"][k],
                     "Actual charges and reserved work leave insufficient cohort capacity")


def message_id(anchor, slot):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gx-v62b/{anchor}/{slot}"))


def execute(work, inputs, slot, anchor, previous_sha=None):
    inputs = external(inputs)
    plan = registration(work, anchor)
    need(common(inputs) == plan["common"], "Shared initial question, material, or limits changed")
    case, meta = case_file(inputs, slot)
    need(all(plan["slots"][slot][k] == v for k, v in meta.items()), "Case differs from independent registration")
    gate(plan, work, slot, anchor, previous_sha)
    folder = work / slot
    save_new(folder / "admission.json", dict(registration_sha256=anchor, slot=slot, admitted_at=live.now(), **meta))
    started = time.monotonic()
    rid, status = None, "FAILED"
    try:
        env = live.live_environment(folder, model=PROTOCOL["model"])
        # V6.1's campaign profile is xhigh. Use the already frozen V6.2B profile on every role.
        env["GX_P0_PROFILE"] = str(folder / "seed-home/profile.json")
        need(read(Path(env["GX_P0_PROFILE"])) == PROFILE, "Frozen role profile changed")
        binary = Path(env.get("GX_P0_CODEX", ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"))
        need(live.digest(binary) == read(ROOT / "release-assets/lock.json")["linux_x64"]["binary_sha256"], "Official host pin mismatch")
        env["GX_P0_CODEX"] = str(binary.resolve())
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        need(live.preflight(env, folder) and PROTOCOL["model_effort"] in read(folder / "preflight.json")["model_catalog"],
             "Official provider, registered effort or Clash preflight failed")
        rid = plan["slots"][slot]["opening"]["run_id"]
        dbfile = folder / "state/gx.db"
        initial = live.snapshot(dbfile, rid)
        need(not any(initial[k] for k in ("jobs", "decisions", "turns", "workflow_id")) and
             parse(initial["ledger"][0]["limits_json"]) == plan["common"]["caps"], "Starting Goal or limits differ")
        with closing(database(folder)) as db:
            need(opening(db, rid) == plan["slots"][slot]["opening"], "Opening task differs from the preregistered packet")
        science = folder / "intervention.json"
        save_new(science, case["intervention"])
        timeout = PROTOCOL["deadline_seconds_per_slot"]
        with ThreadPoolExecutor(max_workers=1) as worker:
            driving = worker.submit(live.invocation, env, ["run", "--state", dbfile.parent, "--run-id", rid,
                                    "--timeout", timeout], timeout + 30, folder / "run.json")
            while not driving.done():
                with closing(database(folder)) as db:
                    first = db.execute("SELECT d.decision_ref FROM v6_decisions d JOIN v6_decision_tasks t "
                                       "USING(task_id,run_id) WHERE d.run_id=? AND t.scope='run' LIMIT 1", (rid,)).fetchone()
                if first:
                    injected = live.invocation(env, ["message", "--state", dbfile.parent, "--run-id", rid, "--queue-only",
                                "--message-id", message_id(anchor, slot), "--action", science], 30, folder / "injection.json")
                    need(injected["exit_code"] == 0, "Custodian assignment could not be queued")
                    break
                time.sleep(.15)
            final = driving.result()
        status = ("CLOSED" if final["exit_code"] == 0 and final["stdout"].get("goal_status") == "CLOSED" else
                  "TIMED_OUT" if final["exit_code"] == 75 else "INCOMPLETE")
    finally:
        save_new(folder / "terminal.json", dict(run_id=rid, status=status, elapsed_seconds=time.monotonic() - started,
                                               finished_at=live.now()))


def observe(work, slot, plan, anchor):
    folder = work / slot
    terminal = read(folder / "terminal.json")
    admission = read(folder / "admission.json")
    need(admission["registration_sha256"] == anchor and admission["slot"] == slot, "Admission belongs to another trial")
    if not terminal["run_id"]:
        return {"run_id": None, "turns": [], "jobs": [], "wakes": [], "costs": None, "missing": ["FAILED_START"],
                "terminal": terminal, "intervention_event": None}
    rid = terminal["run_id"]
    evidence = extract(folder, rid)
    ready = read(folder / "preflight.json")
    need(ready["ready"] and ready["account_type"] == "chatgpt" and ready["provider"] == PROTOCOL["provider"],
         "Original host admission is missing its official provider preflight")
    for turn in evidence["turns"]:
        native = turn["native"]
        need((native["model_provider"], native["model"], native["effort"]) ==
             (PROTOCOL["provider"], PROTOCOL["model"], PROTOCOL["model_effort"]),
             "A scripted or different native provider/model cannot enter the true-model cohort")
        need(turn["host_profile"] == {"provider":PROTOCOL["provider"], "model":PROTOCOL["model"],
                                      "effort":PROTOCOL["model_effort"]}, "GX host and native profile do not match")
    with closing(database(folder)) as db:
        mid = message_id(anchor, slot)
        row = db.execute("SELECT i.action_json,e.event_id,s.source_id,e.payload_json FROM p0_human_inputs i JOIN sources s ON "
                         "json_extract(s.provenance_json,'$.client_message_id')=i.message_id AND s.namespace=i.run_id "
                         "JOIN events e ON e.run_id=i.run_id AND e.subject_id=s.source_id AND e.kind='v6.observation.added' "
                         "WHERE i.run_id=? AND i.message_id=?", (rid, mid)).fetchone()
    event = None
    if row:
        need(digest(parse(row["action_json"])) == plan["slots"][slot]["intervention_sha256"], "Intervention changed at GX ingress")
        event = row["event_id"]
        evidence["assignment_ref"] = parse(row["payload_json"])["artifact_ref"]
        evidence["assignment_text"] = parse(row["action_json"])["text"]
    turns = lead_turns(evidence)
    if turns:
        first = parse(turns[0]["frozen_brief"].split("\n", 1)[1])
        need(turns[0]["brief_sha256"] == plan["slots"][slot]["opening"]["brief_sha256"],
             "The actually delivered opening brief differs from the prospectively frozen bytes")
        need(sha(first["goal"].encode()) == plan["common"]["goal_sha256"] and not first.get("module_reports")
             and not first.get("observations") and not first.get("nodes") and not first.get("head"),
             "Opening delivered brief omitted the common task or contained later information")
        materials = [{k: m[k] for k in ("name", "role", "sha256")} for m in first["materials"]]
        need(materials == plan["common"]["materials"], "Opening data differ from the frozen common material")
        if turns[0]["decision"] and event:
            need(turns[0]["decision"]["event_id"] < event, "Raw assignment arrived before the first independent decision")
        evidence["initial_conditions_sha256"] = digest({k: first.get(k) for k in (
            "goal", "capabilities", "catalog_view", "module_catalog", "research_effort")} | {"materials": materials})
    evidence.update(intervention_event=event, terminal=terminal)
    return evidence


def verified_score(work, slot, plan, anchor, retained_sha):
    folder = work / slot
    need(retained_sha and live.digest(folder / "score.json") == retained_sha,
         "The operator must supply the score commitment retained by the independent reviewer")
    result, dossier = read(folder / "score.json"), read(folder / "dossier.json")
    need(dossier["registration_sha256"] == anchor and result["dossier_sha256"] == digest(dossier),
         "Independent score identifies a different registration or dossier")
    original = observe(work, slot, plan, anchor)
    need(dossier["originals"] == original, "Score does not match the original native and GX records")
    if result["passed"]:
        need(original["terminal"]["status"] == "CLOSED" and original["turns"] and original["intervention_event"]
             and not original["missing"] and result["assigned_measurement_verified"] and not result["stop_unsupported"],
             "A passing score lacks registered true-model evidence")
    return result


def status(work=None, anchor=None, retained=None):
    plan = registration(work, anchor) if work and anchor else None
    retained = retained or {}
    result = {}
    for slot in SLOTS:
        folder = work / slot if work else None
        saved = bool(folder and (folder / "score.json").is_file())
        score = verified_score(work, slot, plan, anchor, retained[slot]) if saved and plan and slot in retained else None
        terminal = read(folder / "terminal.json") if folder and (folder / "terminal.json").is_file() else None
        outcome = "NOT_RUN"
        if terminal:
            outcome = terminal["status"]
        elif folder and (folder / "admission.json").is_file():
            outcome = "ADMITTED_UNRESOLVED"
        if saved and not score:
            outcome = "RECORDED_AWAITING_INDEPENDENT_COMMITMENT"
        if score:
            outcome = "STOP_UNSUPPORTED" if score["stop_unsupported"] else "PASS" if score["passed"] else "FAIL"
        safe = ({k:score[k] for k in ("condition", "passed", "opportunities", "stop_unsupported", "assigned_measurement_verified",
                "strict_cited_work_change", "wake_count", "brief_bytes", "elapsed_seconds", "dossier_sha256",
                "independent_review_sha256")} if score else None)
        if safe is not None:
            safe["windows"] = [{k:r[k] for k in ("window","is_opportunity","disposition","justified","unlisted",
                                    "unsupported_support","strict_cited_work_change")} for r in score["windows"]]
        costs = score["costs"] if score else None
        if not score and folder and (folder / "admission.json").is_file() and (folder / "initial.json").is_file():
            rid = read(folder / "start.json")["stdout"]["run_id"]
            with closing(database(folder)) as db:
                scopes = dict(db.execute("SELECT r.thread_id,t.scope FROM p0_roots r JOIN v6_decision_tasks t "
                                         "USING(task_id,run_id) WHERE r.run_id=?", (rid,)))
                costs = cost_detail(db, rid, scopes)
        result[slot] = dict(status=outcome, enrollment=terminal, score=safe, costs=costs)
    passed = sum(r["status"] == "PASS" for r in result.values())
    admitted = sum(r["status"] != "NOT_RUN" for r in result.values())
    return dict(series=PROTOCOL["series"], planned=PROTOCOL["denominators"], admitted=admitted, passed=passed,
                science_status="NOT_RUN" if not admitted else "PASS" if passed == 2 else "NOT_ESTABLISHED",
                slots=result, private_registration="FROZEN" if work and (work / "registration.json").is_file()
                else "PENDING_CUSTODIAN_COMMITMENTS", historic_control_contribution=0, scripted_contribution=0)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    actions = p.add_subparsers(dest="command", required=True)
    for name in ("status", "register", "run", "briefs", "lock", "score"):
        cmd = actions.add_parser(name)
        cmd.add_argument("--work", type=Path, required=name != "status")
        if name not in ("status", "register"):
            cmd.add_argument("--slot", required=True, choices=SLOTS)
            cmd.add_argument("--registration-sha", required=True)
        if name == "status":
            cmd.add_argument("--registration-sha")
            cmd.add_argument("--score-anchors", type=Path, help="Independent JSON mapping of slot to retained score SHA-256")
        if name == "run":
            cmd.add_argument("--previous-score-sha", help="Reviewer-held first-slot score commitment for second admission")
        if name in ("register", "run"):
            cmd.add_argument("--inputs", type=Path, required=True)
        if name == "register":
            cmd.add_argument("--keys", type=Path, required=True)
            cmd.add_argument("--custody", type=Path, required=True)
            cmd.add_argument("--funding", type=Path)
        if name in ("lock", "score"):
            cmd.add_argument("--review", type=Path, required=True)
        if name == "score":
            cmd.add_argument("--key", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    work = external(args.work) if args.work else None
    if args.command == "status":
        return status(work, args.registration_sha, read(external(args.score_anchors)) if args.score_anchors else None)
    if args.command == "register":
        return {"registration_sha256": register(work, args.inputs, args.keys, args.custody, args.funding)}
    slot, anchor = args.slot, args.registration_sha
    plan = registration(work, anchor)
    if args.command == "run":
        execute(work, args.inputs, slot, anchor, args.previous_score_sha)
        return status(work)
    evidence = observe(work, slot, plan, anchor)
    folder = work / slot
    if args.command == "briefs":
        packet = brief_packet(evidence)
        save_new(folder / "briefs.json", packet)
        return {"packet_sha256": digest(packet)}
    if args.command == "lock":
        sealed = lock_opportunities(evidence, read(external(args.review)), plan["roles"]["opportunity_reader"])
        for mark, turn in zip(sealed["windows"], lead_turns(evidence)):
            if mark["is_new"]:
                report, = [r for r in turn["reports"] if r["ref"] == mark["report_ref"]]
                mark["assignment_operations"] = assignment_traces(evidence, report, mark["source_refs"])
        dossier = {"opportunities": sealed, "originals": evidence, "registration_sha256": anchor}
        save_new(folder / "dossier.json", dossier)
        return {"dossier_sha256": digest(dossier)}
    dossier = read(folder / "dossier.json")
    need(dossier["registration_sha256"] == anchor and dossier["originals"] == evidence, "Original evidence changed since review")
    need(live.digest(external(args.key)) == plan["slots"][slot]["answer_sha256"], "Answer differs from frozen registration")
    answer = answer_file(args.key)
    outcome = adjudicate(evidence, dossier["opportunities"], read(external(args.review)), answer,
                         plan["roles"]["adjudicator"], digest(dossier))
    first = next((m for m in dossier["opportunities"]["windows"] if m["is_new"]), {})
    qualified = any(matches_assay(op, rule) for op in first.get("assignment_operations", [])
                    for rule in answer["eligible_measurements"])
    outcome["assigned_measurement_verified"] = qualified
    if evidence["terminal"]["status"] != "CLOSED" or not qualified:
        outcome["passed"] = False
    other = next(s for s in SLOTS if s != slot)
    prior = work / other / "score.json"
    matched = evidence.get("initial_conditions_sha256")
    if prior.is_file() and read(prior).get("initial_conditions_sha256") not in (None, matched):
        outcome.update(passed=False, unmatched_initial_conditions=True)
    save_new(folder / "score.json", dict(**outcome, costs=evidence["costs"], initial_conditions_sha256=matched,
             dossier_sha256=digest(dossier), independent_review_sha256=live.digest(args.review),
             wake_count=len(evidence["wakes"]), brief_bytes=[t["brief_utf8_bytes"] for t in evidence["turns"]],
             elapsed_seconds=evidence["terminal"]["elapsed_seconds"]))
    return {"slot": slot, "score_sha256": live.digest(folder / "score.json"),
            "passed": outcome["passed"], "stop_unsupported": outcome["stop_unsupported"]}


if __name__ == "__main__":
    import json
    print(json.dumps(main(), indent=2, ensure_ascii=False))
