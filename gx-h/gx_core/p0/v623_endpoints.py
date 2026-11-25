"""Report-scoped measurement summaries and pages derived from frozen named Job outputs."""
import json

from ..domain import ids
from ..errors import SchemaInvalid
from . import v621_metrics

POLICY = "gx.evidence/6.2.3"
REPORT = "gx.v623.module_report_bundle.1"
PAGE = "gx.v623.endpoint_page/1"
PAGE_BYTES = 4096


def size(value):
    return len(ids.canonical_json(value).encode("utf-8"))


def registered(conn, run_id, source_ref):
    """The caller checks visibility. Match the immutable Job or published report in this Run."""
    return [m for m in v621_metrics._cited(conn, run_id, [source_ref])
            if m.get("schema") == v621_metrics.SCHEMA]


def compact_rows(metrics):
    # Report/Job namespace plus digest prefix identifies even repeated names across specimens.
    # Expand colliding prefixes rather than silently mapping an alias to the wrong measurement.
    prefixes = {}
    for m in metrics:
        digest = m["metric_ref"].removeprefix("metric_")
        prefixes.setdefault(digest[:12], []).append(digest)
    result = []
    for m in metrics:
        digest = m["metric_ref"].removeprefix("metric_")
        length = 12
        while any(other != digest and other[:length] == digest[:length] for other in prefixes[digest[:12]]):
            length += 1
        result.append({"key": digest[:length], "id": m["metric_id"], "value": m["display"], "unit": m["unit"]})
    return result


def comparison_keys(conn, metrics, comparisons):
    """Source-local rows matching the registered method, named output and frozen Job settings."""
    if not metrics or not comparisons:
        return set()
    from ..capabilities import manifest, registry
    from ..errors import GxError

    matched = set()
    parameters = {}
    defaults = {}
    for metric, row in zip(metrics, compact_rows(metrics), strict=True):
        method = metric["method"]
        capability = f"{method['id']}@{method['version']}"
        for comparison in comparisons:
            spec = comparison["method"]
            if (spec["capability"] != capability or spec["output_name"] != metric["output_name"]
                    or spec["metric_id"] != metric["metric_id"] or spec["unit"] != metric["unit"]
                    or comparison.get("method_manifest_sha256", method["sha256"]) != method["sha256"]):
                continue
            job = (metric["job_id"], metric["admission_sha256"])
            if job not in parameters:
                saved = conn.execute("""SELECT json_extract(admission_json,'$.parameters')
                    FROM p0_execution_manifests WHERE job_id=? AND admission_sha256=?""", job).fetchone()
                parameters[job] = json.loads(saved[0]) if saved and saved[0] is not None else None
            actual = parameters[job]
            expected = spec["parameters"]
            if actual is None:
                continue
            if expected.keys() < actual.keys():
                # A registration may omit defaults explicitly declared by this exact executable.
                if method["sha256"] not in defaults:
                    try:
                        bundle = registry.load_bundle(conn, method["id"], method["version"], expected_sha256=method["sha256"])
                        defaults[method["sha256"]] = manifest.effective_parameters(bundle["manifest"], {})
                    except GxError:
                        defaults[method["sha256"]] = {}
                expected = {**defaults[method["sha256"]], **expected}
            if ids.canonical_json(actual) == ids.canonical_json(expected):
                matched.add(row["key"])
                break
    return matched


def page(conn, run_id, source, start):
    if type(start) is not int or start < 0:
        raise SchemaInvalid("endpoint_offset must be a nonnegative row offset")
    metrics = registered(conn, run_id, source)
    if start > len(metrics):
        raise SchemaInvalid("endpoint_offset exceeds the number of registered endpoints")
    rows = compact_rows(metrics)
    result = dict(schema=PAGE, source_ref=source, endpoint_offset=start, endpoint_total=len(rows),
                  endpoint_rows=[], verified_metrics=[], next_endpoint_offset=None)
    for i in range(start, min(start + 3, len(rows))):
        result["next_endpoint_offset"] = i + 1 if i + 1 < len(rows) else None
        result["endpoint_rows"].append(rows[i])
        if size(result) > PAGE_BYTES and len(result["endpoint_rows"]) > 1:
            result["endpoint_rows"].pop()
            result["next_endpoint_offset"] = i
            break
        result["verified_metrics"].append(metrics[i])
        if size(result) > PAGE_BYTES:
            result["verified_metrics"].pop()
    return result


def fit(target, rows, total, fits):
    """Insert complete rows under the caller's UTF-8 limit. Never clip a numerical value."""
    target.update(endpoint_total=total, pending_endpoints=bool(total), endpoint_rows=[])
    for row in rows:
        target["endpoint_rows"].append(row)
        target["pending_endpoints"] = len(target["endpoint_rows"]) < total
        if not fits():
            target["endpoint_rows"].pop()
            target["pending_endpoints"] = True
    if not target["endpoint_rows"]:
        target.pop("endpoint_rows")
