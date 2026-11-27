"""Match a frozen registered endpoint to a sealed, role-bound scientific output."""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import json

from .. import config
from ..artifacts import ArtifactService
from ..capabilities import manifest as capabilities, registry, shipped
from ..domain import ids
from ..errors import GxError, SchemaInvalid
from . import manifest, v621_metrics, v62_maturity


def pin(conn, run_id, prediction, *, applicability=None):
    """Pin a scientific readout independently of the claim's threshold and proposed material."""
    method = prediction.get("method")
    if not method:
        return None
    base = v62_maturity.pin_method(conn, run_id, {"method": {
        "capability": method["capability"], "output": method["output"]}})
    installed = registry.candidate(conn, method["capability"])
    card = installed["manifest"]
    output = next((o for o in card["outputs"] if o["name"] == method["output"]), None)
    if output is None:
        raise SchemaInvalid("The predicted output is absent from the granted method")
    supplied = method.get("parameters", {})
    if not isinstance(supplied, dict):
        raise SchemaInvalid("Measurement parameters must be named")
    params = capabilities.effective_parameters(card, supplied)
    expected = set(capabilities.parameter_names(card))
    if set(params) - expected:
        raise SchemaInvalid("Measurement parameters are absent from the pinned capability")
    # A partial historical request may be retained as a proposal. It cannot match a
    # calculation until every effective setting and a controlled reader are known.
    missing = sorted(expected - set(params))
    specs = output.get("metric_specs", {})
    # Old signed packages can have an additional published projection (e.g. a
    # ratio stored beside its counts). Compatibility pins the original executable
    # and manifest together. Future packages declare their own result_pointer.
    compatibility = json.loads((config.CONFIG_DIR / "measurement_selector_compatibility.json").read_bytes())
    aliases = compatibility.get(shipped.portable_digest(installed["bundle"]), {}).get(method["output"], {})
    selector = prediction["observable"]
    allowed = {key: {"/metrics/" + key, decl.get("pointer"), decl.get("result_pointer") or aliases.get(key)}
               for key, decl in specs.items()}
    name = method.get("metric_id")
    if name is None:
        candidates = [key for key in specs if selector in allowed[key]]
        if len(candidates) == 1:
            name = candidates[0]
    decl = specs.get(name)
    if name is not None and (not decl or selector not in allowed[name]):
        raise SchemaInvalid("The metric and its result selector must be declared on the same named output")
    result_pointer = (decl.get("result_pointer") or aliases.get(name)) if decl else None
    if method.get("unit") is not None and (decl is None or decl["unit"] != method["unit"]):
        raise SchemaInvalid("The requested unit differs from the declared metric")
    roles = sorted(output.get("source_roles", capabilities.input_roles(card)))
    if "input_role" in method and method["input_role"] not in roles:
        raise SchemaInvalid("The requested material role is absent from this scientific output")
    role = method.get("input_role") or (roles[0] if len(roles) == 1 else None)
    endpoint = {**base, "schema": "gx.measurement-spec/1", "parameters": params,
                "missing_parameters": missing, "output_name": method["output"],
                "metric_id": name, "metric_definition": decl, "unit": decl["unit"] if decl else None,
                "source_roles": roles, "input_role": role, "observable": "/metrics/" + name if decl else selector,
                "result_pointer": result_pointer,
                "measurement_schema": prediction.get("measurement_schema"), "applicability": applicability}
    if output.get("sufficient_outputs"):
        tables = []
        for table in output["sufficient_outputs"]:
            name, version = capabilities.parse_capability_version(table["capability"])
            producer = conn.execute("SELECT manifest_sha256,outputs_json FROM capabilities "
                                    "WHERE capability_id=? AND version=?", (name, version)).fetchone()
            # An optional producer never installed in this store has no local Job history.
            if producer is None:
                continue
            declared = next((item for item in json.loads(producer["outputs_json"]) if item["name"] == table["output"]), None)
            if (not declared or table["input_role"] not in declared.get("source_roles", ())
                    or not set(table["match_parameters"]) <= expected):
                raise SchemaInvalid("Sufficient output does not bind the declared material role and shared parameters")
            tables.append({**table, "manifest_sha256": producer["manifest_sha256"]})
        endpoint["sufficient_outputs"] = tables
    from . import v621_policy
    if v621_policy.live_science(conn, run_id):
        endpoint["schema"] = "gx.measurement-spec/2"
        definition = {k: v for k, v in endpoint.items() if k != "applicability"}
    else:
        definition = endpoint
    return {**base, "measurement_spec": endpoint, "measurement_spec_sha256": ids.sha256_of_obj(definition)}


def match(conn, registration, fact, *, chronology_only=False):
    """Read a prospective or retrospective fact with the same fully pinned measurement definition.

    A grouped scalar has input provenance, but no one-to-one material observations.
    """
    method = registration["method_pin"]
    spec = method["measurement_spec"]
    cap = fact.get("capability") or {}
    if (spec["capability"] != f"{cap.get('id')}@{cap.get('version')}"
            or spec["manifest_sha256"] != cap.get("sha256") or spec["output_name"] != fact.get("output_name")
            or not fact.get("job_id")):
        return "different_method_or_output", None
    if spec["missing_parameters"]:
        return "unregistered_method_parameters", None
    scopes = fact.get("applicability_scopes", [])
    if not chronology_only and spec.get("applicability") and scopes and set(scopes) != {spec["applicability"]}:
        return "out_of_scientific_scope", None
    sealed = manifest.read_manifest(conn, fact["job_id"])
    adm = sealed["admission"]
    if ids.canonical_json(adm["parameters"]) != ids.canonical_json(spec["parameters"]):
        return "different_method_parameters", None
    named = next((d for d in adm.get("output_declarations", ()) if d["name"] == spec["output_name"]), None)
    key = spec["metric_id"]
    if not key or not named or not spec["metric_definition"] or (
            named.get("metric_specs", {}).get(key) != spec["metric_definition"]):
        return "unregistered_metric_definition", None
    ref = fact["artifact_ref"]
    metric = next((m for m in v621_metrics.from_output(conn, adm["run_id"], ref) if m["metric_id"] == key), None)
    if not metric or metric["schema"] != v621_metrics.SCHEMA or metric["unit"] != spec["unit"]:
        return "unavailable_metric", None
    result_pointer = spec["result_pointer"]
    document_schema = None
    if spec["measurement_schema"] is not None or result_pointer:
        assets = ArtifactService(conn)
        meta = assets.describe(ref)
        raw = assets.read_bytes(ref, expected_sha256=metric["sha256"], max_bytes=v621_metrics.METRIC_READER_BYTES)
        document = v621_metrics._read(raw, metric["reader"], metric["output_name"], meta["media_type"])
        document_schema = document.get("schema") if isinstance(document, dict) else None
        if spec["measurement_schema"] is not None and spec["measurement_schema"] != document_schema:
            return "missing_observable_or_schema", None
        if result_pointer:
            try:
                # Published JSON may encode a finite float approximation of a count ratio.
                # Compare the projection here; decisions below always use the original counts.
                scalar = v621_metrics._number(v621_metrics._fact(document, result_pointer))
            except (GxError, ValueError, TypeError, IndexError, KeyError):
                return "missing_observable_or_schema", None
            if scalar != Decimal(str(metric["value"])):
                return "different_metric_value", None
    inputs = list(metric["inputs"])
    one = inputs if len(inputs) == 1 and inputs[0]["role"] == spec["input_role"] else []
    return None, {"value": metric["value"], "raw_value": metric["raw_value"], "metric": document_schema,
                  "comparison_value": metric["raw_value"], "metric_ref": metric["metric_ref"],
                  "metric_id": key, "unit": metric["unit"], "metric_scope": metric["scope"],
                  "measurement_spec_sha256": method["measurement_spec_sha256"],
                  "source_inputs": inputs, "material_inputs": one, "sha256": fact["sha256"],
                  "manifest_sha256": sealed["manifest_hash"], "admission_sha256": sealed["admission_hash"],
                  "lineage": fact.get("original_observations", []), "applicability_scopes": scopes,
                  **{key: metric[key] for key in ("numerator", "denominator") if key in metric}}


def comparison_value(reading):
    """Compare the original counts exactly, including thresholds on rounded display boundaries."""
    if "numerator" in reading:
        return Fraction(reading["numerator"], reading["denominator"])
    return Fraction(Decimal(reading["raw_value"]))
