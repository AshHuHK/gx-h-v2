"""Build or verify the public V6.2.1 acceptance index from captured command results."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
BASE = "321b3b9542e986e749b4eab08495b288d09ba4be"
RECEIPTS = "acceptance/v621/receipts/"
PILOT = "acceptance/v62c/receipts/"
COMMANDS = RECEIPTS + "commands.json"
PROTOCOL = "acceptance/v62c/protocol.json"
PRIOR = "acceptance/v62b/study/protocol.json"
PRIOR_STATUS = "acceptance/v62b/study/not-run.json"
SLOTS = {"01", "02", "03", "04"}
CLAIM = "claim-receipts/"

# Receipt names are relative to the public receipt directory unless fully qualified.
# Each entry identifies the real command group(s) that must have run on the frozen source.
CASES = (
    ("1A", "repairs", "Published claim and next brief respect the assessed direction and scope.",
     [CLAIM + x + ".json" for x in ("supported", "opposed", "mixed", "different-endpoint")]),
    ("1B", "repairs", "Acquisition pins the actual coordinate bytes before scientific Job admission.",
     ["metrics-trace.json"]),
    ("2A", "repairs", "Each gap requires its own applicable evidence. Mixed batches preserve atomicity.",
     ["gap-mixed-refusal.json", "gap-two-matches.json", "gap-acquired-analysis.json"]),
    ("2B", "repairs", "The Job, delivered report and adopted claim use the same verified measurement.",
     ["metrics-trace.json"]),
    ("2C", "repairs", "The scoped worker publishes secondary outputs. Cold replay adds no Job or charge.",
     ["named-receipt.json"]),
    ("3.1", "scoring", "New enrollment and rating agree on the normalized measurement type.", []),
    ("3.2", "repairs", "Assignment preserves the original grant. A new scoped task uses the exact bytes.",
     ["assigned-job.json"]),
    ("3.3", "scoring", "Direct action and delivered delegation receive separate prospective scores.", []),
    ("3.4", "catalog", "Both deep Leads can inspect a common method and run it on a public control.", ["catalog.json"]),
    ("3.5", "catalog", "Only commissioning and its scoped handback distinguish the two scientific interfaces.", ["catalog.json"]),
    ("4.goal", "repairs", "A changed Goal owns its rounds while prior evidence and run-wide spend survive.", ["goal-trace.json"]),
    ("4.gate", "repairs", "An evaluated false gate terminates. Pending, invalid and approved remain distinct.",
     ["gate-trace.json", "gate-trace.review.json"]),
    ("4.contract", "repairs", "Append-only publication and evidence survive reopen and allow cursor advance.", []),
    ("5.trace", "chain", "Official Lead wire, artifact pins, independent recomputation, public claim and authenticated refusals agree.",
     ["coordinate-publication.json"]),
    ("5.legacy-coordinate", "legacy-coordinate", "The installed historical method produces the same bytes and evidence type under both releases.",
     ["legacy-coordinates/comparison.json", "legacy-coordinates/before.json", "legacy-coordinates/after.json"]),
    ("5.upgrade", "upgrade", "The old Run and plugin pins, decisions, measurements and charges survive upgrade and rollback.",
     ["upgrade/before.json", "upgrade/check.json", "upgrade/restored.json"]),
    ("5.v6", "v6", "Inherited V6 regressions complete under the candidate.", []),
    ("5.v62a", "v62a", "Inherited V6.2A regressions complete under the candidate.", []),
    ("5.v62b", "v62b", "Inherited V6.2B regressions complete under the candidate.", []),
    ("C.spending", "scoring", "A simulated overshoot is booked once and blocks a subsequent paid admission.",
     ["spend-preflight-probe.json"]),
    ("C.dry", "pilot", "Four scripted cases finish on the frozen candidate with no paid calls.",
     [PILOT+"registration.json", PILOT+"result.json"]),
    ("C.parity", "pilot", "Both arms in the four official scripted Runs have matching methods and input access; only commissioning differs.",
     [PILOT+"capability-parity.json", PILOT+"registration.json", PILOT+"result.json"]),
    ("C.score", "rating", "All four engineering outcomes have separate science and delegation scores.",
     [PILOT+"scores.json", PILOT+"registration.json", PILOT+"result.json"]),
)


def encoded(data):
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local(name):
    if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"Expected a release-relative evidence path: {name!r}")
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"Evidence lies outside the release: {name}")
    return path


def read(name):
    return json.loads(local(name).read_bytes())


def commit(revision):
    return subprocess.check_output(["git", "-C", ROOT, "rev-parse", "--verify", revision+"^{commit}"],
                                   text=True).strip()


def landmarks(value):
    """Short lookup pointers; each entire untruncated receipt is separately hashed."""
    out = {"run_ids": [], "event_ids": [], "artifacts": [], "artifact_or_manifest_sha256": []}
    def walk(node, path):
        if isinstance(node, (list, dict)):
            for key, item in (node.items() if isinstance(node, dict) else enumerate(node)):
                walk(item, (*path, str(key)))
            return
        key = path[-1] if path else ""
        if key == "run_id" and isinstance(node, str):
            dest = "run_ids"
        elif isinstance(node, int) and (key == "event_id" or key.endswith("_event_id") or key == "job_admitted_event"):
            dest = "event_ids"
        elif isinstance(node, str) and key in {"artifact_ref", "claim_ref", "report_ref", "result_ref", "output_ref"}:
            dest = "artifacts"
        elif isinstance(node, str) and re.fullmatch(r"[a-f0-9]{64}", node) and ("sha256" in key or key == "manifest_hash"):
            dest = "artifact_or_manifest_sha256"
        else:
            return
        if len(out[dest]) < 12 and node not in (x["value"] for x in out[dest]):
            out[dest].append({"field": ".".join(path), "value": node})
    walk(value, ())
    return {key: values for key, values in out.items() if values}


def science_trace(d):
    source, metric, claim = d["source_event"], d["metric"], d["formal_claim"]
    assert any(p["artifact_ref"] == source["artifact_ref"] and p["sha256"] == source["sha256"] for p in metric["inputs"])
    assert metric["job_id"] == d["job_id"] and metric["output_name"] == "geometry.json"
    assert (metric["numerator"], metric["denominator"], metric["display"]) == (49, 164, "49/164 (0.299)")
    assert metric in d["delivered_report"]["metrics"] and metric in claim["metrics"]
    assert metric["artifact_ref"] in claim["evidence_refs"] and metric["display"] in claim["statement"]
    assert any("149/164" in str(r) and r.get("reason") for r in d["rejected"])
    return f'{metric["display"]}; official level {claim["publication"]["level"]} ({claim["publication"]["direction"]})'


def official_publication(trace, source):
    assert trace["source_version"]["git_head"] == source, "The official wire receipt was produced by another revision"
    original, job, output, metric = (trace[k] for k in ("original", "job", "output", "metric"))
    report, delivery, claim = (trace[k] for k in ("module_report", "delivered_lead", "public_claim"))
    assert original["event_id"] <= metric["provenance_cursor"] < job["admission_event_id"] < report["event_id"] < claim["event_id"]
    assert all(job["input"][k] == original[k] for k in ("artifact_ref", "sha256"))
    assert metric["inputs"] == output["source_inputs"] == [job["input"]]
    assert metric["artifact_ref"] == output["artifact_ref"] and metric["sha256"] == output["runner_sha256"] == output["sha256"]
    assert metric["output_name"] == output["output_name"] == "geometry.json" and output["nature"] == "analysis"
    assert all(metric[k] == job[k] for k in ("job_id", "admission_sha256", "manifest_sha256", "method"))
    check = trace["independent_check"]
    assert check["source_sha256"] == original["sha256"] and trace["real_cpu_jobs"] == 1
    assert (metric["numerator"], metric["denominator"]) == (check["numerator"], check["denominator"]) == (49, 164)
    assert check["integer_difference"] == 0 and check["absolute_ratio_difference"] <= check["fraction_absolute_tolerance"]
    assert delivery["official_session"]["model_provider"] == "gx_loopback" and delivery["delivery_state"] == "acknowledged"
    assert delivery["thread_id"] == delivery["official_session"]["id"] and delivery["turn_id"] == delivery["turn_context"]["turn_id"]
    assert delivery["metric_ref"] == metric["metric_ref"] and delivery["report_ref"] == report["report_ref"]
    assert delivery["finding"] == report["finding"] and output["artifact_ref"] in report["finding"]["source_refs"]
    assert metric["display"] == "49/164 (0.299)" and metric["display"] in report["finding"]["text"] and metric["display"] in claim["statement"]
    assert claim["publication"]["claim_revision_id"] == claim["claim_revision_id"] and claim["publication"]["level"] == "open"
    calls = delivery["calls"]
    assert [c["tool"] for c in calls] == ["gx_stage_claim", "gx_decide_revision"]
    assert calls[0]["official_session_line"] < calls[1]["official_session_line"] and calls[0]["call_id"] != calls[1]["call_id"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", c[k]) for c in calls for k in ("wire_sha256", "arguments_sha256"))
    wrong = trace["refusals"]["different_numerator"]
    assert {r["callback"]["tool"] for r in wrong} == {"gx_submit_module_report", "gx_stage_claim"}
    for r in wrong:
        cb = r["callback"]
        assert "149/164" in (r.get("input") or r.get("claim", "")) and cb["error"] == "SCHEMA_INVALID"
        assert cb["head_before"] == cb["head_after"] and cb["lease_id"] and cb["task_id"]
    foreign = trace["refusals"]["foreign_original"]
    assert trace["control_run_id"] != trace["run_id"] and foreign["target_run_id"] != trace["control_run_id"]
    assert foreign["own_ref_same_bytes"] != foreign["foreign_ref"] and foreign["returncode"] != 0
    assert foreign["foreign_ref"] in foreign["argv"] and original["sha256"] in foreign["argv"] and "run-local" in foreign["diagnostic"]
    return "Official Lead received 49/164 (0.299), staged and adopted open; wrong numerator and cross-Run original refused"


def legacy_coordinates(docs, source):
    comparison, before, after = docs
    assert comparison["schema"] == "gx.accept.legacy_coordinate_comparison/1"
    assert before["commit"] == BASE and after["commit"] == source and before["run_id"] != after["run_id"]
    snapshots = {row["slot"]: row for row in comparison["cases"]}
    assert set(snapshots) == {"before", "after"}
    for label, d in (("before", before), ("after", after)):
        case = snapshots[label]
        assert all(case[k] == d[k] for k in ("commit", "run_id", "job_id", "output_ref", "acquisition_event",
                                             "job_created_event", "admission_sha256", "manifest_sha256", "capability_sha256"))
        assert d["acquisition_event"] < d["job_created_event"] and d["raw_input"]["sha256"] == comparison["input_sha256"]
        assert d["schema"] == "gx.accept.legacy_coordinates/1" and all(d[k] == v for k,v in comparison["common"].items())
    assert before["output_sha256"] == after["output_sha256"] and re.fullmatch(r"[0-9a-f]{64}", before["output_sha256"])
    assert before["nature"] == after["nature"] == "unclassified" and before["output_name"] == after["output_name"] == "geometry.json"
    assert before["capability"] == after["capability"] == "deposited-chain-geometry@1"
    assert before["plugin"] == after["plugin"] == "org.gx.protein@0.2.0" and (before["selected"], before["denominator"]) == (49, 164)
    assert before["real_jobs"] == after["real_jobs"] == 1 and before["paid_calls"] == after["paid_calls"] == 0
    return "One historical @1 CPU Job on each release; geometry.json SHA-256 and unclassified nature agree"


def official_parity(docs, source, registration_sha):
    parity, reg, result = docs
    booked = {s["slot"]:s for s in reg["slots"]}
    assert reg["source_commit"] == source and result["registration_sha256"] == registration_sha
    assert parity["classification"] == result["classification"] == "SCRIPTED_REHEARSAL"
    assert set(booked) == set(parity["slots"]) == set(result["cases"]) == SLOTS
    common = parity["slots"]["01"]["common"]
    assert common["research_effort"] == "deep" and common["model"] == reg["wire_model"] and common["model_effort"] == reg["wire_effort"]
    assert {"gx_read_source", "gx_query_catalog", "gx_read_artifact", "gx_decide_revision"} <= set(common["tools"])
    assert not common["branch_delegation"]
    pairs = {}
    for slot, p in parity["slots"].items():
        measured, booking = result["cases"][slot], booked[slot]
        arm = p["architecture"]
        assert arm == measured["architecture"] == booking["architecture"] and p["run_id"] == measured["run_id"]
        assert p["common"] == common and p["job_count"] == measured["job_count"] == 2
        assert p["source_sha256"] == measured["assignment"]["sha256"] and measured["paid_model_calls"] == 0
        extra = {"gx_open_module","gx_cancel_module"} if arm == "delegation_available" else set()
        assert set(measured["initial_tools"]) == set(common["tools"]) | extra
        assert p["role_usage"] == measured["role_usage"] and p["live_provider_tokens"] is p["live_provider_usd"] is None
        pairs.setdefault(booking["code"], {})[arm] = p["source_sha256"]
    assert pairs.keys() == parity["matched_pairs"].keys() and len(pairs) == 2
    for code, arms in pairs.items():
        assert set(arms) == {"single","delegation_available"} and len(set(arms.values())) == 1
        assert parity["matched_pairs"][code]["cost_ratio_paid"] is None
    return "Four official openings, two matched specimen pairs, same methods and 2 Jobs/slot; only gx_open_module/gx_cancel_module differ"


def outcome(case, docs, source, pins=()):
    """Check substance where a trace is available. A passing command is also required."""
    for d in docs:
        if d.get("status") in {"FAIL", "NOT_RUN"} or d.get("paid_model_calls", 0) != 0:
            raise ValueError("Receipt reports an unsuccessful case or a paid call")
    if case == "1A":
        for d, direction in zip(docs, ("supported", "opposed", "mixed", "undetermined"), strict=True):
            public, science = d["publication"], d["science"]
            assert public["direction"] == science["direction"] == direction and public["level"] == science["level"]
            if direction == "opposed":
                assert public["proposed_level"] == "limited_support" and public["level"] != "limited_support"
        return ", ".join(d["science"]["direction"] + "/" + d["science"]["level"] for d in docs)
    if case in {"1B", "2B"}:
        return science_trace(docs[0])
    if case == "2A":
        reject, allow, coordinate = docs
        assert reject["refusal"]["code"] and not reject["resolution_events"]
        assert len({e["gap_ref"] for e in allow["resolution_events"]}) == 2
        assert coordinate["real_cpu_jobs"] == 1 and sorted(coordinate["states"].values()) == ["OPEN", "RESOLVED"]
        return f'{reject["refusal"]["code"]} with zero settlements; two distinct matches settled; only the measured coordinate mask closed'
    if case == "2C":
        d, = docs
        assert d["fresh_jobs"] == d["authorized_reuses"] == 1 and d["additional_jobs_after_recovery"] == d["additional_spend_after_recovery"] == 0
        assert d["cross_scope_read"] == d["unpublished_primary"] == "refused"
        return "One fresh Job, one reuse, zero additional Job or spend after recovery"
    if case == "3.2":
        d, = docs
        a = d["assignment"]
        assert a["acquisition_event_id"] < a["event_id"] < a["module_event_id"] < d["job_admitted_event"]
        assert d["real_cpu_jobs"] == 1 and d["receipt_replays"] >= 1 and d["original_task_id"] != d["receiving_task_id"]
        return "A scoped successor ran one Job after assignment; repeated delivery reused its receipt"
    if case in {"3.4","3.5"}:
        d, = docs
        a, b = (d["cases"][k] for k in ("single", "delegated"))
        assert all(a[k] == b[k] for k in ("materials","policy","research_effort","native_model","model_effort","result_sha256"))
        assert a["physical_jobs"] == b["physical_jobs"] == 1
        return f'Both deep Leads measured {a["mean"]}; extra delegated tools: ' + ", ".join(d["extra_tools_in_delegated"])
    if case == "4.goal":
        assert docs[0]["cold_open"] and docs[0]["unchanged_budget"]
        return "Goal version change and cold reopen preserved the run budget"
    if case == "4.gate":
        deny, review = docs
        assert deny["additional_jobs"] == 0 and review["candidate_waits"] and review["review_event"]
        return "Fixed negative launched zero extra Jobs; a different reviewed gate admitted the follow-up"
    if case == "5.trace":
        return official_publication(docs[0], source)
    if case == "5.legacy-coordinate":
        return legacy_coordinates(docs, source)
    if case == "C.parity":
        return official_parity(docs, source, pins[1])
    if case == "5.upgrade":
        before, checked, restored = docs
        assert before["revision"] == checked["baseline"] == restored["baseline"] == BASE
        assert checked["candidate"] == restored["candidate"] == source
        assert checked["additional_jobs"] == checked["additional_charges"] == 0
        assert checked["cursor_after"] > checked["cursor_before"]
        assert restored["restored_business_sha256"] == before["business_sha256"]
        return "Old receipts replayed, pending cursor advanced, original business records and cursor restored"
    if case == "C.spending":
        d, = docs
        assert d["paid_gate"] == "BLOCKED" and d["paid_provider_calls"] == 0
        assert {c["charged_role"] for c in d["scripted_cases"]} == {"lead", "module"}
        assert all(c["slot_overshoot"] == 200 and c["settlement_count"] == 1 for c in d["scripted_cases"])
        return "Simulated Lead and module each overshot by 200 tokens with one settlement; paid admission remains blocked"
    if case == "C.dry":
        reg, result = docs
        assert reg["source_commit"] == source and reg["protocol_sha256"] == sha(local(PROTOCOL))
        assert result["registration_sha256"] == pins[0]
        assert result["series"] == reg["series"] and reg["scope"] == "SCRIPTED_REHEARSAL"
        assert result["classification"] == "SCRIPTED_REHEARSAL" and set(result["cases"]) == SLOTS
        assert all(v["status"] == "PASS" and v["paid_model_calls"] == 0 for v in result["cases"].values())
        assert result["public_live_slots_enrolled"] == result["original_private_slots_enrolled"] == 0
        return "Four local rehearsals; zero official provider enrollments"
    if case == "C.score":
        d, reg, run = docs
        assert d["classification"] == "SCRIPTED_REHEARSAL" and set(d["cases"]) == SLOTS
        assert reg["source_commit"] == source and pins[1] == run["registration_sha256"] == d["registration_sha256"]
        for slot, claim in d["cases"].items():
            assert claim["run_id"] == run["cases"][slot]["run_id"]
            assert type(claim["science_pass"]) is bool and claim["live_model_decisions"] == 0
            assert claim["delegation"] in {"NOT_APPLICABLE", "DEMONSTRATED", "USED_INCOMPLETE", "NOT_USED"}
        scientific = sum(c["science_pass"] for c in d["cases"].values())
        delegated = sum(c["delegation"] == "DEMONSTRATED" for c in d["cases"].values())
        assert d["scripted_science_passes"] == scientific and d["scripted_delegation_demonstrated"] == delegated
        return f"{scientific}/4 scripted science judgments; {delegated} complete delegation chains; zero LIVE_MODEL credit"
    return "Completed captured command; see linked checks and output"


def command_groups(manifest, source):
    groups, covers = {}, {}
    for name, supplied in manifest.get("groups", {}).items():
        row = {**supplied}
        groups[name] = row
        for case_group in set([name, *row.get("covers", [])]):
            if case_group in covers:
                raise ValueError(f"Two groups cover {case_group}")
            covers[case_group] = name
        path = local(row["log"]) if row.get("log") else None
        if row.get("source_commit", source) != source:
            row.update(status="NOT_RUN", reason="Captured command belongs to another revision")
        elif not isinstance(row.get("argv"), list) or not row["argv"] or row.get("exit_code") is None or not path or not path.is_file():
            row.update(status="NOT_RUN", reason=row.get("reason") or "Missing executed argv, exit code or archived output")
        else:
            row["log_sha256"] = sha(path)
            with (gzip.open if path.suffix == ".gz" else open)(path, "rt", errors="replace") as fp:
                lines = list(fp)
            row["reported_skips"] = [line.strip() for line in lines if re.search(r"\.\.\. skipped|^SKIPPED \[", line)]
            row["test_totals"] = [line.strip() for line in lines if re.match(r"Ran \d+ tests? in ", line)]
            row["status"] = "PASS" if row["exit_code"] == 0 else "FAIL"
            if row["status"] == "FAIL":
                row["reason"] = row.get("reason") or "Inspect the captured command output"
    return groups, covers


def build(source):
    full = commit(source)
    if full != source:
        raise ValueError("Supply the full tested source commit")
    manifest = read(COMMANDS) if local(COMMANDS).is_file() else {}
    if manifest and manifest.get("source_commit") != source:
        raise ValueError("Command manifest and requested candidate differ")
    groups, covers = command_groups(manifest, source)
    paths = manifest.get("paths", {})
    cases = []
    for case, group_key, expected, needed in CASES:
        group = covers.get(case, covers.get(group_key))
        entry = dict(id=case, group=group, expected=expected, receipts=[], source_commit=source)
        docs, missing = [], []
        for logical in needed:
            filename = paths.get(logical, logical if logical.startswith("acceptance/") else RECEIPTS+logical)
            if not local(filename).is_file():
                missing.append(filename)
                continue
            data = read(filename)
            docs.append(data)
            entry["receipts"].append({"path": filename, "sha256": sha(local(filename)), **landmarks(data)})
        result = groups.get(group, {})
        errors = []
        if result.get("status") == "FAIL":
            errors.append(result["reason"])
        if not missing and docs:
            try:
                actual = outcome(case, docs, source, [r["sha256"] for r in entry["receipts"]])
            except (AssertionError, KeyError, ValueError, TypeError) as exc:
                errors.append(f"{type(exc).__name__}: {exc or 'recorded trace violates the acceptance condition'}")
        else:
            actual = "Completed captured command; see its output"
        if errors:
            entry.update(status="FAIL", actual="; ".join(errors),
                         next_check=result.get("log") or (entry["receipts"] or [{}])[0].get("path"))
        elif result.get("status") != "PASS" or missing:
            reason = [result.get("reason", "No completed command for " + group_key)] if result.get("status") != "PASS" else []
            entry.update(status="NOT_RUN", actual="; ".join(reason + ["Missing " + m for m in missing]),
                         next_check=missing[0] if missing else group_key)
        else:
            entry.update(status="PASS", actual=actual)
        cases.append(entry)
    for case, reason in (
        ("C.live", "Public paid slots require explicit enrollment authorization, a frozen protocol and a verified final-request spending ceiling."),
        ("C.private", "The separate custodian must commit a successor protocol and authorize private enrollment. The original outcome remains 0/2."),
    ):
        cases.append(dict(id=case, status="NOT_RUN", group=None, source_commit=source, expected="Future model evidence",
                          actual=reason, next_check=PROTOCOL, receipts=[]))
    def source_sha(revision, name):
        contents = subprocess.check_output(["git", "-C", ROOT, "show", revision + ":" + name])
        return hashlib.sha256(contents).hexdigest()

    for name in (PRIOR, PRIOR_STATUS):
        if sha(local(name)) != source_sha(BASE, name):
            raise ValueError("Released private protocol or original public status changed: " + name)
    prior_status = read(PRIOR_STATUS)
    prior_score = f'{prior_status["passed"]}/{prior_status["planned"]["total"]}'
    source_paths = [PROTOCOL, "gx-h/contracts/CONTRACT_SHA"]
    for name in source_paths:
        if sha(local(name)) != source_sha(source, name):
            raise ValueError("Checked-out evidence policy or protocol differs from frozen candidate: " + name)
    historical = ["baseline/gap-resolution.json", "baseline/gap-resolution.txt", "baseline/claim-direction.txt"]
    repairs = [c for c in cases if not c["id"].startswith("C.")]
    unverified = [c["id"] for c in repairs if c["status"] != "PASS"]
    return {
        "handoff": {
            "verified_repairs": [c["id"] for c in repairs if c["status"] == "PASS"],
            "dry_rehearsal_entry": "READY" if not unverified else "UNVERIFIED",
            "pending_before_dry_entry": unverified,
            "scripted_four_slot_execution": next(c["status"] for c in cases if c["id"] == "C.dry"),
            "scripted_four_slot_rating": next(c["status"] for c in cases if c["id"] == "C.score"),
            "public_live_entry": "BLOCKED",
            "public_live_prerequisites": ["explicit public batch spending authorization",
                                          "verified pre-dispatch billable limit for every Lead and module request"],
        },
        "schema": "gx.v621.acceptance-index/1", "baseline_commit": BASE, "source_commit": source,
        "source_files": {p: sha(local(p)) for p in source_paths},
        "historical_baseline": {RECEIPTS+p: sha(local(RECEIPTS+p)) for p in historical if local(RECEIPTS+p).is_file()},
        "commands_path": COMMANDS if manifest else None,
        "commands_sha256": sha(local(COMMANDS)) if manifest else None, "commands": groups, "cases": cases,
        "statuses": dict(Counter(c["status"] for c in cases)),
        "scope": "Offline public engineering. Scripted replies have no provider token or fee receipt. "
                 "Job and reuse counts refer to the linked individual runs; overlapping traces must not be added.",
        "original_private_result": prior_score, "live_public_enrollments": 0,
        "protocols": {name: sha(local(name)) for name in (PRIOR, PRIOR_STATUS, PROTOCOL)},
        "counterexamples": [
            {"case": "1A", "request": "Publish limited support for the opposed prediction",
             "receipt": paths.get(CLAIM+"opposed.json", RECEIPTS+CLAIM+"opposed.json")},
            {"case": "2A", "request": "Close unrelated gaps with an authorized partial reading",
             "receipt": paths.get("gap-mixed-refusal.json", RECEIPTS+"gap-mixed-refusal.json")},
            {"case": "2B", "request": "Publish 149/164 for the named 49/164 output",
             "receipt": paths.get("metrics-trace.json", RECEIPTS+"metrics-trace.json")},
        ],
    }


def markdown(idx):
    def link(path, label):
        target = os.path.relpath(path, RECEIPTS)
        return f"[{label}](<{target}>)"
    handoff = idx["handoff"]
    rows = ["# V6.2.1 acceptance", "",
            f"Candidate `{idx['source_commit']}` · V6.2 baseline `{idx['baseline_commit']}`", "",
            idx["scope"], "",
            "- Verified V6.2.1 checks: " + (", ".join(handoff["verified_repairs"]) or "none captured"),
            "- Entry to the zero-paid rehearsal: " + handoff["dry_rehearsal_entry"] +
            ". Four-slot execution: " + handoff["scripted_four_slot_execution"] +
            ". Four-slot rating: " + handoff["scripted_four_slot_rating"] + ".",
            "- Public live entry: " + handoff["public_live_entry"] + ". Pending: " + ", ".join(handoff["public_live_prerequisites"]) + ".",
            "", "| Case | Status | Observed | Evidence |", "|---|---|---|---|"]
    for row in idx["cases"]:
        refs = [link(x["path"], Path(x["path"]).name) for x in row["receipts"]]
        group = idx["commands"].get(row["group"], {})
        if group.get("log") and group.get("log_sha256"):
            refs.append(link(group["log"], "command log"))
        real = row["actual"].replace("|", "\\|")
        rows.append(f"| {row['id']} | {row['status']} | {real} | {', '.join(refs)} |")
    rows.extend(["", "### Counterexamples", ""])
    for negative in idx["counterexamples"]:
        link_text = link(negative["receipt"], "trace") if local(negative["receipt"]).is_file() else "awaiting trace"
        rows.append(f"- {negative['request']} — {link_text}")
    rows += ["", "The original unseen protocol remains at " + idx["original_private_result"] + ". "
             + link(PRIOR_STATUS, "Original cohort status") + " · " + link(PROTOCOL, "New public protocol")]
    rows.extend(["", "Full commands, trace event IDs, input and manifest digests are in [index.json](index.json).",
                 "", "Reported skips within successful test groups", ""])
    skips = [f"- {k} — {s}" for k, g in idx["commands"].items() for s in g.get("reported_skips", [])]
    rows += skips or ["No individual skip line was recorded in the linked logs."]
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--source", help="Full Git commit tested, frozen before publishing these receipts")
    parser.add_argument("--require-complete", action="store_true", help="Require every public engineering row to pass")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run with Python assertions enabled")
    out = local(RECEIPTS+"index.json")
    doc = local(RECEIPTS+"index.md")
    source = args.source
    if not source:
        src = COMMANDS if args.action == "build" else RECEIPTS+"index.json"
        source = read(src)["source_commit"]
    report = build(source)
    payloads = {out: encoded(report), doc: markdown(report).encode()}
    if args.action == "build":
        for path, contents in payloads.items():
            path.write_bytes(contents)
    elif any(not path.is_file() or path.read_bytes() != contents for path, contents in payloads.items()):
        raise ValueError("Receipt contents, captured commands or generated index differ; compare against the released pair")
    print(source, json.dumps(report["statuses"], sort_keys=True))
    incomplete = [c["id"] for c in report["cases"] if c["id"] not in {"C.live", "C.private"} and c["status"] != "PASS"]
    if args.require_complete and incomplete:
        print("Incomplete public engineering:", ", ".join(incomplete), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(2)
