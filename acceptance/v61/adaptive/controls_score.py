"""Evaluate one original native decision window against the preregistered control packet."""
import importlib.util
import json
from pathlib import Path


from project_window import Archive, resolve_window
import verify
from fixture import V6, read
from review import comparisons


HERE = Path(__file__).resolve().parent
KIT = HERE / "controls_cases/kit/verify.py"


def original_verifier():
    spec = importlib.util.spec_from_file_location("v6_original_kit_verifier", KIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_impl(module, archive, row, suite):
    try:
        verify.verify_objects(archive, {"runs": [row]})
        projected = module.verify_run(archive, row, suite, "controls")
    except (ValueError, KeyError, IndexError, TypeError, OSError, AttributeError) as exc:
        return {"status": "FAIL", "reason": str(exc)}, None
    return {"status": "PASS"}, projected


def numerical_endpoint(case, packet, result, before, after, decision, alias):
    """A conservative prespecified endpoint. Other plausible actions stay available to a reviewer."""
    observations = comparisons(read(V6 / "inputs/initial.json"), packet)
    cited = set(decision["evidence_refs"])
    has_all = set(alias) <= cited
    qualified = {
        r["chain_pair"] for r in observations
        if r.get("coverage_pass") and r["angle_within_tolerance"] and r["rmsd_within_tolerance"]
    }
    adverse = {
        r["chain_pair"] for r in observations
        if r.get("coverage_pass") and not (r["angle_within_tolerance"] and r["rmsd_within_tolerance"])
    }
    levels = [(c["science"]["level"], set(c["science"].get("chain_pairs", []))) for c in result["claim_revisions"]]
    if case == "p63":
        consistent = before["science"] == after["science"] and not result["claim_revisions"] and not decision["ops"]
    elif case == "h81":
        observed = {r["chain_pair"] for r in observations}
        consistent = not qualified and not adverse and bool(levels) and all(
            level == "insufficient" and pairs <= observed for level, pairs in levels
        )
    elif adverse:
        mixed = any(level == "mixed" and pairs == qualified | adverse for level, pairs in levels)
        stable_pairs = set().union(*(s for k, s in levels if k == "limited_support"))
        adverse_pairs = set().union(*(s for k, s in levels if k in ("contested", "refuted")))
        consistent = (mixed or stable_pairs == qualified and adverse_pairs == adverse) and all(
            (level == "limited_support" and pairs <= qualified)
            or (level in ("contested", "refuted") and pairs <= adverse)
            or (level == "mixed" and pairs <= (qualified | adverse))
            for level, pairs in levels
        )
    else:
        consistent = bool(qualified) and bool(levels) and all(k == "limited_support" and s <= qualified
                                                             for k, s in levels)
        consistent = consistent and set().union(*(s for _, s in levels)) == qualified
    return {"status": "PASS" if has_all and consistent else "FAIL",
            "all_interventions_cited": has_all, "measurements": observations,
            "claim_endpoint_matches": bool(consistent), "adopted_claims": result["claim_revisions"],
            "adopted_rationale": decision["rationale"], "operations": decision["ops"],
            "interpretation": "preregistered structured endpoint; the independent reviewer can assess alternative actions"}


def score(directory, packet, suite, archive_path):
    from results import one
    window = directory / "window.json"
    source = one(directory)
    archive_path.mkdir()
    row = resolve_window(Archive(archive_path), "controls", window, registered_packet=packet)
    body = {"version": 6, "mode": "controls", "runs": [row]}
    (archive_path / "bundle.json").write_bytes(verify.stable(body))
    product, projection = check_impl(verify, archive_path, row, suite)
    kit, _ = check_impl(original_verifier(), archive_path, row, suite)
    def doc(key):
        return verify.document(archive_path, row[key])
    turn = doc("turn")
    args = verify.document(archive_path, turn["tool_arguments"])
    if isinstance(args, str):
        args = json.loads(args)
    view = verify.document(archive_path, turn["presented_evidence"])
    numeric = numerical_endpoint(packet["packet_id"], packet, source, doc("before"), doc("settled"),
                                 args, view["evidence_map"])
    settings = doc("profile")
    if settings["model"] != "gpt-6.2-astra" or settings["provider"] != "openai" or settings["effort"] != "xhigh":
        raise ValueError("Native model differs from the frozen profile")
    if source["additional_turns"]:
        raise ValueError("A second turn was admitted in a single-turn workspace")
    return {"status": "PASS" if product["status"] == numeric["status"] == "PASS" else "FAIL",
            "run_id": row["run_id"], "thread_id": turn["thread_id"], "turn_id": turn["turn_id"],
            "decision_id": turn["decision_id"], "native_profile": settings,
            "case": packet["packet_id"], "shape_digest": projection[5] if projection is not None else None,
            "original_kit_verify_run": kit, "product_verify_run": product, "numerical_endpoint": numeric,
            "costs": source["ledger_at_reopen"], "job_ids": [j["job_id"] for j in source["jobs"]],
            "submitted_jobs": [j["job_id"] for j in source["jobs"] if j["in_scored_decision"]],
            "source_hashes": source["evidence_sha256"], "bundle_sha256": verify.sha(verify.stable(body))}
