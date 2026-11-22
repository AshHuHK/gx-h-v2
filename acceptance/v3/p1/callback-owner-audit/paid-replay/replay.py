"""Interleave historical accepted callback envelopes on identical disposable shared-FS clones."""

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import gzip
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import sqlite3
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

from reconstruct import HERE, PERF, canonical, cases, prepare, sha

sys.path.insert(0, str(HERE.parent))
from run import PINS


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def quant(values):
    v = sorted(values)
    return {
        "n": len(v),
        "p50": round(statistics.median(v), 5),
        "p95": round(v[math.ceil(0.95 * len(v)) - 1], 5),
        "max": round(max(v), 5),
    }


def summary(rows):
    both = {}
    for name in ["all", *sorted({r["case"] for r in rows})]:
        group = [r for r in rows if not r["warmup"] and (name == "all" or r["case"] == name)]
        v = {}
        for arm in PINS:
            bare = [r for r in group if r["arm"] == arm and not r["traced"]]
            tr = [r for r in group if r["arm"] == arm and r["traced"]]
            v[arm] = {
                k: quant(r[k] for r in bare)
                for k in [
                    "gx_callback_ms",
                    "gx_writer_ms",
                    "wrapper_wall_ms",
                    "cpu_ms",
                    "gx_writer_lock_ms",
                    "outside_writer_ms",
                ]
            }
            v[arm]["traced"] = {
                k: quant(r["trace"][k] for r in tr)
                for k in [
                    "file_fsync_ms",
                    "dir_fsync_ms",
                    "total_fsync_ms",
                    "begin_ms",
                    "commit_ms",
                    "writer_metric_record_ms",
                    "entry_to_product_durable_ms",
                    "product_durable_to_submission_ms",
                    "submission_to_verdict_durable_ms",
                    "after_lease_outbox_to_commit_end_ms",
                    "post_commit_to_callback_ms",
                ]
            }
            v[arm]["above_100ms"] = sum(r["gx_callback_ms"] > 100 for r in bare)
        p0 = {(r["case"], r["round"]): r for r in group if not r["traced"] and r["arm"] == "v21"}
        p1 = {(r["case"], r["round"]): r for r in group if not r["traced"] and r["arm"] == "p1"}
        assert p0.keys() == p1.keys()
        d = [p1[k]["gx_callback_ms"] - p0[k]["gx_callback_ms"] for k in p0]
        rng = random.Random(334320)
        boot = sorted(statistics.median(rng.choices(d, k=len(d))) for _ in range(4000))
        v["paired_p1_minus_v21"] = {
            "median_ms": statistics.median(d),
            "bootstrap_95pct_pair_median_ms": [boot[99], boot[3899]],
            "p1_slower_pairs": sum(x > 0 for x in d),
            "p1_faster_pairs": sum(x < 0 for x in d),
            "n": len(d),
            "marginal_p95_difference_ms": v["p1"]["gx_callback_ms"]["p95"]
            - v["v21"]["gx_callback_ms"]["p95"],
        }
        both[name] = v
    return both


def log_prefix(source, target, expected_ns):
    end = None
    with source.open("rb") as f, target.open("wb") as out:
        while line := f.readline():
            obj = json.loads(line)
            if obj.get("kind") == "db_transaction" and obj["observed_ns"] == expected_ns:
                end = out.tell()
                break
            out.write(line)
    assert end is not None
    return end


def checkout(repos, temp):
    stages = {}
    hashes = {}
    for arm, pin in PINS.items():
        raw = subprocess.check_output(
            ["git", "-C", str(repos[arm]), "archive", pin, "gx-h", "bin", "examples"]
        )
        stage = temp / arm
        stage.mkdir()
        hashes[arm] = sha(raw)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            tar.extractall(stage, filter="data")
        stages[arm] = stage
    return stages, hashes


def run_worker():
    _, _, release, tmp, storage, arm, cpu = sys.argv
    release, tmp, storage = Path(release), Path(tmp), Path(storage)
    sys.path.insert(0, str(release / "gx-h"))
    from gx_core import metrics
    from gx_core.domain import clock
    from gx_core.hostobs.app_server_client import ReceivedMessage
    from gx_core.orchestration import service
    from gx_core.p0 import roots
    from gx_core.persistence import store

    os.sched_setaffinity(0, {int(cpu)})
    fixtures = json.loads((tmp / "fixtures.json").read_bytes())
    obs = None
    work = {n: storage / n for n in fixtures}
    seed = {n: tmp / "seeds" / n for n in fixtures}
    dirs = {
        n: {p.relative_to(d / "artifacts") for p in (d / "artifacts").rglob("*") if p.is_dir()}
        for n, d in seed.items()
    }
    print('{"ready":true}', flush=True)
    for line in sys.stdin:
        name, traced = json.loads(line)
        f = fixtures[name]
        req = f["input"]["request"]
        c = f["sample"]
        dst = work[name]
        casefiles = seed[name]
        database = dst / "gx.db"
        metrics_path = dst / "metrics.jsonl"
        assert not (dst / "gx.db-wal").exists() and not (dst / "gx.db-shm").exists()
        shutil.copyfile(casefiles / "gx.db", database)
        assert sha(database.read_bytes()) == f["snapshot_db_sha256"]
        with metrics_path.open("r+b") as m:
            m.truncate(f["metrics_seed_bytes"])
        os.environ["GX_METRICS_FILE"] = str(metrics_path)
        clock.reset()
        clock.advance((clock.parse(f["clock_cutoff_utc"]) - clock.now()).total_seconds())
        sql_times = {}
        marks = {}

        class Timed(sqlite3.Connection):
            def execute(self, query, *params):
                if obs is None:
                    return super().execute(query, *params)
                if query in ("BEGIN IMMEDIATE", "COMMIT"):
                    start = time.perf_counter_ns()
                    out = super().execute(query, *params)
                    at = time.perf_counter_ns()
                    sql_times[query] = (at - start) / 1e6
                    marks[query] = at
                    return out
                out = super().execute(query, *params)
                if query.startswith("INSERT INTO artifacts("):
                    marks["product_row" if "product_row" not in marks else "verdict_row"] = (
                        time.perf_counter_ns()
                    )
                elif (
                    query.startswith("INSERT INTO events(")
                    and params[0][2] == "task.transition"
                    and params[0][7] == "SUBMITTED"
                ):
                    marks["submitted"] = time.perf_counter_ns()
                elif query.startswith("INSERT INTO outbox("):
                    marks["outbox"] = time.perf_counter_ns()
                return out

        real_connect = sqlite3.connect
        if traced:
            sqlite3.connect = lambda *a, **kw: real_connect(*a, **kw, factory=Timed)
        try:
            db = store.open_store(database)
        finally:
            sqlite3.connect = real_connect
        with closing(db):
            for table in ("tasks", "outbox"):
                store._columns(db, table)
            w = service.hooks_for_run(db, c["run_id"]).build_worker_tools(db, c["run_id"])
            extension = list(w.specs)
            built = {s["name"] for s in (*roots.WORKER_TOOLS, *extension)}
            missing = [s for s in f["input"]["schemas"] if s["name"] not in built]
            assert not missing or (
                arm == "v21" and [s["name"] for s in missing] == ["gx_grid_measurement"]
            )
            # Replay the paid Root's exact original tool schema list using its build's normal constructor.
            # v2.1 sees the original extension's declaration on a P1-origin Root; gx_submit_product never calls it.
            kw = {"worker_prepare": w.prepare} if hasattr(w, "prepare") else {}
            root = roots.Roots(
                db,
                run_id=c["run_id"],
                cwd=dst,
                worker_tools=[*extension, *missing],
                worker_call=w.call,
                **kw,
            )
            bound = roots.require_binding(db, c["run_id"], c["thread_id"])
            assert root._tools_for(bound) == f["input"]["schemas"]
            root.active_turns[c["thread_id"]] = c["turn_id"]
            assert (
                c["attempt"] == bound["attempt"]
                and bound["lease_id"] == f["rebound_owner"]["lease_id"]
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                    (c["thread_id"], c["call_id"]),
                ).fetchone()[0]
                == 0
            )
            # A small part of the original in-memory heartbeat timer is not persisted. Use the same conservative due-check on both builds.
            # The original lease has nearly its full hour left and never renews in these cases.
            assert (
                clock.parse(f["rebound_owner"]["lease_expires_at"]) - clock.now()
            ).total_seconds() > 1800
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            sync_mode = db.execute("PRAGMA synchronous").fetchone()[0]
            prewrites = db.total_changes
            received = ReceivedMessage(1, req)
            syncs = []
            metrics_times = {}
            real_fsync, real_record = os.fsync, metrics.record

            def timed_sync(fd):
                kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                start = time.perf_counter_ns()
                try:
                    return real_fsync(fd)
                finally:
                    end = time.perf_counter_ns()
                    target = "verdict" if "product_row" in marks else "product"
                    syncs.append((target, kind, (end - start) / 1e6))
                    marks[target + "_durable"] = end

            def timed_record(kind, *a, **kw):
                start = time.perf_counter_ns()
                try:
                    return real_record(kind, *a, **kw)
                finally:
                    metrics_times[kind] = (time.perf_counter_ns() - start) / 1e6

            if traced:
                os.fsync = timed_sync
                metrics.record = timed_record
                obs = True
            start, cpu = time.perf_counter_ns(), time.process_time_ns()
            try:
                resp = root._callback(received)
            finally:
                elapsed, used = (time.perf_counter_ns() - start) / 1e6, (
                    time.process_time_ns() - cpu
                ) / 1e6
                obs = None
                os.fsync = real_fsync
                metrics.record = real_record
            assert resp["success"], resp
            ans = json.loads(resp["contentItems"][0]["text"])
            expected = f["input"]["expected"]
            assert ans["task_status"] == "ACCEPTED" and (ans["run_id"], ans["revision"]) == (
                expected["run_id"],
                expected["revision"],
            )

            def stripped(v):
                return {k: x for k, x in v.items() if k != "records_ref"}

            assert (
                stripped(ans["validation"]) == stripped(expected["validation"])
                and len(set((ans["submission_id"], expected["submission_id"]))) == 2
            )
            sub = db.execute(
                "SELECT * FROM submissions WHERE submission_id=?", (ans["submission_id"],)
            ).fetchone()
            assert (
                sub["task_id"],
                sub["lease_id"],
                sub["attempt"],
                sub["content_sha256"],
                sub["brief_sha256"],
            ) == (
                c["task_id"],
                bound["lease_id"],
                c["attempt"],
                f["original_product_sha256"],
                f["original_brief_sha256"],
            )
            files = []
            for ref, digest in (
                (ans["artifact_ref"], f["original_product_sha256"]),
                (ans["records_ref"], f["original_verdict_sha256"]),
            ):
                r = db.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
                file = dst / "artifacts" / r["namespace"] / r["rel_path"]
                assert r["sha256"] == digest == sha(file.read_bytes())
                files.append((file, dst / "artifacts" / ".staging" / r["namespace"]))
            receipt = db.execute(
                "SELECT request_sha FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                (c["thread_id"], c["call_id"]),
            ).fetchone()
            assert receipt[0] == f["request_sha256"]
            assert (
                db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0]
                == f["observed_rows"]["p0_root_deliveries"]
            )
            assert db.total_changes - prewrites == 17
            # Read the actual production metric records outside the timed interval, including the normal append inside the writer-exit path.
            with metrics_path.open("rb") as fp:
                fp.seek(f["metrics_seed_bytes"])
                tail = list(map(json.loads, fp))
            writer, cb = tail
            assert [q["kind"] for q in tail] == ["db_transaction", "p0_callback"] and cb[
                "thread_id"
            ] == c["thread_id"]
            row = {
                "case": name,
                "arm": arm,
                "traced": traced,
                "request_sha256": receipt[0],
                "gx_callback_ms": cb["duration_ms"],
                "gx_writer_ms": writer["duration_ms"],
                "gx_writer_lock_ms": writer["lock_acquire_ms"],
                "outside_writer_ms": cb["duration_ms"] - writer["duration_ms"],
                "cpu_ms": used,
                "wrapper_wall_ms": elapsed,
                "writes": db.total_changes - prewrites,
                "result": sub["result"],
                "synchronous": sync_mode,
                "schema_metadata_supplied_to_v21": bool(missing),
            }
            if traced:
                assert len(syncs) == 13 and sum(x[1] == "file" for x in syncs) == 2
                assert (
                    marks["product_durable"]
                    < marks["product_row"]
                    < marks["submitted"]
                    < marks["verdict_durable"]
                    < marks["verdict_row"]
                    < marks["outbox"]
                    < marks["COMMIT"]
                )
                row["trace"] = {
                    "file_fsync_ms": sum(s for p, k, s in syncs if k == "file"),
                    "dir_fsync_ms": sum(s for p, k, s in syncs if k == "dir"),
                    "total_fsync_ms": sum(s for p, k, s in syncs),
                    "raw_fsyncs": syncs,
                    "begin_ms": sql_times["BEGIN IMMEDIATE"],
                    "commit_ms": sql_times["COMMIT"],
                    "writer_metric_record_ms": metrics_times["db_transaction"],
                    "callback_metric_record_ms": metrics_times["p0_callback"],
                    "entry_to_product_durable_ms": (marks["product_durable"] - start) / 1e6,
                    "product_durable_to_submission_ms": (
                        marks["submitted"] - marks["product_durable"]
                    )
                    / 1e6,
                    "submission_to_verdict_durable_ms": (
                        marks["verdict_durable"] - marks["submitted"]
                    )
                    / 1e6,
                    "after_lease_outbox_to_commit_end_ms": (marks["COMMIT"] - marks["outbox"])
                    / 1e6,
                    "post_commit_to_callback_ms": cb["duration_ms"]
                    - (marks["COMMIT"] - start) / 1e6,
                }
        # Remove exactly the two newly generated artifacts and their newly created parent/staging directories on the reusable clone.
        base = dst / "artifacts"
        original_dirs = dirs[name]
        for file, staging in files:
            file.unlink()
            for d in (file.parent, staging):
                while d != base and d.exists() and d.relative_to(base) not in original_dirs:
                    d.rmdir()
                    d = d.parent
        assert not (dst / "gx.db-wal").exists() and not (dst / "gx.db-shm").exists()
        print(json.dumps(row, separators=(",", ":")), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v21", required=True, type=Path)
    parser.add_argument("--p1", required=True, type=Path)
    parser.add_argument("--storage", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--bare-pairs", type=int, default=20)
    parser.add_argument("--traced-pairs", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261122)
    a = parser.parse_args()
    assert a.bare_pairs > 0 and a.traced_pairs > 0
    storage = a.storage.resolve()
    assert storage.name == "v3-real-performance"
    a.out.mkdir(parents=True, exist_ok=True)
    repos = {"v21": a.v21.resolve(), "p1": a.p1.resolve()}
    cases_in, samples = cases()
    raw_rows = []
    affinity = sorted(os.sched_getaffinity(0))
    cpu = affinity[len(affinity) // 2]
    rng = random.Random(a.seed)
    info = {
        "start": utcnow(),
        "command": [sys.executable, *sys.argv],
        "python": sys.version,
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "pins": PINS,
        "cpu": cpu,
        "filesystem": subprocess.check_output(
            ["findmnt", "-T", str(storage), "-n", "-o", "FSTYPE"], text=True
        ).strip(),
        "diagnostic_sha256": {
            f: sha((HERE / f).read_bytes()) for f in ("reconstruct.py", "replay.py")
        },
        "parent_run_py_sha256": sha((HERE.parent / "run.py").read_bytes()),
        "input_samples_sha256": sha((PERF / "callback-samples.jsonl.gz").read_bytes()),
        "rng_seed": a.seed,
        "strace": shutil.which("strace"),
    }
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="gx-paid-submit-src-"
    ) as temp, tempfile.TemporaryDirectory(
        dir=storage, prefix="callback-paid-replay-"
    ) as shared, ExitStack() as stack:
        tmp = Path(temp)
        data = Path(shared)
        info["disposable_shared_directory"] = str(data)
        stages, info["archive_sha256"] = checkout(repos, tmp)
        fixtures = {}
        for name, row, rank in cases_in:
            seed = tmp / "seeds" / name
            f = prepare(seed, row, rank, samples)
            dst = data / name
            dst.mkdir()
            shutil.copytree(seed / "artifacts", dst / "artifacts")
            f["metrics_seed_bytes"] = log_prefix(
                storage / f["source_acquisition"] / "metrics.jsonl",
                dst / "metrics.jsonl",
                row["writer_samples"][0]["end"],
            )
            fixtures[name] = f
        (tmp / "fixtures.json").write_bytes(canonical(fixtures))
        names = list(fixtures)
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "GX_SCOPE": "operator",
            "GX_PROJECTS": "*",
            "GX_P0_CODEX": str(tmp / "no-transport"),
        }
        workers = {}
        for arm, stage in stages.items():
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(HERE / "replay.py"),
                    "worker",
                    str(stage),
                    str(tmp),
                    str(data),
                    arm,
                    str(cpu),
                ],
                cwd=stage,
                env={**env, "PYTHONPATH": str(stage / "gx-h")},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            workers[arm] = child
            stack.callback(child.wait)
            stack.callback(child.stdin.close)
            stack.callback(child.stdout.close)
            assert json.loads(child.stdout.readline())["ready"]
        # Randomized blocks contain each case once. Arm order is balanced within every case and measurement type.
        orders = {}
        for name in names:
            for traced, n in ((False, a.bare_pairs), (True, a.traced_pairs)):
                firsts = (list(PINS) * math.ceil(n / 2))[:n]
                rng.shuffle(firsts)
                orders[name, traced] = firsts
        plans = (
            [(-1, False, True)]
            + [(i, False, False) for i in range(a.bare_pairs)]
            + [(i, True, False) for i in range(a.traced_pairs)]
        )
        rest = plans[1:]
        rng.shuffle(rest)
        plans = plans[:1] + rest
        for block, (roundid, traced, warmup) in enumerate(plans):
            sequence = names.copy()
            rng.shuffle(sequence)
            for name in sequence:
                first = rng.choice(list(PINS)) if warmup else orders[name, traced][roundid]
                for order, arm in enumerate((first, next(k for k in PINS if k != first))):
                    proc = workers[arm]
                    proc.stdin.write(json.dumps([name, traced]) + "\n")
                    proc.stdin.flush()
                    reply = proc.stdout.readline()
                    if not reply:
                        raise RuntimeError(f"{arm} failed on {name} round {roundid}")
                    raw_rows.append(
                        dict(
                            json.loads(reply),
                            round=roundid,
                            block=block,
                            arm_order_in_pair=order,
                            warmup=warmup,
                        )
                    )
            if block % 5 == 0:
                print(f"{utcnow()} completed block {block}; {len(raw_rows)} callbacks", flush=True)
    info.update(
        finish=utcnow(),
        disposable_removed=not data.exists(),
        bare_pairs_per_case=a.bare_pairs,
        probed_pairs_per_case=a.traced_pairs,
    )
    with gzip.open(a.out / "samples.jsonl.gz", "wt") as f:
        for r in raw_rows:
            print(canonical(r).decode(), file=f)
    with gzip.open(a.out / "official-envelopes.jsonl.gz", "wt") as f:
        for n, fx in fixtures.items():
            print(canonical({"case": n, **fx["input"]}).decode(), file=f)
    for f in fixtures.values():
        del f["input"]
    report = {
        "metadata": info,
        "fixtures": fixtures,
        "measurements": summary(raw_rows),
        "raw_sha256": {
            name: sha((a.out / name).read_bytes())
            for name in ("samples.jsonl.gz", "official-envelopes.jsonl.gz")
        },
    }
    (a.out / "results.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps(report["measurements"]["all"]), flush=True)


if __name__ == "__main__":
    run_worker() if len(sys.argv) > 1 and sys.argv[1] == "worker" else main()
