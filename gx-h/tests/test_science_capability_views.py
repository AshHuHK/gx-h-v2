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
        views.change(job.conn, job.run_id, disable=shipped.GRID, expected_revision=0, request_id="old-view")
        installed = views.change(job.conn, job.run_id, enable=shipped.GRID, expected_revision=1,
                                 expected_digest=shipped.pin(shipped.GRID), request_id="selected-method")
        job.new("first")
        # Interrupt after the Job owner commits, before the detached process starts.
        with patch.object(JobService, "_start", side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):
                v6_jobs.advance(job.conn, job.run_id)
        jid = job.conn.execute("SELECT job_id FROM v6_nodes WHERE node_id='first'").fetchone()[0]
        self.assertTrue(jid)
        from gx_core.p0.manifest import read_manifest
        binding = read_manifest(job.conn, jid)["admission"]["goal_capability"]
        self.assertEqual(binding, {"revision":2, "view_sha256":installed["digest"],
                                  "portable_sha256":shipped.pin(shipped.GRID)})
        job.new("second")
        views.change(job.conn, job.run_id, disable=shipped.GRID, expected_revision=2, request_id="withdraw")
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
        dependency = json.loads(gap[0])["dependency"]
        self.assertEqual(dependency["capability"], shipped.GRID)
        proof = read_manifest(job.conn, jid)
        self.receipt = {"run_id":job.run_id, "job_id":jid, "admission_sha256":proof["admission_hash"],
                        "completion_sha256":proof["manifest_hash"], "accepted_binding":binding,
                        "statuses":found, "pending_gap":dependency, "actual_jobs":1}

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
        self.assertFalse(any(card["status"] == "available" for card in query("recipe", "recipes")["items"]))
        option = query("method", "selectable", reference=shipped.GRID)
        self.assertFalse(option["execution_granted"])
        self.assertEqual(option["spec"]["status"], "selectable")
        args = {"action":"enable", "revision":0, "method":shipped.GRID}
        response = s.call(s.lead, "gx_select_capability", args, "select")
        self.assertEqual(response["revision"],1)
        self.assertEqual(response,s.call(s.lead, "gx_select_capability",args,"select"))
        with self.assertRaises(ScopeDenied):
            s.call(s.lead,"gx_select_capability",dict(args,method="artifact-analysis@1"),"outside")
        self.assertIn(shipped.GRID,capability_views.current(s.conn,s.run_id)["active"])
        import subprocess
        import sys
        from gx_core import config
        command = [sys.executable, config.HARNESS_ROOT.parent/"bin/gx-p0", "capabilities",
                   "--state", s.state, "--run-id", s.run_id]
        saved = json.loads(subprocess.check_output(command))
        self.assertEqual(saved, response)
        removed = json.loads(subprocess.check_output([*command,"--disable",shipped.GRID,
                                                      "--revision","1","--request-id","operator-choice"]))
        self.assertEqual(removed["revision"],2)
        self.assertNotIn(shipped.GRID,removed["active"])
        from tests.test_v62_modules_publication import draft
        s.call(s.lead,"gx_decide_revision",{"decision":draft()},"wait")
        successor = s.next_actor("run")
        next_packet = s.brief(successor)
        self.assertEqual((next_packet["capability_view"]["revision"], next_packet["capability_view"]["disabled"]),
                         (2, shipped.GRID))
        self.assertEqual(next_packet["goal_version"],s.brief(s.lead)["goal_version"])

    def test_two_simultaneous_view_writers_and_prepared_graph(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from gx_core.p0 import capability_views as views, v6_decisions
        from gx_core.plugins import goal

        native = test_v6_native.ScientificLead()
        self.addCleanup(native.doCleanups)
        native.setUp()
        conn, rid = native.conn, native.run
        methods = (shipped.GRID, shipped.ARTIFACT_ANALYSIS)
        for revision, method in enumerate(methods):
            views.change(conn, rid, enable=method, expected_revision=revision, request_id="select-"+method)
        plan = v6_decisions.prepare(conn, run_id=rid, thread_id=native.thread, turn_id=native.turn,
            task_id=native.task, lease_id=native.row["lease_id"], call_id="graph", decision=native.body)
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
        gate = Barrier(2)
        validate = goal.tools_for_catalog

        def meet(rows):
            validate(rows)
            gate.wait(timeout=10)
            return {}

        def disable(method):
            with closing(store.open_store(db_file)) as writer:
                try:
                    answer = views.change(writer, rid, disable=method, expected_revision=2, request_id="drop-"+method)
                    return "committed", answer
                except StaleRevision:
                    return "stale", None
        with patch.object(goal, "tools_for_catalog", side_effect=meet), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(disable, methods))
        self.assertCountEqual([outcome for outcome,_ in outcomes],["committed","stale"])
        self.assertEqual(len(views.current(conn, rid)["active"]),1)
        with self.assertRaises(StaleRevision),store.transaction(conn,transaction_owner="test",operation="adopt"):
            v6_decisions.commit(conn,plan)

    def test_researcher_can_inspect_only_its_own_tools_and_report_after_method_disable(self):
        import tempfile
        from pathlib import Path
        from tests.test_v62_modules_publication import Scenario
        from gx_core.errors import ScopeDenied
        from gx_core.p0 import capability_views, v62_modules

        search = "scholarly-search@1"
        fetch = "scholarly-fetch@1"
        grant = {"evidence_policy":"gx.evidence/6.2.4", "research_effort":"deep",
                 "research_modules":["scholarly_investigation@1"], "capabilities":[search, fetch],
                 "plugins":["org.gx.scholarly-sources@1.0.0"]}
        s = self.enterContext(Scenario(Path(self.enterContext(tempfile.TemporaryDirectory())), spec_updates=grant))
        opening = dict(s.open_args("literature", caps=[search]), module_id="scholarly_investigation",
                       focus="literature", expected_delivery="Assess accessible original passages and gaps")
        first = s.call(s.lead,"gx_open_module",opening,"open")
        researcher = s.next_actor(first["scope"])
        def query(method, key):
            return s.call(researcher, "gx_query_catalog", {"kind":"research","reference":method}, key)
        self.assertEqual(query(search, "own")["spec"]["reference"], search)
        self.assertEqual(query("gx_read_source@1", "source")["spec"]["handler"], "gx_read_source")
        with self.assertRaises(ScopeDenied):
            query(fetch,"other")
        capability_views.change(s.conn,s.run_id,disable=search,expected_revision=0,request_id="disable")
        v62_modules.require_active(s.conn,s.run_id,first["scope"])
        with self.assertRaises(ScopeDenied):
            query(search,"disabled")
        report = s.call(researcher,"gx_submit_module_report",
            {"operation_key":"gap","status":"PARTIAL","findings":[],"artifact_refs":[],
             "gaps":["The original was not acquired before the search method was withdrawn."],
             "next_steps":["Select another journal lookup."]},"gap")
        self.assertTrue(report["report_ref"])
