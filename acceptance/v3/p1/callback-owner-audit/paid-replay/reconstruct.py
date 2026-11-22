"""Use immutable paid session/SQLite evidence to reconstruct only copies of a submission's inputs."""

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3

HERE = Path(__file__).resolve().parent
PERF = HERE.parents[2] / "performance"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc(ns):
    return (
        datetime.fromtimestamp(ns / 1e9, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def before(case, q):
    return q["callback_end_ns"] <= case["callback_start_ns"]


def cases():
    all_samples = list(map(json.loads, gzip.open(PERF / "callback-samples.jsonl.gz", "rt")))
    result = []
    for arm, build in (("baseline", "v21"), ("candidate", "p1")):
        for role in ("evidence_reviewer", "planner"):
            rows = sorted(
                (
                    r
                    for r in all_samples
                    if r["arm"] == arm
                    and r["role"] == role
                    and r.get("product_outcome") == "ACCEPTED"
                ),
                key=lambda r: -r["callback_ms"],
            )
            for rank, r in enumerate(
                rows[: 2 if (arm, role) == ("candidate", "planner") else 1], 1
            ):
                assert r["writer_count"] == 1 and r["official_success"] is True
                result.append((f'{build}-w{r["wave"]:02d}-{r["lane"]}-{role}', r, rank))
    for arm, build in (("baseline", "v21"), ("candidate", "p1")):
        formal = sorted(
            (
                r
                for r in all_samples
                if r["arm"] == arm and r.get("product_outcome") in ("ACCEPTED", "REJECTED")
            ),
            key=lambda r: r["callback_ms"],
        )
        r = formal[math.ceil(0.95 * len(formal)) - 1]
        assert r["product_outcome"] == "ACCEPTED" and r["role"] == "planner"
        rank = 1 + sum(
            x["callback_ms"] > r["callback_ms"]
            for x in formal
            if x["role"] == r["role"] and x["product_outcome"] == "ACCEPTED"
        )
        if not any(case["call_id"] == r["call_id"] for _, case, _ in result):
            result.append((f'{build}-w{r["wave"]:02d}-{r["lane"]}-planner-p95', r, rank))
    return result, all_samples


def fetch_official(stage, case, study):
    tid = case["thread_id"]
    (f,) = (stage / "codex-home" / "sessions").rglob("*" + tid + ".jsonl")
    (receipt,) = (
        r
        for r in json.loads((study / "native-session-hashes.json").read_bytes())
        if r["thread_id"] == tid
    )
    data = f.read_bytes()
    assert (receipt["file"], receipt["bytes"], receipt["sha256"]) == (f.name, len(data), sha(data))
    schemas = record = None
    for lineno, line in enumerate(data.splitlines(), 1):
        row = json.loads(line)
        p = row["payload"]
        item = p.get("item") or {}
        if row["type"] == "session_meta":
            assert p["id"] == tid
            schemas = p["dynamic_tools"]
        if (
            row["type"] == "event_msg"
            and p.get("type") == "item_completed"
            and item.get("type") == "DynamicToolCall"
            and item["id"] == case["call_id"]
        ):
            assert (
                record is None
                and p["turn_id"] == case["turn_id"]
                and item["tool"] == case["tool"]
                and item["success"] is True
            )
            record = (item, lineno, sha(line))
    assert schemas and record
    item, lineno, event_hash = record
    # The private rollout contains account and runtime context. Preserve only the exact public tool payload plus its opaque case/turn IDs.
    msg = {
        "method": "item/tool/call",
        "params": {
            "threadId": tid,
            "turnId": case["turn_id"],
            "callId": case["call_id"],
            "tool": item["tool"],
            "arguments": item["arguments"],
        },
    }
    expected = json.loads(item["content_items"][0]["text"])
    return (
        msg,
        expected,
        schemas,
        {
            "filename": f.name,
            "file_sha256": receipt["sha256"],
            "line_1based": lineno,
            "line_content_sha256": event_hash,
        },
    )


def artifact(state, row):
    path = state / "artifacts" / row["namespace"] / row["rel_path"]
    data = path.read_bytes()
    assert len(data) == row["bytes"] and sha(data) == row["sha256"]
    return data


def scientific_evidence(conn, state, inputs, product):
    def get(ref):
        row = conn.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
        assert row
        return json.loads(artifact(state, row))

    proposal = get(inputs["proposal_ref"])
    evidence = get(inputs["evidence_refs"][0])
    threshold = proposal["threshold"]
    routes = []
    for route in evidence["routes"]:
        ev = get(route["evaluation_ref"])
        data = get(route["observations_ref"])
        spec = get(route["input_ref"])
        assert (route["status"], route["outcome_kind"]) == ("SUCCEEDED", "valid_scientific_outcome")
        assert ev["input_ref"] == route["observations_ref"] and ev["threshold"] == threshold
        assert (
            ev["input_sha256"]
            == conn.execute(
                "SELECT sha256 FROM artifacts WHERE artifact_ref=?", (route["observations_ref"],)
            ).fetchone()[0]
        )
        rows = data["observations"] if isinstance(data, dict) else data
        scores = [r["score"] for r in rows]
        assert [{k: r[k] for k in ("candidate_id", "x")} for r in rows] == spec["candidates"]
        assert scores == [-((r["x"] - 2) ** 2) for r in rows]
        assert (max(scores), sum(s >= threshold for s in scores), len(scores)) == (
            ev["max_score"],
            ev["count_at_or_above"],
            ev["observations"],
        )
        pred = next(p for p in proposal["paths"] if p["id"] == route["path_id"])
        review = (
            product
            if inputs["output_kind"] == "science_assessment"
            else get(inputs["assessment_ref"])
        )
        (finding,) = (f for f in review["findings"] if f["path_id"] == route["path_id"])
        assert finding["evaluation_ref"] == route["evaluation_ref"] and finding["supported"] is (
            (ev["count_at_or_above"] > 0) == pred["expected_reaches_threshold"]
        )
        routes.append(
            {
                **{
                    k: route[k]
                    for k in (
                        "path_id",
                        "job_id",
                        "observations_ref",
                        "evaluation_ref",
                        "input_ref",
                        "eval_id",
                    )
                },
                "scores": scores,
                "threshold": threshold,
                "max_score": ev["max_score"],
                "count_at_or_above": ev["count_at_or_above"],
                "original_reviewer_supported": finding["supported"],
            }
        )
    assert len(routes) == 2
    return routes


def rewind(db, cutoff, first_event, call_rowid, case):
    # Work only on a private SQLite clone. All callback-visible records and files are adjusted as of its original first write.
    # Unconsumed final rows in unrelated bookkeeping tables may persist and are not claimed as a whole-workspace time-travel image.
    counts = {}
    deleted_fk = Counter()
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        )
    ]
    for tab in tables:
        cols = {r[1] for r in db.execute(f'PRAGMA table_info("{tab}")')}
        if "created_at" in cols:
            changed = db.execute(f'DELETE FROM "{tab}" WHERE created_at>?', (cutoff,)).rowcount
            if changed:
                counts[tab] = changed
    db.execute("DELETE FROM events WHERE event_id>=?", (first_event,))
    db.execute(
        "DELETE FROM artifacts WHERE NOT EXISTS (SELECT 1 FROM events WHERE kind='artifact.published' AND subject_id=artifact_ref)"
    )
    db.execute("DELETE FROM p0_root_calls WHERE rowid>=?", (call_rowid,))
    db.execute("DELETE FROM leases WHERE issued_at>?", (cutoff,))
    db.execute("UPDATE leases SET released_at=NULL WHERE released_at>?", (cutoff,))
    db.execute(
        "UPDATE outbox SET consumed_at=NULL,consumer=NULL,attempts=0,last_error=NULL WHERE consumed_at>?",
        (cutoff,),
    )
    db.execute(
        "DELETE FROM p0_root_turn_usage WHERE thread_id=? AND turn_id=?",
        (case["thread_id"], case["turn_id"]),
    )
    # Trim foreign-key children whose originating task/artifact/submission/lease was created in the future.
    while problems := db.execute("PRAGMA foreign_key_check").fetchall():
        for table, rowid in set((r[0], r[1]) for r in problems):
            assert rowid is not None
            deleted_fk[table] += db.execute(
                f'DELETE FROM "{table}" WHERE rowid=?', (rowid,)
            ).rowcount
    history = {}
    for (run_id,) in db.execute("SELECT run_id FROM runs").fetchall():
        st = db.execute(
            "SELECT * FROM events WHERE run_id=? AND subject_kind='run' AND new_state IS NOT NULL ORDER BY event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        rev = db.execute(
            "SELECT max(new_revision) FROM events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        checkpoint = db.execute(
            "SELECT interrupt_ref FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        db.execute(
            "UPDATE runs SET status=?,revision=?,interrupt_ref=?,updated_at=? WHERE run_id=?",
            (st["new_state"], rev, checkpoint[0], st["ts"], run_id),
        )
        if run_id == case["run_id"]:
            history.update(
                run_transition=st["event_id"], run_status=st["new_state"], run_revision=rev
            )
    for (t,) in db.execute("SELECT task_id FROM tasks").fetchall():
        st = db.execute(
            "SELECT * FROM events WHERE subject_kind='task' AND subject_id=? AND new_state IS NOT NULL ORDER BY event_id DESC LIMIT 1",
            (t,),
        ).fetchone()
        leases = db.execute(
            "SELECT * FROM leases WHERE task_id=? ORDER BY attempt DESC", (t,)
        ).fetchall()
        if st["new_state"] in ("CLAIMED", "RUNNING"):
            assert leases and leases[0]["released_at"] is None
            lease, owner = leases[0]["lease_id"], leases[0]["owner"]
        else:
            lease = owner = None
        db.execute(
            "UPDATE tasks SET owner=?,lease_id=?,attempt=?,status=?,updated_at=? WHERE task_id=?",
            (owner, lease, leases[0]["attempt"] if leases else 0, st["new_state"], st["ts"], t),
        )
        if t == case["task_id"]:
            assert (
                st["new_state"] == "CLAIMED"
                and case["attempt"] == leases[0]["attempt"]
                and leases[0]["principal"] == case["thread_id"]
            )
            history.update(
                task_transition=st["event_id"],
                task_status=st["new_state"],
                lease_id=lease,
                lease_expires_at=leases[0]["expires_at"],
            )
    db.execute("UPDATE p0_roots SET goal_desired='active' WHERE thread_id=?", (case["thread_id"],))
    for table, col in [("events", "event_id"), ("outbox", "outbox_id")]:
        db.execute(
            f"UPDATE sqlite_sequence SET seq=(SELECT COALESCE(max({col}),0) FROM {table}) WHERE name=?",
            (table,),
        )
    assert (
        db.execute("PRAGMA foreign_key_check").fetchall() == []
        and db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    )
    return history, counts, dict(deleted_fk)


def prepare(seed, case, rank, all_samples):
    study = PERF / ("gate0" if case["arm"] == "baseline" else "p1")
    stage = Path(json.loads((study / "start.json").read_text())["private_staging"])
    state = stage / f'state-{case["wave"]:02d}'
    orig = state / "gx.db"
    before_sha = sha(orig.read_bytes())
    wal = state / "gx.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    assert "scratchpad/v3-real-performance/" in str(state) and seed.is_relative_to("/tmp")
    seed.mkdir(parents=True)
    shutil.copyfile(orig, seed / "gx.db")
    msg, expected, schemas, custody = fetch_official(stage, case, study)
    product = msg["params"]["arguments"]["product"]
    args = msg["params"]["arguments"]
    cutoff = utc(case["callback_start_ns"])
    assert (
        len(canonical(args)) == case["canonical_argument_bytes"]
        and len(canonical(product)) == case["product_bytes"]
    )
    with closing(
        sqlite3.connect(orig.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as source, closing(sqlite3.connect(seed / "gx.db")) as db:
        source.row_factory = db.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        call = source.execute(
            "SELECT rowid,* FROM p0_root_calls WHERE thread_id=? AND call_id=?",
            (case["thread_id"], case["call_id"]),
        ).fetchone()
        assert (
            call["turn_id"] == case["turn_id"]
            and call["request_sha"] == sha(canonical([case["tool"], args]))
            and json.loads(call["result_json"]) == expected
        )
        lease = source.execute(
            "SELECT * FROM leases WHERE lease_id=(SELECT lease_id FROM submissions WHERE submission_id=?)",
            (case["submission_id"],),
        ).fetchone()
        task = source.execute("SELECT * FROM tasks WHERE task_id=?", (case["task_id"],)).fetchone()
        root = source.execute(
            "SELECT * FROM p0_roots WHERE thread_id=?", (case["thread_id"],)
        ).fetchone()
        assert (lease["lease_id"], root["task_id"], root["run_id"], root["attempt"]) == (
            root["lease_id"],
            case["task_id"],
            case["run_id"],
            case["attempt"],
        )
        names = json.loads(root["tool_names_json"])
        assert names == [t["name"] for t in schemas]
        sub = source.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (case["submission_id"],)
        ).fetchone()
        assert sub["content_sha256"] == sha(canonical(product)) and sub["brief_sha256"] == sha(
            canonical(root["brief"])
        )
        assert (
            sub["result"] == expected["task_status"] == "ACCEPTED"
            and sub["records_artifact_ref"] == expected["records_ref"]
        )
        (prod,) = json.loads(sub["artifact_refs_json"])
        assert prod == expected["artifact_ref"]
        inputs = json.loads(task["input_json"])
        assert inputs == json.loads(root["brief"].split("\n", 1)[1])["input"]
        out = source.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (prod,)).fetchone()
        assert artifact(state, out) == canonical(product)
        verdict = source.execute(
            "SELECT * FROM artifacts WHERE artifact_ref=?", (expected["records_ref"],)
        ).fetchone()
        assert json.loads(artifact(state, verdict))["would_accept"] is True
        edge = source.execute(
            "SELECT * FROM events WHERE kind='artifact.published' AND subject_id=?", (prod,)
        ).fetchone()
        assert cutoff < edge["ts"] <= utc(case["callback_end_ns"])
        # A single acquisition writer appends receipts in commit order. Check the chosen cutoff against all other callbacks from this workspace.
        prior = {}
        for r in all_samples:
            if (r["arm"], r["wave"]) != (case["arm"], case["wave"]):
                continue
            got = source.execute(
                "SELECT rowid FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                (r["thread_id"], r["call_id"]),
            ).fetchone()
            if got is not None and r["call_id"] != case["call_id"]:
                assert (got[0] < call["rowid"]) == before(case, r)
                if before(case, r) and r["thread_id"] == case["thread_id"]:
                    prior[r["call_id"]] = r["tool"]
        assert not list(
            source.execute(
                "SELECT artifact_ref FROM artifacts EXCEPT SELECT subject_id FROM events WHERE kind='artifact.published'"
            )
        )
        routes = scientific_evidence(source, state, inputs, product)
        history, counts, orphans = rewind(db, cutoff, edge["event_id"], call["rowid"], case)
        assert (
            history["run_revision"] == expected["revision"]
            and history["run_status"] == "WAITING_CLIENT"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM submissions WHERE submission_id=?", (case["submission_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM artifacts WHERE artifact_ref IN (?,?)",
                (prod, expected["records_ref"]),
            ).fetchone()[0]
            == 0
        )
        assert {
            r[0]
            for r in db.execute(
                "SELECT call_id FROM p0_root_calls WHERE thread_id=?", (case["thread_id"],)
            )
        } == set(prior)
        assert (
            db.execute(
                "SELECT count(*) FROM p0_root_deliveries WHERE thread_id=? AND turn_id=? AND state='acknowledged'",
                (case["thread_id"], case["turn_id"]),
            ).fetchone()[0]
            == 1
        )
        assert not db.execute(
            "SELECT 1 FROM p0_attempt_waits WHERE root_key=?", (root["root_key"],)
        ).fetchone()
        rows = db.execute("SELECT * FROM artifacts ORDER BY artifact_ref").fetchall()
        files = []
        for r in rows:
            content = artifact(state, r)
            rel = Path(r["namespace"]) / r["rel_path"]
            dst = seed / "artifacts" / rel
            assert not rel.is_absolute() and ".." not in rel.parts
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)
            # Published objects' staging namespace ancestors existed before that artifact was made durable.
            (seed / "artifacts" / ".staging" / r["namespace"]).mkdir(parents=True, exist_ok=True)
            files.append([r["artifact_ref"], str(rel), r["sha256"]])
        assert {r["artifact_ref"] for r in rows} == {
            r[0]
            for r in source.execute(
                "SELECT subject_id FROM events WHERE kind='artifact.published' AND event_id<?",
                (edge["event_id"],),
            )
        }
        nprior = db.execute(
            "SELECT count(*) FROM submissions WHERE task_id=?", (case["task_id"],)
        ).fetchone()[0]
        assert nprior == case["attempt"] - 1
        committed = {
            k: db.execute("SELECT count(*) FROM " + k).fetchone()[0]
            for k in [
                "tasks",
                "submissions",
                "leases",
                "artifacts",
                "events",
                "p0_root_calls",
                "p0_root_deliveries",
                "outbox",
            ]
        }
        assert (
            db.execute(
                "SELECT count(*) FROM tasks WHERE run_id=? AND status='PENDING'", (case["run_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM sources WHERE namespace=?", (case["run_id"],)
            ).fetchone()[0]
            == 0
        )
        db.commit()
    assert before_sha == sha(orig.read_bytes()) and (not wal.exists() or wal.stat().st_size == 0)
    earlier = sorted(
        (
            r
            for r in all_samples
            if r["arm"] == case["arm"] and r["thread_id"] == case["thread_id"] and before(case, r)
        ),
        key=lambda r: r["callback_end_ns"],
    )
    return {
        "source_acquisition": stage.name,
        "source_workspace": state.name,
        "source_final_db_sha256": before_sha,
        "sample": case,
        "role_rank_by_paid_accepted_wall": rank,
        "official": custody,
        "input": {"request": msg, "expected": expected, "schemas": schemas},
        "original_product_sha256": sub["content_sha256"],
        "original_brief_sha256": sub["brief_sha256"],
        "original_verdict_sha256": verdict["sha256"],
        "request_sha256": call["request_sha"],
        "manifest_sha256": sha(canonical(files)),
        "snapshot_db_sha256": sha((seed / "gx.db").read_bytes()),
        "science_routes": routes,
        "observed_rows": committed,
        "earlier_target_thread_tools": list(prior.values()),
        "target_task_previous_submissions": nprior,
        "latest_prior_callback_gap_ms": (case["callback_start_ns"] - earlier[-1]["callback_end_ns"])
        / 1e6,
        "expected_run_revision": expected["revision"],
        "clock_cutoff_utc": cutoff,
        "event_cut_exclusive": edge["event_id"],
        "native_call_rowid_exclusive": call["rowid"],
        "rows_removed_by_time": counts,
        "fk_dependents_removed": orphans,
        "rebound_owner": history,
    }
