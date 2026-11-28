"""Synthetic native callbacks drive a CSV audit through GX's public tool handlers."""
import csv
import hashlib
import json

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

from gx_core import api, project_work
from gx_core.persistence import store
from gx_core.hostobs import host_policy, native_context
from gx_core.native_release import native_state


ROOT = "00000000-0000-4000-8000-000000000001"
PROJECT = "delivery-audit"


class ModuleCollaboration(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="gx-delivery-audit-"))
        self.state = self.stack.enter_context(native_state(Path(directory) / "owned", env={}))
        self.conn = store.open_store(self.state.db_path)
        self.stack.callback(self.conn.close)
        self.serial = 0
        self.authority = native_context.NativeAuthority(project_ref=PROJECT, root_subject="test-coordinator")
        self.stack.callback(self.authority.close)
        self.policy, _ = host_policy.native_codex_policy(self.state.directory)
        self.register_thread(ROOT, parent=None, effort=self.policy["native_coordinator_observation"]["default_effort"])
        csv_path = self.state.directory / "delivery.csv"
        csv_path.write_text("id,price\nA,1\nA,1\nB,\nC,2\n", encoding="utf-8")
        modules = {"keys": "Are row identifiers unique?", "prices": "Which rows lack a price?"}
        self.start_args = {
            "objective": "Report the identifier and price quality of this CSV delivery.",
            "project_ref": PROJECT, "budget_profile": "cpu_only",
            "workflow": {"id": "module_collaboration", "version": "1"},
            "inputs": {"modules": [{"key": key, "question": q} for key, q in modules.items()]},
            "input_files": [{"path": str(csv_path), "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest()}],
            "idempotency_key": project_work.project_objective_key(PROJECT),
        }
        self.started = self.ok("gx_run_start", self.start_args)
        self.run_id = self.started["run_id"]
        [source] = self.status()["workflow"]["input_files"]
        self.source = source["artifact_ref"]
        self.project_team = self.team("Review this delivery's module handoffs.")
        self.module_teams, self.leads, self.workers = {}, {}, {}
        for key, question in modules.items():
            team = self.team(question, parent=self.project_team)
            self.module_teams[key] = team
            lead = self.actor(ROOT, "PLANNING")
            self.leads[key] = lead
            subject = self.subject(lead)
            self.manage("member", {"team_id": team, "subject": subject, "action": "add"})
            self.manage("leader", {"team_id": team, "subject": subject, "lease_seconds": 600})
        for key, names in (("keys", ("rows", "unique")), ("prices", ("missing",))):
            for name in names:
                self.workers[name] = self.actor(self.leads[key], "EXECUTION")

    def register_thread(self, thread_id, *, parent, effort):
        metadata = {
            "id": thread_id, "sessionId": ROOT, "parentThreadId": parent,
            "cliVersion": self.authority.host_version, "forkedFromId": None,
            "model": self.policy["pinned_model"]["id"], "reasoningEffort": effort,
        }
        self.authority.register_thread(metadata, sequence=self.authority.sequence + 1, root=parent is None)
        self.authority.start_turn(thread_id, "test-turn:" + thread_id, sequence=self.authority.sequence + 1)
        return thread_id

    def actor(self, parent, task_class):
        child = f"10000000-0000-4000-8000-{self.authority.sequence:012d}"
        effort = self.policy["task_classes"][task_class]["default_effort"]
        return self.register_thread(child, parent=parent, effort=effort)

    def invoke(self, tool, arguments, *, thread=ROOT):
        call = self.authority.make_call({
            "threadId": thread, "turnId": self.authority.active_turns[thread],
            "callId": self.key(), "tool": tool, "arguments": arguments,
        }, sequence=self.authority.sequence + 1)
        try:
            return api.dispatch(tool, arguments, principal=call.principal, conn=self.conn,
                                transport=native_context.TRANSPORT, native_call=call)
        finally:
            self.authority.complete(call)

    def image(self):
        return tuple(self.conn.iterdump())

    def refused(self, result, code):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], code, result)
        self.assertEqual(result["error"]["side_effect_status"], "NONE", result)

    def key(self):
        self.serial += 1
        return f"delivery-test:{self.serial}"

    def ok(self, tool, arguments, *, thread=ROOT):
        result = self.invoke(tool, arguments, thread=thread)
        self.assertIs(result["ok"], True, result)
        return result["data"]

    def task(self, task_id):
        return self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()

    def manage(self, action, payload):
        status = self.ok("gx_run_status", {"run_id": self.run_id})
        return self.ok("gx_team_manage", {"run_id": self.run_id, "action": action, "payload": payload,
            "coordinator_lease_ref": self.started["coordinator_lease_ref"],
            "expected_revision": status["revision"], "idempotency_key": self.key()})

    def bind_arguments(self, claim, child):
        return {"lease_ref": claim["lease_ref"], "host": "codex", "host_session_ref": ROOT,
                "agent_ref": child, "evidence_level": "CLIENT_REPORTED", "idempotency_key": self.key()}

    def bind(self, claim, child):
        arguments = self.bind_arguments(claim, child)
        result = self.ok("gx_task_bind", arguments, thread=child)
        self.assertIs(result["bound"], True, result)
        self.assertEqual(result["policy_verdict"], "CONFORMS", result)
        self.assertEqual(result["observed"]["source"], "host_protocol")
        self.assertIs(result["observed"]["upstream_tier_independently_verified"], False)
        return arguments, result

    def submit_arguments(self, task_id, claim, ref):
        return {"task_id": task_id, "lease_ref": claim["lease_ref"], "artifact_refs": [ref],
                "idempotency_key": self.key()}

    def public_text(self, ref, *, thread=ROOT):
        chunks, offset, digest = [], 0, None
        while True:
            result = self.ok("gx_artifact_get", {"artifact_ref": ref, "offset": offset, "limit": 8192},
                             thread=thread)
            self.assertIs(result["metadata"]["verified"], True)
            digest = digest or result["sha256"]
            self.assertEqual(result["sha256"], digest)
            chunks.append(result["content"])
            if not result["truncated"]:
                break
            next_offset = result["metadata"]["next_offset"]
            self.assertGreater(next_offset, offset)
            offset = next_offset
        text = "".join(chunks)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
        return text

    def public_json(self, ref, *, thread=ROOT):
        return json.loads(self.public_text(ref, thread=thread))

    def resume(self):
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
        return self.ok("gx_run_resume", {"run_id": self.run_id, "interrupt_ref": row["interrupt_ref"],
            "submission_refs": [], "coordinator_lease_ref": self.started["coordinator_lease_ref"],
            "expected_revision": row["revision"]})

    def subject(self, thread):
        return self.authority.principal_for(thread).subject

    def team(self, objective, parent=None):
        payload = {"intent": {"objective": objective}}
        if parent:
            payload["parent_team_id"] = parent
        team = self.manage("create", payload)["team"]["team_id"]
        self.manage("transition", {"team_id": team, "status": "ACTIVE"})
        return team

    def status(self):
        return self.ok("gx_run_status", {"run_id": self.run_id})

    def resume_args(self):
        status = self.status()
        return {"run_id": self.run_id, "interrupt_ref": status["interrupt_ref"], "submission_refs": [],
            "expected_revision": status["revision"], "coordinator_lease_ref": self.started["coordinator_lease_ref"],
            "idempotency_key": self.key()}

    def reconnect(self):
        self.conn.close()
        self.conn = store.open_store(self.state.db_path)
        self.stack.callback(self.conn.close)

    def ready(self, stage, module, item=None):
        # Pick only an offered next_actions row. Inspect its frozen inputs to identify this fixture's actor.
        plan = self.ok("gx_next_actions", {"run_id": self.run_id})
        for row in plan["next_actions"]:
            task = self.task(row["task_id"])
            inputs = store.loads(task, "input_json")
            if (inputs["assignment"], inputs["module"]["key"], (inputs.get("item") or {}).get("key")) == (stage, module, item):
                return row, plan["as_of_event_id"]
        self.fail(f"No ready task for {stage}/{module}/{item}: {plan['next_actions']}")

    def begin(self, stage, module, item=None):
        row, cursor = self.ready(stage, module, item)
        task_id = row["task_id"]
        team, lead = self.module_teams[module], self.leads[module]
        actor = self.workers[item] if item else lead
        subject = self.subject(actor)
        if item:
            grant = self.manage("grant", {"team_id": team, "subject": self.subject(lead),
                "task_ids": [task_id], "scopes": ["members", "assign", "message"]})
            revision = grant["revision"]
            for action, fields in (("member", {"action": "add"}), ("assign", {"task_id": task_id})):
                result = self.ok("gx_team_manage", {"run_id": self.run_id, "action": action,
                    "payload": {"team_id": team, "subject": subject, **fields},
                    "expected_revision": revision, "expected_generation": grant["result"]["generation"],
                    "idempotency_key": self.key()}, thread=lead)
                revision = result["revision"]
        elif not self.task(task_id)["attempt"]:
            self.manage("assign", {"team_id": team, "task_id": task_id, "subject": subject})
        claim = self.ok("gx_task_claim", {
            "task_id": task_id, "worker_role": row["role"],
            "expected_attempt": row["expected_attempt"], "as_of_event_id": cursor,
        }, thread=actor)
        self.bind(claim, actor)
        return actor, claim

    def finish(self, work, kind, product, *, expected="ACCEPTED"):
        actor, claim = work
        task_id = claim["task"]["task_id"]
        artifact = self.ok("gx_artifact_put", {"run_id": self.run_id, "task_id": task_id, "kind": kind,
            "media_type": "application/json", "content": json.dumps(product), "idempotency_key": self.key()},
            thread=actor)["artifact_ref"]
        request = self.submit_arguments(task_id, claim, artifact)
        result = self.ok("gx_task_submit", request, thread=actor)
        self.assertEqual(result["task_status"], expected, result)
        return artifact

    def submit_plan(self, work, items):
        return self.finish(work, "module_plan", {"items": [{"key": key, "question": question, "role": "executor"}
            for key, question in items]})

    def submit_observation(self, work, name):
        scope = self.public_json(work[1]["task"]["input"]["scope_ref"], thread=work[0])
        [file] = scope["files"]
        self.assertEqual(file["artifact_ref"], self.source)
        source = self.ok("gx_artifact_get", {"artifact_ref": file["artifact_ref"]}, thread=work[0])
        rows = list(csv.DictReader(source["content"].splitlines()))
        findings = {"rows": f"{len(rows)} rows", "unique": f"{len({r['id'] for r in rows})} distinct identifiers",
                    "missing": f"{sum(not r['price'] for r in rows)} missing price",
                    "locate": "Missing price for " + ", ".join(r["id"] for r in rows if not r["price"])}
        return self.finish(work, "module_result", {"finding": findings[name]})

    def synthesis(self, work, results, *, expected="ACCEPTED", gaps=()):
        findings = [self.public_json(ref, thread=work[0])["finding"] for ref in results]
        return self.finish(work, "module_synthesis", {"summary": ", ".join(findings),
            "result_refs": results, "open_gaps": list(gaps), "conflicts": []}, expected=expected)

    def decide(self, handoff_ref, action, reason, **fields):
        return self.ok("gx_team_message", {"run_id": self.run_id, "action": "send",
            "coordinator_lease_ref": self.started["coordinator_lease_ref"], "expected_revision": self.status()["revision"],
            "idempotency_key": self.key(), "payload": {"sender_team_id": self.project_team,
                "recipient_team_id": self.project_team, "kind": "response", "correlation_key": self.key(),
                "artifact_refs": [handoff_ref], "body": {"action": action, "reason": reason, **fields}}})

    def test_module_leads_repair_one_branch_and_close_the_project_from_current_results(self):
        keys_plan = self.begin("module_plan", "keys")
        prices_plan = self.begin("module_plan", "prices")
        self.submit_plan(keys_plan, [("rows", "How many rows?"), ("unique", "How many distinct identifiers?")])
        self.resume()
        rows = self.begin("module_work", "keys", "rows")
        unique = self.begin("module_work", "keys", "unique")
        self.submit_plan(prices_plan, [("missing", "How many prices are absent?")])
        self.resume()
        missing = self.begin("module_work", "prices", "missing")
        for _, claim in (rows, unique, missing):
            self.assertEqual(self.task(claim["task"]["task_id"])["status"], "RUNNING")

        key_refs = [self.submit_observation(rows, "rows"), self.submit_observation(unique, "unique")]
        self.assertEqual(len(set(key_refs)), 2)
        missing_ref = self.submit_observation(missing, "missing")
        self.resume()
        key_lead = self.begin("module_synthesis", "keys")
        self.synthesis(key_lead, [key_refs[0], missing_ref], expected="REJECTED")
        price_summary = self.synthesis(self.begin("module_synthesis", "prices"), [missing_ref],
                                       gaps=["Identify the row for correction."])
        key_summary = self.synthesis(self.begin("module_synthesis", "keys"), key_refs)
        self.assertEqual(self.public_json(key_summary, thread=ROOT)["summary"], "4 rows, 3 distinct identifiers")
        handoff = self.resume()["interrupt"]["payload"]["handoff_ref"]
        self.assertEqual(self.status()["status"], "WAITING_CLIENT")
        branches = self.public_json(handoff, thread=ROOT)["modules"]
        self.assertEqual(branches["keys"]["synthesis"]["artifact_ref"], key_summary)
        self.assertEqual(branches["prices"]["synthesis"]["artifact_ref"], price_summary)
        self.assertCountEqual([r["artifact_ref"] for r in branches["keys"]["results"]], key_refs)
        self.assertEqual(branches["keys"]["synthesis"]["author"], self.subject(self.leads["keys"]))
        self.assertEqual(branches["prices"]["synthesis"]["author"], self.subject(self.leads["prices"]))
        before_decision = self.image()
        project = project_work.next_project_work(self.conn, PROJECT)
        self.assertEqual(project["status"], "READY")
        self.assertEqual(project["waiting_task_ids"], [])
        self.assertTrue(any("gx_team_message" in action and handoff in action for action in project["actions"]), project)
        self.assertEqual(self.image(), before_decision)
        self.decide(handoff, "return", "Identify the row with the absent price.", modules=["prices"])
        return_resume = self.resume_args()
        resumed = self.ok("gx_run_resume", return_resume)
        self.reconnect()
        before_replay = self.image()
        self.assertEqual(self.ok("gx_run_resume", return_resume)["interrupt_ref"], resumed["interrupt_ref"])
        self.assertEqual(self.image(), before_replay)

        prices_plan = self.begin("module_plan", "prices")
        self.assertIn("Identify the row", json.dumps(prices_plan[1]["task"]["input"]))
        self.submit_plan(prices_plan, [("locate", "Which identifier lacks its price?")])
        self.resume()
        self.workers["locate"] = self.workers["missing"]
        repaired_ref = self.submit_observation(self.begin("module_work", "prices", "locate"), "locate")
        self.resume()
        price_lead = self.begin("module_synthesis", "prices")
        self.assertCountEqual(price_lead[1]["task"]["input"]["result_refs"], [missing_ref, repaired_ref])
        repaired_summary = self.synthesis(price_lead, [missing_ref, repaired_ref])
        self.assertEqual(self.public_json(repaired_summary, thread=ROOT)["summary"], "1 missing price, Missing price for B")
        final_handoff = self.resume()["interrupt"]["payload"]["handoff_ref"]
        revised = self.public_json(final_handoff, thread=ROOT)["modules"]
        self.assertEqual(revised["keys"], branches["keys"])
        self.assertEqual(revised["prices"]["synthesis"]["artifact_ref"], repaired_summary)
        self.assertNotEqual(final_handoff, handoff)
        self.assertNotEqual(price_summary, repaired_summary)
        self.assertEqual(self.public_json(key_summary, thread=ROOT)["result_refs"], key_refs)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(input_json,'$.module.key')='keys'").fetchone()[0], 4)

        self.decide(handoff, "accept", "Stale acknowledgement of the earlier audit.")
        stale = self.invoke("gx_run_resume", self.resume_args())
        if stale["ok"]:
            self.assertEqual(stale["data"]["status"], "WAITING_CLIENT")
            self.assertEqual(stale["data"]["interrupt"]["payload"]["handoff_ref"], final_handoff)
        else:
            self.refused(stale, "DEPENDENCY_UNMET")
        self.decide(final_handoff, "accept", "The audit records identifier uniqueness and locates the missing price.")
        project = project_work.next_project_work(self.conn, PROJECT)
        self.assertEqual(project["status"], "READY")
        self.assertTrue(any(action.startswith(f"{self.run_id} gx_run_resume interrupt ") for action in project["actions"]), project)
        final_resume = self.resume_args()
        self.assertEqual(self.ok("gx_run_resume", final_resume)["status"], "COMPLETED")
        checkpoint = store.loads(store.latest_checkpoint(self.conn, self.run_id), "state_json")
        report = self.public_json(checkpoint["outputs"]["report"]["artifact_ref"], thread=ROOT)
        self.assertEqual(report["handoff_ref"], final_handoff)
        self.assertEqual(report["decision"]["author"], self.subject(ROOT))
        self.assertEqual(report["decision"]["action"], "accept")
        self.assertEqual(self.status()["open_gaps"], [])
        self.assertEqual(project_work.next_project_work(self.conn, PROJECT)["status"], "TERMINAL")
        # A terminal reopen also replays inputs, artifacts, submissions and charges exactly.
        self.reconnect()
        before_replay = self.image()
        self.assertEqual(self.ok("gx_run_start", self.start_args)["run_id"], self.run_id)
        self.assertEqual(self.ok("gx_run_resume", final_resume)["status"], "COMPLETED")
        self.assertEqual(self.image(), before_replay)
