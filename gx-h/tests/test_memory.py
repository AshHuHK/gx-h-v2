"""Accepted task artifacts can be retrieved across runs in their project."""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from gx_core import api
from gx_core.artifacts import ArtifactService
from gx_core.context import Principal
from gx_core.knowledge import MemoryService
from gx_core.knowledge.index_run import index_run
from gx_core.persistence import store


class ProjectMemory(unittest.TestCase):
    def test_index_scope_replay_and_withdrawal(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as conn:
            conn.execute(
                "INSERT INTO runs (run_id,project_ref,objective,status,created_at,updated_at) "
                "VALUES ('run','demo','Audit a CSV','COMPLETED','','')"
            )
            products = [
                ("good", "ACCEPTED", "text/markdown"),
                ("rejected", "REJECTED", "text/markdown"),
                ("binary", "ACCEPTED", "application/octet-stream"),
            ]
            refs = {}
            for name, result, media_type in products:
                conn.execute(
                    "INSERT INTO tasks (task_id,run_id,kind,role,status,created_at,updated_at) "
                    "VALUES (?,'run','research_round','researcher',?,'','')", (name, result),
                )
                product = ArtifactService(conn).publish_bytes(
                    namespace="run/tasks", rel_path=name, kind="module_result",
                    data=f"{name}: amber calibration curve".encode(), media_type=media_type,
                    task_id=name, run_id="run",
                )
                refs[name] = product.artifact_ref
                conn.execute(
                    "INSERT INTO submissions (submission_id,task_id,result,artifact_refs_json,created_at) "
                    "VALUES (?,?,?,?,'')", (name, name, result, json.dumps([product.artifact_ref])),
                )

            first = index_run(conn, "run")
            self.assertEqual(first, {"indexed": 1, "already_indexed": 0, "skipped": 1})
            self.assertEqual(index_run(conn, "run"), {"indexed": 0, "already_indexed": 1, "skipped": 1})
            reader = Principal("reader", project_refs=("demo",))
            other = Principal("other-reader", project_refs=("other",))
            search = {"query": "calibration", "project_scope": "demo"}
            found = api.dispatch("gx_memory_search", search, principal=reader, conn=conn)
            self.assertTrue(found["ok"], found.get("error"))
            [hit] = found["data"]["results"]
            read = {"doc_id": hit["doc_id"]}
            doc = api.dispatch("gx_memory_read", read, principal=reader, conn=conn)["data"]
            self.assertEqual(doc["body"], "good: amber calibration curve")
            self.assertEqual(doc["source_refs"], [refs["good"]])
            self.assertTrue(doc["hash_verified"])
            for tool, args in [("gx_memory_search", search), ("gx_memory_read", read)]:
                refused = api.dispatch(tool, args, principal=other, conn=conn)
                self.assertEqual(refused["error"]["code"], "SCOPE_DENIED")

            memory = MemoryService(conn)
            memory.retract(hit["doc_id"], reason="fixture correction")
            self.assertEqual(index_run(conn, "run")["indexed"], 0)
            self.assertFalse(memory.search("calibration", ["real:demo"])["results"])
