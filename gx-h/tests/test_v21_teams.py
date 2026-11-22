"""Team allocation, atomic Root slots and original task/Job/turn ledgers on real GX stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
from threading import Barrier
import tempfile
import unittest
from unittest.mock import patch

from gx_core import budgets, config
from gx_core.domain import clock
from gx_core.capabilities import shipped, storage
from gx_core.context import Ctx, Principal
from gx_core.errors import BudgetExceeded, HoldActive, IdempotencyConflict
from gx_core.jobs.service import JobService
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.orchestration import expansion
from gx_core.p0 import compute, team_admission as teams, workspace
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from gx_core.teams.service import TeamService
from tests.test_p0_root_delivery import Peer


def group(key, roles=(), *, parallel=1, tokens=100):
    return {"team_id":key, "name":key.title(), "max_parallel_roots":parallel, "roles":list(roles),
            "limits":{"worker_tasks":5, "job_submissions":2, "cpu_seconds":70, "api_calls":4, "tokens":tokens}}


class WorkspaceTeams(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.db = store.open_store(self.home / "gx.db")
        self.addCleanup(self.db.close)
        for name, value in (("gx_core.p0.roots.tempfile.gettempdir",str(self.home)),
                            ("gx_core.hostobs.host_policy.p0_role_for_run", {"model":"fixture","effort":"high","provider":"openai"}),
                            ("gx_core.hostobs.host_policy.p0_provider_for_run", "openai")):
            mock = patch(name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        temp = patch.object(config, "RUNTIME_VAR_DIR", self.home / "var")
        temp.start()
        self.addCleanup(temp.stop)

    def run_case(self, key, definitions=(), *, parallel=3, maximum=5):
        spec = {"workspace":{"max_parallel_roots":maximum}, "max_parallel_roots":parallel,
                "teams":list(definitions), "run_budget":{"worker_tasks":25, "cpu_seconds":200,
                    "job_submissions":8, "api_calls":12, "tokens":500}}
        with store.transaction(self.db) as conn:
            rid = records.create_run(conn, objective="Explore a small grid", project_ref="team-test", actor="test",
                                     inputs={"inputs":{"p0":True,"p0_spec":spec}})
            ref = workspace.attach_run(conn, rid, project_ref="team-test", run_key=key,
                                       profile_name="p0", spec=spec, actor="test")
            conn.execute("UPDATE runs SET budget_ref=?,status='ACTIVE' WHERE run_id=?", (ref,rid))
            teams.configure(conn,rid,spec)
        return rid, ref

    def create(self, rid, role="evidence_reviewer", team=None, inputs=None):
        return TeamService(self.db).create_task(run_id=rid, kind="research_round", role=role, team_id=team,
                                                 bundle_kind="research_bundle", input=inputs or {}, acceptance={})

    def roots(self, run, db=None):
        return Roots(db or self.db, run_id=run, cwd=self.home, token_budget=40)

    def occupy(self, roots, task, name):
        row = roots._claim(task)
        roots._bind(row, {"cwd":row["cwd"], "id":name, "sessionId":name, "source":"appServer"})
        return roots._binding(name)

    def yield_task(self, task):
        with store.transaction(self.db) as db:
            row = db.execute("SELECT lease_id FROM tasks WHERE task_id=?", (task,)).fetchone()
            store.release_lease(db, row[0])
            store.transition(db, machine="task", subject_id=task, to_state="READY", actor="test", reason="return",
                             extra_updates={"owner":None,"lease_id":None})

    def test_atomic_allotments_expansion_and_task_attribution(self):
        alpha, beta = group("alpha", ["evidence_reviewer"]), group("beta", ["planner"])
        rid, run_budget = self.run_case("one", (alpha,beta))
        before = self.db.total_changes
        teams.configure(self.db,rid,{"teams":[alpha,beta]})
        self.assertEqual(self.db.total_changes,before)
        with self.assertRaises(IdempotencyConflict):
            teams.configure(self.db,rid,{"teams":[{**alpha,"name":"Changed"}]})
        with self.assertRaises(BudgetExceeded):
            teams.configure(self.db, rid, {"teams":[group("small", tokens=1),group("huge", tokens=3000)]})
        active = {r["team_id"]:r for r in teams.team_status(self.db, rid)}
        self.assertEqual(set(active), {"alpha","beta"})
        self.assertEqual(budgets.state(self.db,run_budget)["reserved"]["tokens"], 200)
        plan = dict(rule="mixed work", key="mixed-team-expansion", trigger={"test":"team"},
                    tasks=[{"role":role,"kind":"research_round","bundle_kind":"research_bundle"}
                           for role in ("evidence_reviewer","planner")])
        ctx = Ctx(self.db, Principal(subject="test",scope="operator"))
        result = expansion.expand(ctx,rid,**plan)
        self.assertTrue(expansion.expand(ctx,rid,**plan)["replayed"])
        for tid,key in zip(result["task_ids"],("alpha","beta")):
            task = TeamService(self.db).task(tid)
            self.assertEqual((task["team_id"],task["budget_ref"]), (key,active[key]["budget_ref"]))
        explicit = self.create(rid, team="beta")
        self.assertEqual(explicit["team_id"],"beta")
        self.assertEqual(teams.team_funding(self.db,rid,explicit["task_id"],run_budget),active["beta"]["budget_ref"])
        self.assertEqual(self.db.execute("SELECT assignment_source FROM p0_task_teams WHERE task_id=?",
                                         (explicit["task_id"],)).fetchone()[0],"explicit")
        self.assertEqual(budgets.state(self.db,active["beta"]["budget_ref"])["spent"]["worker_tasks"],2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM teams").fetchone()[0],0)
        other, other_budget = self.run_case("second")
        lone = self.create(other)
        self.assertNotIn("team_id",lone)
        self.assertEqual(lone["budget_ref"],other_budget)
        self.assertNotEqual(lone["budget_ref"],run_budget)
        self.observed = {"checks":["F03","F04","F08"],"run_id":rid,"run_budget":run_budget,
            "funded_teams":{k:r["budget_ref"] for k,r in active.items()},"reserved_tokens":200,
            "expansion_tasks":result["task_ids"],"explicit_team_task":explicit["task_id"],"untagged_task":lone["task_id"],
            "replayed":True,"over_allocation_rolled_back":True,"legacy_teams":0}

    def test_atomic_last_workspace_slot_across_run_connections_and_team_isolation(self):
        alpha, beta = group("analysis", ["evidence_reviewer"]), group("other", ["planner"])
        first,_ = self.run_case("a",(alpha,beta),parallel=2, maximum=2)
        second,_ = self.run_case("b",(alpha,),parallel=2,maximum=2)
        jobs = [self.create(first)["task_id"],self.create(first)["task_id"],self.create(first,"planner")["task_id"],
                self.create(second)["task_id"]]
        self.roots(first)._claim(jobs[0])
        with self.assertRaises(HoldActive) as quota:
            self.roots(first)._claim(jobs[1])
        self.assertEqual(quota.exception.details["scope"],"team")
        barrier = Barrier(2)
        def claim(item):
            run, task = item
            with closing(store.open_store(self.home / "gx.db")) as other:
                barrier.wait(timeout=10)
                try:
                    self.roots(run,other)._claim(task)
                    return "admitted"
                except HoldActive as exc:
                    return exc.details["scope"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(claim,[(first,jobs[2]),(second,jobs[3])]))
        self.assertCountEqual(result,["admitted","workspace"])
        used = teams._occupancy(self.db, None)
        self.assertEqual(len(used),2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM leases").fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0],0)
        self.observed = {"checks":["F05","F06"],"run_ids":[first,second],"workspace_cap":2,"contenders":result,
                         "team_denial":quota.exception.details,"occupied":[dict(r) for r in used],"lease_count":2}

    def test_run_cap_pause_resume_and_cold_reacquisition(self):
        first,_ = self.run_case("a",[group("analysis",["evidence_reviewer"]),group("other",["planner"])],
                                parallel=1,maximum=2)
        roots = self.roots(first)
        a = self.create(first)["task_id"]
        b = self.create(first,role="planner")["task_id"]
        owner = roots._claim(a)
        with self.assertRaises(HoldActive) as run_limit:
            roots._claim(b)
        self.assertEqual(run_limit.exception.details["scope"],"run")
        teams.team_control(self.db,first,"analysis","pause")
        with self.assertRaises(HoldActive) as paused:
            roots._claim(a)
        self.assertEqual(paused.exception.details["reason"],"team_paused")
        roots._claim(b)
        self.assertEqual(teams.team_control(self.db,first,"analysis","resume")["active_roots"],0)
        with closing(store.open_store(self.home / "gx.db")) as cold:
            with self.assertRaises(HoldActive) as reacquire:
                self.roots(first,cold)._claim(a)
            self.assertEqual(reacquire.exception.details["scope"],"run")
            self.assertEqual(len(teams._occupancy(cold,None)),1)
        self.yield_task(b)
        self.assertEqual(roots._claim(a)["lease_id"],owner["lease_id"])
        self.assertEqual(teams.team_control(self.db,first,"analysis","status")["active_roots"],1)
        self.observed = {"checks":["F05","F07"],"first_run":first,"original_lease":owner["lease_id"],
                         "other_task":b,"paused_denial":paused.exception.details,"cold_resume_denial":reacquire.exception.details,
                         "restored_lease":roots._claim(a)["lease_id"],"active_roots":1}

    def test_park_releases_a_slot_and_wake_reacquires_it_before_steering_the_original_turn(self):
        rid,_ = self.run_case("a",[group("analysis",["evidence_reviewer"])],parallel=1,maximum=1)
        roots = self.roots(rid)
        a,b = (self.create(rid)["task_id"] for _ in range(2))
        binding = self.occupy(roots,a,"root")
        peer = Peer(self.db,rid)
        roots._request = peer._request
        roots._goal_for_budget = lambda *args:None
        roots.loaded.add("root")
        original_turn = roots.dispatch(a)["turn_id"]
        self.db.execute("INSERT INTO jobs(job_id,run_id,task_id,lease_ref,capability_id,capability_version,"
                        "status,idempotency_key,submit_intent_json,input_manifest_ref,created_at) "
                        "VALUES('external',?,?,?,'measurement','1','RUNNING','external','{}','input',?)",
                        (rid,a,binding["lease_id"],clock.now_iso()))
        with store.transaction(self.db):
            self.assertTrue(roots._park_job(binding,"external",turn_id=original_turn)["waiting"])
        self.assertEqual(teams.team_status(self.db,rid)[0]["active_roots"],0)
        roots._claim(b)
        jobs = JobService(self.db)
        with store.transaction(self.db):
            jobs._end(self.db,jobs._row("external"),"SUCCEEDED",reason="external outcome",
                      extra_updates={"finished_at":clock.now_iso()})
        with self.assertRaises(HoldActive) as denied:
            roots.wake_job(a,"external")
        self.assertEqual(denied.exception.details["scope"],"workspace")
        self.assertEqual(len(peer.sent),1)
        self.assertEqual(teams.team_status(self.db,rid)[0]["active_roots"],1)
        with closing(store.open_store(self.home / "gx.db")) as cold:
            roots = self.roots(rid,cold)
            roots._request = peer._request
            roots._goal_for_budget = lambda *args:None
            roots.loaded.add("root")
            with self.assertRaises(HoldActive):
                roots.wake_job(a,"external")
            self.yield_task(b)
            wake = roots.wake_job(a,"external")
            self.assertEqual((wake["turn_id"],peer.sent[-1][0]),(original_turn,"turn/steer"))
            self.assertEqual(roots.wake_job(a,"external")["turn_id"],original_turn)
            self.assertEqual(len(peer.sent),2)
            self.assertEqual(teams.team_status(cold,rid)[0]["active_roots"],1)
            self.assertEqual(roots._binding("root")["lease_id"],binding["lease_id"])
            self.assertEqual(cold.execute("SELECT count(*) FROM jobs").fetchone()[0],1)
        self.observed = {"checks":["F05","C06"],"run_id":rid,"owner":binding["root_key"],"lease":binding["lease_id"],
            "job":"external","job_source":"injected detached Job terminal event","repeated_wake_id":wake["turn_id"],
            "native_methods":[method for method,_ in peer.sent],"denied_before_native":denied.exception.details}

    def test_exhausted_team_cannot_charge_a_sibling_or_pause_the_run(self):
        rid, run_budget = self.run_case("a",[group("empty",["evidence_reviewer"],tokens=0),group("funded",["planner"])],
                                        parallel=2,maximum=2)
        roots = self.roots(rid)
        a = self.create(rid)["task_id"]
        b = self.create(rid,role="planner")["task_id"]
        empty = self.occupy(roots,a,"empty-thread")
        funded = self.occupy(roots,b,"root")
        peer = Peer(self.db,rid)
        def request(method, params):
            value = peer._request(method,params)
            if method == "thread/read":
                value["thread"]["id"] = params["threadId"]
            return value
        roots._request = request
        roots._goal_for_budget = lambda *args:None
        roots.loaded.update(("empty-thread","root"))
        with self.assertRaises(HoldActive) as denied:
            roots.dispatch(a)
        self.assertEqual(denied.exception.details["team_id"],"empty")
        self.assertEqual(len(peer.sent),0)
        receipt = roots.dispatch(b)
        row = self.db.execute("SELECT budget_ref FROM p0_root_deliveries JOIN budget_reservations USING(reservation_ref) "
                              "WHERE turn_id=?", (receipt["turn_id"],)).fetchone()
        charged = teams.team_funding(self.db,rid,b,run_budget)
        self.assertEqual(row["budget_ref"],charged)
        self.assertNotEqual(charged,teams.team_funding(self.db,rid,a,run_budget))
        self.assertEqual(self.db.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()[0],"ACTIVE")
        self.observed = {"checks":["F04","F06"],"run_id":rid,"run_budget":run_budget,"refused_root":empty["root_key"],
                         "refused_team":denied.exception.details["team_id"],"admitted_root":funded["root_key"],
                         "funded_budget":charged,"turn":receipt["turn_id"],"run_status":"ACTIVE","provider_sends":len(peer.sent)}

    def test_real_workflow_job_native_turn_and_terminal_rollup_to_original_team(self):
        rid, run_budget = self.run_case("a", [group("analysis",["evidence_reviewer"])])
        roots = self.roots(rid)
        team_ref = teams.team_control(self.db,rid,"analysis","status")["budget_ref"]
        assets = storage.artifact_service(self.db)
        grid = {"method_id":"fixture-grid","method_version":"1","synthetic":True,"candidates":[{"candidate_id":"x","x":2}]}
        ref = assets.publish_bytes(namespace="teams",rel_path="grid.json",kind="grid",data=json.dumps(grid).encode(),
                                   run_id=rid).artifact_ref
        tid = self.create(rid, inputs={"grid_artifact":ref})["task_id"]
        binding = self.occupy(roots, tid, "root")
        peer = Peer(self.db,rid)
        roots._request = peer._request
        roots.loaded.add("root")
        roots._goal_for_budget = lambda *args:None
        receipt = roots.dispatch(tid)
        turn = receipt["turn_id"]
        roots._observe(ReceivedMessage(1,{"method":"turn/started","params":{"threadId":"root","turn":{"id":turn}}}))
        envelope = {"method":"item/tool/call","params":{"threadId":"root","callId":"analysis","turnId":turn,
            "tool":"gx_job_submit","arguments":{"capability_version":shipped.GRID,"inputs":{"grid":ref},
            "parameters":{"operation":"team-check"},"idempotency_key":"team-check"}}}
        reply = roots._callback(ReceivedMessage(2,envelope))
        self.assertTrue(reply["success"],reply)
        identifier = json.loads(reply["contentItems"][0]["text"])["job_id"]
        self.assertEqual(reply, roots._callback(ReceivedMessage(3,envelope)))
        teams.team_control(self.db,rid,"analysis","pause")
        for _ in range(2):
            compute.drain(self.db,rid)
        self.assertIsNone(self.db.execute("SELECT 1 FROM jobs WHERE job_id=?", (identifier,)).fetchone())
        self.assertEqual(self.db.execute("SELECT state FROM p0_job_proposals WHERE job_id=?",
                                         (identifier,)).fetchone()[0],"PROPOSED")
        teams.team_control(self.db,rid,"analysis","resume")
        compute.drain(self.db,rid)
        command = self.db.execute("SELECT outbox_id,consumed_at FROM outbox WHERE topic=? AND run_id=?",
                                  (compute.SUBMIT,rid)).fetchone()
        self.assertTrue(command["consumed_at"])
        # Replay the same durable outbox item as a coordinator after lost confirmation would.
        self.db.execute("UPDATE outbox SET consumed_at=NULL WHERE outbox_id=?", (command["outbox_id"],))
        compute.drain(self.db,rid)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0],1)
        jobs = JobService(self.db)
        result = jobs.wait(identifier, timeout_seconds=20)
        self.assertEqual(result["status"],"SUCCEEDED",result)
        row = self.db.execute("SELECT j.task_id,j.lease_ref,b.budget_ref,b.status FROM jobs j "
                              "JOIN budget_reservations b USING(reservation_ref) WHERE j.job_id=?", (identifier,)).fetchone()
        self.assertEqual(tuple(row),(tid,binding["lease_id"],team_ref,"SETTLED"))
        def emit(method, params):
            roots.delivery.observe(method,params)
        emit("rawResponse/completed", {"threadId":"root", "turnId":turn, "responseId":"receipt-17",
                                       "usage":{"totalTokens":17}})
        emit("thread/tokenUsage/updated",{"threadId":"root","turnId":turn,"tokenUsage":{"total":{"totalTokens":17}}})
        emit("turn/completed",{"threadId":"root","turn":{"id":turn,"status":"completed","items":[]}})
        charge = self.db.execute("SELECT b.budget_ref,b.status FROM p0_root_deliveries d "
                                 "JOIN budget_reservations b USING(reservation_ref) WHERE d.turn_id=?", (turn,)).fetchone()
        self.assertEqual(tuple(charge),(team_ref,"SETTLED"))
        spent = budgets.state(self.db,team_ref)["spent"]
        self.assertEqual((spent["tokens"],spent["api_calls"],spent["job_submissions"]),(17,1,1))
        self.db.execute("UPDATE runs SET status='COMPLETED' WHERE run_id=?", (rid,))
        workspace.release_finished(self.db)
        workspace.release_finished(self.db)
        self.assertEqual(budgets.state(self.db,run_budget)["spent"], spent)
        project_ref = self.db.execute("SELECT budget_ref FROM p0_workspace").fetchone()[0]
        self.assertEqual(budgets.state(self.db,project_ref)["spent"], spent)
        self.observed = {"checks":["F04","F06","F07","C06"],"workspace_budget":project_ref,"run_budget":run_budget,
            "team_budget":team_ref,"run_id":rid,"task":tid,"lease":binding["lease_id"],"root":binding["root_key"],
            "job_id":identifier,"job_status":result["status"],"output_refs":result["artifact_refs"],
            "native_turn":turn,"native_source":"pinned-wire Peer fixture with injected 17-token receipt",
            "job_source":"real detached fixture-grid@1 process via gx_job_submit",
            "proposal_during_team_pause":"PROPOSED","resumed_job_rows":1,"replayed_outbox_id":command["outbox_id"],
            "settled_team_and_parent_spend":spent}
