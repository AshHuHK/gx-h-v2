"""A6. Verify real dispatch, source-limited spending and bounded report delivery.

The transport supplies scripted turn identities and token receipts. JobService, the
ledger, module owners, artifact verification and all decision callbacks are real.
"""
from hashlib import sha256
import json

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError
from gx_core.p0 import metering, v6_native
from gx_core.persistence import store

from tests.test_v6_decisions import branch, draft
from .interleavings_fixture import Scenario


def encoded(value):
    return ids.canonical_json(value).encode("utf-8")


def job_rows(s):
    return s.rows("SELECT job_id, capability_id, status, reservation_ref FROM jobs WHERE run_id=? ORDER BY rowid", s.run_id)


def module_reports(s, mid):
    return s.rows("SELECT * FROM v62_module_reports WHERE module_instance_id=?", mid)


def checked_report(s, mid):
    row, = module_reports(s, mid)
    ref = row["report_ref"]
    assets = ArtifactService(s.conn)
    body = assets.read_bytes(ref)
    digest = sha256(body).hexdigest()
    assert digest == assets.describe(ref)["sha256"] and assets.verify(ref)["ok"]
    return ref, assets.read_json(ref), digest


def exhaustion(path):
    with Scenario(path) as s:
        # Exactly one native turn may consume this child's model allocation.
        opened = s.open("first-measurement", limits={"worker_tasks": 3, "api_calls": 1, "tokens": 10,
                                                     "cpu_seconds": 120, "job_submissions": 1})
        actor = s.bind(opened["task_id"], metered=True)
        receipt, = s.rows("SELECT reservation_ref,token_budget,state FROM p0_root_deliveries "
                          "WHERE thread_id=? AND turn_id=?", actor.thread, actor.turn)
        assert receipt["state"] == "acknowledged" and receipt["token_budget"] == 10
        admitted = s.submit_node(actor, "bounded")
        node = admitted["aliases"]["local:bounded"]
        settled = s.settle(node)
        assert settled["status"] == "SUCCEEDED" and settled["job_id"]
        result = settled["result_ref"]
        assert ArtifactService(s.conn).verify(result)["ok"]
        assert not module_reports(s, opened["module_instance_id"]), "An admitted live turn may still submit"

        # The private transport reports the 10 synthetic tokens. This exercise
        # never contacts a model provider and never speculates about its billing.
        with store.transaction(s.conn, transaction_owner="V62Acceptance", operation="fixture_turn_settled", run_id=s.run_id):
            s.conn.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens) "
                           "VALUES(?,?,'completed',?)", (actor.thread, actor.turn, 10))
            metering.confirm(s.conn, receipt["reservation_ref"], 10)
        before = job_rows(s)
        s.advance()
        mid = opened["module_instance_id"]
        ref, bundle, digest = checked_report(s, mid)
        assert bundle["status"] == "PARTIAL" and bundle["author"]["kind"] == "GX" and bundle["gaps"]
        assert result in bundle["artifact_refs"]
        assert any(src["artifact_ref"] == result and src["sha256"] == ArtifactService(s.conn).describe(result)["sha256"]
                   for src in bundle["sources"])
        s.advance()
        assert checked_report(s, mid)[0] == ref and job_rows(s) == before and len(before) == 1
        metered = s.rows("SELECT d.reservation_ref, d.token_budget, r.status, t.confidence,t.settled_tokens "
                         "FROM p0_root_deliveries d JOIN budget_reservations r USING(reservation_ref) "
                         "JOIN p0_meter_turns t USING(reservation_ref) WHERE d.thread_id=?", actor.thread)
        assert len(metered) == 1 and metered[0]["settled_tokens"] == 10 and metered[0]["status"] == "SETTLED"
        starts = s.rows("SELECT event_id,thread_id,turn_id,token_budget,reservation_ref FROM p0_root_deliveries d "
                        "JOIN p0_roots r USING(thread_id) JOIN v6_decision_tasks t ON t.task_id=r.task_id "
                        "WHERE t.scope=? AND d.method='turn/start'", opened["scope"])
        assert len(starts) == 1
        budget, = s.rows("SELECT limits_json,spent_json,reserved_json FROM budgets b "
                         "JOIN v62_modules m USING(budget_ref) WHERE m.module_instance_id=?", mid)
        account = {k.removesuffix("_json"): json.loads(v) for k, v in budget.items()}
        available = account["limits"]["tokens"] - account["spent"]["tokens"] - account["reserved"].get("tokens", 0)
        assert available == 0 and account["spent"]["api_calls"] == account["spent"]["job_submissions"] == 1
        return {"module": mid, "scope": opened["scope"], "lease": actor.lease_id, "decision": admitted,
                "report_ref": ref, "report_sha256": digest, "author": bundle["author"], "gaps": bundle["gaps"],
                "verified_result": result, "jobs": before, "remaining_module_model_tokens": available, "child_account": account,
                "module_fixture_native_usage": metered, "module_native_starts": starts,
                "additional_calls_for_partial": len(starts) - 1,
                "trace": s.trace()}


def projection(path):
    limits = {"report_bytes": 2048, "brief_bytes": 4096}
    with Scenario(path, spec_updates={"module_policy": limits}) as s:
        opened = s.open("full-report")
        s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "a6:wait-for-complete-round")
        actor = s.bind(opened["task_id"])
        material = s.materials[0]
        s.call(actor, "gx_read_artifact", {"ref": material, "operation_key": "a6:read"}, "a6-read")
        # 4800 UTF-8 bytes + a short contrary result. Place the latter last to
        # catch a simple prefix truncation that silently drops adverse evidence.
        detail = "完整明細" * 400 + "【END OF FULL FINDING】"
        adverse = "反證：公開標記的來源只支持座標盤點。"
        report = {"status": "COMPLETE", "findings": [
            {"text": detail, "kind": "observation", "source_refs": [material]},
            {"text": adverse, "kind": "counterevidence", "source_refs": [material]}],
                  "gaps": [], "artifact_refs": [material], "next_steps": [],
                  "operation_key": "a6:full-report"}
        s.call(actor, "gx_submit_module_report", report, "a6-submit")
        mid = opened["module_instance_id"]
        full_ref, bundle, digest = checked_report(s, mid)
        assert any(f["text"] == detail for f in bundle["findings"])
        assert any(f["text"] == adverse for f in bundle["findings"])
        new_lead = s.next_actor("run")
        brief = s.brief(new_lead)
        projected, = [row for row in brief["module_reports"] if row["module_instance_id"] == mid]
        root = s.conn.execute("SELECT brief FROM p0_roots WHERE lease_id=?", (new_lead.lease_id,)).fetchone()
        body = root["brief"].encode("utf-8")
        delivered = v6_native.brief(root)
        assert full_ref in str(delivered)
        assert projected["report_ref"] == full_ref and projected["status"] == "COMPLETE"
        assert len(encoded(projected)) <= limits["report_bytes"] and projected["truncated"]
        assert len(body) <= limits["brief_bytes"] and "【END OF FULL FINDING】".encode() not in body
        assert adverse.encode() in body
        readback = s.call(new_lead, "gx_read_artifact", {"ref": full_ref}, "a6-read-published")
        assert readback == bundle
        return {"module": mid, "module_scope": opened["scope"], "lease": actor.lease_id, "lead_lease": new_lead.lease_id,
                "full_report_ref": full_ref, "full_sha256": digest, "full_bytes": len(encoded(bundle)),
                "configured_limits": limits, "projection_bytes": len(encoded(projected)), "whole_lead_brief_bytes": len(body),
                "counterevidence": adverse, "full_reference_reread": True, "trace": s.trace()}


def gated_work(path):
    with Scenario(path) as s:
        material = s.materials[0]
        branch_op = branch("assay", jobs=5)
        branch_op["budget_limits"]["cpu_seconds"] = 180
        producer = {"op": "add_node", "branch": "local:assay", "node": "local:inventory",
                    "capability": "tl1a-coordinate-quality@1", "inputs": [material],
                    "parameters": {}, "depends_on": [], "question": "Which coordinates are present?"}
        gate = {"producer_node": "local:inventory", "producer_version": 1, "source_ref": material,
                "pointer": "/mapping/common_backbone_residues", "equals": 164, "require_lead_review": True}
        heavy = {"op": "add_node", "branch": "local:assay", "node": "local:geometry",
                 "capability": "deposited-chain-geometry@1", "inputs": [material], "parameters": {"mask": "common_ca"},
                 "depends_on": ["local:inventory"], "question": "How similar are the coordinate sets?",
                 "evidence_gate": gate}
        wrong_value = {**heavy, "node": "local:mismatched-value", "parameters": {"mask": "ordered_backbone"},
                       "evidence_gate": {**gate, "equals": 165, "require_lead_review": False}}
        wrong_source = {**heavy, "node": "local:mismatched-source", "parameters": {"mask": "common_backbone"},
                        "evidence_gate": {**gate, "source_ref": s.materials[1], "require_lead_review": False}}
        wrong_version = {**heavy, "node": "local:mismatched-version",
                         "evidence_gate": {**gate, "producer_version": 2, "require_lead_review": False}}
        opening = draft(branch_op, producer, heavy, wrong_value, wrong_source, wrong_version)
        opening["reconsider_when"] = [{"event": "job_settled", "subject_ref": "local:inventory"}]
        accepted = s.call(s.lead, v6_native.DECIDE, {"decision": opening}, "a6:precommission")
        def alias(name):
            return accepted["aliases"]["local:" + name]
        assert not job_rows(s)
        inventory = s.settle(alias("inventory"))
        assert inventory["status"] == "SUCCEEDED"
        ref = inventory["result_ref"]
        measured = ArtifactService(s.conn).read_json(ref)
        assert measured["mapping"]["common_backbone_residues"] == gate["equals"]
        s.advance()
        waiting = s.rows("SELECT n.node_id,n.status,n.job_id,w.wait_json FROM v6_nodes n "
                         "LEFT JOIN v6_node_waits w USING(node_id) WHERE run_id=? AND n.node_id<>?",
                         s.run_id, alias("inventory"))
        assert len(waiting) == 4 and all(not row["job_id"] and row["wait_json"] for row in waiting)
        assert len(job_rows(s)) == 1
        reasons = {}
        for row in s.rows("SELECT subject_id,payload_json FROM events WHERE run_id=? "
                          "AND kind='v6.node.waiting' ORDER BY event_id", s.run_id):
            detail = json.loads(row["payload_json"])["details"]
            if detail.get("reason") == "evidence_pending":
                reasons.setdefault(row["subject_id"], []).append(detail["condition"])
        expected = {"geometry": "awaiting an evidence-citing Lead decision",
                    "mismatched-source": "unrelated source assignment",
                    "mismatched-value": "does not satisfy the adopted condition",
                    "mismatched-version": "producer revision differs"}
        for local_name, reason in expected.items():
            assert any(reason in why for why in reasons[alias(local_name)]), (local_name, reasons)
        lead = s.next_actor("run")
        frozen = s.brief(lead)
        assert ref in frozen["evidence_refs"]
        decision = draft()
        decision["approve_nodes"] = [alias("geometry")]
        decision["evidence_refs"] = [s.materials[1]]
        try:
            s.call(lead, v6_native.DECIDE, {"decision": decision}, "a6:unsupported-review")
        except GxError as err:
            rejected = {"code": err.code, "details": err.details}
        else:
            raise AssertionError("Approval without the required observation spent new compute")
        assert len(job_rows(s)) == 1
        decision["evidence_refs"] = [ref]
        approval = s.call(lead, v6_native.DECIDE, {"decision": decision}, "a6:observed-review")
        followup = s.settle(alias("geometry"))
        assert followup["status"] == "SUCCEEDED" and ArtifactService(s.conn).verify(followup["result_ref"])["ok"]
        s.advance()
        jobs = job_rows(s)
        assert len(jobs) == 2
        assert {r["capability_id"] for r in jobs} == {"tl1a-coordinate-quality", "deposited-chain-geometry"}
        pending = s.rows("SELECT node_id,status,job_id FROM v6_nodes WHERE node_id IN (?,?,?)",
                         alias("mismatched-value"), alias("mismatched-source"), alias("mismatched-version"))
        assert all(row["job_id"] is None and row["status"] == "PENDING" for row in pending)
        return {"opening": accepted, "gates_before_review": waiting, "gate_transitions": reasons, "producer_result_ref": ref,
                "source_ref": material, "measured_value": measured["mapping"]["common_backbone_residues"],
                "unsupported_review": rejected, "approval": approval, "lead_lease": lead.lease_id,
                "expensive_result": followup["result_ref"], "jobs": jobs, "held_after_review": pending,
                "trace": s.trace()}


def run(directory):
    return {"case": "A6", "status": "PASS", "paid_model_calls": 0, "provider_tokens": None,
            "transport": "scripted direct native callback with a synthetic 10-token settlement",
            "exhausted": exhaustion(directory / "exhausted"), "large_report": projection(directory / "large-report"),
            "conditional_dispatch": gated_work(directory / "gated")}
