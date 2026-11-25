"""Read only GX records, source bytes and native calls for the preregistered study."""
from itertools import combinations
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "acceptance/v6"))
import live
import native_witness


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_file(folder, artifact):
    path = folder / "artifacts" / artifact["sha256"]
    if digest(path) != artifact["sha256"] or path.stat().st_size != artifact["bytes"]:
        raise ValueError(f"Artifact {artifact['artifact_ref']} does not reproduce")
    return path


def geometry_oracle(path, outputs):
    sys.path.insert(0, str(ROOT / "acceptance/v6/reviewer"))
    import independent_geometry as oracle
    atoms, union, masks = oracle.selections(oracle.MMCIF2Dict(str(path)))
    verified = []
    for row in outputs:
        assert row["input_sha256"] == digest(path)
        keys = masks[row["mask"]]
        positions = len({k[0] for k in keys})
        assert (row["selected_residues"], row["selected_atoms"], row["observed_union_residues"]) == (
            positions, len(keys), len(union))
        assert abs(row["coverage_fraction"] - positions / len(union)) < 1e-12
        assert len(row["pairs"]) == 3
        errors = []
        for pair, (a, b) in zip(row["pairs"], combinations("ABC", 2), strict=True):
            assert (pair["fixed_chain"], pair["moving_chain"]) == (a, b)
            if len(keys) < 3:
                assert pair.get("unavailable")
                errors.append({"pair":[a,b], "undefined":True})
                continue
            xyz = [oracle.np.array([atoms[c][k][0] for k in keys]) for c in (a, b)]
            rmsd, angle = oracle.fit(*xyz)
            d_rmsd = abs(rmsd - pair["fitted_rmsd_angstrom"])
            d_angle = abs(angle - pair["rotation_angle_deg"])
            assert d_rmsd <= .001 and d_angle <= .01, (row["entry"], row["mask"], a, b)
            errors.append({"pair":[a,b], "reference_rmsd":rmsd, "reference_angle":angle,
                           "rmsd_difference":d_rmsd, "angle_difference":d_angle})
        verified.append({"structure_sha256":digest(path), "entry":row["entry"],
                         "mask":row["mask"], "selected_positions":positions, "fits":errors})
    return verified


def identity(rows):
    return {"jobs":sorted(j["job_id"] for j in rows["jobs"]),
            "turns":sorted((t["thread_id"],t["turn_id"]) for t in rows["p0_meter_native_turns"]),
            "claims":sorted((h["hypothesis_id"],h["revision"]) for h in rows["hypothesis_revisions"]),
            "decisions":sorted(d["decision_ref"] for d in rows["v6_decisions"]),
            "graph_versions":sorted((g["version"],g["sha256"]) for g in rows["v6_graph_versions"])}


def score(private, actor_class):
    folder = private / "reopened"
    snap = json.loads((folder / "capture.json").read_bytes())
    rows = snap["rows"]
    before_cold = json.loads((private / "settled/capture.json").read_bytes())
    assert identity(rows) == identity(before_cold["rows"]) and before_cold["costs"] == snap["costs"]
    assets = {a["artifact_ref"]:a for a in rows["artifacts"]}
    jobs = {j["job_id"]:j for j in rows["jobs"]}
    nodes = {n["node_id"]:n for n in rows["v6_nodes"]}
    graphs = {g["version"]:g for g in rows["v6_graph_versions"]}
    for g in graphs.values():
        assert native_witness.digest(json.loads(g["graph_json"])) == g["sha256"]
    tasks = {t["task_id"]:t for t in rows["v6_decision_tasks"]}
    contexts = {c["context_ref"]:c for c in rows["research_contexts"]}
    settlement = {e["subject_id"]:e for e in rows["events"] if e["kind"] == "v6.node.settled"}
    decisions = sorted(rows["v6_decisions"], key=lambda d:json.loads(d["receipt_json"])["event_id"])
    intervals, observed, output_values = [], {}, []
    for n in nodes.values():
        if n["status"] != "SUCCEEDED":
            continue
        a = assets[n["result_ref"]]
        job = jobs[n["job_id"]]
        assert a["job_id"] == job["job_id"] and job["status"] == "SUCCEEDED"
        raw_result = checked_file(folder, a)
        value = json.loads(raw_result.read_bytes())
        assert value["schema"] == "gx.deposited_chain_geometry/1"
        spec = json.loads(n["spec_json"])
        assert value["mask"] == spec["parameters"]["mask"]
        input_ref, = spec["inputs"]
        assert value["input_sha256"] == assets[input_ref]["sha256"]
        executed = json.loads((private / "state/jobs" / n["job_id"] / "outcome.json").read_bytes())
        assert executed["job_id"] == n["job_id"] and executed["exit_code"] == 0 and executed["tool_pid"]
        intervals.append({"job_id":n["job_id"],"branch":n["branch_id"],"start":executed["started_at"],
                          "finish":executed["finished_at"],"tool_pid":executed["tool_pid"]})
        output_values.append(value)
        observed[n["result_ref"]] = (value,n,a)
    physical = []
    for a,b in combinations(intervals,2):
        if a["branch"] != b["branch"] and max(a["start"],b["start"]) < min(a["finish"],b["finish"]):
            physical.append([a["job_id"],b["job_id"]])
    adjudications = []
    seen_by_scope = {}
    for d in decisions:
        accepted = json.loads(d["receipt_json"])
        req = json.loads(d["request_json"])["decision"]
        task = tasks[d["task_id"]]
        seen_in_previous_native_briefs = seen_by_scope.setdefault(task["scope"], set())
        ctx = contexts[d["context_ref"]]
        frozen = json.loads(ctx["snapshot_json"])
        brief_outputs = {o["artifact_ref"] for o in frozen["observations"]}
        path = live.native_rollout(private, d["thread_id"])
        if not path:
            raise ValueError("Decision has no original official host rollout")
        native = native_witness.rollout(path, d, rows)
        assert native["intent"] == req
        assert native["session"]["id"] == d["thread_id"]
        if actor_class == "LIVE_MODEL":
            assert native["session"]["model_provider"] == "openai"
            assert (native["context"]["model"],native["context"]["effort"]) == (live.MODEL,"xhigh")
        previous = accepted["base"]
        current = accepted["result_head"]
        receipts = []
        conditions = set()
        added, stopped = [], []
        for ref in set(req["evidence_refs"]).intersection(observed):
            value,n,a = observed[ref]
            assert ref in json.loads(task["visible_refs_json"])
            seen_value = native_witness.seen(native,ref,value)
            event = settlement[n["node_id"]]["event_id"]
            assert event <= ctx["event_cursor"] < accepted["event_id"]
            fresh = seen_value and ref in brief_outputs and ref not in seen_in_previous_native_briefs
            receipts.append({"ref":ref,"sha256":a["sha256"],"origin_job":a["job_id"],"settled_event":event,
                             "new_to_lead_scope":fresh,"native_saw_value":seen_value,"value":value})
            if fresh and (value["selected_residues"] < 60 or value["coverage_fraction"] < .6):
                conditions.add("insufficient_coverage")
            if fresh and any("unavailable" in p for p in value["pairs"]):
                conditions.add("undefined_fit")
        graph_before = None
        if previous and any(r["new_to_lead_scope"] for r in receipts):
            graph_before = json.loads(graphs[previous["version"]]["graph_json"])
            masks = {}
            for node in graph_before["nodes"].values():
                if node.get("state") == "ACTIVE" and node.get("capability") == "deposited-chain-geometry@1":
                    masks.setdefault(node["inputs"][0],set()).add(node["parameters"]["mask"])
            if len(masks) < 2 or any(len(m) < 2 for m in masks.values()):
                conditions.add("uncommissioned_comparison")
        if req["mode"] == "amend" and previous:
            entry = graphs[current["version"]]
            assert entry["decision_ref"] == d["decision_ref"] and entry["sha256"] == current["sha256"]
            assert entry["parent_sha"] == graphs[previous["version"]]["sha256"] == previous["sha256"]
            assert current["version"] == previous["version"] + 1
            for resolved in accepted["resolved_ops"]:
                op = resolved["spec"]
                if op["op"] == "add_node":
                    target = nodes[op["node"]]
                    assert target["decision_ref"] == d["decision_ref"]
                    if target["result_ref"] in observed and target["status"] == "SUCCEEDED":
                        if graph_before is None:
                            continue
                        # An already planned measurement (even under a new alias) supplies no new contrast.
                        new = json.loads(target["spec_json"])
                        new_measurement = (tuple(new["inputs"]), new["parameters"]["mask"])
                        planned = {(tuple(old["inputs"]), old["parameters"]["mask"]) for old in graph_before["nodes"].values()
                                   if old["capability"] == new["capability"] and old["state"] == "ACTIVE"}
                        if new_measurement in planned:
                            continue
                        assert settlement[target["node_id"]]["event_id"] > accepted["event_id"]
                        added.append({"node":target["node_id"],"job":target["job_id"],"result":target["result_ref"]})
                if op["op"] in ("cancel_node","stop_branch"):
                    scope = op.get("branch") if op["op"] == "stop_branch" else None
                    for node in nodes.values():
                        if (node["branch_id"] == scope or node["node_id"] == op.get("node")) and node["status"] == "CANCELLED":
                            past = settlement.get(node["node_id"])
                            if past and past["event_id"] > accepted["event_id"]:
                                stopped.append({"node":node["node_id"],"job":node["job_id"],"event":past["event_id"]})
        adjudications.append({"decision":d["decision_ref"],"thread":d["thread_id"],"turn":d["turn_id"],
            "call":d["call_id"],"rollout_sha256":digest(path),"native_call_line":native["call_line"],
            "context":d["context_ref"],"brief_sha256":hashlib.sha256(native["brief"].encode()).hexdigest(),
            "event":accepted["event_id"],"mode":req["mode"],"rationale":req["rationale"],"cited":receipts,
            "base":previous,"head":current,"ops":accepted["resolved_ops"],"new_jobs":added,"stopped":stopped,
            "prospective_conditions":sorted(conditions),"verified_graph_effect":bool(conditions and (added or stopped))})
        seen_in_previous_native_briefs.update(brief_outputs)
    numerical = []
    for m in json.loads((HERE/"study-config.json").read_bytes())["input_files"]:
        matching = [v for v in output_values if v["input_sha256"] == m["sha256"]]
        numerical.extend(geometry_oracle((HERE/m["path"]).resolve(),matching))
    assert len(numerical) == len(output_values)
    if adjudications:
        assert adjudications[0]["base"] is None
        first = sum(o["spec"]["op"] == "add_node" for o in adjudications[0]["ops"])
        assert first <= 5
    else:
        first = 0
    assert len(jobs) <= 10
    return {"source_class":actor_class,"run_id":snap["run_id"],"fresh_native_threads":len({d["thread_id"] for d in decisions}),
            "first_revision_nodes":first,"rule_condition_seen":any(d["prospective_conditions"] for d in adjudications),
            "observed_evidence_to_graph_effect":any(d["verified_graph_effect"] for d in adjudications),
            "interpretation":"This check links source bytes, delivered observation, accepted native call, graph revision and actual changed work. Scientific interpretation of these decisions is reviewed separately.",
            "decisions":adjudications,"closed":any(e["kind"]=="v6.run.closed" for e in rows["events"]),
            "jobs":list(jobs),"cold_identity":identity(rows),"costs":snap["costs"],
            "branches":sorted(b["branch_id"] for b in rows["v6_branches"]),
            "actual_delegations":[{"event":e["event_id"],"task":e["subject_id"],"origin":e["actor"]}
                                  for e in rows["events"] if e["kind"]=="v6.branch.delegated"],
            "worker_process_intervals":intervals,
            "cross_branch_physical_overlap":physical,"independent_numeric_checks":numerical}
