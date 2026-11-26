"""Extract and cite numerical findings from a named, sealed Job output."""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation, localcontext
import io
import json
import math
import re

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import GxError, SchemaInvalid, ScopeDenied
from . import manifest
from .v62_evidence import _fact

SCHEMA = "gx.verified_metric/1"
MISSING = "gx.metric_unavailable/1"
REPORT_SCHEMA = "gx.v622.module_report_bundle.1"
OPERANDS = {"integer/1": ("pointer",), "decimal/1": ("pointer",),
            "ratio/1": ("numerator", "denominator"), "difference/1": ("left", "right")}
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
TAG = re.compile(r"\{\{metric:([^{}]+)\}\}")
NUMBER = re.compile(r"(?<![a-zA-Z_\d])[+−-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")
# File names, accessions, calendar years and document labels are not scientific measurements.
IDENTIFIER = re.compile(r"(?<!\w)(?:[a-zA-Z0-9_.-]+\.(?:cif|pdb|csv|json|txt|pdf|xml)|"
                        r"[0-9][A-Z][A-Z0-9]{2})(?!\w)")
CALENDAR_YEAR = re.compile(r"\b(?:from|in|published|released|deposited|year)\s+(?:19|20)\d{2}\b"
                           r"(?=\s*(?:[,.;:)](?!\d)|$|and\b|with\b|by\b))", re.I)
METRIC_READER_BYTES = 2_000_000


def definition_errors(specs):
    if not isinstance(specs, dict) or not 1 <= len(specs) <= 32:
        return ["metric_specs must name between one and 32 metrics"]
    errors = []
    for name, spec in specs.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(spec, dict):
            errors.append("Invalid metric name or declaration")
            continue
        operands = OPERANDS.get(spec.get("op")) if isinstance(spec.get("op"), str) else None
        reader = spec.get("reader")
        if (not operands or reader not in ("json/1", "csv-row/1") or
                not isinstance(spec.get("unit"), str) or not spec["unit"].strip() or
                type(spec.get("places")) is not int or not 0 <= spec["places"] <= 12):
            errors.append(f"{name}: unsupported formula, reader, unit or display precision")
            continue
        fields = {"op", "reader", "unit", "places", "scope", "bounds", "source_unit", "scale", "result_pointer", *operands}
        scope = spec.get("scope", {})
        result_pointer = spec.get("result_pointer")
        if "result_pointer" in spec and (not isinstance(result_pointer, str) or not result_pointer.startswith("/")):
            errors.append(f"{name}: invalid result field selector")
            continue
        if (spec.keys() - fields or not isinstance(scope, dict) or
                any(not isinstance(k, str) or not NAME.fullmatch(k) for k in scope) or
                any(not isinstance(p, str) or not p.startswith("/") for p in
                    [*(spec.get(k) for k in operands), *scope.values()])):
            errors.append(f"{name}: invalid operand or scope selector")
            continue
        try:
            if ("scale" in spec or "source_unit" in spec) and (
                    not isinstance(spec.get("source_unit"), str) or not spec["source_unit"] or
                    _number(spec.get("scale")) <= 0 or spec["op"] not in ("decimal/1", "difference/1")):
                raise ValueError()
            if "bounds" in spec:
                lo, hi = spec["bounds"]
                if not isinstance(spec["bounds"], list) or _number(lo) > _number(hi):
                    raise ValueError()
            if spec["op"] == "integer/1" and spec["places"] != 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append(f"{name}: invalid scale, bounds or count precision")
    return errors


def _number(v):
    if type(v) not in (int, float, Decimal):
        raise ValueError("expected a numeric observation")
    n = Decimal(str(v))
    if not n.is_finite() or abs(n.adjusted()) > 308 or len(n.as_tuple().digits) > 1024:
        raise ValueError("nonfinite or unsupported numeric observation")
    return n


def _read(raw, reader, name, media):
    media = (media or "").split(";")[0].lower()
    generic = media in ("", "application/octet-stream", "text/plain")
    if reader == "json/1" and (media == "application/json" or media.endswith("+json") or generic and name.endswith(".json")):
        return json.loads(raw, parse_float=Decimal)
    if reader == "csv-row/1" and (media == "text/csv" or generic and name.endswith(".csv")):
        rows = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        header, row = next(rows, []), next(rows, [])
        if (not row or len(header) != len(row) or not all(header) or len(set(header)) != len(header)
                or next(rows, None) is not None):
            raise ValueError("single-record CSV projection requires one unambiguous header and record")
        def cell(v):
            if re.fullmatch(r"[+-]?\d+", v):
                return int(v)
            try:
                return Decimal(v)
            except InvalidOperation:
                return v
        return {k: cell(v) for k, v in zip(header, row)}
    raise ValueError("no compatible metric reader for the output format")


def _json_number(value):
    if value == value.to_integral_value():
        return int(value)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("numeric observation exceeds the supported range")
    return number


def _scope_value(value):
    if isinstance(value, list):
        return [_scope_value(v) for v in value]
    if type(value) in (Decimal, int, float):
        return _json_number(_number(value))
    if type(value) in (str, bool):
        return value
    raise ValueError("selection scope must be a finite scalar or list")


def calculate(spec, document):
    """The only rounding here is the final displayed representation."""
    operands = {k: _fact(document, spec[k]) for k in OPERANDS[spec["op"]]}
    scope = {k: _scope_value(_fact(document, p)) for k, p in spec.get("scope", {}).items()}
    with localcontext() as ctx:
        ctx.prec = 50
        values = {k: _number(v) for k, v in operands.items()}
        if spec["op"] == "ratio/1":
            n, d = operands["numerator"], operands["denominator"]
            if type(n) is not int or type(d) is not int or n < 0 or d <= 0:
                raise ValueError("count ratio needs exact nonnegative count and positive denominator")
            raw = values["numerator"] / values["denominator"]
        elif spec["op"] == "difference/1":
            raw = values["left"] - values["right"]
        else:
            if spec["op"] == "integer/1" and (type(operands["pointer"]) is not int or operands["pointer"] < 0):
                raise ValueError("integer measurement requires an integer field")
            raw = values["pointer"]
        raw *= _number(spec.get("scale", 1))
        if "bounds" in spec and not (_number(spec["bounds"][0]) <= raw <= _number(spec["bounds"][1])):
            raise ValueError("measurement outside declared bounds")
        display = format(raw, f".{spec['places']}f")
        if spec["op"] == "ratio/1":
            display = f"{n}/{d} ({display})"
        elif spec["unit"] != "1":
            display += " " + spec["unit"]
        return dict(op=spec["op"], reader=spec["reader"], unit=spec["unit"], places=spec["places"],
                    selectors={k: spec[k] for k in operands}, scope=scope, value=_json_number(raw),
                    raw_value=str(raw), display=display,
                    **{k: _json_number(v) for k, v in values.items() if k != "pointer"})


def from_output(conn, rid, ref):
    """Called only after the owning scope has admitted this ref for reading."""
    from . import v62_outputs
    assets = ArtifactService(conn)
    meta = assets.describe(ref)
    if meta["run_id"] != rid:
        raise ScopeDenied("Metric outside the presented Run")
    if meta["origin"] != "JOB_OUTPUT" or not meta["job_id"]:
        return []
    job_id = meta["job_id"]
    sealed = manifest.read_manifest(conn, job_id, missing_ok=True)
    if not sealed or not sealed["completion"] or sealed["completion"]["status"] != "SUCCEEDED":
        return []
    adm = sealed["admission"]
    output = next((o for o in sealed["completion"]["outputs"] if o["artifact_ref"] == ref), None)
    if not output:
        return []
    name = output["output_name"]
    decl = next((d for d in adm.get("output_declarations", []) if d["name"] == name), {})
    specs = decl.get("metric_specs")
    if not specs:
        return []
    if adm["run_id"] != rid or meta["sha256"] != output["sha256"]:
        raise SchemaInvalid("Metric publication differs from original named output")
    binding = v62_outputs._binding(adm, output)
    if (output.get("runner_sha256", output["sha256"]) != output["sha256"] or
            any(output.get(k) != v for k, v in binding.items())):
        raise SchemaInvalid("Metric publication differs from admitted output binding")
    proof = dict(artifact_ref=ref, sha256=meta["sha256"], job_id=job_id, output_name=name,
                 method=adm["capability"], admission_sha256=sealed["admission_hash"],
                 manifest_sha256=sealed["manifest_hash"], provenance_cursor=adm.get("provenance_cursor"),
                 inputs=binding["source_inputs"])
    readers = {}
    metrics = []
    for key, spec in specs.items():
        item = dict(metric_id=key, **proof)
        try:
            reader = spec["reader"]
            if reader not in readers:
                readers[reader] = _read(assets.read_bytes(ref, max_bytes=METRIC_READER_BYTES,
                                                       expected_sha256=output["sha256"]), reader, name, meta["media_type"])
            fields = calculate(spec, readers[reader])
            if any(k in adm["parameters"] and adm["parameters"][k] != v for k, v in fields["scope"].items()):
                raise ValueError("output scope differs from admitted method parameters")
            if "input_sha256" in fields["scope"] and fields["scope"]["input_sha256"] not in {
                    p["sha256"] for p in proof["inputs"]}:
                raise ValueError("output identifies another scientific input")
            item.update(schema=SCHEMA, **fields)
        except (GxError, ValueError, ArithmeticError, KeyError, IndexError, TypeError, UnicodeError, csv.Error) as exc:
            item.update(schema=MISSING, reason=str(exc), reader=spec["reader"])
        item["metric_ref"] = "metric_" + ids.sha256_of_obj(item)
        metrics.append(item)
    return metrics


def _cited(conn, rid, refs, cache=None):
    assets = ArtifactService(conn)
    found = {}
    cache = {} if cache is None else cache
    for ref in dict.fromkeys(refs):
        if ref in cache:
            found.update(cache[ref])
            continue
        # Goal evidence can also cite source-event and prior decision identities.
        row = conn.execute("SELECT kind,run_id FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
        if row is None:
            continue
        if row["run_id"] != rid:
            raise ScopeDenied("Metric source belongs to another Run")
        if row["kind"] == "module_report_bundle":
            record = conn.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? AND report_ref=?",
                                  (rid, ref)).fetchone()
            if not record:
                continue
            receipt = json.loads(record[0])
            raw = assets.read_bytes(ref, expected_sha256=receipt["report_sha256"])
            doc = json.loads(raw)
            entries = doc.get("verified_metrics", []) if doc.get("schema") in (REPORT_SCHEMA, "gx.v623.module_report_bundle.1") else doc.get("metrics", [])
        else:
            entries = from_output(conn, rid, ref)
        cache[ref] = {e["metric_ref"]: e for e in entries}
        found.update(cache[ref])
    return list(found.values())


def _method_identifiers(conn, rid):
    from .v62_catalog import _inputs
    inputs = _inputs(conn, rid)
    cards = [*inputs.get("capability_catalog", ()), *inputs.get("research_catalog", {}).get("public_methods", ())]
    return {m["capability_version"] for m in cards if m.get("capability_version")}


def _render(text, metrics, methods=(), *, protected=(), used=None):
    if isinstance(text, list):
        return [_render(part, metrics, methods, used=used) for part in text]
    if not isinstance(text, str):
        raise SchemaInvalid("Quantitative interpretation must be text")
    checked = text
    for start, end in sorted(protected, reverse=True):
        checked = checked[:start] + " " + checked[end:]
    without_tags = TAG.sub("", checked)
    if "@" in without_tags:
        without_tags = re.sub(r"(?<![\w.-])[A-Za-z][A-Za-z0-9_.-]*@[0-9]+(?:\.[0-9]+)*",
                              lambda m: "" if m[0] in methods else m[0], without_tags)
    def identifier(match):
        # An accession such as 2E19 is also a valid numeric literal; its use as an identifier needs context.
        if re.fullmatch(r"\d[Ee]\d{2}", match[0]) and not re.search(
                r"\b(?:pdb|entry|structure|accession)\s+$", without_tags[:match.start()], flags=re.I):
            return match[0]
        return ""
    prose = CALENDAR_YEAR.sub("", IDENTIFIER.sub(identifier, without_tags))
    for metric in metrics:
        for key, value in metric.get("scope", {}).items():
            if isinstance(value, (str, int)) and str(value).isdecimal():
                prose = re.sub(r"\b" + re.escape(key) + r"\s+" + re.escape(str(value)) + r"\b", "", prose, flags=re.I)
    if NUMBER.search(prose):
        raise SchemaInvalid("Cite a declared metric for scientific numbers, for example {{metric:coverage}}")
    return _substitute_tags(text, metrics, without_tags, used=used)


def _substitute_tags(text, metrics, without_tags, *, used=None, report=False):
    single = len(TAG.findall(text)) == 1

    def replace(match):
        key, *checks = match[1].split("|")
        ref, _, name = key.rpartition(":")
        choices = [m for m in metrics if m["metric_id"] == name and (not ref or m["artifact_ref"] == ref)]
        if len(choices) != 1:
            raise SchemaInvalid("Metric template needs one available metric on its cited source")
        item = choices[0]
        if item["schema"] != SCHEMA:
            raise SchemaInvalid("The declared metric has no usable reader result", details={"metric": item})
        values = {**item["scope"], **{k: item[k] for k in ("unit", "display", "numerator", "denominator") if k in item}}
        for check in checks:
            attr, eq, expected = check.partition("=")
            if not eq or attr not in values or str(values[attr]) != expected:
                raise SchemaInvalid("Metric assertion differs from the named output's value, unit or scope")
        # A physical unit immediately appended to a rendered number is part of the quantitative assertion.
        suffix = re.match(r"\s*(%|[µμmunpkMGT]?(?:V|A|g|m|s|K|Pa|Hz|L|mol|eV)|Å)(?![\w])", text[match.end():])
        if suffix and suffix[1] != item["unit"]:
            raise SchemaInvalid("Metric suffix uses a different unit than the named output")
        if single:
            for field, actual in item["scope"].items():
                if isinstance(actual, str):
                    declared = re.findall(r"\b" + re.escape(field) + r"\s*[:=]\s*([\w.-]+)", without_tags, re.I)
                    if any(v.rstrip(".") != actual for v in declared):
                        raise SchemaInvalid("Metric prose declares a different selection scope")
        if used is not None:
            used[item["metric_ref"]] = item
        return item["display"]
    def project(match):
        try:
            return replace(match)
        except SchemaInvalid as exc:
            return f"[Unresolved metric {match[1]}: {exc.message}]"

    rendered = TAG.sub(project if report else replace, text)
    if report:
        return rendered.replace("{{metric:", "[Malformed metric citation] ")
    if "{{metric:" in rendered:
        raise SchemaInvalid("Malformed scientific metric citation")
    return rendered


def render_accepted_text(text, metrics):
    """Project citations in already accepted prose while preserving its wording and identifiers."""
    if "{{metric:" not in text:
        return text
    return _substitute_tags(text, metrics, TAG.sub("", text), report=True)


PREDICATES = {"gt": (">", r">(?!=)|above|greater than|exceeds?"),
              "ge": (">=", r">=|≥|at least"), "lt": ("<", r"<(?!=)|below|less than"),
              "le": ("<=", r"<=|≤|at most"), "eq": ("=", r"==|(?<![<>])=|equals?")}


def _criterion(text, prediction, metrics, methods=(), *, used=None):
    # Only a comparator with its prospective numeric RHS can be stated literally.
    symbol, words = PREDICATES[prediction["operator"]]
    cutoff = prediction["threshold"]
    pattern = re.compile(r"(?P<op>" + words + r")(?P<space>\s*)(?P<n>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)"
                         r"(?:\.\d+)?(?:[eE][+-]?\d+)?)\b", re.I)
    def substitute(match):
        if Decimal(match["n"].replace(",", "")) != _number(cutoff):
            raise SchemaInvalid("Numerical criterion differs from the registered prediction threshold")
        return match["op"] + match["space"] + "{{cutoff}}"
    if "{{cutoff}}" in text:
        raise SchemaInvalid("Use {{criterion}} to cite the registered comparison")
    text = _render(pattern.sub(substitute, text), metrics, methods, used=used)
    return text.replace("{{cutoff}}", str(cutoff)).replace(
        "{{criterion}}", f"{prediction['observable']} {symbol} {cutoff}")


def render_fields(conn, run_id, evidence_refs, text_fields, *, allowed_context=None, prediction=None, used=None):
    """Render already-authorized scientific prose from pinned metrics, retaining their full receipts."""
    metrics = _cited(conn, run_id, evidence_refs)
    methods = _method_identifiers(conn, run_id) if any("@" in field for field in text_fields.values()) else ()
    # Whole phrases can be independently established by the claim owner's source and scope checks.
    trusted = allowed_context or {}
    result = {}
    for key, value in text_fields.items():
        if key in trusted and trusted[key] == value:
            result[key] = value
            continue
        if key == "criteria" and prediction:
            result[key] = _criterion(value, prediction, metrics, methods, used=used)
        else:
            if "{{criterion}}" in value:
                raise SchemaInvalid("A criterion citation requires a prospective structured prediction")
            result[key] = _render(value, metrics, methods, used=used)
    return result, metrics


def measured_statement(metrics):
    """A shareable numerical statement whose entire value and label come from the method."""
    return "; ".join(m["metric_id"] + " = " + m["display"] for m in metrics) + "."


def render_report(conn, rid, bundle, *, current=False, auto_endpoints=False, metric_refs=(), qualitative_notes=()):
    """Only a finding's own citations may supply its numerical statements."""
    cache = {}
    methods = _method_identifiers(conn, rid)
    fields = {}
    selected = {}
    quotations = []
    reads = None
    for finding in bundle["findings"]:
        quote = None
        entries = _cited(conn, rid, finding["source_refs"], cache)
        used = {}
        try:
            text = _render(finding["text"], entries, methods, used=used)
        except SchemaInvalid as error:
            # A verified quotation is a source attribution, never an executed measurement.
            from . import v62_native, v62_sources
            if reads is None:
                reads = v62_native.operation_receipts(conn, rid, bundle["module_instance_id"])
            quote = v62_sources.report_quotation(conn, rid, bundle["scope"], finding, reads)
            if quote:
                try:
                    text = _render(finding["text"], entries, methods, protected=[quote["finding_span"]], used=used)
                except SchemaInvalid as error:
                    if current and "metric" not in error.details:
                        error.details["category"] = "module_report_structure"
                    raise
            elif auto_endpoints and entries and "{{metric:" not in finding["text"]:
                # Free observations, including an incorrect literal, stay available as notes.
                # Their formatting need not consume another model attempt.
                text = finding["text"]
            else:
                if current and "metric" not in error.details:
                    error.details["category"] = "module_report_structure"
                raise
        if current and any(m["artifact_ref"] not in finding["source_refs"] for m in used.values()):
            raise ScopeDenied("A module measurement must cite its own authorized named output")
        selected.update(used)
        findings = fields.setdefault("findings", [])
        if not current:
            if quote:
                quotations.append(dict(finding_index=len(findings), **quote))
            findings.append({**finding, "text": text})
            continue
        if entries and used:
            findings.append({**finding, "text": measured_statement(used.values()), "metric_refs": list(used)})
        if quote:
            # A missing/omitted metric may already have removed an earlier draft finding.
            # The locator must point at the published text, including for mixed sources.
            if entries:
                start, end = quote["finding_span"]
                text = finding["text"][start:end]
                quote = {**quote, "finding_span": [0, len(text)]}
            quotations.append(dict(finding_index=len(findings), **quote))
            findings.append({**finding, "text": text})
        elif not entries:
            findings.append({**finding, "text": text, "evidence_status": "UNVERIFIED"})
        elif not used and finding.get("kind") == "counterevidence":
            findings.append({**finding, "text": "A qualitative note flags this source for review.",
                             "evidence_status": "UNVERIFIED"})
    if quotations:
        bundle["source_quotations"] = quotations
    metrics = _cited(conn, rid, [s["artifact_ref"] for s in bundle["sources"]], cache)
    bundle.update(schema="gx.v621.module_report_bundle.1", metrics=metrics,
                  interpretation={"findings": bundle["findings"], "gaps": bundle["gaps"], "next_steps": bundle["next_steps"]})
    if current:
        for note in qualitative_notes:
            if note.get("kind") == "counterevidence":
                fields.setdefault("findings", []).append({**note, "text": "A qualitative note flags this source for review.",
                                                         "evidence_status": "UNVERIFIED"})
        presented = set(selected)
        for selector in metric_refs:
            ref, name = selector["artifact_ref"], selector["metric_id"]
            entry = next((m for m in _cited(conn, rid, [ref], cache)
                          if m["artifact_ref"] == ref and m["metric_id"] == name), None)
            if entry is None:
                raise SchemaInvalid("Select a declared metric on the cited named Job output",
                                    details={"category": "module_report_structure"})
            if entry["schema"] != SCHEMA:
                raise SchemaInvalid("The declared metric has no usable reader result", details={"metric": entry})
            selected[entry["metric_ref"]] = entry
        if auto_endpoints:
            # A released module may only publish evidence it owns. An inherited report is
            # useful context but cannot stand in for a separately authorized named output.
            originals = {source["artifact_ref"] for source in bundle["sources"]}
            for m in metrics:
                if m["schema"] == SCHEMA and m["artifact_ref"] in originals:
                    selected.setdefault(m["metric_ref"], m)
        for ref, metric in selected.items():
            if ref not in presented:
                fields.setdefault("findings", []).append({"text": measured_statement([metric]),
                    "metric_refs": [ref], "source_refs": [metric["artifact_ref"]]})
        bundle["schema"] = "gx.v623.module_report_bundle.1" if auto_endpoints else REPORT_SCHEMA
        bundle["verified_metrics"] = list(selected.values())
        if auto_endpoints:
            bundle["endpoint_total"] = len(selected)
            bundle["endpoint_state"] = "AVAILABLE" if selected else "NO_VERIFIED_ENDPOINTS"
        bundle["metrics"] = bundle["verified_metrics"]
        bundle["qualitative_notes"] = {"status": "UNVERIFIED", "findings": [*bundle["findings"], *qualitative_notes],
                                       "gaps": list(bundle["gaps"]), "next_steps": list(bundle["next_steps"])}
        # Keep the legacy interpretation shape limited to pre-6.2.2 reports.
        del bundle["interpretation"]
        fields.setdefault("findings", [])
    bundle.update(fields)
    for key in ("gaps", "next_steps"):
        try:
            bundle[key] = _render(bundle[key], metrics, methods)
        except SchemaInvalid as error:
            if current and "metric" not in error.details:
                error.details["category"] = "module_report_structure"
            raise
