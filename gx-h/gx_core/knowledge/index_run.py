"""Index published text products from accepted submissions in their project's memory."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..artifacts import ArtifactService
from ..native_release import native_state
from ..persistence import store
from .memory import MemoryService
from .scopes import namespace_for_run

# Search indexes represent findings and notes. Large data files remain in the artifact store.
MAX_DOCUMENT_BYTES = 256 * 1024


def index_run(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    namespace = namespace_for_run(conn, run_id)
    artifacts = ArtifactService(conn)
    memory = MemoryService(conn)
    counts = {"indexed": 0, "already_indexed": 0, "skipped": 0}
    rows = conn.execute(
        "SELECT DISTINCT a.artifact_ref, a.kind, a.rel_path, a.bytes, a.media_type, a.task_id "
        "FROM tasks t JOIN submissions s ON s.task_id = t.task_id "
        "JOIN json_each(s.artifact_refs_json) r "
        "JOIN artifacts a ON a.artifact_ref = r.value AND a.task_id = t.task_id AND a.run_id = t.run_id "
        "WHERE t.run_id = ? AND t.status = 'ACCEPTED' AND s.result = 'ACCEPTED' AND a.published = 1 "
        "ORDER BY a.artifact_ref", (run_id,),
    )
    for row in rows:
        media_type = (row["media_type"] or "").split(";", 1)[0].strip().lower()
        if media_type not in {"application/json", "text/plain", "text/markdown"} or row["bytes"] > MAX_DOCUMENT_BYTES:
            counts["skipped"] += 1
            continue
        ref = row["artifact_ref"]
        import_ref = f"accepted-artifact:{ref}"
        # Artifact references are immutable. A withdrawn record stays withdrawn on subsequent indexing.
        if conn.execute("SELECT 1 FROM memory_docs WHERE namespace = ? AND import_ref = ?",
                        (namespace, import_ref)).fetchone():
            counts["already_indexed"] += 1
            continue
        data = artifacts.read_bytes(ref, max_bytes=MAX_DOCUMENT_BYTES)
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError:
            counts["skipped"] += 1
            continue
        if not body.strip():
            counts["skipped"] += 1
            continue
        memory.write(namespace=namespace, memory_class="EPISODE", title=row["rel_path"],
                     body=body, source_refs=[ref], import_ref=import_ref,
                     meta={"run_id": run_id, "task_id": row["task_id"], "kind": row["kind"]},
                     actor="memory-index")
        counts["indexed"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="existing native state directory")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not (args.state_dir / "gx.db").is_file():
        parser.error("state directory has no gx.db")
    with native_state(args.state_dir) as state, closing(store.open_store(state.db_path)) as conn:
        print(json.dumps(index_run(conn, args.run_id), sort_keys=True))


if __name__ == "__main__":
    main()
