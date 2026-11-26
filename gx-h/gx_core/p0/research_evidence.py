"""Read a Goal's immutable scientific records for the PI report."""
import json

from ..artifacts import ArtifactService
from . import manifest, v6_claims, v6_goal, v621_metrics, v623_endpoints, v623_exposure


def _fields(record, names):
    return {k: record[k] for k in names.split() if k in record}


def _materials(conn, rid):
    row = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()
    materials = {i["artifact_ref"]: _fields(i, "artifact_ref sha256 bytes role")
                 for i in json.loads(row[0]).get("files", []) if i.get("artifact_ref")}
    for row in conn.execute("""SELECT event_id,ts,kind,payload_json FROM events WHERE run_id=?
            AND kind IN ('v6.observation.added','v621.material.assigned') ORDER BY event_id""", (rid,)):
        body = json.loads(row["payload_json"])
        item = materials.setdefault(body["artifact_ref"], _fields(body, "artifact_ref sha256"))
        item.update(_fields(body, "role name"))
        provenance = _fields(body, "source scope acquisition decision_ref")
        item.setdefault("provenance", []).append({**provenance, "event_id": row["event_id"], "utc": row["ts"]})
    return list(materials.values())


def _claims(conn, rid):
    # Collect each registration's assessments before ordering the versions of a hypothesis.
    rows = conn.execute("""SELECT r.document_json,r.created_at,r.author_principal,e.kind,e.event_id,e.ts,
                  t.goal_version FROM hypotheses h JOIN hypothesis_revisions r USING(hypothesis_id)
            LEFT JOIN events e ON e.run_id=h.run_id AND e.subject_id=h.hypothesis_id
                AND e.new_revision=r.revision AND e.kind IN ('v6.claim.adopted','v62.claim.reassessed')
            LEFT JOIN v6_decisions d ON d.run_id=h.run_id
                AND d.decision_ref=json_extract(r.document_json,'$.decision_id')
            LEFT JOIN v6_decision_tasks t ON t.run_id=h.run_id AND t.task_id=d.task_id
            WHERE h.run_id=? AND json_extract(r.document_json,'$.schema')=?
            ORDER BY r.rowid""", (rid, v6_claims.SCHEMA))
    predictions, exploratory = {}, []
    for r in rows:
        claim = json.loads(r["document_json"])
        maturity = claim.get("maturity", {})
        reg = maturity.get("registration")
        assessment = {**_fields(claim, "claim_revision_id decision_id assessment_cursor availability_cursor level"),
                      **_fields(maturity, "stage direction results gaps preexisting_result material_coverage "
                                "supporting_refs opposing_refs reviewed_evidence disputed_extractions replication reasons"),
                      "utc": r["ts"] or r["created_at"], "event_id": r["event_id"], "event": r["kind"],
                      "author": r["author_principal"]}
        details = _fields(claim, "hypothesis_id statement scope criteria decision_scope limitations "
                          "lead_interpretation evidence_refs decision_delivery source_records study_class")
        if not reg:
            exploratory.append({**details, **assessment, "goal_version": r["goal_version"],
                                "classification": "unregistered_interpretation"})
            continue
        key = reg["claim_revision_id"]
        if key not in predictions:
            stamp = conn.execute("SELECT ts FROM events WHERE run_id=? AND event_id=?", (rid, reg["event_id"])).fetchone()
            predictions[key] = {**details, "goal_version": r["goal_version"], "prediction": reg["prediction"],
                                "registration": {**reg, "utc": stamp[0] if stamp else None,
                                                 "decision_ref": claim["decision_id"]}, "assessments": []}
        item = predictions[key]
        item["assessments"].append(assessment)
        if claim.get("study_class") == "historical_replay":
            item["classification"] = "historical_replay"
        elif reg.get("posthoc_revision_of") or maturity.get("preexisting_result"):
            item["classification"] = "contains_retrospective_evidence"
        elif maturity.get("results"):
            item["classification"] = "prospectively_tested"
        else:
            item["classification"] = "registered_without_qualifying_result"
    return list(predictions.values()), exploratory


def _comparisons(conn, rid):
    plans = {}
    for row in conn.execute("""SELECT decision_ref,receipt_json,e.ts FROM v6_decisions d JOIN events e
            ON e.run_id=d.run_id AND e.event_id=json_extract(d.receipt_json,'$.event_id')
            WHERE d.run_id=? ORDER BY e.event_id""", (rid,)):
        receipt = json.loads(row["receipt_json"])
        if binding := receipt.get("comparison"):
            plans[binding["comparison_id"]] = {**binding, "registered_utc": row["ts"], "actions": []}
        action = receipt.get("comparison_action")
        if action and action["comparison_id"] in plans:
            plans[action["comparison_id"]]["actions"].append({**action, "event_id": receipt["event_id"],
                                                           "decision_ref": row["decision_ref"], "utc": row["ts"]})
    return list(plans.values())


def _jobs(conn, rid):
    jobs, metrics_by_source = [], {}
    assets = ArtifactService(conn)
    for row in conn.execute("""SELECT job_id,status,capability_id,capability_version FROM jobs
            WHERE run_id=? ORDER BY rowid""", (rid,)):
        proof = manifest.read_manifest(conn, row["job_id"], missing_ok=True)
        adm = proof["admission"] if proof else {}
        completion = (proof or {}).get("completion") or {}
        job = {"job_id": row["job_id"], "status": row["status"], "method": adm.get("capability", {
               "id": row["capability_id"], "version": row["capability_version"]}),
               "parameters": adm.get("parameters"), "inputs": adm.get("inputs", []), "owner": adm.get("owner"),
               "manifest_sha256": (proof or {}).get("manifest_hash"), "outputs": []}
        jobs.append(job)
        for output in completion.get("outputs", []):
            ref = output["artifact_ref"]
            item = _fields(output, "output_name artifact_ref sha256 bytes nature source_inputs")
            meta = assets.describe(ref)
            item["location"] = {"namespace": meta["namespace"], "rel_path": meta["rel_path"]}
            metrics = v621_metrics.from_output(conn, rid, ref)
            metrics_by_source[ref] = metrics
            item["metrics"] = []
            specs = next((d.get("metric_specs", {}) for d in adm.get("output_declarations", [])
                          if d["name"] == output.get("declaration_name", output["output_name"])), {})
            for metric in metrics:
                measured = _fields(metric, "schema metric_id metric_ref value raw_value display unit scope "
                                   "numerator denominator reason")
                if metric["schema"] == v621_metrics.MISSING:
                    measured.update(value=None, unit=specs.get(metric["metric_id"], {}).get("unit"))
                item["metrics"].append(measured)
            # Bounded JSON summaries retain the plugin's own aggregate counts and nulls.
            # Named detail tables keep their exact locator and digest regardless of size.
            if (v6_goal._is_json_artifact(meta)
                    and meta["bytes"] <= v621_metrics.METRIC_READER_BYTES):
                document = json.loads(assets.read_bytes(ref, expected_sha256=output["sha256"]))
                if isinstance(document, dict):
                    item["summary"] = {k: v for k, v in document.items()
                                       if not isinstance(v, (dict, list)) or (isinstance(v, list) and len(v) <= 16
                                           and all(not isinstance(x, (dict, list)) for x in v))}
                    for name in ("counts", "denominators", "summary"):
                        if isinstance(document.get(name), dict):
                            item["summary"][name] = document[name]
                    item["denominators"] = _denominators(item["summary"])
            job["outputs"].append(item)
    return jobs, metrics_by_source


def _deliveries(conn, rid, metrics_by_source):
    events, lead_events, module_events = [], {}, {}
    # The transport can deliver either the Job artifact or a module report carrying
    # exactly the same metric. Resolve compact source-local keys with the owner's codec.
    keys = {}
    for event in conn.execute("""SELECT e.event_id,e.ts,e.payload_json,t.scope FROM events e
            LEFT JOIN v6_decision_tasks t ON t.run_id=e.run_id AND t.task_id=json_extract(e.payload_json,'$.task_id')
            WHERE e.run_id=? AND e.kind=? ORDER BY e.event_id""", (rid, v623_exposure.EVENT)):
        body = json.loads(event["payload_json"])
        endpoints = []
        for proof in body["endpoints"]:
            ref = proof["source_ref"]
            if ref not in keys:
                source_metrics = metrics_by_source.get(ref)
                if source_metrics is None:
                    source_metrics = v623_endpoints.registered(conn, rid, ref)
                valid = [m for m in source_metrics if m.get("schema") == v621_metrics.SCHEMA]
                keys[ref] = {row["key"]: (row, m["metric_ref"]) for row, m in
                             zip(v623_endpoints.compact_rows(valid), valid, strict=True)}
            metric = proof.get("metric_ref") or proof.get("external_endpoint", {}).get("metric_ref")
            compact = proof.get("row")
            if compact:
                known = keys[ref].get(compact["key"])
                metric = known[1] if known and known[0] == compact else None
            endpoints.append({**proof, "metric_ref": metric})
            if metric and event["scope"]:
                received = lead_events if event["scope"] == "run" else module_events
                received.setdefault(metric, set()).add(event["event_id"])
        events.append({**body, "endpoints": endpoints, "scope": event["scope"], "event_id": event["event_id"],
                       "utc": event["ts"]})
    return events, lead_events, module_events


def evidence(conn, rid):
    predictions, exploratory = _claims(conn, rid)
    jobs, metrics = _jobs(conn, rid)
    deliveries, lead, modules = _deliveries(conn, rid, metrics)
    for job in jobs:
        for output in job["outputs"]:
            received = set()
            for m in output["metrics"]:
                m["lead_delivery_event_ids"] = sorted(lead.get(m["metric_ref"], ()))
                m["module_delivery_event_ids"] = sorted(modules.get(m["metric_ref"], ()))
                received.update(m["lead_delivery_event_ids"])
            output["lead_delivery_event_ids"] = sorted(received)
    published = {o["artifact_ref"]: o for j in jobs for o in j["outputs"]}
    external = {(m["artifact_ref"], m["measurement_spec_sha256"]): m for d in deliveries
                for proof in d["endpoints"] if (m := proof.get("external_endpoint"))}
    hypotheses = {}
    for prediction in sorted(predictions, key=lambda p: p["registration"]["event_id"]):
        hid = prediction["hypothesis_id"]
        if hid in hypotheses:
            hypotheses[hid]["revisions"].append(prediction)
        else:
            prediction["revisions"] = []
            hypotheses[hid] = prediction
        for assessment in prediction["assessments"]:
            for result in assessment.get("results", []):
                observed = result.get("measurement") or external.get((
                    result["artifact_ref"], prediction["registration"].get("measurement_spec_sha256")), {})
                result["lead_delivery_event_ids"] = sorted(lead.get(observed.get("metric_ref"), ()))
                result["unit"] = observed.get("unit") or prediction["prediction"].get("external_endpoint", {}).get("unit")
                counts = published.get(result["artifact_ref"], {}).get("denominators", {})
                result["denominators"] = {**counts, **_fields(observed, "numerator denominator")}
            for gap in assessment.get("gaps", []):
                if gap["kind"] == "unavailable_metric":
                    out = published.get(gap.get("artifact_ref"), {})
                    gap["denominators"] = out.get("denominators", {})
                    gap["unavailable_reason"] = out.get("summary", {}).get("unavailable_reason")
    return {"materials": _materials(conn, rid), "predictions": list(hypotheses.values()), "exploratory_claims": exploratory,
            "comparison_plans": _comparisons(conn, rid), "jobs": jobs, "deliveries": deliveries}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))


def _denominators(summary):
    # Native ratio measurements carry exact counts. For integer endpoints also
    # retain the counts the plugin reported alongside the scalar, including zeros.
    declared = summary.get("denominators")
    if isinstance(declared, dict):
        return declared
    counts = _fields(summary, "denominator counts numerator")
    for k, v in summary.items():
        if type(v) is int and not k.startswith(("minimum_", "maximum_", "min_", "max_")):
            if k.endswith(("_count", "_total", "_eligible", "_evaluable", "_unmeasured", "_missed_observed")):
                counts[k] = v
    return counts


def _registration_lines(p):
    lines = []
    reg, pred = p["registration"], p["prediction"]
    method = pred.get("method") or pred.get("external_endpoint") or pred.get("measurement_schema")
    lines.extend([p["scope"], "",
                  f"Registered at event {reg['event_id']}, {reg['utc']} ({reg['claim_revision_id']}). "
                  f"GX assessment {p['classification']}.",
                  p["criteria"], f"Registered rule · {pred['observable']} {pred['operator']} {pred['threshold']}.",
                  f"Method {_json(method)}. Materials {_json(reg['data'])}.", ""])
    seen = set()
    for a in p["assessments"]:
        lines.append(f"- {a['claim_revision_id']} at event {a['event_id']} ({a['utc']}) "
                     f"— {a['stage']}, {a['direction']}.")
        for result in a.get("results", []):
            ref = result["artifact_ref"]
            if ref in seen:
                continue
            seen.add(ref)
            unit = result["unit"] or "unit unrecorded"
            counts = _json(result["denominators"]) if result["denominators"] else "not reported"
            delivered = ", ".join(map(str, result["lead_delivery_event_ids"])) or "none recorded"
            lines.append(f"  - {result['value']} {unit} ({'supports' if result['supports'] else 'opposes'}). "
                         f"Source {ref}. Denominators {counts}. Lead delivery events {delivered}.")
        for gap in a.get("gaps", []):
            key = (gap["kind"], gap.get("artifact_ref"), gap.get("event_id"))
            if key not in seen:
                seen.add(key)
                value = f" ({gap['value']})" if "value" in gap else ""
                lines.append(f"  - {gap['kind']}{value} · {gap.get('artifact_ref', '')}")
                if gap.get("denominators") or gap.get("unavailable_reason"):
                    lines.append(f"    - Published counts {_json(gap.get('denominators', {}))}. "
                                 f"{gap.get('unavailable_reason') or ''}")
    lines.append("")
    return lines


def evidence_lines(data):
    lines = ["## Predictions and assessments", ""]
    if not data["predictions"]:
        lines.append("GX recorded no adopted claim with a registered prediction.")
    for p in data["predictions"]:
        lines.extend([f"### {p['statement']}", "", *_registration_lines(p)])
        for revision in p.get("revisions", []):
            lines.extend([f"#### Revised registration at event {revision['registration']['event_id']}",
                          "", revision["statement"], "", *_registration_lines(revision)])
    if data["exploratory_claims"]:
        lines.extend(["### Interpretations without a registered prediction", ""])
        for item in data["exploratory_claims"]:
            lines.append(f"- {item['claim_revision_id']} · {item['statement']} — {item['scope']}")
    for plan in data["comparison_plans"]:
        lines.extend(["", f"Comparison {plan['comparison_id']} registered at event {plan['initial_event']} "
                      f"({plan['registered_utc']}). Method {_json(plan['method'])}.",
                      f"Original comparison prediction {_json(plan.get('prediction'))}."])
    if data["materials"]:
        lines.extend(["", "## Materials and sources", ""])
    for material in data["materials"]:
        lines.append(f"- {material.get('role', 'Observation')} · {material['artifact_ref']} "
                     f"(SHA-256 {material.get('sha256') or 'unrecorded'}).")
        for origin in material.get("provenance", []):
            acquisition = origin.get("acquisition") or {}
            identity = acquisition.get("dependencies", {}).get("source", [])
            lines.append(f"  - {origin.get('source', '')} at event {origin['event_id']} · "
                         f"{', '.join(identity)}. {acquisition.get('applicability') or ''}")
    lines.extend(["", "## Measurements", ""])
    for job in data["jobs"]:
        method = job["method"]
        lines.extend([f"### {job['job_id']} · {job['status']}", "",
                      f"{method['id']}@{method['version']} · {_json(job['parameters'])}",
                      f"Inputs {_json(job['inputs'])}.", ""])
        for out in job["outputs"]:
            deliveries = ", ".join(map(str, out["lead_delivery_event_ids"])) or "none recorded"
            lines.append(f"- {out['output_name']} · {out['artifact_ref']} (SHA-256 {out['sha256']}). "
                         f"Lead endpoint delivery events {deliveries}.")
            if out.get("summary"):
                lines.append(f"  - Published summary {_json(out['summary'])}")
            for m in out["metrics"]:
                value = m.get("display", m.get("reason", "missing"))
                received = ", ".join(map(str, m["lead_delivery_event_ids"])) or "none recorded"
                lines.append(f"  - {m['metric_id']} · {value} ({m['metric_ref']}). "
                             f"Lead delivery events {received}.")
        lines.append("")
    return lines
