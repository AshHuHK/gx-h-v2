"""Publish and replay one closed public engineering Run, including original native delivery and CPU evidence."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v62b.study import receipts as native
from acceptance.v62c import rehearse
from acceptance.v622.public import funding
from acceptance.v622.public.enrollment import digest, read
from acceptance.v622.public_archive import snapshot
from acceptance.v623.study import independent_compare, independent_geometry, operate, run_receipt

ARCHIVE = "gx-study.zip"
FORMAT = "gx.public-coordinate-engineering-archive/1"
INDEX = "manifest.json"
SECRET = re.compile(
    rb'gh[pousr]_[a-zA-Z0-9_]{20,}|github_pat_[a-zA-Z0-9_]{20,}|sk-(?:proj-)?[a-zA-Z0-9_-]{20,}'
    rb'|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'
    rb'|(?:Authorization[\\"]*\s*[:=][\\"]*\s*(?:Bearer|Basic)\s+[A-Za-z0-9_+./-]{6,})'
    rb'|"(?:access_token|refresh_token|id_token|client_secret|password|api_key)"\s*:\s*"[^"\s]{6,}"', re.I)
KINDS = {"asset_content", "capability_script", "input_manifest", "job_stderr", "job_stdout", "research_decision",
         "run_input", "block_influence", "v62_public_method_spec", "v6_claim_proposal"}


def check(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative(name):
    path = PurePosixPath(name)
    check(isinstance(name, str) and name == path.as_posix() and not path.is_absolute()
          and name != "." and ".." not in path.parts and "\\" not in name, "Invalid evidence path")
    return name


def source_file(base, name):
    path = base / relative(name)
    check(path.is_file() and path.resolve() == path and path.is_relative_to(base), "Unresolved evidence file: " + name)
    return path


def available_code(record, name, expected):
    current = (ROOT / relative(name)).resolve()
    if current.is_file() and current.is_relative_to(ROOT):
        raw = current.read_bytes()
        if sha(raw) == expected:
            return raw
    historical = subprocess.run(["git", "show", record["source_commit"] + ":" + name], cwd=ROOT,
                                capture_output=True)
    return historical.stdout if historical.returncode == 0 and sha(historical.stdout) == expected else None


def eligibility(seal, result, protocol):
    mode = seal["mode"]
    check(mode in ("SYNTHETIC_FIXTURE", "PUBLIC_RETROSPECTIVE_CPU"), "Only the public engineering study can be archived")
    check(seal["series"] == protocol["series"] + ("-SYNTHETIC" if mode == "SYNTHETIC_FIXTURE" else ""),
          "Not the registered public study series")
    check(seal["native_profile"]["provider"] == "gx_loopback" and result["status"] == "CLOSED" and
          result["series"] == seal["series"] and result["source_commit"] == seal["source_commit"] and
          result["seal_sha256"] and result["classification"] == mode, "Not a completed engineering fixture")
    for entry in (seal, result):
        check(entry.get("actor_class") == operate.ACTOR and entry.get("paid_provider_calls") == 0
              and entry.get("live_scientific_decisions") == 0, "Unexpected live or unidentified actor")
    check(re.fullmatch(r"[a-f0-9]{40}", seal["source_commit"]), "No valid source commit")
    check(len(seal["inputs"]) == len(result["cells"]) == 2, "Two ordered input deposits are required")
    if mode == "PUBLIC_RETROSPECTIVE_CPU":
        check([(i["entry"], i["sha256"]) for i in seal["inputs"]] ==
              [(i["entry"], i["sha256"]) for i in protocol["inputs"]], "Unexpected public deposited materials")
    return result["run_id"]


def write_archive(work, directory):
    seal = read(source_file(work, "seal.json"))
    result = read(source_file(work, "study-receipt.json"))
    rid = eligibility(seal, result, read(operate.PROTOCOL))
    check(digest(operate.PROTOCOL) == seal["operator_reference_sha256"], "Operator reference changed")
    check(not (work / "lead/codex/auth.json").exists(), "Expected credential-free loopback study directory")
    entries, blobs, unavailable = {}, set(), []
    path = directory / ARCHIVE
    with TemporaryDirectory() as temp, zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as out:
        def add(name, raw, expected=None):
            relative(name)
            checksum = sha(raw)
            check(not expected or checksum == expected, "Evidence digest mismatch: " + name)
            check(not SECRET.search(raw), "Credential-like content in public evidence: " + name)
            check(name not in entries, "Duplicate evidence path: " + name)
            entries[name] = checksum
            if checksum not in blobs:
                out.writestr("blobs/" + checksum, raw)
                blobs.add(checksum)

        def file(name, expected=None):
            add(name, source_file(work, name).read_bytes(), expected)

        # Keep measured isolation facts, excluding command environments and proxy configurations.
        network_file = source_file(work.parent, "network.json")
        net = read(network_file)
        boundary = {k: net[k] for k in ("outer_namespace", "fixture_namespace", "interfaces", "ipv4_routes",
                                        "ipv6_route_interfaces", "exit_code", "started_unix", "finished_unix")}
        add("boundary.json", json.dumps({**boundary, "original_sha256": digest(network_file)}, sort_keys=True).encode())
        for name in ("seal.json", "study-receipt.json", "custody/opening-1.json", "custody/opening-2.json",
                     "lead/raw/requests.jsonl", "lead/raw/wire.json", "lead/raw/commands.json"):
            file(name)
        file("custody/grant.json", seal["grant_sha256"])
        file("lead/goal.md", seal["lead_goal_sha256"])
        file("lead/method-card.json", seal["frozen_files"][str(operate.CARD.relative_to(ROOT))])
        for i, item in enumerate(seal["inputs"], 1):
            check(item["file"] == f"custody/{i}/coordinates.cif" and item["descriptor"] == f"custody/{i}/acquisition.json",
                  "Unexpected material path")
            file(item["file"], item["sha256"])
            file(item["descriptor"], item["descriptor_sha256"])
        for name, expected in seal["frozen_files"].items():
            check(name.startswith(("acceptance/v623/study/", "gx-h/plugins/installed/org.gx.block-influence/1.0.0/",
                                   "gx-h/plugins/installed/org.gx.protein/0.3.0/",
                                   "gx-h/adapters/scientific/", "gx-h/gx_core/p0/")) or name == "gx-h/contracts/CONTRACT_SHA",
                  "Unexpected frozen source path")
            raw = available_code(seal, name, expected)
            if raw is None:
                check(seal["mode"] == "SYNTHETIC_FIXTURE", "Missing public pinned source: " + name)
                unavailable.append(name)
            else:
                add("source/" + name, raw, expected)
        # The independent reader is replayed from installed code only; record its version for reproducibility.
        for script in (Path(independent_compare.__file__), Path(independent_geometry.__file__)):
            add("reviewer/" + script.name, script.read_bytes())
        artifacts = set()
        for index, db_name in enumerate(("lead/state/gx.db", "custody/accounting.sqlite")):
            backup = Path(temp) / str(index)
            snapshot(source_file(work, db_name), backup)
            with closing(sqlite3.connect(backup)) as db:
                check(db.execute("PRAGMA integrity_check").fetchone() == ("ok",), "SQLite snapshot failed integrity check")
                if index == 0:
                    check(db.execute("SELECT run_id FROM runs").fetchall() == [(rid,)], "Study database contains other Runs")
                    for run_id, kind, ns, rel, checksum in db.execute("SELECT run_id,kind,namespace,rel_path,sha256 FROM artifacts"):
                        check(run_id in (rid, None) and kind in KINDS, "Unexpected artifact in public study store")
                        item = "lead/state/artifacts/" + relative(ns) + "/" + relative(rel)
                        if item not in artifacts:
                            file(item, checksum)
                            artifacts.add(item)
            add(db_name, backup.read_bytes())
        sessions = native.native_session(work / "lead/codex/sessions")
        decisions = {r["thread_id"]: r["transcript_sha256"] for r in result["native_decisions"]}
        for thread, checksum in decisions.items():
            file(sessions[thread].relative_to(work).as_posix(), checksum)
        # Dedicated study archives deliberately have no config.toml, account store, logs DB or environment dump.
        contents = dict(schema=FORMAT, run_id=rid, mode=seal["mode"], series=seal["series"], files=entries,
                        source_commit=seal["source_commit"], synthetic_unavailable_source_files=sorted(unavailable))
        out.writestr(INDEX, json.dumps(contents, sort_keys=True, indent=2))
    return contents


def unpack(path, work):
    with zipfile.ZipFile(path) as packed:
        infos = packed.infolist()
        check(sum(i.file_size for i in infos) <= 512 * 1024**2, "Unexpected public study archive size")
        check(len({i.filename for i in infos}) == len(infos), "Duplicate archive members")
        index = json.loads(packed.read(INDEX))
        check(index["schema"] == FORMAT, "Unknown archive format")
        files = index["files"]
        check(all(re.fullmatch("[a-f0-9]{64}", v) for v in files.values()), "Invalid content digest")
        check({i.filename for i in infos} == {INDEX, *("blobs/" + v for v in files.values())}, "Unexpected archive member")
        extracted = 0
        for name, checksum in files.items():
            target = work / relative(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = packed.read("blobs/" + checksum)
            extracted += len(raw)
            check(extracted <= 600 * 1024**2 and sha(raw) == checksum, "Restored file differs from its published content")
            with target.open("xb") as out:
                out.write(raw)
    return index


def replay_native_calls(db, slot, rid, wire, seal):
    """Rejoin every host call to its acknowledged turn, GX request and immutable staged product."""
    sessions = native.native_session(slot / "codex/sessions")
    deliveries, staged, calls = {}, [], set()
    for w in wire:
        cid = w["call_id"]
        check(cid not in calls, "Duplicate scripted host call ID")
        calls.add(cid)
        row = db.execute("""
            SELECT c.thread_id,c.turn_id,c.request_json,r.result_json FROM p0_v6_native_calls c
            JOIN p0_root_calls r USING(thread_id,turn_id,call_id)
            JOIN p0_roots roots USING(thread_id) WHERE roots.run_id=? AND c.call_id=?
        """, (rid, cid)).fetchone()
        check(row is not None, "GX did not acknowledge an emitted host call")
        thread, turn = row["thread_id"], row["turn_id"]
        request, response = json.loads(row["request_json"]), json.loads(row["result_json"])
        check(request["tool"] == w["tool"] and request["arguments"] == w["args"] and
              request["callId"] == cid and request["turnId"] == turn and request["threadId"] == thread,
              "Wire and persisted tool arguments disagree")
        identity = (thread, turn, sha(w["brief_text"].encode()))
        if identity not in deliveries:
            info, context, utc, accepted, _ = native.delivered(sessions[thread], turn, w["brief_text"])
            check(info["model_provider"] == "gx_loopback" and context["model"] == seal["native_profile"]["model"] and
                  context["effort"] == seal["native_profile"]["effort"], "Unexpected native researcher profile")
            deliveries[identity] = accepted, utc
        accepted, utc = deliveries[identity]
        check(cid in accepted and accepted[cid][:2] == (w["tool"], w["args"]),
              "Official transcript does not confirm the same successful tool call")
        if w["tool"] == "gx_stage_claim":
            _, raw = native.artifact(db, slot, rid, response["claim_ref"])
            check(sha(raw) == response["sha256"], "Staged claim differs from GX native receipt")
            staged.append({"call_id": cid, "thread_id": thread, "turn_id": turn, "brief_delivered_utc": utc,
                           "claim_ref": response["claim_ref"], "claim_sha256": response["sha256"]})
    check(staged, "Missing original native claim staging")
    return len(calls), len(deliveries), staged


def verify_run(work, index):
    seal = read(work / "seal.json")
    result = read(work / "study-receipt.json")
    protocol = work / "source" / operate.PROTOCOL.relative_to(ROOT)
    rid = eligibility(seal, result, read(protocol))
    for script in (Path(independent_compare.__file__), Path(independent_geometry.__file__)):
        check(digest(script) == digest(work / "reviewer" / script.name), "Use the study's pinned independent reviewer")
    check(rid == index["run_id"] and seal["source_commit"] == index["source_commit"] and
          digest(work / "seal.json") == result["seal_sha256"] and digest(protocol) == seal["operator_reference_sha256"],
          "Restored Run, source baseline or protocol differs from registration")
    for name, pinned in seal["frozen_files"].items():
        if name in index["synthetic_unavailable_source_files"]:
            check(seal["mode"] == "SYNTHETIC_FIXTURE", "Unpinned source in public study")
        else:
            check(digest(source_file(work, "source/" + name)) == pinned, "Frozen scientific source changed")
    slot = work / "lead"
    boundary = read(work / "boundary.json")
    check(boundary["exit_code"] == 0 and boundary["fixture_namespace"] != boundary["outer_namespace"]
          and not boundary["ipv4_routes"] and set(boundary["ipv6_route_interfaces"]) <= {"lo"},
          "Expected isolated engineering-host boundary with loopback-only routes")
    rows = [json.loads(row) for row in (slot / "raw/requests.jsonl").read_bytes().splitlines()]
    check(rows and all(row.get("peer") in ("127.0.0.1", "::1") for row in rows), "Expected loopback request records")
    ledger = funding.BatchFunding(work / "custody/accounting.sqlite", series=seal["series"], slots=["lead"], batch_cap=1_000_000)
    wire = read(slot / "raw/wire.json")
    restored = run_receipt.collect(work, rid, seal, ledger, wire, rehearse.state_hash(slot, rid))
    check(restored == result, "Restored evidence differs from the original decision, Job or accounting receipt")
    numbers = []
    with closing(rehearse.db_open(slot)) as db:
        native_calls, deliveries, stages = replay_native_calls(db, slot, rid, wire, seal)
        for item, cell in zip(seal["inputs"], result["cells"], strict=True):
            raw = source_file(work, item["file"]).read_bytes()
            check(sha(raw) == cell["input_sha256"] == item["sha256"] and
                  digest(source_file(work, item["descriptor"])) == item["descriptor_sha256"], "Changed custody source")
            entry = item["entry"] if seal["mode"] == "PUBLIC_RETROSPECTIVE_CPU" else "SYN"
            for job in cell["jobs"]:
                _, admitted = native.artifact(db, slot, rid, job["input_ref"])
                check(admitted == raw, "Custody original differs from admitted input")
                # Complete independent parser, fit and controls before opening the claimed Job output.
                oracle = independent_geometry.reference(raw, entry, item["sha256"], job["mask"])
                output, = [o for o in job["outputs"] if o["output_name"] == "block-influence.json"]
                meta, data = native.artifact(db, slot, rid, output["artifact_ref"])
                check(meta["sha256"] == output["sha256"] == output["runner_sha256"] and meta["job_id"] == job["job_id"],
                      "Job, sealed named output and runner disagree")
                comparison = independent_compare.compare(oracle, json.loads(data))
                check(comparison["status"] == "PASS", "Independent numerical audit failed for " + job["job_id"])
                numbers.append(dict(entry=item["entry"], mask=job["mask"], job_id=job["job_id"], material_sha256=sha(raw),
                                    output_ref=output["artifact_ref"], output_sha256=sha(data),
                                    max_abs_errors=comparison["max_abs_errors"]))
    return dict(status="PASS", run_id=rid, mode=seal["mode"], source_commit=seal["source_commit"],
                protocol_sha256=seal["operator_reference_sha256"], reproduced_native_decisions=len(result["native_decisions"]),
                cold_state_sha256=result["cold_state_sha256"], published_claims=len(result["published_claims"]),
                named_outputs=numbers, paid_provider_calls=0, live_scientific_decisions=0,
                native_calls=native_calls, delivered_briefs=deliveries, staged_claims=stages,
                network_receipt_sha256=boundary["original_sha256"],
                file_count=len(index["files"]), synthetic_unavailable_source_files=index["synthetic_unavailable_source_files"])


def replay(directory):
    archive = directory / ARCHIVE
    with TemporaryDirectory() as temp:
        work = Path(temp)
        index = unpack(archive, work)
        result = verify_run(work, index)
    return {**result, "archive_sha256": digest(archive), "archive_bytes": archive.stat().st_size}


def pack(work, dest):
    check(not dest.exists(), "Choose a fresh output directory")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=dest.parent) as stage:
        directory = Path(stage) / "receipts"
        directory.mkdir()
        write_archive(work, directory)
        result = replay(directory)
        (directory / "replay.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        directory.rename(dest)
    return result


def verify_publication(fresh, published):
    # Independent LAPACK calculations can vary below the pre-registered numerical tolerance.
    # Each replay has already compared every individual endpoint with that tolerance.
    def stable(receipt):
        return {**receipt, "named_outputs": [{k: v for k, v in row.items() if k != "max_abs_errors"}
                                            for row in receipt["named_outputs"]]}
    check(stable(fresh) == stable(published), "Restored scientific or native identities differ from publication")
    for row in published["named_outputs"]:
        for endpoint, tolerance in independent_geometry.FIT_TOL.items():
            for prefix, factor in (("fit_", 1), ("delta_", 2)):
                check(0 <= row["max_abs_errors"][prefix + endpoint] <= factor * tolerance,
                      "Published independent error exceeds the reference tolerance")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("pack")
    create.add_argument("--work", required=True, type=Path)
    for command in (create, sub.add_parser("replay")):
        command.add_argument("--receipts", required=True, type=Path)
    args = parser.parse_args()
    directory = args.receipts.resolve()
    proof = pack(args.work.resolve(), directory) if args.command == "pack" else replay(directory)
    if args.command == "replay":
        verify_publication(proof, read(directory / "replay.json"))
    print(json.dumps(proof, ensure_ascii=False))
