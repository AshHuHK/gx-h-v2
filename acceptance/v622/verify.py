"""Build or independently verify the offline release evidence manifest."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "e5b7ee977ec33854362bc2440d8f9249464aed20"
OLD_PRODUCT = "83e8f34d2886a5c4cedc966ec4a7dd7b2addf691"
DEFAULT = ROOT / "acceptance/v622/receipts"
CASES = ("R1", "R2", "R3", "R4", "R5", "R6", "B1", "B2", "B3", "F", "G")
PUBLIC = {"01": ("P4", "single", "MAINTAIN"), "02": ("K7", "delegation_available", "CHANGE"),
          "03": ("K7", "single", "CHANGE"), "04": ("P4", "delegation_available", "MAINTAIN")}


def need(test, reason):
    if not test:
        raise ValueError(reason)


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def git(*args):
    return subprocess.check_output(["git", "-C", ROOT, *args], text=True).strip()


def source_pins(commit):
    need(re.fullmatch(r"[0-9a-f]{40}", commit), "The candidate must be a full Git commit")
    need(git("rev-parse", commit + "^{commit}") == commit, "Unknown source commit")
    need(git("merge-base", commit, BASE) == BASE, "Candidate does not descend from the review baseline")
    paths = ["gx-h/gx_core", "gx-h/contracts", "gx-h/plugins/installed", "gx-h/adapters", "gx-h/config", "acceptance/v622/public",
             "release-assets/lock.json"]
    changed = git("diff", "--name-only", commit, "--", *paths)
    need(not changed, "Checkout differs from frozen inputs: " + changed)
    pins = {p: git("rev-parse", commit + ":" + p) for p in paths}
    pinned_files = ("gx-h/contracts/CONTRACT_SHA", "acceptance/v622/public/protocol.json",
                    "acceptance/v62b/study/not-run.json")
    for p in pinned_files:
        revision = BASE if "not-run" in p else commit
        blob = subprocess.check_output(["git", "-C", ROOT, "show", revision + ":" + p])
        need((ROOT / p).read_bytes() == blob, "Pinned source changed: " + p)
        pins[p] = hashlib.sha256(blob).hexdigest()
    return pins


def collect(root):
    rows = {}
    def run(case, fn):
        try:
            rows[case] = {"status": "PASS", "observed": fn()}
        except FileNotFoundError as error:
            rows[case] = {"status": "NOT_RUN", "reason": "Missing " + str(error.filename or error)}
        except (ValueError, TypeError, KeyError, OSError, IndexError, subprocess.CalledProcessError) as error:
            rows[case] = {"status": "FAIL", "reason": str(error)}

    commands = read(root / "commands.json") if (root / "commands.json").exists() else {"groups":{}}
    commit = commands.get("source_commit")
    if commit is None:
        rows["candidate"] = {"status":"NOT_RUN", "reason":"No source commit was registered for this acceptance"}
    else:
        run("candidate", lambda: source_pins(commit))
    pins = rows["candidate"].get("observed", {})
    def result(group):
        recorded = commands["groups"].get(group)
        if recorded is None:
            raise FileNotFoundError("command group " + group)
        need(rows["candidate"]["status"] == "PASS", "Cannot attribute this command to verified candidate source")
        need(recorded.get("source_commit") == commit, f"{group}: command belongs to a different source")
        need(isinstance(recorded.get("argv"), list) and bool(recorded["argv"]), f"{group}: missing executed argv")
        need(recorded.get("exit_code") == 0, f"{group}: command exited {recorded.get('exit_code')}")
        log = (root / recorded["log"]).resolve()
        need(log.is_relative_to(root) and log != root, f"{group}: invalid command log path")
        need(log.stat().st_size > 0, f"{group}: empty command log")
        return recorded

    def historical():
        d = read(root / "baseline/commands.json")
        need(d["index"]["exit_code"] == 0 and d["spending"]["exit_code"] == 2, "Baseline probes had unexpected exits")
        index = (root / "baseline/index.log").read_text().strip()
        product, statuses = index.split(" ", 1)
        need(product == OLD_PRODUCT and json.loads(statuses) == {"PASS": 23, "NOT_RUN": 2}, "Old index changed")
        for case in CASES[:6]:
            old = read(root / "baseline" / (case + ".json"))
            need(re.fullmatch(r"gxr_[A-Za-z0-9]+", old["run_id"]), case + " missing baseline Run")
            if case == "R1":
                need(old["wrong_mask_publication"]["level"] == "limited_support", "Missing original wrong-mask support")
            if case in ("R2","R3"):
                need(old["external"]["level"] == "limited_support", "Missing original post-hoc support")
            if case == "R4":
                need("verified_metrics" not in old["report"] and old["report"]["findings"], "Missing free-text report baseline")
            if case == "R5":
                need(all(o["exit_code"] == 0 for o in old["operations"]) and old["observations"], "Missing source mismatch baseline")
            if case == "R6":
                need(old["refusal"] is None and old["attempted_close"], "Missing stale-close baseline")
        old_f = read(root / "baseline/F.json")
        need(old_f["external_relabel_science_pass"], "Missing unchanged-route false positive")
        return {"release_commit": BASE, "product": product, "index": json.loads(statuses), "paid_check_exit": 2}

    def repair(case):
        result("repairs")
        dirs = {"R1":"R1.json", "R2":"propositions/R2.json","R3":"propositions/R3.json",
                "R4":"R4.json", "R5":"sources/wrong-accession.json", "R6":"close/late-direct.json"}
        d = read(root / "repairs" / dirs[case])
        run_id = d["run_id"]
        need(re.fullmatch(r"gxr_[a-z0-9]+", run_id), "Unexpected Run ID")
        info = {"run_id":run_id, "receipt":"repairs/" + dirs[case]}
        if case == "R1":
            need(d["independent"] == {"common_ca":[164,164], "ordered_backbone":[49,164]}, "Independent controls differ")
            need(d["wrong_mask_publication"]["publication"]["direction"] != "supported"
                 and d["correct_mask_publication"]["publication"]["direction"] == "opposed", "Wrong mask or direction")
            need(d["operator_status"]["claim_revision_id"] == d["correct_mask_publication"]["claim_revision_id"],
                 "Operator export and adopted revision differ")
            info.update(independent=d["independent"], claim=d["operator_status"]["claim_revision_id"])
        if case in ("R2", "R3"):
            external = d["external"]
            material = [r["qualification"] for r in d["published"]["maturity"]["material_coverage"]]
            expected = ["retrospective", "unmeasured" if case == "R2" else "retrospective"]
            need(sorted(material) == sorted(expected) and external["level"] in ("insufficient","open")
                 and external["claim_revision_id"] == d["published"]["claim_revision_id"], "Retrospective support exported")
            info.update(material_coverage=material, published=external["level"])
            if case == "R3":
                future = read(root / "repairs/propositions/R3-fresh.json")
                need(future["run_id"] == run_id and future["external"]["level"] == "limited_support"
                     and d["original_verdict"]["maturity"]["direction"] == "opposed", "Prospective successor lost its provenance")
                info["successor"] = future["external"]["claim_revision_id"]
        if case == "R4":
            m, = d["report"]["verified_metrics"]
            need((m["numerator"], m["denominator"]) == (49,164), "Wrong raw measurement")
            need(d["exported"]["verified_metrics"] == [m] and d["report"]["qualitative_notes"]["status"] == "UNVERIFIED",
                 "Source-free interpretation counted as verified")
            indexed = read(root / "repairs/R4-index-read.json")
            retry = read(root / "repairs/R4-retries.json")
            bounded = read(root / "repairs/R4-retries-partial.json")
            need(indexed["verified_metrics_read"] and retry["count"] == 1 and bounded["attempts"] == 3 and
                 retry["settlements_before"] == retry["settlements_after"] and
                 bounded["settlements_before"] == bounded["settlements_after"], "Report read or bounded repair duplicated work")
            info.update(metric=m["metric_ref"], display=m["display"], index_read_run=indexed["run_id"],
                        repair_run=retry["run_id"], bounded_repair_run=bounded["run_id"])
        if case == "R5":
            need({o["command"] for o in d["operations"]} == {"observe","assign-material"}
                 and all(o["exit_code"] != 0 for o in d["operations"]) and not d["observations"],
                 "Mismatched archive accession admitted")
            good = read(root / "repairs/sources/verified-assignments.json")
            ids = [json.loads(o["payload_json"])["acquisition"]["source_identity"] for o in good["observations"]]
            need({i["entry_id"] for i in ids if i["status"] == "matched_accession"} == {"2RE9","2O0O"}
                 and len({i["artifact_sha256"] for i in ids if i["status"] == "verified_derivation"}) == 2,
                 "Missing verified original accessions or derivations")
            info["verified_run"] = good["run_id"]
            info["refused_commands"] = [o["command"] for o in d["operations"]]
        if case == "R6":
            need(d["closed"] and d["replay_identical"] and d["additional_jobs"] == d["additional_job_charges"] == 0
                 and d["initial_cursor"] < d["job_event"]["event_id"] <= d["refreshed_cursor"],
                 "Close failed its delivery fence or repeated work")
            module = read(root / "repairs/close/late-module.json")
            need(module["wakes"] == 1 and module["replay_identical"] and module["jobs"] == module["job_charges"] == 0,
                 "Module result did not survive late delivery")
            info["module_run"] = module["run_id"]
            info.update(initial_cursor=d["initial_cursor"], refreshed_cursor=d["refreshed_cursor"])
        return info

    def suite(folder):
        result(folder)
        directory = root / folder
        reg = read(directory / "registration.json")
        scores = read(directory / "scores.json")
        summary = read(directory / "result.json")
        parity = read(directory / "capability-parity.json")
        replay = read(directory / "replay.json")
        need(reg["source_commit"] == commit and reg["protocol_sha256"] == pins["acceptance/v622/public/protocol.json"],
             "Registered code or protocol differs")
        need(scores["registration_sha256"] == summary["registration_sha256"] == digest(directory / "registration.json"),
             "Score points at another enrollment")
        need(reg["research_effort"] == "deep" and reg["wire_model"] == "gpt-6.2-astra"
             and reg["wire_effort"] == "max", "Unequal scientific policy or model")
        need(set(scores["cases"]) == set(summary["cases"]) == set(PUBLIC), "Slot missing or replaced")
        need(scores["paid_model_calls"] == summary["paid_model_calls"] == 0 and parity["status"] == "PASS",
             "External provider use or capability mismatch")
        need(replay["source_commit"] == commit and replay["status"] == "PASS" and replay["archive_sha256"] ==
             digest(directory / "public-raw-replay.tar.gz") and replay["scores_sha256"] == digest(directory / "scores.json"),
             "Portable rescore failed to reproduce published scores")
        protocol = read(ROOT / "acceptance/v622/public/protocol.json")
        controls = folder == "formal"
        expected = protocol["controls"] if controls else protocol["slots"]
        need(reg["slots"] == expected and reg["scope"] == ("FORMAL_OFFLINE" if controls else "SCRIPTED_REHEARSAL")
             and reg["provider"] == "gx_loopback" and reg["paid_calls_authorized"] is False, "Enrollment differs from protocol")
        need(digest(directory / "runtime-pins.json") == reg["runtime_pins_sha256"], "Runtime pins mismatch")
        for rel, frozen in read(directory / "runtime-pins.json").items():
            path = (ROOT / rel).resolve()
            need(path.is_relative_to(ROOT) and digest(path) == frozen, "Registered source changed: " + rel)
        boundary = read(directory / "network.json")
        need(boundary["exit_code"] == 0 and not boundary["ipv4_routes"] and
             set(boundary["ipv6_route_interfaces"]) <= {"lo"} and boundary["original_proxy_environment"]
             and boundary["outer_namespace"] != boundary["fixture_namespace"],
             "Batch lacks an isolated loopback execution receipt")
        ledger = read(directory / "funding.json")
        need(ledger["batch"]["cap"] == 4_000_000 and ledger["batch"]["held_tokens"] == 0 and
             ledger["paid_provider_calls"] == 0 and ledger["live_provider_usd"] is None and
             all(l["cap"] == 1_000_000 for l in ledger["slots"].values()), "Budget or paid usage mismatch")
        need(scores["provider_tokens"] is None and scores["provider_usd"] is None and scores["live_model_decisions"] == 0,
             "Official live usage is unavailable offline")
        items = {}
        for registered in reg["slots"]:
            slot = registered["slot"]
            grade = scores["cases"][slot]
            arm = registered["architecture"]
            need(grade["architecture"] == arm and grade["status"] == "SCORED", "Missing allocated score")
            need(grade["run_id"] == summary["cases"][slot]["run_id"] and re.fullmatch(r"gxr_[a-z0-9]+", grade["run_id"]),
                 "Missing original run ID in independent score")
            expected_action = "CHANGE" if registered["code"] == "K7" else "MAINTAIN"
            nominal = registered.get("case") == "F"
            need(grade["opportunity"] == "DELIVERED" and grade["condition"] == expected_action and
                 grade["science_pass"] == (not nominal) and grade["action_realized"] and not grade["unsupported_support"]
                 and grade["decision"] == ("MAINTAIN" if nominal else expected_action), "Executed scientific action differs")
            items[slot] = {k:grade[k] for k in ("run_id","decision","scientific_judgment","opportunity","delegation",
                                                "science_pass","first_window","decision_ref","effect","action_realized")}
        need(set(items) == set(PUBLIC) and len({x["run_id"] for x in items.values()}) == 4, "Repeated Run")
        return items

    def control(case):
        if rows["formal"]["status"] == "NOT_RUN":
            raise FileNotFoundError("formal batch or replay")
        need(rows["formal"]["status"] == "PASS", "The formal batch or its independent replay failed")
        proof = read(root / "formal/verification.json")
        return check_behavior(case, rows["formal"]["observed"], proof)

    def compat():
        result("compat")
        data = read(root / "compat/groups.json")
        groups = data["groups"]
        expected = {"discovery","repairs","score-controls","archive","v6","a1","a2-through-a5","a6","a7","b4","c4"}
        need(data["source_commit"] == commit and data["working_tree_at_start"] == []
             and {g["group"] for g in groups} == expected, "Incomplete or unpinned compatibility run")
        for g in groups:
            need(g["status"] == "PASS" and g["source_commit"] == commit and g["paid_model_calls"] == 0
                 and g["steps"], "Compatibility failure: " + g["group"])
            for step in g["steps"]:
                need(step["exit_code"] == 0 and digest(root / "compat" / step["log"]) == step["log_sha256"],
                     "Unverified compatibility step")
        m = read(root / "compat/migration.json")
        r = read(root / "compat/rollback.json")
        need(m["status"] == r["status"] == "PASS" and m["run_id"] == r["run_id"] and m["source_commit"] == commit
             and m["previous_commit"] == BASE and len(m["cold_reopens"]) == 2 and m["additional_jobs"] ==
             m["additional_model_turns"] == m["additional_charges"] == 0, "Historical Run changed on resume")
        return {"source_commit":commit, "groups":sorted(expected), "old_run":m["run_id"]}

    def funding():
        result("funding")
        d = read(root / "funding/result.json")
        need(d["status"] == "PASS" and d["paid_provider_calls"] == 0 and d["paid_state"] == "BLOCKED"
             and d["paid_authorized"] is False, "Unproven paid admission")
        needed = {"concurrency","batch_atomic","oversized","disconnect","idempotent","unknown_upper"}
        need(needed <= set(d["cases"]), "Missing a budget control")
        c = d["cases"]
        need(sorted(c["concurrency"]["statuses"]) == [200,403] and sorted(c["batch_atomic"]["statuses"]) == [200,403]
             and c["concurrency"]["closed"]["batch"]["settled_tokens"] == 4_000_000, "Concurrent admissions overspent")
        hold = c["disconnect"]["unknown_after_restart"]["batch"]
        need(hold["held_tokens"] > 0 and hold["unresolved_requests"] == 1 and hold["final_scripted_tokens"] is None
             and c["disconnect"]["denied_third_call"] == 403 and not c["disconnect"]["same_receipt_debited_again"]
             and c["idempotent"]["duplicate_debits"] == 0, "Lost or duplicated unresolved usage")
        need(c["oversized"]["too_short"]["provider_dispatches"] == 0 and c["oversized"]["inclusive_maximum"] >
             c["oversized"]["old_turn_reservation"] and c["unknown_upper"]["admitted"] == 0 and
             c["unknown_upper"]["status"] == "BLOCKED", "Unbounded billable request admitted")
        return {"cases":sorted(d["cases"]), "paid_state":d["paid_state"], "provider_calls":0}

    run("baseline", historical)
    for case in CASES[:6]:
        run(case, lambda c=case: repair(c))
    run("compat", compat)
    for group in ("standard","formal"):
        run(group, lambda g=group: suite(g))
    for case in ("B1","B2","B3","F"):
        run(case, lambda c=case: control(c))
    run("G", funding)

    def gate(keys):
        states = {rows[k]["status"] for k in keys}
        return "FAIL" if "FAIL" in states else "NOT_RUN" if "NOT_RUN" in states else "PASS"

    eng = gate(("candidate","baseline","compat",*CASES[:6]))
    offline = gate(("candidate","standard","formal",*CASES[6:]))
    historical_study = read(ROOT / "acceptance/v62b/study/not-run.json")
    need(historical_study["admitted"] == 0 and historical_study["planned"]["total"] == 2, "Historical cohort changed")
    files = {p.relative_to(root).as_posix():digest(p) for p in sorted(root.rglob("*"))
             if p.is_file() and p != root / "index.json"}
    return {"schema":"gx.v622.offline-acceptance/1", "source_commit":commit, "baseline":BASE,
            "input_versions":pins, "files":files, "checks":rows,
            "decisions":{"V6.2.2":eng,"V6.2C_OFFLINE":offline,"V6.2C_PAID":"BLOCKED"},
            "live":{"public":{"status":"NOT_RUN","admitted":0,"slots":4},
                    "private":{"status":"NOT_RUN","admitted":0,"slots":2,"series":historical_study["series"]}},
            "paid_gate":{"batch_authorization":False,"maximum_provider_request_usage":"unproved"}}


def check_behavior(case, slots, proof):
    """Cross-check the behavioral controls against the formal job and decision traces."""
    slot = {"B1":"01","B2":"02","B3":"03","F":"04"}[case]
    grade = slots[slot]
    witness = proof["formal_controls"][case]
    need(proof["status"] == "PASS" and witness["run_id"] == grade["run_id"] and
         witness["actual_effect"] == grade["effect"] and witness["assignment_event_id"] < witness["action_event_id"],
         "Events and independent score describe different actions")
    if case == "B1":
        need(not witness["module_open_event_ids"] and grade["delegation"] == "NOT_USED" and grade["science_pass"], "Direct scientific work lost credit")
    elif case == "B2":
        need(witness["assignment_event_id"] < witness["module_open_event_id"] < witness["action_event_id"] and
             grade["delegation"] == "DEMONSTRATED" and grade["science_pass"], "Delivered report did not drive the later action")
    elif case == "B3":
        need(not witness["post_action_jobs"] and grade["effect"]["kind"] == "closed_run" and grade["effect"]["unresolved"] and grade["science_pass"],
             "Scoped early close was not enacted")
    else:
        need(not grade["science_pass"] and grade["scientific_judgment"] == "CHANGE" and grade["decision"] == "MAINTAIN"
             and grade["effect"]["kind"] in ("maintained_job","completed_job"), "A nominal route change received credit")
    return {"slot":slot,"case":case,"run_id":grade["run_id"],"grade":grade,"witness":witness}



def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("build","verify"))
    ap.add_argument("--receipts", type=Path, default=DEFAULT)
    ap.add_argument("--require-complete", action="store_true")
    args = ap.parse_args()
    directory = args.receipts.resolve()
    result = collect(directory)
    path = directory / "index.json"
    if args.action == "build":
        path.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    else:
        need(read(path) == result, "Release index differs from its receipt files or frozen inputs")
    print(json.dumps(result["decisions"], sort_keys=True))
    if args.require_complete and not all(result["decisions"][k] == "PASS" for k in ("V6.2.2","V6.2C_OFFLINE")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
