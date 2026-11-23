"""Replay only the B3 brief transformation against preserved, paid grid assignments."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gx-h"))
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.p0 import grid_brief, roots


def replay(conn, run_id, row, state_name):
    root = roots.Roots(conn, run_id=run_id, cwd=Path.cwd())
    original = json.loads(row["brief"].removeprefix(grid_brief.PREFIX))
    task_id, key = original["task_id"], row["root_key"]
    context = {"task_id": task_id, "intent": original["intent"],
               "supplement_sources": original["supplement_sources"], **original.get("workflow_context", {})}
    # The old selected plugin read the assigned inputs and live context in its
    # hook. Rebuild only those original grants on a private database copy.
    conn.execute("DELETE FROM p0_root_artifact_reads WHERE root_key=?", (key,))
    root._allow_refs(key, [original["input"], context["intent"], context["supplement_sources"]])
    start = time.perf_counter_ns()
    rendered = grid_brief.render(root, key, original)
    elapsed = time.perf_counter_ns() - start
    packet = json.loads(rendered.removeprefix(grid_brief.PREFIX))
    actual = packet["preloaded_artifacts"]
    assets = ArtifactService(conn)
    for ref, entry in actual.items():
        raw = assets.read_bytes(ref)
        assert len(raw) <= grid_brief.PRELOAD_BYTES and hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert json.loads(raw) == entry["content"]
    source_bytes = sum(assets.describe(ref)["bytes"] for ref in actual)
    payload_bytes = len(ids.canonical_json(actual).encode())
    assert max(source_bytes, payload_bytes) <= grid_brief.PRELOAD_BYTES

    call_digests = {ids.sha256_of_obj(["gx_read_artifact", {"ref": ref}]): ref for ref in actual}
    unchanged_context = ids.sha256_of_obj(["gx_task_context", {}])
    reads, context_reads, other = [], [], 0
    for call_id, digest, result in conn.execute(
            "SELECT call_id,request_sha,result_json FROM p0_root_calls WHERE thread_id=? ORDER BY rowid",
            (row["thread_id"],)):
        if ref := call_digests.get(digest):
            assert json.loads(result) == actual[ref]["content"]
            reads.append({"call_id": call_id, "ref": ref})
        elif digest == unchanged_context and json.loads(result) == context:
            context_reads.append(call_id)
        else:
            other += 1
    return {"state": state_name, "run_id": run_id, "task_id": task_id, "attempt": row["attempt"],
            "role": row["role"], "historical_brief_sha256": ids.sha256_of_obj(row["brief"]),
            "replay_brief_sha256": ids.sha256_of_obj(rendered), "historical_brief_bytes": len(row["brief"].encode()),
            "replay_brief_bytes": len(rendered.encode()), "preloaded_source_bytes": source_bytes,
            "preloaded_payload_bytes": payload_bytes, "render_ns": elapsed,
            "verified_refs": {ref: entry["sha256"] for ref, entry in actual.items()},
            "lazy": packet.get("lazy_artifacts", {}), "historical_reads_now_in_brief": reads,
            "historical_exact_context_reads": context_reads, "other_historical_callbacks": other}


def main(args):
    rows = []
    for db in sorted(args.states.glob("state-*/gx.db")):
        uri = db.resolve().as_uri() + "?mode=ro&immutable=1"
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(uri, uri=True)) as source:
            path = Path(directory)
            (path / "artifacts").symlink_to((db.parent / "artifacts").resolve(), target_is_directory=True)
            with closing(sqlite3.connect(path / "gx.db")) as conn:
                source.backup(conn)
                conn.row_factory = sqlite3.Row
                for row in conn.execute("SELECT r.* FROM p0_roots r JOIN runs USING(run_id) WHERE "
                                        "workflow_id='science_grid' AND workflow_version='2' AND task_id IS NOT NULL "
                                        "ORDER BY r.task_id,r.attempt").fetchall():
                    rows.append(replay(conn, row["run_id"], row, db.parent.name))
    if not rows:
        raise ValueError("no historical science_grid@2 worker briefs found")
    result = {"measurement": "offline B3 rendering of preserved @2 briefs and matching callback receipts",
              "source": args.states.name, "limits_bytes": {"source_per_item": grid_brief.PRELOAD_BYTES,
                  "source_total": grid_brief.PRELOAD_BYTES, "serialized_preload_total": grid_brief.PRELOAD_BYTES},
              "aggregate": {"waves": len({r["state"] for r in rows}), "runs": len({r["run_id"] for r in rows}),
                  "assignments_by_role": dict(Counter(r["role"] for r in rows)), "verified_refs": sum(
                      len(r["verified_refs"]) for r in rows), "stale_preloaded_refs": 0,
                  "max_preloaded_payload_bytes": max(r["preloaded_payload_bytes"] for r in rows),
                  "historical_reads_now_in_brief": sum(len(r["historical_reads_now_in_brief"]) for r in rows),
                  "historical_exact_context_reads": sum(len(r["historical_exact_context_reads"]) for r in rows)},
              "assignments": rows}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
