"""Derive claim maturity from immutable source versions, event order and actual measurements."""
from __future__ import annotations

import itertools
import json
import math
import operator
import re
from decimal import Decimal

from .. import contracts, schema
from ..artifacts.service import ArtifactService
from ..domain import clock, ids
from ..errors import SchemaInvalid, ScopeDenied
from ..persistence import store
from .v62_outputs import PREDICTION_TARGET_NATURE


VERSION = "gx.research.v6.2b.1"
RULE = "gx.claim-maturity/1"
COMPARISONS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge, "eq": operator.eq}
NATURE = PREDICTION_TARGET_NATURE
DEPENDENCIES = ("data", "acquisition", "sample", "source", "publication", "processing", "sampling")


def enrolled(conn, run_id):
    row = conn.execute("SELECT json_extract(inputs_json,'$.inputs.research_evidence_version') "
                       "FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return bool(row and row[0] == VERSION)


def prediction_schema():
    return contracts.native_goal_tool_spec("gx_stage_claim")["inputSchema"]["properties"]["prediction"]


def _finite(number):
    return type(number) is int or (type(number) is float and math.isfinite(number))


def validate_prediction(prediction):
    problems = schema.validate(prediction, prediction_schema())
    if problems:
        raise SchemaInvalid("Invalid falsifiable prediction: " + ", ".join(problems))
    method = prediction.get("method")
    metric = prediction.get("measurement_schema")
    if method is None and metric is None:
        raise SchemaInvalid("Identify the registered method and named output or the original observable/units schema")
    if method and (not re.fullmatch(r"[^@\s]+@[^@\s]+", method["capability"]) or not method["output"].strip()):
        raise SchemaInvalid("The selected method needs its exact capability@version and output name")
    if metric is not None and not metric.strip():
        raise SchemaInvalid("The measurement schema must be an exact versioned identifier")
    pointer = prediction["observable"]
    if len(pointer) < 2 or not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise SchemaInvalid("The observable must be an exact JSON Pointer into the result")
    refs = prediction["data_refs"]
    if not refs or any(not ref.strip() for ref in refs) or len(set(refs)) != len(refs):
        raise SchemaInvalid("Preregistration needs distinct immutable data references")
    if not _finite(prediction["threshold"]):
        raise SchemaInvalid("The decision threshold must be a finite number")


def pin_data(conn, run, prediction, visible):
    refs = prediction["data_refs"]
    if not set(refs) <= visible:
        raise ScopeDenied("Prediction data must be original artifacts visible in this decision scope")
    pins = []
    for ref in refs:
        row = conn.execute("SELECT artifact_ref,sha256 FROM artifacts WHERE run_id=? AND artifact_ref=? "
                           "AND published=1", (run, ref)).fetchone()
        if row is None:
            raise SchemaInvalid("A prediction data reference must identify an immutable artifact version")
        pins.append(dict(row))
    if len({pin["sha256"] for pin in pins}) != len(pins):
        from . import v621_policy, v623_chronology
        if not v621_policy.live_science(conn, run):
            raise SchemaInvalid("Several aliases of one data version cannot register distinct acquisitions")
        v623_chronology.distinguish_samples(conn, run, pins)
    return pins


def pin_method(conn, run, prediction):
    method = prediction.get("method")
    if not method:
        return None
    granted = conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run,)).fetchone()
    name, version = method["capability"].rsplit("@", 1)
    capability = conn.execute("SELECT manifest_sha256 FROM capabilities WHERE capability_id=? AND version=?",
                              (name, version)).fetchone()
    if method["capability"] not in json.loads(granted[0]) or not capability:
        raise SchemaInvalid("The predicted calculation must name a capability version granted to this Goal")
    return {**method, "manifest_sha256": capability[0]}


def _measurement(conn, ref, pointer):
    """Select one finite scalar from the hash-verified original, never from the projected brief."""
    asset = ArtifactService(conn)
    try:
        value = asset.read_json(ref)
    except SchemaInvalid:  # Non-JSON data have no numeric JSON Pointer.
        return None, None
    return _scalar_at(value, pointer)


def _scalar_at(value, pointer):
    metric = value.get("schema") if isinstance(value, dict) else None
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and len(key) <= len(str(len(value))):
            index = int(key)
            if index >= len(value):
                return None, metric
            value = value[index]
        else:
            return None, metric
    return (value if _finite(value) else None), metric


def ground(conn, run, ref, scope, cursor):
    from . import v62_sources
    return v62_sources.claim_ground(conn, run, ref, scope, cursor)


def annotate(conn, run, fact, scope, cursor):
    from . import v62_outputs
    # Initial briefs can have no scientific events but explicitly include ingested run materials.
    # Read an authorized recent artifact as of its publication, never using later provenance.
    published = conn.execute("SELECT MIN(event_id) FROM events WHERE subject_id=? AND kind='artifact.published'",
                             (fact["artifact_ref"],)).fetchone()[0]
    fact["output_provenance"] = v62_outputs.classify(conn, run, fact["artifact_ref"], scope=scope,
                                                      through_event=max(cursor, published or 0))
    provenance = fact["output_provenance"]
    if fact["source_class"] == "execution_record":
        record_kind = "engineering_record"
    elif provenance.get("record_kind") == "test_fixture":
        record_kind = "test_fixture"
    elif provenance["nature"] == "unclassified":
        record_kind = "unclassified"
    else:
        record_kind = provenance["nature"]
    fact["record_nature"] = record_kind
    return fact


def qualified_ground(fact, claim, *, require_acquisition=False):
    if fact.get("source_class") == "reviewed_source_evidence":
        target = fact.get("target_claim_ref")
        same_proposition = target in claim.get("maturity_lineage", []) if target else fact.get("claim") == claim["statement"]
        return (same_proposition and fact.get("record_nature") == "formal_evidence" and fact.get("verdict") == "SUPPORTED"
                and fact.get("direction") in ("supports", "opposes")
                and fact.get("applicability") == claim["scope"])
    kind = fact.get("record_nature")
    if (require_acquisition and kind in ("observed", "analysis")
            and not fact.get("output_provenance", {}).get("original_inputs_grounded")):
        return False
    scopes = set(fact.get("output_provenance", {}).get("applicability_scopes", ()))
    if (scopes or require_acquisition and kind in ("observed", "analysis")) and scopes != {claim["scope"]}:
        return False
    target = claim.get("prediction", {}).get("target")
    if kind in ("observed", "analysis") and "model_prediction" in fact.get("output_provenance", {}).get("upstream_natures", []):
        return kind == "analysis" and target == "analysis"
    if kind in ("observed", "analysis"):
        return True
    return kind == "model_prediction" and target == "model_prediction"


def _identity(claim):
    return {key: claim.get(key) for key in ("statement", "scope", "criteria", "chain_pairs", "decision_scope")}


def _equivalent_rule(previous, prediction, data, method_pin):
    if prediction is None or prediction == previous["prediction"]:
        return True
    if not (previous.get("policy") and previous.get("method_pin") and method_pin):
        return False
    comparable = ("target", "measurement_schema", "operator", "threshold")
    if previous.get("chronology_scope") and {p["artifact_ref"] for p in data} != {p["artifact_ref"] for p in previous["data"]}:
        return False
    return (previous["method_pin"]["measurement_spec_sha256"] == method_pin["measurement_spec_sha256"]
            and previous.get("quantifier", "all") == prediction.get("quantifier", "all")
            and all(previous["prediction"].get(k) == prediction.get(k) for k in comparable)
            and {p["sha256"] for p in data} == {p["sha256"] for p in previous["data"]})


def _same_proposition(claim, prior, data, method_pin):
    if not prior or _identity(prior) != _identity(claim):
        return False
    if "publication" in prior:
        if prior["limitations"] != claim["limitations"]:
            return False
        prior_rule = prior["maturity"]["registration"]
        if prior_rule and not _equivalent_rule(prior_rule, claim.get("prediction"), data, method_pin):
            return False
    return True


def registration(claim, prior, data, method_pin, event_id):
    """Only adoption registers the rule. A change of proposition, scope, data or rule starts a new clock."""
    previous = (prior or {}).get("maturity", {}).get("registration")
    prediction = claim.get("prediction")
    if (previous and _same_proposition(claim, prior, data, method_pin)
            and _equivalent_rule(previous, prediction, data, method_pin)):
        return previous
    if prediction is None:
        return None
    parent_result = None
    if previous:
        dataset = {pin["sha256"] for pin in data}
        if previous.get("policy"):
            from . import v622_propositions
            posthoc = (v622_propositions.same_endpoint(previous, prediction, method_pin)
                       and dataset.intersection(v622_propositions.exposed_materials(prior)))
        else:
            same_observable = all(previous["prediction"].get(k) == prediction.get(k)
                                  for k in ("observable", "target", "measurement_schema", "method"))
            posthoc = same_observable and any(dataset.intersection(r["matched_data_sha256"])
                                              for r in prior["maturity"]["results"])
        if posthoc:
            parent_result = prior["claim_revision_id"]
    return {"event_id": event_id, "claim_revision_id": claim["claim_revision_id"], "posthoc_revision_of": parent_result,
            "decision_scope": claim["decision_scope"], "claim_scope": claim["scope"], "prediction": prediction,
            "claim_sha256": ids.sha256_of_obj(_identity(claim)), "data": data, "method_pin": method_pin}


def _in_scope(requested, observed):
    return requested == "run" or observed == "run" or requested.removeprefix("branch:") == observed.removeprefix("branch:")


def _metric_matches(reg, fact, metric):
    prediction = reg["prediction"]
    if prediction.get("measurement_schema") is not None and prediction["measurement_schema"] != metric:
        return False
    pin = reg.get("method_pin")
    if pin:
        capability = fact.get("capability", {})
        return (pin["capability"] == f"{capability.get('id')}@{capability.get('version')}"
                and pin["manifest_sha256"] == capability.get("sha256") and pin["output"] == fact.get("output_name"))
    return True


def _reading(conn, reg, fact):
    """Check identity before admitting a numeric observation or declaring historical exposure."""
    if reg.get("policy") and reg.get("method_pin"):
        from . import v622_measurement
        reason, reading = v622_measurement.match(conn, reg, fact)
        if reason or not reading:
            return reason or "unmeasured_observable", None, None, None
        return None, reading["value"], reading.get("metric"), reading
    if reg.get("policy") and fact.get("job_id"):
        return "method_not_registered", None, None, None
    if reg.get("chronology_scope"):
        from . import v623_chronology
        value, metric = v623_chronology.external_measurement(conn, fact["artifact_ref"], reg["prediction"])
    else:
        value, metric = _measurement(conn, fact["artifact_ref"], reg["prediction"]["observable"])
    reason = None if _metric_matches(reg, fact, metric) else "method_or_measurement_schema_mismatch"
    return reason, value, metric, None


def _applies(fact, claim_scope):
    declared = fact.get("applicability_scopes", [])
    return not declared or set(declared) == {claim_scope}


def _historical_facts(conn, run, ref, through):
    """Use the original branch's scientific scope for chronology, without releasing its evidence."""
    from . import v62_outputs
    scopes = conn.execute("""SELECT DISTINCT json_extract(payload_json,'$.scope') FROM events
        WHERE run_id=? AND kind='v6.observation.added' AND event_id<=?
          AND json_extract(payload_json,'$.artifact_ref')=? AND json_type(payload_json,'$.acquisition')='object'
        UNION SELECT json_extract(m.admission_json,'$.owner.branch_id') FROM artifacts a
              JOIN p0_execution_manifests m USING(job_id) WHERE a.artifact_ref=?""", (run, through, ref, ref))
    for scope in {row[0] or "run" for row in scopes} or {"run"}:
        yield v62_outputs.classify(conn, run, ref, scope=scope, through_event=through)


def _disjoint_sampling(prior, current):
    if not all(f["record_kind"] == "supplied_observation" and f["independence_complete"] for f in (prior, current)):
        return False
    left, right = ({p["sha256"] for p in f["data_refs"]} for f in (prior, current))
    if not left or not right or left & right:
        return False
    for axis in ("data", "acquisition", "sample", "source", "publication", "sampling"):
        both = set(prior["dependencies"][axis]) & set(current["dependencies"][axis])
        if axis == "data":
            both.discard("sha256:" + current["sha256"])  # Equal scalar bytes can come from distinct sites.
        if both:
            return False
    return True


def _first_relevant(conn, fact, reg):
    """Keep old unidentified copies. Exclude only samples independently identified before registration."""
    first = fact.get("first_event_id")
    boundary = reg["event_id"]
    if first is None or first > boundary:
        return first
    copies = conn.execute("""SELECT e.event_id,e.run_id,e.subject_id FROM events e
        WHERE kind='artifact.published' AND json_extract(payload_json,'$.sha256')=?
        AND event_id<=? ORDER BY event_id""", (fact["sha256"], boundary))
    for row in copies:
        if not row["run_id"] or row["subject_id"] == fact["artifact_ref"]:
            return row["event_id"]
        prior = _historical_facts(conn, row["run_id"], row["subject_id"], boundary)
        if any(not _disjoint_sampling(old, fact) for old in prior):
            return row["event_id"]
    return conn.execute("SELECT min(event_id) FROM events WHERE kind='artifact.published' AND subject_id=?",
                        (fact["artifact_ref"],)).fetchone()[0]


def _known_before(conn, reg, through):
    """Past output anywhere in this store disqualifies the same outcome on the same frozen inputs.

    This query supplies only a chronology judgment to the claim. Another scope's measurement
    bytes, source identities and numeric values are never projected into the brief.
    """
    if reg.get("posthoc_revision_of"):
        return True
    boundary = reg["event_id"]
    pins = [p["sha256"] for p in reg["data"]]
    # A material can itself already contain the value, even without a scientific receipt.
    for pin in reg["data"]:
        ref = pin["artifact_ref"]
        if not reg.get("method_pin") or not reg.get("policy"):
            value, _ = _measurement(conn, ref, reg["prediction"]["observable"])
            if value is None:
                continue
        run = conn.execute("SELECT run_id FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()[0]
        for fact in _historical_facts(conn, run, ref, through):
            if _applies(fact, reg["claim_scope"]):
                reason, value, _, _ = _reading(conn, reg, fact)
                if not reason and value is not None:
                    return True
    # Check actual output availability, including a completed Job whose node report is still pending.
    # Qualify data by their digest rather than movable names, run ids, or visibility of an old report.
    # New-policy lineage follows only the original inputs bound to this named output.
    # In particular, shared worker configuration is not a shared scientific material.
    job_edges = ("""
        SELECT json_extract(i.value,'$.sha256'), json_extract(o.value,'$.artifact_ref')
        FROM p0_execution_manifests m, json_each(m.completion_json,'$.outputs') o,
             json_each(o.value,'$.source_inputs') i
    """ if reg.get("policy") else """
        SELECT json_extract(d.value,'$.sha256'), a.artifact_ref
        FROM p0_execution_manifests m JOIN artifacts a USING(job_id), json_each(m.admission_json,'$.inputs') d
        WHERE a.origin='JOB_OUTPUT' AND a.published=1 AND a.kind NOT IN ('job_stdout','job_stderr')
    """)
    candidates = conn.execute(f"""
        WITH RECURSIVE edges(input_sha, result_ref) AS (
            SELECT json_extract(d.value,'$.sha256'), json_extract(e.payload_json,'$.artifact_ref')
            FROM events e, json_each(e.payload_json,'$.acquisition.data_refs') d
            WHERE e.kind='v6.observation.added' AND e.event_id<=?
            UNION ALL
            {job_edges}
            UNION ALL
            SELECT json_extract(d.value,'$.sha256'), json_extract(e.payload_json,'$.artifact_ref')
            FROM events e, json_each(e.payload_json,'$.acquisition.derived_from') d
            WHERE ? AND e.kind='v6.observation.added' AND e.event_id<=?
        ), ancestors(sha) AS (
            SELECT value FROM json_each(?)
            UNION SELECT i.input_sha FROM ancestors original JOIN artifacts a ON a.sha256=original.sha
                  JOIN edges i ON i.result_ref=a.artifact_ref
                  JOIN events e ON e.kind='artifact.published' AND e.subject_id=a.artifact_ref AND e.event_id<=?
        ), reachable(sha) AS (
            SELECT sha FROM ancestors
            UNION SELECT a.sha256 FROM reachable r JOIN edges i ON i.input_sha=r.sha
                  JOIN artifacts a ON a.artifact_ref=i.result_ref
                  JOIN events e ON e.kind='artifact.published' AND e.subject_id=a.artifact_ref AND e.event_id<=?
        )
        SELECT DISTINCT a.run_id,a.artifact_ref FROM artifacts a JOIN edges i ON i.result_ref=a.artifact_ref
        JOIN reachable r ON r.sha=i.input_sha
        WHERE EXISTS (SELECT 1 FROM events e WHERE e.kind='artifact.published'
                      AND json_extract(e.payload_json,'$.sha256')=a.sha256 AND e.event_id<?)
          AND EXISTS (SELECT 1 FROM events e WHERE e.subject_id=a.artifact_ref
                      AND e.kind='artifact.published' AND e.event_id<=?)""",
        (through, bool(reg.get("policy")), through, json.dumps(pins), through, through, boundary, through))
    for row in candidates:
        for fact in _historical_facts(conn, row["run_id"], row["artifact_ref"], through):
            if _applies(fact, reg["claim_scope"]) and fact["nature"] in (NATURE[reg["prediction"]["target"]], "unclassified"):
                reason, value, _, reading = _reading(conn, reg, fact)
                if reg.get("policy") and value is not None and not reason:
                    if reading is not None and not reading["material_inputs"]:
                        continue
                    if reading is None and len(fact.get("data_refs", ())) > 1:
                        # Nested provenance may span several generations; only an explicitly
                        # pooled acquisition lacks per-material attribution.
                        provenance = conn.execute("""SELECT json_array_length(payload_json,'$.acquisition.data_refs')
                            FROM events WHERE run_id=? AND kind='v6.observation.added'
                            AND json_extract(payload_json,'$.artifact_ref')=? AND event_id<=?""",
                            (row["run_id"], row["artifact_ref"], through)).fetchall()
                        if any(p[0] and p[0] > 1 for p in provenance):
                            continue
                first = _first_relevant(conn, fact, reg)
                if not reason and value is not None and first is not None and first <= boundary:
                    return True
    return False


def _published(conn, run, scope, through):
    from . import v62_outputs
    from . import v62_lead, v62_reports
    private = v62_lead.branch_ids(conn, run)
    released = set(v62_reports.released_evidence_refs(conn, run, through)) if private and scope == "run" else set()
    selected_outputs = {}
    if released:
        for row in conn.execute("SELECT job_id,artifact_ref FROM artifacts WHERE run_id=? AND origin='JOB_OUTPUT' "
                                "AND artifact_ref IN (SELECT value FROM json_each(?))", (run, json.dumps(sorted(released)))):
            selected_outputs.setdefault(row["job_id"], []).append({"artifact_ref": row["artifact_ref"]})
    rows = conn.execute("SELECT event_id,kind,subject_id,payload_json,reason FROM events WHERE run_id=? AND event_id<=? "
                        "AND kind IN ('v6.observation.added','v6.node.settled') ORDER BY event_id", (run, through))
    for row in rows:
        item = json.loads(row["payload_json"])
        observed_scope = item.get("scope") or item.get("branch_id") or "run"
        internal = v62_lead.internal_event(row, private)
        if not _in_scope(scope, observed_scope) or (internal and not released):
            continue
        primary = item.get("artifact_ref") or item.get("result_ref")
        if not internal or primary in released:
            yield row["event_id"], item, row["reason"], observed_scope
        if item.get("status") in ("SUCCEEDED", "REUSED") and item.get("job_id"):
            # A public graph node releases its outputs. A private module releases only its cited originals.
            outputs = selected_outputs.get(item["job_id"], []) if internal else v62_outputs.published_outputs(conn, run, item["job_id"])
            for out in outputs:
                ref = out["artifact_ref"]
                if ref != primary and (not internal or ref in released):
                    yield row["event_id"], {**item, "result_ref": ref}, row["reason"], observed_scope


def _failure_matches(conn, item, data):
    # Some commission failures happen before Job admission. Their node still names the original input.
    if item.get("job_id"):
        row = conn.execute("SELECT admission_json FROM p0_execution_manifests WHERE job_id=?",
                           (item["job_id"],)).fetchone()
        if row and any(pin["sha256"] in data for pin in json.loads(row[0])["inputs"]):
            return True
    row = conn.execute("SELECT spec_json FROM v6_nodes WHERE node_id=?", (item.get("node_id"),)).fetchone()
    if row:
        refs = json.loads(row[0]).get("inputs", [])
        return conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref IN (SELECT value FROM json_each(?)) "
                            "AND sha256 IN (SELECT value FROM json_each(?)) LIMIT 1",
                            (json.dumps(refs), json.dumps(sorted(data)))).fetchone() is not None
    return False


def _results(conn, run, claim, reg, through, *, require_acquisition=False, timeline=None, bindings=None):
    from . import v62_outputs
    pred = reg["prediction"]
    data = {p["sha256"] for p in reg["data"]}
    results, gaps, prior_availability = [], [], False
    seen = set()
    original_facts = set()
    verified_materials = reg.get("material_identity") == "endpoint_verified"
    for eid, item, reason, observed_scope in _published(conn, run, claim["decision_scope"], through):
        if item.get("status") and item["status"] not in ("SUCCEEDED", "REUSED"):
            if _failure_matches(conn, item, data):
                gaps.append({"kind": "engineering_gap", "event_id": eid, "node_id": item.get("node_id"),
                             "job_id": item.get("job_id"), "status": item["status"], "reason": reason})
            continue
        ref = item.get("artifact_ref") or item.get("result_ref")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        fact = v62_outputs.classify(conn, run, ref, scope=observed_scope, through_event=through)
        pins = {p["sha256"] for p in fact.get("data_refs", [])} | {fact["sha256"]}
        matches = sorted(data & pins)
        if not matches and (bindings is None or not any(p["artifact_ref"] in bindings for p in [fact, *fact.get("data_refs", ())])):
            continue
        identity = ids.sha256_of_obj([fact["sha256"], fact["nature"], fact.get("capability"), fact.get("output_name"),
                                      fact.get("dependencies"), fact.get("unknown_dependencies"),
                                      fact.get("applicability_scopes"), matches])
        if not reg.get("policy"):
            if identity in original_facts:
                continue
            original_facts.add(identity)
        mismatch, value, metric, reading = _reading(conn, reg, fact)
        if mismatch:
            gaps.append({"kind": mismatch, "event_id": eid, "artifact_ref": ref})
            continue
        if value is None:
            gaps.append({"kind": "unmeasured_observable", "event_id": eid, "artifact_ref": ref,
                         "observable": pred["observable"]})
            continue
        if reg.get("policy"):
            from . import v622_propositions
            material_pins = v622_propositions.input_materials(fact, item, reading)
            if bindings is not None:
                if verified_materials:
                    from . import v623_chronology
                    keys = v623_chronology.material_keys(bindings, fact, item, reading)
                    identity = ids.sha256_of_obj([identity, keys])
                else:
                    keys = sorted({key for p in material_pins for key in bindings.get(p["artifact_ref"], ())})
                matches = sorted({p["sha256"] for p in reg["data"] if p.get("material_key", p["sha256"]) in keys})
            else:
                matches = sorted(data.intersection(p["sha256"] for p in material_pins))
            if not matches:
                gaps.append({"kind": "no_individual_material_attribution", "event_id": eid, "artifact_ref": ref})
                continue
        if reading:
            from .v622_measurement import comparison_value
            passed = COMPARISONS[pred["operator"]](comparison_value(reading), Decimal(str(pred["threshold"])))
        else:
            passed = COMPARISONS[pred["operator"]](value, pred["threshold"])
        if timeline is None:
            first = _first_relevant(conn, fact, reg)
        else:
            from . import v623_chronology
            first = min((timeline[sha]["endpoint_first_available_event_id"] for sha in keys
                         if timeline[sha]["endpoint_first_available_event_id"] is not None), default=None)
        launch = None
        if fact.get("job_id"):
            launch = conn.execute("SELECT min(event_id) FROM events WHERE subject_id=? AND kind='job.transition' "
                                  "AND new_state='SUBMITTING'", (fact["job_id"],)).fetchone()[0]
        exclusion = None
        if fact.get("record_kind") == "test_fixture":
            exclusion = "test_fixture"
        elif (timeline is not None and not fact.get("job_id") and pred["target"] == "experimental_observation" and
              (not v623_chronology.sample_identified(item) or fact.get("record_kind") == "conflicting_acquisition")):
            exclusion = "unknown_first_production"
        elif (require_acquisition and fact["nature"] in ("observed", "analysis")
              and not fact.get("original_inputs_grounded")):
            exclusion = "missing_original_acquisition"
        elif fact["nature"] != NATURE[pred["target"]]:
            exclusion = "nature_mismatch"
        elif fact["nature"] == "observed" and "model_prediction" in fact.get("upstream_natures", []):
            exclusion = "prediction_ancestry"
        elif ((require_acquisition and fact["nature"] in ("observed", "analysis") and not fact["applicability_scopes"])
              or not _applies(fact, claim["scope"])):
            exclusion = "out_of_scientific_scope"
        elif first is None:
            exclusion = "unknown_first_production"
        elif first <= reg["event_id"] or any(reg.get("known_at_registration", {}).get(sha) for sha in (keys if timeline is not None else matches)):
            exclusion = "already_available"
        elif verified_materials and any(timeline[key].get("material_identity_uncertainty") for key in keys):
            exclusion = "unknown_first_production"
        elif timeline is not None and any(timeline[k].get("sufficient_production_order_uncertain") for k in keys):
            exclusion = "unverified_production_order"
        elif fact.get("job_id") and (launch is None or launch <= reg["event_id"]):
            exclusion = "unverified_production_order"
        if exclusion:
            gap = {"kind": exclusion, "artifact_ref": ref, "event_id": eid, "nature": fact["nature"]}
            if reg.get("policy"):
                gap.update(matched_data_sha256=matches, sha256=fact["sha256"])
                if timeline is not None:
                    gap["matched_material_keys"] = keys
                if reading:
                    gap["measurement"] = reading
            if exclusion in ("already_available", "unverified_production_order"):
                prior_availability = prior_availability or exclusion == "already_available"
                gap.update(value=value, first_event_id=first, first_bytes_event_id=fact.get("first_event_id"),
                           launch_event_id=launch, supports=passed)
            gaps.append(gap)
            continue
        if reg.get("policy"):
            # Only a qualified result consumes its identity. An incompatible mask or
            # missing attribution may happen to produce the very same output bytes.
            if identity in original_facts:
                continue
            original_facts.add(identity)
        results.append({"artifact_ref": ref, "sha256": fact["sha256"], "event_id": eid, "first_event_id": first,
                        "first_bytes_event_id": fact.get("first_event_id"), "launch_event_id": launch,
                        "nature": fact["nature"], "value": value, "supports": passed, "matched_data_sha256": matches,
                        **({"matched_material_keys": keys} if timeline is not None else {}),
                        **({"measurement": reading} if reading else {}),
                        "measurement_schema": metric, "capability": fact.get("capability"), "output_name": fact.get("output_name"),
                        "original_observations": fact.get("original_observations", []),
                        "dependencies": fact.get("dependencies", {}), "unknown_dependencies": fact.get("unknown_dependencies", []),
                        "independence_complete": fact.get("independence_complete", False),
                        "applicability_scopes": fact.get("applicability_scopes", []),
                        "upstream_natures": fact.get("upstream_natures", [])})
    return results, gaps, prior_availability


def _acquisition_groups(results):
    """An alias shares every known dependency of the original, including through a third receipt."""
    groups = []
    for item in results:
        identities = {(key, value) for key in ("data", "acquisition", "sample", "source", "publication", "sampling")
                      for value in item["dependencies"].get(key, [])}
        identities.update(("bytes", p["sha256"]) for p in item["original_observations"])
        identities.add(("bytes", item["sha256"]))
        identities.update(("registered", sha) for sha in item["matched_data_sha256"])
        refs = {item["artifact_ref"]}
        other_groups = []
        for known, members in groups:
            if identities & known:
                identities.update(known)
                refs.update(members)
            else:
                other_groups.append((known, members))
        groups = [*other_groups, (identities, refs)]
    return {ref: group for _, group in groups for ref in group}


def _independence(results, scope):
    comparisons = []
    groups = _acquisition_groups(results)
    for a, b in itertools.combinations(results, 2):
        if not a["supports"] or not b["supports"]:
            continue
        shared = {key: sorted(set(a["dependencies"].get(key, [])) & set(b["dependencies"].get(key, [])))
                  for key in DEPENDENCIES}
        shared_inputs = sorted(set(a["matched_data_sha256"]) & set(b["matched_data_sha256"]))
        original_a = {p["sha256"] for p in a["original_observations"]}
        original_b = {p["sha256"] for p in b["original_observations"]}
        shared_data_ids = [key for key in shared["data"] if not key.startswith("sha256:")]
        original_refs_a = {p["artifact_ref"] for p in a["original_observations"]}
        original_refs_b = {p["artifact_ref"] for p in b["original_observations"]}
        if shared_data_ids or original_refs_a & original_refs_b:
            relation = "same_data_method_consistency"
        elif any(shared[key] for key in ("acquisition", "sample", "source", "publication", "sampling")):
            relation = "dependent_acquisitions"
        elif shared_inputs:
            relation = "same_data_method_consistency"
        elif a["sha256"] == b["sha256"] or original_a & original_b:
            relation = "ambiguous_identical_bytes"
        elif (a["nature"] == "model_prediction" or b["nature"] == "model_prediction"
              or "model_prediction" in a["upstream_natures"] + b["upstream_natures"]):
            relation = "model_outputs"
        elif not original_a or not original_b or not a["independence_complete"] or not b["independence_complete"]:
            relation = "incomplete_provenance"
        elif set(a["applicability_scopes"]) != {scope} or set(b["applicability_scopes"]) != {scope}:
            relation = "unverified_claim_scope"
        elif b["artifact_ref"] in groups[a["artifact_ref"]]:
            relation = "dependent_acquisitions"
        else:
            relation = "independent_acquisitions"
        pair = {"result_refs": [a["artifact_ref"], b["artifact_ref"]], "relation": relation,
                "shared": shared, "shared_registered_data": shared_inputs}
        if relation == "dependent_acquisitions":
            pair["linked_results"] = sorted(groups[a["artifact_ref"]])
        comparisons.append(pair)
    return comparisons


def _grounds(conn, run, claim, through):
    from . import v62_sources
    source_records = list(claim["source_records"])
    for ref in claim.get("continuing_review_refs", []):
        prior = v62_sources.claim_ground(conn, run, ref, claim["decision_scope"], through)
        if prior:
            source_records.append(prior)
    revision_refs = claim["maturity_lineage"]
    for target in revision_refs:
        source_records.extend(v62_sources.published_reviews(conn, run, claim["decision_scope"], through, target))
    extractions = {f["evidence_ref"] for f in source_records if f.get("source_class") == "reviewed_source_evidence"}
    for evidence_ref in sorted(extractions):
        source_records.extend(v62_sources.related_reviews(conn, run, claim["decision_scope"], through, evidence_ref))
    facts = {}
    for source in source_records:
        key = source.get("review_ref") or source.get("artifact_ref") or source.get("ref")
        if key:
            facts[key] = source
    return facts


def assessment(conn, run, claim, reg, through, available_through, *, frozen_chronology=None):
    """Reproducible for the two recorded cursors, including a frozen brief older than adoption."""
    facts = _grounds(conn, run, claim, through)
    disputed = {fact.get("evidence_ref") for fact in facts.values()
                if fact.get("source_class") == "reviewed_source_evidence" and fact.get("record_nature") == "formal_evidence"
                and fact.get("verdict") in ("OVERSTATED", "NOT_SUPPORTED", "CONTRADICTED")}
    from . import v621_policy
    require_acquisition = v621_policy.enabled(conn, run)
    grounded = [ref for ref, fact in facts.items() if fact.get("evidence_ref") not in disputed
                and qualified_ground(fact, claim, require_acquisition=require_acquisition)]
    reviews = []
    gaps = []
    for ref, fact in facts.items():
        if fact.get("source_class") == "reviewed_source_evidence":
            reviews.append({key: fact.get(key) for key in ("review_ref", "evidence_ref", "source_ref", "source_sha256",
                            "segment_id", "direction", "claim", "verdict", "applicability", "record_nature",
                            "extract_receipt_id", "review_receipt_id", "target_claim_ref")})
        elif fact.get("source_class") == "execution_record":
            gaps.append({"kind": "engineering_gap" if fact.get("job_status") in ("FAILED", "CANCELLED", "TIMED_OUT")
                         else "execution_record", "artifact_ref": ref, "job_id": fact.get("job_id"),
                         "status": fact.get("job_status")})
    retrospective = {"already_available", "unverified_production_order"}
    current = bool(reg and reg.get("policy"))
    coverage = []
    results, pairs, preexisting = [], [], False
    if reg:
        timeline = bindings = None
        if reg.get("chronology_scope"):
            from . import v623_chronology
            cursor = max(available_through, through)
            if frozen_chronology is not None and cursor == reg["event_id"]:
                timeline, bindings = frozen_chronology
            else:
                bindings = {}
                timeline = v623_chronology.availability(conn, reg, cursor, bindings=bindings)
        results, test_gaps, old = _results(conn, run, claim, reg, through, require_acquisition=require_acquisition,
                                           timeline=timeline, bindings=bindings)
        gaps.extend(test_gaps)
        if current:
            from . import v622_propositions
            coverage = v622_propositions.with_coverage(reg, results, test_gaps, timeline)
            if timeline is not None:
                preexisting = old or any(r["qualification"] == "retrospective" for r in coverage)
            else:
                preexisting = old or any(r["known_at_registration"] for r in coverage)
        else:
            preexisting = old or _known_before(conn, reg, available_through)
        pairs = _independence(results, claim["scope"])
        grounded.extend(r["artifact_ref"] for r in results)
        grounded.extend(g["artifact_ref"] for g in test_gaps if g["kind"] in retrospective)
        grounded = list(dict.fromkeys(grounded))
    support = [r["review_ref"] for r in reviews if r["review_ref"] in grounded and r["direction"] == "supports"]
    opposing = [r["review_ref"] for r in reviews if r["review_ref"] in grounded and r["direction"] == "opposes"]
    reviewed_support, reviewed_opposition = bool(support), bool(opposing)
    support.extend(r["artifact_ref"] for r in results if r["supports"])
    opposing.extend(r["artifact_ref"] for r in results if not r["supports"])
    if not current:
        support.extend(g["artifact_ref"] for g in gaps if g["kind"] in retrospective and g["supports"])
        opposing.extend(g["artifact_ref"] for g in gaps if g["kind"] in retrospective and not g["supports"])
    if current:
        direction = v622_propositions.direction(coverage, reviewed_support, reviewed_opposition)
    elif support and opposing:
        direction = "mixed"
    elif opposing:
        direction = "opposed"
    elif support:
        direction = "supported"
    else:
        direction = "undetermined"
    reasons = []
    stage = "idea"
    if grounded:
        stage = "grounded"
        reasons.append("Applicable reviewed evidence or a classified scientific artifact is cited.")
    uncertain_production = any(g["kind"] == "unverified_production_order" for g in gaps)
    if reg and ((current and results) or (not preexisting and not uncertain_production)):
        stage = "preregistered"
        reasons.append("GX froze the rule before each qualifying prospective result." if current else
                       "The registered rule, scope and input versions precede the first known applicable result.")
        if results:
            stage = "tested"
            reasons.append("GX compared the original measured value with the registered decision rule.")
        if (any(pair["relation"] == "independent_acquisitions" for pair in pairs) and not opposing
                and (not current or direction == "supported")):
            stage = "replicated"
            reasons.append("Independent acquisitions support the registered claim within the same attested scope.")
    if uncertain_production:
        reasons.append("GX cannot establish prospective order for a detached Job launched before registration.")
    if reg and preexisting:
        reasons.append("An applicable result existed by registration; interpretation of this rule is retrospective.")
    if not reasons:
        reasons.append("No applicable independently reviewed evidence or classified scientific result is cited.")
    if gaps:
        reasons.append("Unmeasured outcomes and execution gaps do not establish counterevidence.")
    if disputed:
        reasons.append("Published reviews dispute this extraction. Its interpretation remains unresolved.")
    if opposing:
        reasons.append("Adverse results or independently reviewed opposing evidence are retained.")
    return {"rule": reg["policy"] if current else RULE,
            **({"material_coverage": coverage} if current else {}), "stage": stage, "direction": direction, "reasons": reasons, "registration": reg,
            "preexisting_result": preexisting, "grounds": grounded, "reviewed_evidence": reviews, "disputed_extractions": sorted(disputed),
            "supporting_refs": support, "opposing_refs": opposing, "results": results, "replication": pairs,
            "gaps": gaps}


def continuing_lineage(claim, prior, data, method_pin):
    return prior.get("maturity_lineage", []) if _same_proposition(claim, prior, data, method_pin) else []


def attach(conn, run, claim, prior, data, method_pin, event_id):
    lineage = continuing_lineage(claim, prior, data, method_pin)
    if lineage:
        claim["continuing_review_refs"] = [r["review_ref"] for r in prior["maturity"]["reviewed_evidence"]]
    claim["maturity_lineage"] = list(dict.fromkeys([*lineage, claim["claim_ref"], claim["claim_revision_id"]]))
    reg = registration(claim, prior, data, method_pin, event_id)
    frozen_chronology = None
    if reg and reg["event_id"] == event_id:
        from . import v621_policy, v622_propositions
        if v621_policy.current(conn, run):
            frozen_chronology = v622_propositions.freeze(conn, reg, claim, prior,
                                                         typed_chronology=v621_policy.live_science(conn, run),
                                                         verified_materials=v621_policy.science_execution(conn, run))
    claim["assessment_cursor"] = claim["event_cursor"]
    claim["availability_cursor"] = event_id
    claim["maturity"] = assessment(conn, run, claim, reg, claim["assessment_cursor"], event_id,
                                   frozen_chronology=frozen_chronology)


def recompute(conn, run, revision):
    """Replay one immutable claim revision without writing or substituting newer evidence."""
    from . import v6_claims
    claim = v6_claims.read_revision(conn, run, revision)
    if not claim or "maturity" not in claim:
        return None
    return assessment(conn, run, claim, claim["maturity"]["registration"],
                      claim["assessment_cursor"], claim["availability_cursor"])


def reassess(conn, run_id):
    from . import v6_claims
    if not enrolled(conn, run_id):
        return []
    # A completed publication can change interpretation. Native tool drafts and reads cannot.
    row = conn.execute("""
        SELECT max(event_id) FROM events WHERE run_id=? AND (kind IN (
            'v6.observation.added','v6.node.settled','v6.claim.adopted','v62.module.wake','v62.source.reviewed'))""", (run_id,)).fetchone()
    through = row[0]
    checkpoint = conn.execute("SELECT event_cursor FROM v62_maturity_cursors WHERE run_id=?", (run_id,)).fetchone()
    if through is None or (checkpoint and checkpoint[0] >= through):
        return []
    # Calculate with the reader first; admission and concurrent reassessments retain the existing head CAS.
    changes = []
    for old in v6_claims.current(conn, run_id):
        if "maturity" not in old or old["assessment_cursor"] >= through:
            continue
        available = max(through, old["availability_cursor"])
        new = assessment(conn, run_id, old, old["maturity"]["registration"], through, available)
        if new != old["maturity"]:
            changes.append((old, new, available))
    receipts = []
    with store.transaction(conn, transaction_owner="ScientificLead", operation="reassess_claims", run_id=run_id):
        for old, new, available in changes:
            hid, parent = old["hypothesis_id"], old["revision"]
            updated = conn.execute("UPDATE hypotheses SET head_revision=head_revision+1 WHERE run_id=? "
                                   "AND hypothesis_id=? AND head_revision=?", (run_id, hid, parent))
            if not updated.rowcount:
                # Another writer changed the premises. Leave the cursor available for a fresh read.
                return receipts
            version = parent + 1
            revision_id = f"{hid}@{version}"
            receipt = {**old, "revision": version, "claim_revision_id": revision_id,
                       "claim_ref": revision_id, "originating_claim_ref": old.get("originating_claim_ref", old["claim_ref"]),
                       "assessment_cursor": through, "availability_cursor": available,
                       "assessment_of": old["claim_revision_id"], "revision_author": "GX", "maturity": new,
                       "maturity_lineage": [*old["maturity_lineage"], revision_id]}
            if "publication" in old:
                published = old["publication"]
                v6_claims.set_publication(receipt, proposed_level=published["proposed_level"],
                                          proposal_ref=published["proposal_ref"])
            conn.execute("INSERT INTO hypothesis_revisions(hypothesis_id,revision,parent_revision,document_json,sha256,"
                         "author_principal,created_at) VALUES(?,?,?,?,?,'GX',?)",
                         (hid, version, parent, ids.canonical_json(receipt), ids.sha256_of_obj(receipt), clock.now_iso()))
            adverse = set(new["opposing_refs"]) - set(old["maturity"]["opposing_refs"])
            refs = list(dict.fromkeys([*new["grounds"], *new["supporting_refs"], *new["opposing_refs"]]))
            store.append_event(conn, run_id=run_id, kind="v62.claim.reassessed", actor="GX", subject_kind="hypothesis",
                               subject_id=hid, prev_revision=parent, new_revision=version,
                               payload={"scope": receipt["decision_scope"], "claim_ref": revision_id,
                                        "claim_revision_id": revision_id, "previous_claim_ref": old["claim_revision_id"],
                                        "before": old["maturity"]["stage"], "after": new["stage"],
                                        "direction": new["direction"], "evidence_refs": refs,
                                        "critical_counterevidence": bool(adverse), "through_cursor": through})
            receipts.append(receipt)
        conn.execute("INSERT INTO v62_maturity_cursors VALUES (?,?) ON CONFLICT(run_id) DO UPDATE "
                     "SET event_cursor=max(event_cursor,excluded.event_cursor)", (run_id, through))
    return receipts
