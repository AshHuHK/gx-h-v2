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
OPERANDS = {"integer/1": ("pointer",), "decimal/1": ("pointer",),
            "ratio/1": ("numerator", "denominator"), "difference/1": ("left", "right")}
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
TAG = re.compile(r"\{\{metric:([^{}]+)\}\}")
NUMBER = re.compile(r"(?<![a-zA-Z_\d])[+−-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")
# File names, accessions, calendar years and document labels are not scientific measurements.
IDENTIFIER = re.compile(r"(?<!\w)(?:[a-zA-Z0-9_.-]+\.(?:cif|pdb|csv|json|txt|pdf|xml)|"
                        r"[0-9][A-Z][A-Z0-9]{2})(?!\w)")
CALENDAR_YEAR = re.compile(r"\b(?:from|in|published|released|deposited|year)\s+(?:19|20)\d{2}\b(?![./]\d)", re.I)
METRIC_READER_BYTES = 2_000_000


def definition_errors(specs):
    if not isinstance(specs, dict) or not 1 <= len(specs) <= 32:
        return ["metric_specs must name between one and 32 metrics"]
    errors = []
    for name, spec in specs.items():
        if not NAME.fullmatch(name) or not isinstance(spec, dict):
            errors.append("Invalid metric name or declaration")
            continue
        operands = OPERANDS.get(spec.get("op")) if isinstance(spec.get("op"), str) else None
        reader = spec.get("reader")
        if (not operands or reader not in ("json/1", "csv-row/1") or
                not isinstance(spec.get("unit"), str) or not spec["unit"].strip() or
                type(spec.get("places")) is not int or not 0 <= spec["places"] <= 12):
            errors.append(f"{name}: unsupported formula, reader, unit or display precision")
            continue
        fields = {"op", "reader", "unit", "places", "scope", "bounds", "source_unit", "scale", *operands}
        scope = spec.get("scope", {})
        if (spec.keys() - fields or not isinstance(scope, dict) or
                any(not NAME.fullmatch(k) for k in scope) or
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
    if not n.is_finite():
        raise ValueError("nonfinite observation")
    return n


def _read(raw, reader, name, media):
    media = (media or "").split(";")[0].lower()
    if reader == "json/1" and (media == "application/json" or media.endswith("+json") or name.endswith(".json")):
        return json.loads(raw, parse_float=Decimal)
    if reader == "csv-row/1" and (media == "text/csv" or name.endswith(".csv")):
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


def calculate(spec, document):
    """The only rounding here is the final displayed representation."""
    operands = {k: _fact(document, spec[k]) for k in OPERANDS[spec["op"]]}
    scope = {k: _fact(document, p) for k, p in spec.get("scope", {}).items()}
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
            if spec["op"] == "integer/1" and type(operands["pointer"]) is not int:
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
                 manifest_sha256=sealed["manifest_hash"], inputs=binding["source_inputs"])
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


def _cited(conn, rid, refs):
    assets = ArtifactService(conn)
    found = {}
    for ref in dict.fromkeys(refs):
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
            entries = json.loads(raw).get("metrics", [])
        else:
            entries = from_output(conn, rid, ref)
        for entry in entries:
            found[entry["metric_ref"]] = entry
    return list(found.values())


def _render(text, metrics):
    if isinstance(text, list):
        return [_render(part, metrics) for part in text]
    if not isinstance(text, str):
        raise SchemaInvalid("Quantitative interpretation must be text")
    if metrics:
        prose = CALENDAR_YEAR.sub("", IDENTIFIER.sub("", TAG.sub("", text)))
        for metric in metrics:
            for key, value in metric.get("scope", {}).items():
                if isinstance(value, (str, int)) and str(value).isdecimal():
                    prose = re.sub(r"\b" + re.escape(key) + r"\s+" + re.escape(str(value)) + r"\b", "", prose, flags=re.I)
        if NUMBER.search(prose):
            raise SchemaInvalid("Cite a declared metric for scientific numbers, for example {{metric:coverage}}")
    def replace(match):
        key, *checks = match[1].split("|")
        ref, _, name = key.rpartition(":")
        choices = [m for m in metrics if m["metric_id"] == name and (not ref or m["artifact_ref"] == ref)]
        if len(choices) != 1 or choices[0]["schema"] != SCHEMA:
            raise SchemaInvalid("Metric template needs one available metric on its cited source")
        item = choices[0]
        values = {**item["scope"], **{k: item[k] for k in ("unit", "display", "numerator", "denominator") if k in item}}
        for check in checks:
            attr, eq, expected = check.partition("=")
            if not eq or attr not in values or str(values[attr]) != expected:
                raise SchemaInvalid("Metric assertion differs from the named output's value, unit or scope")
        return item["display"]
    rendered = TAG.sub(replace, text)
    if "{{metric:" in rendered:
        raise SchemaInvalid("Malformed scientific metric citation")
    return rendered


def render_fields(conn, run_id, evidence_refs, text_fields):
    """Render already-authorized scientific prose from pinned metrics, retaining their full receipts."""
    metrics = _cited(conn, run_id, evidence_refs)
    return {k: _render(value, metrics) for k, value in text_fields.items()}, metrics


def render_report(conn, rid, bundle):
    """Only a finding's own citations may supply its numerical statements."""
    fields = {}
    for finding in bundle["findings"]:
        rendered, _ = render_fields(conn, rid, finding["source_refs"], {"text": finding["text"]})
        fields.setdefault("findings", []).append({**finding, **rendered})
    metrics = _cited(conn, rid, [s["artifact_ref"] for s in bundle["sources"]])
    bundle.update(schema="gx.v621.module_report_bundle.1", metrics=metrics,
                  interpretation={"findings": bundle["findings"], "gaps": bundle["gaps"], "next_steps": bundle["next_steps"]})
    bundle.update(fields)
    for key in ("gaps", "next_steps"):
        bundle[key] = _render(bundle[key], metrics)
