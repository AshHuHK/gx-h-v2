"""Prospective evidence is qualified separately for each registered material and endpoint."""
from __future__ import annotations

from ..domain import ids


RULE = "gx.claim-proposition/2"


def same_endpoint(previous, prediction, method_pin):
    old = previous["prediction"]
    if previous.get("chronology_scope") and old.get("external_endpoint") != prediction.get("external_endpoint"):
        return False
    if any(old.get(k) != prediction.get(k) for k in ("target", "measurement_schema")):
        return False
    old_method = previous.get("method_pin")
    if not (old_method and method_pin):
        return old_method == method_pin and old["observable"] == prediction["observable"]
    # Scope restricts admissibility, but a new wording cannot erase an already
    # seen readout on the same specific material and measurement definition.
    def definition(pin):
        return {k: v for k, v in pin["measurement_spec"].items() if k != "applicability"}
    return definition(old_method) == definition(method_pin)


def exposed_materials(prior):
    return {pin["sha256"] for pin in prior["maturity"].get("material_coverage", ())
            if pin["qualification"] in ("prospective", "retrospective")}


def freeze(conn, registration, claim, prior, *, typed_chronology=False, verified_materials=False):
    from . import v62_maturity

    pred = registration["prediction"]
    specification = registration.get("method_pin") or {
        "measurement_schema": pred["measurement_schema"], "observable": pred["observable"], "target": pred["target"]}
    spec_sha = specification.get("measurement_spec_sha256") or ids.sha256_of_obj(specification)
    registration.update(policy=RULE, quantifier=pred.get("quantifier", "all"), measurement_spec_sha256=spec_sha)
    if typed_chronology:
        from . import v623_chronology
        if verified_materials:
            registration["material_identity"] = "endpoint_verified"
        spec_sha = ids.sha256_of_obj(v623_chronology.endpoint(pred, registration.get("method_pin")))
        registration.update(policy=v623_chronology.RULE, chronology_scope="same_store", measurement_spec_sha256=spec_sha)
    registration["proposition_sha256"] = ids.sha256_of_obj({
        "claim": registration["claim_sha256"], "limitations": claim["limitations"], "scope": claim["scope"],
        "measurement": spec_sha,
        "materials": sorted(p.get("material_key", p["sha256"]) for p in registration["data"]), "quantifier": registration["quantifier"],
        "comparison": pred["operator"], "threshold": pred["threshold"], "target": pred["target"],
        "observable": None if registration.get("method_pin") else pred["observable"]})
    # Narrowing the text of a previously tested claim cannot hide the original
    # reading. Preserve its material binding without tainting unmeasured siblings.
    if typed_chronology:
        bindings = {}
        timeline = v623_chronology.availability(conn, registration, registration["event_id"], bindings=bindings)
        registration["endpoint_chronology_at_registration"] = timeline
        registration["known_at_registration"] = {k: t["endpoint_first_available_event_id"] is not None
                                                 for k, t in timeline.items()}
        return timeline, bindings
    previously_tested = exposed_materials(prior) if registration["posthoc_revision_of"] else set()
    registration["known_at_registration"] = {}
    for pin in registration["data"]:
        sha = pin["sha256"]
        same_endpoint = {**registration, "data": [pin], "posthoc_revision_of": None}
        registration["known_at_registration"][sha] = sha in previously_tested or bool(
            v62_maturity._known_before(conn, same_endpoint, registration["event_id"]))


def input_materials(fact, observation, reading):
    if reading is not None:
        return reading["material_inputs"]
    acquisition = observation.get("acquisition") or {}
    # A single scalar attributed to multiple materials is a pooled observation.
    direct = acquisition.get("data_refs") or [{"artifact_ref": fact["artifact_ref"], "sha256": fact["sha256"]}]
    return direct if len(direct) == 1 else []


def with_coverage(reg, results, gaps, timeline=None):
    by_material = []
    for pin in reg["data"]:
        sha = pin.get("material_key", pin["sha256"])
        eligible = [r for r in results if sha in r.get("matched_material_keys", r["matched_data_sha256"])]
        excluded = [g for g in gaps if sha in g.get("matched_material_keys", g.get("matched_data_sha256", ()))]
        confirmed = [r["artifact_ref"] for r in eligible if r["supports"]]
        adverse = [r["artifact_ref"] for r in eligible if not r["supports"]]
        if eligible:
            status = "prospective"
        elif reg["known_at_registration"][sha] or any(g["kind"] == "already_available" for g in excluded):
            status = "retrospective"
        elif any(g["kind"] in ("unknown_first_production", "unverified_production_order") for g in excluded):
            status = "timing_unknown"
        else:
            status = "unmeasured"
        timing = timeline[sha] if timeline is not None else {}
        if timing.get("material_identity_uncertainty"):
            status = "timing_unknown"
        first = timing.get("endpoint_first_available_event_id")
        if first is not None:
            if first <= reg["event_id"]:
                status = "retrospective"
            elif status == "unmeasured":
                status = "measured_unqualified"
        by_material.append({**pin, **timing, "qualification": status,
                            "measurement_spec_sha256": reg["measurement_spec_sha256"],
                            "known_at_registration": reg["known_at_registration"][sha],
                            "registration_event_id": reg["event_id"],
                            "supporting_refs": confirmed, "opposing_refs": adverse,
                            "retrospective_refs": [g["artifact_ref"] for g in excluded if g["kind"] == "already_available"],
                            "uncertain_refs": [g["artifact_ref"] for g in excluded if g["kind"] in
                                               ("unknown_first_production", "unverified_production_order")]})
    return by_material


def direction(coverage, reviewed_support, reviewed_opposition):
    """A counterexample to all is decisive; a conflict for the same material stays explicit."""
    if any(r["opposing_refs"] and not r["supporting_refs"] for r in coverage):
        return "opposed"
    if any(r["opposing_refs"] for r in coverage):
        return "mixed"
    if reviewed_opposition:
        return "mixed" if reviewed_support or all(r["supporting_refs"] for r in coverage) else "opposed"
    return "supported" if all(r["supporting_refs"] for r in coverage) else "undetermined"


def lead_delivery(conn, run, draft):
    """Keep the native receipt for the adopting turn separate from GX availability chronology."""
    from . import v6_native

    root = conn.execute("""SELECT run_id,thread_id,brief FROM p0_roots
        WHERE run_id=? AND task_id=? AND lease_id=? AND thread_id=?""",
        (run, draft["task_id"], draft["lease_id"], draft["thread_id"])).fetchone()
    receipt = {"context_ref": draft["context_ref"], "snapshot_cursor": draft["event_cursor"],
               "thread_id": draft["thread_id"], "turn_id": draft["turn_id"], "state": "unverified"}
    delivered = root and v6_native.acknowledged_brief(conn, root, draft["turn_id"])
    if not delivered:
        return receipt
    brief, row = delivered
    metrics = [m["metric_ref"] for item in [*brief.get("observations", []), *brief.get("module_reports", [])]
               for m in item.get("verified_metrics", item.get("metrics", [])) if m.get("metric_ref")]
    return {**receipt, "state": "acknowledged", "delivery_receipt": row["event_id"],
            "brief_turn_id": row["turn_id"], "brief_sha256": row["text_sha256"], "metric_refs": sorted(set(metrics)),
            "observations": [{"artifact_ref": x["artifact_ref"], "sha256": x["sha256"], "event_id": x["event_id"],
                              "projection_sha256": ids.sha256_of_obj(x["observation"])}
                             for x in brief.get("observations", []) if "observation" in x and x.get("artifact_ref")]}
