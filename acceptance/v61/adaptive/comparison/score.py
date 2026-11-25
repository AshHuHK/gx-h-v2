"""Independent coordinate reproduction and an evidence-grounded, equal-ceiling comparison."""
from contextlib import closing
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from common import ENTRIES, MASKS, METHOD, PAIRS, ROOT, grant, pair_results
sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT / "acceptance/v6/reviewer")]
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np
from independent_geometry import fit, selections


def reference(panel):
    expected = {}
    for item in panel:
        entry = item["role"].removeprefix("structure_")
        atoms, union, masks = selections(MMCIF2Dict(item["path"]))
        for mask, keys in masks.items():
            positions = len({key[0] for key in keys})
            pairs = []
            for a,b in itertools.combinations(("A","B","C"),2):
                pair = {"fixed_chain":a,"moving_chain":b}
                if len(keys) >= 3:
                    x, y = (np.array([atoms[c][key][0] for key in keys]) for c in (a,b))
                    rmsd, angle = fit(x,y)
                    pair.update(fitted_rmsd_angstrom=rmsd, rotation_angle_deg=angle)
                pairs.append(pair)
            expected[entry,mask] = {"entry":entry,"mask":mask,"input_sha256":item["sha256"],
                      "selected_residues":positions,"selected_atoms":len(keys),"observed_union_residues":len(union),
                      "coverage_fraction":positions/len(union),"pairs":pairs,
                      "matched_keys_sha256":hashlib.sha256(json.dumps(keys,separators=(",",":")).encode()).hexdigest()}
    return expected


def checked(out, ref):
    differences = []
    stat_keys = ("input_sha256","matched_keys_sha256","selected_residues","selected_atoms","observed_union_residues")
    if (any(out.get(k) != ref[k] for k in stat_keys)
            or not np.isfinite(out["coverage_fraction"])
            or abs(out["coverage_fraction"]-ref["coverage_fraction"]) > 1e-12):
        differences.append("source or coverage")
    actual_pairs = {(p["fixed_chain"],p["moving_chain"]):p for p in out["pairs"]}
    if len(out["pairs"]) != 3 or set(actual_pairs) != {tuple(pair.split("/")) for pair in PAIRS}:
        differences.append("chain pairs")
    for p in ref["pairs"]:
        actual = actual_pairs.get((p["fixed_chain"],p["moving_chain"]),{})
        for k, tol in (("rotation_angle_deg",.01),("fitted_rmsd_angstrom",.001)):
            if k in p and (k not in actual or not np.isfinite(actual[k]) or abs(actual[k]-p[k]) > tol):
                differences.append(f"{p['fixed_chain']}/{p['moving_chain']} {k}")
            elif k not in p and k in actual:
                differences.append("unexpected fit")
    return differences


def read_bytes(state, row):
    data = (state / "artifacts" / row["namespace"] / row["rel_path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Artifact differs from GX original: " + row["artifact_ref"])
    return json.loads(data)


def snapshot(state):
    from gx_core.p0.v6_costs import resource_report
    with closing(sqlite3.connect((state/"gx.db").resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        run, = db.execute("SELECT run_id,status,workflow_id,workflow_version,objective,inputs_json FROM runs")
        rid = run["run_id"]
        recorded = json.loads(run["inputs_json"])
        def rows(sql):
            return [dict(r) for r in db.execute(sql, (rid,))]
        measurements = {}
        artifacts = rows("""SELECT a.*,j.status AS job_status FROM artifacts a JOIN jobs j USING(job_id)
                     WHERE a.run_id=? AND j.capability_id='deposited-chain-geometry' AND a.origin='JOB_OUTPUT' AND a.kind='deposited_chain_geometry'""")
        for row in artifacts:
            value = read_bytes(state,row)
            if value.get("method") != METHOD or row["job_status"] != "SUCCEEDED":
                raise ValueError("Result differs from its method or Job identity")
            manifest = db.execute("SELECT completion_json,completion_sha256 FROM p0_execution_manifests WHERE job_id=?",
                                  (row["job_id"],)).fetchone()
            if (not manifest or hashlib.sha256(manifest[0].encode()).hexdigest()!=manifest[1] or
                not any(item["artifact_ref"] == row["artifact_ref"] and item["sha256"] == row["sha256"]
                        for item in json.loads(manifest[0])["outputs"])):
                raise ValueError("Missing immutable terminal output binding")
            measurements[row["artifact_ref"]] = {"job_id":row["job_id"],"sha256":row["sha256"],"value":value}
        decisions = rows("SELECT decision_ref,context_ref,thread_id,request_json,receipt_json FROM v6_decisions WHERE run_id=? ORDER BY rowid")
        nodes = rows("SELECT node_id,branch_id,job_id,status,result_ref,decision_ref FROM v6_nodes WHERE run_id=?")
        claims = []
        if run["workflow_id"]:
            for row in rows("SELECT * FROM artifacts WHERE run_id=? AND kind='comparison_conclusion'"):
                doc = read_bytes(state,row)
                claims.append({"id":row["artifact_ref"],"statement":json.dumps({"statements":doc["statements"]}),
                               "evidence_refs":doc["evidence_refs"]})
        else:
            from gx_core.p0.v6_claims import current
            claims = [{"id":c["claim_revision_id"],"statement":c["statement"],
                       "evidence_refs":c["evidence_refs"]} for c in current(db,rid)]
        # Human research interventions after bootstrap. cold resume is an operation, not scientific input.
        messages = rows("SELECT message_id,json_extract(action_json,'$.kind') AS kind FROM p0_operator_messages WHERE run_id=? ORDER BY rowid")
        initial = str(uuid.uuid5(uuid.NAMESPACE_URL, "gx-p0-bootstrap/" + rid))
        followup = [m["message_id"] for m in messages if m["message_id"] != initial]
        workspace_limits, = db.execute("SELECT parent.limits_json FROM runs r JOIN budgets rb ON r.budget_ref=rb.budget_ref "
                                        "JOIN budgets parent ON rb.parent_ref=parent.budget_ref WHERE r.run_id=?",(rid,)).fetchone()
        return {"workspace_limits":json.loads(workspace_limits),"run_id":rid,"status":run["status"],"objective":run["objective"],"workflow":run["workflow_id"],
                "materials":{item["role"]:item["sha256"] for item in recorded["files"]},
                "cost":resource_report(db,rid), "measurements":measurements, "claims":claims, "nodes":nodes,
                "decisions":decisions, "operator_messages":messages, "human_intervention_ids":followup,
                "scopes":rows("SELECT scope,task_id FROM v6_decision_tasks WHERE run_id=?"),
                "branches":rows("SELECT branch_id,status FROM v6_branches WHERE run_id=?")}


def rate(n,d):
    return {"numerator":n,"denominator":d,"value":n/d if d else None}


def score(snap, expected, mode, manual=None):
    measurements = snap["measurements"]
    accepted, independent = {}, {}
    for ref, row in measurements.items():
        val = row["value"]
        k = val["entry"],val["mask"]
        reasons = checked(val,expected[k]) if k in expected else ["unregistered material or selection"]
        independent[ref] = {"job_id":row["job_id"],"entry":k[0],"mask":k[1],"differences":reasons}
        if not reasons:
            accepted[ref] = k
    decisions = []
    for raw in snap["decisions"]:
        d = json.loads(raw["request_json"])["decision"]
        receipt = json.loads(raw["receipt_json"])
        nodes = [n for n in snap["nodes"] if n["decision_ref"] == raw["decision_ref"] and n["result_ref"] in measurements]
        # Unconditional close is never a new investigation. Paired study's stricter causal verifier is separate.
        refs = [r for r in d["evidence_refs"] if r in accepted]
        decisions.append({"decision_ref":raw["decision_ref"],"thread_id":raw["thread_id"],"cited_verified_outputs":refs,
                          "mode":d["mode"],"result_head":receipt["result_head"],"executed_new_jobs":[r["job_id"] for r in nodes]})
    post = [d for d in decisions if d["cited_verified_outputs"] and d["mode"]=="amend" and d["executed_new_jobs"]]
    units, pending, correct = [],[],set()
    for claim in snap["claims"]:
        try:
            stated = json.loads(claim["statement"])["statements"]
            if not isinstance(stated,list) or set(json.loads(claim["statement"])) != {"statements"}:
                raise ValueError("Uncoded additional claims")
        except (ValueError,TypeError,KeyError):
            stated = (manual or {}).get(claim["id"])
            if stated is None:
                pending.append(claim["id"])
                continue
        observed = {accepted[r] for r in claim["evidence_refs"] if r in accepted}
        for item in stated:
            if item.get("type") == "other":
                # A blinded reviewer preserves all additional scientific assertions, including
                # overclaims outside the enrolled coordinate comparison, in the denominator.
                required = set(item.get("evidence_refs", []))
                supported = bool(required and required <= set(claim["evidence_refs"]) & set(accepted)
                                 and item["supported"] and item["rationale"].strip())
                units.append({"claim_id":claim["id"],"statement":item["quote"],"supported":supported,
                              "reviewer_rationale":item["rationale"],"full_scope":False})
                continue
            e, pair = item["entry"],item["pair"]
            if e == "both":
                masks = item["assessed_masks_by_entry"]
                needed = {(entry,m) for entry, selected in masks.items() for m in selected}
                full_scope = set(masks) == set(ENTRIES) and all(set(masks[k]) == set(MASKS) for k in ENTRIES)
            else:
                masks = item["assessed_masks"]
                needed = {(e,m) for m in masks}
                full_scope = set(masks) == set(MASKS)
            label = None
            if e in (*ENTRIES,"both") and pair in PAIRS and needed and needed <= observed:
                report = pair_results([expected[k] for k in sorted(needed)])
                projection, = [q for q in report if (q["entry"],q["pair"])==(e,pair)]
                if projection["eligible_masks"] == sorted(item["eligible_masks"]):
                    label = projection["label"]
            supported = label == item["label"] and label is not None
            full = supported and full_scope
            if full:
                correct.add((e,pair))
            units.append({"claim_id":claim["id"],"entry":e,"pair":pair,"supported":supported,
                          "full_scope":full,"stated":item["label"],"reference":label})
    met = snap["cost"]; n = len(set(accepted.values()))
    tokens = met["tokens"] or {}
    confirmed = tokens.get("provider_attributed",0)
    exact = not any(tokens.get(k) for k in ("provisional_booked","unattributed_delta","unresolved","possibly_understated"))
    cpu = met["measured_cpu"]["observed"]
    return {"independent_checks":independent,"claim_units":units,"pending_blind_prose_adjudication":pending,
            "conclusion_quality":None if pending else rate(len(correct),(len(ENTRIES)+1)*len(PAIRS)),
            "unsupported_claims":None if pending else rate(sum(not u["supported"] for u in units),len(units)),
            "unique_trustworthy_measurements":n,"resource_usage":met,
            "provider_paid_tokens_per_evidence":rate(confirmed if mode=="live" else 0,n),
            "paid_token_ledger_complete":exact,"cpu_seconds_per_evidence":rate(cpu,n),
            "loopback_fixture_tokens":confirmed if mode=="rehearse" else None,
            "usd":met["provider_charge"]["amount"],
            "human_interventions":len(snap["human_intervention_ids"]),
            "interventions_per_enrolled_run":rate(len(snap["human_intervention_ids"]),1),
            "decision_effects":decisions, "cited_observation_followup_commits":len(post)}
