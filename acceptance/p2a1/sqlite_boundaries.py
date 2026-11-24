"""Audit P2A.1 writer changes and measure production operations on file-backed SQLite."""
import argparse
import ast
from collections import Counter
from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.phase2a.performance import SQLITE_TIMES, quantiles, transaction_summary
from gx_core import metrics
from gx_core.persistence import store

BASE = "61ea96daa218719a7663cb8fc8b404256560b124"
PRODUCT = ("gx-h/gx_core", "gx-h/examples/p01_fixture.py")
SQL_WRITE = re.compile(r"^\s*(?:INSERT|UPDATE|REPLACE|DELETE|CREATE|ALTER|DROP)\b", re.I)
TX_CONTROL = re.compile(r"^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def call_name(node):
    return (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None)


def inventory(source, path):
    """AST nodes retain lexical ownership and file lines. Comments/line moves stay unchanged."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    entries = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name == "transaction":
            entry = {"kind": "transaction"}
        elif name in {"execute", "executemany", "executescript"} and node.args:
            text = node.args[0]
            if isinstance(text, ast.JoinedStr) and text.values:
                text = text.values[0]
            sql = text.value if isinstance(text, ast.Constant) and isinstance(text.value, str) else ""
            if TX_CONTROL.search(sql):
                entry = {"kind": "raw_transaction"}
            elif SQL_WRITE.search(sql):
                entry = {"kind": "sql_write"}
            else:
                continue
        elif name in {"commit", "rollback"}:
            entry = {"kind": "raw_transaction"}
        else:
            continue
        ancestry = []
        parent = parents.get(node)
        while parent is not None:
            ancestry.append(parent)
            parent = parents.get(parent)
        functions = [a.name for a in reversed(ancestry) if isinstance(a, (ast.FunctionDef, ast.ClassDef))]
        scope = ".".join(functions)
        entry.update(path=path, line=node.lineno, function=scope)
        fingerprint = node
        if name == "transaction":
            labels = {k.arg: k.value for k in node.keywords}
            for key in ("transaction_owner", "operation"):
                label = labels.get(key)
                options = [label.body, label.orelse] if isinstance(label, ast.IfExp) else [label]
                values = [item.value for item in options if isinstance(item, ast.Constant)
                          and isinstance(item.value, str) and item.value and item.value != "unattributed"]
                entry[key] = values if len(values) == len(options) else []
            rid = labels.get("run_id")
            deferred = labels.get("created_run")
            entry["run_id"] = ast.unparse(rid) if rid else None
            entry["created_run"] = ast.unparse(deferred) if deferred else None
            surrounding = next((a for a in ancestry if isinstance(a, ast.With)), None)
            if surrounding is not None:
                fingerprint = surrounding
        key = (scope, ast.dump(fingerprint))
        entries.setdefault(key, []).append(entry)
    return entries


def source_audit():
    base = git("rev-parse", "--verify", BASE + "^{commit}").strip()
    paths = set(git("diff", "--name-only", base, "--", *PRODUCT).splitlines())
    paths.update(git("ls-files", "--others", "--exclude-standard", "--", *PRODUCT).splitlines())
    changed = []
    for path in sorted(paths):
        if not path.endswith(".py") or not (ROOT / path).is_file():
            continue
        existed = git("ls-tree", "--name-only", base, "--", path).strip()
        before = inventory(git("show", f"{base}:{path}"), path) if existed else {}
        after = inventory((ROOT / path).read_text(), path)
        for signature, instances in after.items():
            changed.extend(instances[len(before.get(signature, ())):])
    bad = [r for r in changed if r["kind"] == "raw_transaction" or r["kind"] == "transaction" and (
        not r["transaction_owner"] or not r["operation"]
        or (r["run_id"] in (None, "None") and r["created_run"] in (None, "None")))]
    return {"baseline": base, "source_commit": git("rev-parse", "HEAD").strip(),
            "changed_files": sorted(paths), "changed_entries": changed, "unlabeled_or_raw_writers": bad}


class Observer:
    """Observe actual SQLite writer state during existing expensive owner calls and sink emission."""
    def __init__(self):
        self.entries = Counter()
        self.frames = ContextVar("sqlite_boundary_frames", default=())
        self.calls = Counter()
        self.inside = Counter()

    def observe(self, name):
        self.calls[name] += 1
        for conn, owner, operation in reversed(self.frames.get()):
            if conn.in_transaction:
                self.inside[name, owner, operation] += 1
                break

    def sample(self, owner, name):
        original = getattr(owner, name)
        label = f"{owner.__name__}.{name}"

        @wraps(original)
        def call(*args, **kwargs):
            self.observe(label)
            return original(*args, **kwargs)
        return patch.object(owner, name, call)

    def track_transactions(self):
        original = store.transaction

        @contextmanager
        def tracked(conn, **kwargs):
            owner, op = kwargs.get("transaction_owner"), kwargs.get("operation")
            self.entries[owner, op, "nested" if conn.in_transaction else "physical"] += 1
            frame = (conn, owner, op)
            token = self.frames.set((*self.frames.get(), frame))
            try:
                with original(conn, **kwargs) as tx:
                    yield tx
            finally:
                self.frames.reset(token)
        return patch.object(store, "transaction", tracked)


def usage_sample(work):
    from acceptance.closure.test_usage import UsageClosure
    from gx_core.domain import clock
    from gx_core.errors import GxError
    from gx_core.p0 import metering, operator

    fixture = UsageClosure()
    fixture.setUp()
    try:
        conn, rid = fixture.db, fixture.run
        turn = fixture.interrupted()
        fixture.start_child(turn, "audit-child", "audit-child-turn")
        metering.remember_host_history(conn, rid, "root", "root", {"id": "root", "path": str(work / "synthetic.log")})
        ref = fixture.inspect("interrupted")["turns"][0]["reservation_ref"]
        # Exercise a historical hold without a usable grant, as in the existing NO_GRANT case.
        conn.execute("UPDATE p0_root_deliveries SET token_budget=NULL WHERE reservation_ref=?", (ref,))
        conn.execute("UPDATE budget_reservations SET amounts_json=? WHERE reservation_ref=?", ('{"api_calls":1}', ref))
        clock.advance(31)
        metering.maintain(conn, rid)
        ctx = operator.local_context(conn)

        def decide(mid, decision, **fields):
            action = {"kind": "usage_resolve", "decision": decision, "reservation_ref": ref,
                      "reason": "review synthetic receipt", **fields}
            return operator.submit(ctx, rid, action, mid)["result"]

        for kind in ("usage", "audit", "usage"):
            operator.submit(ctx, rid, {"kind": kind}, "read-" + kind)
        accepted = decide("accept", "accept-exposure")
        try:
            decide("stale", "release-hold", acceptance_event_id=-1, approve_release=True)
        except GxError:
            pass
        else:
            raise AssertionError("a stale release succeeded")
        try:
            decide("missing-log", "attach-evidence", thread_id="root", turn_id=turn,
                   response_id="missing", source="codex-rollout")
        except GxError:
            pass
        else:
            raise AssertionError("a missing history was accepted")
        decide("release", "release-hold", acceptance_event_id=accepted["operator_event_id"], approve_release=True)
        assert conn.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?", (ref,)).fetchone()[0] == "RELEASED"
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA query_only=ON")
            assert metering.inspect(reader, rid)["cost_report"] and not reader.in_transaction
    finally:
        fixture.doCleanups()


def parked_engine(conn):
    """Exercise the real checkpoint writer and recheck while two committed Jobs hold both slots."""
    from acceptance.closure.profile_probe import CAPABILITY
    from acceptance.hardening import cpu_fairness as case
    from gx_core.errors import DependencyUnmet
    from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
    from gx_core.orchestration import service
    from gx_core.orchestration.engine import Engine, Node, NodeResult, Workflow
    from gx_core.orchestration.registry import WorkflowRegistry

    def admit(ctx):
        row = JobService(ctx.conn).submit(run_id=ctx.run_id, capability_version=CAPABILITY, purpose=PURPOSE_WORKFLOW,
            input_manifest_ref=ctx.inputs["manifest"], idempotency_key="waiting-slot", parameters={"operation": "count"})
        return NodeResult(outputs={"job_id": row["job_id"]})

    flow = Workflow(case.FLOW, "1", [Node("admit", admit, work="stage"),
                    Node("independent", case.independent, work="mechanical"), Node("result", case.result, ("admit",))],
                    stage_rules={"admit": case.admitted}, completion_evidence=case.closed)
    registry = WorkflowRegistry([flow])
    service.set_registry(registry)
    try:
        engine = Engine(conn, registry)
        run = case.start(engine, {}, "parked admission", 1)
        rid = run["id"]
        ref = engine.waits(rid)["admit"]["interrupt_ref"]
        try:
            engine.resume(run_id=rid, interrupt_ref=ref)
        except DependencyUnmet:
            pass
        else:
            raise AssertionError("the occupied slots unexpectedly admitted the waiting stage")
        assert engine.waits(rid)["admit"]["interrupt_ref"] == ref
    finally:
        service.set_registry(None)


def workloads(work, observer, run_ids):
    import os
    from acceptance.closure.profile import test_environment
    from acceptance.closure.profile_probe import CAPABILITY, measure_client, measure_resources
    from gx_core.capabilities import ladder, registry
    from gx_core.p0 import manifest
    from gx_core.jobs import capacity, run_resources, wakeup
    from gx_core.p0 import metering_report, operator, operator_usage
    from gx_core.persistence import records

    trace = work / "all-metrics.jsonl"
    env = test_environment(work / "process")
    env["GX_METRICS_FILE"] = str(trace)
    original_create = records.create_run

    @wraps(original_create)
    def create(*args, **kwargs):
        rid = original_create(*args, **kwargs)
        run_ids.add(rid)
        return rid

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(observer.track_transactions())
        stack.enter_context(patch.object(records, "create_run", create))
        for owner, name in ((wakeup, "notify"), (capacity, "cpu_state"), (capacity, "jobs_state"),
                            (capacity, "sample_admission"), (run_resources, "observe"),
                            (manifest, "_source"), (manifest, "_runtime"),
                            (operator, "_read"), (operator_usage, "prepare_history"),
                            (metering_report, "classified_cost"), (metrics, "record_transaction")):
            stack.enter_context(observer.sample(owner, name))
        with closing(store.open_store(work / "run-start.db")) as conn:
            view = measure_client(conn)
            assert view["run_started"], view["run_start_error"]
            replay = measure_client(conn)
            assert replay["run_started"] and conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        with closing(store.open_store(work / "jobs.db")) as conn:
            room = measure_resources(conn, real=False)
            assert room["held_when_full"] == 2 and room["free_after_cancel"] == 1
            parked_engine(conn)
            ctx = operator.local_context(conn)
            cap = registry.resolve(conn, CAPABILITY)
            rid = ladder.probe_run(ctx, cap)
            assert ladder.probe_run(ctx, cap) == rid
        usage_sample(work)



def measure_queue_head(work):
    """Measure the real admission lookup at increasing queue sizes in an independent WAL database."""
    from gx_core.jobs import cpu_queue
    from gx_core.persistence import records

    results = []
    count = 0
    with closing(store.open_store(work / "queue-size.db")) as conn:
        for size in (1, 100, 400):
            with store.transaction(conn, transaction_owner="Fixture", operation="seed_cpu_waits"):
                for i in range(count, size):
                    rid = records.create_run(conn, objective="Synthetic queued Job", project_ref="scale", actor="fixture")
                    conn.execute("UPDATE runs SET status='ACTIVE' WHERE run_id=?", (rid,))
                    state = {"node_waits": {str(n): {"reason": "pending", "metadata": "x" * 128} for n in range(12)},
                             "resumptions": {}, "context": "x" * 2048}
                    store.save_checkpoint(conn, run_id=rid, workflow_id="scale", workflow_version="1", node="cpu", state=state)
                    cpu_queue.park(conn, rid, "cpu", "key-" + str(i), 2, "2026-11-24T00:00:00Z", i + 1)
            count = size
            tables, samples = set(), []

            def reads(action, first, second, database, cause):
                if action == sqlite3.SQLITE_READ:
                    tables.add(first)
                return sqlite3.SQLITE_OK

            conn.set_authorizer(reads)
            try:
                for _ in range(5):
                    with store.transaction(conn, transaction_owner="Fixture", operation="read_cpu_head"):
                        started = time.monotonic_ns()
                        head = cpu_queue.positions(conn, 2, head_only=True)
                        samples.append((time.monotonic_ns() - started) / 1_000_000)
                        assert [r["job_key"] for r in head] == ["key-0"]
            finally:
                conn.set_authorizer(None)
            results.append({"waiting_runs": size, "tables_read": sorted(tables), "lookup_ms": quantiles(samples),
                            "head_only_without_checkpoint_reads": "job_cpu_waits" in tables and "checkpoints" not in tables})
    return results


def run(out):
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("supply an empty evidence directory")
    audit = source_audit()
    write_json(out / "source-audit.json", audit)
    observation = Observer()
    run_ids = set()
    with tempfile.TemporaryDirectory(prefix="gx-p2a1-sqlite-") as tmp:
        work = Path(tmp)
        try:
            workloads(work, observation, run_ids)
            head = measure_queue_head(work)
            error = None
        except Exception as exc:
            import traceback
            traceback.print_exc()
            head, error = [], type(exc).__name__
        raw = work / "all-metrics.jsonl"
        measured = [json.loads(line) for line in raw.read_text().splitlines()] if raw.exists() else []
    required = {("JobService", "admit"), ("Operator", "submit"), ("Operator", "apply"), ("Operator", "reject"),
                ("Usage", "maintain"), ("Usage", "remember_official_history"), ("Run", "run_start"),
                ("Capability", "create_probe_run"), ("P01Fixture", "prepare_run"),
                ("Engine", "checkpoint_node"), ("Engine", "resume")}
    labels = required | {(owner, op) for r in audit["changed_entries"] if r["kind"] == "transaction"
                         for owner in r["transaction_owner"] for op in r["operation"]}
    # The existing generic idempotency wrapper also serves unrelated, workspace-scoped calls.
    labels.discard(("Store", "run_idempotent"))
    samples = [row for row in measured if row["kind"] == "db_transaction"
               and (row["transaction_owner"], row["operation"]) in labels]
    pairs = {(row["transaction_owner"], row["operation"]) for row in samples}
    with (out / "transactions.jsonl").open("w") as stream:
        for row in samples:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    write_json(out / "summary.json", transaction_summary(samples))
    evidence = [{"owner": owner, "operation": op, "physicality": mode, "count": n}
                for (owner, op, mode), n in observation.entries.items() if (owner, op) in labels]
    held = [{"call": call, "owner": owner, "operation": op, "count": count}
            for (call, owner, op), count in observation.inside.items()]
    starts = [r for r in samples if (r["transaction_owner"], r["operation"]) == ("Run", "run_start")]
    required_calls = ("capacity.sample_admission", "manifest._source", "manifest._runtime", "run_resources.observe",
                      "wakeup.notify", "operator._read", "operator_usage.prepare_history", "metering_report.classified_cost")
    covered = all(any(name.endswith(suffix) and count > 0 for name, count in observation.calls.items())
                  for suffix in required_calls)
    checks = {"all_changed_transactions_tagged": not audit["unlabeled_or_raw_writers"],
              "observed_writers": required <= pairs,
              "heavy_operations_outside_writer": covered and not held,
              "indexed_fair_queue_head": bool(head) and all(r["head_only_without_checkpoint_reads"] for r in head),
              "run_start_replay_identity": len(starts) == 2 and len({r["run_id"] for r in starts}) == 1,
              "run_ids_match_created_runs": bool(samples) and all(r["run_id"] in run_ids for r in samples),
              "complete_phase_timings": all(r["duration_ms"] == r["lock_wait_ms"] + r["body_ms"] + r["finish_ms"]
                                            and all(r[field] >= 0 for field in SQLITE_TIMES) for r in samples),
              "workloads_completed": error is None}
    passed = all(checks.values())
    write_json(out / "result.json", {"case_id": "SQLITE-BOUNDARIES", "source_commit": audit["source_commit"],
                  "status": "PASS" if passed else "FAIL", "passed": passed, "evidence_class": "SCRIPTED",
                  "checks": checks, "run_ids": sorted(run_ids), "writer_samples": len(samples),
                  "outside_call_counts": {name: n - sum(r["count"] for r in held if r["call"] == name)
                                          for name, n in observation.calls.items()},
                  "calls_inside_writer": held, "observed_boundaries": evidence,
                  "unobserved_required_labels": sorted(required - pairs),
                  "queue_scaling": head, "workload_error": error})
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(0 if run(parser.parse_args().output) else 1)
