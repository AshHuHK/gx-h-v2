import json
import unittest
from contextlib import closing
from unittest.mock import patch

from gx_core.capabilities import shipped
from gx_core.errors import StaleRevision, VersionImmutable
from gx_core.p0 import v6_jobs
from gx_core.persistence import store
from tests import test_v6_jobs, test_v6_native


class CapabilityViews(unittest.TestCase):
    def test_revisions_and_replay(self):
        from gx_core.p0 import capability_views as views
        from gx_core.plugins import goal

        run = test_v6_native.ScientificLead()
        self.addCleanup(run.doCleanups)
        run.setUp()
        conn, rid = run.conn, run.run
        snapshot = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
        enabled = dict(enable=shipped.GRID, expected_revision=0, expected_digest=shipped.pin(shipped.GRID),
                       request_id="first")
        with self.assertRaises(VersionImmutable):
            views.change(conn, rid, **dict(enabled, expected_digest="0"*64))
        first = views.change(conn, rid, **enabled)
        self.assertEqual(1, first["revision"])
        self.assertEqual([shipped.GRID], [r["capability_version"] for r in goal.catalog(conn, rid)])
        self.assertTrue(shipped.participates(conn, rid, shipped.GRID))
        with closing(store.open_store(conn.execute("PRAGMA database_list").fetchone()["file"])) as other:
            with self.assertRaises(StaleRevision):
                views.change(other, rid, disable=shipped.GRID, expected_revision=0, request_id="stale")
            second = views.change(other, rid, disable=shipped.GRID, expected_revision=1, request_id="second")
            self.assertEqual(2, second["revision"])
            self.assertEqual(first, views.change(other, rid, **enabled))
        self.assertFalse(shipped.participates(conn, rid, shipped.GRID))
        self.assertEqual(views.current(conn, rid)["active"], {})
        self.assertEqual(snapshot,conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
        self.assertEqual(1,conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0])

    def test_disable_preserves_admitted_job_but_blocks_pending_node_after_reopen(self):
        from gx_core.p0 import capability_views as views
        from gx_core.jobs.service import JobService
        job = test_v6_jobs.DynamicJobs()
        self.addCleanup(job.doCleanups)
        job.setUp()
        job.new("first")
        # Interrupt after the Job owner commits, before the detached process starts.
        with patch.object(JobService, "_start", side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):
                v6_jobs.advance(job.conn, job.run_id)
        jid = job.conn.execute("SELECT job_id FROM v6_nodes WHERE node_id='first'").fetchone()[0]
        self.assertTrue(jid)
        job.new("second")
        views.change(job.conn, job.run_id, disable=shipped.GRID, expected_revision=0, request_id="withdraw")
        job.connect()
        for _ in range(100):
            v6_jobs.advance(job.conn, job.run_id)
            found = dict(job.conn.execute("SELECT node_id,status FROM v6_nodes"))
            if found == {"first":"SUCCEEDED","second":"BLOCKED"}:
                break
            import time
            time.sleep(0.05)
        self.assertEqual(found, {"first":"SUCCEEDED","second":"BLOCKED"})
        self.assertEqual([jid], [r[0] for r in job.conn.execute("SELECT job_id FROM jobs")])
        gap = job.conn.execute("SELECT payload_json FROM events WHERE kind='v6.node.settled' AND subject_id='second'").fetchone()
        self.assertEqual(json.loads(gap[0])["dependency"]["capability"], shipped.GRID)

    def test_public_native_discovery_and_scoped_selection(self):
        import tempfile
        from pathlib import Path
        from tests.test_v62_modules_publication import Scenario
        from gx_core.errors import ScopeDenied
        from gx_core.p0 import capability_views

        method = "pcna-archive-search@1"
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        grant = {"evidence_policy":"gx.evidence/6.2.4", "research_effort":"deep", "research_modules":[],
                 "capabilities":[method], "plugins":["org.gx.pcna-archive@1.0.0"],
                 "capability_options":[{"method":shipped.GRID, "digest":shipped.pin(shipped.GRID)}]}
        s = self.enterContext(Scenario(directory, spec_updates=grant))
        query = lambda kind, call, **args: s.call(s.lead, "gx_query_catalog", {"kind":kind, **args}, call)
        summary = query("database", "find", query="archive")["items"][0]
        self.assertEqual((summary["reference"], summary["status"]), (method, "available"))
        self.assertNotIn("inputs", summary)
        detail = query("database","detail",reference=method)["spec"]
        self.assertEqual(summary["digest"], detail["portable_sha256"])
        self.assertTrue(detail["inputs"])
        native = query("research","readers",reference="gx_read_source@1")["spec"]
        self.assertEqual(native["handler"],"gx_read_source")
        skill = query("skill","skills",reference="source-evidence-review@1")["spec"]
        self.assertIn("gx_extract_evidence",skill["tools"])
        self.assertEqual(query("recipe", "recipes")["total"],0)
        args = {"action":"enable", "revision":0, "method":shipped.GRID}
        response = s.call(s.lead, "gx_select_capability", args, "select")
        self.assertEqual(response["revision"],1)
        self.assertEqual(response,s.call(s.lead, "gx_select_capability",args,"select"))
        with self.assertRaises(ScopeDenied):
            s.call(s.lead,"gx_select_capability",dict(args,method="artifact-analysis@1"),"outside")
        self.assertIn(shipped.GRID,capability_views.current(s.conn,s.run_id)["active"])
