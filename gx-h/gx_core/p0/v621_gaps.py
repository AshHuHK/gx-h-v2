"""Bind a research gap to an observable on a specific acquired material."""
import json
import operator
import re

from ..domain import ids
from ..errors import GxError, SchemaInvalid, ScopeDenied, StaleRevision
from . import manifest, v62_maturity, v62_outputs, v621_policy, v623_chronology

_COMPARISONS = {"eq": operator.eq, "lt": operator.lt, "lte": operator.le, "gt": operator.gt, "gte": operator.ge}


def register(conn, module, payload, sources):
    if "gap_criteria" not in payload:
        return {}
    if not v621_policy.enabled(conn, module["run_id"]):
        raise SchemaInvalid("Gap criteria require the current research evidence policy")
    declarations = payload["gap_criteria"]
    if not isinstance(declarations, list):
        raise SchemaInvalid("Gap criteria must be a list of declarations")
    available = {s["artifact_ref"]: s["sha256"] for s in sources}
    result = {}
    verified = v621_policy.science_execution(conn, module["run_id"])
    for entry in declarations:
        required = {"question", "data_ref", "applicability", "measurement_schema", "observable", "nature", "op"}
        optional = {"method", "output_name", "method_parameters", "threshold"}
        current = v621_policy.current(conn, module["run_id"])
        if current:
            optional.update(("metric_id", "unit", "input_role"))
        if (not isinstance(entry, dict) or not required <= entry.keys() <= required | optional
                or not all(isinstance(entry[k], str) and entry[k].strip() for k in required)):
            raise SchemaInvalid("Gap criteria must name the question, material, scope, observable and closure test")
        q, ref = entry["question"], entry["data_ref"]
        if q not in payload["gaps"] or q in result or payload["gaps"].count(q) != 1:
            raise SchemaInvalid("Each gap criterion must identify a single reported question")
        if ref not in available:
            raise ScopeDenied("A gap must cite the material it concerns")
        pointer = entry["observable"]
        if not pointer.startswith("/") or len(pointer) < 2 or re.search(r"~(?![01])", pointer):
            raise SchemaInvalid("A gap observable must name a numeric field using a JSON Pointer")
        op = entry["op"]
        if op == "finite":
            if "threshold" in entry:
                raise SchemaInvalid("A finite reading criterion has no threshold")
        elif op not in _COMPARISONS or not v62_maturity._finite(entry.get("threshold")):
            raise SchemaInvalid("A threshold criterion requires a finite value and supported comparison")
        method_pin = None
        if entry["nature"] == "analysis":
            method = entry.get("method", "")
            if (not isinstance(method, str) or method.count("@") != 1
                    or not isinstance(entry.get("output_name"), str) or not entry["output_name"].strip()):
                raise SchemaInvalid("An analysis criterion requires the method version and named output")
            parameters = entry.get("method_parameters", {})
            if not isinstance(parameters, dict) or not all(isinstance(k, str) and k for k in parameters):
                raise SchemaInvalid("Gap method parameters must identify named analysis settings")
            if current:
                from . import v622_measurement
                selectors = {k: entry[k] for k in ("metric_id", "unit", "input_role") if k in entry}
                method_pin = v622_measurement.pin(conn, module["run_id"], {
                    "method": {"capability": method, "output": entry["output_name"], "parameters": parameters,
                               **selectors}, "observable": entry["observable"],
                    "measurement_schema": entry["measurement_schema"]}, applicability=entry["applicability"])
            else:
                method_pin = v62_maturity.pin_method(conn, module["run_id"],
                    {"method": {"capability": method, "output": entry["output_name"]}})
        elif entry["nature"] != "observed" or {"method", "output_name", "method_parameters", "metric_id", "unit", "input_role"} & entry.keys():
            raise SchemaInvalid("Original observations and named analysis outputs have separate criteria")
        criterion = {**entry, "version": 1, "data_sha256": available[ref]}
        if method_pin:
            criterion["method_sha256"] = method_pin["manifest_sha256"]
            if current:
                criterion.update(version=2, method_pin=method_pin)
        if verified:
            criterion["material_identity"] = "endpoint_verified"
        result[q] = criterion
    return {"gap_criteria": result}


def declaration(bundle, question, source_hashes, report_ref, report_event):
    criterion = bundle.get("gap_criteria", {}).get(question)
    goal = bundle["goal_version"]
    key = "gap:" + ids.sha256_of_obj([bundle["run_id"], goal, question.casefold().split(), source_hashes, criterion])
    return {"gap_ref": key, "question": question, "report_ref": report_ref, "goal_version": goal,
            "registered_event_id": report_event, "criterion": criterion,
            "closure": "conditional" if criterion else "awaiting_review"}


def _endpoint(conn, criterion, through):
    if criterion.get("material_identity") != "endpoint_verified":
        return None
    pin = {"artifact_ref": criterion["data_ref"], "sha256": criterion["data_sha256"]}
    pred = {"target": "analysis" if criterion["nature"] == "analysis" else "experimental_observation",
            "measurement_schema": criterion["measurement_schema"], "observable": criterion["observable"]}
    reg = {"data": [pin], "prediction": pred, "method_pin": criterion.get("method_pin"),
           "material_identity": criterion["material_identity"]}
    bindings = {}
    timeline = v623_chronology.availability(conn, reg, through, bindings=bindings)
    return pred, timeline[pin["sha256"]], bindings


def _acquired_observation(conn, task, ref):
    # Classification already rejected conflicting or out-of-scope acquisition receipts.
    row = conn.execute("""SELECT json_extract(payload_json,'$.acquisition') FROM events
        WHERE run_id=? AND kind='v6.observation.added' AND event_id<=? AND
        json_extract(payload_json,'$.scope')='run' AND json_extract(payload_json,'$.artifact_ref')=?
        AND json_type(payload_json,'$.acquisition')='object' ORDER BY event_id LIMIT 1""",
        (task["run_id"], task["event_cursor"], ref)).fetchone()
    return {"acquisition": json.loads(row[0])} if row else {}


def _match(conn, fact, criterion, boundary, endpoint=None, observation=None):
    if fact["nature"] != criterion["nature"] or fact["record_kind"] not in ("supplied_observation", "job_output"):
        return "source_nature", None
    if "model_prediction" in fact["upstream_natures"] or not fact["original_observations"]:
        return "missing_original", None
    if criterion["nature"] == "analysis" and not fact.get("original_inputs_grounded"):
        return "missing_original", None
    target = {"artifact_ref": criterion["data_ref"], "sha256": criterion["data_sha256"]}
    if endpoint is None and target not in fact["data_refs"] and target not in fact["original_observations"]:
        return "different_material", None
    if set(fact["applicability_scopes"]) != {criterion["applicability"]}:
        return "different_scope", None
    capability = fact.get("capability", {})
    if criterion["nature"] == "analysis" and (
        criterion["method"] != f"{capability.get('id')}@{capability.get('version')}"
        or criterion["method_sha256"] != capability.get("sha256")
        or criterion["output_name"] != fact.get("output_name")
    ):
        return "different_method_or_output", None
    reading = None
    if criterion.get("method_pin"):
        from . import v622_measurement
        reason, reading = v622_measurement.match(conn, criterion, fact)
        if reason:
            return reason, None
        if endpoint is None and target not in [{k: i[k] for k in ("artifact_ref", "sha256")} for i in reading["material_inputs"]]:
            return "different_material", None
    parameters = criterion.get("method_parameters")
    if parameters and reading is None:
        admitted = manifest.read_manifest(conn, fact["job_id"])["admission"]["parameters"]
        if any(k not in admitted or ids.canonical_json(admitted[k]) != ids.canonical_json(v)
               for k, v in parameters.items()):
            return "different_method_parameters", None
    if endpoint is not None:
        pred, timing, bindings = endpoint
        keys = v623_chronology.material_keys(bindings, fact, observation or {}, reading)
        if target["sha256"] not in keys:
            return "different_material", None
        if reading is None:
            # Another receipt for this endpoint cannot supply a missing attribution on this reading.
            deps = (observation or {}).get("acquisition", {}).get("dependencies", {})
            if not (deps.get("source") and deps.get("acquisition")):
                return "unknown_first_production", None
        if uncertainty := timing.get("material_identity_uncertainty"):
            return uncertainty[0], None
        first = timing["endpoint_first_available_event_id"]
    else:
        first = v62_maturity._first_relevant(conn, fact, {"event_id": boundary})
        if first is None or first <= boundary:
            return "already_available", None
    if reading:
        number, name = reading["value"], reading["metric"]
    elif endpoint is not None:
        number, name = v623_chronology.external_measurement(conn, fact["artifact_ref"], pred)
    else:
        number, name = v62_maturity._measurement(conn, fact["artifact_ref"], criterion["observable"])
    if name != criterion["measurement_schema"] or number is None:
        return "missing_observable_or_schema", None
    if endpoint is not None:
        if first is None:
            return "unknown_first_production", None
        if first <= boundary:
            return "already_available", None
    if criterion["op"] != "finite":
        from decimal import Decimal
        value = v622_measurement.comparison_value(reading) if reading else number
        threshold = Decimal(str(criterion["threshold"])) if reading else criterion["threshold"]
        if not _COMPARISONS[criterion["op"]](value, threshold):
            return "threshold_not_met", None
    receipt = {key: fact[key] for key in ("artifact_ref", "sha256", "nature", "record_kind",
                                         "original_observations", "applicability_scopes")}
    receipt.update(value=number, observable=criterion["observable"], measurement_schema=name,
                   first_production_event=first, output_name=fact.get("output_name"), job_id=fact.get("job_id"))
    if endpoint is not None:
        receipt.update(material=target, endpoint_first_available_event_id=first)
    if reading:
        receipt.update(measurement=reading)
    if fact.get("job_id"):
        receipt.update(capability=capability, admission_sha256=fact["admission_sha256"],
                       manifest_sha256=fact["completion_sha256"])
    return "matched", receipt


def prepare(conn, task, decision):
    """Classify visible source bytes and perform numerical comparisons before the writer is acquired."""
    selected = decision["resolved_gaps"]
    if task["scope"] != "run":
        raise ScopeDenied("Only the Run Lead can resolve research gaps")
    brief = json.loads(task["snapshot_json"])
    gaps = {g["gap_ref"]: g for g in brief.get("research_progress", {}).get("open_gaps", [])}
    if any(g not in gaps for g in selected):
        from . import harness_policy
        if harness_policy.enabled(brief):
            raise ScopeDenied("resolved_gaps accepts only gap_ref values from research_progress.open_gaps in this "
                              "Lead brief. Omit resolved_gaps if this decision does not resolve a registered gap.",
                              details={"field": "resolved_gaps"})
        raise ScopeDenied("The requested gap must be open in this frozen Lead brief")
    visible = set(json.loads(task["visible_refs_json"]))
    if not set(decision["evidence_refs"]) <= visible:
        raise ScopeDenied("Use gap evidence released into this Lead brief")
    facts, observations, results = {}, {}, []
    for key in selected:
        gap = gaps[key]
        criterion = gap.get("criterion")
        row = {"gap_ref": key, "goal_version": task["goal_version"], "criterion": criterion,
               "report_ref": gap["report_ref"], "through_cursor": task["event_cursor"],
               "matches": [], "rejections": []}
        if criterion is None:
            row["reason"] = "awaiting_review"
        elif gap.get("goal_version") != task["goal_version"]:
            raise StaleRevision("A different goal version established this gap")
        else:
            endpoint = _endpoint(conn, criterion, task["event_cursor"])
            for ref in decision["evidence_refs"]:
                if ref not in facts:
                    try:
                        facts[ref] = v62_outputs.classify(conn, task["run_id"], ref, scope="run",
                                                          through_event=task["event_cursor"])
                    except GxError as exc:
                        facts[ref] = exc
                if isinstance(facts[ref], GxError):
                    row["rejections"].append({"artifact_ref": ref, "reason": "unverified_source",
                                               "code": facts[ref].code})
                    continue
                if endpoint is not None and facts[ref]["record_kind"] == "supplied_observation" and ref not in observations:
                    observations[ref] = _acquired_observation(conn, task, ref)
                reason, match = _match(conn, facts[ref], criterion, gap["registered_event_id"], endpoint,
                                       observations.get(ref))
                if match is None:
                    row["rejections"].append({"artifact_ref": ref, "reason": reason})
                else:
                    row["matches"].append(match)
            row["reason"] = "matched" if row["matches"] else "no_matching_evidence"
        results.append(row)
    if any(row["reason"] != "matched" for row in results):
        raise SchemaInvalid("The batch includes unresolved research gaps; submit matching gaps separately",
                            details={"gap_results": results})
    return results
